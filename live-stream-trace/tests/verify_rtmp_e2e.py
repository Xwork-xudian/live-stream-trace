#!/usr/bin/env python3
"""RTMP 端到端验证：用 ffmpeg 的 `-listen 1` 输出模式当 RTMP 服务端（可被读取）。

每个阶段起一个全新服务端（ffmpeg 的 listen 模式是一次性的），逐项验证：
  A. rtmp_trace.probe_stream       —— H.264 / 分辨率 / 帧率 探测
  B. rtmp_trace.record_sample      —— 录制样本
  C. scan_flv / build_checks / render_html —— FLV 结构、GOP、体检项、报告
  D. platforms/rtmp.py ADAPTER.resolve     —— 统一项目适配器
  E. ADAPTER.capture_sample                —— 适配器采样 + 深度音画同步
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[2]                      # 项目根
LST = WS / "live-stream-trace"
RTMP_PROJ = WS / "rtmp-h264-stream-trace"
MEDIA = Path(__file__).resolve().parent / "media" / "test_d300.flv"
FFMPEG = "/opt/ffmpeg/bin/ffmpeg"
PORT = 19371
URL = f"rtmp://127.0.0.1:{PORT}/live/test"
WORK = Path(__file__).resolve().parent / ".out" / "rtmp_e2e"
WORK.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(RTMP_PROJ))
sys.path.insert(0, str(LST / "core"))
sys.path.insert(0, str(LST / "platforms"))
import rtmp_trace          # noqa: E402
from rtmp import ADAPTER as RTMP_ADAPTER  # noqa: E402

fails: list[str] = []


def check(name, ok, extra=""):
    print(f"  {'✅' if ok else '❌'} {name} {extra}")
    if not ok:
        fails.append(name)


class Server:
    """以「可被读取」的方式提供 RTMP 流（一次性，用完即弃）。"""

    def __init__(self, seconds: int = 40):
        self.seconds = seconds
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", str(self.seconds),
             "-i", str(MEDIA), "-c", "copy", "-f", "flv", "-listen", "1", URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.5)
        return self

    def __exit__(self, *_):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        time.sleep(0.4)


print("=" * 92)
print(" RTMP 端到端验证（本地 ffmpeg 作为 RTMP 服务端）")
print("=" * 92)
print(f" URL={URL}  源={MEDIA.name}")

# ── A. 探测 ──
print("\n[A] probe_stream：H.264 / 分辨率 / 帧率")
with Server():
    probe = rtmp_trace.probe_stream(URL, timeout=20)
check("探测成功", probe.get("ok"), f"耗时 {probe.get('elapsed_ms')} ms")
v = probe.get("video") or {}
a = probe.get("audio") or {}
check("视频 H.264", (v.get("codec") or "").lower() == "h264", f"codec={v.get('codec')}")
check("分辨率 640x360", (v.get("width"), v.get("height")) == (640, 360),
      f"{v.get('width')}x{v.get('height')}")
check("帧率 25", abs((v.get("fps") or 0) - 25) < 0.5, f"fps={v.get('fps')}")
check("音频 aac", (a.get("codec") or "").lower() == "aac", f"codec={a.get('codec')}")

# ── B. 录制样本 ──
print("\n[B] record_sample：ffmpeg -c copy 录制")
sample = WORK / "rtmp_sample.flv"
with Server():
    size = rtmp_trace.record_sample(URL, sample, seconds=12, timeout=20)
check("录制到样本", size > 0, f"{size} 字节 → {sample.name}")

# ── C. FLV 结构 / GOP / 体检 / 报告 ──
print("\n[C] scan_flv / build_checks / render_html")
scan = rtmp_trace.scan_flv(sample.read_bytes()) if size else {}
check("解析出视频帧", (scan.get("video_frames") or 0) > 100, f"frames={scan.get('video_frames')}")
check("解析出音轨帧", (scan.get("audio_frames") or 0) > 100, f"frames={scan.get('audio_frames')}")
check("检出关键帧", (scan.get("keyframes") or 0) >= 3, f"keyframes={scan.get('keyframes')}")
gop = scan.get("gop_median_ms") or 0
check("GOP 中位 ≈2000ms（x264 -g50@25fps）", abs(gop - 2000) <= 200, f"gop={gop} ms")
check("A/V 起始偏移有值", scan.get("av_start_offset_ms") is not None,
      f"offset={scan.get('av_start_offset_ms')} ms")
checks = rtmp_trace.build_checks(probe, scan, size, 12)
names = {c["name"]: c for c in checks}
check("体检：H.264 通过", names.get("视频编码 H.264", {}).get("ok") is True)
check("体检：GOP 通过", names.get("关键帧间隔(GOP)", {}).get("ok") is True)
verdict, level = rtmp_trace.overall_verdict(checks)
check("总体结论非阻塞性", "阻塞" not in verdict, f"→ {verdict}（{level}）")
html = rtmp_trace.render_html("TEST", URL, probe, scan, checks, {}, "rtmp_sample.flv", size, 12, WORK)
check("HTML 报告含编码与结论", ("h264" in html) and (verdict in html) and ("RTMP" in html))
txt = rtmp_trace.render_text("TEST", URL, probe, scan, checks, {}, size, 12)
check("文本报告含体检项", "视频编码 H.264" in txt)

# ── D. 统一项目适配器 resolve ──
print("\n[D] platforms/rtmp.py → ADAPTER.resolve()")
with Server():
    room = RTMP_ADAPTER.resolve(URL)
check("状态=直播中", room.status == "直播中", f"status={room.status}")
check("房间号=host/app/stream", room.room_id == "127.0.0.1/live/test", f"room_id={room.room_id}")
check("rmtmp 档位 1 个且可用", len(room.rtmp) == 1 and room.rtmp[0].ok,
      f"label={room.rtmp[0].label if room.rtmp else '—'}")
check("识别为 H.264", (room.rtmp[0].codec or "").lower() == "h264" if room.rtmp else False)
check("标记不可网页播放", room.rtmp[0].playable is False if room.rtmp else False)
check("指标含分辨率/帧率", str(room.metrics.get("resolution")) == "640x360",
      f"{room.metrics.get('resolution')} @ {room.metrics.get('fps')}fps, is_h264={room.metrics.get('is_h264')}")

# ── E. 适配器采样 + 深度音画同步 ──
print("\n[E] ADAPTER.capture_sample + 时间戳级/内容级音画同步")
dest = WORK / "adapter_sample.flv"
with Server():
    got = RTMP_ADAPTER.capture_sample(room.rtmp[0], dest, seconds=12, room_id=room.room_id)
check("适配器录制成功", got > 0, f"{got} 字节")
deep = rtmp_trace.deep_av_sync(dest, 12) if got else {}
tl = deep.get("timestamp_level") or {}
check("时间戳级音画同步可用", tl.get("stream_offset_ms") is not None,
      f"stream_offset={tl.get('stream_offset_ms')} ms, decode={tl.get('decode_mean_ms')} ms")
check("rttmp_trace 检测到统一 core", rtmp_trace.unified_available())

print("\n" + "=" * 92)
print("❌ 失败项: " + ", ".join(fails) if fails else "✅ RTMP 端到端验证全部通过")
print("=" * 92)
sys.exit(1 if fails else 0)
