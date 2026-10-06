"""模型客户端：所有厂商走 OpenAI 兼容的 /chat/completions，默认 DeepSeek。

密钥安全：只从配置或环境变量读取，绝不写进日志/项目文档/对话记录；错误信息里的
密钥会被打码后再抛出。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import ModelProfile


class LLMError(RuntimeError):
    """调用模型失败（信息面向用户，可直接显示）。"""


@dataclass
class ChatResult:
    text: str
    model: str = ""
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0
    raw: dict = field(default_factory=dict)
    # "stop" 正常结束；"length" 说明被 max_tokens 截断 —— 这是"JSON 格式不对"最常见的原因
    finish_reason: str = ""


def redact(text: str, secret: str) -> str:
    if secret and len(secret) >= 8:
        return text.replace(secret, "***")
    return text


def image_data_url(path: Path | str, detail_max_side: int = 1400) -> str:
    """把图片编码成 data URL（DeepSeek 支持 base64 内联，仅允许出现在 user 消息里）。"""
    import io

    from PIL import Image

    p = Path(path)
    if not p.is_file():
        raise LLMError(f"图片不存在：{p}")
    with Image.open(p) as im:
        im = im.convert("RGB")
        if max(im.size) > detail_max_side:
            scale = detail_max_side / float(max(im.size))
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
        bio = io.BytesIO()
        im.save(bio, format="JPEG", quality=88)
        data = bio.getvalue()
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(path: Path | str, detail: str = "low", max_side: int = 1400) -> dict:
    return {"type": "image_url",
            "image_url": {"url": image_data_url(path, max_side), "detail": detail}}


class LLMClient:
    def __init__(self, profile: ModelProfile, timeout: float = 180.0,
                 proxy: str | None = None) -> None:
        self.profile = profile
        self.timeout = timeout
        # httpx >= 0.28 的关键字是 proxy=（单数）；旧名 proxies= 会直接 TypeError
        self.proxy = proxy or None

    # -- 配置检查 --------------------------------------------------
    def check_ready(self, need_vision: bool = False) -> None:
        p = self.profile
        if not p.base_url:
            raise LLMError(f"模型「{p.name}」还没有填 base_url，请在设置里补全。")
        if not p.model:
            raise LLMError(f"模型「{p.name}」还没有填模型名，请在设置里补全。")
        if not p.resolved_key():
            hint = f"，或设置环境变量 {p.api_key_env}" if p.api_key_env else ""
            raise LLMError(f"模型「{p.name}」缺少 API 密钥：请在本工具的设置页填入{hint}。")
        if need_vision and not p.vision:
            raise LLMError(
                f"当前模型「{p.name}」不支持读图，无法用于教学模式。"
                f"请在设置里切换到支持读图的模型（例如默认的 DeepSeek Flash）。"
            )

    def _url(self) -> str:
        return f"{self.profile.base_url.rstrip('/')}/chat/completions"

    # -- 调用 ------------------------------------------------------
    def chat(self, messages: list[dict], *, json_mode: bool = False,
             temperature: float | None = None, max_tokens: int | None = None,
             retries: int = 2) -> ChatResult:
        p = self.profile
        self.check_ready()
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": p.temperature if temperature is None else temperature,
            "max_tokens": p.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode and p.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {p.resolved_key()}",
            "Content-Type": "application/json",
        }

        last_err = ""
        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                kw: dict[str, Any] = {"timeout": self.timeout}
                if self.proxy:
                    kw["proxy"] = self.proxy
                with httpx.Client(**kw) as client:
                    resp = client.post(self._url(), headers=headers, json=payload)
            except httpx.TimeoutException:
                last_err = f"请求超时（{self.timeout:.0f}s）。图片较多或网络较慢时可稍后重试；" \
                           f"若本机有代理，可在「设置 → 网络」里启用。"
            except httpx.HTTPError as e:
                last_err = f"网络错误：{redact(str(e), p.resolved_key())}"
            else:
                elapsed = time.time() - t0
                if resp.status_code == 401:
                    raise LLMError(f"鉴权失败（401）：API 密钥无效或已过期。请在设置里更正「{p.name}」的密钥。")
                if resp.status_code == 402:
                    raise LLMError(f"余额不足（402）：请为「{p.name}」对应的账号充值。")
                if resp.status_code == 429:
                    last_err = "触发限速（429），稍后重试。"
                elif resp.status_code >= 400:
                    detail = redact(resp.text[:400], p.resolved_key())
                    raise LLMError(f"模型返回 {resp.status_code}：{detail}")
                else:
                    try:
                        data = resp.json()
                    except Exception:
                        raise LLMError(f"返回内容不是 JSON：{redact(resp.text[:200], p.resolved_key())}") from None
                    try:
                        text = data["choices"][0]["message"]["content"] or ""
                    except Exception:
                        raise LLMError(f"返回结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from None
                    if json_mode and not text.strip():
                        last_err = "模型返回了空的 JSON（官方已知偶发），重试中。"
                    else:
                        usage = data.get("usage") or {}
                        choice = (data.get("choices") or [{}])[0] or {}
                        return ChatResult(text=text, model=data.get("model", p.model),
                                          usage=usage, elapsed=elapsed, raw=data,
                                          finish_reason=str(choice.get("finish_reason") or ""))
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
        raise LLMError(last_err or "模型调用失败")


# ---------------------------------------------------------------------------
# 模型返回的 JSON 解析：必须足够皮实
#
# 实机体检出的真实失败形态（tools/check_json_tolerance.py 逐条钉住）：
#   * JSON 后面跟着一段含花括号的后记（"（未评项用 {} 表示）"）—— 旧的 rfind("}") 会切错
#   * 字符串内容里本身就含 } —— 同上
#   * // 注释、尾随逗号、单引号、未加引号的键、中文全角引号与冒号
#   * 回答被 max_tokens 截断（缺收尾括号）—— 旧实现甚至会从里面抠出一个数组，
#     于是报出"结构不对"这种误导人的信息
# ---------------------------------------------------------------------------

_FULLWIDTH = {"｛": "{", "｝": "}", "［": "[", "］": "]", "：": ":", "，": ",",
              "“": '"', "”": '"', "‘": "'", "’": "'"}


@dataclass
class ParseResult:
    data: Any
    repaired: list[str] = field(default_factory=list)
    truncated: bool = False
    raw: str = ""


def _strip_fences(text: str) -> str:
    """去掉 markdown 围栏，取其中最长的一段（模型常把答案放在围栏里，之外还有废话）。"""
    s = (text or "").strip()
    if "```" not in s:
        return s
    parts = s.split("```")
    inside = [p for p in parts[1::2]]                     # 围栏内部
    if not inside:
        return s
    best = max(inside, key=len).strip()
    return re.sub(r"^[A-Za-z0-9_+.-]{0,12}\s*", "", best, count=1).strip() or s


def _scan_balanced(s: str, start: int) -> str | None:
    """从 s[start]（{ 或 [）起按括号配对取出完整片段，正确处理字符串与转义。

    这正是取代 rfind("}") 的关键：只有真正配对的那个括号才是结尾。
    """
    stack: list[str] = []
    quote: str | None = None
    i = start
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "{[": 
            stack.append(c)
        elif c in "}]":
            if not stack:
                return None
            op = stack.pop()
            if (op, c) not in (("{", "}"), ("[", "]")):
                return None
            if not stack:
                return s[start:i + 1]
        i += 1
    return None                                            # 未闭合（多半是被截断）


def _strip_comments(s: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < len(s) and s[i + 1] == "/":
            j = s.find("\n", i)
            i = len(s) if j < 0 else j
            continue
        if c == "/" and i + 1 < len(s) and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = len(s) if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _sanitize(s: str) -> str:
    """把常见的"手写 JSON"修成合法 JSON。只做低风险替换，避免误伤正常内容。"""
    t = (s or "").strip()
    if any(k in t for k in _FULLWIDTH):
        for a, b in _FULLWIDTH.items():
            t = t.replace(a, b)
    t = _strip_comments(t)
    if "'" in t and '"' not in t:                          # 全单引号才替换，避免混用时误伤
        t = t.replace("'", '"')
    t = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)", r'\1"\2"\3', t)   # 未加引号的键
    t = re.sub(r",(\s*[}\]])", r"\1", t)                   # 尾随逗号
    t = re.sub(r"\bNaN\b|\b-?Infinity\b", "null", t)
    return t


def _close_truncated(s: str) -> str | None:
    """尽力把被截断的 JSON 补成合法：先补引号与括号，不行就砍掉最后一个未完成的片段。"""
    def _suffix(text: str) -> tuple[str, int]:
        stack: list[str] = []
        quote: str | None = None
        i = 0
        while i < len(text):
            c = text[i]
            if quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c in "{[":
                stack.append(c)
            elif c in "}]" and stack:
                stack.pop()
            i += 1
        tail = ('"' if quote else "") + "".join("}" if o == "{" else "]" for o in reversed(stack))
        return tail, len(stack)

    tail, _ = _suffix(s)
    if not tail:
        return None
    trial = s + tail
    try:
        json.loads(trial)
        return trial
    except Exception:
        pass
    cut = s
    for _ in range(10):                                    # 砍掉最后一个逗号之后的碎片再试
        k = cut.rfind(",")
        if k < 0:
            return None
        cut = cut[:k]
        tail, _ = _suffix(cut)
        trial = cut + tail
        try:
            json.loads(trial)
            return trial
        except Exception:
            continue
    return None


def _candidates(text: str) -> list[str]:
    """按可能性排序的候选 JSON 片段。"""
    s = (text or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(x: str | None) -> None:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(_strip_fences(s))
    add(s)
    tried = 0
    for i, ch in enumerate(s):                             # 从每个 { 或 [ 起尝试配对
        if ch in "{[":
            add(_scan_balanced(s, i))
            tried += 1
            if tried >= 25:
                break
    return out


def parse_json_reply_ex(text: str) -> ParseResult:
    """解析模型返回的 JSON，返回数据与"做过哪些修复"的说明。"""
    raw = text or ""
    fallback: Any = None
    truncated = False
    repaired: list[str] = []
    for cand in _candidates(raw):
        variants: list[tuple[str, list[str]]] = [(cand, [])]
        san = _sanitize(cand)
        if san != cand:
            variants.append((san, ["规整标点/注释/尾随逗号"]))
        for body, notes in variants:
            try:
                data = json.loads(body)
            except Exception:
                closed = _close_truncated(body)
                if closed is not None:
                    try:
                        data = json.loads(closed)
                        truncated = True
                        repaired = notes + ["补齐被截断的结尾"]
                        if isinstance(data, dict):
                            return ParseResult(data=data, repaired=repaired,
                                               truncated=True, raw=raw)
                        fallback = fallback if fallback is not None else data
                        continue
                    except Exception:
                        pass
                continue
            if isinstance(data, dict):
                return ParseResult(data=data, repaired=notes, truncated=False, raw=raw)
            if fallback is None:
                fallback = data
    if fallback is not None:
        return ParseResult(data=fallback, repaired=repaired, truncated=truncated, raw=raw)

    tail = raw.strip()[-160:].replace("\n", " ")
    looks_cut = raw.count("{") > raw.count("}") or raw.count("[") > raw.count("]")
    hint = ("回答疑似被长度上限截断" if looks_cut else "模型没有返回可解析的 JSON")
    raise LLMError(f"{hint}。原始返回末尾：…{tail}")


def parse_json_reply(text: str) -> dict:
    """兼容旧调用：只取数据。"""
    return parse_json_reply_ex(text).data
