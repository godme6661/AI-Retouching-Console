"""路径解析：所有落盘位置集中在这里，便于迁移与清理。"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

# <root>/cogitator/paths.py -> <root>
APP_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = APP_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PROJECTS_DIR = APP_ROOT / "projects"
WEB_DIR = Path(__file__).resolve().parent / "web"
LOG_DIR = APP_ROOT / "logs"

DEFAULT_PORT = 8760


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, PROJECTS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def safe_name(name: str, fallback: str = "project") -> str:
    """把用户输入/文件名收敛成安全的目录名（去掉路径分隔与保留字符）。"""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        name = fallback
    if len(name) > 60:
        name = name[:60]
    return name


def unique_dir(parent: Path, name: str) -> Path:
    """在 parent 下取一个不冲突的目录（name、name-2、name-3 …）。"""
    base = safe_name(name)
    candidate = parent / base
    i = 2
    while candidate.exists():
        candidate = parent / f"{base}-{i}"
        i += 1
    return candidate


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口上是否已有服务在监听。

    用 connect_ex 判断，**不用 bind 试探**：Windows 上设了 SO_REUSEADDR 的 socket
    即使端口已被占用也能 bind 成功（实测确认），据此挑端口会挑到占用端口，
    然后 uvicorn 才报 WinError 10048 绑定失败。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def port_bindable(port: int, host: str = "127.0.0.1") -> bool:
    """能否真正独占绑定该端口（Windows 上用 SO_EXCLUSIVEADDRUSE 才准）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows 专用，语义正确
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred: int = DEFAULT_PORT, tries: int = 40, host: str = "127.0.0.1") -> int:
    """从 preferred 起找一个真正可用的端口（本机回环）。"""
    for offset in range(tries):
        port = preferred + offset
        if port_in_use(port, host):
            continue
        if port_bindable(port, host):
            return port
    raise RuntimeError(f"在 {preferred}~{preferred + tries} 范围内没有可用端口")


def enable_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文日志会乱码/报错。

    同时打开行缓冲：否则输出被重定向到文件时（例如用启动脚本收集日志）
    会一直攒在缓冲区里，看不到服务地址与报错。
    """
    for stream in ("stdout", "stderr"):
        s = getattr(__import__("sys"), stream, None)
        try:
            s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:
            pass


os.environ.setdefault("PYTHONUTF8", "1")
