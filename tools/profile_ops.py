"""在真实 24MP 尺寸上逐个算子计时，找出渲染慢在哪里。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from cogitator import image_ops as iop  # noqa: E402
from cogitator import render, styles  # noqa: E402

H, W = 4000, 6000


def bench(name: str, fn, repeat: int = 1) -> float:
    fn()                                     # 预热
    t0 = time.time()
    for _ in range(repeat):
        fn()
    dt = (time.time() - t0) / repeat
    print(f"  {name:<28} {dt * 1000:8.1f} ms")
    return dt


def main() -> int:
    print(f"图像尺寸 {W}×{H} = {W*H/1e6:.1f} MP，float32 单通道 {W*H*4/1048576:.0f} MB，RGB {W*H*12/1048576:.0f} MB\n")
    rng = np.random.default_rng(7)
    img = rng.random((H, W, 3), dtype=np.float32)
    print("== 基础设施 ==")
    bench("np.zeros 尺寸", lambda: np.zeros((H, W, 3), np.float32))
    bench("img * 2.0（一次乘）", lambda: img * 2.0)
    bench("np.clip（一次裁剪）", lambda: np.clip(img, 0, 1))
    bench("luma 点积", lambda: img @ iop.LUMA)
    bench("cv2 高斯 sigma=3", lambda: iop._gaussian(img, 3.0))
    bench("cv2 高斯 sigma=12", lambda: iop._gaussian(img, 12.0))
    bench("np.percentile 0.5%", lambda: np.percentile(img.reshape(-1, 3), 0.5, axis=0))

    print("\n== 典型算子（真实尺寸）==")
    total = 0.0
    for name, params in [
        ("exposure", {"value": 0.12}), ("shadows", {"value": 14}), ("highlights", {"value": -8}),
        ("contrast", {"value": -10}), ("saturation", {"value": -8}), ("vibrance", {"value": 10}),
        ("temperature", {"value": -6}), ("tint", {"value": 3}),
        ("hsl", {"color": "green", "hue": -10, "saturation": -14, "luminance": 10}),
        ("split_tone", {"highlight_hue": 50, "highlight_sat": 6, "shadow_hue": 190, "shadow_sat": 8}),
        ("clarity", {"value": -8}), ("sharpen", {"value": 15, "radius": 1.0}),
        ("grain", {"amount": 6, "size": 1.1}),
        ("auto_enhance", {"strength": 45}),
        ("vignette", {"amount": 8, "feather": 65}),
    ]:
        fn = iop.COLOR_FUNCS[name]
        total += bench(name, lambda fn=fn, p=params: fn(img, p))
    print(f"  {'—— 合计':<28} {total * 1000:8.1f} ms")

    print("\n== 预览链路 ==")
    bench("fit_max_side 1400（缩小）", lambda: iop.fit_max_side(img, 1400))
    small = iop.fit_max_side(img, 1400)
    t = time.time()
    for name, params in [("sharpen", {"value": 15, "radius": 1.0}),
                         ("clarity", {"value": -8}), ("split_tone", {"highlight_hue": 50, "highlight_sat": 6}),
                         ("hsl", {"color": "green", "hue": -10, "saturation": -14})]:
        iop.COLOR_FUNCS[name](small, params)
    print(f"  {'4 个空间算子 @1400px':<28} {(time.time()-t)*1000:8.1f} ms   "
          f"（同 4 个 @24MP 约 {sum([iop.COLOR_FUNCS[n](img, p) and 0 or 0 for n, p in []] or [0]) }）")

    print("\n== 引擎内部：自动增强的分步耗时 ==")
    bench("auto_enhance 全量", len and (lambda: iop.op_auto_enhance(img, {"strength": 45})))
    flat = img.reshape(-1, 3)
    bench("  └ 灰度世界均值", lambda: flat.mean(axis=0))
    bench("  └ 双百分位拉伸", lambda: (np.percentile(flat, 0.5, axis=0), np.percentile(flat, 99.5, axis=0)))
    bench("  └ 逐通道归一化", lambda: np.clip((img - 0.4) / 0.3, 0, 1))

    print("\n== JPEG 预览写盘 ==")
    bench("to_pil 24MP → uint8", lambda: (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
    return 0


if __name__ == "__main__":
    sys.exit(main())
