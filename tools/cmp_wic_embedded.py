"""对比 WIC 解码结果与 CR3 内嵌 JPEG：尺寸、亮度、高光/暗部、逐像素差异。

用来回答"WIC 的 RAW 解码到底带来了什么"——是真正的 RAW 渲染，还是仅仅把内嵌预览重包一层。
"""
from __future__ import annotations
import os

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from cogitator import raw_io

VERIFY = Path(__file__).resolve().parent.parent / ".verify"
SRC = os.environ.get("COGITATOR_RAW_SAMPLE", "")
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def stats(name: str, arr: np.ndarray) -> None:
    lum = arr @ LUMA
    p = np.percentile(lum, [1, 50, 99])
    print(f"  {name:12} {arr.shape[1]}x{arr.shape[0]}  亮度均值 {lum.mean():.3f}  "
          f"分位 1/50/99% = {p[0]:.3f}/{p[1]:.3f}/{p[2]:.3f}  "
          f"过曝 {(lum > 0.99).mean()*100:.2f}%  死黑 {(lum < 0.01).mean()*100:.2f}%  "
          f"通道均值 {arr.reshape(-1, 3).mean(0).round(3)}")


def main() -> int:
    wic_path = VERIFY / "wic_decode.png"
    if not wic_path.is_file():
        print("没有 wic_decode.png，先跑 check_wic.ps1")
        return 1
    dec = raw_io.embedded_preview(SRC)
    emb_path = VERIFY / "embedded_jpeg.jpg"
    emb_path.write_bytes(dec.jpeg_bytes)

    wic = Image.open(wic_path).convert("RGB")
    emb = Image.open(emb_path).convert("RGB")
    print("=== 尺寸 ===")
    print(f"  WIC 解码      {wic.size}")
    print(f"  内嵌 JPEG     {emb.size}")
    print(f"  差异          {wic.size[0]-emb.size[0]} x {wic.size[1]-emb.size[1]}"
          "（WIC 多出来的是传感器未裁切边缘）")

    # 对齐：以内嵌 JPEG 为基准，从 WIC 图的中心裁出同尺寸（两图都是整幅，边缘多几像素）
    W, H = emb.size
    ox = (wic.size[0] - W) // 2
    oy = (wic.size[1] - H) // 2
    wic_c = wic.crop((ox, oy, ox + W, oy + H))
    a = np.asarray(wic_c, np.float32) / 255.0
    b = np.asarray(emb, np.float32) / 255.0

    print("\n=== 统计（同一区域）===")
    stats("WIC", a)
    stats("内嵌JPEG", b)

    print("\n=== 逐像素差异（WIC vs 内嵌 JPEG，同区域）===")
    d = np.abs(a - b)
    print(f"  平均 {d.mean():.4f}   中位 {np.median(d):.4f}   95 分位 {np.percentile(d, 95):.4f}   最大 {d.max():.3f}")
    # 若两者只是同一数据的不同压缩，差异应集中在高频；若是不同渲染，差异会整体偏大
    print(f"  平均每通道差异 >0.05 的像素占比 {(d.mean(-1) > 0.05).mean()*100:.2f}%")
    lum_a, lum_b = a @ LUMA, b @ LUMA
    print(f"  亮度方向差异均值 {(lum_a - lum_b).mean():+.4f}（正=WIC 更亮）")

    # 高光宽容度：统计接近过曝区域的细节保留
    hi = lum_b > 0.95
    if hi.any():
        print(f"\n=== 高光区（以内嵌 JPEG 的 >0.95 区域为准，{hi.mean()*100:.2f}% 像素）===")
        print(f"  该区域 WIC 亮度均值 {lum_a[hi].mean():.3f}  内嵌 JPEG {lum_b[hi].mean():.3f}")
        print(f"  该区域 WIC 方差 {lum_a[hi].var():.5f}  内嵌 JPEG 方差 {lum_b[hi].var():.5f}")
        print("  （方差更大说明该区域在 WIC 版里还有层次，即高光宽容度更好）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
