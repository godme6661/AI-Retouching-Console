"""RAW（CR3 等）支持：优先真解码，缺依赖时取内嵌全尺寸 JPEG 预览。

实测结论（Canon EOS 200D II 的 CR3）：
  * CR3 是 ISO-BMFF 盒容器，`mdat` 里除 RAW 数据外还内嵌一张**全分辨率 JPEG**
    （实测 6000×4000、2.4 MB），因此**零依赖**即可拿到完整像素用于修图；
    另有 1620×1080 的中等预览与 160×120 缩略图（THMB 盒）。
  * 本机未安装 rawpy，也没有 Windows RAW 影像扩展；若将来装了 rawpy，
    本模块会自动改走真正的 RAW 解码（16bit、可救高光），无需改其它代码。

诚实标注：走内嵌预览时拿到的是**相机自己渲染的 8bit JPEG**（已套用于 Picture Style、
已经过影调映射），因此没有 RAW 的高光宽容度；文件里会把所用路径记下来，不假装是 RAW 解码。
"""

from __future__ import annotations

import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RAW_EXTS = {".cr3", ".cr2", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
            ".rw2", ".orf", ".pef", ".dng", ".raw", ".rwl", ".3fr", ".iiq", ".x3f"}
CODEC_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ALL_EXTS = CODEC_EXTS | RAW_EXTS

# 内嵌预览至少要这么大才值得用（过滤 THMB 之类的小缩略图）
MIN_PREVIEW_PIXELS = 400_000        # 约 0.4MP
MIN_PREVIEW_BYTES = 20_000


@dataclass
class Decoded:
    image: object                    # PIL.Image
    method: str                      # rawpy | embedded-jpeg
    note: str = ""
    jpeg_bytes: bytes | None = None  # 走内嵌预览时保留原始 JPEG 字节（可零损直接落盘）
    raw_size: tuple[int, int] | None = None   # 传感器原始尺寸（若已知）


class RawUnsupported(RuntimeError):
    pass


def upright(im):
    """按 EXIF 方向标记摆正图像。

    这条很重要却容易漏：PIL 不会自动应用 Orientation 标记，手机/相机竖拍照片
    常带 6/8 标记，不处理就会在预览与导出里躺倒。本机 228 张 CR3 实测标记全为 1，
    但换相机/手机照片就会遇到，所以统一在这里处理。
    """
    from PIL import ImageOps

    try:
        fixed = ImageOps.exif_transpose(im)
        return fixed if fixed is not None else im
    except Exception:
        return im


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTS


def rawpy_available() -> bool:
    try:
        import rawpy  # noqa: F401

        return True
    except Exception:
        return False


# ---------------------------------------------------------------- WIC（Windows 系统解码器）
# 本机实测（Windows 11 + Microsoft.RawImageExtension 2.5.35.0）：
#   CR3 → 6024×4020 Bgr32（8bpp），比内嵌 JPEG 多出传感器未裁切边缘；
#   高光宽容度明显更好（在 JPEG 已 >0.95 的区域，方差 0.00244 vs 0.00008，约 30 倍），
#   但整体更暗（亮度均值 0.403 vs 0.558），需要自行补影调。
# 代价：整链约 1.5s/张（含 PowerShell 进程启动与 69 MB 临时文件往返），内嵌 JPEG 仅 0.22s。
WIC_DUMP = Path(__file__).resolve().parent / "wic_dump.ps1"
_WIC_CACHE: dict[str, object] = {}


def wic_available() -> bool:
    """本机是否具备 WIC 路径：Windows + 有 PowerShell + 装了 RAW 影像扩展。"""
    return bool(wic_status()["available"])


