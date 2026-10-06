"""「退出」链路的自动化回归（自己起实例，不打扰用户正在用的那个）。

覆盖：
  1. /api/shutdown 返回 200 且声明 graceful
  2. 进程在数秒内真的退出、端口释放（旧实现靠 os._exit 与响应抢跑，这里钉住后果）
  3. 服务退出后再调用该接口是"连不上"而不是挂死
  4. 前端静态守卫：不使用浏览器原生 confirm()（它可能被静默阻止 → 按钮点了没反应）
  5. /web/* 带 no-store，避免浏览器用旧 JS 造成"点了没反应"的幽灵问题
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAIL: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAIL.append(label)


def free_port(start: int = 8830) -> int:
    for p in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("没有空闲端口")


def get(url: str, timeout: float = 5.0) -> tuple[int, bytes, dict]:
    """返回 (状态码, 正文, 响应头)。

    响应头统一转成**小写键**：HTTP 头部本身大小写不敏感，而 Starlette 发出来的是小写，
    urllib 给的字典却区分大小写——曾因此把"已经有头"误判成"没有头"。
    """
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}


def post(url: str, payload: dict | None = None, timeout: float = 10.0) -> tuple[int, bytes]:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"== 起一个临时实例（端口 {port}）==")
    proc = subprocess.Popen([sys.executable, "-m", "cogitator", "--port", str(port), "--no-browser"],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                st, body, _ = get(f"{base}/api/health", 2)
                if st == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.35)
        check(up, "临时实例已就绪")
        if not up:
            out = proc.stdout.read()[-500:] if proc.stdout else ""
            print("  实例输出：", out)
            return 1

        print("\n== 前端静态守卫（防止「点了没反应」「拖拽误叠加」复发）==")
        js = (ROOT / "cogitator" / "web" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "cogitator" / "web" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "cogitator" / "web" / "style.css").read_text(encoding="utf-8")
        code = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
        check(re.search(r"(?<![A-Za-z_])confirm\s*\(", code) is None,
              "app.js 不调用浏览器原生 confirm()（可能被静默阻止并返回 false）")
        check("function askConfirm" in code, "改用应用内确认框 askConfirm")
        check("无法连接本地服务" in code, "api() 捕获 fetch 异常，服务断开也有可读反馈")
        check(html.count('draggable="false"') >= 2,
              f"预览图与原图都标记为不可拖拽（{html.count('draggable=\"false\"')} 处）")
        check("-webkit-user-drag" in css, "CSS 也关掉了原生图片拖拽")
        check("__internalDrag" in code, "drop 处理器区分系统拖放与页面内拖拽（否则成品会被当素材叠加）")
        check("text/uri-list" in code, "drop 只接受真正的文件拖放")
        check("cmpBlink" not in code and "cmpBlink" not in html,
              "「闪烁」对比已按需求移除")
        # v1.4.7 新能力的静态守卫（真实浏览器由 tools/check_ui_extras.mjs 覆盖）
        check("cycleZoom" in code and 'id="btnZoom"' in html,
              "1:1 检查视图（缩放循环）已就位")
        check("panelGrip" in code and 'id="panelGrip"' in html and "--panel-w" in css,
              "右侧面板可拖宽（含窄窗口上下布局）")
        check("helpDlg" in code and 'id="helpDlg"' in html and "keys" in css,
              "快捷键帮助对话框已就位")
        check("cog_export_prefs" in code, "导出参数记忆（本地存储）已就位")
        check("ctrlKey" in code and "'z'" in code, "Ctrl+Z / Ctrl+Y 快捷键已绑定")
        check("LOCAL_MSGS" in code and "msg.pending" in css,
              "对话等待占位与失败回执已就位")
        # v1.4.8
        srv = (ROOT / "cogitator" / "server.py").read_text(encoding="utf-8")
        check("/project/view" in srv and "_FULL_CACHE" in srv,
              "输出像素视口端点（全分辨率渲染 + 按 rev 缓存）已就位")
        check("pixelOn" in code and 'id="btnPixel"' in html and "stage.pixel" in css,
              "输出像素查看模式（前端）已就位")
        check("OPEN_OPS" in code and "oprow fold" in code,
              "长管线折叠已就位（超过 8 步折叠中间段）")
        check("assetUsage" in code and "已用于" in code and "data-goto-layer" in code,
              "素材↔图层归属可视已就位")
        st, body, hdrs = get(f"{base}/web/app.js")
        check("no-store" in (hdrs.get("cache-control") or ""),
              f"/web/app.js 带禁缓存头（{hdrs.get('cache-control')}）")

        print("\n== 关闭链路 ==")
        t0 = time.time()
        st, body = post(f"{base}/api/shutdown")
        info = json.loads(body)
        check(st == 200 and info.get("ok") is True, f"shutdown 返回 200 且 ok（{body.decode()[:60]}）")
        check(info.get("graceful") is True, "走的是优雅退出（服务器句柄已注入）")
        code_exit = proc.wait(timeout=15)
        dt = time.time() - t0
        check(code_exit == 0, f"进程正常退出（exit={code_exit}，耗时 {dt:.1f}s）")
        check(dt < 8, f"退出足够快（{dt:.1f}s < 8s）")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            check(s.connect_ex(("127.0.0.1", port)) != 0, f"端口 {port} 已释放")
        try:
            post(f"{base}/api/shutdown", timeout=3)
            check(False, "服务已停后再次调用应连不上")
        except (urllib.error.URLError, ConnectionError, OSError):
            check(True, "服务已停后再次调用是「连不上」而不是挂死")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    print()
    if FAIL:
        print(f"退出链路自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("退出链路自检全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
