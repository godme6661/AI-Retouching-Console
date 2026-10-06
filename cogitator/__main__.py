"""启动入口：python -m cogitator [--port N] [--no-browser] [--open 图片路径]

只监听 127.0.0.1。启动后自动打开浏览器。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__
from .paths import enable_utf8_stdio, pick_port


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdio()
    ap = argparse.ArgumentParser(prog="cogitator", description=f"{APP_NAME} —— AI 操控的轻量修图台")
    ap.add_argument("--port", type=int, default=0, help="监听端口（默认自动挑一个空闲端口）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--open", nargs="*", default=[], help="启动后直接开这些图片为新工程")
    args = ap.parse_args(argv)

    explicit_port = bool(args.port)
    port = args.port or pick_port()
    url = f"http://{args.host}:{port}/"

    from . import server  # 延迟导入：只有真要起服务时才加载图像/网络依赖

    if args.open:
        try:
            from .document import Project

            server.STATE.project = Project.create_from_images(args.open, server.STATE.settings)
            print(f"[{APP_NAME}] 已打开工程：{server.STATE.project.dir}")
        except Exception as e:
            print(f"[{APP_NAME}] 打开图片失败：{e}")

    if not args.no_browser:
        def _open() -> None:
            time.sleep(0.9)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()

    print(f"[{APP_NAME}] v{__version__} 服务地址：{url}")
    print(f"[{APP_NAME}] 按 Ctrl+C 可退出（界面上的「退出」按钮同样有效）")

    import uvicorn

    # 构造 Server 实例而不是 uvicorn.run()：这样 /api/shutdown 能拿到句柄，
    # 用 should_exit 做优雅退出（先发完响应再退出），不再与响应抢跑
    attempts = 0
    while True:
        config = uvicorn.Config(server.app, host=args.host, port=port,
                                log_level="warning", access_log=False)
        srv = uvicorn.Server(config)
        server.set_uvicorn_server(srv)
        try:
            srv.run()
            break
        except KeyboardInterrupt:
            print(f"\n[{APP_NAME}] 已退出。")
            break
        except (OSError, SystemExit) as e:
            # 端口可能在这一瞬间被别人抢走（或探测在 Windows 上被 SO_REUSEADDR 误导）。
            # 但**用户显式指定端口时不再静默换端口**——悄悄跑到别的端口会掩盖问题
            # （曾经让指向 8760 的检查脚本连不上，却不知道服务其实在 8761）。
            attempts += 1
            busy = ("10048" in str(e) or getattr(e, "errno", None) in (48, 98, 10048)
                    or isinstance(e, SystemExit))
            if explicit_port:
                print(f"\n[{APP_NAME}] 无法绑定指定端口 {port}（可能已被占用）。")
                print(f"[{APP_NAME}] 请换一个端口，例如：python -m cogitator --port {port + 7}")
                print(f"[{APP_NAME}] 或不指定 --port，让程序自动挑选可用端口。")
                return 1
            if not busy or attempts > 5:
                print(f"\n[{APP_NAME}] 启动失败：{e}")
                print(f"[{APP_NAME}] 若提示端口被占用，请换一个端口重试，例如："
                      f"python -m cogitator --port {port + 7}")
                return 1
            print(f"[{APP_NAME}] 端口 {port} 被占用，换一个试试…")
            port = pick_port(port + 1)
            url = f"http://{args.host}:{port}/"
            print(f"[{APP_NAME}] 新地址：{url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
