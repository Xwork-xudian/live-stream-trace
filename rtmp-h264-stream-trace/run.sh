#!/usr/bin/env bash
# rtmp-h264-stream-trace 快速运行脚本
#   例: ./run.sh rtmp://127.0.0.1:1935/live/test --seconds 20
#   例: ./run.sh "rtmps://cdn.example.com/app/key?token=xxx" --no-record
set -euo pipefail
cd "$(dirname "$0")"

# 解释器选择：本项目 venv → 同级统一项目 venv（含 numpy/opencv，可做内容级嘴型同步）→ 现场创建 → 系统 python3
PY=""
if [ -x .venv/bin/python ]; then
  PY=./.venv/bin/python
elif [ -x ../live-stream-trace/.venv/bin/python ]; then
  PY=../live-stream-trace/.venv/bin/python
  echo "[i] 复用 ../live-stream-trace/.venv（含 numpy/opencv）" >&2
else
  echo "未找到虚拟环境，正在创建并安装依赖…" >&2
  python3 -m venv .venv >/dev/null 2>&1 || true
  if [ -x .venv/bin/python ]; then
    ./.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    # PyPI 直连常被限速：优先国内镜像，失败再回退默认源
    ./.venv/bin/python -m pip install -q -i https://mirrors.aliyun.com/pypi/simple/ \
      --trusted-host mirrors.aliyun.com -r requirements.txt >/dev/null 2>&1 || \
    ./.venv/bin/python -m pip install -q -r requirements.txt >/dev/null 2>&1 || true
    PY=./.venv/bin/python
  else
    echo "[!] 无法创建 venv，改用系统 python3（核心功能仅依赖标准库 + ffmpeg）" >&2
    PY=python3
  fi
fi

if [ $# -lt 1 ]; then
  echo "用法: $0 <rtmp://host[:port]/app/stream> [rtmp_trace.py 的其他参数...]" >&2
  echo "      $0 rtmp://127.0.0.1:1935/live/test --seconds 20" >&2
  exit 2
fi
exec "$PY" rtmp_trace.py "$@"
