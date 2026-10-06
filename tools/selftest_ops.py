"""算子层自检：注册表 ↔ 实现是否一一对应、数值行为是否符合刻度约定。"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from cogitator import image_ops, ops  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAIL.append(msg)


def main() -> int:
    print("== 注册表与实现一致性 ==")
    impl_names = set(image_ops.COLOR_FUNCS) | set(image_ops.GEOM_FUNCS)
    pipeline_specs = {s.name for s in ops.SPECS if s.kind == "pipeline"}
    mask_specs = {s.name for s in ops.SPECS if s.kind == "mask"}
    missing = pipeline_specs - impl_names
    extra = impl_names - pipeline_specs
    check(not missing, f"管线算子都有实现（缺 {sorted(missing) or '无'}）")
    check(not extra, f"没有游离实现（多 {sorted(extra) or '无'}）")
    need_mask = {"remove_bg", "mask_brush", "mask_from_color", "clear_mask"}
    check(mask_specs == need_mask, f"蒙版算子归入 mask 类（现为 {sorted(mask_specs)}）")

    print("\n== 校验器 ==")
    try:
        ops.validate_op({"op": "exposure", "value": 0.3})
        check(True, "合法指令通过")
    except ops.OpError as e:
        check(False, f"合法指令被判非法：{e}")
    for bad, why in [
        ({"op": "not_an_op"}, "未知算子被拒"),
        ({"op": "exposure", "value": 99}, "超范围被拒"),
        ({"op": "exposure"}, "缺必填被拒"),
        ({"op": "exposure", "value": 1, "typo": 2}, "多余参数被拒"),
        ({"op": "crop_ratio", "ratio": "7:5"}, "非法枚举被拒"),
    ]:
        try:
            ops.validate_op(bad)
            check(False, why)
        except ops.OpError:
            check(True, why)
    v = ops.validate_op({"op": "levels"})
    check(v.get("black") == 0 and v.get("white") == 255, "缺省参数自动补齐")
    check(ops.validate_op({"op": "exposure", "value": 1}).get("layer") is None,
          "管线算子缺省作用于当前图层")
    try:
        ops.validate_ops([{"op": "exposure", "value": 0.1}] * 41)
        check(False, "超量指令被拒")
    except ops.OpError:
        check(True, "超量指令被拒")

    print("\n== 手册生成 ==")
    m = ops.manual()
    check("exposure" in m and "remove_bg" in m and "crop_ratio" in m, "手册覆盖全部算子")
    check(all(s.name in m for s in ops.SPECS), "手册无遗漏算子")

    print("\n== 调色数值行为 ==")
    img = np.full((8, 8, 3), 0.5, dtype=np.float32)
    e = image_ops.op_exposure(img, {"value": 1.0})
    check(abs(float(e.mean()) - 1.0) < 1e-5, "曝光 +1EV 使中灰翻倍")
    b = image_ops.op_brightness(img, {"value": 20})
    check(abs(float(b.mean()) - 0.6) < 1e-5, "亮度 +20 → +0.1")
    c_up = image_ops.op_contrast(np.full((4, 4, 3), 0.25, np.float32), {"value": 50})
    check(float(c_up.mean()) < 0.25, "提对比：暗部更暗")
    c_dn = image_ops.op_contrast(np.full((4, 4, 3), 0.25, np.float32), {"value": -50})
    check(float(c_dn.mean()) > 0.25, "降对比：暗部抬亮")
    g = image_ops.op_grayscale(np.dstack([np.ones((4, 4)), np.zeros((4, 4)), np.zeros((4, 4))]).astype(np.float32), {})
    check(abs(float(g[..., 0].mean()) - 0.2126) < 1e-3, "黑白按亮度权重")
    s0 = image_ops.op_saturation(img, {"value": -100})
    check(np.allclose(s0[..., 0], s0[..., 1], atol=1e-5), "饱和度 -100 = 去色")
    bl = image_ops.op_blur(np.pad(np.ones((4, 4, 3), np.float32), ((2, 2), (2, 2), (0, 0))), {"value": 50})
    check(bl.shape == (8, 8, 3), "模糊保持尺寸")
    chk = image_ops.op_sharpen(img, {"value": 0})
    check(np.allclose(chk, img, atol=1e-5), "锐化 0 = 原样")

    print("\n== 新增：分区 HSL / 分离色调 / 暗角 / 颗粒 ==")
    # HSV 往返必须无损，否则分区 HSL 会整体偏色
    for rgb in ((0.8, 0.2, 0.2), (0.1, 0.7, 0.3), (0.2, 0.3, 0.9), (0.55, 0.55, 0.55)):
        a = np.array([[list(rgb)]], np.float32)
        h, s, v = image_ops.rgb_to_hsv(a)
        back = image_ops.hsv_to_rgb(h, s, v)
        check(np.allclose(back, a, atol=2e-3), f"RGB↔HSV 往返一致 {rgb}")

    red = np.zeros((8, 8, 3), np.float32); red[..., 0] = 0.8; red[..., 1] = 0.2; red[..., 2] = 0.2
    green = np.zeros((8, 8, 3), np.float32); green[..., 1] = 0.8
    mix = np.concatenate([red, green], axis=1)
    out = image_ops.op_hsl(mix, {"color": "green", "hue": -20, "saturation": -40, "width": 45})
    check(np.allclose(out[:, :8], mix[:, :8], atol=1e-4), "分区 HSL 不碰非目标色带（红色原样）")
    check(not np.allclose(out[:, 8:], mix[:, 8:], atol=1e-3), "分区 HSL 改变了目标色带（绿色）")
    desat = image_ops.op_hsl(mix, {"color": "green", "saturation": -100, "width": 45})
    spread = (desat[:, 8:].max(-1) - desat[:, 8:].min(-1)).mean()
    check(float(spread) < 0.05, f"绿带去饱和后通道差归零（{float(spread):.4f}）")
    check(float((mix[:, 8:].max(-1) - mix[:, 8:].min(-1)).mean()) > 0.5, "对照：原绿色通道差很大")

    dark = np.full((8, 8, 3), 0.15, np.float32)
    bright = np.full((8, 8, 3), 0.85, np.float32)
    tone = np.concatenate([dark, bright], axis=1)
    st = image_ops.op_split_tone(tone, {"highlight_hue": 45, "highlight_sat": 40,
                                        "shadow_hue": 210, "shadow_sat": 40, "balance": 0})
    check(abs(float(st[:, :8].mean()) - 0.15) < 0.03, "分离色调基本保持阴影亮度（零均值色偏）")
    check(float(st[:, :8, 2].mean()) > float(st[:, :8, 0].mean()) + 0.01, "阴影被染冷（蓝>红）")
    check(float(st[:, 8:, 0].mean()) > float(st[:, 8:, 2].mean()) + 0.01, "高光被染暖（红>蓝）")
    st0 = image_ops.op_split_tone(tone, {"highlight_sat": 0, "shadow_sat": 0})
    check(np.allclose(st0, tone, atol=1e-6), "着色强度 0 = 原样")

    flat = np.full((40, 40, 3), 0.8, np.float32)
    vg = image_ops.op_vignette(flat, {"amount": 50, "feather": 50})
    check(abs(float(vg[20, 20].mean()) - 0.8) < 0.01, "暗角中心不受影响")
    check(float(vg[0, 0].mean()) < float(vg[20, 20].mean()) - 0.15, "暗角四角明显压暗")
    vgb = image_ops.op_vignette(flat, {"amount": -40, "feather": 50})
    check(float(vgb[0, 0].mean()) > 0.8, "负值暗角=提亮四角")
    check(np.allclose(image_ops.op_vignette(flat, {"amount": 0}), flat), "暗角 0 = 原样")

    mid = np.full((64, 64, 3), 0.5, np.float32)
    g1 = image_ops.op_grain(mid, {"amount": 40, "size": 1.0})
    g2 = image_ops.op_grain(mid, {"amount": 40, "size": 1.0})
    check(np.allclose(g1, g2), "颗粒确定性：同参数两次渲染逐像素一致")
    check(0.002 < float(np.abs(g1 - mid).mean()) < 0.15, "颗粒幅度合理")
    hi = image_ops.op_grain(np.full((64, 64, 3), 0.98, np.float32), {"amount": 40, "size": 1.0})
    check(float(np.abs(g1 - mid).mean()) > float(np.abs(hi - 0.98).mean()) * 1.5,
          "颗粒在中间调最明显、高光收敛")
    check(np.allclose(image_ops.op_grain(mid, {"amount": 0}), mid), "颗粒 0 = 原样")


    print("\n== 曲线 / 色阶 ==")
    ramp = np.linspace(0, 1, 256, dtype=np.float32).reshape(1, 256, 1).repeat(3, axis=2)
    lv = image_ops.op_levels(ramp, {"black": 0, "white": 128, "gamma": 1.0})
    check(abs(float(lv[0, 255, 0]) - 1.0) < 1e-5, "色阶白场 128 → 最亮处到 1")
    cv = image_ops.op_curve(ramp, {"channel": "rgb", "points": [[0, 0], [255, 128]]})
    check(abs(float(cv[0, 255, 0]) - 128 / 255) < 0.01, "曲线可把白点压到中灰")

    print("\n== 几何 ==")
    base = np.zeros((100, 200, 3), np.float32)
    m0 = np.ones((100, 200), np.float32)
    i1, m1 = image_ops.geom_crop(base, m0, {"x": 10, "y": 10, "w": 50, "h": 40})
    check(i1.shape == (40, 50, 3) and m1.shape == (40, 50), "裁切同步作用于蒙版")
    i2, m2 = image_ops.geom_crop_ratio(base, m0, {"ratio": "1:1", "anchor": "center"})
    check(i2.shape[0] == i2.shape[1] == 100, "1:1 居中裁切")
    i3, m3 = image_ops.geom_rotate(base, m0, {"angle": 90, "expand": True})
    check(i3.shape[:2] == (200, 100), "旋转 90° 且扩画幅")
    i4, _ = image_ops.geom_straighten(base, m0, {"angle": 3})
    check(i4.shape[0] < 100 and i4.shape[1] < 200, "校正倾斜会内切去黑边")

    # 内切矩形必须真的落在原图内：纯白图校正后不得出现黑角（横竖幅都要过）
    white = np.ones((100, 200, 3), np.float32)
    for ang in (3, 7.5, -6, 12):
        st_img, _ = image_ops.geom_straighten(white, None, {"angle": ang})
        check(float(st_img.min()) > 0.9, f"横幅校正 {ang}° 无黑角（最小 {float(st_img.min()):.3f}）")
    white_p = np.ones((200, 140, 3), np.float32)
    st_p, _ = image_ops.geom_straighten(white_p, None, {"angle": 5})
    check(float(st_p.min()) > 0.9 and st_p.shape[0] > st_p.shape[1],
          f"竖幅校正 5° 无黑角且保持竖幅（得 {st_p.shape[1]}x{st_p.shape[0]}，最小 {float(st_p.min()):.3f}）")

    # 旋转方向：正角度必须是与用户直觉一致的顺时针（左上角的标记应转到右上角）
    mark = np.zeros((100, 200, 3), np.float32)
    mark[10:25, 10:25] = 1.0
    m90, _ = image_ops.geom_rotate(mark, None, {"angle": 90, "expand": True})
    ys, xs = np.nonzero(m90[..., 0] > 0.5)
    check(m90.shape[:2] == (200, 100) and float(xs.mean()) > m90.shape[1] * 0.6 and float(ys.mean()) < m90.shape[0] * 0.4,
          f"旋转 +90° = 顺时针（标记落在 x{xs.mean():.0f}, y{ys.mean():.0f}）")
    i5, m5 = image_ops.geom_flip(base, m0, {"axis": "h"})
    check(i5.shape == base.shape and m5.shape == m0.shape, "翻转同步蒙版")
    i6, m6 = image_ops.geom_resize(base, m0, {"w": 100, "h": 0, "scale": 0})
    check(i6.shape[:2] == (50, 100) and m6.shape == (50, 100), "按宽等比缩放并同步蒙版")

    print("\n== 尺寸预演与真实一致 ==")
    for spec in (
        [{"op": "crop", "x": 5, "y": 5, "w": 120, "h": 60}],
        [{"op": "crop_ratio", "ratio": "16:9"}],
        [{"op": "rotate", "angle": 30, "expand": True}],
        [{"op": "straighten", "angle": 4}],
        [{"op": "resize", "scale": 0.5}],
        [{"op": "crop_ratio", "ratio": "1:1"}, {"op": "resize", "w": 64}],
    ):
        pred = image_ops.pipeline_size((200, 100), spec)
        cur_i, cur_m = base, m0
        for op in spec:
            cur_i, cur_m = image_ops.GEOM_FUNCS[op["op"]](cur_i, cur_m, op)
        real = (cur_i.shape[1], cur_i.shape[0])
        check(pred == real, f"尺寸预演 {spec[0]['op']}{'…' if len(spec) > 1 else ''}: 预测{pred} 实际{real}")

    print("\n== 蒙版工具 ==")
    mk = np.zeros((50, 50), np.float32)
    mk[10:40, 10:40] = 1.0
    grow = image_ops.mask_grow(mk, 3)
    check(float(grow.sum()) > float(mk.sum()), "蒙版扩张生效")
    shrink = image_ops.mask_grow(mk, -3)
    check(float(shrink.sum()) < float(mk.sum()), "蒙版收缩生效")
    fe = image_ops.mask_feather(mk, 4)
    check(fe.max() <= 1.0 and fe.min() >= 0.0 and float(fe[10, 10]) < 1.0, "羽化产生过渡带")
    st = image_ops.mask_draw_strokes(np.ones((50, 50), np.float32),
                                     [{"mode": "erase", "radius": 0.1, "points": [[0.5, 0.5]]}], 50, 50)
    check(float(st[25, 25]) == 0.0 and float(st[0, 0]) == 1.0, "笔画擦除只影响笔刷范围")
    st2 = image_ops.mask_draw_strokes(np.zeros((50, 50), np.float32),
                                      [{"mode": "add", "radius": 0.1, "points": [[0.5, 0.5]]}], 50, 50)
    check(float(st2[25, 25]) == 1.0, "笔画恢复生效")
    wb = image_ops.mask_from_color(np.dstack([np.zeros((20, 20)), np.zeros((20, 20)),
                                              np.ones((20, 20))]).astype(np.float32),
                                   0.5, 0.5, 20, False, None)
    check(float(wb.mean()) > 0.9, "魔棒选中同色区域")

    print("\n== 端口探测与启动脚本（交付物回归）==")
    from cogitator import paths as cpaths

    probe_port = 8798
    check(not cpaths.port_in_use(probe_port), f"{probe_port} 当前空闲")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", probe_port))
    srv.listen(1)
    try:
        check(cpaths.port_in_use(probe_port), "已有监听时 port_in_use 为真（用 connect_ex，不受 SO_REUSEADDR 影响）")
        check(not cpaths.port_bindable(probe_port), "已被占用时 port_bindable 为假（Windows 上靠 SO_EXCLUSIVEADDRUSE）")
        picked = cpaths.pick_port(probe_port, tries=5)
        check(picked != probe_port, f"pick_port 会跳过被占用的端口（返回 {picked}）")
    finally:
        srv.close()
    check(cpaths.pick_port(probe_port, tries=3) == probe_port, "端口释放后又会被选中")

    bat = Path(__file__).resolve().parent.parent / "启动机魂修图台.bat"
    check(bat.is_file(), "启动脚本存在")
    if bat.is_file():
        raw = bat.read_bytes()
        crlf = raw.count(b"\r\n")
        bare_lf = raw.count(b"\n") - crlf
        check(crlf > 10, f"启动脚本是 CRLF 换行（{crlf} 处）")
        check(bare_lf == 0, f"没有裸 LF（{bare_lf} 处）—— cmd.exe 解析纯 LF 的 .bat 会出错")
        text = raw.decode("utf-8", "replace")
        check("cogitator" in text and "pause" in text, "脚本内容完整（会调用 cogitator 且失败时 pause）")
        check("goto" in text or "if errorlevel" in text, "包含错误分支处理")

    print()
    if FAIL:
        print(f"自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("算子层自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
