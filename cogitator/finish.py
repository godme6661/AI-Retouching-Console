"""出片环节：水印与边框。

## 为什么放在导出期，而不是做成算子

算子属于"修图管线"——它必须是确定性的、可重放、可撤销，进而且影响预览。
而水印与边框是**出片环节**的事（送审样片加水印、发朋友圈加白边），
它们不该改变工程本身。所以：

* 只在导出时应用，工程与预览完全不受影响；
* 参数随手写在导出请求里（也可以在设置里存成**导出预设**复用）；
* 与 `docs/DECISIONS.md` D1（AI 不碰像素）不冲突：这里改的是**成品**，不是源图。

## 参数（都走 HTTP 的 `finish` 字段）

    {
      "border":    {"style": "white|black|polaroid", "width": 4, "bottom": 14, "color": "#ffffff"},
      "watermark": {"text": "© 2026 某人",         # 或 "image": "D:/logo.png"
                    "position": "br|bl|tr|tl|center|bc|tc", "opacity": 45,
                    "scale": 12, "font_size": 0, "color": "#ffffff",
                    "margin": 3, "shadow": true, "font_file": ""}
    }

宽度/边距/透明度都是**百分比或 0~100 的直观值**，不需要用户换算像素。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

POSITIONS = ("tl", "tc", "tr", "center", "bl", "bc", "br")
BORDER_STYLES = ("white", "black", "polaroid")

# 跨平台候选字体：中文优先，其余为常见回退
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


def _pct(v: Any, default: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, x))


def _find_font(explicit: str = "") -> str:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise ValueError(f"找不到字体文件：{explicit}")
    for c in FONT_CANDIDATES:
        if Path(c).is_file():
            return c
    raise ValueError("系统里找不到可用字体；请在 watermark.font_file 里指定一个 .ttf/.ttc 路径")


def _anchored_pos(box_wh: tuple[int, int], size: tuple[int, int], position: str,
                  margin: int) -> tuple[int, int]:
    """按九宫格算左上角坐标（tl/tc/tr/center/bl/bc/br）。"""
    W, H = box_wh
    w, h = size
    x = {"l": margin, "c": (W - w) // 2, "r": W - w - margin}
    y = {"t": margin, "c": (H - h) // 2, "b": H - h - margin}
    hx = "l" if position.endswith("l") else ("r" if position.endswith("r") else "c")
    if position in ("tl", "tc", "tr"):
        hy = "t"
    elif position in ("bl", "bc", "br"):
        hy = "b"
    else:
        hx, hy = "c", "c"
    return max(0, x[hx]), max(0, y[hy])


def add_border(img: Image.Image, opts: dict | None) -> Image.Image:
    """加边框：white/black 为四边等宽，polaroid 为下方留宽的拍立得样式。"""
    if not opts:
        return img
    style = str(opts.get("style") or "white").lower()
    if style not in BORDER_STYLES:
        raise ValueError(f"边框样式只支持 {', '.join(BORDER_STYLES)}，收到 {style!r}")
    W, H = img.size
    base = max(W, H)
    width = int(round(base * _pct(opts.get("width"), 4.0, 0, 25) / 100.0))
    bottom = width
    if style == "polaroid":
        bottom = int(round(base * _pct(opts.get("bottom"), 14.0, 0, 40) / 100.0))
    color = str(opts.get("color") or ("black" if style == "black" else "#ffffff"))
    if width <= 0 and bottom <= 0:
        return img
    out = Image.new("RGBA" if img.mode == "RGBA" else "RGB", (W + width * 2, H + width + bottom), color)
    out.paste(img, (width, width))
    return out


def add_watermark(img: Image.Image, opts: dict | None) -> Image.Image:
    """加水印：文字或图片；支持九宫格定位、透明度、缩放与描边阴影。"""
    if not opts:
        return img
    position = str(opts.get("position") or "br").lower()
    if position not in POSITIONS:
        raise ValueError(f"水印位置只支持 {', '.join(POSITIONS)}，收到 {position!r}")
    opacity = _pct(opts.get("opacity"), 45.0) / 100.0
    W, H = img.size
    base = max(W, H)
    margin = int(round(base * _pct(opts.get("margin"), 3.0, 0, 20) / 100.0))
    out = img.convert("RGBA")
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))

    if opts.get("image"):
        src = Path(str(opts["image"]))
        if not src.is_file():
            raise ValueError(f"找不到水印图片：{src}")
        logo = Image.open(src).convert("RGBA")
        tw = max(8, int(round(W * _pct(opts.get("scale"), 12.0, 1, 60) / 100.0)))
        th = max(8, int(round(logo.height * tw / max(1, logo.width))))
        logo = logo.resize((tw, th), Image.LANCZOS)
        a = logo.getchannel("A").point(lambda v: int(v * opacity))
        logo.putalpha(a)
        x, y = _anchored_pos((W, H), (tw, th), position, margin)
        layer.alpha_composite(logo, (x, y))
    else:
        text = str(opts.get("text") or "").strip()
        if not text:
            return img
        fs = int(round(base * _pct(opts.get("font_size"), 4.0, 0, 30) / 100.0)) or 24
        fs = max(10, fs)
        font = ImageFont.truetype(_find_font(str(opts.get("font_file") or "")), fs)
        drawer = ImageDraw.Draw(layer)
        box = drawer.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        x, y = _anchored_pos((W, H), (tw, th), position, margin)
        if opts.get("shadow", True):
            # 描边/阴影：保证白字在亮背景、黑字在暗背景上都读得出来
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (2, 2), (-2, -2)):
                drawer.text((x + dx, y + dy), text, font=font,
                            fill=(0, 0, 0, int(160 * opacity)))
        color = str(opts.get("color") or "#ffffff").lstrip("#")
        try:
            rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            rgb = (255, 255, 255)
        drawer.text((x, y), text, font=font, fill=rgb + (int(255 * opacity),))
    return Image.alpha_composite(out, layer)


def apply_finish(img: Image.Image, finish: dict | None) -> Image.Image:
    """按顺序应用：水印 → 边框（让边框把水印也框在里面，符合"照片+署名"的直觉）。"""
    if not finish:
        return img
    out = add_watermark(img, finish.get("watermark"))
    out = add_border(out, finish.get("border"))
    return out


def describe(finish: dict | None) -> str:
    """给日志/报告用的一句话描述。"""
    if not finish:
        return ""
    bits = []
    wm = finish.get("watermark") or {}
    if wm.get("text"):
        bits.append(f"水印文字「{str(wm['text'])[:20]}」@{wm.get('position', 'br')}")
    elif wm.get("image"):
        bits.append(f"水印图片 {Path(str(wm['image'])).name}@{wm.get('position', 'br')}")
    bd = finish.get("border") or {}
    if bd.get("style"):
        bits.append(f"边框 {bd['style']} {bd.get('width', 4)}%")
    return "；".join(bits)


def validate(finish: dict | None) -> dict:
    """提前校验，避免导出到一半才发现参数错（返回规范化后的副本）。"""
    if not finish:
        return {}
    if not isinstance(finish, dict):
        raise ValueError("finish 必须是对象，例如 {\"watermark\": {\"text\": \"© 2026\"}}")
    out: dict = {}
    wm = finish.get("watermark")
    if isinstance(wm, dict):
        pos = str(wm.get("position") or "br").lower()
        if pos not in POSITIONS:
            raise ValueError(f"水印位置只支持 {', '.join(POSITIONS)}")
        if not str(wm.get("text") or "").strip() and not wm.get("image"):
            raise ValueError("水印需要 text 或 image 之一")
        out["watermark"] = wm
    bd = finish.get("border")
    if isinstance(bd, dict):
        style = str(bd.get("style") or "white").lower()
        if style not in BORDER_STYLES:
            raise ValueError(f"边框样式只支持 {', '.join(BORDER_STYLES)}")
        out["border"] = {**bd, "style": style}
    return out
