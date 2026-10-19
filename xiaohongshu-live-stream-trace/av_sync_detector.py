#!/usr/bin/env python3
"""音画同步检测器（A/V Sync Detector）

双层测量，精确到毫秒：
  1. 标签级（无需解码）：解析 FLV tag 时间戳（含 AVC/增强 FLV-HEVC 的 CompositionTime），
     计算起始偏移、A/V 交错偏移序列与漂移速率、音频媒体时钟漂移（AAC 帧时长 vs mux 时间戳）、
     视频帧率/抖动/丢帧、交错突发、时间戳跳变。
  2. 解码级（地面真值，可选）：用 ffmpeg 实时解码并逐帧解析状态行的 A-V 差值，
     这是播放器真正感知的音画误差（ffmpeg 输出精度 1ms）。

用法:
    python av_sync_detector.py --file captures/xxx.flv
    python av_sync_detector.py --url "http://..." --duration 10 --decode-check
    python av_sync_detector.py --file xxx.flv --decode-check --room-id 123

输出: captures/av_sync_analysis_<ts>.json + captures/av_sync_report_<ts>.txt
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import OUTPUT_DIR, ensure_dirs

logger = logging.getLogger(__name__)

AAC_SAMPLES_PER_FRAME = 1024
SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                16000, 12000, 11025, 8000, 7350]
FFMPEG_CANDIDATES = ("/opt/ffmpeg/bin/ffmpeg",)

# 人眼-人耳感知门限（听觉超前/滞后不对称：超前更容易被察觉）
# 来源：ITU-R BT.1359-1（相对定时可察觉/可接受门限）、EBU R37（广播建议窗口）
PERCEPTUAL_ITU = {"audio_lead_detect_ms": 45, "audio_lag_detect_ms": -125,
                  "audio_lead_accept_ms": 90, "audio_lag_accept_ms": -185}
PERCEPTUAL_EBU_R37 = {"audio_lead_ms": 40, "audio_lag_ms": -60}


def resolve_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in FFMPEG_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# ─────────────────────────── FLV 解析 ───────────────────────────

def iter_flv_tags(data: bytes):
    """迭代 FLV 标签，返回 (type, ts_ms, size, body)。ts 含扩展字节。"""
    if len(data) < 13 or data[:3] != b"FLV":
        raise ValueError("不是有效的 FLV 数据（缺少 FLV 头）")
    has_audio = bool(data[4] & 0x04)
    has_video = bool(data[4] & 0x01)
    pos, n = 13, len(data)
    while pos + 11 <= n:
        ttype = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if size <= 0 or pos + 11 + size > n:
            break
        ts = int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24)
        body = data[pos + 11:pos + 11 + size]
        yield ttype, ts, size, body
        pos += 11 + size + 4


def parse_audio_specific_config(cfg: bytes) -> Optional[dict]:
    """解析 AAC AudioSpecificConfig → 采样率/声道。"""
    if len(cfg) < 2:
        return None
    b0, b1 = cfg[0], cfg[1]
    sf_idx = ((b0 & 0x07) << 1) | (b1 >> 7)
    sr = SAMPLE_RATES[sf_idx] if sf_idx < len(SAMPLE_RATES) else 48000
    ch = (b1 >> 3) & 0x0F
    return {
        "object_type": (b0 >> 3) & 0x1F,
        "sample_rate": sr,
        "channels": ch or 2,
        "sample_rate_index": sf_idx,
    }


def _signed24(v: int) -> int:
    return v - 0x1000000 if v >= 0x800000 else v


def extract_tracks(data: bytes) -> dict:
    """解析标签流 → 音频/视频轨道信息。PTS = tag ts + CompositionTime。"""
    audio_ts: list[int] = []          # 原始 AAC 帧的 tag 时间戳（不含 seq 头）
    audio_cfg: Optional[dict] = None
    audio_other = 0
    video_pts: list[int] = []
    video_key_idx: list[int] = []
    video_codecs: set[str] = set()
    interleave: list[tuple[int, str]] = []  # (tag_ts, kind) 按文件顺序

    for ttype, ts, _size, body in iter_flv_tags(data):
        if ttype == 8 and body:
            b0 = body[0]
            fmt = (b0 >> 4) & 0x0F
            if fmt == 10:  # AAC
                if len(body) >= 2 and body[1] == 0:
                    if audio_cfg is None and len(body) >= 4:
                        audio_cfg = parse_audio_specific_config(body[2:])
                else:
                    audio_ts.append(ts)
            else:
                audio_other += 1
                audio_ts.append(ts)
            interleave.append((ts, "a"))
        elif ttype == 9 and body:
            b0 = body[0]
            key = ((b0 >> 4) & 0x0F) == 1
            if (b0 & 0x80) and len(body) >= 9:  # 增强 FLV（FourCC，如 hvc1/avc1）
                codec = body[1:5].decode("ascii", "replace")
                cts = _signed24(int.from_bytes(body[6:9], "big"))
            else:
                cid = b0 & 0x0F
                codec = {2: "sorenson", 4: "vp6", 7: "avc", 12: "hevc"}.get(cid, f"cid{cid}")
                cts = (_signed24(int.from_bytes(body[2:5], "big"))
                       if cid in (7, 12) and len(body) >= 5 else 0)
            video_pts.append(ts + cts)
            video_codecs.add(codec)
            if key:
                video_key_idx.append(len(video_pts) - 1)
            interleave.append((ts, "v"))

    return {
        "audio_ts": audio_ts,
        "audio_cfg": audio_cfg,
        "audio_other": audio_other,
        "video_pts": video_pts,
        "video_key_idx": video_key_idx,
        "video_codecs": sorted(video_codecs),
        "interleave": interleave,
    }


# ─────────────────────────── 统计 ───────────────────────────

def _deltas(seq: list[int]) -> list[int]:
    return [b - a for a, b in zip(seq, seq[1:])]


def _slope_per_min(points: list[tuple[float, float]]) -> float:
    """最小二乘斜率，输入 (x_ms, y_ms)，返回 y 随 x 的 ms/ms → 换算 ms/分钟。"""
    if len(points) < 3:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope * 60000.0


def _track_report(pts: list[int], nominal_delta: Optional[float],
                  label: str) -> dict:
    """通用轨道统计：抖动/丢帧/倒退/跳变。"""
    d = _deltas(pts)
    if not d:
        return {"label": label, "frames": len(pts)}
    pos = [x for x in d if x > 0]
    med = (statistics.median(pos) if pos else 0) if nominal_delta is None else nominal_delta
    drops = sum(max(0, round(x / med) - 1) for x in d if med and x > med * 1.5)
    backwards = sum(1 for x in d if x < 0)
    repeats = sum(1 for x in d if x == 0)
    jumps = [{"at_ms": pts[i], "delta_ms": d[i]}
             for i, x in enumerate(d) if abs(x) > 1000][:5]
    jitter = statistics.pstdev(pos) if len(pos) > 2 else 0.0
    return {
        "label": label,
        "frames": len(pts),
        "median_delta_ms": round(float(med), 3),
        "jitter_ms": round(float(jitter), 3),
        "dropped_frames": drops,
        "backwards_jumps": backwards,
        "repeat_frames": repeats,
        "rewind_pct": round(100.0 * backwards / len(d), 2),
        "ts_discontinuities": jumps,
        "span_ms": (pts[-1] - pts[0]) if len(pts) > 1 else 0,
    }


def _longest_segment(pts: list[int], jump_threshold_ms: int = 1000):
    """按 |帧间 delta| > 阈值切分时间轴（CDN 重写/编码器重启），返回最长段索引与跳变列表。"""
    if not pts:
        return 0, -1, [], 0
    jumps: list[dict] = []
    seg_start, best = 0, (0, 0)
    segments = 1
    for i in range(1, len(pts)):
        if abs(pts[i] - pts[i - 1]) > jump_threshold_ms:
            jumps.append({"prev_ms": pts[i - 1], "next_ms": pts[i],
                          "delta_ms": pts[i] - pts[i - 1]})
            segments += 1
            if (i - 1 - seg_start) > (best[1] - best[0]):
                best = (seg_start, i - 1)
            seg_start = i
    if (len(pts) - 1 - seg_start) > (best[1] - best[0]):
        best = (seg_start, len(pts) - 1)
    return best[0], best[1], jumps, segments


def _joint_window(a_all: list[int], v_all: list[int]):
    """音视频各自最长连续段的时间交集（保证两轨落在同一时间段，避免 CDN 重写帧错配）。"""
    a_s, a_e, a_j, a_segs = _longest_segment(a_all)
    v_s, v_e, v_j, v_segs = _longest_segment(v_all)
    a_seg = a_all[a_s:a_e + 1]
    v_seg = v_all[v_s:v_e + 1]
    if not a_seg or not v_seg:
        return (a_s, a_e), (v_s, v_e), a_j, v_j, a_segs, v_segs
    lo, hi = max(a_seg[0], v_seg[0]), min(a_seg[-1], v_seg[-1])

    def _clip(seq):
        lo_i = next((i for i, x in enumerate(seq) if x >= lo), None)
        hi_i = next((i for i in range(len(seq) - 1, -1, -1) if seq[i] <= hi), None)
        return (lo_i, hi_i) if lo_i is not None and hi_i is not None and hi_i >= lo_i else None

    ac, vc = _clip(a_seg), _clip(v_seg)
    if ac and vc and (ac[1] - ac[0]) > 5 and (vc[1] - vc[0]) > 5:
        return ((a_s + ac[0], a_s + ac[1]), (v_s + vc[0], v_s + vc[1]),
                a_j, v_j, a_segs, v_segs)
    return (a_s, a_e), (v_s, v_e), a_j, v_j, a_segs, v_segs


def analyze_tracks(tracks: dict) -> dict:
    """标签级音画同步分析（毫秒精度）。"""
    a_all = tracks["audio_ts"]
    v_all = tracks["video_pts"]
    (a_s, a_e), (v_s, v_e), a_jumps, v_jumps, a_segs, v_segs = _joint_window(a_all, v_all)
    a_ts = a_all[a_s:a_e + 1] if a_e >= a_s else []
    v_pts = v_all[v_s:v_e + 1] if v_e >= v_s else []
    m: dict[str, Any] = {
        "has_audio": bool(a_all), "has_video": bool(v_all),
        "ts_health": {
            "audio_jumps": len(a_jumps), "video_jumps": len(v_jumps),
            "audio_segments": a_segs, "video_segments": v_segs,
            "audio_jumps_detail": a_jumps[:5], "video_jumps_detail": v_jumps[:5],
            "analyzed_audio_frames": len(a_ts), "analyzed_video_frames": len(v_pts),
            "total_audio_frames": len(a_all), "total_video_frames": len(v_all),
        },
    }
    if a_jumps or v_jumps:
        logger.warning("检测到时间戳跳变（音频 %d / 视频 %d 处），已自动在最长连续段内计算偏移",
                       len(a_jumps), len(v_jumps))

    # ── 起始偏移：音频首帧 PTS - 视频首帧 PTS（正=音频时间戳超前）──
    if a_ts and v_pts:
        m["start_offset_ms"] = a_ts[0] - v_pts[0]

    # ── 音频媒体时钟漂移：AAC 帧真实时长 vs mux 时间戳 ──
    if tracks["audio_cfg"] and len(a_ts) > 10:
        frame_dur = AAC_SAMPLES_PER_FRAME / tracks["audio_cfg"]["sample_rate"] * 1000.0
        expected_span = (len(a_ts) - 1) * frame_dur
        actual_span = a_ts[-1] - a_ts[0]
        drift = actual_span - expected_span
        m["aac"] = {**tracks["audio_cfg"], "frame_duration_ms": round(frame_dur, 4)}
        m["audio_clock_drift_ms"] = round(drift, 2)
        m["audio_clock_drift_per_min_ms"] = round(
            drift / (expected_span / 60000.0), 3) if expected_span > 0 else 0.0

    # ── 视频轨道 ──
    if len(v_pts) > 2:
        d = _deltas(v_pts)
        med = statistics.median([x for x in d if x > 0]) or 0
        # 帧率用「排序去重后的时间戳」估算，规避 CDN 回退重发/重复帧污染
        uniq = sorted(set(v_pts))
        ud = [x for x in _deltas(uniq) if x > 0]
        med_u = statistics.median(ud) if ud else med
        fps = 1000.0 / med_u if med_u else 0.0
        m["video"] = _track_report(v_pts, med, "video")
        m["video"]["fps_est"] = round(fps, 3)
        # 关键帧索引基于完整轨道；必须按当前分析窗口 [v_s, v_e] 映射回绝对索引，
        # 否则在含 B 帧（PTS 重排）或窗口被裁剪时下标会越界。
        _key_abs = [i for i in tracks["video_key_idx"] if v_s <= i <= v_e]
        if len(_key_abs) > 1:
            kpts = [v_all[i] for i in _key_abs]
            kd = _deltas(kpts)
            m["video"]["gop_median_ms"] = round(float(statistics.median(kd)), 1) if kd else 0
        m["video"]["codecs"] = tracks["video_codecs"]

    # ── 音频轨道（时间戳抖动）──
    if len(a_ts) > 2:
        m["audio"] = _track_report(a_ts, m.get("aac", {}).get("frame_duration_ms"), "audio")

    # ── A/V 交错偏移序列（按 tag 顺序，1 秒降采样）──
    if a_ts and v_pts:
        series: list[tuple[int, float]] = []
        last_keep = -10**9
        last_a = last_v = None
        win_lo = max(a_ts[0], v_pts[0])
        win_hi = min(a_ts[-1], v_pts[-1])
        for ts, kind in tracks["interleave"]:
            if ts < win_lo or ts > win_hi:
                continue
            if kind == "a":
                last_a = ts
            else:
                last_v = ts
            if last_a is not None and last_v is not None and ts - last_keep >= 1000:
                series.append((ts, float(last_a - last_v)))
                last_keep = ts
        if len(series) >= 2:
            offs = [s[1] for s in series]
            m["av_offset"] = {
                "series_sampled_per_sec": [{"t_ms": t, "offset_ms": round(o, 1)} for t, o in series[:600]],
                "first_ms": round(series[0][1], 1),
                "last_ms": round(series[-1][1], 1),
                "mean_ms": round(statistics.fmean(offs), 2),
                "median_ms": round(statistics.median(offs), 2),
                "min_ms": round(min(offs), 1),
                "max_ms": round(max(offs), 1),
                "max_abs_ms": round(max(abs(o) for o in offs), 1),
                "interleave_trend_per_min_ms": round(_slope_per_min(series), 3),
            }

    # ── 交错质量：同型连续 run ──
    inter = tracks["interleave"]
    if inter:
        runs, cur, cur_kind = [], 0, None
        for _, kind in inter:
            if kind == cur_kind:
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur, cur_kind = 1, kind
        runs.append(cur)
        m["interleave"] = {
            "max_same_kind_run": max(runs),
            "bursts_ge_10": sum(1 for r in runs if r >= 10),
            "a_tag_ratio": round(sum(1 for _, k in inter if k == "a") / len(inter), 3),
        }
    return m


# ─────────────────────────── 解码级验证 ───────────────────────────

AV_RE = re.compile(r"A-V:\s*(-?\d+(?:\.\d+)?)")


def resolve_ffplay() -> Optional[str]:
    exe = shutil.which("ffplay")
    if exe:
        return exe
    for cand in ("/opt/ffmpeg/bin/ffplay",):
        if Path(cand).exists():
            return cand
    return None


def run_decode_check(source: str, seconds: int, ffplay: Optional[str]) -> Optional[dict]:
    """ffplay 无头实播 N 秒（SDL dummy 驱动，不弹窗口），逐帧解析 A-V 差值（秒→毫秒）。
    A-V = 音频PTS - 视频PTS，是解码后的地面真值（ffplay 输出精度 1ms）。"""
    ffplay = ffplay or resolve_ffplay()
    if not ffplay:
        logger.warning("ffplay 不可用，跳过解码级验证")
        return None
    cmd = [ffplay, "-autoexit", "-t", str(seconds), "-loglevel", "info", source]
    env = dict(os.environ)
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    out = ""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        try:
            so, se = proc.communicate(timeout=seconds + 20)
        except subprocess.TimeoutExpired:
            # 直播流可能不按 -t 退出：杀掉进程但保留已采集的状态行
            proc.kill()
            so, se = proc.communicate()
        out = (se or "") + (so or "")
    except OSError as exc:
        logger.warning("解码验证失败: %s", exc)
        return None
    vals = [float(x) * 1000.0 for x in AV_RE.findall(out)]
    if len(vals) < 5:
        return None
    vals = vals[5:] or vals           # 丢弃启动瞬态
    if not vals:
        return None
    return {
        "samples": len(vals),
        "window_seconds": seconds,
        "mean_ms": round(statistics.fmean(vals), 2),
        "min_ms": round(min(vals), 2),
        "max_ms": round(max(vals), 2),
        "stdev_ms": round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0,
        "note": "ffmpeg 逐帧 A-V = 音频PTS-视频PTS（负值=音频滞后）；解码级地面真值",
    }


# ─────────────────────────── 在线拉流模式 ───────────────────────────

def live_capture(url: str, duration: int, max_bytes: int) -> tuple[bytes, list[tuple[float, int]]]:
    """流式下载 duration 秒，记录每块的（相对墙钟ms, 累计字节）。"""
    import requests
    marks: list[tuple[float, int]] = []
    buf = bytearray()
    t0 = time.monotonic()
    with requests.get(url, stream=True, timeout=(10, 30)) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=32768):
            if not chunk:
                continue
            buf.extend(chunk)
            marks.append((round((time.monotonic() - t0) * 1000), len(buf)))
            if (time.monotonic() - t0) >= duration or len(buf) >= max_bytes:
                break
    return bytes(buf), marks


def pacing_analysis(bytes_total: int, marks: list[tuple[float, int]],
                    media_span_ms: float) -> Optional[dict]:
    """到达节奏：媒体时间推进速率 vs 墙钟（含停顿/追赶检测）。"""
    if len(marks) < 3 or media_span_ms <= 0:
        return None
    wall_span = marks[-1][0]
    ratio = media_span_ms / wall_span if wall_span > 0 else 0
    if ratio > 1.5:
        return {"applicable": False, "wall_span_ms": round(wall_span),
                "media_span_ms": round(media_span_ms), "pace_ratio": round(ratio, 4),
                "note": "非实时传输（整文件/缓存下发），流节奏与停顿指标不适用"}
    gaps = [{"at_wall_ms": marks[i - 1][0], "gap_ms": round(marks[i][0] - marks[i - 1][0], 1)}
            for i in range(1, len(marks)) if marks[i][0] - marks[i - 1][0] > 2000]
    return {
        "applicable": True,
        "wall_span_ms": round(wall_span),
        "media_span_ms": round(media_span_ms),
        "pace_ratio": round(ratio, 4),
        "stalls_gt_2s": gaps[:5],
        "note": "pace_ratio≈1 实时匀速；>1 CDN 追赶/突发；TCP 缓冲会平滑毫秒级抖动",
    }


# ─────────────────────────── 判定与输出 ───────────────────────────

def build_verdict(result: dict) -> None:
    """双量判定：流内偏移（内容本身，主指标）+ 播放器修正后残差（次要）。

    感知同步的两个层次：
      A. 流内偏移 = 音频 PTS - 视频 PTS（标签级/解码前）。这是推流/转码端的固有缺陷，
         是 EBU R37、ITU-R BT.1359 等标准的度量对象。
      B. 播放器残差 = 现代播放器（ffplay/浏览器等）会用音频时钟对齐视频、
         主动吸收**恒定**偏移后的剩余误差。
    因此：流内偏移大 + 残差小 ≠ 正常（说明播放器在暗中修复，代价是丢帧/重采样，
    且部分终端不修正）；漂移（ms/min）无法被恒定修正掩盖，会周期性触发跳变。
    """
    m = result["metrics"]
    if not (m.get("has_audio") and m.get("has_video")):
        result["verdict"], result["verdict_level"] = "无法评估（缺音频或视频轨道）", "低"
        return

    start_off = float(m.get("start_offset_ms", 0.0))
    avo = m.get("av_offset") or {}
    stream_off = float(avo.get("median_ms", avo.get("mean_ms", start_off)))
    peak = float(avo.get("max_abs_ms", abs(start_off)))
    # 主指标：交错偏移中位数（稳健）；无交错数据时回退起始偏移
    primary = stream_off if avo else start_off
    start_off_ref = start_off
    _audio_span_ms = float((m.get("audio") or {}).get("span_ms", 0) or 0)
    drift_measurable = _audio_span_ms >= 20000 and m.get("audio_clock_drift_per_min_ms") is not None
    drift = float(m.get("audio_clock_drift_per_min_ms", 0.0)) if drift_measurable else 0.0
    residual = float(result["decode_check"]["mean_ms"]) if result.get("decode_check") else None

    itu, ebu = PERCEPTUAL_ITU, PERCEPTUAL_EBU_R37
    ebu_ok = ebu["audio_lag_ms"] <= primary <= ebu["audio_lead_ms"]
    itu_detect = (primary >= itu["audio_lead_detect_ms"]) or (primary <= itu["audio_lag_detect_ms"])
    itu_accept = (primary >= itu["audio_lead_accept_ms"]) or (primary <= itu["audio_lag_accept_ms"])
    drifted = abs(drift) > 50

    if itu_accept:
        level, grade = "低", "流内明显偏移（超出 ITU 可接受门限）"
    elif itu_detect:
        level, grade = "低", "流内可感知偏移（超出 ITU 可察觉门限）"
    elif not ebu_ok:
        level, grade = "中", "流内轻微偏移（超 EBU R37 建议窗口）"
    elif drifted:
        level, grade = "中", "存在时钟漂移（会逐渐累积成可感偏移）"
    else:
        level, grade = "高", "流内同步良好（符合 EBU R37 广播窗口）"

    direction = "音频超前画面" if primary >= 0 else "音频滞后画面"
    lines = [
        (f"【流内偏移·主指标】{primary:+.2f} ms（{direction}）"
         f"，峰值 |{peak:.1f}| ms，"
         + (f"音频时间线漂移 {drift:+.2f} ms/min（{_audio_span_ms/1000:.0f}s 样本）"
            if drift_measurable else
            f"漂移未参与判定（样本仅 {_audio_span_ms/1000:.0f}s，<20s 噪声较大）")),
        (f"【播放器残差】"
         + (f"ffplay 解码级 A-V 均值 {residual:+.2f} ms（±{result['decode_check']['stdev_ms']:.2f}，"
            f"范围 [{result['decode_check']['min_ms']:+.1f}, {result['decode_check']['max_ms']:+.1f}]）"
            if residual is not None else "未启用解码级验证（--decode-check）")),
        (f"感知门限：EBU R37 建议窗口 [{ebu['audio_lag_ms']:+d}, {ebu['audio_lead_ms']:+d}] ms（窗口不对称："
         f"听觉超前比滞后更易察觉）；ITU-R BT.1359-1 可察觉 +{itu['audio_lead_detect_ms']}/{itu['audio_lag_detect_ms']} ms，"
         f"可接受 +{itu['audio_lead_accept_ms']}/{itu['audio_lag_accept_ms']} ms"),
    ]
    if residual is not None and abs(primary) - abs(residual) > 20:
        lines.append(
            f"⚠️ 播放器修正：流内偏移 |{primary:.1f}| ms 被播放器吸收，残差仅 {residual:+.1f} ms。"
            f"观众在此播放器上可能感知不到明显不同步，但代价是丢帧/时间戳重排，"
            f"且部分终端（简单播放器/硬件解码链）不做修正或修正能力有限。")
    if drifted:
        lines.append(f"⚠️ 漂移 {drift:+.1f} ms/min 是更危险的缺陷：恒定偏移可被修正，漂移无法，"
                     f"会周期性触发跳帧/重采样并最终超出修正范围。")
    _v = m.get("video") or {}
    if _v.get("rewind_pct", 0) >= 2:
        lines.append(f"ℹ️ 检测到帧时间戳回退 {_v['rewind_pct']:.1f}%（{_v.get('backwards_jumps')} 次）/ 重复帧 {_v.get('repeat_frames')}："
                     f"抖音边沿拉流常见的回退重发（起播/续传时重发上一段 GOP），解码端会丢弃重复帧，"
                     f"通常不影响观看；帧率类指标已按正增量统计。")
    tsh = m.get("ts_health") or {}
    if tsh.get("audio_jumps") or tsh.get("video_jumps"):
        lines.append(f"⚠️ 时间戳跳变：音频 {tsh.get('audio_jumps', 0)} 处 / 视频 {tsh.get('video_jumps', 0)} 处"
                     f"（≥1s 不连续，常见于 CDN 边沿时间戳重写或编码器重启）；"
                     f"本次偏移已在最长连续段内计算（音频段 {tsh.get('audio_segments')} 个 / "
                     f"视频段 {tsh.get('video_segments')} 个）。")
    _span_ms = float(result.get("media_span_ms") or 0)
    if 0 < _span_ms < 10000:
        lines.append(f"ℹ️ 样本时长仅 {_span_ms/1000:.1f}s：帧率/丢帧/漂移等速率类指标仅供参考"
                     f"（短样本可能被下载上限截断），建议用 --url 模式采样 20~30s。")
    lines.append("⚠️ 本值 = 流内 PTS 级偏移，不含本地显示/音频输出链延迟（蓝牙耳机 +100~200ms、"
                 "电视图像处理 +10~40ms、功放/声霸等）。端到端听感偏移 ≈ 流内偏移（若播放器未修正）"
                 "或 ≈ 播放器残差，再叠加输出链延迟。")
    result["verdict"], result["verdict_level"] = grade, level
    result["verdict_detail"] = chr(10).join(lines)
    result["perceptual"] = {
        "stream_offset_ms": round(primary, 2),
        "stream_peak_abs_ms": round(peak, 1),
        "player_residual_ms": round(residual, 2) if residual is not None else None,
        "audio_timeline_drift_per_min_ms": round(drift, 2) if drift_measurable else None,
        "drift_measurable": drift_measurable,
        "direction": direction,
        "ebu_r37_ok": ebu_ok,
        "itu_detectable": itu_detect,
        "itu_acceptable": not itu_accept,
        "thresholds": {"itu": itu, "ebu_r37": ebu},
        "excludes_local_output_chain": True,
    }


def build_text_report(result: dict) -> str:
    m, lines = result["metrics"], []
    title = f"音画同步检测报告 - {result.get('source', '?')}"
    lines.append("=" * 60)
    lines.append(f"  {title}")
    lines.append("=" * 60)
    lines.append(f"  生成时间: {result.get('generated_at', '')}")
    lines.append(f"  感知判定: {result.get('verdict', '—')}（等级 {result.get('verdict_level', '—')}）")
    lines.append("")
    lines.append(f"  音频轨道: {'有' if m.get('has_audio') else '无'}"
                 f"{' | AAC ' + str(m['aac']['sample_rate']) + 'Hz/' + str(m['aac']['channels']) + 'ch' if m.get('aac') else ''}")
    lines.append(f"  视频轨道: {'有' if m.get('has_video') else '无'}"
                 f"{' | ' + ','.join(m.get('video', {}).get('codecs', [])) if m.get('video') else ''}")
    if m.get("video"):
        v = m["video"]
        lines.append(f"  视频: {v.get('fps_est', '?')} fps | 帧间抖动 {v.get('jitter_ms', 0):.2f} ms"
                     f" | 丢帧 {v.get('dropped_frames', 0)} | 倒退 {v.get('backwards_jumps', 0)}"
                     f" | GOP {v.get('gop_median_ms', '?')} ms")
    if m.get("audio_clock_drift_per_min_ms") is not None:
        lines.append(f"  音频时钟漂移: {m['audio_clock_drift_ms']:+.2f} ms"
                     f"（{m['audio_clock_drift_per_min_ms']:+.2f} ms/min）")
    if m.get("av_offset"):
        o = m["av_offset"]
        lines.append(f"  A/V 交错偏移: 起始 {o['first_ms']:+.1f} → 末尾 {o['last_ms']:+.1f} ms"
                     f" | 均值 {o['mean_ms']:+.2f} | 最大 |{o['max_abs_ms']:.1f}|"
                     f" | 交错趋势 {o.get('interleave_trend_per_min_ms', 0):+.2f} ms/min")
    if m.get("ts_health") and (m["ts_health"].get("audio_jumps") or m["ts_health"].get("video_jumps")):
        t = m["ts_health"]
        lines.append(f"  时间戳跳变: 音频 {t['audio_jumps']} 处 / 视频 {t['video_jumps']} 处"
                     f"（段数 音频 {t['audio_segments']} / 视频 {t['video_segments']}）")
    if m.get("interleave"):
        i = m["interleave"]
        lines.append(f"  交错: 最大同型连续 {i['max_same_kind_run']} tags | 突发(≥10) {i['bursts_ge_10']} 次"
                     f" | 音频tag占比 {i['a_tag_ratio']:.1%}")
    if result.get("decode_check"):
        d = result["decode_check"]
        lines.append(f"  解码级 A-V: 平均 {d['mean_ms']:+.2f} ms（±{d['stdev_ms']:.2f}）"
                     f" 范围 [{d['min_ms']:+.2f}, {d['max_ms']:+.2f}] ms | {d['samples']} 帧样本")
    if result.get("pacing"):
        p = result["pacing"]
        if p.get("applicable"):
            lines.append(f"  到达节奏: 媒体/墙钟 = {p['pace_ratio']} | 停顿>2s: {len(p['stalls_gt_2s'])} 次")
        else:
            lines.append(f"  到达节奏: 不适用（{p.get('note', '')}）")
    lines.append("")
    lines.append(f"  {result.get('verdict_detail', '')}")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_outputs(result: dict, output_dir: Path, ts: str | None = None) -> tuple[Path, Path]:
    ensure_dirs()
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"av_sync_analysis_{ts}.json"
    txt_path = output_dir / f"av_sync_report_{ts}.txt"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(build_text_report(result), encoding="utf-8")
    return json_path, txt_path


def run_av_sync_analysis(sample: str | Path, output_dir: Path, ts: str,
                         room_id: Optional[str] = None,
                         decode_check: bool = True,
                         decode_seconds: int = 8) -> dict:
    """供 analyze_room 调用的入口：分析已有样本并落盘。"""
    result = analyze_file(sample, decode_check=decode_check, decode_seconds=decode_seconds)
    result["room_id"] = str(room_id) if room_id else None
    result["session_ts"] = ts
    json_path, txt_path = save_outputs(result, Path(output_dir), ts=ts)
    result["output_files"] = [str(json_path), str(txt_path)]
    return result


# ─────────────────────────── 分析入口 ───────────────────────────

def analyze_file(path: str | Path, decode_check: bool = False,
                 decode_seconds: int = 8) -> dict:
    path = Path(path)
    data = path.read_bytes()
    tracks = extract_tracks(data)
    metrics = analyze_tracks(tracks)
    result = {
        "type": "av_sync_analysis",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(path),
        "mode": "file",
        "sample_bytes": len(data),
        "media_span_ms": max(
            tracks["audio_ts"][-1] - tracks["audio_ts"][0] if len(tracks["audio_ts"]) > 1 else 0,
            tracks["video_pts"][-1] - tracks["video_pts"][0] if len(tracks["video_pts"]) > 1 else 0,
        ),
        "metrics": metrics,
    }
    if decode_check:
        result["decode_check"] = run_decode_check(str(path), decode_seconds, None)
    build_verdict(result)
    return result


def analyze_url(url: str, duration: int = 10, max_bytes: int = 8 * 1024 * 1024,
                decode_check: bool = True, decode_seconds: int = 8) -> dict:
    data, marks = live_capture(url, duration, max_bytes)
    tracks = extract_tracks(data)
    metrics = analyze_tracks(tracks)
    media_span = max(
        tracks["audio_ts"][-1] - tracks["audio_ts"][0] if len(tracks["audio_ts"]) > 1 else 0,
        tracks["video_pts"][-1] - tracks["video_pts"][0] if len(tracks["video_pts"]) > 1 else 0,
    )
    result = {
        "type": "av_sync_analysis",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": url,
        "mode": "live_url",
        "sample_bytes": len(data),
        "media_span_ms": media_span,
        "metrics": metrics,
    }
    if metrics.get("has_audio") and metrics.get("has_video"):
        result["pacing"] = pacing_analysis(len(data), marks, media_span)
    if decode_check:
        result["decode_check"] = run_decode_check(url, decode_seconds, None)
    build_verdict(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="音画同步检测器（标签级 + 解码级，毫秒精度）")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", "-f", help="分析本地 FLV 文件")
    src.add_argument("--url", "-u", help="在线拉流分析（流式采样）")
    parser.add_argument("--duration", type=int, default=10, help="URL 模式采样秒数（默认 10）")
    parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024, help="URL 模式最大下载字节")
    parser.add_argument("--decode-check", action="store_true", help="附加 ffmpeg 解码级 A-V 验证")
    parser.add_argument("--decode-seconds", type=int, default=8, help="解码验证时长秒数（默认 8）")
    parser.add_argument("--room-id", help="房间号（写入 JSON 供统一报告关联）")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--no-save", action="store_true", help="不写文件，仅打印")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.file:
        result = analyze_file(args.file, decode_check=args.decode_check,
                              decode_seconds=args.decode_seconds)
    else:
        result = analyze_url(args.url, duration=args.duration, max_bytes=args.max_bytes,
                             decode_check=True, decode_seconds=args.decode_seconds)
    result["room_id"] = args.room_id

    if not args.no_save:
        json_path, txt_path = save_outputs(result, Path(args.output_dir))
        logger.info("JSON 已保存: %s", json_path)
        logger.info("报告已保存: %s", txt_path)
    print(build_text_report(result))


if __name__ == "__main__":
    main()
