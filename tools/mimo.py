"""调用小米 MiMo 开放平台（OpenAI 兼容）的极简客户端。

用途：让外部模型协助本项目的重构/美化。**密钥只从文件或环境变量读取，绝不打印、绝不写入仓库。**

官方文档：https://mimo.mi.com/docs/zh-CN/api/chat/openai-api
  POST https://api.xiaomimimo.com/v1/chat/completions
  认证：api-key: $MIMO_API_KEY   （也支持 Authorization: Bearer）

用法：
  python tools/mimo.py models
  python tools/mimo.py ask --prompt "你好" 
  python tools/mimo.py ask --prompt-file tmp/prompt.md --out tmp/reply.md
  python tools/mimo.py ask --file cogitator/web/style.css --prompt "只输出改进后的 CSS"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

DEFAULT_KEY_FILE = os.environ.get("MIMO_KEY_FILE", "")
DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.6-flash"


def load_key(key_file: str = "") -> str:
    """按 环境变量 → 指定文件 → 默认文件 的顺序取密钥。不回显内容。"""
    env = (os.environ.get("MIMO_API_KEY") or "").strip()
    if env:
        return env
    for cand in [key_file, DEFAULT_KEY_FILE]:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace").strip()
            for line in txt.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "://" not in line and "=" not in line:
                    return line
    raise SystemExit("找不到 MiMo 密钥：请设置环境变量 MIMO_API_KEY，或用 --key-file 指定文件")


def headers(key: str) -> dict:
    return {"api-key": key, "Content-Type": "application/json"}


def call(base: str, key: str, model: str, messages: list[dict], *,
         max_tokens: int = 8192, temperature: float = 0.3, timeout: float = 300,
         proxy: str = "") -> dict:
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    kw: dict = {"timeout": timeout}
    if proxy:
        kw["proxy"] = proxy
    with httpx.Client(**kw) as c:
        r = c.post(f"{base.rstrip('/')}/chat/completions", headers=headers(key), json=payload)
    if r.status_code >= 400:
        # 出错时回显服务端说明，但先剔除密钥以防万一
        msg = r.text[:600].replace(key, "***")
        raise SystemExit(f"HTTP {r.status_code}：{msg}")
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="MiMo（小米开放平台）调用工具")
    ap.add_argument("cmd", choices=["models", "ask"])
    ap.add_argument("--key-file", default="")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--file", action="append", default=[], help="附加上下文文件，可多次")
    ap.add_argument("--system", default="")
    ap.add_argument("--out", default="", help="把回答写入文件")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--proxy", default="", help="如需代理，例如 http://127.0.0.1:7897")
    a = ap.parse_args()

    key = load_key(a.key_file)
    if a.cmd == "models":
        with httpx.Client(timeout=60) as c:
            r = c.get(f"{a.base.rstrip('/')}/models", headers=headers(key))
        if r.status_code >= 400:
            print(f"HTTP {r.status_code}：{r.text[:300].replace(key, '***')}")
            return 1
        data = r.json()
        ids = [m.get("id", "?") for m in (data.get("data") or [])]
        print(f"可用模型 {len(ids)} 个：")
        for i in ids:
            print("  -", i)
        return 0

    parts: list[str] = []
    for f in a.file:
        p = Path(f)
        parts.append(f"\n\n===== 文件：{f} =====\n" + p.read_text(encoding="utf-8", errors="replace"))
    ask = a.prompt
    if a.prompt_file:
        ask = Path(a.prompt_file).read_text(encoding="utf-8", errors="replace")
    messages = []
    if a.system:
        messages.append({"role": "system", "content": a.system})
    messages.append({"role": "user", "content": ask + "".join(parts)})

    data = call(a.base, key, a.model, messages, max_tokens=a.max_tokens,
                temperature=a.temperature, proxy=a.proxy)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except Exception:
        text = json.dumps(data, ensure_ascii=False)[:2000]
    usage = data.get("usage") or {}
    finish = (data.get("choices") or [{}])[0].get("finish_reason")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"回答已写入 {a.out}（{len(text)} 字符，模型 {data.get('model')}，"
              f"用量 {usage.get('total_tokens')}，finish={finish}）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
