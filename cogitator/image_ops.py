"""像素引擎：调色与几何算子，全部是确定性数值运算（不重绘、不生成）。

约定：图像统一为 float32 的 HxWx3 数组，取值范围 0~1；蒙版为 float32 HxW，0~1。
每一步都是纯函数，便于单测与逐步微调。
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable

import numpy as np

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# ---------------------------------------------------------------- 渲染缩放上下文
# 预览不需要按 24MP 全分辨率跑：把源图按显示尺寸缩小后再走同一条管线，
# 并以同一个比例缩放"以像素为单位"的参数（裁切坐标、锐化半径、颗粒尺寸…），
# 得到的正是「全分辨率导出缩小到该尺寸观看」的效果 —— 这与 PS 的 fit-to-screen 预览同理。
# 用线程局部保存，避免污染算子签名（算子参数必须与注册表契约严格一致）。
_SCALE = threading.local()


def set_render_scale(scale: float) -> None:
    _SCALE.value = float(scale)


def render_scale() -> float:
    return float(getattr(_SCALE, "value", 1.0) or 1.0)


def px(value: float) -> float:
    """把以像素为单位的尺寸按当前渲染比例换算。"""
    return float(value) * render_scale()

# ---------------------------------------------------------------- 基础工具


def clip01(a: np.ndarray) -> np.ndarray:
    return np.clip(a, 0.0, 1.0, out=a) if a.dtype == np.float32 else np.clip(a, 0.0, 1.0)


def luma(img: np.ndarray) -> np.ndarray:
    """返回 HxWx1 的亮度。"""
    return (img @ LUMA).astype(np.float32)[..., None]


def _gaussian(arr: np.ndarray, sigma: float) -> np.ndarray:
    """高斯模糊，按可用库降级：cv2 → scipy → 盒式近似。

    大半径时先降采样再模糊再升采样：模糊本身是低频操作，这样在 24MP 上把
    sigma=22 的耗时从 4.0s 降到约 0.15s，肉眼几乎无差别（曾在真实 24MP 图上逐个算子计时得出）。
    """
    if sigma <= 0.03:
        return arr
    try:
        import cv2  # type: ignore

        if sigma > 6.0:
            f = max(2, int(round(sigma / 3.0)))
            h, w = arr.shape[:2]
            nh, nw = max(1, h // f), max(1, w // f)
            small = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)
            blurred = cv2.GaussianBlur(small, (0, 0), sigmaX=float(sigma / f),
                                       sigmaY=float(sigma / f), borderType=cv2.BORDER_REPLICATE)
            return cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)
        return cv2.GaussianBlur(arr, (0, 0), sigmaX=float(sigma), sigmaY=float(sigma),
                                borderType=cv2.BORDER_REPLICATE)
    except Exception:
        pass
    try:
        from scipy.ndimage import gaussian_filter  # type: ignore

        sig = (sigma, sigma, 0) if arr.ndim == 3 else (sigma, sigma)
        return gaussian_filter(arr, sigma=sig, mode="nearest").astype(np.float32)
    except Exception:
        pass
    return _box_blur(arr, max(1, int(round(sigma))))


def _box_blur(arr: np.ndarray, radius: int) -> np.ndarray:
    """不依赖任何库的盒式模糊（三次迭代逼近高斯）。"""
    out = arr.astype(np.float32, copy=True)
    k = radius * 2 + 1
    for _ in range(3):
        for axis in (0, 1):
            pad = [(0, 0)] * out.ndim
            pad[axis] = (radius, radius)
            p = np.pad(out, pad, mode="edge")
            c = np.cumsum(p, axis=axis)
            zero_shape = list(c.shape)
            zero_shape[axis] = 1
            c = np.concatenate([np.zeros(zero_shape, dtype=c.dtype), c], axis=axis)
            sl_hi = [slice(None)] * out.ndim
            sl_lo = [slice(None)] * out.ndim
            sl_hi[axis] = slice(k, None)
            sl_lo[axis] = slice(0, -k)
            out = (c[tuple(sl_hi)] - c[tuple(sl_lo)]) / k
    return out.astype(np.float32)


def _lum_weight(img: np.ndarray, power: float) -> np.ndarray:
    return np.power(luma(img), power).astype(np.float32)


# ---------------------------------------------------------------- 调色算子

def op_exposure(img: np.ndarray, p: dict) -> np.ndarray:
    return img * np.float32(2.0 ** float(p["value"]))


def op_brightness(img: np.ndarray, p: dict) -> np.ndarray:
    return img + np.float32(float(p["value"]) / 100.0 * 0.5)


def op_contrast(img: np.ndarray, p: dict) -> np.ndarray:
    c = float(p["value"]) / 100.0
    factor = np.float32(1.0 + c if c >= 0 else 1.0 / (1.0 - c) if c > -1 else 0.0)
    out = img - np.float32(0.5)          # 原地复用，减少 24MP 上的临时数组
    out *= factor
    out += np.float32(0.5)
    return out


def op_saturation(img: np.ndarray, p: dict) -> np.ndarray:
    s = float(p["value"]) / 100.0
    L = luma(img)
    out = img - L
    out *= np.float32(1.0 + s)
    out += L
    return out


def op_vibrance(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    mx = img.max(axis=-1, keepdims=True)
    mn = img.min(axis=-1, keepdims=True)
    sat = (mx - mn) / np.maximum(mx, 1e-4)
    if v >= 0:
        sat *= -v
        sat += (1.0 + v)                 # 等价于 1 + v*(1-sat)，省一次分配
    else:
        sat[...] = 1.0 + v
    L = luma(img)
    out = img - L
    out *= sat
    out += L
    return out


def op_temperature(img: np.ndarray, p: dict) -> np.ndarray:
    t = float(p["value"]) / 100.0
    out = img.copy()
    out[..., 0] *= np.float32(1.0 + 0.18 * t)
    out[..., 2] *= np.float32(1.0 - 0.18 * t)
    out[..., 1] *= np.float32(1.0 + 0.02 * t)
    return out


def op_tint(img: np.ndarray, p: dict) -> np.ndarray:
    t = float(p["value"]) / 100.0
    out = img.copy()
    out[..., 1] *= np.float32(1.0 - 0.15 * t)
    out[..., 0] *= np.float32(1.0 + 0.05 * t)
    out[..., 2] *= np.float32(1.0 + 0.05 * t)
    return out


def op_highlights(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    w = luma(img)
    np.power(w, 1.6, out=w)
    w *= np.float32(v * 0.45)
    out = img + w
    return out


def op_shadows(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    w = luma(img)
    np.subtract(1.0, w, out=w)
    np.power(w, 1.6, out=w)
    w *= np.float32(v * 0.45)
    return img + w


def op_whites(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    w = luma(img)
    w -= 0.5
    w *= 2.0
    np.clip(w, 0.0, 1.0, out=w)
    w *= np.float32(v * 0.3)
    return img + w


def op_blacks(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    w = luma(img)
    np.subtract(0.5, w, out=w)
    w *= 2.0
    np.clip(w, 0.0, 1.0, out=w)
    w *= np.float32(v * 0.3)
    return img + w


def op_levels(img: np.ndarray, p: dict) -> np.ndarray:
    lo = float(p.get("black", 0) or 0) / 255.0
    hi = float(p.get("white", 255) or 255) / 255.0
    gamma = float(p.get("gamma", 1.0) or 1.0)
    if hi - lo < 1e-4:
        hi = lo + 1e-4
    out = np.clip((img - lo) / (hi - lo), 0.0, 1.0)
    if abs(gamma - 1.0) > 1e-3:
        out = np.power(out, np.float32(1.0 / gamma))
    return out


def op_curve(img: np.ndarray, p: dict) -> np.ndarray:
    pts = p.get("points") or []
    if len(pts) < 2:
        return img
    xs = np.array([pt[0] for pt in pts], dtype=np.float32) / 255.0
    ys = np.array([pt[1] for pt in pts], dtype=np.float32) / 255.0
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    grid = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut = np.interp(grid, xs, ys).astype(np.float32)
    ch = (p.get("channel") or "rgb").lower()
    if ch == "rgb":
        idx = np.clip((img * 255.0).round().astype(np.int32), 0, 255)
        return lut[idx]
    ci = {"r": 0, "g": 1, "b": 2}[ch]
    out = img.copy()
    idx = np.clip((img[..., ci] * 255.0).round().astype(np.int32), 0, 255)
    out[..., ci] = lut[idx]
    return out


def op_hue(img: np.ndarray, p: dict) -> np.ndarray:
    a = math.radians(float(p["value"]))
    c, s = math.cos(a), math.sin(a)
    m = np.array([
        [0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928],
        [0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283],
        [0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072],
    ], dtype=np.float32)
    return img @ m.T


def op_grayscale(img: np.ndarray, p: dict) -> np.ndarray:
    return np.repeat(luma(img), 3, axis=-1)


def op_sepia(img: np.ndarray, p: dict) -> np.ndarray:
    k = float(p.get("value", 60)) / 100.0
    m = np.array([[0.393, 0.769, 0.189],
                  [0.349, 0.686, 0.168],
                  [0.272, 0.534, 0.131]], dtype=np.float32)
    sep = np.clip(img @ m.T, 0.0, 1.0)
    return img * (1.0 - k) + sep * k


def op_clarity(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    sigma = max(2.0, min(img.shape[:2]) / 180.0)
    base = _gaussian(img, sigma)
    return img + (img - base) * np.float32(v * 1.6)


def op_sharpen(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p.get("value", 50)) / 100.0
    # 半径以像素计 → 按渲染比例换算：预览尺寸下锐化本就该"看不见"，
    # 这与「全分辨率导出缩小后观看」的效果一致（PS 的 fit-to-screen 同理）
    radius = max(0.3, px(float(p.get("radius", 1.2) or 1.2)))
    base = _gaussian(img, radius)
    return img + (img - base) * np.float32(v * 1.8)


def op_blur(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p["value"]) / 100.0
    sigma = v * max(3.0, min(img.shape[:2]) / 40.0)
    return _gaussian(img, sigma)


def op_denoise(img: np.ndarray, p: dict) -> np.ndarray:
    v = float(p.get("value", 50)) / 100.0
    if v <= 0.01:
        return img
    sc = render_scale()
    try:
        import cv2  # type: ignore

        u8 = np.clip(img * 255.0, 0, 255).astype(np.uint8)
        d = max(3, int(round(v * 10 * sc)) * 2 + 1)          # 空间核按渲染比例换算
        out = cv2.bilateralFilter(u8, d, v * 90.0, max(1.0, v * 12.0 * sc))
        return out.astype(np.float32) / 255.0
    except Exception:
        sigma = max(0.4, v * 2.0 * sc)
        return _gaussian(img, sigma) * v + img * (1.0 - v)


def op_auto_enhance(img: np.ndarray, p: dict) -> np.ndarray:
    """自动白平衡 + 自动色阶 + 轻微对比/自然饱和度/锐化，按 strength 线性混合。"""
    s = float(p.get("strength", 70)) / 100.0
    if s <= 0.01:
        return img
    out = img

    # 1) 灰度世界白平衡（带限幅，避免把夕阳拍成灰）
    means = out.reshape(-1, 3).mean(axis=0)
    gray = float(means.mean())
    gains = np.clip(gray / np.maximum(means, 1e-4), 0.6, 1.7)
    gains = 1.0 + (gains - 1.0) * s
    out = out * gains.astype(np.float32)

    # 2) 逐通道百分位色阶拉伸
    flat = out.reshape(-1, 3)
    lo = np.percentile(flat, 0.5, axis=0)
    hi = np.percentile(flat, 99.5, axis=0)
    hi = np.where(hi - lo < 1e-3, lo + 1e-3, hi)
    stretched = np.clip((out - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)
    out = out + (stretched - out) * np.float32(s)

    # 3) 收尾修饰
    if s > 0.05:
        out = op_contrast(out, {"value": 10 * s})
        out = op_vibrance(out, {"value": 14 * s})
        out = op_sharpen(out, {"value": 30 * s, "radius": 1.1})
    return out


COLOR_FUNCS: dict[str, Callable[[np.ndarray, dict], np.ndarray]] = {
    "exposure": op_exposure,
    "brightness": op_brightness,
    "contrast": op_contrast,
    "saturation": op_saturation,
    "vibrance": op_vibrance,
    "temperature": op_temperature,
    "tint": op_tint,
    "highlights": op_highlights,
    "shadows": op_shadows,
    "whites": op_whites,
    "blacks": op_blacks,
    "levels": op_levels,
    "curve": op_curve,
    "hue": op_hue,
    "hsl": None,            # 占位，下方定义后回填
    "split_tone": None,
    "vignette": None,
    "grain": None,
    "grayscale": op_grayscale,
    "sepia": op_sepia,
    "clarity": op_clarity,
    "sharpen": op_sharpen,
    "blur": op_blur,
    "denoise": op_denoise,
    "auto_enhance": op_auto_enhance,
}


# ---------------------------------------------------------------- HSL 与调色分级
# 这几个算子是把"美商"落到像素上的关键：全局饱和拉杆是最容易毁图的操作，
# 而分区 HSL / 分离色调 / 暗角 / 颗粒才是专业修图的常规手法。
# 参考：色彩分级（色彩校正 vs 色彩分级、HSL 分区控制、曲线通道语义）
#   https://aperty.ai/zh-hant/blog/color-grading

HSL_BANDS = {"red": 0.0, "orange": 30.0, "yellow": 60.0, "green": 120.0,
             "aqua": 180.0, "blue": 240.0, "purple": 280.0, "magenta": 320.0}


def rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    d = mx - mn
    h = np.zeros_like(mx)
    active = d > 1e-6
    is_r = active & (mx == r)
    is_g = active & (mx == g) & ~is_r
    is_b = active & (mx == b) & ~is_r & ~is_g
    dd = np.where(active, d, 1.0)
    h[is_r] = (((g - b) / dd)[is_r]) % 6.0
    h[is_g] = ((b - r) / dd)[is_g] + 2.0
    h[is_b] = ((r - g) / dd)[is_b] + 4.0
    h = h * 60.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return h.astype(np.float32), s.astype(np.float32), mx.astype(np.float32)


def hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """HSV→RGB（矢量版）。

    刻意不走"堆 6 段再挑一个"的写法：那在 24MP 上要分配约 1.7GB、耗时数秒。
    这里只用 4 个候选值 (v,t,p,q) 拼一张 (...,4) 表，再用查表索引装配。
    """
    h = np.asarray(h, dtype=np.float32)
    s = np.asarray(s, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    c = v * s
    hp = np.mod(h / 60.0, 6.0)
    k = hp.astype(np.int32)
    f = hp - k
    t = v * (1.0 - s * (1.0 - f))
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    table = np.stack([v, t, p, q], axis=-1)                     # (...,4)
    sel = _HSV_IDX[k]                                           # (...,3) int8
    rgb = np.take_along_axis(table, sel.astype(np.intp), axis=-1)
    # 注意：v/t/p/q 已经含了 m = v-c 这个偏移，这里不能再加一次（加了会整体提亮）
    return np.clip(rgb, 0.0, 1.0).astype(np.float32)


# 6 个扇区对应的 (v,t,p,q) 取值组合（0=v 1=t 2=p 3=q），与上式一一对应
_HSV_IDX = np.array([[0, 1, 2], [3, 0, 2], [2, 0, 1],
                     [2, 3, 0], [1, 2, 0], [0, 2, 3]], dtype=np.int8)


def hue_band_weight(h: np.ndarray, center: float, width: float = 45.0) -> np.ndarray:
    """以 center 为中心、width 度半宽的平滑权重（色相是环形的）。"""
    d = np.abs(((h - center + 180.0) % 360.0) - 180.0)
    d /= max(width, 1e-3)
    np.subtract(1.0, d, out=d)
    np.clip(d, 0.0, 1.0, out=d)
    return np.power(d, 1.4, out=d).astype(np.float32, copy=False)


def op_hsl(img: np.ndarray, p: dict) -> np.ndarray:
    """分区 HSL：只动指定色相带（红/橙/黄/绿/青/蓝/紫/品红）。

    这是替代"无脑拉全局饱和度"的正确手法：改绿植、改天空、统一肤色都靠它。
    性能：只在**落入该色带**的像素上做 HSV 往返（通常只占画面一小部分），
    实测 24MP 上从 7.6s 降到约 1s。
    """
    band = str(p.get("color", "red"))
    if band not in HSL_BANDS:
        return img
    width = float(p.get("width", 45) or 45)
    hue_shift = float(p.get("hue", 0) or 0)
    sat_adj = float(p.get("saturation", 0) or 0) / 100.0
    lum_adj = float(p.get("luminance", 0) or 0) / 100.0
    if abs(hue_shift) < 1e-3 and abs(sat_adj) < 1e-4 and abs(lum_adj) < 1e-4:
        return img

    h, s, v = rgb_to_hsv(img)
    w = hue_band_weight(h, HSL_BANDS[band], width)
    idx = np.flatnonzero(w.ravel() > 1e-3)
    if idx.size == 0:
        return img
    hm = h.ravel()[idx]
    sm = s.ravel()[idx]
    vm = v.ravel()[idx]
    wm = w.ravel()[idx]
    hm = np.mod(hm + hue_shift * wm, 360.0)
    sm = np.clip(sm + sat_adj * wm, 0.0, 1.0)
    vm = np.clip(vm * (1.0 + lum_adj * wm), 0.0, 1.0)
    tinted = hsv_to_rgb(hm, sm, vm)                              # 只算 (K,3)
    keep = (1.0 - wm)[:, None]
    out = img.copy()
    flat = out.reshape(-1, 3)
    flat[idx] = tinted * wm[:, None] + flat[idx] * keep
    return out


def _tint_vector(hue_deg: float) -> np.ndarray:
    """取某色相的零均值色偏向量：加上它只改色相不改亮度。"""
    h = np.full((1,), float(hue_deg) % 360.0, dtype=np.float32)
    s = np.ones((1,), np.float32)
    v = np.ones((1,), np.float32)
    rgb = hsv_to_rgb(h, s, v)[0]
    return (rgb - rgb.mean()).astype(np.float32)


def op_split_tone(img: np.ndarray, p: dict) -> np.ndarray:
    """分离色调：高光加一个色、阴影加另一个色（电影感/胶片感的核心手法）。"""
    hl_hue = float(p.get("highlight_hue", 45) or 0)
    hl_sat = float(p.get("highlight_sat", 15) or 0) / 100.0
    sh_hue = float(p.get("shadow_hue", 210) or 0)
    sh_sat = float(p.get("shadow_sat", 15) or 0) / 100.0
    balance = float(p.get("balance", 0) or 0) / 100.0
    if hl_sat <= 1e-4 and sh_sat <= 1e-4:
        return img
    L = luma(img)
    pivot = 0.5 + balance * 0.35
    up = max(0.15, 1.0 - pivot)
    dn = max(0.15, pivot)
    w_hl = np.clip((L - pivot) / up, 0.0, 1.0)
    w_sh = np.clip((pivot - L) / dn, 0.0, 1.0)
    out = img
    if hl_sat > 1e-4:
        out = out + (_tint_vector(hl_hue) * 0.9)[None, None, :] * (w_hl * hl_sat)
    if sh_sat > 1e-4:
        out = out + (_tint_vector(sh_hue) * 0.9)[None, None, :] * (w_sh * sh_sat)
    return out


def op_vignette(img: np.ndarray, p: dict) -> np.ndarray:
    """暗角：正值压暗四角（收拢视线），负值提亮四角（少用，容易显廉价）。"""
    amount = float(p.get("amount", 20) or 0) / 100.0
    feather = max(0.05, min(0.95, float(p.get("feather", 55) or 55) / 100.0))
    if abs(amount) < 1e-4:
        return img
    H, W = img.shape[:2]
    yy = (np.arange(H, dtype=np.float32) - (H - 1) / 2.0) / max(H / 2.0, 1e-6)
    xx = (np.arange(W, dtype=np.float32) - (W - 1) / 2.0) / max(W / 2.0, 1e-6)
    r = np.sqrt((yy[:, None] ** 2 + xx[None, :] ** 2) / 2.0)
    t = np.clip((r - (1.0 - feather)) / feather, 0.0, 1.0)
    mask = (t * t * (3.0 - 2.0 * t)).astype(np.float32)      # smoothstep，避免可见的圆环
    return img * (1.0 - amount * mask)[..., None]


def op_grain(img: np.ndarray, p: dict) -> np.ndarray:
    """胶片颗粒：中间调最明显、高光与暗部收敛；固定种子保证同一文档渲染结果稳定。

    缩放到预览尺寸时，颗粒的尺寸按比例缩小、幅度按比例衰减 —— 这正是
    「全分辨率导出缩小后观看」时颗粒的样子，不是凭空变淡。
    """
    sc = render_scale()
    amount = float(p.get("amount", 12) or 0) / 100.0 * sc
    size = max(0.3, px(float(p.get("size", 1.2) or 1.2)))
    if amount <= 1e-4:
        return img
    H, W = img.shape[:2]
    rng = np.random.default_rng(20260104 + int(amount * 1000) + int(size * 100))
    noise = rng.normal(0.0, 1.0, (H, W)).astype(np.float32)
    if size > 0.6:
        noise = _gaussian(noise, size)
        noise /= max(float(noise.std()), 1e-6)
    L = luma(img)
    mid = (1.0 - np.square(2.0 * L - 1.0)).astype(np.float32)   # 中间调权重
    return img + noise[..., None] * (amount * 0.13) * mid


COLOR_FUNCS.update({
    "hsl": op_hsl,
    "split_tone": op_split_tone,
    "vignette": op_vignette,
    "grain": op_grain,
})


# ---------------------------------------------------------------- 几何算子
# 签名统一：(img, mask_or_None, params) -> (img, mask_or_None)，蒙版与图像同步变换。

def _ratio_wh(ratio: str, w: int, h: int) -> tuple[int, int]:
    a, b = (float(x) for x in ratio.split(":"))
    target = a / b
    cur = w / h
    if cur > target:                    # 太宽 → 收窄
        nw, nh = int(round(h * target)), h
    else:                               # 太高 → 收矮
        nw, nh = w, int(round(w / target))
    return max(1, nw), max(1, nh)


def _apply_crop(img: np.ndarray, mask: np.ndarray | None, x: int, y: int, w: int, h: int):
    H, W = img.shape[:2]
    x = max(0, min(int(x), W - 1))
    y = max(0, min(int(y), H - 1))
    w = max(1, min(int(w), W - x))
    h = max(1, min(int(h), H - y))
    img2 = img[y:y + h, x:x + w]
    mask2 = mask[y:y + h, x:x + w] if mask is not None else None
    return np.ascontiguousarray(img2), (np.ascontiguousarray(mask2) if mask2 is not None else None)


def geom_crop(img, mask, p):
    sc = render_scale()
    return _apply_crop(img, mask, p.get("x", 0) * sc, p.get("y", 0) * sc,
                       p["w"] * sc, p["h"] * sc)


def geom_crop_ratio(img, mask, p):
    H, W = img.shape[:2]
    nw, nh = _ratio_wh(p.get("ratio", "1:1"), W, H)
    anchor = p.get("anchor", "center")
    x = {"center": (W - nw) // 2, "top": (W - nw) // 2, "bottom": (W - nw) // 2,
         "left": 0, "right": W - nw}.get(anchor, (W - nw) // 2)
    y = {"center": (H - nh) // 2, "top": 0, "bottom": H - nh,
         "left": (H - nh) // 2, "right": (H - nh) // 2}.get(anchor, (H - nh) // 2)
    return _apply_crop(img, mask, x, y, nw, nh)


def _rotate_arr(a: np.ndarray, angle: float, expand: bool) -> np.ndarray:
    """旋转（角度为正=顺时针，与用户直觉一致）。

    不用 PIL 的 rotate(expand=True)：它的画幅用 ceil/floor 拼出来，旋转中心与
    图像中心存在半像素偏差，会让内切矩形在贴边处漏出黑角。这里自己算偶数尺寸、
    精确居中的画布，再走 AFFINE 变换，中心永远精确落在 nw/2, nh/2。
    """
    from PIL import Image

    h, w = a.shape[:2]
    if abs(angle) < 1e-6:
        return a
    if expand:
        nw, nh = rotate_canvas_size(w, h, angle)
    else:
        nw, nh = w, h
    rad = math.radians(float(angle))
    ca, sa = math.cos(rad), math.sin(rad)
    # 前向：X = ca*dx - sa*dy, Y = sa*dx + ca*dy（dx,dy 相对图像中心）
    # 逆映射（目标→源）取转置，用于 PIL 的 AFFINE：
    #   xs = ca*(xd-nw/2) + sa*(yd-nh/2) + w/2
    #   ys = -sa*(xd-nw/2) + ca*(yd-nh/2) + h/2
    mat = (ca, sa, w / 2.0 - ca * nw / 2.0 - sa * nh / 2.0,
           -sa, ca, h / 2.0 + sa * nw / 2.0 - ca * nh / 2.0)
    if a.ndim == 2:
        pil = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8), mode="L")
        out = pil.transform((nw, nh), Image.AFFINE, mat, resample=Image.BICUBIC, fillcolor=0)
        return np.asarray(out, dtype=np.float32) / 255.0
    pil = Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), mode="RGB")
    out = pil.transform((nw, nh), Image.AFFINE, mat, resample=Image.BICUBIC, fillcolor=(0, 0, 0))
    return np.asarray(out, dtype=np.float32) / 255.0


def _ceil_snap(v: float, eps: float = 1e-6) -> int:
    """向上取整，但把"几乎就是整数"的浮点误差吸附回整数（cos90° 会算出 50.000000000000004）。"""
    r = round(v)
    if abs(v - r) < eps:
        return int(r)
    return int(math.ceil(v))


def rotate_canvas_size(w: int, h: int, angle_deg: float) -> tuple[int, int]:
    """旋转并扩画幅后的尺寸：取偶数值，使旋转中心精确等于图像中心。"""
    if abs(float(angle_deg)) < 1e-6:
        return max(1, int(w)), max(1, int(h))
    rad = math.radians(float(angle_deg))
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = w / 2.0, h / 2.0
    xs, ys = [], []
    for dx in (-hw, hw):
        for dy in (-hh, hh):
            xs.append(ca * dx - sa * dy)
            ys.append(sa * dx + ca * dy)
    nw = 2 * _ceil_snap(max(abs(v) for v in xs))
    nh = 2 * _ceil_snap(max(abs(v) for v in ys))
    return max(1, nw), max(1, nh)


def geom_rotate(img, mask, p):
    angle = float(p["angle"])
    expand = bool(p.get("expand", True))
    img2 = _rotate_arr(img, angle, expand)
    mask2 = _rotate_arr(mask, angle, expand) if mask is not None else None
    return img2, mask2


def _max_inscribed(w: int, h: int, angle_deg: float) -> tuple[int, int]:
    """旋转后能放进去的最大轴对齐矩形（用于 straighten 去黑边）。"""
    if w <= 0 or h <= 0 or abs(angle_deg) < 1e-6:
        return w, h
    a = math.radians(abs(angle_deg))
    if w < h:
        w, h = h, w
        swapped = True
    else:
        swapped = False
    sin_a, cos_a = math.sin(a), math.cos(a)
    if h <= 2 * sin_a * cos_a * w or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * h
        wr = x / sin_a if sin_a > 1e-10 else w
        hr = x / cos_a if cos_a > 1e-10 else h
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a
    wr, hr = int(wr), int(hr)
    if swapped:
        wr, hr = hr, wr
    return max(1, min(wr, w)), max(1, min(hr, h))


def geom_straighten(img, mask, p):
    """校正倾斜：旋转后按「原图的最大内切矩形」居中裁切，并留安全内缩防漏黑边。"""
    angle = float(p["angle"])
    img2, mask2 = geom_rotate(img, mask, {"angle": angle, "expand": True})
    H, W = img.shape[:2]
    iw, ih = _max_inscribed(W, H, angle)
    margin = max(1, int(round(2 * render_scale())))     # 内缩也按像素换算
    iw = max(1, iw - margin * 2)
    ih = max(1, ih - margin * 2)
    # rotate_canvas_size 取偶数，旋转中心精确在画布中心；裁切框同中心即可
    nw, nh = img2.shape[1], img2.shape[0]
    x, y = (nw - iw) // 2, (nh - ih) // 2
    return _apply_crop(img2, mask2, x, y, iw, ih)


def geom_flip(img, mask, p):
    axis = 1 if p.get("axis", "h") == "h" else 0
    img2 = np.flip(img, axis=axis)
    mask2 = np.flip(mask, axis=axis) if mask is not None else None
    return np.ascontiguousarray(img2), (np.ascontiguousarray(mask2) if mask2 is not None else None)


def geom_resize(img, mask, p):
    from PIL import Image

    sc = render_scale()
    H, W = img.shape[:2]
    w = int(round(int(p.get("w") or 0) * sc))
    h = int(round(int(p.get("h") or 0) * sc))
    scale = float(p.get("scale") or 0)
    if scale > 0:
        nw, nh = max(1, int(round(W * scale))), max(1, int(round(H * scale)))
    elif w > 0 and h > 0:
        nw, nh = w, h
    elif w > 0:
        nw, nh = w, max(1, int(round(H * w / W)))
    elif h > 0:
        nw, nh = max(1, int(round(W * h / H))), h
    else:
        return img, mask
    img2 = np.asarray(Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
                      .resize((nw, nh), Image.LANCZOS), dtype=np.float32) / 255.0
    mask2 = None
    if mask is not None:
        mask2 = np.asarray(Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8), mode="L")
                           .resize((nw, nh), Image.BILINEAR), dtype=np.float32) / 255.0
    return img2, mask2


GEOM_FUNCS: dict[str, Callable[..., Any]] = {
    "crop": geom_crop,
    "crop_ratio": geom_crop_ratio,
    "rotate": geom_rotate,
    "straighten": geom_straighten,
    "flip": geom_flip,
    "resize": geom_resize,
}


def pipeline_size(size: tuple[int, int], ops: list[dict]) -> tuple[int, int]:
    """只算尺寸不碰像素：把当前图层栅格尺寸算给模型/前端看。"""
    w, h = int(size[0]), int(size[1])
    for op in ops:
        name = op.get("op")
        if name == "crop":
            w = max(1, min(int(op.get("w", w)), w))
            h = max(1, min(int(op.get("h", h)), h))
        elif name == "crop_ratio":
            w, h = _ratio_wh(op.get("ratio", "1:1"), w, h)
        elif name == "rotate":
            if op.get("expand", True) and abs(float(op.get("angle", 0))) % 180 > 1e-6:
                w, h = rotate_canvas_size(w, h, float(op["angle"]))
        elif name == "straighten":
            # straighten 以原尺寸求内切矩形，再留 2px 安全内缩
            w, h = _max_inscribed(w, h, float(op.get("angle", 0)))
            w, h = max(1, w - 4), max(1, h - 4)
        elif name == "resize":
            scale = float(op.get("scale") or 0)
            if scale > 0:
                w, h = max(1, int(w * scale)), max(1, int(h * scale))
            elif op.get("w") and op.get("h"):
                w, h = int(op["w"]), int(op["h"])
            elif op.get("w"):
                h = max(1, int(h * int(op["w"]) / w))
                w = int(op["w"])
            elif op.get("h"):
                w = max(1, int(w * int(op["h"]) / h))
                h = int(op["h"])
    return w, h


# ---------------------------------------------------------------- 蒙版工具

def mask_feather(mask: np.ndarray, feather: float) -> np.ndarray:
    if feather and feather > 0:
        return np.clip(_gaussian(mask, float(feather)), 0.0, 1.0).astype(np.float32)
    return mask


def mask_grow(mask: np.ndarray, pixels: float) -> np.ndarray:
    """正值扩张、负值收缩（像素）。"""
    if not pixels or abs(pixels) < 0.5:
        return mask
    k = int(round(abs(pixels))) * 2 + 1
    try:
        import cv2  # type: ignore

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        if pixels > 0:
            return cv2.dilate(mask, kernel)
        return cv2.erode(mask, kernel)
    except Exception:
        from scipy.ndimage import grey_dilation, grey_erosion  # type: ignore

        size = (k, k)
        if pixels > 0:
            return grey_dilation(mask, size=size, mode="nearest").astype(np.float32)
        return grey_erosion(mask, size=size, mode="nearest").astype(np.float32)


def mask_draw_strokes(mask: np.ndarray, strokes: list[dict], width: int, height: int) -> np.ndarray:
    """在蒙版上画笔画：add=恢复（置 1），erase=擦除（置 0）。坐标归一化。"""
    out = mask.copy()
    base = min(width, height)
    for st in strokes:
        pts = [(float(x) * (width - 1), float(y) * (height - 1)) for x, y in st.get("points", [])]
        if not pts:
            continue
        radius = max(1, int(round(float(st.get("radius", 0.04)) * base)))
        val = 1.0 if st.get("mode", "erase") == "add" else 0.0
        # 用椭圆核多次膨胀出圆形笔刷（避免依赖 cv2 的绘制）
        mask_pts = np.zeros((height, width), dtype=np.uint8)
        for x, y in pts:
            xi, yi = int(round(x)), int(round(y))
            x0, x1 = max(0, xi - radius), min(width, xi + radius + 1)
            y0, y1 = max(0, yi - radius), min(height, yi + radius + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            yy, xx = np.ogrid[y0:y1, x0:x1]
            disc = (xx - xi) ** 2 + (yy - yi) ** 2 <= radius * radius
            mask_pts[y0:y1, x0:x1][disc] = 1
        out[mask_pts > 0] = val
    return out


def mask_from_color(img: np.ndarray, x: float, y: float, tolerance: float,
                    invert: bool, base: np.ndarray | None) -> np.ndarray:
    """魔棒：以归一化坐标取色，按容差做软阈值选区。"""
    H, W = img.shape[:2]
    xi = int(round(min(max(x, 0.0), 1.0) * (W - 1)))
    yi = int(round(min(max(y, 0.0), 1.0) * (H - 1)))
    # 取 3x3 平均，抗噪
    y0, y1 = max(0, yi - 1), min(H, yi + 2)
    x0, x1 = max(0, xi - 1), min(W, xi + 2)
    ref = img[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    dist = np.sqrt(((img - ref) ** 2).sum(axis=-1))
    tol = max(0.005, float(tolerance) / 100.0)
    soft = np.clip((tol * 1.6 - dist) / max(tol * 0.9, 1e-4), 0.0, 1.0).astype(np.float32)
    if base is not None:
        soft = np.maximum(base, soft)
    if invert:
        soft = 1.0 - soft
    return soft


def overlay_mask(base: np.ndarray | None, add: np.ndarray) -> np.ndarray:
    if base is None:
        return add
    return np.maximum(base, add)


# ---------------------------------------------------------------- PIL 桥接

def pil_to_float(img) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return np.ascontiguousarray(arr)


def float_to_pil(arr: np.ndarray):
    from PIL import Image

    u8 = (np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return Image.fromarray(u8, mode="RGB")


def mask_to_pil(mask: np.ndarray):
    from PIL import Image

    u8 = (np.clip(mask, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return Image.fromarray(u8, mode="L")


def pil_mask_to_float(img) -> np.ndarray:
    return np.ascontiguousarray(np.asarray(img.convert("L"), dtype=np.float32) / 255.0)


def fit_max_side(arr: np.ndarray, max_side: int) -> np.ndarray:
    from PIL import Image

    H, W = arr.shape[:2]
    if max(H, W) <= max_side:
        return arr
    scale = max_side / float(max(H, W))
    nw, nh = max(1, int(W * scale)), max(1, int(H * scale))
    out = Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8)).resize((nw, nh), Image.LANCZOS)
    return np.asarray(out, dtype=np.float32) / 255.0
