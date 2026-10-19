#!/usr/bin/env bash
# 音画同步「上传样本」入口端到端验证（依赖已启动的 web_ui 服务）
set -u
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LST="$WS/live-stream-trace"
PY="$LST/.venv/bin/python"
BASE=http://127.0.0.1:8899
cd "$LST"

echo "── 0) 重启服务 ──"
pkill -f "core/web_ui.py" 2>/dev/null
sleep 1
(nohup "$PY" core/web_ui.py 8899 > .ekko-tmp/web_ui.log 2>&1 &)
sleep 3
curl -s -m 8 -o /dev/null -w "  GET /        → %{http_code}\n" "$BASE/"

echo "── 1) 上传页渲染 ──"
curl -s -m 10 "$BASE/sync" | grep -o '音画同步检测 · 上传样本' | head -1 | sed 's/^/  ✅ /'

echo "── 2) POST /api/upload（multipart，等同浏览器 FormData）──"
UP=$(curl -s -m 60 -F "file=@tests/media/test_d300.flv;filename=test_d300.flv" \
      "$BASE/api/upload?name=test_d300.flv")
echo "$UP" | "$PY" -c "
import json,sys; d=json.load(sys.stdin)
print('  ✅ upload_id=%s | %s MB | 时长 %ss | %sx%s' % (d['upload_id'], d['size_mb'],
      (d.get('media') or {}).get('duration'), (d.get('media') or {}).get('width'), (d.get('media') or {}).get('height')))
print('     preview=%s' % d['preview_url'])
open('/tmp/uid.txt','w').write(d['upload_id'])
"
UPID=$(cat /tmp/uid.txt)

echo "── 3) POST /api/sync（提交检测任务）──"
JOB=$(curl -s -m 30 -X POST "$BASE/api/sync" -H 'Content-Type: application/json' \
  -d "{\"upload_id\":\"$UPID\",\"options\":{\"seconds\":16,\"roi\":\"0.40625,0.55556,0.09375,0.083333\",\"use_face\":false}}")
JID=$(echo "$JOB" | "$PY" -c "import json,sys; print((json.load(sys.stdin).get('job') or {}).get('id') or 'ERR')")
echo "  ✅ job_id=$JID"

echo "── 4) 轮询任务 ──"
for i in $(seq 1 60); do
  OUT=$(curl -s -m 15 "$BASE/api/jobs/$JID")
  ST=$(echo "$OUT" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('status'))")
  [ "$ST" = "done" ] || [ "$ST" = "failed" ] && break
  sleep 2
done
echo "$OUT" | "$PY" -c "
import json,sys; j=json.load(sys.stdin)
print('  ✅ status=%s rc=%s 耗时=%ss report=%s' % (j['status'], j['returncode'], j.get('elapsed_s'), j.get('report_url')))
print('     日志尾部:', ' | '.join((j.get('log_tail') or '').strip().splitlines()[-3:])[:220])
"

echo "── 5) GET /api/sync/summary/<job>（页面数据源）──"
curl -s -m 15 "$BASE/api/sync/summary/$JID" | "$PY" -c "
import json,sys; d=json.load(sys.stdin)
av, lp = d.get('av') or {}, d.get('lip') or {}
print('  时间戳级: %s | 流内偏移 %s ms' % (av.get('verdict'), av.get('stream_offset_ms')))
print('  内容级: %s | 偏移 %s ms | 分辨力 %s ms | CI± %s | 一致率 %s' % (
      lp.get('verdict'), lp.get('content_offset_ms'), lp.get('lag_resolution_ms'),
      lp.get('content_offset_ci_ms'), lp.get('window_agreement')))
print('  曲线点数: %d | preview=%s' % (len(lp.get('lag_curve') or []), d.get('preview_url')))
print('  联合诊断:', (d.get('diagnosis') or '—')[:150])
"

echo "── 6) Range 播放（拖动进度条必需）──"
curl -s -m 15 -o /dev/null -w "  无 Range → %{http_code} %{size_download}B\n" "$BASE/uploads/$UPID"
curl -s -m 15 -o /dev/null -w "  带 Range → %{http_code} %{size_download}B\n" -H 'Range: bytes=0-65535' "$BASE/uploads/$UPID"

echo "── 7) 历史合并（含上传样本报告）──"
curl -s -m 15 "$BASE/api/history" | "$PY" -c "
import json,sys; rs=json.load(sys.stdin).get('reports') or []
print('  共 %d 条；' % len(rs), [ (r['kind'], r['dir']) for r in rs[:3] ])
"
