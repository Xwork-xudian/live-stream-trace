#!/usr/bin/env bash
# 验证网页系统全链路：解析 → 建任务 → 轮询 → 报告 → 历史
set -u
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FF="${FFMPEG:-ffmpeg}"   # 默认用 PATH 里的 ffmpeg；也可 FFMPEG=/path/to/ffmpeg 覆盖
MEDIA="$WS/live-stream-trace/tests/media/test_d300.flv"
BASE=http://127.0.0.1:8899
PORT=19391
STOP=/tmp/rtmp_web.stop
rm -f "$STOP"

( while [ ! -f "$STOP" ]; do
    "$FF" -hide_banner -loglevel error -re -stream_loop 3 -i "$MEDIA" \
      -c copy -f flv -listen 1 "rtmp://127.0.0.1:$PORT/live/web" >/dev/null 2>&1
  done ) &
LOOP=$!
sleep 3

echo "── 1) 淘口令 POST /api/resolve ──"
curl -sS -m 30 -X POST "$BASE/api/resolve" -H 'Content-Type: application/json' \
  -d '{"text":"【淘宝】666:/🗝AbCd1234EfGh《 CZ356 示例主播的直播简直太火爆了，快来看!..."}' \
  | "$WS/live-stream-trace/.venv/bin/python" -c "
import json,sys; d=json.load(sys.stdin)
print('  ok=%s kind=%s 口令码=%s 主播线索=%s' % (d['ok'], d['kind'], d['password_code'], d['anchor_hint']))
print('  手动步骤 %d 条: %s' % (len(d.get('manual') or []), (d.get('manual') or [''])[0][:60]))
"

echo "── 2) 建分析任务 POST /api/analyze（RTMP）──"
JOB=$(curl -sS -m 30 -X POST "$BASE/api/analyze" -H 'Content-Type: application/json' \
  -d "{\"text\":\"rtmp://127.0.0.1:$PORT/live/web\",\"capture_seconds\":12,\"av_sync\":true,\"lip_sync\":false}" \
  | "$WS/live-stream-trace/.venv/bin/python" -c "import json,sys; d=json.load(sys.stdin); print(d.get('job',{}).get('id') or 'ERR:'+str(d)[:200])")
echo "  job_id=$JOB"
case "$JOB" in ERR*|"") touch "$STOP"; kill "$LOOP" 2>/dev/null; exit 1;; esac

echo "── 3) 轮询 /api/jobs/$JOB ──"
for i in $(seq 1 60); do
  OUT=$(curl -sS -m 15 "$BASE/api/jobs/$JOB")
  ST=$(printf '%s' "$OUT" | "$WS/live-stream-trace/.venv/bin/python" -c "import json,sys; print(json.load(sys.stdin).get('status'))")
  if [ "$ST" = "done" ] || [ "$ST" = "failed" ] || [ "$ST" = "interrupted" ]; then break; fi
  sleep 2
done
printf '%s' "$OUT" | "$WS/live-stream-trace/.venv/bin/python" -c "
import json,sys; j=json.load(sys.stdin)
print('  status=%s rc=%s 耗时=%ss' % (j.get('status'), j.get('returncode'), j.get('elapsed_s')))
print('  report_url=%s' % j.get('report_url'))
tail='\n'.join((j.get('log_tail') or '').strip().splitlines()[-6:])
print('  --- 日志尾部 ---'); print('  ' + tail.replace('\n','\n  '))
"

echo "── 4) 报告可访问性 ──"
RL=$(printf '%s' "$OUT" | "$WS/live-stream-trace/.venv/bin/python" -c "import json,sys; print(json.load(sys.stdin).get('report_url') or '')")
if [ -n "$RL" ]; then
  CODE=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "$BASE$RL")
  echo "  GET $RL → $CODE"
  curl -sS -m 15 "$BASE$RL" | grep -q "人脸嘴型音画同步" && echo "  ✅ 报告含音画同步板块" || echo "  ⚠️ 报告未含嘴型板块（本次以 --no-lip-sync 运行属预期）"
  curl -sS -m 15 "$BASE$RL" | grep -q "RTMP 源流" && echo "  ✅ 报告含 RTMP 源流" || echo "  ❌ 报告缺 RTMP 源流"
fi

echo "── 5) GET /api/history ──"
curl -sS -m 15 "$BASE/api/history" | "$WS/live-stream-trace/.venv/bin/python" -c "
import json,sys; d=json.load(sys.stdin)
rs=d.get('reports') or []
print('  历史报告 %d 份，最新 3 份：' % len(rs))
for r in rs[:3]:
    print('   · %s | %s | room=%s | %s | %s' % (r['time'], r.get('platform_name'), r.get('room_id'), (r.get('title') or '')[:24], r['report_url']))
"

touch "$STOP"; kill "$LOOP" 2>/dev/null
