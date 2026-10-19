#!/usr/bin/env bash
# douyin-live-stream-trace 快速运行脚本
# 用法: ./run.sh <直播间号> [额外参数...]
#   例: ./run.sh 376389454958 --format both --detect-dual-stream
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "未找到虚拟环境，正在创建并安装依赖…" >&2
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

ROOM="${1:-}"
if [ -z "$ROOM" ]; then
  echo "用法: $0 <直播间号> [analyze_room.py 的其他参数...]" >&2
  exit 2
fi
shift

exec ./.venv/bin/python analyze_room.py --room "$ROOM" --playwright "$@"