def wic_status(force: bool = False) -> dict:
    """探测 WIC 路径（结果缓存）：可用性、扩展版本、PowerShell、失败原因。"""
    if not force and "status" in _WIC_CACHE:
        return _WIC_CACHE["status"]  # type: ignore[return-value]
    import shutil as _shutil
    import subprocess

    info: dict = {"available": False, "platform": sys.platform,
                  "powershell": None, "extension": None, "reason": ""}
    if not sys.platform.startswith("win"):
        info["reason"] = "非 Windows 平台，WIC 不可用"
        _WIC_CACHE["status"] = info
        return info
    ps = _shutil.which("powershell.exe") or _shutil.which("pwsh.exe")
    info["powershell"] = ps
    if not ps or not WIC_DUMP.is_file():
        info["reason"] = "找不到 powershell.exe 或包内的 wic_dump.ps1"
        _WIC_CACHE["status"] = info
        return info
    try:
        out = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AppxPackage Microsoft.RawImageExtension | Select-Object -First 1 -ExpandProperty Version)"],
            capture_output=True, text=True, timeout=25)
        ver = (out.stdout or "").strip()
        info["extension"] = ver or None
    except Exception as e:
        info["reason"] = f"查询扩展失败：{type(e).__name__}"
        _WIC_CACHE["status"] = info
        return info
    if info["extension"]:
        info["available"] = True
        info["reason"] = f"已安装 RAW 影像扩展 {info['extension']}"
    else:
        info["reason"] = ("未安装「Raw Image Extension」（可从 Microsoft Store 安装，"
                          "或改用 rawpy / 内嵌预览）")
    _WIC_CACHE["status"] = info
    return info


def decode_with_wic(path: str | Path, max_side: int = 0) -> Decoded:
    """经 WIC（系统 RAW 编解码器）解码：起 PowerShell 取像素 → 读回临时文件。

    临时文件用「头部 + 裸 BGR24」而非 PNG：24MP 的 PNG 编码要数秒，裸字节只要几十毫秒。
    """
    import shutil as _shutil
    import subprocess
    import tempfile

    st = wic_status()
    if not st["available"]:
        raise RawUnsupported(str(st["reason"]))
    ps = str(st["powershell"])
    tmp = Path(tempfile.gettempdir()) / f"wic_{os.getpid()}_{int(time.time()*1000)}.bin"
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(WIC_DUMP), "-Path", str(path), "-Out", str(tmp),
             "-MaxSide", str(int(max_side or 0))],
            capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not tmp.is_file():
            msg = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RawUnsupported(f"WIC 解码失败：{msg[-1][:200] if msg else '未知错误'}")
        import numpy as np

        with tmp.open("rb") as f:
            head = f.read(16)
            if head[:4] != b"WIC1":
                raise RawUnsupported("WIC 输出头部异常")
            w = int.from_bytes(head[4:8], "little")
            h = int.from_bytes(head[8:12], "little")
            ch = int.from_bytes(head[12:16], "little")
            buf = np.frombuffer(f.read(w * h * ch), dtype=np.uint8).reshape(h, w, ch)
            rgb = buf[:, :, ::-1].copy()                       # BGR → RGB
        from PIL import Image

        return Decoded(image=Image.fromarray(rgb, "RGB"), method="wic",
                       note=(f"由 Windows RAW 影像扩展解码（{w}×{h}，8bit，"
                             f"高光层次优于相机内嵌 JPEG，但整体偏暗、需补影调）"),
                       raw_size=(w, h))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def method_status() -> list[dict]:
    """列出每条解码路径在本机的可用性与代价（给界面与排障用）。"""
    wic = wic_status()
    out = [
        {"method": "rawpy", "label": "rawpy / LibRaw",
         "available": rawpy_available(),
         "quality": "16bit 可选，白平衡/去马赛克可控",
         "cost": "进程内，无临时文件",
         "note": "pip install rawpy（Windows x64 wheel 约 0.9 MB，Python 3.9~3.14 均有现成 wheel，可离线打包）"},
        {"method": "wic", "label": "Windows RAW 影像扩展（WIC）",
         "available": bool(wic["available"]),
         "quality": "8bit，编解码器默认渲染（无参数可控）",
         "cost": "约 1.5 秒/张，含进程启动与约 70 MB 临时文件往返",
         "note": wic["reason"]},
        {"method": "embedded-jpeg", "label": "文件内嵌 JPEG 预览",
         "available": True,
         "quality": "8bit，相机已渲染（无 RAW 宽容度）",
         "cost": "约 0.2 秒/张，零依赖、零临时文件",
         "note": "永远可用；本机 CR3 实测内嵌 6000×4000 全分辨率"},
    ]
    return out


def resolve_method(prefer: str = "auto") -> str:
    """决定实际使用哪条路径。auto = rawpy → wic → 内嵌预览（有序回退）。"""
    prefer = (prefer or "auto").lower()
    if prefer in ("rawpy", "wic", "preview", "embedded-jpeg"):
        return "embedded-jpeg" if prefer == "preview" else prefer
    if rawpy_available():
        return "rawpy"
    if wic_available():
        return "wic"
    return "embedded-jpeg"


# ---------------------------------------------------------------- 内嵌预览

