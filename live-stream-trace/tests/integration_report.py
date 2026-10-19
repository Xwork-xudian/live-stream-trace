#!/usr/bin/env python3
"""集成测试：验证「人脸嘴型」板块进入统一报告的 HTML / 文本，并输出联合诊断。"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "platforms")]

from av_sync_detector import run_av_sync_analysis      # noqa: E402
from lip_sync_detector import run_lip_sync_analysis    # noqa: E402
import report as core_report                           # noqa: E402

MEDIA = Path(__file__).resolve().parent / "media" / "test_d300.flv"
ROI = [0.40625, 0.55556, 0.09375, 0.083333]
TS = "20261013_235959"
ROOM = "TESTROOM"

work = Path(__file__).resolve().parent / ".out" / "int_captures"
out = Path(__file__).resolve().parent / ".out" / "int_report"
shutil.rmtree(work, ignore_errors=True)
shutil.rmtree(out, ignore_errors=True)
work.mkdir(parents=True)
out.mkdir(parents=True)

# 1) 证据：时间戳级 + 内容级 + 归一化 payload
run_av_sync_analysis(MEDIA, work, TS, room_id=ROOM, decode_check=False)
run_lip_sync_analysis(MEDIA, work, TS, room_id=ROOM, roi=ROI, seconds=20)
# 1b) 合成「健康片段选段」证据（真实选段耗时且依赖人脸，这里给确定性样本）
(work / f"segment_scan_{TS}.json").write_text(json.dumps({
    "type": "segment_scan", "timestamp": TS, "source": str(MEDIA),
    "window_seconds": 6.0, "step_seconds": 1.5,
    "scan_meta": {"duration": 20.0, "width": 640, "height": 360,
                  "vcodec": "h264", "acodec": "aac", "fps": 25.0},
    "windows": [
        {"index": 0, "start": 0.0, "end": 6.0, "score": 42.0, "score_effective": 55.0,
         "face_cov": 0.6, "face_jitter_px": 21.3, "motion_activity": 0.31, "speech_activity": 0.2,
         "frontality": 0.4, "occlusion": 0.0, "r_mouth": 0.09, "r_control": 0.07,
         "mouth_specific": False},
        {"index": 1, "start": 6.0, "end": 12.0, "score": 88.0, "score_effective": 101.5,
         "face_cov": 1.0, "face_jitter_px": 4.2, "motion_activity": 0.62, "speech_activity": 0.41,
         "frontality": 0.81, "occlusion": 0.0, "r_mouth": 0.42, "r_control": 0.13,
         "mouth_specific": True},
        {"index": 2, "start": 12.0, "end": 18.0, "score": 51.0, "score_effective": 66.0,
         "face_cov": 0.8, "face_jitter_px": 12.0, "motion_activity": 0.35, "speech_activity": 0.25,
         "frontality": 0.55, "occlusion": 0.02, "r_mouth": 0.15, "r_control": 0.11,
         "mouth_specific": False},
    ],
    "best": {"start": 6.0, "end": 12.0, "wins": 1,
             "best_window": {"index": 1, "start": 6.0, "end": 12.0, "score": 88.0,
                             "score_effective": 101.5, "face_cov": 1.0, "face_jitter_px": 4.2,
                             "motion_activity": 0.62, "speech_activity": 0.41, "frontality": 0.81,
                             "occlusion": 0.0, "r_mouth": 0.42, "r_control": 0.13,
                             "mouth_specific": True}},
    "error": None,
    "clip": "segment_TEST_clip.mp4",
    "crops": ["segment_TEST_face_1.png", "segment_TEST_mouth_1.png"],
    "measurement": {"verdict": "内容级同步良好", "verdict_level": "高",
                    "content_offset_ms": -41.5, "ci_ms": 3.2, "window_agreement": 0.83,
                    "resolution_ms": 5.0, "direction": "音频滞后画面",
                    "r": 0.31, "r_robust": 0.36, "mouth_r": 0.42, "control_r": 0.13,
                    "specific": True, "face_coverage": 1.0, "talking": True, "media_span_s": 6.0,
                    "peak_lag_ms": -40.0, "detail": "片段测量说明：确定性测试样本。",
                    "lag_curve": [[-200.0, -0.10], [-100.0, 0.05], [0.0, 0.20],
                                  [-40.0, 0.36], [100.0, 0.02], [200.0, -0.08]]},
}, ensure_ascii=False, indent=2), encoding="utf-8")

(work / f"live_stream_info_{TS}.json").write_text(json.dumps({
    "type": "live_stream_info", "platform": "demo", "platform_name": "演示平台",
    "platform_color": "#00AAFF", "room_id": ROOM, "status": "直播中",
    "title": "内容级音画同步集成测试", "anchor": "测试主播", "timestamp": TS,
    "tiers": [], "rtmp": [], "metrics": {"viewers": 1}, "push": {},
}, ensure_ascii=False, indent=2), encoding="utf-8")

# 2) 报告流水线（与 live_trace.py 一致）
files_data = core_report.merge_results_into_files_data(
    core_report.scan_captures(work), core_report.discover_json_results(work))
sessions = core_report.correlate_by_time(files_data, tolerance_seconds=600)
sessions = core_report.enrich_sessions(sessions)
sessions = core_report.filter_sessions_by_room_id(sessions, ROOM) or sessions
html_path, txt_path = out / "index.html", out / "report.txt"
core_report.generate_html_report(sessions, html_path)
core_report.generate_text_report(sessions, txt_path)

html = html_path.read_text(encoding="utf-8")
txt = txt_path.read_text(encoding="utf-8")
sess = sessions[0] if sessions else {}
lip = sess.get("lip_sync_analysis") or {}
avs = sess.get("av_sync_analysis") or {}

checks = [
    ("会话已挂载 lip_sync_analysis", bool(lip)),
    ("会话同时挂载 av_sync_analysis", bool(avs)),
    ("HTML 含「人脸嘴型音画同步（内容级）」", "人脸嘴型音画同步（内容级）" in html),
    ("HTML 含「联合诊断」", "联合诊断" in html),
    ("HTML 含「🎯 健康片段内容」", "健康片段内容" in html),
    ("HTML 含「🔬 检测过程」", "检测过程" in html),
    ("HTML 含逐窗打分明细", "逐窗打分" in html),
    ("HTML 含相关-滞后曲线（内联 SVG）", "相关-滞后曲线" in html and "<polyline" in html),
    ("HTML 含片段偏移 / CI / 特异性", "置信区间 CI" in html and "特异性" in html),
    ("HTML 含片段回放小播放器", "segment_TEST_clip.mp4" in html and "<video" in html),
    ("HTML 已移除独立「健康片段（自动选段）」区块", "健康片段（自动选段）" not in html),
    ("HTML 含「时间戳自洽、内容错位」结论", "推流前管线" in html),
    ("文本报告含内容级板块", "人脸嘴型音画同步（内容级）" in txt),
    ("文本报告含联合诊断", "联合诊断" in txt),
    ("报告可正常写出（>10KB）", html_path.stat().st_size > 10000),
]
print("=" * 88)
print(" 集成测试：嘴型（内容级）板块进入统一报告")
print("=" * 88)
bad = 0
for name, ok in checks:
    print(f"  {'✅' if ok else '❌'} {name}")
    bad += 0 if ok else 1
print("-" * 88)
print("内容级:", (lip.get("perceptual") or {}).get("content_offset_ms"),
      "ms | 时间戳级:", (avs.get("perceptual") or {}).get("stream_offset_ms"), "ms")
from lip_sync_detector import diagnose_vs_stream_offset  # noqa: E402
print("联合诊断:", diagnose_vs_stream_offset(lip, avs))
print("报告:", html_path)
print("段选（文本报告节选）:")
for line in txt.splitlines():
    if "嘴型" in line or "联合诊断" in line or "健康片段" in line:
        print("   ", line.strip())
print("-" * 88)
print("❌ 失败" if bad else "✅ 全部通过")
sys.exit(1 if bad else 0)
