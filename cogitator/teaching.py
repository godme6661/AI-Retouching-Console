"""教学模式：让视觉模型以摄影讲师视角评价照片，并给出可执行的提升建议。

按用户裁决，评价**只依据视觉模型读图**；未配置支持读图的模型时明确报错、不静默降级。
可选地把模型给出的修图指令一键应用到工程（走同一套算子校验）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llm, ops, prompts, skills
from .document import DocError, Project

SCORE_LABELS = {
    "exposure": "曝光",
    "focus": "对焦清晰度",
    "color": "白平衡与色彩",
    "composition": "构图",
    "lighting": "用光",
    "impact": "表现力",
    "overall": "综合",
}


@dataclass
class Critique:
    summary: str = ""
    scores: dict = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    shooting_tips: list[str] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    edits_note: str = ""
    applied: list[dict] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    target: str = "original"
    model: str = ""
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)
    error: str = ""
    # 解析容错的如实记录（例如回答被长度上限截断、做过一轮 JSON 修复重问）
    parse_note: str = ""
    retried: bool = False

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "scores": self.scores,
            "score_labels": SCORE_LABELS,
            "strengths": self.strengths,
            "issues": self.issues,
            "shooting_tips": self.shooting_tips,
            "edits": self.edits,
            "edits_note": self.edits_note,
            "applied": self.applied,
            "loaded": self.loaded,
            "target": self.target,
            "model": self.model,
            "elapsed": round(self.elapsed, 2),
            "usage": self.usage,
            "error": self.error,
            "parse_note": self.parse_note,
            "retried": self.retried,
        }


def _parse_with_one_repair(client, messages: list[dict], res, result: "Critique",
                           max_tokens: int = 4096) -> tuple[dict, str]:
    """解析模型返回；失败时做**一轮**「只输出合法 JSON」的修复重问。

    为什么值得单独重问：教学模式一次要输出 summary + 7 项评分 + 优点 + 问题 + 建议，
    是本项目里最长的结构化回答，最容易踩到"格式不对/被截断"。旧实现只抛错让用户重试，
    而模型往往在明确要求下就能给出干净 JSON。
    """
    finish = str(getattr(res, "finish_reason", "") or "").lower()

    def _notes_of(p: llm.ParseResult, extra: str = "") -> str:
        parts: list[str] = []
        if extra:
            parts.append(extra)
        if p.truncated:
            parts.append("回答被长度上限截断，已采用其中完整写好的部分")
        if p.repaired:
            parts.append("自动修正了：" + "、".join(p.repaired))
        if finish == "length" and not p.truncated:
            parts.append("回答触达长度上限")
        return "；".join(parts)

    try:
        parsed = llm.parse_json_reply_ex(res.text)
        return parsed.data, _notes_of(parsed)
    except llm.LLMError as first_err:
        condense = finish == "length"
        fix_messages = list(messages) + [
            {"role": "assistant", "content": res.text[:4000]},
            {"role": "user", "content": prompts.build_json_repair_ask(str(first_err), condense=condense)},
        ]
        result.retried = True
        try:
            res2 = client.chat(fix_messages, json_mode=True, temperature=0.0,
                               max_tokens=max(max_tokens, 4096))
        except llm.LLMError as call_err:
            raise llm.LLMError(
                ("回答被长度上限截断；" if condense else "")
                + f"模型未按 JSON 格式返回，重问也失败（{call_err}）。") from None
        try:
            parsed2 = llm.parse_json_reply_ex(res2.text)
        except llm.LLMError as second_err:
            raise llm.LLMError(
                ("回答被长度上限截断（内容太长）。" if condense else "")
                + f"模型未按 JSON 格式返回，重问一次仍未成功。{second_err}") from None
        if res2.usage:
            result.usage = res2.usage
        return parsed2.data, _notes_of(parsed2, extra="已重问一次并成功解析")


def _clean_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def _clean_issues(v: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(v, str):
        return [{"title": "问题", "detail": v, "fix": ""}]
    for item in (v or []):
        if isinstance(item, dict):
            out.append({
                "title": str(item.get("title") or item.get("name") or "问题")[:60],
                "detail": str(item.get("detail") or item.get("desc") or item.get("description") or "")[:600],
                "fix": str(item.get("fix") or item.get("advice") or item.get("suggestion") or "")[:400],
            })
        elif item:
            out.append({"title": "问题", "detail": str(item)[:600], "fix": ""})
    return out


def _clean_scores(v: Any) -> dict:
    out: dict[str, float] = {}
    if not isinstance(v, dict):
        return out
    for k, label in SCORE_LABELS.items():
        raw = v.get(k, v.get(label))
        if raw is None:
            continue
        try:
            out[k] = round(max(0.0, min(10.0, float(raw))), 1)
        except (TypeError, ValueError):
            continue
    if "overall" not in out and out:
        vals = [out[k] for k in out if k != "overall"]
        if vals:
            out["overall"] = round(sum(vals) / len(vals), 1)
    return out


class TeachingSession:
    def __init__(self, project: Project, settings: Any) -> None:
        self.project = project
        self.settings = settings

    def _source_image(self, target: str) -> Path:
        """original=最底层可见图层的原图（用户按下快门的样子）；current=当前成图。"""
        if target == "current":
            p = self.project.preview_path()
            if not p.is_file():
                self.project._push_preview()
            return p
        for layer in self.project.doc.get("layers", []):
            if layer.get("visible", True):
                src = Path(self.project.dir) / layer["source"]
                if src.is_file():
                    return src
        p = self.project.preview_path()
        if not p.is_file():
            self.project._push_preview()
        return p

    def critique(self, target: str = "original", apply_edits: bool = False) -> Critique:
        target = "current" if str(target).lower() in ("current", "edited", "成图") else "original"
        profile = self.settings.teaching_model()
        result = Critique(target=target, model=profile.model)
        timeout = float(self.settings.network.get("timeout", 180) or 180)
        client = llm.LLMClient(profile, timeout=timeout, proxy=self.settings.proxy_url())
        client.check_ready(need_vision=True)          # 无视觉模型 → 明确报错

        img = self._source_image(target)
        detail = str(self.settings.data.get("vision", {}).get("teaching_detail", "high"))
        max_tokens = int((self.settings.data.get("teaching") or {}).get("max_tokens") or 6144)
        t0 = time.time()

        # 预载技能包里与点评最相关的参考（诊断清单 / 失误清单），其余按需加载
        auto = [str(x) for x in (self.settings.skills.get("teach_auto") or [])]
        ref_text, got, _missing = skills.load_refs(auto, self.settings, limit=len(auto) or 0)
        result.loaded = [d["title"] for d in got]

        ask = ("请评价这张照片，并给出提升拍摄水平的建议。"
               + ("（这是用户已经修过的成图）" if target == "current" else "（这是原始照片）"))
        if ref_text:
            ask += "\n\n" + prompts.build_refs_block(ref_text)
        messages = [
            {"role": "system", "content": prompts.build_teacher_prompt(self.settings)},
            {"role": "user", "content": [llm.text_block(ask),
                                         llm.image_block(img, detail=detail, max_side=1600)]},
        ]
        res = client.chat(messages, json_mode=True, temperature=0.3, max_tokens=max_tokens)
        result.usage = res.usage
        data, result.parse_note = _parse_with_one_repair(client, messages, res, result)

        # 模型可以再要 1~2 份参考，给一轮补充机会（渐进式披露）
        wants = data.get("load") if isinstance(data, dict) else None
        if wants:
            more_text, more, _m = skills.load_refs(wants, self.settings, limit=2)
            if more_text:
                result.loaded += [d["title"] for d in more]
                messages.append({"role": "assistant", "content": res.text[:600]})
                messages.append({"role": "user", "content": prompts.build_refs_block(more_text)})
                res = client.chat(messages, json_mode=True, temperature=0.3, max_tokens=max_tokens)
                try:
                    data = llm.parse_json_reply(res.text)
                except llm.LLMError:
                    pass                                # 保留第一轮结果
        result.elapsed = time.time() - t0
        if not isinstance(data, dict):
            raise llm.LLMError("教学模式返回的 JSON 结构不对（应为对象）。")

        result.summary = str(data.get("summary") or data.get("overall") or "").strip()
        result.scores = _clean_scores(data.get("scores"))
        result.strengths = _clean_list(data.get("strengths") or data.get("highlights"))
        result.issues = _clean_issues(data.get("issues") or data.get("problems"))
        result.shooting_tips = _clean_list(data.get("shooting_tips") or data.get("tips")
                                           or data.get("next_time"))

        raw_edits = data.get("edits") or data.get("ops") or []
        try:
            result.edits = ops.validate_ops(raw_edits)
        except ops.OpError as e:
            result.edits = []
            result.edits_note = f"模型给的建议修图指令不合法，已忽略：{e}"
        if not result.scores and not result.summary:
            raise llm.LLMError("教学模式没有给出有效点评内容，请重试。")

        if apply_edits and result.edits:
            try:
                result.applied = self.project.apply_ops(result.edits)
            except (ops.OpError, DocError) as e:
                result.edits_note = f"应用建议指令失败：{e}"

        self._record(result)
        return result

    def _record(self, c: Critique) -> None:
        self.project.chat.append({
            "role": "teacher",
            "content": c.summary,
            "critique": c.to_dict(),
            "ts": time.strftime("%H:%M:%S"),
        })
        try:
            self.project.save()
        except Exception:
            pass
