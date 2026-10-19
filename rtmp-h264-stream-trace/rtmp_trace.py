#!/usr/bin/env python3
"""rtmp-h264-stream-trace —— RTMP / RTMPS（H.264）源流体检工具。

针对「自己推的 RTMP 流」或任意裸 RTMP 地址做一次性体检，不依赖任何直播平台接口：

  1. 连通性与建连耗时
  2. 视频编码校验（是否 H.264；HEVC 会告警）
  3. 分辨率 / 帧率 / 像素格式 / B 帧 / 编码 profile·level
  4. 码率（ffprobe 上报值 + 按录制样本实测值）
  5. GOP / 关键帧间隔（解析 FLV tag 时间戳，中位数 + 最大值）
  6. 音轨（编码/采样率/声道/码率）与音画起始偏移（毫秒）
  7. 推流端识别（FLV onMetaData 的 encoder 标签 → OBS / FFmpeg …）

可选增强（若同级存在 ../live-stream-trace/core，自动启用）：
  · 时间戳级音画同步（av_sync_detector：tag PTS + 解码级 A-V）
  · 内容级音画同步（lip_sync_detector：人脸嘴型，能发现推流前管线失同步）

产物：<report_dir>/rtmp_report_<ts>/{index.html, report.txt} + captures/ 原始证据

用法:
    python rtmp_trace.py rtmp://127.0.0.1:1935/live/stream
    python rtmp_trace.py rtmp://a.com/app/key --seconds 25 --format both
    python rtmp_trace.py rtmp://a.com/app/key --no-record        # 只探测不录制
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
REPORTS = ROOT / "reports"

FFMPEG_CANDIDATES = ("/opt/ffmpeg/bin/ffmpeg",
                     "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
FFPROBE_CANDIDATES = ("/opt/ffmpeg/bin/ffprobe",
                      "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")

# 可选：复用统一项目 core 的深度分析（存在即启用）
UNIFIED_CORE = ROOT.parent / "live-stream-trace" / "core"

logger = logging.getLogger("rtmp_trace")

H264_PROFILES = {"66": "Baseline", "77": "Main", "88": "Extended",
                 "100": "High", "110": "High 10", "122": "High 4:2:2", "244": "High 4:4:4"}
AAC_SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                    16000, 12000, 11025, 8000, 7350]
PUSH_PATTERNS = ((r"obs", "OBS Studio"), (r"ffmpeg|lavf|librtmp", "FFmpeg / Lavf"),
                 (r"streamlabs", "Streamlabs"), (r"xsplit", "XSplit"),
                 (r"vmix", "vMix"), (r"wirecast", "Wirecast"), (r"kasa", "Kasa"))
# 常见 RTMP 地址形如 rtmp://host[:port]/app/stream
RTMP_RE = re.compile(r"rtmps?t?://[^\s\"'<>\\]{4,500}", re.I)


# ─────────────────────────── 可执行文件 ───────────────────────────

def _resolve(name: str, cands: tuple) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    for c in cands:
        if Path(c).exists():
            return c
    return None


def ffmpeg_bin() -> str | None:
    return _resolve("ffmpeg", FFMPEG_CANDIDATES)


def ffprobe_bin() -> str | None:
    return _resolve("ffprobe", FFPROBE_CANDIDATES)


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fps(v):
    try:
        num, _, den = str(v or "").partition("/")
        d = float(den or 1)
        return round(float(num) / d, 3) if d else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ─────────────────────────── 探测 ───────────────────────────

def probe_stream(url: str, timeout: int = 25) -> dict:
    out = {"ok": False, "error": None, "elapsed_ms": None,
           "video": {}, "audio": {}, "format": {}, "tags": {}}
    exe = ffprobe_bin()
    if not exe:
        out["error"] = "ffprobe 不可用"
        return out
    cmd = [exe, "-v", "error", "-rw_timeout", str(timeout * 1_000_000),
           "-show_streams", "-show_format", "-of", "json", url]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired:
        out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        out["error"] = f"探测超时（{timeout}s）：无人在推流或端口不可达"
        return out
    out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        out["error"] = (lines[-1] if lines else "ffprobe 失败")[:240]
        return out
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except (json.JSONDecodeError, ValueError) as exc:
        out["error"] = f"ffprobe 输出解析失败: {exc}"
        return out
    fmt = data.get("format") or {}
    out["format"] = {"format_name": fmt.get("format_name"),
                     "duration": _num(fmt.get("duration")),
                     "bit_rate": _num(fmt.get("bit_rate")),
                     "nb_streams": fmt.get("nb_streams")}
    out["tags"] = dict(fmt.get("tags") or {})
    for s in data.get("streams") or []:
        for k, v in (s.get("tags") or {}).items():
            out["tags"].setdefault(k, v)
        if s.get("codec_type") == "video" and not out["video"]:
            out["video"] = {"codec": s.get("codec_name"), "profile_id": s.get("profile"),
                            "level": s.get("level"), "width": s.get("width"),
                            "height": s.get("height"),
                            "fps": _fps(s.get("avg_frame_rate") or s.get("r_frame_rate")),
                            "pix_fmt": s.get("pix_fmt"), "has_b_frames": s.get("has_b_frames"),
                            "bit_rate": _num(s.get("bit_rate"))}
        elif s.get("codec_type") == "audio" and not out["audio"]:
            out["audio"] = {"codec": s.get("codec_name"), "profile": s.get("profile"),
                            "sample_rate": _num(s.get("sample_rate")),
                            "channels": s.get("channels"),
                            "channel_layout": s.get("channel_layout"),
                            "bit_rate": _num(s.get("bit_rate"))}
    out["ok"] = bool(out["video"] or out["audio"])
    if not out["ok"]:
        out["error"] = "已连接但未解析出音视频流"
    return out


def record_sample(url: str, dest: Path, seconds: int, timeout: int = 25) -> int:
    """ffmpeg -c copy 录制 N 秒 FLV，返回字节数（0 表示失败）。"""
    exe = ffmpeg_bin()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not exe:
        return 0
    cmd = [exe, "-v", "error", "-nostdin", "-rw_timeout", str(timeout * 1_000_000),
           "-i", url, "-t", str(max(3, seconds)), "-c", "copy", "-f", "flv", "-y", str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 40, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("录制超时")
        return 0
    if proc.returncode != 0 and not dest.exists():
        logger.warning("录制失败: %s", proc.stderr.decode("utf-8", "replace")[-200:])
        return 0
    return dest.stat().st_size if dest.exists() else 0


# ─────────────────────────── FLV 解析（GOP / 音画偏移 / 编码器）───────────────────────────

def iter_flv_tags(data: bytes):
    if len(data) < 13 or data[:3] != b"FLV":
        raise ValueError("不是有效的 FLV 数据")
    pos, n = 13, len(data)
    while pos + 11 <= n:
        ttype = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if size <= 0 or pos + 11 + size > n:
            break
        ts = int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24)
        yield ttype, ts, size, data[pos + 11:pos + 11 + size]
        pos += 11 + size + 4


def _amf0_string_after(body: bytes, key: str) -> str | None:
    idx = body.find(key.encode())
    if idx < 0:
        return None
    i, guard = idx + len(key), 0
    while i < len(body) and body[i] != 0x02 and guard < 64:
        i, guard = i + 1, guard + 1
    if i + 3 > len(body):
        return None
    ln = int.from_bytes(body[i + 1:i + 3], "big")
    if ln <= 0 or i + 3 + ln > len(body):
        return None
    return body[i + 3:i + 3 + ln].decode("utf-8", "replace")


def _parse_aac_cfg(cfg: bytes) -> dict | None:
    if len(cfg) < 2:
        return None
    b0, b1 = cfg[0], cfg[1]
    sf = ((b0 & 0x07) << 1) | (b1 >> 7)
    return {"object_type": (b0 >> 3) & 0x1F,
            "sample_rate": AAC_SAMPLE_RATES[sf] if sf < len(AAC_SAMPLE_RATES) else None,
            "channels": (b1 >> 3) & 0x0F or 2}


def scan_flv(data: bytes) -> dict:
    """解析 FLV → A/V 时间戳、关键帧、GOP、编码器标签、AAC 参数。"""
    video_pts: list[int] = []
    key_idx: list[int] = []
    audio_ts: list[int] = []
    video_tags = audio_tags = other_tags = 0
    codecs: set[str] = set()
    aac_cfg: dict | None = None
    encoder: str | None = None
    meta_flags: dict = {}
    for ttype, ts, _size, body in iter_flv_tags(data):
        if ttype == 9 and body:
            video_tags += 1
            b0 = body[0]
            key = ((b0 >> 4) & 0x0F) == 1
            if (b0 & 0x80) and len(body) >= 9:
                codecs.add(body[1:5].decode("ascii", "replace"))
                cts = int.from_bytes(body[6:9], "big")
                cts = cts - 0x1000000 if cts >= 0x800000 else cts
            else:
                cid = b0 & 0x0F
                codecs.add({2: "sorenson", 4: "vp6", 7: "avc", 12: "hevc"}.get(cid, f"cid{cid}"))
                cts = 0
                if cid in (7, 12) and len(body) >= 5:
                    raw = int.from_bytes(body[2:5], "big")
                    cts = raw - 0x1000000 if raw >= 0x800000 else raw
            video_pts.append(ts + cts)
            if key:
                key_idx.append(len(video_pts) - 1)
        elif ttype == 8 and body:
            audio_tags += 1
            fmt = (body[0] >> 4) & 0x0F
            if fmt == 10:
                if len(body) >= 2 and body[1] == 0:
                    if aac_cfg is None and len(body) >= 4:
                        aac_cfg = _parse_aac_cfg(body[2:])
                else:
                    audio_ts.append(ts)
            else:
                audio_ts.append(ts)
        elif ttype == 18 and body:
            other_tags += 1
            if encoder is None:
                encoder = _amf0_string_after(body, "encoder")
            for flag in ("isHardEncoder", "hasVideo", "hasAudio", "displayWidth", "displayHeight"):
                if flag not in meta_flags and flag.encode() in body:
                    meta_flags[flag] = True
    gop_ms: list[int] = []
    kpts = [video_pts[i] for i in key_idx if i < len(video_pts)]
    gop_ms = [b - a for a, b in zip(kpts, kpts[1:]) if b > a]
    return {
        "video_tags": video_tags, "audio_tags": audio_tags, "script_tags": other_tags,
        "codecs": sorted(codecs), "keyframes": len(kpts),
        "gop_median_ms": (sorted(gop_ms)[len(gop_ms) // 2] if gop_ms else None),
        "gop_max_ms": (max(gop_ms) if gop_ms else None),
        "gop_min_ms": (min(gop_ms) if gop_ms else None),
        "first_video_pts": video_pts[0] if video_pts else None,
        "last_video_pts": video_pts[-1] if video_pts else None,
        "first_audio_ts": audio_ts[0] if audio_ts else None,
        "last_audio_ts": audio_ts[-1] if audio_ts else None,
        "av_start_offset_ms": ((audio_ts[0] - video_pts[0])
                               if audio_ts and video_pts else None),
        "aac": aac_cfg, "encoder": encoder, "meta_flags": meta_flags,
        "video_frames": len(video_pts), "audio_frames": len(audio_ts),
    }


def infer_push(encoder: str | None) -> tuple[str, str | None]:
    """从 encoder 标签推断推流端与版本。"""
    if not encoder:
        return "未知（FLV onMetaData 未上报 encoder）", None
    low = encoder.lower()
    name = "未知"
    for pat, label in PUSH_PATTERNS:
        if re.search(pat, low):
            name = label
            break
    ver = None
    m = re.search(r"libobs version ([\d.]+)", encoder, re.I)
    if m:
        ver = f"OBS {m.group(1)}"
    else:
        m = re.search(r"(?:lavf|libavformat|ffmpeg)[^\d]*([\d.]+)", encoder, re.I)
        if m:
            ver = f"FFmpeg（Lavf{m.group(1)}）"
    return name, ver


# ─────────────────────────── 可选：统一项目的深度分析 ───────────────────────────

def unified_available() -> bool:
    return (UNIFIED_CORE / "av_sync_detector.py").exists()


def _load_unified():
    if str(UNIFIED_CORE) not in sys.path:
        sys.path.insert(0, str(UNIFIED_CORE))
    import av_sync_detector  # noqa: PLC0415
    return av_sync_detector


def deep_av_sync(sample: Path, seconds: int) -> dict:
    """时间戳级 + 内容级音画同步（复用统一项目 core）。失败返回 {}。"""
    out: dict = {}
    try:
        avs_mod = _load_unified()
        avs = avs_mod.analyze_file(sample, decode_check=True, decode_seconds=min(10, seconds))
        out["timestamp_level"] = {
            "stream_offset_ms": (avs.get("perceptual") or {}).get("stream_offset_ms"),
            "decode_mean_ms": (avs.get("decode_check") or {}).get("mean_ms"),
            "direction": (avs.get("perceptual") or {}).get("direction"),
            "verdict": avs.get("verdict"), "verdict_level": avs.get("verdict_level"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("时间戳级音画同步失败: %s", exc)
    try:
        if (UNIFIED_CORE / "lip_sync_detector.py").exists():
            if str(UNIFIED_CORE) not in sys.path:
                sys.path.insert(0, str(UNIFIED_CORE))
            import lip_sync_detector  # noqa: PLC0415
            lip = lip_sync_detector.analyze_media(sample, seconds=min(20, seconds))
            out["content_level"] = {
                "content_offset_ms": (lip.get("perceptual") or {}).get("content_offset_ms"),
                "confidence": (lip.get("perceptual") or {}).get("confidence"),
                "verdict": lip.get("verdict"), "verdict_level": lip.get("verdict_level"),
                "roi_mode": (lip.get("metrics") or {}).get("roi_mode"),
                "face_coverage": (lip.get("metrics") or {}).get("face_coverage"),
            }
            if out.get("timestamp_level") and out["content_level"].get("content_offset_ms") is not None:
                out["diagnosis"] = lip_sync_detector.diagnose_vs_stream_offset(
                    lip, {"perceptual": {"stream_offset_ms": out["timestamp_level"]["stream_offset_ms"]}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("内容级音画同步失败: %s", exc)
    return out


# ─────────────────────────── 结论 / 报告 ───────────────────────────

def build_checks(probe: dict, scan: dict, sample_bytes: int, seconds: int) -> list[dict]:
    v, a = probe.get("video") or {}, probe.get("audio") or {}
    codec = (v.get("codec") or "").lower()
    checks: list[dict] = []

    def add(name, ok, detail, level="info"):
        checks.append({"name": name, "ok": ok, "detail": detail, "level": level})

    add("连通性", bool(probe.get("ok")),
        f"建连 {probe.get('elapsed_ms')} ms" if probe.get("ok") else str(probe.get("error")),
        "ok" if probe.get("ok") else "error")
    add("视频编码 H.264", codec in ("h264", "avc1"),
        f"codec={v.get('codec') or '—'} profile={v.get('profile_id')} level={v.get('level')}",
        "ok" if codec in ("h264", "avc1") else "error")
    add("分辨率/帧率", bool(v.get("width")),
        f"{v.get('width')}x{v.get('height')} @ {v.get('fps')} fps · {v.get('pix_fmt')}",
        "ok" if v.get("width") else "warn")
    if v.get("has_b_frames") is not None:
        hb = int(v.get("has_b_frames") or 0)
        add("B 帧", hb == 0, f"has_b_frames={hb}"
            + ("（低延迟推流建议关闭 B 帧）" if hb else "（无 B 帧，低延迟友好）"),
            "ok" if hb == 0 else "warn")
    if scan.get("gop_median_ms") is not None:
        gop_s = scan["gop_median_ms"] / 1000.0
        add("关键帧间隔(GOP)", gop_s <= 4.0,
            f"中位 {gop_s:.2f}s · 最大 {(scan.get('gop_max_ms') or 0) / 1000:.2f}s "
            f"· 关键帧 {scan.get('keyframes')} 个",
            "ok" if gop_s <= 4.0 else "warn")
    add("音轨", bool(a.get("codec")),
        f"{a.get('codec') or '—'} {a.get('sample_rate') or '—'}Hz "
        f"{a.get('channels') or '—'}ch {round((a.get('bit_rate') or 0) / 1000)}kbps"
        if a.get("codec") else "未检测到音轨",
        "ok" if a.get("codec") else "warn")
    if sample_bytes and seconds:
        kbps = round(sample_bytes * 8 / max(seconds, 1) / 1000)
        add("实测码率", True, f"≈{kbps} kbps（{sample_bytes} 字节 / {seconds}s，含封装开销）")
    if scan.get("av_start_offset_ms") is not None:
        off = scan["av_start_offset_ms"]
        add("A/V 起始偏移(标签级)", abs(off) <= 40, f"{off:+d} ms（正=AAC 时间戳超前）",
            "ok" if abs(off) <= 40 else "warn")
    return checks


def overall_verdict(checks: list[dict]) -> tuple[str, str]:
    if any(c["level"] == "error" for c in checks):
        return "存在阻塞性问题", "低"
    if any(c["level"] == "warn" for c in checks):
        return "基本可用，有需要关注的项", "中"
    return "体检通过（H.264 RTMP 流正常）", "高"


def render_html(ts: str, url: str, probe: dict, scan: dict, checks: list[dict],
                deep: dict, sample_name: str, sample_bytes: int, seconds: int,
                report_dir: Path) -> str:
    v, a = probe.get("video") or {}, probe.get("audio") or {}
    push_name, push_ver = infer_push(scan.get("encoder"))
    verdict, level = overall_verdict(checks)
    color = {"高": "#4caf50", "中": "#ffb300", "低": "#ef5350"}[level]
    esc = html.escape

    def card(title, rows):
        items = "".join(
            f"<div class='metric'><div class='muted small'>{esc(str(k))}</div>"
            f"<div class='val'>{esc(str(v))}</div></div>" for k, v in rows)
        return (f"<div class='card'><h3>{esc(title)}</h3><div class='grid'>{items}</div></div>")

    check_rows = "".join(
        f"<tr><td>{'✅' if c['ok'] else ('❌' if c['level'] == 'error' else '⚠️')}</td>"
        f"<td>{esc(c['name'])}</td><td class='muted'>{esc(c['detail'])}</td></tr>"
        for c in checks)

    deep_html = ""
    if deep:
        tl = deep.get("timestamp_level") or {}
        cl = deep.get("content_level") or {}
        rows = [("时间戳级偏移", f"{tl.get('stream_offset_ms')} ms（{tl.get('direction') or '—'}）"),
                ("解码级 A-V 均值", f"{tl.get('decode_mean_ms')} ms"),
                ("内容级偏移（人脸嘴型）", (f"{cl.get('content_offset_ms')} ms"
                                     f"（置信度 {cl.get('confidence')}）"
                                     if cl.get("content_offset_ms") is not None
                                     else (cl.get("verdict") or "未测")))]
        deep_html = card("音画同步（深度分析）", rows)
        if deep.get("diagnosis"):
            deep_html += f"<div class='card'><div class='small'><strong>联合诊断：</strong>{esc(deep['diagnosis'])}</div></div>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RTMP H.264 流体检报告 - {esc(ts)}</title>
<style>
body{{margin:0;padding:24px;background:#0f1115;color:#e6e6e6;font-family:Segoe UI,Arial,sans-serif;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 6px}} h3{{font-size:15px;margin:0 0 12px;color:#9fb0c8}}
.card{{background:#171a21;border:1px solid #2b2f3a;border-radius:14px;padding:16px;margin:16px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.metric{{background:#11151d;border:1px solid #232833;border-radius:10px;padding:10px}}
.muted{{color:#8b97a8}} .small{{font-size:12px}} .val{{font-weight:600}}
.badge{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;color:#0b0d10;background:{color};font-weight:700}}
table{{width:100%;border-collapse:collapse}} td{{padding:8px 6px;border-bottom:1px solid #232833;vertical-align:top}}
code{{background:#11151d;padding:2px 6px;border-radius:6px;word-break:break-all}}
</style></head><body><div class="wrap">
<h1>RTMP / RTMPS H.264 流体检报告</h1>
<div class="muted small">生成于 {esc(ts)} ｜ 目标 <code>{esc(url)}</code></div>
<div class="card"><h3>总体结论</h3>
<span class="badge">{esc(verdict)}</span>
<div class="small muted" style="margin-top:8px">样本 {esc(sample_name or '未录制')}（{sample_bytes} 字节 / {seconds}s）
｜ 证据目录 <code>{esc(str(report_dir))}</code></div></div>
<div class="card"><h3>体检项</h3><table>{check_rows}</table></div>
{card("流参数", [
  ("视频编码", f"{v.get('codec') or '—'}（profile_id={v.get('profile_id')} level={v.get('level')}）"),
  ("分辨率", f"{v.get('width')}x{v.get('height')}"),
  ("帧率", f"{v.get('fps')} fps"),
  ("像素格式 / B帧", f"{v.get('pix_fmt')} / {v.get('has_b_frames')}"),
  ("视频码率", f"{round((v.get('bit_rate') or 0)/1000) or '—'} kbps"),
  ("音频", f"{a.get('codec') or '—'} {a.get('sample_rate') or '—'}Hz {a.get('channels') or '—'}ch"),
  ("音频码率", f"{round((a.get('bit_rate') or 0)/1000) or '—'} kbps"),
  ("封装总码率", f"{round(((probe.get('format') or {}).get('bit_rate') or 0)/1000) or '—'} kbps"),
])}
{card("FLV 结构（录制样本）", [
  ("视频帧 / 音频帧", f"{scan.get('video_frames')} / {scan.get('audio_frames')}"),
  ("ts 类型计数(v/a/script)", f"{scan.get('video_tags')} / {scan.get('audio_tags')} / {scan.get('script_tags')}"),
  ("视频 codec id", ", ".join(scan.get("codecs") or []) or "—"),
  ("关键帧数 / GOP 中位", f"{scan.get('keyframes')} / {scan.get('gop_median_ms')} ms"),
  ("GOP 最小 / 最大", f"{scan.get('gop_min_ms')} / {scan.get('gop_max_ms')} ms"),
  ("A/V 起始偏移(标签级)", f"{scan.get('av_start_offset_ms')} ms"),
  ("AAC 采样率/声道", f"{(scan.get('aac') or {}).get('sample_rate')} / {(scan.get('aac') or {}).get('channels')}"),
])}
{card("推流端", [
  ("识别", push_name), ("版本", push_ver or "—"),
  ("encoder 原始标签", scan.get("encoder") or "—"),
  ("server", (probe.get("tags") or {}).get("server", "—")),
])}
{deep_html}
<div class="card"><h3>提示</h3><div class="small">
· RTMP 无法在浏览器内播放：请用 <code>ffplay {esc(url)}</code> 或 VLC 打开。<br>
· 「A/V 起始偏移(标签级)」只反映时间戳层面；若需要「观众实际感知」的内容级错位，
  请启用深度分析（需同级 ../live-stream-trace/core 且安装 numpy + opencv）。<br>
· 本报告仅用于技术研究与自有流体检，请勿用于违反平台条款的用途。
</div></div>
</div></body></html>"""


