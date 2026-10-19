#!/usr/bin/env bash
# 统一项目 live-stream-trace 的 RTMP 全链路端到端验证。
# ffmpeg 的 listen 模式一次只服务一个客户端，而 CLI 会建立两次连接（探测 + 采样），
# 因此这里用一个「循环重启的服务端」提供流，由适配器的录制重试兜住启动间隙。
set -u
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FF="${FFMPEG:-ffmpeg}"   # 默认用 PATH 里的 ffmpeg；也可 FFMPEG=/path/to/ffmpeg 覆盖
MEDIA="$WS/live-stream-trace/tests/media/test_d300.flv"
PORT=19385
URL="rtmp://127.0.0.1:$PORT/live/cli"
STOP=/tmp/rtmp_loop.stop
rm -f "$STOP"

( while [ ! -f "$STOP" ]; do
    "$FF" -hide_banner -loglevel error -re -stream_loop 3 -i "$MEDIA" \
      -c copy -f flv -listen 1 "$URL" >/dev/null 2>&1
  done ) &
LOOP=$!
sleep 3

cd "$WS/live-stream-trace"
# 合成样本（testsrc，画面里没有人脸）→ 选段必然「整段未检出人脸」；
# 用 --measure-timeout 60 把「测不出来持续测」限制成 1 分钟，避免这个冒烟脚本空跑满 5 分钟。
./.venv/bin/python live_trace.py "$URL" --format both --capture-seconds 12 \
  --lip-sync-seconds 12 --max-bytes 3000000 --measure-timeout 60 2>&1 | tail -30

touch "$STOP"
kill "$LOOP" 2>/dev/null
wait "$LOOP" 2>/dev/null

LATEST=$(ls -dt "$WS/live-stream-trace/reports"/unified_report_* 2>/dev/null | head -1)
echo "---- 报告: $LATEST ----"
for kw in "RTMP 源流" "音画同步（时间戳级 + 内容级）" "人脸嘴型音画同步" "源流（RTMP·H.264）"; do
  if grep -q "$kw" "$LATEST/index.html" 2>/dev/null; then echo "  ✅ HTML 含: $kw"; else echo "  ❌ HTML 缺: $kw"; fi
done
if grep -q "人脸嘴型音画同步" "$LATEST/report.txt" 2>/dev/null; then
  echo "  ✅ 文本报告含: 人脸嘴型音画同步"
else
  echo "  ❌ 文本报告缺: 人脸嘴型音画同步"
fi
