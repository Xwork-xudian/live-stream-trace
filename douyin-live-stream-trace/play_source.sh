#!/usr/bin/env bash
# 用 ffplay 播放最新采集会话的「源流」地址（未转码，不经云端二压）
# 用法: ./play_source.sh [--backup] [房间号]
#   --backup   使用备线路源流地址
#   房间号     可选，默认取 captures/ 下最新一份 stream_url_raw
set -euo pipefail
cd "$(dirname "$0")"

FFPLAY="${FFPLAY:-$(command -v ffplay || echo ffplay)}"
FFPROBE="${FFPROBE:-$(command -v ffprobe || echo ffprobe)}"

BACKUP=0
ROOM=""
for a in "$@"; do
  case "$a" in
    --backup) BACKUP=1 ;;
    *) ROOM="$a" ;;
  esac
done

# 选取数据文件
if [ -n "$ROOM" ]; then
  raw=$(ls -1t captures/stream_url_raw_*${ROOM}*.json 2>/dev/null | head -1 || true)
else
  raw=$(ls -1t captures/stream_url_raw_*.json 2>/dev/null | head -1 || true)
fi
if [ -z "$raw" ]; then
  echo "captures/ 下没有 stream_url_raw_*.json，请先运行: ./run.sh <直播间号>" >&2
  exit 1
fi
echo "数据源: $raw"

# 构造源流地址（ao 纯音频地址去掉 only_audio / eupf_relay_mode）
URL=$(python3 - "$raw" "$BACKUP" <<'PY'
import json, sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
raw, backup = sys.argv[1], sys.argv[2] == '1'
su = json.load(open(raw))['stream_url_obj']
sd = json.loads(su['live_core_sdk_data']['pull_data']['stream_data'])
ao = sd['data'].get('ao', {})
line = 'backup' if backup else 'main'
u = ao.get(line, {}).get('flv', '')
if not u:
    print('NO_SOURCE', end='')
    raise SystemExit
p = urlparse(u)
q = {k: v[0] for k, v in parse_qs(p.query).items()}
for k in ('only_audio', 'eupf_relay_mode'):
    q.pop(k, None)
print(urlunparse(p._replace(query=urlencode(q))), end='')
PY
)
if [ "$URL" = "NO_SOURCE" ]; then
  echo "该会话没有 ao 源流地址（房间可能未开播）" >&2
  exit 1
fi
echo "源流地址: $URL"
echo "  （复制给 Windows 播放器/VLC 也可以）"

echo '== 编码探测 =='
"$FFPROBE" -v error -show_entries stream=codec_name,profile -of csv=p=0 \
  -analyzeduration 3M -probesize 3M "$URL" 2>/dev/null | head -3 \
  || echo "(探测失败：流可能已下播或地址过期，仍尝试播放)"

echo "启动 ffplay（q 或 Ctrl+C 退出）…"
exec "$FFPLAY" -fflags nobuffer -flags low_delay -framedrop "$URL"
