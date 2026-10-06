"""渲染：把「源图 + 算子管线 + 变换/蒙版/混合」合成成一张图。

非破坏性：任何时刻都能从源图重放整条管线得到同一结果；预览与导出走同一条代码路径，
所以"所见即所得"是结构上保证的，而不是靠两边各写一遍。

性能：按（源图 mtime + 管线 JSON + 蒙版文件 mtime）缓存图层栅格，
拖滑杆/撤销重做时只有真正变化的那一层会重算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from . import image_ops as iop
from . import raw_io

RASTER_CACHE: dict[str, np.ndarray] = {}
RASTER_CACHE_MAX = 48


def _cache_put(key: str, value: np.ndarray) -> np.ndarray:
    if len(RASTER_CACHE) >= RASTER_CACHE_MAX:
        for k in list(RASTER_CACHE)[: RASTER_CACHE_MAX // 4]:
            RASTER_CACHE.pop(k, None)
    RASTER_CACHE[key] = value
    return value


def clear_cache() -> None:
    RASTER_CACHE.clear()


def _resolve(base_dir: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else (Path(base_dir) / p)


def load_source(base_dir: Path, rel: str, scale: float = 1.0) -> tuple[np.ndarray, tuple[int, int]]:
    """读原图（float32 RGB），带 mtime 缓存；scale<1 时按显示尺寸缩小后再进管线。

    预览用缩小后的源图：24MP 上这让整条管线从 ~15s 降到 ~1s，而结果与
    「全分辨率导出缩小到该尺寸观看」一致（以像素为单位的参数会同步换算）。
    """
    from PIL import Image

    path = _resolve(base_dir, rel)
    assert path is not None
    st = path.stat()
    key = f"src::{path}::{st.st_mtime_ns}::{round(scale, 4)}"
    cached = RASTER_CACHE.get(key)
    if cached is not None:
        return cached, (cached.shape[1], cached.shape[0])
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        im = raw_io.upright(im)          # 应用 EXIF 方向（PIL 不会自动做）
        if scale < 1.0:
            nw = max(1, int(round(im.width * scale)))
            nh = max(1, int(round(im.height * scale)))
            im = im.resize((nw, nh), Image.LANCZOS)
        rgb = iop.pil_to_float(im)
    _cache_put(key, rgb)
    return rgb, (rgb.shape[1], rgb.shape[0])


def load_mask(base_dir: Path, rel: str) -> np.ndarray | None:
    path = _resolve(base_dir, rel)
    if path is None or not path.is_file():
        return None
    st = path.stat()
    key = f"mask::{path}::{st.st_mtime_ns}"
    cached = RASTER_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import Image

    with Image.open(path) as im:
        m = iop.pil_mask_to_float(im)
    _cache_put(key, m)
    return m


def _mask_op_alpha(base_dir: Path, op: dict, shape: tuple[int, int], current: np.ndarray | None):
    """蒙版类算子：取用已物化的蒙版文件，再套上可调的羽化/扩张/反选。"""
    H, W = shape
    if op.get("op") == "clear_mask":
        return None
    rel = op.get("mask_file")
    if not rel:
        return current
    m = load_mask(base_dir, rel)
    if m is None:
        return current
    if m.shape[:2] != (H, W):                      # 后续几何算子改过尺寸 → 同步缩放
        from PIL import Image

        m = np.asarray(
            Image.fromarray((np.clip(m, 0, 1) * 255).astype(np.uint8), mode="L").resize((W, H), Image.BILINEAR),
            dtype=np.float32,
        ) / 255.0
    if op.get("invert"):
        m = 1.0 - m
    grow = iop.px(float(op.get("grow") or 0))          # 像素单位 → 按渲染比例换算
    if grow:
        m = iop.mask_grow(m, grow)
    feather = iop.px(float(op.get("feather") or 0))
    if feather:
        m = iop.mask_feather(m, feather)
    return np.clip(m, 0.0, 1.0)


def render_layer_raster(layer: dict, base_dir: Path, upto: int | None = None,
                        scale: float = 1.0) -> tuple[np.ndarray, np.ndarray | None]:
    """重放该图层的算子管线，返回 (rgb float32 HxWx3, alpha float32 HxW 或 None)。

    scale<1 时在缩小后的源图上跑同一条管线，并以该比例换算所有"像素单位"的参数。
    """
    ops = list(layer.get("ops") or [])
    if upto is not None:
        ops = ops[:upto]
    mask_key = []
    for op in ops:
        if op.get("mask_file"):
            p = _resolve(base_dir, op["mask_file"])
            if p and p.is_file():
                mask_key.append(f"{p}:{p.stat().st_mtime_ns}")
    raw_src = _resolve(base_dir, layer.get("source", ""))
    src_mtime = raw_src.stat().st_mtime_ns if raw_src and raw_src.is_file() else 0
    src_abs = str(raw_src or layer.get("source"))
    sc = round(float(scale), 4)
    if not mask_key and upto is None:
        key = f"ras::{src_abs}::{src_mtime}::{sc}::{json.dumps(ops, sort_keys=True, ensure_ascii=False)}"
        cached = RASTER_CACHE.get(key)
        if cached is not None:
            return cached, (cached[..., 3] if cached.shape[-1] == 4 else None)

    rgb, _ = load_source(base_dir, layer["source"], scale)
    alpha: np.ndarray | None = None
    prev_scale = iop.render_scale()
    iop.set_render_scale(sc)
    try:
        for op in ops:
            name = op.get("op")
            if name in iop.GEOM_FUNCS:
                rgb, alpha = iop.GEOM_FUNCS[name](rgb, alpha, op)
                rgb = np.clip(rgb, 0.0, 1.0)
            elif name in iop.COLOR_FUNCS:
                rgb = np.clip(iop.COLOR_FUNCS[name](rgb, op), 0.0, 1.0)
            elif name in ("remove_bg", "mask_brush", "mask_from_color", "clear_mask"):
                alpha = _mask_op_alpha(base_dir, op, rgb.shape[:2], alpha)
    finally:
        iop.set_render_scale(prev_scale)
    if upto is None and not mask_key:
        packed = np.dstack([rgb, alpha]) if alpha is not None else rgb
        key = f"ras::{src_abs}::{src_mtime}::{sc}::{json.dumps(ops, sort_keys=True, ensure_ascii=False)}"
        _cache_put(key, packed)
    return rgb, alpha


# ---------------------------------------------------------------- 混合模式

def blend_pixels(mode: str, cb: np.ndarray, cs: np.ndarray) -> np.ndarray:
    """W3C 混合函数 B(Cb, Cs)，输入输出均为 0~1。"""
    if mode in ("normal", "", None):
        return cs
    if mode == "multiply":
        return cb * cs
    if mode == "screen":
        return 1.0 - (1.0 - cb) * (1.0 - cs)
    if mode == "overlay":
        return np.where(cb <= 0.5, 2.0 * cb * cs, 1.0 - 2.0 * (1.0 - cb) * (1.0 - cs))
    if mode == "darken":
        return np.minimum(cb, cs)
    if mode == "lighten":
        return np.maximum(cb, cs)
    if mode == "add":
        return np.clip(cb + cs, 0.0, 1.0)
    if mode == "difference":
        return np.abs(cb - cs)
    if mode == "soft_light":
        d = np.where(cb <= 0.25, ((16.0 * cb - 12.0) * cb + 4.0) * cb, np.sqrt(np.maximum(cb, 0.0)))
        return np.where(cs <= 0.5, cb - (1.0 - 2.0 * cs) * cb * (1.0 - cb),
                        cb + (2.0 * cs - 1.0) * (d - cb))
    return cs


def composite_over(canvas_rgb: np.ndarray, canvas_a: np.ndarray,
                   src_rgb: np.ndarray, src_a: np.ndarray, mode: str) -> None:
    """原地把源合成到画布上（straight alpha + W3C 混合公式）。"""
    as_ = src_a[..., None]
    ab = canvas_a[..., None]
    cb = canvas_rgb
    cs = src_rgb
    b = blend_pixels(mode, cb, cs)
    cs2 = (1.0 - ab) * cs + ab * b
    ao = as_ + ab * (1.0 - as_)
    num = as_ * cs2 + ab * cb * (1.0 - as_)
    with np.errstate(invalid="ignore", divide="ignore"):
        co = np.where(ao > 1e-6, num / np.maximum(ao, 1e-6), cb)
    canvas_rgb[...] = np.clip(co, 0.0, 1.0)
    canvas_a[...] = np.clip(ao[..., 0], 0.0, 1.0)


def _premultiplied_transform(rgb: np.ndarray, a: np.ndarray, layer: dict) -> tuple[np.ndarray, np.ndarray]:
    """按图层字段做翻转/缩放/旋转。预乘 alpha 再变换，避免透明区域的黑边渗色。"""
    from PIL import Image

    flip_h = bool(layer.get("flip_h"))
    flip_v = bool(layer.get("flip_v"))
    scale = float(layer.get("scale", 1.0) or 1.0)
    rot = float(layer.get("rotation", 0.0) or 0.0)
    if flip_h:
        rgb, a = np.flip(rgb, 1), np.flip(a, 1)
    if flip_v:
        rgb, a = np.flip(rgb, 0), np.flip(a, 0)
    if abs(scale - 1.0) < 1e-4 and abs(rot) < 1e-4:
        return np.ascontiguousarray(rgb), np.ascontiguousarray(a)

    pm = np.dstack([rgb * a[..., None], a])
    u8 = (np.clip(pm, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    pil = Image.fromarray(u8, mode="RGBA")
    if abs(scale - 1.0) >= 1e-4:
        nw = max(1, int(round(pil.width * scale)))
        nh = max(1, int(round(pil.height * scale)))
        pil = pil.resize((nw, nh), Image.LANCZOS)
    if abs(rot) >= 1e-4:
        pil = pil.rotate(-rot, resample=Image.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
    out = np.asarray(pil, dtype=np.float32) / 255.0
    rgb_p, a_out = out[..., :3], out[..., 3]
    with np.errstate(invalid="ignore", divide="ignore"):
        rgb = np.where(a_out[..., None] > 1e-4, rgb_p / np.maximum(a_out[..., None], 1e-4), 0.0)
    return np.clip(rgb, 0.0, 1.0), np.clip(a_out, 0.0, 1.0)


def parse_bg(bg: str | None) -> tuple[np.ndarray, float]:
    """返回 (rgb, alpha)：bg 为空/None 表示透明画布。"""
    if not bg:
        return np.zeros(3, dtype=np.float32), 0.0
    s = str(bg).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return np.ones(3, dtype=np.float32), 1.0
    try:
        rgb = np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32) / 255.0
    except ValueError:
        return np.ones(3, dtype=np.float32), 1.0
    return rgb, 1.0


def render_document(doc: dict, base_dir: Path, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """渲染整份文档，返回 (rgb HxWx3, alpha HxW)。

    scale<1 用于预览：整体按比例缩小渲染（画布、图层位置与缩放同步换算），
    得到的是「全分辨率导出缩小到该尺寸观看」的效果。
    """
    sc = max(0.01, float(scale))
    W = max(1, int(round(int(doc["canvas"]["w"]) * sc)))
    H = max(1, int(round(int(doc["canvas"]["h"]) * sc)))
    bg_rgb, bg_a = parse_bg(doc["canvas"].get("bg"))
    rgb = np.empty((H, W, 3), dtype=np.float32)
    rgb[...] = bg_rgb
    alpha = np.full((H, W), bg_a, dtype=np.float32)

    for layer in doc.get("layers", []):
        if not layer.get("visible", True):
            continue
        lrgb, la = render_layer_raster(layer, base_dir, scale=sc)
        opacity = float(layer.get("opacity", 1.0))
        if la is None:
            la = np.ones(lrgb.shape[:2], dtype=np.float32)
        # 注意：栅格已按 sc 缩小，图层自身的 scale 是无量纲倍率，**不能再乘 sc**（乘了就缩小两次）。
        # 需要按 sc 换算的只有画布坐标系里的位置（cx/cy）。
        lrgb, la = _premultiplied_transform(lrgb, la, layer)
        la = la * max(0.0, min(1.0, opacity))
        if float(la.max()) <= 1e-4:
            continue

        cx = float(layer.get("x", doc["canvas"]["w"] / 2.0)) * sc
        cy = float(layer.get("y", doc["canvas"]["h"] / 2.0)) * sc
        lh, lw = lrgb.shape[:2]
        x0 = int(round(cx - lw / 2.0))
        y0 = int(round(cy - lh / 2.0))
        dx0, dy0 = max(0, x0), max(0, y0)
        dx1, dy1 = min(W, x0 + lw), min(H, y0 + lh)
        if dx1 <= dx0 or dy1 <= dy0:
            continue
        sx0, sy0 = dx0 - x0, dy0 - y0
        sub_rgb = lrgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        sub_a = la[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        composite_over(rgb[dy0:dy1, dx0:dx1], alpha[dy0:dy1, dx0:dx1],
                       sub_rgb, sub_a, str(layer.get("blend") or "normal"))
    return rgb, alpha


def flatten_on(rgb: np.ndarray, alpha: np.ndarray, bg: tuple[float, float, float] = (1.0, 1.0, 1.0)):
    a = alpha[..., None]
    return np.clip(rgb * a + np.array(bg, dtype=np.float32) * (1.0 - a), 0.0, 1.0)


def to_pil(rgb: np.ndarray, alpha: np.ndarray | None = None):
    """alpha 为 None → RGB；否则 RGBA（alpha 全 1 时自动降为 RGB）。"""
    from PIL import Image

    u8 = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if alpha is None or float(alpha.min()) >= 0.999:
        return Image.fromarray(u8, mode="RGB")
    au8 = (np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return Image.fromarray(np.dstack([u8, au8]), mode="RGBA")


def histogram_stats(rgb: np.ndarray) -> dict[str, Any]:
    """给文本模型当"眼睛"用的客观指标（教学模式不用，供修图对话参考）。"""
    flat = rgb.reshape(-1, 3)
    lum = (flat @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    p = np.percentile(lum, [1, 5, 25, 50, 75, 95, 99])
    means = flat.mean(axis=0)
    gray = float(means.mean())
    cast = (means / max(gray, 1e-6)).tolist()
    return {
        "平均亮度": round(float(lum.mean()), 3),
        "亮度分位_1_5_25_50_75_95_99": [round(float(v), 3) for v in p],
        "过曝像素占比": round(float((lum > 0.99).mean()), 4),
        "死黑像素占比": round(float((lum < 0.01).mean()), 4),
        "通道均值_RGB": [round(float(v), 3) for v in means],
        "白平衡偏移": [round(float(v), 3) for v in cast],
        "对比度_标准差": round(float(lum.std()), 3),
    }


def sharpness_score(rgb: np.ndarray) -> float:
    """拉普拉斯方差（越小越糊），供教学/评估对话参考。"""
    g = (rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    lap = (g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4.0 * g[1:-1, 1:-1])
    return round(float(lap.var() * 10000.0), 2)
