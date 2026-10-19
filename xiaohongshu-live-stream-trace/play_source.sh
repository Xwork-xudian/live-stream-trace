#!/usr/bin/env bash
# 用 ffplay 播放最新一次采集的小红书「源流」（未转码）
# 用法: ./play_source.sh [--hls] [房间号]
set -euo pipefail
cd "$(dirname "$0")"

FFPLAY="${FFPLAY:-$(command -v ffplay || echo ffplay)}"
FFPROBE="${FFPROBE:-$(command -v ffprobe || echo ffprobe)}"

WANT_HLS=0
WANT_RTMP=0
ROOM=""
for a in "$@"; do
  case "$a" in
    --hls) WANT_HLS=1 ;;
    --rtmp) WANT_RTMP=1 ;;
    *) ROOM="$a" ;;
  esac
done

if [ -n "$ROOM" ]; then
  src=$(ls -1t captures/xhs_stream_*.json 2>/dev/null | while read -r f; do
          python3 -c "import json,sys; d=json.load(open('$f')); sys.exit(0 if str(d.get('room_id'))=='$ROOM' else 1)" 2>/dev/null && echo "$f" && break
        done)
else
  src=$(ls -1t captures/xhs_stream_*.json 2>/dev/null | head -1 || true)
fi
if [ -z "${src:-}" ]; then
  echo "未找到 captures/xhs_stream_*.json，请先运行: ./run.sh <直播间号>" >&2
  exit 1
fi
echo "数据源: $src"

KEY="source_flv"
[ "$WANT_HLS" = "1" ] && KEY="source_hls"
[ "$WANT_RTMP" = "1" ] && KEY="__rtmp__"
URL=$(python3 -c "
import json
d = json.load(open('$src'))
if '$KEY' == '__rtmp__':
    print((d.get('rtmp_source') or {}).get('url', ''))
else:
    print((d.get('stream_urls') or {}).get('$KEY', ''))")
if [ -z "$URL" ]; then
  echo "该会话没有 $KEY 地址" >&2
  exit 1
fi
echo "拉流地址($KEY): $URL"
echo "  （复制给 Windows VLC/播放器也可以）"

echo '== 编码探测 =='
"$FFPROBE" -v error -show_entries stream=codec_name,profile -of csv=p=0 \
  -analyzeduration 3M -probesize 3M "$URL" 2>/dev/null | head -3 \
  || echo "(探测失败：可能已下播或地址过期，仍尝试播放)"

echo "启动 ffplay（q 或 Ctrl+C 退出）…"
exec "$FFPLAY" -fflags nobuffer -flags low_delay -framedrop "$URL"
