#!/usr/bin/env bash
# 启动 live-stream-trace 网页系统（一框输入 → 生成报告 → 历史可查）
#   用法: ./start_web.sh [端口]    （默认 8899）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "未找到 .venv，请先：" >&2
  echo "  python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt" >&2
  exit 2
fi

PORT="${1:-8899}"
echo "网页系统: http://localhost:${PORT}/"
exec ./.venv/bin/python core/web_ui.py "$PORT"
