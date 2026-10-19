#!/usr/bin/env bash
# 打开最新（或指定）的 HTML 报告
# 用法: ./open_report.sh [reports/unified_report_xxx/index.html]
# WSL 环境下自动转换为 Windows 路径并用 Windows 默认浏览器打开。
set -euo pipefail
cd "$(dirname "$0")"

target="${1:-}"
if [ -z "$target" ]; then
  target=$(ls -1dt reports/unified_report_*/index.html 2>/dev/null | head -1 || true)
fi
if [ -z "$target" ] || [ ! -f "$target" ]; then
  echo "未找到报告。请先运行: ./run.sh <直播间号>" >&2
  exit 1
fi

echo "报告: $target"
if command -v wslpath >/dev/null 2>&1 && grep -qi microsoft /proc/version 2>/dev/null; then
  win=$(wslpath -w "$target")
  echo "Windows 路径: $win"
  opened=0
  if command -v explorer.exe >/dev/null 2>&1; then
    explorer.exe "$win" >/dev/null 2>&1 || true && opened=1
  elif [ -x /mnt/c/Windows/explorer.exe ]; then
    /mnt/c/Windows/explorer.exe "$win" >/dev/null 2>&1 || true && opened=1
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$target" >/dev/null 2>&1 || true && opened=1
  fi
  [ "$opened" = "1" ] && echo "已尝试用 Windows 默认浏览器打开" \
    || echo "将上面的 Windows 路径粘贴到资源管理器/浏览器地址栏即可打开"
else
  xdg-open "$target" 2>/dev/null || python3 -m webbrowser "file://$PWD/$target" || true
fi