def find_embedded_jpegs(data: bytes, min_bytes: int = MIN_PREVIEW_BYTES) -> list[tuple[int, int]]:
    """扫描 SOI..EOI，返回 (偏移, 长度) 列表。RAW 里的预览图就是普通 JPEG。"""
    out: list[tuple[int, int]] = []
    pos = 0
    n = len(data)
    while pos < n - 3:
        i = data.find(b"\xff\xd8\xff", pos)
        if i < 0:
            break
        j = data.find(b"\xff\xd9", i + 3)
        if j < 0:
            break
        ln = j + 2 - i
        if ln >= min_bytes:
            out.append((i, ln))
        pos = j + 2
    return out


def embedded_preview(path: str | Path, min_pixels: int = MIN_PREVIEW_PIXELS) -> Decoded | None:
    """取出 RAW 里最大的内嵌 JPEG 预览；取不到返回 None。"""
    from PIL import Image

    p = Path(path)
    data = p.read_bytes()
    best: tuple[int, int, tuple[int, int]] | None = None   # (score, offset, size)
    for off, ln in find_embedded_jpegs(data):
        blob = data[off:off + ln]
        try:
            with Image.open(io.BytesIO(blob)) as im:
                w, h = im.size
                im.verify()                                # 触发一次完整校验
        except Exception:
            continue
        if w * h < min_pixels:
            continue
        if best is None or w * h > best[0]:
            best = (w * h, off, (w, h))
    if best is None:
        return None
    _score, off, size = best
    ln = next(l for o, l in find_embedded_jpegs(data) if o == off)
    blob = data[off:off + ln]
    img = upright(Image.open(io.BytesIO(blob)))
    img.load()
    return Decoded(image=img.convert("RGB"), method="embedded-jpeg",
                   note=f"取自 CR3 内嵌 JPEG 预览（{size[0]}×{size[1]}，相机已渲染的 8bit 图，无 RAW 宽容度）",
                   jpeg_bytes=blob, raw_size=size)


# ---------------------------------------------------------------- 真解码

def decode_with_rawpy(path: str | Path, half_size: bool = False) -> Decoded:
    """真 RAW 解码。**本机未安装 rawpy，这条分支未经实测**（只保证与内嵌预览路径同签名、同语义）。"""
    import rawpy  # type: ignore

    from PIL import Image

    with rawpy.imread(str(path)) as raw:
        try:
            raw_size = (int(raw.sizes.width), int(raw.sizes.height))
        except Exception:
            raw_size = None
        flip = int(getattr(raw.sizes, "flip", 0) or 0)
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False,
                              output_bps=8, half_size=half_size)
    img = Image.fromarray(rgb)
    # LibRaw 的 flip 约定：0 不转、3 转 180°、5 逆时针 90°、6 顺时针 90°
    rot = {3: 180, 5: 90, 6: -90}.get(flip)
    if rot:
        img = img.rotate(rot, expand=True)
    return Decoded(image=img, method="rawpy",
                   note=f"由 rawpy/LibRaw 解码（{img.width}×{img.height}，相机白平衡，8bit 输出）",
                   raw_size=raw_size)


# ---------------------------------------------------------------- 统一入口

def decode(path: str | Path, prefer: str = "auto", half_size: bool = False) -> Decoded:
    """解码任意受支持的图片。

    RAW 按 `prefer` 选择路径：auto = rawpy → WIC → 内嵌 JPEG（有序回退，永远有一条能用）；
    也可显式指定 rawpy / wic / preview。普通图片走 PIL。

    注意：无论走哪条路径，导入时都会把解码结果**物化**成工程源图（见 convert_to_source），
    因此已建工程在不同机器上渲染完全一致——解码差异只影响"导入那一刻"。
    """
    from PIL import Image

    p = Path(path)
    if not p.is_file():
        raise RawUnsupported(f"文件不存在：{p}")
    ext = p.suffix.lower()
    if ext not in ALL_EXTS:
        raise RawUnsupported(f"不支持的格式：{ext}")

    if ext in RAW_EXTS:
        method = resolve_method(prefer)
        errors: list[str] = []
        # 显式指定时只用那一条；auto 时按序回退
        chain = [method] if prefer not in ("auto", "", None) else [method, "embedded-jpeg"]
        seen: set[str] = set()
        for m in chain:
            if m in seen:
                continue
            seen.add(m)
            try:
                if m == "rawpy":
                    if not rawpy_available():
                        raise RawUnsupported("未安装 rawpy")
                    return decode_with_rawpy(p, half_size=half_size)
                if m == "wic":
                    return decode_with_wic(p)
                got = embedded_preview(p)
                if got is None:
                    raise RawUnsupported("文件里没有可用的内嵌预览")
                return got
            except RawUnsupported as e:
                errors.append(f"{m}: {e}")
                continue
        raise RawUnsupported(
            f"{p.name} 无法解码（{'；'.join(errors)}）。可安装 rawpy：pip install rawpy")

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(p) as im:
        im.load()
        return Decoded(image=upright(im).convert("RGB"), method="pil", note="")


