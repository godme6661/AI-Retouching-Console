#!/usr/bin/env bash
# 机魂修图台 —— macOS / Linux 启动脚本（Windows 用 启动机魂修图台.bat）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8760}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.10+。" >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "启动： http://127.0.0.1:${PORT}/"
exec python -m cogitator --port "${PORT}"
