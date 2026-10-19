#!/usr/bin/env bash
# 启动本地 HTTP 服务并打开报告（浏览器直接拉流播放的兜底方案）
# 用法: ./serve_report.sh [端口] [报告index.html路径]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8899}"
target="${2:-}"
if [ -z "$target" ]; then
  target=$(ls -1dt reports/unified_report_*/index.html 2>/dev/null | head -1 || true)
fi
if [ -z "$target" ] || [ ! -f "$target" ]; then
  echo "未找到报告。请先运行 ./run.sh <直播间号>" >&2
  exit 1
fi

url="http://localhost:$PORT/$target"
echo "报告: $target"

# 端口空闲则启动服务（绑定 0.0.0.0 以便 WSL 端口转发到 Windows）
if python3 -c "import socket,sys
s=socket.socket(); s.settimeout(1)
rc=s.connect_ex(('127.0.0.1',$PORT)); s.close()
sys.exit(0 if rc!=0 else 1)"; then
  nohup python3 -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1 &
  sleep 1
  echo "✅ 本地服务已启动: http://localhost:$PORT （后台运行，停止: pkill -f 'http.server $PORT'）"
else
  echo "端口 $PORT 已有服务在运行，直接打开页面"
fi
echo "播放地址: $url"

# 打开浏览器
if command -v wslpath >/dev/null 2>&1 && grep -qi microsoft /proc/version 2>/dev/null; then
  if command -v explorer.exe >/dev/null 2>&1; then
    explorer.exe "$url" >/dev/null 2>&1 || true
  elif [ -x /mnt/c/Windows/explorer.exe ]; then
    /mnt/c/Windows/explorer.exe "$url" >/dev/null 2>&1 || true
  fi
  echo "已尝试用 Windows 默认浏览器打开（如未弹出，请手动访问上面的地址）"
else
  xdg-open "$url" 2>/dev/null || python3 -m webbrowser "$url" || true
fi
