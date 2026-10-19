#!/usr/bin/env bash
# 用「报告 + 流代理」本地服务打开最新报告（推荐：解决 CDN Referer 校验/短时地址问题）
# 用法: ./serve_report.sh [端口] [报告目录]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8899}"
REPORT="${2:-}"
PIDS=$(pgrep -f "python.*core/serve.py" || true)
if [ -n "$PIDS" ]; then
  echo "已有服务在运行（PID: $PIDS），如需重启请先: kill $PIDS"
fi
ARGS=("$PORT")
[ -n "$REPORT" ] && ARGS+=("$REPORT")

echo "启动报告服务（含流代理：自动补平台 Referer + 按需刷新地址）…"
setsid nohup ./.venv/bin/python core/serve.py "${ARGS[@]}" --open >/tmp/lst_serve.log 2>&1 < /dev/null &
sleep 3
tail -4 /tmp/lst_serve.log || true
URL="http://localhost:${PORT}/"
FILE=$(ls -1dt reports/unified_report_*/index.html 2>/dev/null | head -1 || true)
[ -n "${FILE:-}" ] && URL="http://localhost:${PORT}/$(dirname "$FILE" | xargs basename)/index.html"

if command -v wslpath >/dev/null 2>&1 && grep -qi microsoft /proc/version 2>/dev/null; then
  if command -v explorer.exe >/dev/null 2>&1; then explorer.exe "$URL" >/dev/null 2>&1 || true; fi
  echo "已在 Windows 打开: $URL"
else
  echo "浏览器访问: $URL"
fi
