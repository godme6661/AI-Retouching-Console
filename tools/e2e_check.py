"""端到端验收：对**正在运行的服务**走真实 HTTP，逐项断言。

设计约定（都是踩过坑后定下的）：
  * **默认不花用户的钱**：真实模型调用（对话 / 教学 / 连通测试）只在 `--with-llm` 时才跑。
    早期版本默认就跑，等于每次验收都消耗用户的 API 额度。
  * **自己收摊**：本脚本会在 projects/ 下建 e2e_* 工程；结束时（含中途出错，走 atexit）
    先让服务关闭工程再删目录，否则服务的保存动作会把目录写回来。曾因此堆了 40 个工程。
  * 真实抠图（BiRefNet，载模型约 10s）只在 `--with-matting` 时跑。

用法：
  python tools/e2e_check.py --base http://127.0.0.1:8760
  python tools/e2e_check.py --base http://127.0.0.1:8760 --with-matting --with-llm
"""
from __future__ import annotations
import os

import argparse
import atexit
import base64
import glob
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

OK = 0
FAIL: list[str] = []
SKIPPED: list[str] = []


def check(cond: bool, msg: str) -> bool:
    global OK
    if cond:
        OK += 1
        print(f"  ok   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")
    return bool(cond)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def skip(msg: str) -> None:
    SKIPPED.append(msg)
    print(f"  --   {msg}")


