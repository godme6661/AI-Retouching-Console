"""探测本机代理（默认 127.0.0.1:7897）与几个关键外网端点的可达性。

用来判断：直连被墙的站点在走代理后是否可达（HuggingFace 实测直连超时）。
"""
from __future__ import annotations

import socket
import sys
import time

import httpx

PROXY = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7897"
TARGETS = [
    ("https://api.deepseek.com/v1/models", "DeepSeek API"),
    ("https://huggingface.co/api/models?limit=1", "HuggingFace Hub"),
    ("https://hf-mirror.com/api/models?limit=1", "HF 镜像"),
    ("https://api.openai.com/v1/models", "OpenAI"),
    ("https://api.siliconflow.cn/v1/models", "硅基流动"),
]


def tcp_ok(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(url: str, proxy: str | None, timeout: float = 15.0) -> str:
    t0 = time.time()
    try:
        # 注意：httpx >= 0.28 的关键字是 proxy=（单数），旧名 proxies= 会 TypeError
        kw = {"proxy": proxy} if proxy else {}
        with httpx.Client(timeout=timeout, follow_redirects=True, **kw) as c:
            r = c.get(url)
        return f"HTTP {r.status_code}  {time.time()-t0:.1f}s  {len(r.content)}B"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:80]}  {time.time()-t0:.1f}s"


if __name__ == "__main__":
    host_port = PROXY.split("//")[-1]
    host, port = host_port.split(":")
    print(f"代理 {PROXY} TCP 可连: {tcp_ok(host, int(port))}")
    print()
    print(f"{'目标':<16}{'直连':<46}{'走代理'}")
    for url, label in TARGETS:
        direct = probe(url, None, timeout=8)
        via = probe(url, PROXY, timeout=15) if tcp_ok(host, int(port)) else "（代理不可用）"
        print(f"{label:<16}{direct:<46}{via}")
