"""查清教学模式实际把哪张图送进模型（并用实证展示它与画布的关系）。

用户问：教学看的是画布上的图（用户所见）还是原图？
这个脚本直接把三条路径都跑出来对比尺寸与内容：
  A. TeachingSession._source_image("original")  ← 界面写死用的就是这条
  B. TeachingSession._source_image("current")   ← 后端支持但界面没有入口
  C. 项目预览 preview_path()（= 画布上用户看到的那张）
并演示"图层重排/隐藏"如何改变 A 的含义。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from cogitator import teaching as teach_mod  # noqa: E402
from cogitator.config import Settings  # noqa: E402
from cogitator.document import Project  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="teach_src_"))


def mkimg(path: Path, w: int, h: int, color: tuple[int, int, int]) -> str:
    a = np.zeros((h, w, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = color
    a[: h // 2, : w // 2] = (255, 255, 255)          # 左上角白块，便于辨认
    Image.fromarray(a).save(path, quality=95)
    return str(path)


def brief(p: Path) -> str:
    with Image.open(p) as im:
        arr = np.asarray(im.convert("RGB"), np.float32) / 255
        return (f"{im.size[0]}×{im.size[1]}  亮度均值 {arr.mean():.3f}  "
                f"通道均值 {arr.reshape(-1, 3).mean(0).round(2)}  {p.name}")


def main() -> int:
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = TMP / "projects"
    docmod.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings.load()

    src = mkimg(TMP / "photo.jpg", 1200, 900, (60, 90, 160))
    proj = Project.create_from_images([src], settings=settings, name="教学看图")
    sess = teach_mod.TeachingSession(proj, settings)

    print("== 未编辑时 ==")
    print("  A original :", brief(sess._source_image("original")))
    print("  B current  :", brief(sess._source_image("current")))
    print("  C 画布预览 :", brief(proj.preview_path()))

    print("\n== 做三件常见的编辑：裁切 1:1 + 提亮 + 抠图 ==")
    proj.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"}])
    proj.apply_ops([{"op": "exposure", "value": 0.6, "layer": "L1"}])
    proj.apply_ops([{"op": "remove_bg", "engine": "grabcut", "layer": "L1"}])
    print("  A original :", brief(sess._source_image("original")), " ← 界面固定送这张")
    print("  B current  :", brief(sess._source_image("current")))
    print("  C 画布预览 :", brief(proj.preview_path()))
    st = proj.state()
    print(f"  画布现在 {st['canvas']['w']}×{st['canvas']['h']}；"
          f"A 是否等于画布尺寸：见上（A 仍是未裁切的存图）")

    print("\n== 加一张图当图层、把底图隐藏后，A 的含义会变 ==")
    src2 = mkimg(TMP / "overlay.jpg", 400, 400, (180, 60, 60))
    proj.add_asset(src2, as_layer=True)
    layers_before = [l["id"] for l in proj.doc["layers"]]
    a_before = sess._source_image("original")
    proj.apply_ops([{"op": "set_layer", "layer": layers_before[0], "visible": False}])
    a_after = sess._source_image("original")
    print(f"  底图可见时 A = {a_before.name}")
    print(f"  底图隐藏后 A = {a_after.name}　← 变成了新加的那张图，而不是用户拍的原片")
    return 0


if __name__ == "__main__":
    sys.exit(main())
