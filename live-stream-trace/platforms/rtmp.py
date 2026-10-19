"""RTMP / RTMPS 源流适配器（自定义直播源检测）。

用于「把推流地址直接交给工具」的场景，无需平台接口：

    ./run.sh rtmp://127.0.0.1:1935/live/streamkey
    ./run.sh "rtmps://cdn.example.com/app/key?token=xxx"
    ./run.sh --platform rtmp "rtmp://a.com/app/key"

能力：
  · 识别 rtmp:// / rtmps:// / rtmpt://；房间号取 host/app/stream（供报告关联）。
  · 探测（ffprobe）：连通性、建连耗时、视频编码（**校验 H.264**）、profile/level、分辨率、
    帧率、像素格式、B 帧、码率；音频编码/采样率/声道/码率；封装与编码器标签。
  · 采样：ffmpeg `-c copy` 录制 N 秒 FLV → 复用统一流水线的 FLV 元数据解析、推流端指纹、
    时间戳级音画同步与内容级（人脸嘴型）音画同步。
  · 播放：RTMP 不能在浏览器播放 → playable=False，报告提示用 VLC/ffplay。

说明：本适配器代表“通用 RTMP H.264 源流”这一类输入，与直播平台无关；
同一套分析/报告/播放器逻辑由 core 提供，无需改动 core。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from base import (LiveRoom, PlatformAdapter, PlatformMeta, StreamTier,
                  ffprobe_bin, format_client_version, infer_push_channel)

logger = logging.getLogger(__name__)

FFMPEG_CANDIDATES = (
    "/opt/ffmpeg/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
RTMP_URL_RE = re.compile(r"rtmps?t?://[^\s\"'<>\\]{4,500}", re.I)
RTTMP_PREFIXES = ("rtmp://", "rtmps://", "rtmpt://", "rtmpe://", "rtmpte://")


def ffmpeg_bin() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in FFMPEG_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def parse_rtmp(url: str) -> dict:
    """拆解 rtmp 地址 → host/port/app/stream/room_id。"""
    u = (url or "").strip()
    parsed = urlparse(u)
    app = stream = ""
    parts = [p for p in (parsed.path or "").split("/") if p]
    if parts:
        app = parts[0]
        stream = parts[-1] if len(parts) > 1 else ""
    host = parsed.hostname or ""
    port = parsed.port or (443 if u.lower().startswith("rtmps") else 1935)
    room_id = "/".join(x for x in (host, app, stream) if x) or host
    return {"url": u, "scheme": (parsed.scheme or "rtmp").lower(), "host": host,
            "port": port, "app": app, "stream": stream, "query": parsed.query,
            "room_id": room_id}


def probe_stream(url: str, timeout: int = 25) -> dict:
    """ffprobe 拉流探测（含建连耗时）。失败返回 ok=False + error，不抛异常。"""
    out: dict = {"ok": False, "error": None, "elapsed_ms": None,
                 "video": {}, "audio": {}, "format": {}, "tags": {}}
    exe = ffprobe_bin()
    if not exe:
        out["error"] = "ffprobe 不可用（未找到可执行文件）"
        return out
    cmd = [exe, "-v", "error", "-rw_timeout", str(int(timeout * 1_000_000)),
           "-show_streams", "-show_format", "-of", "json", url]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired:
        out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        out["error"] = f"探测超时（{timeout}s）：可能无人推流或端口不可达"
        return out
    out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        out["error"] = (err[-1] if err else "ffprobe 失败")[:240]
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
        tags = dict(s.get("tags") or {})
        out["tags"].update({k: v for k, v in tags.items() if k not in out["tags"]})
        if s.get("codec_type") == "video" and not out["video"]:
            out["video"] = {
                "codec": s.get("codec_name"), "profile": s.get("profile"),
                "level": s.get("level"), "width": s.get("width"), "height": s.get("height"),
                "fps": _fps(s.get("avg_frame_rate") or s.get("r_frame_rate")),
                "pix_fmt": s.get("pix_fmt"), "has_b_frames": s.get("has_b_frames"),
                "bit_rate": _num(s.get("bit_rate")),
                "field_order": s.get("field_order"),
            }
        elif s.get("codec_type") == "audio" and not out["audio"]:
            out["audio"] = {
                "codec": s.get("codec_name"), "profile": s.get("profile"),
                "sample_rate": _num(s.get("sample_rate")), "channels": s.get("channels"),
                "channel_layout": s.get("channel_layout"), "bit_rate": _num(s.get("bit_rate")),
            }
    out["ok"] = bool(out["video"] or out["audio"])
    if not out["ok"]:
        out["error"] = "已连上但未解析出任何音视频流"
    return out


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _fps(value) -> Optional[float]:
    try:
        num, _, den = str(value or "").partition("/")
        d = float(den or 1)
        return round(float(num) / d, 3) if d else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


CODEC_LABELS = {"h264": "H.264", "avc1": "H.264", "hevc": "HEVC/H.265", "h265": "HEVC/H.265"}


def codec_label(codec: str | None) -> str:
    """编码名展示化：h264 → H.264。"""
    if not codec:
        return "未知编码"
    return CODEC_LABELS.get(codec.lower(), codec.upper())


class RtmpAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="rtmp", name="RTMP 源流", color="#8b5cf6",
        live_url="", cta="RTMP 无网页播放器（请用 VLC/ffplay）",
    )
    samples_rtmp = True
    probe_timeout = 25
    capture_retries = 3          # RTMP 录制重试次数（真实源常瞬时不可达）
    capture_retry_interval = 2   # 每次重试间隔（秒）

    # ── 识别 ──
    def matches(self, text: str) -> bool:
        t = (text or "").strip().lower()
        return t.startswith(RTTMP_PREFIXES) or bool(RTMP_URL_RE.match(t))

    # ── 解析 ──
    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        m = RTMP_URL_RE.search(text or "")
        url = m.group(0) if m else (text or "").strip()
        parsed = parse_rtmp(url)
        room = LiveRoom(platform="rtmp", room_id=parsed["room_id"])
        info = probe_stream(url, timeout=self.probe_timeout)
        v, a = info["video"], info["audio"]

        codec = (v.get("codec") or "").lower()
        is_h264 = codec in ("h264", "avc1")
        note = "浏览器不支持 RTMP，播放请用 ./play_source.sh --rtmp 或 VLC/ffplay"
        if codec and not is_h264:
            note += f"；当前视频编码为 {codec}（非 H.264）"
        tier = StreamTier(
            label=f"源流（RTMP·{codec_label(codec)}）",
            protocol="rtmp", url=url, ok=info["ok"], status=info.get("error"),
            codec=v.get("codec"), width=v.get("width"), height=v.get("height"),
            bitrate=v.get("bit_rate") or info["format"].get("bit_rate"),
            quality_key="source", playable=False, note=note,
        )
        room.rtmp = [tier]
        room.status = "直播中" if info["ok"] else "未推流 / 不可达"

        room.metrics = {
            "endpoint": f"{parsed['host']}:{parsed['port']}",
            "app": parsed["app"],
            "stream": parsed["stream"],
            "video_codec": v.get("codec") or "—",
            "is_h264": "是" if is_h264 else ("否" if codec else "未知"),
            "resolution": (f"{v.get('width')}x{v.get('height')}"
                           if v.get("width") and v.get("height") else "—"),
            "fps": v.get("fps") or "—",
            "video_bitrate_kbps": (round(v["bit_rate"] / 1000) if v.get("bit_rate") else "—"),
            "total_bitrate_kbps": (round(info["format"]["bit_rate"] / 1000)
                                   if info["format"].get("bit_rate") else "—"),
            "audio_codec": a.get("codec") or "—",
            "probe_ms": info.get("elapsed_ms"),
        }

        enc = info["tags"].get("encoder") or info["tags"].get("ENCODER") or ""
        meta_tags = {"encoder": enc} if enc else {}
        if meta_tags:
            room.push = {"channel": infer_push_channel(meta_tags),
                         "version": format_client_version(meta_tags) or "",
                         "server": info["tags"].get("server", ""),
                         "third_party": True}
        else:
            room.push = {"channel": "未知（RTMP 源流未上报 encoder 标签）", "version": ""}

        if not info["ok"]:
            room.notes.append(f"拉流探测失败：{info.get('error')}")
        if codec and not is_h264:
            room.notes.append(f"⚠️ 视频编码为 {codec}：本工具关注 H.264 流，"
                              "现有分析对 HEVC 仍可用，但网页播放器不可播。")
        room.notes.append("RTMP 源流没有网页直播间，报告不提供浏览器内播放（请用 VLC/ffplay）。")
        room.raw = {"probe": info, "parsed": parsed}
        return room

    # ── 采样（ffmpeg 录制，供 FLV 分析 / 音画同步复用）──
    def capture_sample(self, tier, dest_path, *, seconds: int = 20, max_bytes: int = 0,
                       headers: Optional[dict] = None, room_id: str = "") -> int:
        exe = ffmpeg_bin()
        if not exe or not tier:
            return 0
        dest = Path(dest_path)
        cmd = [exe, "-v", "error", "-nostdin", "-rw_timeout",
               str(int(self.probe_timeout * 1_000_000)),
               "-i", tier.url, "-t", str(max(3, int(seconds))), "-c", "copy", "-f", "flv",
               "-y", str(dest)]
        # RTMP 源常见“尚未推流 / 瞬时不可达 / 边缘重启” → 重试（每次前重连）
        for attempt in range(1, self.capture_retries + 1):
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=int(seconds) + 40, check=False)
            except subprocess.TimeoutExpired:
                logger.warning("RTMP 录制超时（第 %d 次）", attempt)
                continue
            size = dest.stat().st_size if dest.exists() else 0
            if size > 0:
                return size
            logger.warning("RTMP 录制失败（第 %d/%d 次）: %s", attempt, self.capture_retries,
                           proc.stderr.decode("utf-8", "replace")[-160:].strip())
            if attempt < self.capture_retries:
                time.sleep(self.capture_retry_interval)
        return 0

    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> Optional[str]:
        # RTMP 地址不随会话签名过期，无需刷新
        return None


ADAPTER = RtmpAdapter()
