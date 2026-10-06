"""探测 CR3（佳能 RAW）在本机可用的解码路径。

三条候选路径，都要用实测数据说话而不是猜：
  1) rawpy / LibRaw —— 真正的 RAW 解码（16bit、可救高光），需额外安装；
  2) CR3 内嵌 JPEG 预览 —— ISO-BMFF 盒结构里就有，零依赖、离线可用，但分辨率有限；
  3) Windows WIC（RAW 影像扩展）—— 装了扩展就能全分辨率解码，用 .NET 调。
"""
from __future__ import annotations
import os

import re
import struct
import sys
from pathlib import Path

FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 盒结构
def walk_boxes(data: bytes, start: int = 0, end: int | None = None, depth: int = 0,
               max_depth: int = 3) -> list[dict]:
    """遍历 ISO-BMFF 盒。返回 [{type, offset, size, uuid, children}]。"""
    end = len(data) if end is None else end
    out: list[dict] = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8].decode("latin-1")
        header = 8
        if size == 1:                                   # 64 位长度
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        node = {"type": typ, "offset": pos, "size": size, "uuid": None, "children": []}
        if typ == "uuid" and pos + header + 16 <= end:
            node["uuid"] = data[pos + header:pos + header + 16].hex()
        out.append(node)
        # 只有容器盒才继续下钻
        if typ in ("moov", "trak", "mdia", "minf", "stbl", "uuid", "moof", "traf") and depth < max_depth:
            inner = pos + header + (16 if typ == "uuid" else 0)
            node["children"] = walk_boxes(data, inner, pos + size, depth + 1, max_depth)
        pos += size
    return out


def find_jpegs(data: bytes, min_size: int = 20000) -> list[tuple[int, int]]:
    """扫描 SOI..EOI 得到内嵌 JPEG 段（CR3 里的预览图就是普通 JPEG）。"""
    out: list[tuple[int, int]] = []
    pos = 0
    while True:
        i = data.find(b"\xff\xd8\xff", pos)
        if i < 0:
            break
        j = data.find(b"\xff\xd9", i + 3)
        if j < 0:
            break
        ln = j + 2 - i
        if ln >= min_size:
            out.append((i, ln))
        pos = j + 2
    return out


def jpeg_size(blob: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(blob)) as im:
            return im.size
    except Exception:
        return None


def main() -> int:
    import glob

    print("== 候选路径 1：rawpy / LibRaw ==")
    try:
        import rawpy  # type: ignore

        print(f"  rawpy 已安装：{getattr(rawpy, '__version__', '?')}")
        raw_ok = True
    except Exception:
        print("  rawpy 未安装（需要时才装）")
        raw_ok = False

    print("\n== 候选路径 3：Windows WIC / RAW 影像扩展 ==")
    ext = list(Path(r"C:\Program Files\WindowsApps").glob("*RawImageExtension*")) + \
          list(Path(r"C:\Program Files\WindowsApps").glob("*RawImage*"))
    print(f"  RawImageExtension 包目录：{len(ext)} 个")
    for e in list(Path(r"C:\Program Files\WindowsApps").glob("*RawImage*"))[:3]:
        print("   ", e.name)

    files = sorted(glob.glob(os.path.join(os.environ.get("COGITATOR_RAW_DIR", "samples"), "*.CR3"))
               + glob.glob(os.path.join(os.environ.get("COGITATOR_RAW_DIR", "samples"), "*.cr3")))
    if not files:
        print("\nE 盘没有找到 CR3")
        return 1
    sample = files[-1]
    print(f"\n== 候选路径 2：解析 CR3 盒结构（样本 {Path(sample).name}）==")
    data = Path(sample).read_bytes()
    print(f"  文件大小 {len(data) / 1024 / 1024:.1f} MB")
    top = walk_boxes(data)
    print("  顶层盒：", ", ".join(f"{b['type']}({b['size']})" for b in top[:8]))
    uuids: list[str] = []

    def collect(nodes: list[dict]) -> None:
        for n in nodes:
            if n["uuid"]:
                uuids.append(n["uuid"])
            collect(n["children"])

    collect(top)
    print(f"  uuid 盒 {len(uuids)} 个：{uuids[:6]}")

    thumbs = []

    def collect_thumbs(nodes: list[dict]) -> None:
        for n in nodes:
            if n["type"] in ("THMB", "PRVW", "thmb", "prvw"):
                thumbs.append(n)
            collect_thumbs(n["children"])

    collect_thumbs(top)
    print(f"  THMB/PRVW 盒 {len(thumbs)} 个：" +
          ", ".join(f"{t['type']}@{t['offset']}({t['size']})" for t in thumbs[:6]))

    jpgs = find_jpegs(data)
    print(f"  扫描到内嵌 JPEG {len(jpgs)} 段：")
    best = None
    for off, ln in sorted(jpgs, key=lambda t: -t[1])[:5]:
        size = jpeg_size(data[off:off + ln])
        print(f"    @{off:<10} {ln / 1024:8.1f} KB  尺寸={size}")
        if size and (best is None or size[0] * size[1] > best[1][0] * best[1][1]):
            best = ((off, ln), size)
    check(bool(jpgs), "CR3 里能扫到内嵌 JPEG")
    check(best is not None and best[1][0] >= 1000,
          f"最大预览分辨率 {best[1] if best else None}")
    if best:
        out = Path(__file__).resolve().parent.parent / ".verify" / "cr3_preview.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data[best[0][0]:best[0][0] + best[0][1]])
        print(f"  已导出预览：{out}  {out.stat().st_size / 1024:.0f} KB")

    m = re.search(rb"\x00(Canon|CANON)\x00", data[:200000])
    model = re.search(rb"(EOS [A-Z0-9 ]{2,12}|Canon EOS [A-Z0-9 ]{2,12})", data[:400000])
    print(f"  相机标识：{model.group(0).decode('latin-1', 'ignore') if model else '未识别'}")

    print("\n== 结论 ==")
    print(f"  rawpy 可用：{raw_ok}　内嵌预览可用：{bool(best)}"
          + (f"（{best[1][0]}x{best[1][1]}）" if best else ""))
    print("  → 无 rawpy 时，可用内嵌预览保证 CR3 能被打开与编辑（分辨率有限）；")
    print("     若要全分辨率 RAW 解码（16bit、救高光），需要安装 rawpy。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
