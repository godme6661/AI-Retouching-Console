"""RAW（CR3）支持自检：
  · 合成容器：内嵌多张不同尺寸 JPEG 时，必须挑出最大的那张，并忽略过小/损坏的
  · 真机 CR3（E 盘存在时才跑）：必须取到全尺寸预览，且落盘字节与文件内原始 JPEG 完全一致
  · 走 rawpy 的路径（未安装时）必须给出可读提示，而不是崩
  · 通过工程导入后，渲染链路正常（能出预览、能套风格、能导出）
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from cogitator import raw_io  # noqa: E402
from cogitator.config import Settings  # noqa: E402
from cogitator.document import DocError, Project  # noqa: E402

FAIL: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="cogitator_raw_"))
E_DIR = os.environ.get("COGITATOR_RAW_DIR", "samples")


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def make_jpeg(w: int, h: int, color: tuple[int, int, int], quality: int = 92) -> bytes:
    bio = io.BytesIO()
    a = np.zeros((h, w, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = color
    a[: h // 3] = (240, 240, 240)
    Image.fromarray(a).save(bio, format="JPEG", quality=quality)
    return bio.getvalue()


def make_fake_raw(path: Path, jpegs: list[bytes], pad: int = 4096) -> Path:
    """拼一个「BMFF 盒 + mdat 内嵌 JPEG」的假 RAW，用来测挑选逻辑。"""
    mdat = b"\x00" * 16
    for j in jpegs:
        mdat += j + b"\x00" * pad
    boxes = [(b"ftyp", b"crx " + b"\x00" * 16),
             (b"moov", b"\x00" * 64),
             (b"mdat", mdat)]
    out = bytearray()
    for typ, payload in boxes:
        out += struct.pack(">I", len(payload) + 8) + typ + payload
    path.write_bytes(bytes(out))
    return path


def main() -> int:
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = TMP / "projects"
    docmod.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings.load()

    print("== 环境 ==")
    print(f"  rawpy 可用：{raw_io.rawpy_available()}")
    print(f"  RAW 扩展名：{len(raw_io.RAW_EXTS)} 种，例如 "
          f"{', '.join(sorted(e.lstrip('.') for e in list(raw_io.RAW_EXTS))[:8])}…")
    check(".cr3" in raw_io.RAW_EXTS and ".nef" in raw_io.RAW_EXTS, "CR3/NEF 在支持列表里")
    check(raw_io.is_raw("a.CR3") and not raw_io.is_raw("a.jpg"), "扩展名判定正确（大小写不敏感）")

    print("\n== 合成容器：必须挑出最大的内嵌预览 ==")
    small = make_jpeg(320, 240, (200, 60, 60))
    mid = make_jpeg(1600, 1200, (60, 200, 60))
    big = make_jpeg(3000, 2000, (60, 60, 200))
    broken = b"\xff\xd8\xff\xe0" + b"\x00" * 5000 + b"\xff\xd9"          # 假 JPEG，不能解码
    fake = make_fake_raw(TMP / "fake.DNG", [small, broken, big, mid])
    got = raw_io.embedded_preview(fake)
    check(got is not None, "能从合成容器里取出预览")
    if got:
        check(got.image.size == (3000, 2000), f"挑出的是最大那张（{got.image.size}）")
        check("embedded-jpeg" == got.method, "标注了解码方式")
        check(got.jpeg_bytes == big, "取出的 JPEG 字节与原始内嵌数据完全一致（零重编码）")
        check("8bit" in got.note and "RAW" in got.note, f"提示如实说明局限：{got.note[:38]}…")

    too_small = make_fake_raw(TMP / "tiny.CR3", [make_jpeg(160, 120, (10, 10, 10))])
    check(raw_io.embedded_preview(too_small) is None, "只有小缩略图时返回 None（不硬凑）")

    print("\n== 真机 CR3（样本）==")
    real = sorted(glob.glob(str(Path(E_DIR) / "*.CR3")))
    if not real:
        print("  （没找到 CR3 样本：可放到 samples/ 或设 COGITATOR_RAW_DIR，跳过真机项）")
    else:
        src = Path(real[-1])
        print(f"  样本：{src.name}  {src.stat().st_size / 1048576:.1f} MB")
        info = raw_io.probe(src)
        check(info.get("ok") and info.get("raw"), f"probe 识别为 RAW：{info.get('file')}")
        check(info.get("method") in ("rawpy", "wic", "embedded-jpeg"),
              f"probe 报告会用哪条路径：{info.get('method')}")
        check(bool(info.get("methods")), "probe 附带三条路径的可用性明细")
        if not raw_io.rawpy_available():
            check(info.get("size", [0, 0])[0] >= 3000,
                  f"内嵌预览达到全分辨率量级：{info.get('size')}")
        dec = raw_io.decode(src)
        check(dec.image.size[0] >= 3000, f"decode 出图 {dec.image.size}（{dec.method}）")
        check(dec.method == raw_io.resolve_method("auto"),
              f"auto 选中的路径与实际使用一致：{dec.method}")
        arr = np.asarray(dec.image, dtype=np.float32) / 255.0
        check(float(arr.std()) > 0.05, f"不是空图（标准差 {float(arr.std()):.3f}）")
        check(arr.shape[2] == 3, "三通道 RGB")

        print("\n== 三条存储路径都要各自验证（不要靠 if 跳过）==")
        d_pre, m_pre = raw_io.convert_to_source(src, TMP / "conv", "P1", prefer="preview")
        inner = raw_io.embedded_preview(src)
        check(m_pre["decoded"] == "embedded-jpeg" and m_pre.get("stored", "").startswith("embedded-jpeg"),
              f"prefer=preview → 内嵌预览（{m_pre.get('stored')}）")
        check(inner is not None and inner.jpeg_bytes == d_pre.read_bytes(),
              f"内嵌 JPEG **逐字节**原样落盘（{d_pre.stat().st_size / 1024:.0f} KB，未重编码）")
        if raw_io.wic_available():
            d_wic, m_wic = raw_io.convert_to_source(src, TMP / "conv", "P2", prefer="wic")
            check(m_wic["decoded"] == "wic" and m_wic.get("stored") == "jpeg-q95",
                  f"prefer=wic → WIC 解码后存 JPEG q95（{m_wic.get('stored')}）")
            check(m_wic["size"][0] >= 3000 and m_wic["size"] != m_pre["size"],
                  f"WIC 尺寸 {m_wic['size']} 与内嵌 {m_pre['size']} 不同（证明走了真解码）")
            check("8bit" in (m_wic.get("note") or ""), "WIC 的局限也如实记录")
        d_auto, m_auto = raw_io.convert_to_source(src, TMP / "conv", "P3", prefer="auto")
        check(m_auto["decoded"] == raw_io.resolve_method("auto"),
              f"prefer=auto → {m_auto['decoded']}（与本机能力一致）")

        print("\n== 通过工程导入并编辑（真机 CR3）==")
        proj = Project.create_from_images([str(src)], settings=settings, name="CR3真机")
        st = proj.state()
        l1 = st["layers"][0]
        check(l1["src_size"][0] >= 3000, f"工程里图层尺寸 {l1['src_size']}")
        check(st["canvas"]["w"] == l1["src_size"][0], f"画布跟随 {st['canvas']['w']}×{st['canvas']['h']}")
        asset = st["assets"][0]
        raw_asset = proj.doc["assets"][0]
        check(asset.get("source_kind") == "raw", f"素材记录了来源类型：{asset.get('source_kind')}")
        check(asset.get("decoded") in ("rawpy", "wic", "embedded-jpeg"),
              f"记录了实际解码方式：{asset.get('decoded')}")
        check(str(asset.get("original", "")).lower().endswith(".cr3"),
              "记录了原始 CR3 路径（可溯源）")
        check(bool(asset.get("note")), f"记录了解码局限：{(asset.get('note') or '')[:38]}…")
        check((proj.dir / raw_asset["file"]).is_file(), "工程源图文件已落盘")
        check(proj.preview_path().is_file(), "预览已生成")
        check(proj.preview_path().stat().st_size > 5000,
              f"预览非空（{proj.preview_path().stat().st_size / 1024:.0f} KB）")
        res = proj.apply_ops([{"op": "auto_enhance", "strength": 45, "layer": "L1"},
                              {"op": "apply_style", "style": "jp-airy", "strength": 70,
                               "layer": "L1"}])
        check(len(res) == 2, f"在 CR3 上应用调整与风格：{[r['op'] for r in res]}")
        check(proj.layer("L1")["ops"][0]["op"] == "auto_enhance", "管线记录正确")
        out = proj.export("jpg", quality=90, max_side=1600)
        check(out.is_file() and Image.open(out).size[0] == 1600, f"导出成功 {out.name}")
        check(proj.undo_step(), "CR3 工程同样可撤销")

    print("\n== EXIF 方向必须被应用（否则竖拍照片会躺倒）==")
    # 造一张 240×120 的图：左上角有白色方块（方向一错就能看出来）
    base = np.zeros((120, 240, 3), np.uint8)
    base[..., 2] = 180
    base[10:40, 10:40] = (255, 255, 255)
    src_img = Image.fromarray(base)
    exif = Image.Exif()
    exif[274] = 6                                     # 顺时针 90°（竖拍常见）
    rot_path = TMP / "rotated.jpg"
    src_img.save(rot_path, exif=exif)
    with Image.open(rot_path) as raw_im:
        check(int(raw_im.getexif().get(274, 1)) == 6, "测试图确实带方向标记 6")
        raw_im.load()
        expected = raw_im.convert("RGB").rotate(-90, expand=True)   # 顺时针 90°
        upright = raw_io.upright(raw_im)
        check(upright.size == (120, 240), f"摆正后尺寸变为 {upright.size}（原 240×120）")
        same = np.array_equal(np.asarray(upright.convert("RGB")), np.asarray(expected))
        check(same, "摆正结果与「顺时针旋转 90°」逐像素一致（方向没搞反）")
        exp = np.asarray(expected, np.float32)
        ys, xs = np.nonzero(exp[..., 0] > 200)
        cy, cx = float(ys.mean()), float(xs.mean())
        check(cx > exp.shape[1] * 0.5 and cy < exp.shape[0] * 0.5,
              f"白块落在右上方（中心 {cx:.0f},{cy:.0f}／画幅 {exp.shape[1]}×{exp.shape[0]}）")
        arr = np.asarray(upright.convert("RGB"), np.float32)
        check(arr[10:40, 80:110].mean() > 200 and arr[200:230, 10:40].mean() < 100,
              f"摆正后白块在右上、左下为背景（{arr[10:40, 80:110].mean():.0f} / {arr[200:230, 10:40].mean():.0f}）")
    # 工程导入：记录的尺寸必须是摆正后的尺寸，渲染也必须一致
    projR = Project.create_from_images([str(rot_path)], settings=settings, name="方向测试")
    stR = projR.state()
    check(stR["layers"][0]["src_size"] == [120, 240],
          f"工程按摆正后的尺寸建画布：{stR['layers'][0]['src_size']}")
    check(stR["canvas"] == {"w": 120, "h": 240, "bg": None}, f"画布 {stR['canvas']}")
    got, _ = projR.render_full(1.0)
    check(got.shape[:2] == (240, 120), f"渲染输出尺寸 {got.shape[:2]} 与记录一致")
    g = np.clip(got, 0, 1)
    check(float(g[10:40, 70:100].mean()) > 0.7, "渲染结果里白块也在右上（方向一致）")
    check(float(g[200:230, 10:40].mean()) < 0.4, "渲染结果里左下不是白块")

    print("\n== 零算子管线必须逐像素等于源图（能抓出整幅旋转/翻转/色偏）==")
    plain = make_jpeg(400, 300, (120, 160, 90))
    plain_path = TMP / "plain.jpg"
    plain_path.write_bytes(plain)
    projP = Project.create_from_images([str(plain_path)], settings=settings, name="零算子")
    gotP, _ = projP.render_full(projP.preview_scale())
    with Image.open(plain_path) as im:
        refP = np.asarray(im.convert("RGB").resize((gotP.shape[1], gotP.shape[0]), Image.LANCZOS),
                          dtype=np.float32) / 255.0
    dP = float(np.abs(refP - gotP).mean())
    check(dP < 0.01, f"普通 JPEG 零算子差异 {dP:.5f}")
    if real:
        src = Path(real[-1])
        projZ = Project.create_from_images([str(src)], settings=settings, name="零算子CR3")
        gotZ, _ = projZ.render_full(projZ.preview_scale())
        # 基准必须是**工程自己的源图**（它来自哪条解码路径都行），不能用内嵌 JPEG 硬当基准——
        # 否则一旦 auto 选了 WIC，这个断言就在测"WIC 与内嵌是否相同"，而那是错的
        stored = projZ.dir / projZ.doc["assets"][0]["file"]
        with Image.open(stored) as im:
            refZ = np.asarray(im.convert("RGB").resize((gotZ.shape[1], gotZ.shape[0]), Image.LANCZOS),
                              dtype=np.float32) / 255.0
        dZ = float(np.abs(refZ - gotZ).mean())
        check(dZ < 0.01,
              f"CR3 零算子与**工程源图**逐像素一致（差异 {dZ:.5f}，解码路径 "
              f"{projZ.doc['assets'][0].get('decoded')}）")
        d_rot = float(np.abs(np.rot90(refZ, 1) - gotZ).mean()) if np.rot90(refZ, 1).shape == gotZ.shape else -1
        check(d_rot < 0 or d_rot > 0.05,
              f"对照：旋转 90° 后差异 {d_rot:.5f}（远大于零算子差异，说明这类错误抓得到）")

    print("\n== 解码路径选择（推广到其它机器的关键）==")
    ms = raw_io.method_status()
    check({m["method"] for m in ms} == {"rawpy", "wic", "embedded-jpeg"},
          f"报告三条解码路径：{[m['method'] for m in ms]}")
    check(any(m["method"] == "embedded-jpeg" and m["available"] for m in ms),
          "内嵌预览永远可用（保底路径）")
    check(all(m.get("quality") and m.get("cost") for m in ms), "每条路径都注明了画质与代价")
    check(raw_io.resolve_method("preview") == "embedded-jpeg", "prefer=preview → 内嵌预览")
    check(raw_io.resolve_method("wic") == "wic", "prefer=wic 被尊重")
    check(raw_io.resolve_method("rawpy") == "rawpy", "prefer=rawpy 被尊重")
    auto = raw_io.resolve_method("auto")
    check(auto in ("rawpy", "wic", "embedded-jpeg"), f"auto 解析为 {auto}")
    if raw_io.rawpy_available():
        check(auto == "rawpy", "装了 rawpy 时 auto 优先选它（画质最好）")
    elif raw_io.wic_available():
        check(auto == "wic", "没有 rawpy 但有 WIC 时 auto 选 WIC")
    else:
        check(auto == "embedded-jpeg", "都没有时 auto 回退到内嵌预览")
    ws = raw_io.wic_status()
    check("available" in ws and "reason" in ws,
          f"WIC 状态可读（{ws['reason'][:46]}）")
    check(raw_io.WIC_DUMP.is_file(), "WIC 解码脚本随包分发（cogitator/wic_dump.ps1）")

    if ws.get("available") and real:
        print("\n== WIC 真解码（本机已装扩展）==")
        import tempfile as _tf

        dec = raw_io.decode_with_wic(real[-1])
        check(dec.method == "wic" and dec.image.size[0] >= 3000,
              f"WIC 解出 {dec.image.size}（{dec.note[:34]}…）")
        a = np.asarray(dec.image.convert("RGB"), np.float32) / 255.0
        emb = raw_io.embedded_preview(real[-1])
        b = np.asarray(emb.image.resize(dec.image.size), np.float32) / 255.0
        LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
        la, lb = a @ LUMA, b @ LUMA
        check(float(la.mean()) < float(lb.mean()),
              f"WIC 渲染更暗（{float(la.mean()):.3f} < {float(lb.mean()):.3f}）—— 需自行补影调")
        hi = lb > 0.95
        if hi.any():
            check(float(la[hi].var()) > float(lb[hi].var()),
                  f"WIC 在 JPEG 已压死的高光区仍有层次（方差 {float(la[hi].var()):.5f} "
                  f"vs {float(lb[hi].var()):.5f}）")
        small = raw_io.decode_with_wic(real[-1], max_side=800)
        check(max(small.image.size) == 800, f"WIC 支持按 max_side 缩小解码 {small.image.size}")
        check(raw_io.decode(real[-1], prefer="wic").method == "wic", "decode(prefer=wic) 走 WIC")
        left = glob.glob(str(Path(_tf.gettempdir()) / "wic_*.bin"))
        check(not left, f"WIC 临时文件已清理（残留 {len(left)} 个）")

    print("\n== 错误路径可读 ==")
    if not raw_io.rawpy_available():
        try:
            raw_io.decode(real[-1] if real else fake, prefer="rawpy")
            check(False, "强制 rawpy 应报错")
        except raw_io.RawUnsupported as e:
            check("rawpy" in str(e), f"强制 rawpy 时给出可读提示：{str(e)[:44]}")
    try:
        raw_io.decode(TMP / "nope.CR3")
        check(False, "不存在的文件应报错")
    except raw_io.RawUnsupported as e:
        check("不存在" in str(e), "不存在的文件报错清晰")
    try:
        Project.create_from_images([str(TMP / "x.txt")], settings=settings)
        check(False, "不支持的格式应被拒")
    except DocError as e:
        check("RAW" in str(e) or "支持" in str(e), f"不支持格式给出支持清单：{str(e)[:40]}…")

    print()
    if FAIL:
        print(f"RAW 自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("RAW 支持自检全部通过。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