def probe(path: str | Path) -> dict:
    """给界面/接口用：报告这个 RAW 在本机会用哪条路径、能拿到什么（等价于 prefer=auto）。"""
    return probe_with(path, "auto")


def probe_with(path: str | Path, prefer: str = "auto") -> dict:
    """给界面/接口用：报告这个 RAW 在本机会用哪条路径、能拿到什么。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": "文件不存在"}
    if not is_raw(p):
        try:
            with Image.open(p) as im:
                fixed = upright(im)
                return {"ok": True, "raw": False, "size": [fixed.width, fixed.height],
                        "method": "pil", "rawpy": rawpy_available(),
                        "methods": method_status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    chosen = resolve_method(prefer)
    info: dict = {"ok": True, "raw": True, "file": p.name,
                  "mb": round(p.stat().st_size / 1048576, 1),
                  "method": chosen, "rawpy": rawpy_available(),
                  "wic": wic_status(),
                  "methods": method_status()}
    try:
        dec = decode(p, prefer=chosen)
        info.update({"size": list(dec.image.size), "note": dec.note})
    except RawUnsupported as e:
        info.update({"ok": False, "error": str(e)})
    return info


def probe_with(path: str | Path, prefer: str = "auto") -> dict:
    """给界面/接口用：报告这个 RAW 在本机会用哪条路径、能拿到什么。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": "文件不存在"}
    if not is_raw(p):
        try:
            with Image.open(p) as im:
                fixed = upright(im)
                return {"ok": True, "raw": False, "size": [fixed.width, fixed.height],
                        "method": "pil", "rawpy": rawpy_available()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    chosen = resolve_method(prefer)
    info: dict = {"ok": True, "raw": True, "file": p.name,
                  "mb": round(p.stat().st_size / 1048576, 1),
                  "method": chosen, "rawpy": rawpy_available(),
                  "wic": wic_status()["available"],
                  "methods": method_status()}
    try:
        dec = decode(p, prefer=prefer if prefer != "auto" else chosen)
        info.update({"size": list(dec.image.size), "note": dec.note})
    except RawUnsupported as e:
        info.update({"ok": False, "error": str(e)})
    return info


def convert_to_source(src: str | Path, dest_dir: Path, stem: str,
                      prefer: str = "auto") -> tuple[Path, dict]:
    """把任意图片准备成工程可用的「源图」文件，返回 (落盘路径, 元信息)。

    RAW 走内嵌预览时**直接写出原始 JPEG 字节**（零重编码损失）；走 rawpy 时存 JPEG q95。
    """
    from PIL import Image

    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(src).suffix.lower()
    meta: dict = {"original": str(src), "original_ext": ext}
    if ext not in RAW_EXTS:
        out = dest_dir / f"{stem}{ext}"
        import shutil

        shutil.copy2(src, out)                 # 字节原样复制（含 EXIF，方向标记留给渲染时应用）
        # 尺寸要按**摆正后**的算：渲染时会应用 EXIF 方向，两边必须一致，否则几何全错
        with Image.open(out) as im:
            fixed = upright(im)
            meta["size"] = [int(fixed.width), int(fixed.height)]
        meta.update({"decoded": "copy", "source_kind": "image"})
        return out, meta

    dec = decode(src, prefer=prefer)
    meta.update({"decoded": dec.method, "raw_size": list(dec.raw_size) if dec.raw_size else None,
                 "note": dec.note, "source_kind": "raw"})
    if dec.jpeg_bytes:
        out = dest_dir / f"{stem}.jpg"                  # 原始字节直接落盘，不再编码
        out.write_bytes(dec.jpeg_bytes)
        meta["stored"] = "embedded-jpeg(byte-exact)"
    else:
        out = dest_dir / f"{stem}.jpg"
        dec.image.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
        meta["stored"] = "jpeg-q95"
    meta["size"] = list(dec.image.size)
    return out, meta