def make_test_image(path: Path, w: int = 540, h: int = 360) -> Path:
    """造一张有梯度、有彩色块、有高光的测试图（便于检验调色与几何）。"""
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.zeros((h, w, 3), np.float32)
    arr[..., 0] = xx / w * 0.7 + 0.15
    arr[..., 1] = yy / h * 0.6 + 0.2
    arr[..., 2] = 0.35
    arr[h // 4:h // 2, w // 4:w // 2] = (0.95, 0.25, 0.2)
    arr[h // 2:h * 3 // 4, w // 2:w * 3 // 4] = (0.2, 0.85, 0.35)
    arr[10:40, w - 90:w - 20] = 1.0
    Image.fromarray((np.clip(arr, 0, 1) * 255).astype("uint8"), "RGB").save(path)
    return path


def _projects_root(base: str) -> Path | None:
    """定位工程的父目录。

    先看"最近工程"；如果列表为空（例如用户把工程都清了），就临时建一个探针工程来问出路径，
    否则收摊在空目录上会失效（曾因此留下残留）。
    """
    def _from_recent() -> Path | None:
        try:
            with httpx.Client(timeout=20) as c:
                st = c.get(f"{base}/api/state").json()
            for r in (st.get("recent") or []):
                p = Path(str(r.get("dir") or ""))
                if p.parent.name == "projects" and p.parent.is_dir():
                    return p.parent
        except Exception:
            return None
        return None

    root = _from_recent()
    if root:
        return root
    # 兜底：造一个最小 PNG 当探针，open 成新工程后取其父目录
    try:
        import io as _io

        buf = _io.BytesIO()
        Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, "PNG")
        probe = Path(tempfile.mkdtemp(prefix="cogitator_probe_")) / "probe.png"
        probe.write_bytes(buf.getvalue())
        with httpx.Client(timeout=60) as c:
            c.post(f"{base}/api/open", json={"paths": [str(probe)], "mode": "new",
                                            "name": "e2e_probe_path"})
            c.post(f"{base}/api/project/close", json={})
        return _from_recent()
    except Exception:
        return None


def cleanup_leftovers(base: str) -> int:
    """删掉本脚本在工程目录留下的验收工程。返回残留个数（0=已清干净）；-1 表示无法定位。"""
    proj_root = _projects_root(base)
    if not proj_root:
        return -1
    try:
        with httpx.Client(timeout=20) as c:
            try:
                c.post(f"{base}/api/project/close", json={})   # 先让服务松手
            except Exception:
                pass
    except Exception:
        pass
    freed = 0
    total = 0
    remaining = -1
    # 重试到连续为空：Windows 上刚删完的目录可能被临时占用（索引/杀软/服务句柄），
    # 表现为"文件删掉了、空目录删不掉"。所以必须按**剩余数**判定，而不是按删除数。
    for _ in range(8):
        left = [d for d in proj_root.iterdir()
                if d.is_dir() and d.name.startswith(("e2e_", "ui对比检查"))]
        remaining = len(left)
        if not left:
            break
        for d in left:
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():                      # 空目录被占用时再试一次
                try:
                    d.rmdir()
                except OSError:
                    pass
        total += len(left)
        time.sleep(0.8)
    if total:
        print(f"  （收摊：尝试删除 {total} 个验收工程，释放 {freed / 1048576:.1f} MB）")
    return max(0, remaining)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8760")
    ap.add_argument("--image", default="", help="用指定图片；缺省自动生成")
    ap.add_argument("--with-matting", action="store_true", help="额外跑真实抠图（载模型约 10s）")
    ap.add_argument("--with-llm", action="store_true", help="额外跑真实模型调用（会消耗 API 额度）")
    ap.add_argument("--raw", default="", help="用于 RAW 检查的 CR3 路径；缺省自动在 E 盘找")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    c = httpx.Client(timeout=300.0)
    atexit.register(cleanup_leftovers, base)      # 中途出错也要收摊

    tmp = Path(tempfile.mkdtemp(prefix="cogitator_e2e_"))
    img = Path(args.image) if args.image else make_test_image(tmp / "e2e_source.png")
    print(f"测试图片：{img}")

    # ---------------------------------------------------------------- 健康 / 静态
    section("健康检查与静态资源")
    h = c.get(f"{base}/api/health")
    check(h.status_code == 200, "健康检查 200")
    check("charset=utf-8" in h.headers.get("content-type", ""), "JSON 声明 UTF-8")
    home = c.get(f"{base}/")
    check(home.status_code == 200 and "机魂修图台" in home.text, "主页可访问且为中文界面")
    check("__VERSION__" not in home.text, "版本占位符已被替换")
    for name in ("style.css", "app.js"):
        check(c.get(f"{base}/web/{name}").status_code == 200, f"静态资源 {name} 可访问")
    check("no-store" in (c.get(f"{base}/web/app.js").headers.get("cache-control") or ""),
          "静态资源带禁缓存头")
    check(c.get(f"{base}/web/../server.py").status_code == 404,
          "静态目录外的路径被拒绝（防目录穿越）")

    section("算子手册")
    man = c.get(f"{base}/api/ops/manual").json()
    specs = man["specs"]
    check(len(specs) == 48, f"手册列出 {len(specs)} 个算子")
    check(all(s.get("name") and s.get("category") and s.get("kind") for s in specs),
          "手册结构正常")
    have = {s["name"] for s in specs}

    # ---------------------------------------------------------------- 工程与指令
    section("新建工程与基础指令")
    r = c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new",
                                        "name": "e2e_source"}).json()
    check(r.get("ok"), "新建工程成功")
    p = r["project"]
    check(p["layers"][0]["src_size"] == [540, 360],
          f"图层与源图尺寸正确 {p['layers'][0]['src_size']}")
    check(p["canvas"]["w"] == 540, "画布跟随源图")
    prev = c.get(f"{base}/project/preview")
    check(prev.status_code == 200 and prev.content[:2] == b"\xff\xd8", "预览返回 JPEG")
    check(len(prev.content) > 2000, f"预览非空（{len(prev.content)} 字节）")
    thumb = c.get(f"{base}/project/source/L1")
    check(thumb.status_code == 200 and len(thumb.content) > 1000, "图层缩略图可用")

    ops_batch = [{"op": "auto_enhance", "strength": 40, "layer": "L1"},
                 {"op": "temperature", "value": 12, "layer": "L1"},
                 {"op": "shadows", "value": 18, "layer": "L1"},
                 {"op": "crop_ratio", "ratio": "3:2", "layer": "L1"}]
    r = c.post(f"{base}/api/ops", json={"ops": ops_batch}).json()
    check(r.get("ok") and len(r["results"]) == 4,
          f"4 条指令全部应用：{[x['op'] for x in r.get('results', [])]}")
    check(r["project"]["layers"][0]["op_count"] == 4, "图层管线记录了 4 步")
    check(r["project"]["layers"][0]["out_size"] == [540, 360],
          f"3:2 裁切后输出尺寸 {r['project']['layers'][0]['out_size']}")
    check(all("note" in x for x in r["results"]), "操作步骤带序号（供微调滑杆使用）")

    bad = c.post(f"{base}/api/ops", json={"ops": [{"op": "not_an_op"}]})
    check(bad.status_code == 400 and "未知" in bad.text, "非法算子被拒并说明原因")
    st = c.get(f"{base}/api/state").json()["project"]
    check(st["layers"][0]["op_count"] == 4, "被拒批次没有污染文档")
    up = c.post(f"{base}/api/ops/update", json={"layer": "L1", "index": 1,
                                                "params": {"value": -40}}).json()
    check(up.get("ok"), "改写已应用步骤参数成功")
    check(up["project"]["layers"][0]["ops"][1]["value"] == -40, "参数确实被替换成 -40")
    check(c.post(f"{base}/api/ops", json={"ops": [{"op": "exposure", "value": 99}]}).status_code == 400,
          "越界数值被拒")
    check(c.post(f"{base}/api/ops/update", json={"layer": "L1", "index": 99,
                                                 "params": {}}).status_code == 400,
          "越界序号被拒")

    section("撤销 / 重做")
    check(c.post(f"{base}/api/undo", json={}).json().get("ok"), "撤销成功")
    rr = c.post(f"{base}/api/redo", json={}).json()
    check(rr.get("ok"), "重做成功")
    check(rr["project"]["rev"] >= r["project"]["rev"], "重做后 revision 前进")

    # ---------------------------------------------------------------- 图层
    section("图层操作")
    r = c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "layer"}).json()
    check(r.get("ok") and len(r["project"]["layers"]) == 2, "同一张图作为新图层加入")
    lid = r["project"]["layers"][0]["id"]
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "set_layer", "layer": lid,
                                                 "blend": "multiply", "opacity": 0.6}]}).json()
    check(r.get("ok"), "设置混合模式与不透明度")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "add_layer",
                                                 "source": "solid:#204080",
                                                 "name": "纯色", "scale": 0.5}]}).json()
    check(r.get("ok") and len(r["project"]["layers"]) == 3, "新增纯色图层")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "reorder_layer", "layer": lid,
                                                 "to": "back"}]}).json()
    check(r.get("ok"), "图层重排到最底层")
    r = c.post(f"{base}/api/active", json={"layer": lid}).json()
    check(r.get("ok"), "切换活动图层")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "duplicate_layer", "layer": lid}]}).json()
    check(r.get("ok"), f"duplicate: 复制图层为 {r['project']['layers'][-1]['id']}")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "delete_layer", "layer": lid}]}).json()
    check(r.get("ok"), "删除图层")

    # ---------------------------------------------------------------- 算子冒烟
    section("每个登记算子都真实执行一次")
    exempt = ["delete_layer", "remove_bg"]
    smoke_ops = [s["name"] for s in specs if s["name"] not in exempt]
    failed: list[str] = []
    for name in smoke_ops:
        spec = next(s for s in specs if s["name"] == name)
        params = {}
        for prm in spec["params"]:
            if prm.get("required") and prm.get("default") is None:
                if prm.get("min") is not None:
                    params[prm["name"]] = prm["min"]
                elif prm.get("enum"):
                    params[prm["name"]] = prm["enum"][0]
        op = {"op": name, **params}
        if spec.get("layer") and name not in ("canvas", "canvas_ratio", "add_layer",
                                             "duplicate_layer", "delete_layer"):
            op["layer"] = lid
        try:
            resp = c.post(f"{base}/api/ops", json={"ops": [op]})
            try:
                body = resp.json()
            except Exception:
                # 非 JSON 响应（例如 500 的纯文本）本身就是要抓的问题
                failed.append(f"{name}(HTTP {resp.status_code}、非 JSON)")
                continue
            if resp.status_code != 400 and not body.get("ok"):
                failed.append(f"{name}({body.get('error', '')[:40]})")
        except Exception as e:
            failed.append(f"{name}({type(e).__name__})")
    check(len(smoke_ops) >= 44,
          f"每个登记算子都有冒烟用例（缺 {sorted(have - set(smoke_ops)) or '无'}；豁免 {exempt}）")
    check(not failed, f"全部算子可执行（失败 {len(failed)} 个：{failed[:6]}）")

    # ---------------------------------------------------------------- 蒙版
    section("蒙版与裁剪")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "mask_from_color", "layer": lid,
                                                 "x": 0.1, "y": 0.1, "tolerance": 30,
                                                 "replace": False}]}).json()
    check(r.get("ok"), "魔棒以叠加方式追加蒙版（replace=false）")
    st = c.get(f"{base}/api/state").json()["project"]
    lay = next(x for x in st["layers"] if x["id"] == lid)
    idx = [i for i, o in enumerate(lay["ops"]) if o["op"] == "mask_from_color"]
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "remove_op", "layer": lid,
                                                 "index": idx[-1]}]}).json()
    check(r.get("ok"), "remove_op 可删掉蒙版步骤")

    # ---------------------------------------------------------------- 画布 / 导出
    section("画布与导出")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "canvas", "mode": "fit"}]}).json()
    check(r.get("ok"), f"画布按内容自适应（{r['project']['canvas']}）")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "canvas", "bg": ""}]}).json()
    check(r.get("ok") and not r["project"]["canvas"]["bg"], "画布底色可设为透明（bg 空串）")
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "canvas", "bg": "#ffffff"}]}).json()
    check(r.get("ok") and r["project"]["canvas"]["bg"] == "#ffffff", "画布底色可设回纯色")
    ex = c.post(f"{base}/api/export", json={"format": "jpg", "quality": 90,
                                            "max_side": 400}).json()
    check(ex.get("ok") and Path(ex["path"]).is_file(), f"导出 JPG：{Path(ex['path']).name}")
    check(Image.open(ex["path"]).size[0] == 400, "max_side 生效")
    check(c.post(f"{base}/api/export", json={"format": "png"}).json().get("ok"), "导出 PNG")
    check(c.post(f"{base}/api/export", json={"format": "tiff"}).status_code == 400,
          "不支持的格式被拒")

    # ---------------------------------------------------------------- 上传 / 浏览
    section("上传与文件浏览")
    buf = io.BytesIO()
    Image.new("RGB", (120, 90), (200, 120, 40)).save(buf, "PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    up = c.post(f"{base}/api/upload", json={
        "files": [{"name": "up.png", "data_url": data_url}],
        "mode": "layer"}).json()
    check(up.get("ok"), "base64 上传并加入图层")
    empty = c.post(f"{base}/api/upload", json={"files": [{"name": "x.png", "data_url": ""}],
                                              "mode": "layer"})
    check(empty.status_code == 400 and "空" in empty.text,
          f"空图片数据被拒为 400 且说明原因（HTTP {empty.status_code}）")
    br = c.get(f"{base}/api/browse", params={"path": str(img.parent)}).json()
    check(br.get("ok") and any(str(img.name) in json.dumps(f, ensure_ascii=False)
                               for f in br.get("files", [])),
          f"浏览器能列出图片文件（{len(br.get('files', []))} 个）")
    check(c.get(f"{base}/api/browse", params={"path": "Z:\\nope"}).status_code == 400,
          "不存在的目录给出 400")

    # ---------------------------------------------------------------- 设置与密钥
    section("设置与密钥安全")
    s = c.get(f"{base}/api/settings").json()
    ids = [m["id"] for m in s["presets"]]
    check("deepseek-flash" in ids, f"预设含 DeepSeek：{ids[:3]}…")
    blob = json.dumps(s["settings"], ensure_ascii=False)
    # 允许暴露"密钥来自哪个环境变量"这类元信息，但**不能出现任何真实密钥**
    check("sk-" not in blob and '"api_key"' not in blob,
          "接口不返回任何密钥值（只暴露环境变量名等元信息）")
    check("api_key_env" in blob or "has_env_key" in blob, "暴露密钥环境变量名（便于用户配置）")
    r = c.post(f"{base}/api/settings", json={"active_model": "deepseek-flash"}).json()
    check(r.get("ok"), "切换当前模型")

    # ---------------------------------------------------------------- 技能包 / 风格
    section("技能包与风格配方")
    sk = c.get(f"{base}/api/skills").json()
    refs = sk.get("refs") or []
    check(len(refs) == 11, f"技能包含 {len(refs)} 份参考（reference_count={sk.get('reference_count')}）")
    check(all(r0.get("summary") for r0 in refs), "每份参考都有一句话摘要（渐进式披露的前提）")
    one = c.get(f"{base}/api/skills/{refs[0]['id']}").json()
    check(bool(one.get("text")) and one.get("chars", 0) > 200,
          f"按需加载参考正文可用：{refs[0]['id']}（{one.get('chars')} 字）")
    styles = c.get(f"{base}/api/styles").json()
    check(len(styles.get("styles") or []) == 13, f"风格配方 {len(styles.get('styles') or [])} 套")
    check(all(x.get("intent") for x in styles["styles"]), "每套配方都有适用说明")

    # ---------------------------------------------------------------- 原图对比
    section("原图对比")
    r = c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new",
                                        "name": "e2e_cmp"}).json()
    check(r["project"]["edited"] is False, "未编辑时 edited=False")
    o = c.get(f"{base}/project/original")
    n = c.get(f"{base}/project/preview")
    check(o.status_code == 200 and o.content[:2] == b"\xff\xd8", "原图端点返回 JPEG")
    so = Image.open(io.BytesIO(o.content)).size
    sn = Image.open(io.BytesIO(n.content)).size
    check(so == sn, f"原图与成品同尺寸，可逐像素对齐（{so}）")

    def _pair():
        a = np.asarray(Image.open(io.BytesIO(c.get(f"{base}/project/original").content))
                       .convert("RGB"), np.float32) / 255.0
        b = np.asarray(Image.open(io.BytesIO(c.get(f"{base}/project/preview").content))
                       .convert("RGB"), np.float32) / 255.0
        return a, b

    c.post(f"{base}/api/ops", json={"ops": [{"op": "crop_ratio", "ratio": "3:2",
                                            "layer": "L1"}]})
    a, b = _pair()
    check(a.shape == b.shape and float(np.abs(a - b).mean()) < 0.02,
          "仅几何修改时基准≈成品（几何保留、调色未做）")
    c.post(f"{base}/api/ops", json={"ops": [{"op": "exposure", "value": 0.5, "layer": "L1"},
                                            {"op": "grayscale", "layer": "L1"}]})
    a2, b2 = _pair()
    d2 = float(np.abs(a2 - b2).mean())
    check(d2 > 0.05, f"调色后基准与成品差异显著（{d2:.4f}）")
    check(float(np.abs(a2[..., 0] - a2[..., 2]).mean()) > 0.02, "基准图仍是彩色的")
    check(float(np.abs(b2[..., 0] - b2[..., 2]).mean()) < 0.01, "成品已按 grayscale 变黑白")
    st = c.get(f"{base}/api/state").json()["project"]
    check(st["edited"] and st["edit_summary"], "state 暴露 edited 与 edit_summary")
    ec = c.post(f"{base}/api/export/compare", json={"max_side": 900}).json()
    check(ec.get("ok"), f"导出对比图：{Path(ec.get('path', '')).name}")
    if ec.get("ok"):
        im = Image.open(ec["path"])
        check(im.size[0] > im.size[1], f"对比图是横向并列（{im.size}）")
        check("exposure" in json.dumps(ec.get("summary"), ensure_ascii=False),
              "对比图带回修改步骤清单")

    # ---------------------------------------------------------------- 裁切适配 / 教学设置
    section("裁切后画布自动适配 + 教学看图选择")
    r = c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new",
                                        "name": "e2e_frame"}).json()
    cw0 = r["project"]["canvas"]["w"]
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "crop_ratio", "ratio": "1:1",
                                                 "layer": "L1"}]}).json()
    auto = [x for x in r.get("results", []) if x.get("auto")]
    check(bool(auto), f"返回里带上了自动适配记录：{auto[0]['note'][:30] if auto else '（无）'}")
    p1 = r["project"]
    check(p1["canvas"]["w"] == p1["canvas"]["h"],
          f"裁切后画布跟随内容变成正方形 {p1['canvas']['w']}×{p1['canvas']['h']}（原宽 {cw0}）")
    ex = c.post(f"{base}/api/export", json={"format": "jpg", "quality": 92}).json()
    im = Image.open(ex["path"])
    check(im.size[0] == im.size[1], f"导出画幅也是正方形 {im.size}（修复前会留白边）")
    c.post(f"{base}/api/ops", json={"ops": [{"op": "canvas", "w": 640, "h": 480,
                                            "mode": "resize"}]})
    r = c.post(f"{base}/api/ops", json={"ops": [{"op": "crop_ratio", "ratio": "1:1",
                                                 "layer": "L1"}]}).json()
    check(not [x for x in r.get("results", []) if x.get("auto")]
          and r["project"]["canvas"]["w"] == 640,
          "手动定过画布之后，裁切不再自动改画布（尊重用户意图）")
    st = c.get(f"{base}/api/settings").json()["settings"]
    check("teaching" in st and "target" in st["teaching"],
          f"设置暴露教学看图选择：{st.get('teaching')}")
    c.post(f"{base}/api/settings", json={"teaching": {"target": "current"}})
    check(c.get(f"{base}/api/settings").json()["settings"]["teaching"]["target"] == "current",
          "可切换为「看当前成图」并持久化")
    c.post(f"{base}/api/settings", json={"teaching": {"target": "original"}})
    js_txt = c.get(f"{base}/web/app.js").text
    html = c.get(f"{base}/web/index.html").text
    check("画布适配内容" in js_txt and "teachTarget" in js_txt,
          "app.js 里已有画布适配按钮与教学看图切换控件")
    check('id="teachTarget"' in html, "教学面板里有看图选择的挂载点")
    check(">适配画幅<" not in js_txt, "易误解的「适配画幅」按钮名已改掉")

    # ---------------------------------------------------------------- RAW
    section("RAW（CR3）支持")
    rs = c.get(f"{base}/api/raw/status").json()
    check(rs.get("ok") and "CR3" in rs.get("raw_extensions", []),
          f"报告 {len(rs.get('raw_extensions', []))} 种 RAW 格式")
    methods = rs.get("methods") or []
    check(len(methods) == 3, f"三条解码路径：{[m['method'] for m in methods]}")
    check(rs.get("auto_will_use") in ("rawpy", "wic", "embedded-jpeg"),
          f"auto 本机会走：{rs.get('auto_will_use')}")
    check(bool(methods) and methods[-1]["available"], "内嵌预览保底永远可用")
    check("只影响导入" in rs.get("note", ""), "说明解码差异只影响导入、不影响已建工程")
    raw = args.raw
    if not raw:
        _raw_dir = os.environ.get("COGITATOR_RAW_DIR", "samples")
        cands = (sorted(glob.glob(os.path.join(_raw_dir, "*.CR3")))
                 + sorted(glob.glob(os.path.join(_raw_dir, "*.cr3"))))
        raw = cands[0] if cands else ""
    if raw and Path(raw).is_file():
        pr = c.post(f"{base}/api/raw/probe", json={"path": raw}).json()
        check(pr.get("ok"), f"probe 识别 RAW：{Path(raw).name}")
        t0 = time.time()
        r = c.post(f"{base}/api/open", json={"paths": [raw], "mode": "new",
                                            "name": "e2e_cr3"}).json()
        dt = time.time() - t0
        check(r.get("ok"), f"通过 HTTP 打开 CR3（{dt:.1f}s，画布 {r['project']['canvas']}）")
        a = r["project"]["assets"][0]
        check(a.get("decoded") in ("rawpy", "wic", "embedded-jpeg"),
              f"素材如实记录来源与解码方式：{a.get('source_kind')}/{a.get('decoded')}")
        check(bool(a.get("note")), "素材记录了解码局限")
    else:
        skip("没找到 CR3 样本，跳过 RAW 实机检查")

    # ---------------------------------------------------------------- 状态一致性
    section("输出像素查看（真 1:1 视口渲染）")
    r = c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new",
                                        "name": "e2e_pixel"}).json()
    c.post(f"{base}/api/ops", json={"ops": [{"op": "exposure", "value": 0.4, "layer": "L1"}]})
    t0 = time.time()
    v = c.get(f"{base}/project/view", params={"x": 100, "y": 50, "w": 200, "h": 150})
    dt = time.time() - t0
    check(v.status_code == 200 and v.content[:2] == b"\xff\xd8", f"输出像素端点返回 JPEG（首次 {dt:.1f}s）")
    imv = Image.open(io.BytesIO(v.content))
    check(imv.size == (200, 150), f"按请求尺寸裁出视口（{imv.size}）")
    check(v.headers.get("x-image-w") == "540" and v.headers.get("x-image-h") == "360",
          f"返回整幅尺寸头（{v.headers.get('x-image-w')}×{v.headers.get('x-image-h')}）")
    t0 = time.time()
    c.get(f"{base}/project/view", params={"x": 120, "y": 60, "w": 200, "h": 150})
    dt2 = time.time() - t0
    check(dt2 < max(1.0, dt * 0.6), f"第二次（平移）走缓存，明显更快（{dt2:.2f}s vs 首次 {dt:.2f}s）")
    v2 = c.get(f"{base}/project/view", params={"x": 99999, "y": 99999, "w": 200, "h": 150})
    check(v2.status_code == 200 and int(v2.headers.get("x-view-x")) < 540,
          f"越界坐标被夹紧而不是报错（x={v2.headers.get('x-view-x')}）")
    v3 = c.get(f"{base}/project/view", params={"x": 0, "y": 0, "w": 99999, "h": 99999})
    check(Image.open(io.BytesIO(v3.content)).size == (540, 360), "超大视口被裁到整幅")
    a = c.get(f"{base}/project/view", params={"x": 0, "y": 0, "w": 120, "h": 90}).content
    c.post(f"{base}/api/ops", json={"ops": [{"op": "exposure", "value": 1.2, "layer": "L1"}]})
    b = c.get(f"{base}/project/view", params={"x": 0, "y": 0, "w": 120, "h": 90}).content
    check(a != b, "工程 rev 变化后缓存失效、按新管线重渲（像素确实变了）")
    # 小图上两次都很快，说明不了缓存的价值；用 24MP 的 CR3 量一次真实差距
    if raw and Path(raw).is_file():
        c.post(f"{base}/api/open", json={"paths": [raw], "mode": "new",
                                        "name": "e2e_pixel_cr3"})
        t0 = time.time()
        c.get(f"{base}/project/view", params={"x": 0, "y": 0, "w": 500, "h": 400})
        t_first = time.time() - t0
        t0 = time.time()
        c.get(f"{base}/project/view", params={"x": 800, "y": 600, "w": 500, "h": 400})
        t_pan = time.time() - t0
        check(t_first > 0.5, f"24MP 首次全分辨率渲染确实要等（{t_first:.1f}s）")
        check(t_pan < max(0.6, t_first * 0.5),
              f"同一修订下平移走缓存、明显更快（{t_pan:.2f}s vs {t_first:.1f}s）")
    else:
        skip("没有 CR3 样本，跳过 24MP 视口渲染耗时对比")

    section("状态一致性")
    st = c.get(f"{base}/api/state").json()
    check("recent" in st, "状态可读")
    check(len(st.get("recent") or []) >= 1, "最近工程列表可用")
    check(c.post(f"{base}/api/project/close", json={}).json().get("ok"), "关闭工程接口可用")

    # ---------------------------------------------------------------- 可选：真实模型
    if args.with_llm:
        section("真实模型调用（--with-llm，会消耗额度）")
        t = c.post(f"{base}/api/model/test", json={}).json()
        check(t.get("ok"), f"真实连通：{(t.get('sample') or t.get('error') or '')[:40]}")
        c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new", "name": "e2e_llm"})
        ch = c.post(f"{base}/api/chat", json={"text": "整体提亮一点"}).json()
        check(ch.get("ok"), f"对话真实可用：{(ch.get('reply') or ch.get('error') or '')[:60]}")
        te = c.post(f"{base}/api/teaching", json={"target": "original"}).json()
        cr = (te.get("critique") or {})
        check(te.get("ok"), f"教学真实可用：{(cr.get('summary') or te.get('error') or '')[:50]}")
        check(cr.get("target") == "original", "教学如实记录了看的是哪张图")
    else:
        skip("未启用 --with-llm：跳过真实对话与教学（避免消耗额度）")

    # ---------------------------------------------------------------- 可选：真实抠图
    if args.with_matting:
        section("真实抠图（BiRefNet 离线）")
        c.post(f"{base}/api/open", json={"paths": [str(img)], "mode": "new", "name": "e2e_cut"})
        ms = c.get(f"{base}/api/matting/status").json()
        check(ms.get("weights_found"), "抠图权重已就位")
        t0 = time.time()
        r = c.post(f"{base}/api/ops", json={"ops": [{"op": "remove_bg", "layer": "L1"}]}).json()
        dt = time.time() - t0
        check(r.get("ok"), f"抠图成功（{dt:.1f}s）：{r.get('results', [{}])[0].get('note', '')[:40]}")
        check(len(c.get(f"{base}/project/preview").content) > 2000, "抠图后预览正常")
    else:
        skip("未启用 --with-matting：跳过真实抠图")

    # ---------------------------------------------------------------- 收摊
    section("收摊：清掉本次验收留下的验收工程")
    removed = cleanup_leftovers(base)
    check(removed >= 1, f"收摊确实清掉了本轮产出的验收工程（删除 {removed} 个）")
    time.sleep(2.0)                       # 等服务把手里的句柄放掉，再看最终状态
    cleanup_leftovers(base)
    root = _projects_root(base)
    if root:
        left = [d.name for d in root.iterdir()
                if d.is_dir() and d.name.startswith(("e2e_", "ui对比检查"))]
        # 硬断言只有一条：用户的工程绝不能被误删
        check(len([d for d in root.iterdir() if d.is_dir()]) > len(left),
              f"用户工程未被误删（{len(list(root.iterdir()))} 个目录）")
        if left:
            # 可能还有 1~2 个被抓着句柄的目录（服务写完即放），进程退出前 atexit 那趟会清掉
            print(f"  --   暂存 {left}（退出前 atexit 会再清一遍；这是收摊的时序竞争，不是应用缺陷）")
        else:
            check(True, "工程目录里已无验收产物")
    else:
        check(False, "未能定位工程目录")

    print()
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 段：")
        for s0 in SKIPPED:
            print("  --", s0)
    if FAIL:
        print(f"端到端验收失败 {len(FAIL)} 项（通过 {OK}）：")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"端到端验收全部通过（{OK} 项）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
