"""生成一张合成"照片"，供文档截图使用。

为什么不直接用真实照片：公开仓库里的示例图不应包含任何个人拍摄内容。
这张图用渐变天空、山脊、太阳、人物剪影与色块合成，能体现调色/抠图/对比等功能，
且完全由程序生成、可自由分发。

用法：python tools/make_demo_photo.py [输出路径]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1800, 1200


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/demo/demo-photo.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = yy / H
    img = np.zeros((H, W, 3), np.float32)
    # 天空渐变（暖橙 → 青蓝），方便演示色温与分离色调
    img[..., 0] = 0.98 - 0.55 * t
    img[..., 1] = 0.72 - 0.28 * t
    img[..., 2] = 0.45 + 0.35 * t

    # 太阳 + 光晕
    cx, cy, r = W * 0.72, H * 0.28, 90
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    glow = np.clip(1.0 - d / (r * 6), 0, 1) ** 2
    img += glow[..., None] * np.array([0.5, 0.35, 0.1], np.float32)
    img[d < r] = [1.0, 0.95, 0.8]

    # 远山两层（用正弦叠加出山脊）
    for layer, (base, amp, col, freq) in enumerate([
            (0.62, 90, (0.32, 0.36, 0.45), 3.1),
            (0.72, 60, (0.20, 0.24, 0.30), 5.3)]):
        ridge = (base * H + amp * np.sin(xx / W * math.pi * freq + layer)
                 + amp * 0.4 * np.sin(xx / W * math.pi * freq * 2.7))
        mask = yy > ridge
        img[mask] = col

    pil = Image.fromarray((np.clip(img, 0, 1) * 255).astype("uint8"), "RGB")
    d = ImageDraw.Draw(pil, "RGBA")

    # 前景地面
    d.rectangle([0, int(H * 0.86), W, H], fill=(28, 34, 30, 255))
    # 人物剪影（演示抠图会聚焦的"主体"）
    px, py = int(W * 0.30), int(H * 0.86)
    d.ellipse([px - 34, py - 330, px + 34, py - 262], fill=(24, 26, 34, 255))     # 头
    d.polygon([(px - 78, py), (px - 52, py - 250), (px + 52, py - 250), (px + 78, py)],
              fill=(24, 26, 34, 255))                                             # 身子
    d.line([(px, py - 250), (px - 96, py - 150)], fill=(24, 26, 34, 255), width=18)
    d.line([(px, py - 250), (px + 96, py - 150)], fill=(24, 26, 34, 255), width=18)
    # 几个色块（方便演示 HSL / 饱和度 / 黑白）
    for i, c in enumerate([(200, 60, 60), (60, 170, 90), (70, 110, 210), (215, 190, 70)]):
        x0 = int(W * 0.58) + i * 150
        d.rounded_rectangle([x0, int(H * 0.70), x0 + 120, int(H * 0.70) + 120],
                            radius=16, fill=c + (235,))

    pil = pil.filter(ImageFilter.GaussianBlur(0.6))
    pil.save(out, quality=95)
    print(f"已生成合成示例图：{out}（{pil.size[0]}×{pil.size[1]}，{out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
