"""用真实 CR3 跑一批：原图 / 自动增强 / 日系空气感 三列对比图，并记录耗时。

用法：
    python tools/batch_demo.py [--dir "E:\\DCIM\\100CANON"] [--count 4] [--style jp-airy]
"""
from __future__ import annotations
import os

import argparse
import glob
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from cogitator.config import Settings  # noqa: E402
from cogitator.document import Project  # noqa: E402

CELL_W = 620
LABEL_H = 26


def spread(files: list[str], count: int) -> list[str]:
    """按时间均匀取样，覆盖不同日期/题材，而不是只看最近几张。"""
    if count >= len(files):
        return files
    step = len(files) / float(count)
    return [files[int(i * step)] for i in range(count)]


def cell(proj: Project, label: str) -> Image.Image:
    """取当前工程的预览图，缩放到统一宽度并加标签。"""
    src = Image.open(proj.preview_path()).convert("RGB")
    h = int(src.height * CELL_W / src.width)
    src = src.resize((CELL_W, h), Image.LANCZOS)
    canvas = Image.new("RGB", (CELL_W, h + LABEL_H), (24, 26, 32))
    canvas.paste(src, (0, LABEL_H))
    d = ImageDraw.Draw(canvas)
    d.text((8, 6), label, fill=(230, 233, 240))
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.environ.get("COGITATOR_RAW_DIR", "samples"))
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--style", default="jp-airy")
    ap.add_argument("--out", default=".verify/raw_batch_sheet.jpg")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.dir) / "*.CR3")))
    if not files:
        print(f"目录里没有 CR3：{args.dir}")
        return 1
    picked = spread(files, args.count)
    print(f"取样 {len(picked)} 张（共 {len(files)} 张）风格={args.style}\n")

    tmp = Path(tempfile.mkdtemp(prefix="cogitator_batch_"))
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = tmp / "projects"
    docmod.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings.load()

    rows: list[list[Image.Image]] = []
    t_all = time.time()
    try:
        for i, f in enumerate(picked, 1):
            name = Path(f).name
            print(f"[{i}/{len(picked)}] {name}  {Path(f).stat().st_size/1048576:.1f} MB")
            t0 = time.time()
            proj = Project.create_from_images([f], settings=settings, name=f"batch{i}")
            t_import = time.time() - t0
            lay = proj.state()["layers"][0]
            asset = proj.state()["assets"][0]
            print(f"    导入 {t_import:.1f}s → {lay['src_size'][0]}×{lay['src_size'][1]}"
                  f"（{asset.get('decoded')}，源图 {asset.get('size')}）")

            row = [cell(proj, f"原图 · {name} · {lay['src_size'][0]}×{lay['src_size'][1]}")]

            t0 = time.time()
            proj.apply_ops([{"op": "auto_enhance", "strength": 45, "layer": "L1"}])
            t_auto = time.time() - t0
            row.append(cell(proj, f"自动增强 45%  ({t_auto:.1f}s)"))

            t0 = time.time()
            proj.apply_ops([{"op": "apply_style", "style": args.style, "strength": 70,
                             "layer": "L1"}])
            t_style = time.time() - t0
            st = proj.state()
            row.append(cell(proj, f"+ 风格 {args.style} 70%  ({t_style:.1f}s, "
                                 f"{st['layers'][0]['op_count']} 步)"))

            t0 = time.time()
            out = proj.export("jpg", quality=92, max_side=2400)
            print(f"    渲染 {t_style:.1f}s　导出 2400px {time.time()-t0:.1f}s → {out.name}")
            rows.append(row)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 拼成对比图
    pad, head = 8, 34
    cw, ch = rows[0][0].size
    W = pad + (cw + pad) * 3
    H = head + pad + (ch + pad) * len(rows)
    sheet = Image.new("RGB", (W, H), (16, 18, 22))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 10), f"机魂修图台 · 真实 CR3 批量实测（取样 {len(rows)} 张，"
                      f"{picked[0].split(chr(92))[-1]} 等，全分辨率管线）", fill=(216, 161, 58))
    for r, row in enumerate(rows):
        y = head + pad + r * (ch + pad)
        for c, im in enumerate(row):
            sheet.paste(im, (pad + c * (cw + pad), y))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=90)
    print(f"\n对比图已保存：{out}  {sheet.size}  {out.stat().st_size/1024:.0f} KB")
    print(f"总耗时 {time.time()-t_all:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
