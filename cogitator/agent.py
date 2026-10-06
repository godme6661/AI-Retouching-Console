"""对话代理：自然语言 → 结构化编辑指令 → 本机执行 → 自我复核。

一轮对话的完整链路：
  1. 组装系统提示：角色约束 + 算子手册 + 风格配方 + 技能包常驻准则 + 参考索引 + 当前工程状态；
  2. 历史消息只带文本（省 token），当前这一轮带上最新预览图；文本模型则附客观指标；
  3. 要 JSON（含 diagnosis / intent / ops / load / question）；
     若模型请求 load，取出参考正文注入后**重问一轮**（渐进式披露，不计入纠错次数）；
  4. 校验失败或执行失败 → 把错误原样回灌让它自我修正（最多 2 次）；
  5. 应用成功且开启自动精修时，把**执行后的成图**再交给它复核一轮，允许回退（remove_op）；
  6. 记录对话（含诊断、意图、加载了哪些参考、复核结论），供界面展示与追溯。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import llm, ops, prompts, render, skills
from .document import DocError, Project

MAX_REPAIR = 2
MAX_ROUNDS = 7


@dataclass
class AgentReply:
    reply: str = ""
    question: str = ""
    diagnosis: str = ""
    intent: str = ""
    ops: list[dict] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)        # 参考 id
    loaded_titles: list[str] = field(default_factory=list)
    refine: dict[str, Any] = field(default_factory=dict)   # 复核环节的结果
    error: str = ""
    model: str = ""
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)
    attempts: int = 0
    vision_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "question": self.question,
            "diagnosis": self.diagnosis,
            "intent": self.intent,
            "ops": self.ops,
            "applied": self.applied,
            "loaded": self.loaded,
            "loaded_titles": self.loaded_titles,
            "refine": self.refine,
            "error": self.error,
            "model": self.model,
            "elapsed": round(self.elapsed, 2),
            "usage": self.usage,
            "attempts": self.attempts,
            "vision_used": self.vision_used,
        }


class ImageAgent:
    def __init__(self, project: Project, settings: Any) -> None:
        self.project = project
        self.settings = settings
        self._usage_total: dict[str, int] = {}

    # ------------------------------------------------------------ 上下文
    def _client(self) -> llm.LLMClient:
        profile = self.settings.model()
        timeout = float(self.settings.network.get("timeout", 180) or 180)
        return llm.LLMClient(profile, timeout=timeout, proxy=self.settings.proxy_url())

    def _history_messages(self, limit: int = 12) -> list[dict]:
        out: list[dict] = []
        for msg in self.project.chat[-limit:]:
            role = msg.get("role")
            content = msg.get("content") or ""
            if role == "assistant" and msg.get("ops"):
                names = ", ".join(o.get("op", "?") for o in msg["ops"])
                content = f"{content}\n（我执行了：{names}）"
            if role in ("user", "assistant") and content:
                out.append({"role": role, "content": content})
        return out

    def _objective_metrics(self) -> str:
        """文本模型没有眼睛时，用客观指标替代。"""
        rgb, alpha = self.project.render_full()
        st = render.histogram_stats(rgb)
        sharp = render.sharpness_score(rgb)
        return ("当前成图的客观指标（你没有读图能力，请据此判断，不要假装看到了画面）：\n"
                f"尺寸 {rgb.shape[1]}x{rgb.shape[0]}\n"
                f"平均亮度 {st['平均亮度']}（0~1）\n"
                f"亮度分位 1/5/25/50/75/95/99%：{st['亮度分位_1_5_25_50_75_95_99']}\n"
                f"过曝像素占比 {st['过曝像素占比']}，死黑像素占比 {st['死黑像素占比']}\n"
                f"通道均值 RGB {st['通道均值_RGB']}，白平衡偏移 {st['白平衡偏移']}（>1 偏多、<1 偏少）\n"
                f"对比度(亮度标准差) {st['对比度_标准差']}　锐度(拉普拉斯方差) {sharp}（越小越糊）")

    def _image_blocks(self, text: str, profile, detail_key: str = "chat_detail") -> list[dict]:
        blocks: list[dict] = [llm.text_block(text)]
        if profile.vision:
            try:
                preview = self.project.preview_path()
                if preview.is_file():
                    blocks.append(llm.image_block(
                        preview,
                        detail=str(self.settings.data.get("vision", {}).get(detail_key, "low")),
                        max_side=int(self.settings.preview_max_side)))
                else:
                    blocks.append(llm.text_block(self._objective_metrics()))
            except Exception:
                blocks.append(llm.text_block(self._objective_metrics()))
        else:
            blocks.append(llm.text_block(self._objective_metrics()))
        return blocks

    # ------------------------------------------------------------ 主流程
    def ask(self, user_text: str, refine: bool | None = None) -> AgentReply:
        text = (user_text or "").strip()
        if not text:
            raise llm.LLMError("请先输入内容。")
        profile = self.settings.model()
        client = self._client()
        client.check_ready()

        out = AgentReply(model=profile.model)
        t0 = time.time()
        do_refine = bool(self.settings.chat.get("auto_refine", True)) if refine is None else bool(refine)
        max_refs = int(self.settings.skills.get("max_refs_per_turn", 2) or 2)

        loaded_ids: list[str] = []
        history = self._history_messages()
        blocks = self._image_blocks(text, profile)
        if profile.vision:
            out.vision_used = True

        messages = [{"role": "system",
                     "content": prompts.build_editor_prompt(self.project.llm_context(),
                                                            self.settings, loaded=loaded_ids)}]
        messages += history
        messages.append({"role": "user", "content": blocks})

        data: dict | None = None
        repairs = 0
        load_rounds = 0
        last_error = ""

        for _ in range(MAX_ROUNDS):
            out.attempts += 1
            res = client.chat(messages, json_mode=True)
            self._add_usage(out, res.usage)
            try:
                parsed = llm.parse_json_reply(res.text)
            except llm.LLMError as e:
                last_error = str(e)
                if repairs >= MAX_REPAIR:
                    break
                repairs += 1
                messages += [{"role": "assistant", "content": res.text[:2000]},
                             {"role": "user", "content": prompts.REPAIR_TEMPLATE.format(error=last_error)}]
                continue
            if isinstance(parsed, list):
                parsed = {"ops": parsed}
            if not isinstance(parsed, dict):
                last_error = "返回的 JSON 不是对象"
                break

            # ---- 按需加载参考（渐进式披露），不计入纠错次数
            wants = parsed.get("load") or []
            if wants and load_rounds < 1 and len(loaded_ids) < max_refs and repairs == 0:
                ref_text, got, missing = skills.load_refs(
                    wants, self.settings, limit=max_refs - len(loaded_ids))
                if got:
                    loaded_ids.extend(d["id"] for d in got)
                    out.loaded = list(loaded_ids)
                    out.loaded_titles.extend(d["title"] for d in got)
                    load_rounds += 1
                    messages[0] = {"role": "system",
                                   "content": prompts.build_editor_prompt(
                                       self.project.llm_context(), self.settings, loaded=loaded_ids)}
                    messages.append({"role": "assistant", "content": res.text[:600]})
                    messages.append({"role": "user", "content": prompts.build_refs_block(ref_text)})
                    continue
                if missing:
                    messages.append({"role": "user",
                                     "content": f"（没有找到参考 {missing}；请按常识继续，不要再请求加载。）"})
                    continue

            data = parsed
            out.diagnosis = str(parsed.get("diagnosis") or "").strip()
            out.intent = str(parsed.get("intent") or "").strip()
            out.reply = str(parsed.get("reply") or parsed.get("message") or "").strip()
            out.question = str(parsed.get("question") or "").strip()

            try:
                validated = ops.validate_ops(parsed.get("ops") or [])
            except ops.OpError as e:
                last_error = f"指令不合法：{e}"
                if repairs >= MAX_REPAIR:
                    break
                repairs += 1
                messages += [{"role": "assistant", "content": res.text[:2000]},
                             {"role": "user", "content": prompts.REPAIR_TEMPLATE.format(error=last_error)}]
                continue

            if not validated:
                out.ops = []
                out.elapsed = time.time() - t0
                self._record(text, out)
                return out

            try:
                out.applied = self.project.apply_ops(validated)
                out.ops = validated
            except (ops.OpError, DocError) as e:
                last_error = f"执行失败：{e}"
                if repairs >= MAX_REPAIR:
                    break
                repairs += 1
                messages += [{"role": "assistant", "content": res.text[:2000]},
                             {"role": "user", "content": prompts.REPAIR_TEMPLATE.format(error=last_error)}]
                continue
            break

        if data is None or (not out.applied and last_error):
            out.error = last_error or "模型多次未能给出可执行指令。"
            out.elapsed = time.time() - t0
            self._record(text, out)
            return out

        # ---- 自动精修：把执行后的成图再交给模型复核一轮
        if do_refine and out.applied:
            try:
                out.refine = self._refine(client, profile, out)
            except llm.LLMError as e:
                out.refine = {"enabled": True, "error": str(e)}

        out.elapsed = time.time() - t0
        self._record(text, out)
        return out

    # ------------------------------------------------------------ 复核
    def _refine(self, client: llm.LLMClient, profile, out: AgentReply) -> dict[str, Any]:
        max_ops = int(self.settings.chat.get("refine_max_ops", 3) or 3)
        system = prompts.build_refine_prompt(out.intent, max_ops, self.settings)
        blocks = self._image_blocks("请复核这张成图：意图是否达成？有没有引入失误？只改确实看到的问题。",
                                    profile)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": blocks}]
        info: dict[str, Any] = {"enabled": True, "verdict": "", "problems": [],
                                "ops": [], "applied": [], "error": ""}
        res = client.chat(messages, json_mode=True, temperature=0.1)
        self._add_usage(out, res.usage)
        try:
            parsed = llm.parse_json_reply(res.text)
        except llm.LLMError as e:
            info["error"] = f"复核返回无法解析：{e}"
            return info
        if not isinstance(parsed, dict):
            info["error"] = "复核返回不是对象"
            return info
        info["verdict"] = str(parsed.get("verdict") or "").strip()
        info["problems"] = [str(x) for x in (parsed.get("problems") or []) if str(x).strip()]
        try:
            fixed = ops.validate_ops(parsed.get("ops") or [], max_ops=max_ops)
        except ops.OpError as e:
            info["error"] = f"复核给出的修正指令不合法，已忽略：{e}"
            return info
        if not fixed:
            info["note"] = "复核认为无需修改"
            return info
        try:
            info["applied"] = self.project.apply_ops(fixed)
            info["ops"] = fixed
        except (ops.OpError, DocError) as e:
            info["error"] = f"应用复核修正失败：{e}"
        return info

    # ------------------------------------------------------------ 记账
    def _add_usage(self, out: AgentReply, usage: dict) -> None:
        if not usage:
            return
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(usage.get(k), int):
                self._usage_total[k] = self._usage_total.get(k, 0) + usage[k]
        out.usage = dict(self._usage_total)

    def _record(self, user_text: str, out: AgentReply) -> None:
        ts = time.strftime("%H:%M:%S")
        self.project.chat.append({"role": "user", "content": user_text, "ts": ts})
        entry: dict[str, Any] = {
            "role": "assistant",
            "content": out.reply or out.error or "（无文字说明）",
            "ops": out.ops,
            "ts": ts,
        }
        if out.diagnosis:
            entry["diagnosis"] = out.diagnosis
        if out.intent:
            entry["intent"] = out.intent
        if out.loaded_titles:
            entry["loaded"] = out.loaded_titles
        if out.applied:
            entry["results"] = out.applied
        if out.question:
            entry["question"] = out.question
        if out.refine:
            entry["refine"] = out.refine
        if out.error:
            entry["error"] = out.error
        self.project.chat.append(entry)
        try:
            self.project.save()
        except Exception:
            pass