def render_text(ts: str, url: str, probe: dict, scan: dict, checks: list[dict],
                deep: dict, sample_bytes: int, seconds: int) -> str:
    verdict, level = overall_verdict(checks)
    push_name, push_ver = infer_push(scan.get("encoder"))
    lines = ["=" * 68, f"  RTMP H.264 流体检报告 - {ts}", "=" * 68,
             f"  目标: {url}", f"  总体结论: {verdict}（置信度 {level}）", "-" * 68, "  体检项:"]
    for c in checks:
        mark = "✅" if c["ok"] else ("❌" if c["level"] == "error" else "⚠️")
        lines.append(f"   {mark} {c['name']}: {c['detail']}")
    lines += ["-" * 68, "  FLV 结构（录制样本）:",
              f"   视频帧/音频帧: {scan.get('video_frames')}/{scan.get('audio_frames')}"
              f"  关键帧: {scan.get('keyframes')}",
              f"   GOP 中位/最大: {scan.get('gop_median_ms')} / {scan.get('gop_max_ms')} ms",
              f"   A/V 起始偏移(标签级): {scan.get('av_start_offset_ms')} ms",
              f"   样本: {sample_bytes} 字节 / {seconds}s",
              "-" * 68, f"  推流端: {push_name}" + (f"（{push_ver}）" if push_ver else ""),
              f"   encoder: {scan.get('encoder') or '—'}"]
    if deep:
        tl = deep.get("timestamp_level") or {}
        cl = deep.get("content_level") or {}
        lines += ["-" * 68, "  音画同步（深度分析）:",
                  f"   时间戳级: {tl.get('stream_offset_ms')} ms（{tl.get('direction')}）"
                  f" / 解码级 {tl.get('decode_mean_ms')} ms",
                  f"   内容级（人脸嘴型）: {cl.get('content_offset_ms')} ms"
                  f"（置信度 {cl.get('confidence')}, {cl.get('verdict') or '—'}）"]
        if deep.get("diagnosis"):
            lines.append(f"   联合诊断: {deep['diagnosis']}")
    lines.append("=" * 68)
    return "\n".join(lines)


