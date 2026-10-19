#!/usr/bin/env bash
# 用 ffplay 播放最新一次采集的源流（支持 --rtmp / --tier 选择档位）
# 用法: ./play_source.sh [--rtmp] [--hls] [--tier <子串>] [房间号]
set -euo pipefail
cd "$(dirname "$0")"

FFPLAY="${FFPLAY:-$(command -v ffplay || echo ffplay)}"
FFPROBE="${FFPROBE:-$(command -v ffprobe || echo ffprobe)}"

WANT_RTMP=0; WANT_HLS=0; TIER=""; ROOM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rtmp) WANT_RTMP=1 ;;
    --hls) WANT_HLS=1 ;;
    --tier) shift; TIER="${1:-}" ;;
    *) ROOM="$1" ;;
  esac
  shift
done

src=$(ls -1t captures/live_stream_info_*.json 2>/dev/null | head -1 || true)
if [ -z "${src:-}" ]; then
  echo "未找到 captures/live_stream_info_*.json，请先运行: ./run.sh <直播间链接>" >&2
  exit 1
fi
echo "数据源: $src"

URL=$(python3 - "$src" "$WANT_RTMP" "$WANT_HLS" "$TIER" <<'PY'
import json, sys
path, want_rtmp, want_hls, tier = sys.argv[1], sys.argv[2] == '1', sys.argv[3] == '1', sys.argv[4]
d = json.load(open(path, encoding='utf-8'))
if want_rtmp:
    for r in d.get('rtmp') or []:
        if r.get('ok') and r.get('url'):
            print(r['url']); raise SystemExit
    for r in d.get('rtmp') or []:
        if r.get('url'):
            print(r['url']); raise SystemExit
    raise SystemExit
tiers = [t for t in (d.get('tiers') or []) if t.get('ok')]
if want_hls:
    tiers = [t for t in tiers if t.get('protocol') == 'hls'] or tiers
if tier:
    tiers = [t for t in tiers if tier in (t.get('label') or '')] or tiers

def score(t):
    s = 0
    label = t.get('label') or ''
    if '源流' in label or '原画' in label: s += 100
    if t.get('protocol') == 'flv': s += 50
    if t.get('playable', True): s += 20
    if '备线' in label: s -= 10
    return s
print(max(tiers, key=score)['url'] if tiers else '')
PY
)
if [ -z "$URL" ]; then
  echo "未找到可用档位（可加 --rtmp / --hls / --tier 原画）" >&2
  exit 1
fi
echo "地址: $URL"
echo "  （复制给 Windows VLC/播放器也可以）"

echo '== 编码探测 =='
"$FFPROBE" -v error -show_entries stream=codec_name,profile -of csv=p=0 \
  -analyzeduration 3M -probesize 3M "$URL" 2>/dev/null | head -3 \
  || echo "(探测失败：可能已下播或地址过期，仍尝试播放)"

echo "启动 ffplay（q 或 Ctrl+C 退出）…"
exec "$FFPLAY" -fflags nobuffer -flags low_delay -framedrop "$URL"
