#!/usr/bin/env bash
# 统一入口：多平台直播流分析
# 用法:
#   ./run.sh <直播间链接|房间号> [额外参数...]
#   ./run.sh https://live.bilibili.com/5440 --format both
#   ./run.sh "https://www.xiaohongshu.com/livestream/xxx/123456" --no-av-sync
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "未找到虚拟环境，正在创建并安装依赖…" >&2
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

if [ $# -eq 0 ]; then
  echo "用法: $0 <直播间链接|房间号> [live_trace.py 参数...]" >&2
  echo "示例: $0 https://live.bilibili.com/5440" >&2
  echo "      $0 \"https://www.xiaohongshu.com/livestream/xxx/123456\" --format html" >&2
  exit 2
fi

exec ./.venv/bin/python live_trace.py "$@"
