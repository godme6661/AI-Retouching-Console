"""用本机代理抓取参考资料并抽取正文，存入 .research/ 供人工提炼。

用途：为知识库收集可核查的来源。抓下来的内容只作素材，不进知识库原文。
用法：python tools/fetch_research.py [--proxy http://127.0.0.1:7897]
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from pathlib import Path

import httpx

URLS = {
    "retouch-mistakes": "https://petapixel.com/2017/07/29/common-amateur-portrait-retouching-mistakes/",
    "jp-airy-style": "https://blog.pinkoi.com/tw/lifestyle/tv0dirja/",
    "oversharpening": "https://www.digifotopro.nl/en/oversharpening-in-photography-how-to-avoid-it",
    "composition-impact": "https://visualwilderness.com/composition-creativity/nature-photography-composition-from-rules-to-visual-impact",
    "histogram-ettr": "https://photographylife.com/exposing-to-the-right-explained",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TAG_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
BLOCK_RE = re.compile(r"</(p|div|li|h[1-6]|tr|section|article)>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\xa0]+")
NL_RE = re.compile(r"\n{3,}")


def to_text(raw: str) -> str:
    raw = TAG_RE.sub(" ", raw)
    raw = BLOCK_RE.sub("\n", raw)
    raw = ANY_TAG.sub("", raw)
    raw = html.unescape(raw)
    raw = WS_RE.sub(" ", raw)
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if len(ln) > 1]
    return NL_RE.sub("\n\n", "\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--out", default=".research")
    ap.add_argument("--limit", type=int, default=40000)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, url in URLS.items():
        for label, kw in (("代理", {"proxy": args.proxy}), ("直连", {})):
            try:
                with httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS, **kw) as c:
                    r = c.get(url)
                if r.status_code != 200:
                    print(f"[{name}] {label} HTTP {r.status_code}")
                    continue
                text = to_text(r.text)[: args.limit]
                path = out / f"{name}.txt"
                path.write_text(f"# 来源: {url}\n# 抓取方式: {label}\n\n{text}", encoding="utf-8")
                print(f"[{name}] {label} OK  {len(text)} 字  → {path}")
                break
            except Exception as e:
                print(f"[{name}] {label} {type(e).__name__}: {str(e)[:70]}")
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
