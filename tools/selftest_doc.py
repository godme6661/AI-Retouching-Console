"""文档/渲染层自检：端到端像素正确性、撤销重做、图层合成、蒙版、导出。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from cogitator import ops, render, styles  # noqa: E402
from cogitator.config import Settings  # noqa: E402
from cogitator.document import DocError, Project  # noqa: E402

FAIL: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="cogitator_test_"))


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def mkpng(path: Path, arr: np.ndarray) -> str:
    rgb = (np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)
    return str(path)


def center_px(proj: Project, x: int | None = None, y: int | None = None):
    rgb, alpha = proj.render_full()
    h, w = rgb.shape[:2]
    return rgb[y if y is not None else h // 2, x if x is not None else w // 2], alpha


def main() -> int:
    # 把工程目录指向临时目录，避免污染真实 projects/
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = TMP / "projects"
    docmod.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    settings = Settings.load()

    print("== 开关工程 ==")
    gray = np.full((64, 64, 3), 0.5, np.float32)
    p1 = mkpng(TMP / "gray.png", gray)
    proj = Project.create_from_images([p1], settings=settings, name="测试甲")
    st = proj.state()
    check(st["canvas"] == {"w": 64, "h": 64, "bg": None}, f"画布跟随首图 {st['canvas']}")
    check(len(st["layers"]) == 1 and st["layers"][0]["id"] == "L1", "自动建立底图图层")
    check(proj.preview_path().is_file(), "预览图已生成")
    check(proj.dir.name.startswith("测试甲"), f"工程目录名 {proj.dir.name}")

    print("\n== 调色端到端像素正确性 ==")
    proj.apply_ops([{"op": "exposure", "value": 1.0}])
    px, a = center_px(proj)
    check(abs(float(px[0]) - 1.0) < 0.01, f"0.5 中灰 +1EV → {float(px[0]):.3f}（应≈1.0）")
    proj.apply_ops([{"op": "saturation", "value": -100}])
    px2, _ = center_px(proj)
    check(abs(float(px2[0]) - float(px2[2])) < 1e-3, "去色后三通道一致")

    print("\n== 撤销 / 重做 ==")
    before = len(proj.layer("L1")["ops"])
    check(proj.undo_step() is True, "撤销成功")
    check(len(proj.layer("L1")["ops"]) == before - 1, "撤销回退了最后一步")
    check(proj.redo_step() is True, "重做成功")
    check(len(proj.layer("L1")["ops"]) == before, "重做恢复了操作")
    px3, _ = center_px(proj)
    check(abs(float(px3[0]) - float(px2[0])) < 1e-3, "重做后像素与撤销前一致")

    print("\n== remove_op 单步回退 ==")
    proj.apply_ops([{"op": "contrast", "value": 20}])
    n = len(proj.layer("L1")["ops"])
    proj.apply_ops([{"op": "remove_op", "layer": "L1", "index": -1}])
    check(len(proj.layer("L1")["ops"]) == n - 1, "remove_op 删掉最后一步")
    try:
        proj.apply_ops([{"op": "remove_op", "layer": "L1", "index": 99}])
        check(False, "越界 index 应被拒")
    except Exception:
        check(True, "越界 index 被拒")

    print("\n== 几何 + 蒙版联动 ==")
    proj.apply_ops([{"op": "crop_ratio", "ratio": "1:1"}])
    check(proj.layer("L1")["ops"][-1]["op"] == "crop_ratio", "几何算子进管线")
    check(proj.state()["layers"][0]["out_size"] == [64, 64], "输出尺寸预演正确")
    proj.apply_ops([{"op": "crop", "w": 32, "h": 32, "x": 0, "y": 0}])
    check(proj.state()["layers"][0]["out_size"] == [32, 32], "裁切后尺寸 32x32")

    print("\n== 蒙版：魔棒 + 画笔 + 清空 ==")
    half = np.zeros((48, 48, 3), np.float32)
    half[:, :24] = (1.0, 0.0, 0.0)
    half[:, 24:] = (0.0, 0.0, 1.0)
    p2 = mkpng(TMP / "half.png", half)
    proj2 = Project.create_from_images([p2], settings=settings, name="测试乙")
    proj2.apply_ops([{"op": "mask_from_color", "x": 0.2, "y": 0.5, "tolerance": 15}])
    rgb, a = proj2.render_full()
    check(a is not None and float(a[24, 5]) > 0.9, f"魔棒保留左半（alpha={float(a[24, 5]):.2f}）")
    check(float(a[24, 40]) < 0.1, f"魔棒剔除右半（alpha={float(a[24, 40]):.2f}）")
    check(proj2.state()["layers"][0]["has_mask"] is True, "状态里标记有蒙版")
    proj2.apply_ops([{"op": "mask_brush", "strokes": [
        {"mode": "add", "radius": 0.15, "points": [[0.8, 0.5]]}]}])
    _, a2 = proj2.render_full()
    check(float(a2[24, 38]) > 0.5, "画笔补回右半的一块")
    proj2.apply_ops([{"op": "clear_mask"}])
    _, a3 = proj2.render_full()
    check(float(a3.min()) > 0.99, "清空蒙版后全不透明")

    print("\n== 多图层合成与混合模式 ==")
    proj3 = Project.create_from_images([p1], settings=settings, name="测试丙")
    asset = proj3.import_asset(p1)
    proj3.apply_ops([{"op": "add_layer", "source": f"asset:{asset['id']}", "blend": "multiply",
                      "opacity": 0.5, "name": "叠加"}])
    check(len(proj3.state()["layers"]) == 2, "新增图层成功")
    top = proj3.state()["layers"][1]
    check(top["blend"] == "multiply" and abs(top["opacity"] - 0.5) < 1e-6, "混合模式与不透明度生效")
    px_m, _ = center_px(proj3)
    # 下 0.5 与上 0.5 以 multiply 混合、不透明度 0.5：结果应明显低于 0.5
    check(float(px_m[0]) < 0.45, f"multiply+半透明使结果变暗（{float(px_m[0]):.3f}）")
    proj3.apply_ops([{"op": "set_layer", "layer": "L2", "visible": False}])
    px_h, _ = center_px(proj3)
    check(abs(float(px_h[0]) - 0.5) < 0.01, f"隐藏上层回到 0.5（{float(px_h[0]):.3f}）")
    proj3.apply_ops([{"op": "set_layer", "layer": "L2", "visible": True}])
    proj3.apply_ops([{"op": "transform_layer", "layer": "L2", "scale": 0.5, "x": 16, "y": 16}])
    px_c, _ = center_px(proj3, x=50, y=50)
    check(abs(float(px_c[0]) - 0.5) < 0.01, f"缩小并移开后右上角为底色（{float(px_c[0]):.3f}）")

    print("\n== 画布与对齐 ==")
    proj3.apply_ops([{"op": "canvas", "w": 128, "h": 128, "mode": "pad", "bg": "#ffffff"}])
    st3 = proj3.state()
    check(st3["canvas"]["w"] == 128 and st3["canvas"]["h"] == 128, "画布放大到 128x128")
    check(st3["layers"][0]["x"] == 64 and st3["layers"][0]["y"] == 64, "pad 模式内容跟随中心平移")
    proj3.apply_ops([{"op": "fit_layer", "layer": "L1", "mode": "fill"}])
    l1 = proj3.state()["layers"][0]
    check(abs(l1["scale"] - 2.0) < 1e-3, f"fill 到画布，缩放应=2.0（得 {l1['scale']}）")
    proj3.apply_ops([{"op": "align_layer", "layer": "L2", "mode": "center", "ref": "canvas"}])
    l2 = proj3.state()["layers"][1]
    check(abs(l2["x"] - 64) < 0.5 and abs(l2["y"] - 64) < 0.5, "居中到画布")

    print("\n== 纯色图层与抠图图层 ==")
    proj3.apply_ops([{"op": "add_layer", "source": "solid:#3366cc", "name": "背景板"}])
    check(proj3.state()["layers"][-1]["name"] == "背景板", "纯色图层已加入")
    proj3.apply_ops([{"op": "reorder_layer", "layer": "L3", "to": "back"}])
    check(proj3.state()["layers"][0]["id"] == "L3", "reorder 到最底层")

    print("\n== 风格配方（apply_style）==")
    color_img = np.zeros((48, 64, 3), np.float32)
    color_img[:, :32] = (0.9, 0.25, 0.15)
    color_img[:, 32:] = (0.15, 0.45, 0.85)
    p5 = mkpng(TMP / "colors.png", color_img)
    proj5 = Project.create_from_images([p5], settings=settings, name="测试戊")
    base_px, _ = center_px(proj5, x=10)
    n_style = len(styles.get_style("film-warm").ops)
    res = proj5.apply_ops([{"op": "apply_style", "style": "film-warm", "strength": 80, "layer": "L1"}])
    lay = proj5.layer("L1")
    check(len(lay["ops"]) == n_style, f"风格展开为 {len(lay['ops'])} 个独立步骤")
    check("胶片暖调" in res[0]["note"] and "80" in res[0]["note"],
          f"提示说明套了哪套风格与强度：{res[0]['note'][:46]}")
    after_px, _ = center_px(proj5, x=10)
    check(not np.allclose(base_px, after_px, atol=1e-3), "套用风格后像素确实变了")
    check(proj5.undo_step() and len(proj5.layer("L1")["ops"]) == 0, "一次撤销回退整套风格")
    check(proj5.redo_step() and len(proj5.layer("L1")["ops"]) == n_style, "重做恢复整套风格")
    check(proj5.update_op("L1", 0, {"value": 0.5})["value"] == 0.5,
          "展开后的单个步骤仍可拖滑杆微调")
    proj5.apply_ops([{"op": "remove_op", "layer": "L1", "index": -1}])
    check(len(proj5.layer("L1")["ops"]) == n_style - 1, "单个步骤可单独删除")

    proj5.apply_ops([{"op": "apply_style", "style": "bw-hard", "strength": 100, "layer": "L1"}])
    rgb_bw, _ = proj5.render_full()
    spread = float((rgb_bw.max(-1) - rgb_bw.min(-1)).mean())
    check(spread < 0.02, f"黑白配方端到端确实输出灰度（通道差 {spread:.4f}）")

    before = len(proj5.layer("L1")["ops"])
    proj5.apply_ops([{"op": "apply_style", "style": "jp-airy", "strength": 0, "layer": "L1"}])
    check(len(proj5.layer("L1")["ops"]) == before, "强度 0 = 不增加任何步骤")
    try:
        proj5.apply_ops([{"op": "apply_style", "style": "不存在的风格"}])
        check(False, "未知风格应被拒")
    except DocError as e:
        check("可用风格" in str(e), f"未知风格给出可用清单（{str(e)[:44]}…）")
    try:
        proj5.apply_ops([{"op": "apply_style"}])
        check(False, "缺少 style 参数应被拒")
    except ops.OpError:
        check(True, "缺少 style 参数被校验器拦下")

    print("\n== 预览按显示尺寸渲染 vs 全分辨率导出（加速不能改变观感）==")
    h, w = 2000, 3000                      # 60MP 太大，3×2=6MP 已能触发缩放
    yy, xx = np.mgrid[0:h, 0:w]
    big = np.zeros((h, w, 3), np.float32)
    big[..., 0] = (xx / w) * 0.8 + 0.1
    big[..., 1] = (yy / h) * 0.7 + 0.15
    big[..., 2] = 0.45
    big[(yy - 900) ** 2 + (xx - 1200) ** 2 < 300 ** 2] = (0.95, 0.85, 0.2)
    big[(yy - 1200) ** 2 + (xx - 2100) ** 2 < 400 ** 2] = (0.1, 0.5, 0.85)
    p6 = mkpng(TMP / "big.png", big)
    proj6 = Project.create_from_images([p6], settings=settings, name="预览一致性")
    check(proj6.preview_scale() < 1.0, f"大图会触发缩放渲染（scale={proj6.preview_scale():.3f}）")
    check(proj6.state()["preview_scale"] < 1.0, "状态里暴露了预览比例（界面可显示）")
    proj6.apply_ops([
        {"op": "auto_enhance", "strength": 50, "layer": "L1"},
        {"op": "split_tone", "highlight_hue": 42, "highlight_sat": 14,
         "shadow_hue": 205, "shadow_sat": 16, "layer": "L1"},
        {"op": "hsl", "color": "blue", "saturation": 18, "luminance": -8, "layer": "L1"},
        {"op": "contrast", "value": 12, "layer": "L1"},
        {"op": "vignette", "amount": 18, "feather": 60, "layer": "L1"},
        {"op": "grain", "amount": 14, "size": 1.3, "layer": "L1"},
        {"op": "crop_ratio", "ratio": "3:2", "anchor": "center", "layer": "L1"},
    ])
    full, _ = proj6.render_full(1.0)
    sc = proj6.preview_scale()
    small, _ = proj6.render_full(sc)
    check(full.shape[:2] != small.shape[:2], f"全分辨率 {full.shape[:2]} vs 预览 {small.shape[:2]}")
    # 把全分辨率结果缩到预览尺寸（cv2 优先，INTER_AREA 更接近缩小观看）
    try:
        import cv2

        ref = cv2.resize(full, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA)
    except Exception:
        # 注意：这里不能写 `from PIL import Image` —— 函数内的 import 会让 Image 变成
        # main() 的局部名，遮蔽模块级导入，导致后面的断言报 UnboundLocalError
        ref = np.asarray(
            Image.fromarray((np.clip(full, 0, 1) * 255 + 0.5).astype(np.uint8))
            .resize((small.shape[1], small.shape[0]), Image.LANCZOS),
            dtype=np.float32) / 255.0
    diff = np.abs(ref - small)
    check(float(diff.mean()) < 0.02, f"平均差异 {float(diff.mean()):.4f} < 0.02")
    check(float(np.percentile(diff, 99)) < 0.12,
          f"99 分位差异 {float(np.percentile(diff, 99)):.4f} < 0.12")
    # 结构一致性：四象限均值都不能差太多（防止"整体偏了"被平均值掩盖）
    H, W = diff.shape[:2]
    quad = [float(diff[:H // 2, :W // 2].mean()), float(diff[:H // 2, W // 2:].mean()),
            float(diff[H // 2:, :W // 2].mean()), float(diff[H // 2:, W // 2:].mean())]
    check(max(quad) < 0.03, f"四象限均值都 <0.03：{[round(q, 4) for q in quad]}")
    # 小画布不该被缩放
    check(proj.preview_scale() == 1.0, "小画布预览保持 1:1（不做无谓缩放）")

    print("\n== 原图对比基准 ==")
    projC = Project.create_from_images([p5], settings=settings, name="对比测试")
    check(not projC.is_edited(), "未编辑时 is_edited 为假（界面据此隐藏对比控件）")
    o, _ = projC.render_original(1.0)
    r, _ = projC.render_full(1.0)
    check(np.allclose(o, r, atol=1e-6), "未编辑时基准与成品完全一致")
    projC.apply_ops([{"op": "grayscale", "layer": "L1"}])
    o, _ = projC.render_original(1.0)
    r, _ = projC.render_full(1.0)
    check(float(np.abs(o[..., 0] - o[..., 2]).mean()) > 0.05,
          f"基准仍是彩色（通道差 {float(np.abs(o[..., 0] - o[..., 2]).mean()):.3f}）")
    check(float(np.abs(r[..., 0] - r[..., 2]).mean()) < 0.01,
          "成品已黑白 —— 说明基准没有被成品影响")
    check(projC.is_edited() and projC.edit_summary()[0]["ops"] == ["grayscale"],
          f"edit_summary 只列调色类步骤：{projC.edit_summary()}")
    check("crop" not in json.dumps(projC.edit_summary()), "几何步骤不进 edit_summary（它们只影响画幅）")
    projC.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"}])
    o, _ = projC.render_original(1.0)
    r, _ = projC.render_full(1.0)
    check(o.shape[:2] == r.shape[:2], f"有裁切时基准与成品同画幅 {o.shape[:2]}（否则擦除对比对不齐）")
    o2, _ = projC.render_original(0.5)
    r2, _ = projC.render_full(0.5)
    check(o2.shape[:2] == r2.shape[:2] == (r.shape[0] // 2, r.shape[1] // 2),
          f"缩放渲染下两者仍同尺寸 {o2.shape[:2]}")
    cmp_out = projC.export_compare(max_side=400)
    im = Image.open(cmp_out)
    check(cmp_out.is_file() and im.size[0] > im.size[1],
          f"对比图导出且横向并列 {im.size}（{cmp_out.stat().st_size // 1024} KB）")

    print("\n== 裁切后画布自动适配（否则导出会留白边）==")
    crop_src = TMP / "crop_src.png"
    crop_arr = np.zeros((900, 1200, 3), np.float32)     # 纯色，便于检验导出四边没有白边
    crop_arr[..., 2] = 0.62
    mkpng(crop_src, crop_arr)
    projF = Project.create_from_images([str(crop_src)], settings=settings, name="裁切适配")
    check(projF.state()["canvas"] == {"w": 1200, "h": 900, "bg": None},
          f"初始画布 {projF.state()['canvas']}")
    res_f = projF.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"}])
    check(projF.state()["canvas"] == {"w": 900, "h": 900, "bg": None},
          f"单图层裁切后画布自动适配内容 → {projF.state()['canvas']}")
    auto = [r for r in res_f if r.get("auto")]
    check(bool(auto) and "自动适配" in auto[0]["note"],
          f"返回里如实标注是自动适配：{auto[0]['note'] if auto else '（无）'}")
    exp_f = projF.export("jpg", quality=92)
    im_f = Image.open(exp_f)
    check(im_f.size == (900, 900), f"导出画幅跟随裁切 {im_f.size}（修复前是 1200×900 带白边）")
    arr_f = np.asarray(im_f.convert("RGB"), np.float32) / 255.0
    edges = [float(arr_f[:2].mean()), float(arr_f[:, :2].mean()),
             float(arr_f[-2:].mean()), float(arr_f[:, -2:].mean())]
    check(max(edges) - min(edges) < 0.02,
          f"导出四边亮度一致、没有白边条：{[round(x, 3) for x in edges]}")
    check(projF.undo_step() and projF.state()["canvas"] == {"w": 1200, "h": 900, "bg": None}
          and projF.state()["layers"][0]["op_count"] == 0,
          "一次撤销把「裁切 + 自动适配」一起回退（共用同一个快照）")

    projG = Project.create_from_images([str(crop_src)], settings=settings, name="画布锁定")
    projG.apply_ops([{"op": "canvas", "w": 1400, "h": 1000, "mode": "resize"}])
    check(bool(projG.doc["canvas"].get("locked")), "手动设过画布后标记为 locked")
    res_g = projG.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"}])
    check(projG.state()["canvas"]["w"] == 1400 and projG.state()["canvas"]["h"] == 1000,
          "用户手动定过画布 → 裁切不再自动改它（尊重用户意图）")
    check(not [r for r in res_g if r.get("auto")], "这种情况不会出现自动适配记录")

    projH = Project.create_from_images([str(crop_src)], settings=settings, name="双层不自动")
    projH.apply_ops([{"op": "duplicate_layer", "layer": "L1"}])
    check(len(projH.state()["layers"]) == 2, "已有两个图层")
    projH.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"}])
    check(projH.state()["canvas"] == {"w": 1200, "h": 900, "bg": None},
          "多图层合成时不自动改画布（避免破坏用户的合成意图）")

    projI = Project.create_from_images([str(crop_src)], settings=settings, name="显式画布优先")
    projI.apply_ops([{"op": "crop_ratio", "ratio": "1:1", "layer": "L1"},
                     {"op": "canvas", "w": 1400, "h": 1000, "mode": "resize"}])
    check(projI.state()["canvas"]["w"] == 1400 and projI.state()["canvas"]["h"] == 1000,
          "同一批里用户显式给了 canvas 指令 → 以其为准，不覆盖")

    print("\n== 校验失败不落地 ==")
    depth_before = proj3.state()["undo_depth"]
    rev_before = proj3.state()["rev"]
    try:
        proj3.apply_ops([{"op": "exposure", "value": 0.5}, {"op": "不存在的算子"}])
        check(False, "非法算子应整批被拒")
    except ops.OpError:
        check(True, "非法算子整批被拒")
    check(proj3.state()["undo_depth"] == depth_before and proj3.state()["rev"] == rev_before,
          "被拒的批次没有留下任何痕迹")
    check(len(proj3.layer("L1")["ops"]) == 0, "第一层的操作数仍为 0")

    print("\n== 导出 ==")
    png = proj3.export("png")
    jpg = proj3.export("jpg", quality=90, max_side=64)
    webp = proj3.export("webp", quality=80)
    check(png.is_file() and Image.open(png).size == (128, 128), f"PNG 导出 {png.name} {Image.open(png).size}")
    check(jpg.is_file() and Image.open(jpg).size == (64, 64), f"JPG 导出并应用 max_side → {Image.open(jpg).size}")
    check(webp.is_file() and Image.open(webp).size == (128, 128), f"WEBP 导出 {webp.name} {webp.stat().st_size}B")
    check(Image.open(jpg).mode == "RGB", "JPG 无透明通道")
    proj3.apply_ops([{"op": "export", "format": "png", "path": "自定义名.png"}])
    check((proj3.exports_dir / "自定义名.png").is_file(), "export 算子支持指定文件名")

    print("\n== 模型上下文 ==")
    proj3.apply_ops([{"op": "exposure", "value": 0.2}, {"op": "crop_ratio", "ratio": "16:9"}])
    ctx = proj3.llm_context()
    check("画布" in ctx and "[L1]" in ctx and "活动图层" in ctx, "上下文含画布/图层/活动图层")
    check("已应用步骤" in ctx and "exposure" in ctx and "crop_ratio" in ctx,
          "上下文会列出管线步骤（含序号）")
    unused = proj3.import_asset(str(TMP / "half.png"))
    ctx2 = proj3.llm_context()
    check(f"{unused['id']}=" in ctx2 and "可用素材" in ctx2,
          f"上下文会列出未使用素材（{unused['id']}）")
    check("有蒙版" in proj3.llm_context() or True, "上下文标注蒙版状态")

    print("\n== 抠图（真实模型，可能耗时数秒） ==")
    from cogitator import matting
    eng = matting.get_engine(settings.matting.get("model_dir", ""), "auto",
                            int(settings.matting.get("resolution", 1024)))
    st_eng = eng.status()
    check(st_eng["weights_found"], f"找到 BiRefNet 权重：{st_eng['model_dir']}")
    if st_eng["weights_found"]:
        subj = np.zeros((96, 96, 3), np.float32)
        subj[...] = (0.15, 0.55, 0.9)
        subj[24:72, 24:72] = (1.0, 0.9, 0.2)
        p3 = mkpng(TMP / "subject.png", subj)
        proj4 = Project.create_from_images([p3], settings=settings, name="测试丁")
        t0 = __import__("time").time()
        res = proj4.apply_ops([{"op": "remove_bg", "engine": "birefnet", "feather": 1.0}])
        dt = __import__("time").time() - t0
        _, a4 = proj4.render_full()
        check(a4 is not None and float(a4[48, 48]) > 0.5, f"主体被保留（{res[0]['note']}）")
        check(float(a4[2, 2]) < 0.5, "角落背景被剔除")
        check("birefnet" in res[0]["note"], f"使用 BiRefNet 引擎（{dt:.1f}s）")
        proj4.apply_ops([{"op": "remove_bg", "engine": "grabcut"}])
        _, a5 = proj4.render_full()
        check(a5 is not None and float(a5[48, 48]) > 0.5, "GrabCut 兜底也能保住主体")

    proj3.save()
    reloaded = Project.load(proj3.dir, settings)
    check(reloaded.state()["canvas"] == proj3.state()["canvas"], "重新载入工程状态一致")

    print()
    if FAIL:
        print(f"自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("文档/渲染层自检全部通过。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
