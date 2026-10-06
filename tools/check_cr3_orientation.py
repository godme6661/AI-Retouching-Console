"""核查：CR3 内嵌 JPEG 是否带 EXIF 方向标记；以及"零算子管线"是否逐像素等于源图。

我原有的断言抓不到"整幅图被转了 90°"这类错误（尺寸、方差、字节一致性都照样通过），
所以这里专门补两个方向性检查。
"""
from __future__ import annotations
import os

import glob
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from cogitator import raw_io  # noqa: E402

TAGS = {1: "正常", 2: "水平镜像", 3: "旋转180", 4: "垂直镜像", 5: "转置", 6: "顺时针90", 7: "反转置", 8: "逆时针90"}


def main() -> int:
    files = sorted(glob.glob(os.path.join(os.environ.get("COGITATOR_RAW_DIR", "samples"), "*.CR3"))
               + glob.glob(os.path.join(os.environ.get("COGITATOR_RAW_DIR", "samples"), "*.cr3")))
    print(f"检查 {len(files)} 个 CR3 的内嵌预览方向标记：")
    counts: dict[int, int] = {}
    samples: list[tuple[str, int, tuple[int, int]]] = []
    for f in files:
        data = Path(f).read_bytes()
        segs = raw_io.find_embedded_jpegs(data)
        if not segs:
            continue
        off, ln = max(segs, key=lambda t: t[1])
        try:
            with Image.open(io.BytesIO(data[off:off + ln])) as im:
                o = int(im.getexif().get(274, 1) or 1)
                w, h = im.size
        except Exception:
            continue
        counts[o] = counts.get(o, 0) + 1
        if o != 1 and len(samples) < 5:
            samples.append((Path(f).name, o, (w, h)))
    for o, c in sorted(counts.items()):
        print(f"  方向 {o}（{TAGS.get(o, '?')}）：{c} 张")
    if samples:
        print("  带方向标记的样本：")
        for n, o, sz in samples:
            print(f"    {n}  方向={o}({TAGS.get(o)})  尺寸={sz}")

    print("\n零算子管线 vs 源图（应当逐像素一致，若被旋转/翻转就会露馅）：")
    if not files:
        print("  （没有 CR3 可测）")
        return 0
    f = files[-1]
    data = Path(f).read_bytes()
    off, ln = max(raw_io.find_embedded_jpegs(data), key=lambda t: t[1])
    with Image.open(io.BytesIO(data[off:off + ln])) as im:
        src = im.convert("RGB")
        orient = int(im.getexif().get(274, 1) or 1)
    print(f"  源图 {src.size} 方向标记={orient}({TAGS.get(orient)})")

    from cogitator.config import Settings
    from cogitator.document import Project
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cogitator_orient_"))
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = tmp / "p"
    docmod.PROJECTS_DIR.mkdir(parents=True)
    proj = Project.create_from_images([f], settings=Settings.load(), name="零算子")
    scale = proj.preview_scale()
    got, _ = proj.render_full(scale)
    # 源图按同一比例缩小作为参照
    ref_img = src.resize((got.shape[1], got.shape[0]), Image.LANCZOS)
    ref = np.asarray(ref_img, dtype=np.float32) / 255.0
    diff = np.abs(ref - got)
    print(f"  预览 {got.shape[1]}×{got.shape[0]}（scale={scale:.3f}）")
    print(f"  平均差异 {float(diff.mean()):.5f}　最大 {float(diff.max()):.5f}")
    ok = float(diff.mean()) < 0.01
    print(f"  → 零算子管线{'与源图一致' if ok else '与源图不一致（可能被旋转/翻转/色偏）'}")
    # 再测"转 90° 后是否更像"：如果像是被转了，差异会显著变化
    for name, arr in (("旋转90", np.rot90(ref, 1)), ("水平翻转", np.fliplr(ref))):
        if arr.shape == got.shape:
            d = float(np.abs(arr - got).mean())
            print(f"  对照：与{name}后的源图差异 {d:.5f}")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
