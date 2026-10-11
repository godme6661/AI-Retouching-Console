"""跨文件批处理：把一套配方应用到一批照片。

## 为什么做成无头命令行，而不是网页里的一个按钮

1. **服务是单写者**：它只持有一个"当前工程"。批处理要顺序建/删几百个工程，
   塞进那个进程会把用户正在编辑的工程挤掉，出错也难隔离。
2. **独立进程 = 崩溃不影响你正在用的实例**，也方便挂到计划任务或 CI 里跑。
3. **与工程模型一致**：配方就是纯 JSON 的算子列表，可复用、可审阅、可从任一工程导出。

## 配方从哪来（三选一）

* `--recipe recipe.json`：手写或别处存档的算子列表（也接受 `{"ops": [...]}` 包裹）
* `--from-project <工程目录>`：直接用某个工程第一图层的管线当配方
  （蒙版类步骤会**剥掉已物化的蒙版文件**，让新工程按参数重新生成——蒙版是确定性的）
* `--style <风格id> --strength 80`：套用内置风格配方

## 用法

    python -m cogitator batch --in D:\\照片 --out D:\\成品 --style jp-airy --strength 80
    python -m cogitator batch --in a.jpg b.png --out out --recipe r.json --format jpg --max-side 2400
    python -m cogitator batch --in D:\\照片 --out out --from-project projects/IMG_2009

跑完会在输出目录写 `batch-report.json`（每张的耗时、成败与错误原因），退出码 0/1。
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import styles as styles_mod
from .config import Settings
from .document import ASSET_EXT, Project
from .paths import PROJECTS_DIR, ensure_dirs, safe_name

# 从工程里拿配方时，这些字段属于"上一个工程的产物"，不能带过来
_PER_PROJECT_KEYS = ("mask_file", "mask_size")


def collect_inputs(items: list[str], recursive: bool = True) -> list[Path]:
    """把文件与目录混着给也能用；按扩展名过滤，去重并排序。"""
    out: list[Path] = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            pat = "**/*" if recursive else "*"
            out += [f for f in sorted(p.glob(pat)) if f.is_file()]
        elif p.is_file():
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p.suffix.lower() in ASSET_EXT and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_recipe(args) -> tuple[list[dict], str]:
    """返回 (算子列表, 配方来源说明)。"""
    if args.recipe:
        raw = args.recipe
        p = Path(raw)
        text = p.read_text(encoding="utf-8") if p.is_file() else raw
        data = json.loads(text)
        ops = data.get("ops") if isinstance(data, dict) else data
        if not isinstance(ops, list):
            raise SystemExit("配方格式不对：应为算子列表，或 {\"ops\": [...]}")
        return ops, f"recipe:{p.name if p.is_file() else 'inline'}"

    if args.from_project:
        doc_file = Path(args.from_project) / "document.json"
        if not doc_file.is_file():
            raise SystemExit(f"找不到工程文件：{doc_file}")
        doc = json.loads(doc_file.read_text(encoding="utf-8"))
        layers = doc.get("layers") or []
        if not layers:
            raise SystemExit("该工程没有图层")
        ops = []
        for o in (layers[0].get("ops") or []):
            clean = {k: v for k, v in o.items() if k not in _PER_PROJECT_KEYS}
            ops.append(clean)
        return ops, f"project:{Path(args.from_project).name}（{len(ops)} 步，蒙版将按参数重新生成）"

    if args.style:
        style, ops = styles_mod.expand(args.style, float(args.strength))
        return ops, f"style:{style.id}（{style.name}，{len(ops)} 步，强度 {args.strength}）"

    return [], "无（只做格式转换/尺寸限制）"


def run_one(src: Path, recipe: list[dict], out_dir: Path, *, fmt: str, quality: int,
            max_side: int, settings: Settings, skip_existing: bool,
            keep_projects: bool, finish: dict | None = None) -> dict:
    """处理一张：建工程 → 套配方 → 导出 → 清理临时工程。

    清理用"前缀独占"的做法：给临时工程起一个带随机后缀的名字（batch-<8位>-<原名>），
    这样即使 create_from_images 自己抛异常（此时拿不到 proj 对象），也能按前缀把它删掉——
    否则导入失败的文件会在 projects/ 里留下空壳目录（实测过这个泄漏）。
    """
    t0 = time.time()
    out_path = out_dir / f"{src.stem}.{fmt}"
    rec = {"input": str(src), "output": str(out_path), "ok": False, "seconds": 0.0, "error": ""}
    if skip_existing and out_path.is_file():
        rec.update(ok=True, error="已存在，跳过", seconds=0.0)
        return rec
    tag = f"batch-{uuid.uuid4().hex[:8]}-{safe_name(src.stem)}"
    proj = None
    try:
        proj = Project.create_from_images([str(src)], settings=settings, name=tag)
        if recipe:
            proj.apply_ops(recipe)
        proj.export(fmt, quality, max_side, str(out_path), finish=finish)
        rec["ok"] = out_path.is_file()
        if not rec["ok"]:
            rec["error"] = "导出没有产出文件"
    except Exception as e:                       # 单张失败不该中断整批
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        if not keep_projects:
            _cleanup_projects(tag, proj)
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def _cleanup_projects(tag: str, proj) -> None:
    """删掉本次产生的临时工程：先按对象，再按名字前缀兜底（导入失败时对象是 None）。"""
    try:
        if proj is not None:
            shutil.rmtree(getattr(proj, "dir", None) or "", ignore_errors=True)
    except Exception:
        pass
    try:
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and d.name.startswith(tag[:14]):     # 前缀含随机段，不会误删别人的
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cogitator batch",
                                 description="把一套配方应用到一批照片（无头运行）")
    ap.add_argument("--in", dest="inputs", nargs="+", required=True,
                    help="输入：文件、目录，或两者混用")
    ap.add_argument("--out", required=True, help="输出目录（会被创建）")
    ap.add_argument("--recipe", default="", help="配方 JSON 文件路径，或直接给内联 JSON")
    ap.add_argument("--from-project", default="", help="以某个工程的管线作为配方")
    ap.add_argument("--style", default="", help=f"内置风格 id：{', '.join(styles_mod.style_ids())}")
    ap.add_argument("--strength", type=float, default=100.0, help="风格强度 1~100（配合 --style）")
    ap.add_argument("--format", default="jpg", choices=["jpg", "png", "webp"])
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--max-side", type=int, default=0, help="最长边上限，0=原尺寸")
    ap.add_argument("--skip-existing", action="store_true", help="输出已存在则跳过")
    ap.add_argument("--no-recursive", action="store_true", help="目录只取第一层")
    ap.add_argument("--jobs", type=int, default=1, help="并行数（默认 1，顺序最稳）")
    ap.add_argument("--keep-projects", action="store_true",
                    help="保留临时工程目录（排查用；默认处理完就删）")
    g = ap.add_argument_group("出片参数（水印/边框，只影响成品）")
    g.add_argument("--preset", default="", help="用设置里保存的导出预设（含格式/质量/尺寸/出片参数）")
    g.add_argument("--watermark-text", default="", help="文字水印内容")
    g.add_argument("--watermark-image", default="", help="图片水印路径（与文字二选一）")
    g.add_argument("--watermark-pos", default="br",
                   choices=["tl", "tc", "tr", "center", "bl", "bc", "br"], help="水印位置，默认右下")
    g.add_argument("--watermark-opacity", type=float, default=45.0, help="水印不透明度 0~100")
    g.add_argument("--watermark-scale", type=float, default=12.0,
                   help="图片水印占画面宽度百分比；文字水印忽略此项")
    g.add_argument("--border", default="", choices=["", "white", "black", "polaroid"], help="边框样式")
    g.add_argument("--border-width", type=float, default=4.0, help="边框宽度（占长边百分比）")
    g.add_argument("--border-bottom", type=float, default=14.0, help="拍立得下边宽度（百分比）")
    args = ap.parse_args(argv)

    ensure_dirs()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = collect_inputs(args.inputs, recursive=not args.no_recursive)
    if not inputs:
        print("没有找到可处理的图片（支持 png/jpg/webp/bmp/tif/gif 与 CR3/CR2/NEF/ARW/DNG 等 RAW）")
        return 1
    recipe, src_desc = load_recipe(args)
    settings = Settings.load()

    # 出片参数：先取预设，再用命令行显式参数覆盖
    finish: dict = {}
    if args.preset:
        data = getattr(settings, "data", {}) or {}
        hit = next((x for x in (data.get("export_presets") or [])
                    if str(x.get("name")) == str(args.preset)), None)
        if not hit:
            raise SystemExit(f"没有名为 {args.preset!r} 的导出预设")
        if isinstance(hit.get("finish"), dict):
            finish = dict(hit["finish"])
        if not args.style and not args.recipe and not args.from_project and hit.get("format"):
            args.format = str(hit["format"])
            args.quality = int(hit.get("quality") or args.quality)
            args.max_side = int(hit.get("max_side") or args.max_side)
    wm = {}
    if args.watermark_text:
        wm["text"] = args.watermark_text
    elif args.watermark_image:
        wm["image"] = args.watermark_image
    if wm:
        wm.update(position=args.watermark_pos, opacity=args.watermark_opacity,
                  scale=args.watermark_scale)
        finish["watermark"] = wm
    if args.border:
        finish["border"] = {"style": args.border, "width": args.border_width,
                            "bottom": args.border_bottom}
    if finish:
        from . import finish as finish_mod

        try:
            finish = finish_mod.validate(finish)
        except ValueError as e:
            raise SystemExit(f"出片参数有问题：{e}") from None

    print(f"配方来源：{src_desc}")
    print(f"输入 {len(inputs)} 张 → {out_dir}　格式 {args.format}　质量 {args.quality}　"
          f"最长边 {args.max_side or '原尺寸'}　并行 {args.jobs}")
    if finish:
        from . import finish as finish_mod

        print(f"出片：{finish_mod.describe(finish)}")
    print("-" * 72)

    results: list[dict] = []
    t0 = time.time()
    total = len(inputs)
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futs = [(i, pool.submit(run_one, p, recipe, out_dir, fmt=args.format,
                                    quality=args.quality, max_side=args.max_side,
                                    settings=settings, skip_existing=args.skip_existing,
                                    keep_projects=args.keep_projects, finish=finish))
                    for i, p in enumerate(inputs, 1)]
            for i, f in futs:
                rec = f.result()
                results.append(rec)
                _print_one(i, total, rec)
    else:
        for i, p in enumerate(inputs, 1):
            rec = run_one(p, recipe, out_dir, fmt=args.format, quality=args.quality,
                          max_side=args.max_side, settings=settings, finish=finish,
                          skip_existing=args.skip_existing, keep_projects=args.keep_projects)
            results.append(rec)
            _print_one(i, total, rec)

    took = time.time() - t0
    oks = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "recipe": src_desc,
              "finish": finish,
              "format": args.format, "quality": args.quality, "max_side": args.max_side,
              "total": total, "ok": len(oks), "failed": len(bad),
              "seconds": round(took, 1),
              "avg_seconds": round(sum(r["seconds"] for r in results) / max(1, total), 2),
              "results": results}
    (out_dir / "batch-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    print("-" * 72)
    print(f"完成：成功 {len(oks)} / 失败 {len(bad)}，总耗时 {took:.1f}s"
          f"（平均 {report['avg_seconds']}s/张）")
    if bad:
        print("失败清单：")
        for r in bad[:10]:
            print(f"  ✗ {Path(r['input']).name}：{r['error']}")
    print(f"报告：{out_dir / 'batch-report.json'}")
    return 0 if not bad else 1


def _print_one(i: int, total: int, rec: dict) -> None:
    name = Path(rec["input"]).name
    if rec["ok"]:
        note = f"（{rec['error']}）" if rec["error"] else ""
        print(f"[{i}/{total}] {name} → {Path(rec['output']).name}  {rec['seconds']}s {note}")
    else:
        print(f"[{i}/{total}] {name} ✗ {rec['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