# ─────────────────────────── 主流程 ───────────────────────────

def run(args) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    CAPTURES.mkdir(parents=True, exist_ok=True)
    report_dir = Path(args.report_dir) / f"rtmp_report_{ts}"
    report_dir.mkdir(parents=True, exist_ok=True)

    target = (args.target or "").strip()
    m = RTMP_RE.search(target)
    if not m:
        print("❌ 请提供 RTMP 地址，形如 rtmp://host[:port]/app/stream")
        return 2
    url = m.group(0)

    print(f"════ Step 1/4 探测 ════\n{url}")
    probe = probe_stream(url, timeout=args.timeout)
    if probe["ok"]:
        v = probe["video"]
        print(f"✅ 连通（{probe['elapsed_ms']} ms） 视频={v.get('codec')} "
              f"{v.get('width')}x{v.get('height')} @{v.get('fps')}fps "
              f"音频={(probe['audio'] or {}).get('codec')}")
    else:
        print(f"❌ 探测失败: {probe.get('error')}")

    print(f"\n════ Step 2/4 录制样本（{args.seconds}s）════")
    sample_path = CAPTURES / f"rtmp_sample_{ts}.flv"
    sample_bytes = 0 if args.no_record else record_sample(url, sample_path, args.seconds,
                                                          timeout=args.timeout)
    if sample_bytes:
        print(f"✅ 样本: {sample_path.name}（{sample_bytes} 字节）")
    else:
        print("⚠️ 未录制到样本（可能无人推流）；仅基于探测结果出报告")

    print("\n════ Step 3/4 FLV 结构 / GOP / 音画偏移 ════")
    scan: dict = {}
    deep: dict = {}
    if sample_bytes:
        try:
            scan = scan_flv(sample_path.read_bytes())
            print(f"   帧: 视频 {scan['video_frames']} / 音频 {scan['audio_frames']}；"
                  f"关键帧 {scan['keyframes']}；GOP 中位 {scan['gop_median_ms']} ms；"
                  f"A/V 起始偏移 {scan['av_start_offset_ms']} ms")
            push, ver = infer_push(scan.get("encoder"))
            print(f"   推流端: {push}" + (f"（{ver}）" if ver else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ FLV 解析失败: {exc}")
        if unified_available() and not args.no_deep:
            print("   深度分析（统一项目 core）: 时间戳级 + 内容级音画同步…")
            deep = deep_av_sync(sample_path, args.seconds)
            tl = deep.get("timestamp_level") or {}
            cl = deep.get("content_level") or {}
            print(f"     - 时间戳级: {tl.get('stream_offset_ms')} ms"
                  f" | 内容级: {cl.get('content_offset_ms')} ms（{cl.get('verdict') or '—'}）")
        else:
            print("   （未找到 ../live-stream-trace/core，跳过深度音画同步分析）")

    checks = build_checks(probe, scan, sample_bytes, args.seconds)
    verdict, level = overall_verdict(checks)

    print("\n════ Step 4/4 报告 ════")
    html_path = report_dir / "index.html"
    txt_path = report_dir / "report.txt"
    if args.format in ("html", "both"):
        html_path.write_text(render_html(ts, url, probe, scan, checks, deep,
                                         sample_path.name if sample_bytes else "",
                                         sample_bytes, args.seconds, report_dir),
                             encoding="utf-8")
    if args.format in ("text", "both"):
        txt_path.write_text(render_text(ts, url, probe, scan, checks, deep,
                                        sample_bytes, args.seconds), encoding="utf-8")
    summary = {"type": "rtmp_report", "timestamp": ts, "url": url, "overall": verdict,
               "level": level, "probe": probe, "flv_scan": scan, "checks": checks,
               "deep_av_sync": deep, "sample_bytes": sample_bytes, "seconds": args.seconds}
    (CAPTURES / f"rtmp_report_{ts}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n── 汇总 ──")
    for c in checks:
        mark = "✅" if c["ok"] else ("❌" if c["level"] == "error" else "⚠️")
        print(f"  {mark} {c['name']}: {c['detail']}")
    print(f"\n总体结论: {verdict}（置信度 {level}）")
    if args.format in ("html", "both"):
        print(f"HTML 报告: {html_path}")
    if args.format in ("text", "both"):
        print(f"文本报告: {txt_path}")
    return 0 if probe.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="RTMP/RTMPS（H.264）源流体检")
    ap.add_argument("target", nargs="?", help="rtmp://host[:port]/app/stream")
    ap.add_argument("--url", help="同 target（也可用 --url 传）")
    ap.add_argument("--seconds", type=int, default=20, help="录制样本时长（默认 20s）")
    ap.add_argument("--timeout", type=int, default=25, help="探测/录制超时（默认 25s）")
    ap.add_argument("--no-record", action="store_true", help="只探测，不录制样本")
    ap.add_argument("--no-deep", action="store_true", help="跳过深度音画同步分析")
    ap.add_argument("--report-dir", default=str(REPORTS), help="报告输出目录")
    ap.add_argument("--format", choices=["html", "text", "both"], default="both")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if not args.target and args.url:
        args.target = args.url
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
