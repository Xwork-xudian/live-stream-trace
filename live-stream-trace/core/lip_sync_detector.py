#!/usr/bin/env python3
"""人脸嘴型音画同步检测器（内容级 / content-level A/V sync）。

与 av_sync_detector 的分工（关键，勿混淆）：

  · av_sync_detector 测【时间戳级】偏移：FLV tag PTS、AAC 帧时长、ffmpeg 解码 A-V 差值。
    它只能发现“封装 / 时间戳”层面的不同步。

  · 本模块测【内容级】偏移：直接比较画面里“嘴在动”和声音“在响”是否对齐，完全不看时间戳。
    因此：
      - 采集/编码链（摄像头、采集卡、声卡、前置处理）在“进 OBS 之前”引入的不同步，
        会被烘焙进画面与声音内容。此时推流器写出的时间戳是自洽的 →
        时间戳级检测报 ~0 ms（漏报），而内容级检测能如实测出该偏移（本模块的价值）。
      - 反之，时间戳级偏移大而内容级正常 → 属 mux/播放器层面问题，多数播放器可自动修正。

方法：
  1. ffmpeg 抽帧（灰度、降采样、固定 fps）与抽音（16k 单声道 PCM）；
  2. 人脸检测（OpenCV Haar）+ 嘴部 ROI；或使用手动 ROI（--roi，用于无脸/测试场景）；
  3. 嘴部运动能量 m(t)：相邻帧 ROI 内亮度绝对差均值（嘴动 → 能量峰）；
  4. 音频包络 a(t)：按帧率分窗 RMS；
  5. 归一化互相关求最佳滞后 → 内容级音画偏移（毫秒），抛物线插值取亚帧精度；
  6. 分段时间一致性 + 峰值相关系数 → 置信度（高/中/低）。

符号约定：offset_ms > 0 表示【音频超前画面】（先听到、后看到），与 av_sync_detector 一致。

用法:
    python lip_sync_detector.py --file captures/xxx.flv
    python lip_sync_detector.py --file xxx.flv --roi 0.35,0.55,0.30,0.35   # 手动 ROI（相对坐标 x,y,w,h）
    python lip_sync_detector.py --url "http://..." --seconds 20 --fps 25
输出: captures/lip_sync_analysis_<ts>.json + captures/lip_sync_report_<ts>.txt
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from config import OUTPUT_DIR, ensure_dirs
except ImportError:  # 允许独立运行
    OUTPUT_DIR = Path("captures")
    def ensure_dirs():
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

AUDIO_RATE = 16000
FFMPEG_CANDIDATES = ("/opt/ffmpeg/bin/ffmpeg",)
FFPROBE_CANDIDATES = ("/opt/ffmpeg/bin/ffprobe",)

DEFAULT_FPS = 25
FINE_FPS = 200          # 互相关细网格帧率（5ms 分辨力）
ROI_FPS_CAP = 60        # ROI 抽帧上限（按源帧率抽帧，不做倍帧上采样）
DEFAULT_SECONDS = 20
MAX_ANALYZE_WIDTH = 320
MAX_LAG_MS = 600            # 互相关搜索范围 ±600 ms（覆盖常见采集链延时）
MIN_FRAMES = 12             # 少于此帧数不出结论

# 与 av_sync_detector 保持一致的感知门限（ITU-R BT.1359-1 / EBU R37）
PERCEPTUAL_ITU = {"audio_lead_detect_ms": 45, "audio_lag_detect_ms": -125,
                  "audio_lead_accept_ms": 90, "audio_lag_accept_ms": -185}
PERCEPTUAL_EBU_R37 = {"audio_lead_ms": 40, "audio_lag_ms": -60}


# ─────────────────────────── 可执行文件定位 ───────────────────────────

def _resolve(bin_name: str, candidates: tuple) -> Optional[str]:
    exe = shutil.which(bin_name)
    if exe:
        return exe
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def resolve_ffmpeg() -> Optional[str]:
    return _resolve("ffmpeg", FFMPEG_CANDIDATES)


def resolve_ffprobe() -> Optional[str]:
    return _resolve("ffprobe", FFPROBE_CANDIDATES)


def _cv2():
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenCV 不可用（人脸检测需要）: %s", exc)
        return None


def _np():
    try:
        import numpy as np  # noqa: PLC0415
        return np
    except Exception as exc:  # noqa: BLE001
        logger.warning("NumPy 不可用: %s", exc)
        return None


# ─────────────────────────── 媒体探测 / 抽取 ───────────────────────────

def probe_media(path: str | Path, ffprobe: Optional[str] = None) -> dict:
    """返回 {duration, width, height, has_audio, has_video, vcodec, acodec, fps}。"""
    ffprobe = ffprobe or resolve_ffprobe()
    info = {"duration": 0.0, "width": 0, "height": 0,
            "has_audio": False, "has_video": False, "vcodec": None, "acodec": None, "fps": None}
    if not ffprobe:
        return info
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=40, check=False)
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe 失败: %s", exc)
        return info
    for s in data.get("streams", []) or []:
        if s.get("codec_type") == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
            info["vcodec"] = s.get("codec_name")
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/0"
            try:
                num, _, den = rate.partition("/")
                if float(den or 1) > 0:
                    info["fps"] = round(float(num) / float(den or 1), 3)
            except (TypeError, ValueError):
                pass
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["acodec"] = s.get("codec_name")
    try:
        info["duration"] = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        info["duration"] = 0.0
    if info["duration"] <= 0.05:
        dur = _ffmpeg_duration_s(path)         # 优先用 ffmpeg 解码器读到的时长
        if dur <= 0.05 and Path(path).suffix.lower() == ".flv":
            dur = flv_duration_s(path)                 # 最后兜底：FLV tag 首尾差
        if dur > 0:
            info["duration"] = round(dur + 1.0 / max(info.get("fps") or 25.0, 1.0), 2)
            info["duration_estimated"] = True
    return info


def _ffmpeg_duration_s(path: str | Path, ffmpeg: Optional[str] = None) -> float:
    """从 `ffmpeg -i` 的 stderr 解析 Duration（ffprobe JSON 缺 duration 时的兜底）。"""
    exe = ffmpeg or resolve_ffmpeg()
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, timeout=60)
        text = (r.stderr or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        return 0.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def flv_duration_s(path: str | Path) -> float:
    """扫描 FLV tag 时间戳估算时长（抖音/小红书直播录制常缺 duration 元数据）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0.0
    if len(data) < 13 or data[:3] != b"FLV":
        return 0.0
    # 注意：直播 FLV 时间戳可能是“流上行绝对时间”，且开头常带一个 ts=0 的复位 tag，
    # 随后跳到绝对时间（例如 0 → 8161266 ms）。若直接取「末-首」会把时长放大上千倍
    # （实测 7.4s 被判成 8168s），进而使 ensure_lip_sample 误判“样本已够长”而跳过补采。
    # 故改为按相邻 tag 时间戳差 >1s 切分连续段，取最长段时长（与 av_sync_detector._longest_segment 口径一致）。
    stamps: list[int] = []
    pos, n = 13, len(data)
    while pos + 11 <= n:
        ttype = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if size <= 0 or pos + 11 + size > n:
            break
        if ttype in (8, 9):
            stamps.append(int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24))
        pos += 11 + size + 4
    if not stamps:
        return 0.0
    best_ms = 0
    seg_min = seg_max = prev = stamps[0]
    for ts in stamps[1:]:
        if abs(ts - prev) > 1000:          # 时间戳跳变（CDN 重写/编码器复位）→ 另起一段
            best_ms = max(best_ms, seg_max - seg_min)
            seg_min = seg_max = ts
        else:
            seg_min = min(seg_min, ts)
            seg_max = max(seg_max, ts)
        prev = ts
    best_ms = max(best_ms, seg_max - seg_min)
    if best_ms <= 0:
        return 0.0
    return best_ms / 1000.0


def extract_audio_pcm(path: str | Path, seconds: float, ffmpeg: Optional[str] = None):
    """抽取单声道 16k PCM → float32 ndarray（-1..1）。失败返回 None。"""
    np = _np()
    ffmpeg = ffmpeg or resolve_ffmpeg()
    if not np or not ffmpeg:
        return None
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-t", f"{seconds:.3f}",
           "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("抽取音频失败: %s", exc)
        return None
    raw = out.stdout
    if len(raw) < AUDIO_RATE:      # <0.5s 视为失败
        return None
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return pcm


def extract_video_frames(path: str | Path, seconds: float, fps: int, size: tuple,
                         ffmpeg: Optional[str] = None):
    """抽灰度帧 → uint8 (N, H, W)；size=(W,H) 已按比例缩放。失败返回 None。"""
    np = _np()
    ffmpeg = ffmpeg or resolve_ffmpeg()
    if not np or not ffmpeg:
        return None
    w, h = size
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-t", f"{seconds:.3f}",
           "-vf", f"fps={fps},scale={w}:{h}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("抽帧失败: %s", exc)
        return None
    buf = out.stdout
    frame_bytes = w * h
    n = len(buf) // frame_bytes
    if n < 2:
        return None
    frames = np.frombuffer(buf[:n * frame_bytes], dtype=np.uint8).reshape(n, h, w)
    return frames


def scaled_size(width: int, height: int, max_width: int = MAX_ANALYZE_WIDTH) -> tuple:
    if width <= 0 or height <= 0:
        return (max_width, max_width)
    w = min(width, max_width)
    h = max(2, int(round(height * w / width)))
    return (w, h)


# ─────────────────────────── 人脸 / 嘴部 ROI ───────────────────────────

def _load_cascades() -> list:
    """加载人脸级联（多个，按优先级）。多一路备选级联可显著提升真实直播流的检出率。"""
    cv2 = _cv2()
    if not cv2:
        return []
    try:
        import cv2.data  # noqa: PLC0415
        root = Path(cv2.data.haarcascades)
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法定位 Haar 级联目录: %s", exc)
        return []
    out = []
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml",
                 "haarcascade_profileface.xml"):
        path = root / name
        if not path.exists():
            continue
        try:
            c = cv2.CascadeClassifier(str(path))
            if not c.empty():
                out.append(c)
        except Exception:  # noqa: BLE001
            continue
    if not out:
        logger.warning("未找到可用的人脸级联（%s）", root)
    return out


def _load_cascade():
    cs = _load_cascades()
    return cs[0] if cs else None


def rois_from_faces_multi(faces_multi, shape, k: int = 0):
    """延迟导入 face_backend.rois_from_faces_multi（无该模块时退回人脸框比例法）。"""
    try:
        from face_backend import rois_from_faces_multi as _fn  # noqa: PLC0415
        return _fn(faces_multi, shape, k)
    except Exception:  # noqa: BLE001
        picked = [(fl[k] if len(fl) > k else None) for fl in faces_multi]
        boxes = [(f["box"] if f else None) for f in picked]
        r, cov, jit = boxes_to_mouth_rois(boxes, shape)
        return r, cov, jit, None


_FACE_BACKEND = None


def get_face_backend():
    """人脸后端单例：YuNet（cv2.dnn，带 5 点关键点）优先，模型缺失时退化 Haar。

    返回 face_backend.FaceBackend 或 None（两者都不可用时）。
    """
    global _FACE_BACKEND
    if _FACE_BACKEND is None:
        try:
            from face_backend import FaceBackend  # noqa: PLC0415
            _FACE_BACKEND = FaceBackend()
        except Exception as exc:  # noqa: BLE001
            logger.warning("人脸后端不可用: %s", exc)
            _FACE_BACKEND = False
    return _FACE_BACKEND or None


def detect_faces(frames, cascades) -> list:
    """逐帧人脸检测 → list[tuple(x,y,w,h)|None]（取最大脸）。

    依次尝试「多个级联 × 两组参数」，命中即止；真实直播流（人物偏小/侧脸/光照差）
    单路检测常漏，多路组合能明显提高覆盖率。
    """
    cv2 = _cv2()
    if isinstance(cascades, (list, tuple)):
        cascade_list = list(cascades)
    else:                                   # 兼容传入单个级联
        cascade_list = [cascades] if cascades is not None else []
    n, h, w = frames.shape
    params = [
        (1.10, 5, (max(12, int(w * 0.10)), max(12, int(h * 0.10)))),
        (1.05, 4, (max(10, int(w * 0.07)), max(10, int(h * 0.07)))),
    ]
    boxes = []
    for i in range(n):
        face = None
        for cascade in cascade_list:
            for scale, neighbors, min_size in params:
                try:
                    found = cascade.detectMultiScale(frames[i], scaleFactor=scale,
                                                     minNeighbors=neighbors, minSize=min_size,
                                                     flags=cv2.CASCADE_SCALE_IMAGE)
                except Exception:  # noqa: BLE001
                    continue
                if len(found):
                    face = max(((int(x), int(y), int(bw), int(bh)) for x, y, bw, bh in found),
                               key=lambda b: b[2] * b[3])
                    break
            if face:
                break
        boxes.append(face)
    return boxes


def detect_faces_multi(frames, cascades, top_k: int = 2) -> list:
    """逐帧人脸检测 → list[list[box]]（按面积降序，最多 top_k 张）。

    商铺/带货直播常见「主播 + 画中画模特」双脸；只取最大脸可能锁定到没说话的那张。
    """
    cv2 = _cv2()
    if isinstance(cascades, (list, tuple)):
        cascade_list = list(cascades)
    else:
        cascade_list = [cascades] if cascades is not None else []
    n, h, w = frames.shape
    params = [
        (1.10, 5, (max(12, int(w * 0.10)), max(12, int(h * 0.10)))),
        (1.05, 4, (max(10, int(w * 0.07)), max(10, int(h * 0.07)))),
    ]
    out = []
    for i in range(n):
        found_all = []
        for cascade in cascade_list:
            for scale, neighbors, min_size in params:
                try:
                    found = cascade.detectMultiScale(frames[i], scaleFactor=scale,
                                                     minNeighbors=neighbors, minSize=min_size,
                                                     flags=cv2.CASCADE_SCALE_IMAGE)
                except Exception:  # noqa: BLE001
                    continue
                found_all.extend((int(x), int(y), int(bw), int(bh)) for x, y, bw, bh in found)
                if found_all:
                    break
            if found_all:
                break
        found_all.sort(key=lambda b: b[2] * b[3], reverse=True)
        out.append(found_all[:top_k])
    return out


def boxes_to_mouth_rois(boxes, shape: tuple) -> tuple:
    """人脸框 → 嘴部 ROI。返回 (rois(N,4) = x0,y0,x1,y1, coverage, jitter_px)。

    缺失帧用最近的有效框填充；无有效框时 rois 为空。
    """
    np = _np()
    h, w = shape
    valid_idx = [i for i, b in enumerate(boxes) if b]
    coverage = len(valid_idx) / max(1, len(boxes))
    if not valid_idx:
        return None, coverage, 0.0
    last = None
    filled = []
    for i, b in enumerate(boxes):
        if b:
            last = b
        filled.append(last)
    # 反向补全前导缺失
    first = next((b for b in filled if b), None)
    filled = [b if b else first for b in filled]
    rois = np.zeros((len(filled), 4), dtype=np.float32)
    for i, (x, y, bw, bh) in enumerate(filled):
        x0 = x + 0.25 * bw
        y0 = y + 0.62 * bh
        x1 = x + 0.75 * bw
        y1 = y + 0.95 * bh
        rois[i] = (max(0, x0), max(0, y0), min(w, x1), min(h, y1))
    centers = np.array([[x + bw / 2, y + bh / 2] for x, y, bw, bh in
                        [b for b in boxes if b]], dtype=np.float32)
    jitter = float(centers.std(axis=0).mean()) if len(centers) > 1 else 0.0
    return rois, coverage, jitter


def roi_from_spec(spec, shape: tuple, n_frames: int):
    """'x,y,w,h'（相对 0..1）→ rois(N,4)。"""
    np = _np()
    h, w = shape
    x, y, rw, rh = (float(v) for v in spec)
    x0, y0 = x * w, y * h
    x1, y1 = (x + rw) * w, (y + rh) * h
    return np.tile(np.array([x0, y0, x1, y1], dtype=np.float32), (n_frames, 1))


# ─────────────────────────── 信号构建 ───────────────────────────

def control_rois_from(roi_box, shape: tuple, max_controls: int = 3) -> list:
    """由嘴部 ROI 生成若干「同尺寸对照区域」（左/右/下/上偏移；需在画面内且不与嘴部重叠）。

    用途：嘴型特异性对照。若对照区域的相关性与嘴部相当，说明测到的是画面整体运动/BGM 耦合，
    而不是真正的嘴型同步（商铺直播实测过这种伪可信结果）。
    """
    h, w = shape
    x0, y0, x1, y1 = (float(v) for v in roi_box)
    bw, bh = max(8.0, x1 - x0), max(8.0, y1 - y0)
    cands = ((x0 - 1.6 * bw, y0), (x0 + 1.6 * bw, y0),
             (x0, y0 + 1.8 * bh), (x0, y0 - 1.8 * bh))
    out: list = []
    for cx, cy in cands:
        a0, b0, a1, b1 = cx, cy, cx + bw, cy + bh
        if a0 < 0 or b0 < 0 or a1 > w or b1 > h:
            continue
        if not (a1 <= x0 or a0 >= x1 or b1 <= y0 or b0 >= y1):
            continue
        out.append((a0, b0, a1, b1))
        if len(out) >= max_controls:
            break
    return out


def control_rois_from_face(face_box_xywh, mouth_roi, shape: tuple, max_controls: int = 3) -> list:
    """由**人脸框**(x, y, w, h) + 嘴部 ROI(x0,y0,x1,y1) 生成同尺寸对照区，尽量落在**脸外**。

    为什么需要单独的函数：`control_rois_from` 接受的是 (x0,y0,x1,y1)，而人脸检测输出的是
    (x,y,w,h)。直接混用会把 `w/h` 当成 `x1/y1`，算出负宽高 → 对照区退化成 8×8 小噪声块，
    特异性判据被系统性误判（实测真实直播间全部踩此坑）。本函数显式做坐标换算。
    """
    h, w = shape
    fx, fy, fw, fh = (float(v) for v in face_box_xywh)
    mx0, my0, mx1, my1 = (float(v) for v in mouth_roi)
    bw, bh = max(8.0, mx1 - mx0), max(8.0, my1 - my0)
    cands = (
        (fx - 1.2 * bw, my0),                    # 人脸左侧（脸外）
        (fx + fw + 0.2 * bw, my0),               # 人脸右侧（脸外）
        (mx0, fy + fh + 0.2 * bh),               # 人脸框下方（胸前/桌面）
        (mx0, fy - 1.2 * bh),                    # 人脸框上方（背景/头顶）
        (fx - 1.2 * bw, fy),                     # 肩部两侧
        (fx + fw + 0.2 * bw, fy),
    )
    out: list = []
    for cx, cy in cands:
        a0, b0, a1, b1 = cx, cy, cx + bw, cy + bh
        if a0 < 0 or b0 < 0 or a1 > w or b1 > h:
            continue
        if not (a1 <= mx0 or a0 >= mx1 or b1 <= my0 or b0 >= my1):   # 不与嘴部重叠
            continue
        out.append((a0, b0, a1, b1))
        if len(out) >= max_controls:
            break
    if not out:                              # 实在找不到脸外位置 → 退回嘴部周边的通用做法
        return control_rois_from(mouth_roi, shape, max_controls)
    return out


def mouth_motion_signal(frames, rois) -> tuple:
    """嘴部运动能量 m(t) 与“张口对比度” o(t)。"""
    np = _np()
    n = frames.shape[0]
    m = np.zeros(n - 1, dtype=np.float32)
    o = np.zeros(n, dtype=np.float32)
    for i in range(n):
        x0, y0, x1, y1 = (int(round(v)) for v in rois[i])
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(frames.shape[2], max(x0 + 2, x1)), min(frames.shape[1], max(y0 + 2, y1))
        cur = frames[i, y0:y1, x0:x1]
        if cur.size == 0:
            continue
        prof = cur.mean(axis=1)
        o[i] = float(prof.max() - prof.min()) / 255.0
        if i > 0:
            prev = frames[i - 1, y0:y1, x0:x1]
            if prev.shape == cur.shape and cur.size:
                d = np.abs(cur.astype(np.int16) - prev.astype(np.int16))
                m[i - 1] = float(d.mean())
    return m, o


def audio_envelope(pcm, fps: int) -> "object":
    """按帧率分窗 RMS 包络（长度 ≈ 秒数×fps）。"""
    np = _np()
    n = max(1, int(round(AUDIO_RATE / fps)))
    k = len(pcm) // n
    if k < 2:
        return np.zeros(0, dtype=np.float32)
    seg = pcm[:k * n].reshape(k, n)
    return np.sqrt((seg ** 2).mean(axis=1)).astype(np.float32)


def band_limit(pcm, lo: float = 250.0, hi: float = 3400.0, sr: int = AUDIO_RATE):
    """语音频带限（去 BGM 低频轰鸣与高频噪声），显著提升「嘴动 ↔ 语音」相关性。"""
    np = _np()
    n = len(pcm)
    if n < 256:
        return pcm
    spec = np.fft.rfft(pcm)
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    keep = (freqs >= lo) & (freqs <= hi)
    spec[~keep] = 0
    return np.fft.irfft(spec, n=n).astype(np.float32)


def audio_envelope_fine(pcm, fps: int, win_ms: float = 25.0, band: bool = True):
    """高分辨率音频包络：步长 1/fps、窗口居中 → (env, 时间轴秒)。

    25fps 逐帧 RMS 只有 40ms 粒度，与音节的 4~6Hz 结构拍频，互相关会在 lag 轴上
    剧烈振荡（混叠）→ 峰值随机。细网格 + 频带限可同时解决混叠与 BGM 干扰。
    """
    np = _np()
    if band:
        pcm = band_limit(pcm)
    hop = max(1, int(round(AUDIO_RATE / fps)))
    win = max(hop, int(round(AUDIO_RATE * win_ms / 1000.0)))
    if len(pcm) <= win + hop:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    n = (len(pcm) - win) // hop
    if n < 8:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    p = pcm.astype(np.float64) ** 2
    c = np.concatenate(([0.0], np.cumsum(p)))
    starts = np.clip(np.arange(n) * hop - win // 2, 0, len(pcm) - win)
    e = (c[starts + win] - c[starts]) / float(win)
    return (np.sqrt(np.maximum(e, 0.0)).astype(np.float32),
            (starts + win / 2.0) / float(AUDIO_RATE))


def iter_color_frames(path, seconds: float, fps: int, size: tuple, ffmpeg: Optional[str] = None,
                      chunk_s: float = 4.0):
    """分块抽彩色帧（bgr24）→ 生成 (chunk_start_s, ndarray(N,H,W,3))，控制内存峰值。"""
    np = _np()
    exe = ffmpeg or resolve_ffmpeg()
    if np is None or not exe:
        return
    w, h = int(size[0]), int(size[1])
    fb = w * h * 3
    t0 = 0.0
    while t0 < seconds - 1e-6:
        dur = min(chunk_s, seconds - t0)
        cmd = [exe, "-v", "error", "-nostdin", "-ss", f"{t0:.3f}", "-i", str(path),
               "-t", f"{dur:.3f}", "-vf", f"fps={fps},scale={w}:{h}",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=300, check=False).stdout
        except Exception as exc:  # noqa: BLE001
            logger.warning("彩色抽帧失败: %s", exc)
            return
        n = len(out) // fb
        if n:
            yield t0, np.frombuffer(out[:n * fb], dtype=np.uint8).reshape(n, h, w, 3)
        t0 += dur


def detrend_series(x, win: int = 5):
    """去慢漂移（减移动均值）并 z-score 归一化；nan 用中位数填充。"""
    np = _np()
    if np is None or x is None or len(x) < 4:
        return None
    v = np.asarray(x, dtype=np.float64)
    v = np.nan_to_num(v, nan=float(np.nanmedian(v)))
    if win > 1 and len(v) > win:
        v = v - np.convolve(v, np.ones(win) / win, mode="same")
    sd = float(v.std())
    return ((v - v.mean()) / sd).astype(np.float32) if sd > 1e-9 else None


def extract_roi_frames(path, seconds: float, fps: int, box_src, out_size,
                       ffmpeg: Optional[str] = None):
    """按 ROI 裁剪后高帧率抽帧 → uint8 (N, H, W)。box_src=(x, y, w, h) 源像素坐标。

    只裁剪嘴部小区域，因此可以用 100fps 抽帧而开销可控（全帧 100fps 才是浪费）。
    """
    np = _np()
    ffmpeg = ffmpeg or resolve_ffmpeg()
    if not np or not ffmpeg:
        return None
    x, y, w, h = (int(round(v)) for v in box_src)
    ow, oh = out_size
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-t", f"{seconds:.3f}",
           "-vf", f"fps={fps},crop={w}:{h}:{x}:{y},scale={ow}:{oh}",
           "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ROI 裁剪抽帧失败: %s", exc)
        return None
    buf = out.stdout
    fb = ow * oh
    n = len(buf) // fb
    if n < 8:
        return None
    return np.frombuffer(buf[:n * fb], dtype=np.uint8).reshape(n, oh, ow)


def _pair_views(a, b, dx: int, dy: int):
    """取 b 相对 a 平移 (dx, dy) 后的重叠区域对（用于运动补偿）。"""
    h, w = a.shape
    x0, x1 = max(0, dx), min(w, w + dx)
    y0, y1 = max(0, dy), min(h, h + dy)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None, None
    return a[y0:y1, x0:x1], b[y0 - dy:y1 - dy, x0 - dx:x1 - dx]


def mouth_openness_signal(roi_frames, fps: int, band_frac: float = 0.45, detrend_s: float = 0.5):
    """唇线「暗带宽度」信号（内容级音画同步的第四变体）。

    动机：美颜磨皮会抹平嘴部的高频纹理（帧差能量被压平），但**唇线是强暗边**，平滑后依然存在。
    逐帧在嘴部 ROI 内取行均值剖面，阈值 = mean − band_frac·(mean − min)，统计低于阈值的行数占比：
    张口时上下唇分离、暗带变宽 → 信号上升。
    对信号做轻度去漂移（减去 0.5s 滑动均值），只保留与语音同速率的波动。
    """
    np = _np()
    n = int(roi_frames.shape[0]) if roi_frames is not None else 0
    if np is None or n < 4:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    prof = roi_frames.astype(np.float32).mean(axis=2)          # (n, H) 行均值剖面
    mn = prof.min(axis=1, keepdims=True)
    mean = prof.mean(axis=1, keepdims=True)
    thr = mean - band_frac * np.maximum(mean - mn, 1e-3)
    sig = (prof < thr).mean(axis=1).astype(np.float64)         # 暗带行数占比
    win = max(3, int(round(detrend_s * fps)))
    if win < n:
        sig = sig - np.convolve(sig, np.ones(win) / win, mode="same")
    return sig.astype(np.float32), (np.arange(n) + 0.5) / float(fps)


def mouth_motion_fine(roi_frames, fps: int, align: bool = True, max_shift: int = 6):
    """ROI 相邻帧差 → (运动能量, 时间轴秒, 对齐位移中位数)。

    默认**先平移对齐再求差**：真实直播里人物会整体移动，未对齐时帧差主要来自
    “人脸整体位移”而非嘴部开合，会把相关性直接打成噪声（商铺直播尤甚）。
    时间取区间中点，避免半帧系统偏差。
    """
    np = _np()
    n = int(roi_frames.shape[0])
    if n < 2:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32), 0.0
    m = np.zeros(n - 1, dtype=np.float32)
    if not align:
        d = np.abs(roi_frames[1:].astype(np.int16) - roi_frames[:-1].astype(np.int16))
        m = d.mean(axis=(1, 2)).astype(np.float32)
        return m, (np.arange(n - 1) + 0.5) / float(fps), 0.0
    small = roi_frames[:, ::2, ::2].astype(np.int16)
    prev_full = roi_frames[0].astype(np.int16)
    shifts: list = []
    for i in range(1, n):
        cur_full = roi_frames[i].astype(np.int16)
        best = (1e18, 0, 0)
        pa, pb = small[i - 1], small[i]
        for dy in range(-max_shift, max_shift + 1, 2):          # 粗搜（1/2 分辨率，步长 2）
            for dx in range(-max_shift, max_shift + 1, 2):
                a, b = _pair_views(pa, pb, dx, dy)
                if a is None:
                    continue
                c = float(np.abs(a - b).mean())
                if c < best[0]:
                    best = (c, dx, dy)
        _, bx, by = best
        for dy in (by - 1, by, by + 1):                         # 细化（全分辨率，±1）
            for dx in (bx - 1, bx, bx + 1):
                a, b = _pair_views(prev_full, cur_full, dx, dy)
                if a is None:
                    continue
                c = float(np.abs(a - b).mean())
                if c < best[0]:
                    best = (c, dx, dy)
        _, dx, dy = best
        a, b = _pair_views(prev_full, cur_full, dx, dy)
        if a is None:
            m[i - 1] = 0.0
        else:
            h, w = a.shape                                          # 只统计中央区域，避开边缘伪运动
            cy0, cy1 = h // 4, max(h // 4 + 1, 3 * h // 4)
            cx0, cx1 = w // 4, max(w // 4 + 1, 3 * w // 4)
            m[i - 1] = float(np.abs(a[cy0:cy1, cx0:cx1] - b[cy0:cy1, cx0:cx1]).mean())
        shifts.append(max(abs(dx), abs(dy)))
    t = (np.arange(n - 1) + 0.5) / float(fps)   # 区间中点，与音频窗口中心对齐
    return m, t, (float(np.median(shifts)) if shifts else 0.0)


def _source_roi_box(rois, scaled_shape, src_shape):
    """缩放帧内的多帧 ROI → 源分辨率下的固定裁剪框（偶数宽高、已裁剪到画面内）。"""
    np = _np()
    if rois is None or len(rois) == 0:
        return None
    sh, sw = scaled_shape
    src_w, src_h = int(src_shape[0] or 0), int(src_shape[1] or 0)
    if not src_w or not src_h or not sw or not sh:
        return None
    med = np.median(np.asarray(rois, dtype=np.float32), axis=0)     # x0, y0, x1, y1
    sx, sy = src_w / float(sw), src_h / float(sh)
    x0, y0, x1, y1 = med[0] * sx, med[1] * sy, med[2] * sx, med[3] * sy
    x = max(0, min(int(round(x0)), max(0, src_w - 8)))
    y = max(0, min(int(round(y0)), max(0, src_h - 8)))
    w = max(8, min(int(round(x1 - x0)) // 2 * 2, src_w - x))
    h = max(8, min(int(round(y1 - y0)) // 2 * 2, src_h - y))
    if w < 8 or h < 8:
        return None
    return (x, y, w, h)


OCC_REL = 0.55             # 可见性低于「本片段自身基线」的此比例 → 判为可能遮挡
OCC_FLOOR = 0.20           # 绝对下限，避免基线极低时满屏误报
OCC_FRACTION_GATE = 0.4    # 遮挡帧占比超过此值 → 直接影响判定措辞
TRACK_OUT_W = 192          # union 裁剪后的最大抽取宽度（限定跟踪通道开销）
TRACK_MAX_OH = 400         # union 抽取的最大高度（防止“高而窄”的 union 造成面积爆炸）


def occlusion_mask(vis_series, rel: float = OCC_REL, floor: float = OCC_FLOOR):
    """可见性序列 → (遮挡布尔掩码, 阈值, 基线)。

    绝对阈值不可跨内容迁移：美颜/柔光直播的整体可见性本就偏低（实测真实房间中位 0.43，
    用绝对 0.6 会误报约一半帧）。故改用「相对本片段自身可见性基线」的比例阈值，
    并设绝对下限防止基线极低时满屏误报。有效样本不足返回 (None, None, None)。
    """
    np = _np()
    if np is None or vis_series is None:
        return None, None, None
    fin = np.isfinite(vis_series)
    if int(fin.sum()) < 4:
        return None, None, None
    ref = float(np.median(vis_series[fin]))
    thr = max(floor, rel * ref)
    mask = np.zeros(len(vis_series), dtype=bool)
    mask[fin] = vis_series[fin] < thr
    return mask, thr, ref


def _longest_run(mask) -> int:
    """布尔数组里最长连续 True 的长度。"""
    np = _np()
    if np is None or mask is None or len(mask) == 0:
        return 0
    best = cur = 0
    for v in np.asarray(mask).tolist():
        cur = cur + 1 if v else 0
        if cur > best:
            best = cur
    return best


def _inlier_mask(v, k: float = 3.5):
    """MAD 内点掩码（剔除离群值；MAD≈0 时全保留）。"""
    np = _np()
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    if mad <= 1e-6:
        return np.ones(len(v), dtype=bool)
    return np.abs(v - med) <= k * 1.4826 * mad


def _track_geometry(rois, scaled_shape, src_shape, margin_frac: float = 0.25,
                    win_scale: tuple = (1.5, 1.6)):
    """逐帧 ROI（缩放坐标）→ (跟踪中心(源像素 N×2), union 裁剪框(源像素), 取样窗(源像素))。

    用 5~95 分位（抗离群误检）而不是 min/max 求包络，避免单帧误检把裁剪框撑到全屏。
    union 的边距**保证取样窗始终能居中**（否则窗口会被夹在边缘 → 取样错位）。
    返回 (None, None, None) 表示不可用。
    """
    np = _np()
    if np is None or rois is None or len(rois) < 2:
        return None, None, None
    sh, sw = scaled_shape
    src_w, src_h = int(src_shape[0] or 0), int(src_shape[1] or 0)
    if not src_w or not src_h or not sw or not sh:
        return None, None, None
    sx, sy = src_w / float(sw), src_h / float(sh)
    arr = np.asarray(rois, dtype=np.float64)
    x0, y0, x1, y1 = arr[:, 0] * sx, arr[:, 1] * sy, arr[:, 2] * sx, arr[:, 3] * sy
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rw, rh = float(np.median(x1 - x0)), float(np.median(y1 - y0))
    span = max(rw, rh, 8.0)
    w_win, h_win = float(win_scale[0]) * rw, float(win_scale[1]) * rh
    # 边距 ≥ 半窗 + 4px，确保取样窗在中心极值处也能完整落在 union 内
    pad_x = max(rw / 2.0 + margin_frac * span, w_win / 2.0 + 4.0)
    pad_y = max(rh / 2.0 + margin_frac * span, h_win / 2.0 + 4.0)

    def _env(mask):
        lx, hx = float(cx[mask].min()), float(cx[mask].max())
        ly, hy = float(cy[mask].min()), float(cy[mask].max())
        ux0, uy0 = max(0.0, lx - pad_x), max(0.0, ly - pad_y)
        ux1, uy1 = min(float(src_w), hx + pad_x), min(float(src_h), hy + pad_y)
        uw, uh = int(ux1 - ux0), int(uy1 - uy0)
        return (int(ux0), int(uy0), uw - uw % 2, uh - uh % 2)

    # 默认用**全量**包络：正常轨迹（含镜头切换造成的双峰）都应完整覆盖，避免取样被夹。
    allm = np.ones(len(cx), dtype=bool)
    union = _env(allm)
    # 只有当全量包络大到接近整帧（检测散布/误检）时，才退回 MAD 内点包络
    if union[2] > 0.7 * src_w or union[3] > 0.7 * src_h or union[2] < 16 or union[3] < 16:
        mask = _inlier_mask(cx, 6.0) & _inlier_mask(cy, 6.0)
        if int(mask.sum()) >= 2:
            union2 = _env(mask)
            if union2[2] >= 16 and union2[3] >= 16:
                union = union2
    if union[2] < 16 or union[3] < 16:
        return None, None, None
    centers = np.stack([cx, cy], axis=1)
    return centers, union, (w_win, h_win)


def extract_tracked_roi_frames(path, seconds: float, fps: int, union_box, out_w: int,
                               centers_src, centers_fps: float, win_src, ffmpeg=None):
    """union 裁剪 + 逐帧按跟踪中心取样 → (crops(N,MH,MW), meta)。

    比「固定中位 ROI + ±6px 平移补偿」稳健得多：带货直播里主播会走动/镜头推拉，
    固定框会让嘴部整体滑出裁剪区（实测源坐标中心位移可达数百像素），
    跟踪取样把「人物整体移动」从信号里消掉，只留下嘴部开合。
    `win_src=(w,h)` 为期望取样窗的**源像素**尺寸；内部按 union 缩放比例换算到提取图坐标。
    """
    np = _np()
    if np is None or centers_src is None or len(centers_src) < 2:
        return None, {}
    x, y, w, h = (int(round(v)) for v in union_box)
    if w < 16 or h < 16:
        return None, {}
    # 同时限制宽度与高度：union 若「高而窄」（检测散布），只限宽会让抽取面积爆炸
    ow = int(min(int(out_w), w, max(16, int(round(TRACK_MAX_OH * w / float(h))))))
    ow -= ow % 2
    oh = max(2, int(round(h * ow / float(w))))
    oh -= oh % 2
    uf = extract_roi_frames(path, seconds, fps, (x, y, w, h), (ow, oh), ffmpeg)
    if uf is None or uf.shape[0] < 4:
        return None, {}
    n = uf.shape[0]
    scale = ow / float(w)
    mw = max(16, min(ow, int(round(float(win_src[0]) * scale))))
    mh = max(12, min(oh, int(round(float(win_src[1]) * scale))))
    mw -= mw % 2
    mh -= mh % 2
    t_uf = (np.arange(n) + 0.5) / float(fps)
    t_c = (np.arange(len(centers_src)) + 0.5) / float(centers_fps)
    kx = (np.interp(t_uf, t_c, centers_src[:, 0]) - x) * scale
    ky = (np.interp(t_uf, t_c, centers_src[:, 1]) - y) * (oh / float(h))
    crops = np.zeros((n, mh, mw), dtype=np.uint8)
    clipped = 0
    for i in range(n):
        px = int(round(kx[i] - mw / 2.0))
        py = int(round(ky[i] - mh / 2.0))
        cx0 = max(0, min(ow - mw, px))
        cy0 = max(0, min(oh - mh, py))
        if cx0 != px or cy0 != py:
            clipped += 1
        crops[i] = uf[i, cy0:cy0 + mh, cx0:cx0 + mw]
    meta = {"union_box_src": [x, y, w, h], "extract_size": [ow, oh],
            "tracked_win": [mw, mh], "track_win_clipped": int(clipped),
            "track_span_px": [int(round(float(np.ptp(centers_src[:, 0])))),
                              int(round(float(np.ptp(centers_src[:, 1]))))]}
    return crops, meta


def multiscale_lag_estimates(a, m, fps: int, max_lag_frames: int, span_s: float = 0.0,
                             mask=None, min_cov: float = 0.6) -> list:
    """多尺度分段滞后估计 → [(lag_ms, r, 权重)]。

    为什么不用 3 秒滑窗做判据：真实直播里 3 秒只有 2~3 个音节，单窗峰值会被局部噪声带跑
    （实测同一段样本的 3s 窗滞后从 −275ms 跳到 +253ms），但**更长的分段高度一致**。
    因此判据用「整段 / 二分 / 三分 / 6s 滑窗」这些更长的尺度，短窗只作局部抖动诊断。
    """
    np = _np()
    n = min(len(a), len(m))
    out: list = []

    def _covered(s, e):
        if mask is None:
            return True
        seg = mask[s:e]
        return len(seg) > 0 and float(np.mean(seg)) >= min_cov

    for parts in (1, 2, 3):
        if n < parts * max(24, int(2.0 * fps)):
            continue
        edges = [int(round(i * n / parts)) for i in range(parts + 1)]
        for s, e in zip(edges[:-1], edges[1:]):
            if not _covered(s, e):          # 说话时段门控：跳过覆盖不足的分段
                continue
            _, _, lag, r = cross_correlate(a[s:e], m[s:e], max_lag_frames)
            if lag is None or r is None:
                continue
            dur = (e - s) / float(fps)
            out.append((lag / fps * 1000.0, float(r), float(dur)))
    if span_s >= 18.0:                      # 长样本再加 6s 滑窗（步长 3s）
        win = max(24, int(round(6.0 * fps)))
        step = max(1, int(round(3.0 * fps)))
        for s in range(0, max(1, n - win + 1), step):
            e = s + win
            if not _covered(s, e):
                continue
            _, _, lag, r = cross_correlate(a[s:e], m[s:e], max_lag_frames)
            if lag is None or r is None:
                continue
            out.append((lag / fps * 1000.0, float(r), 6.0))
    return out


def robust_offset_from_windows(est: list, res_ms: float, min_r: float = 0.15,
                              min_windows: int = 3) -> dict:
    """滑窗结果 → 稳健偏移估计（相关性筛选 + 加权中位数 + MAD 置信区间）。"""
    np = _np()
    if not est:
        return {"ok": False, "reason": "无滑窗样本"}
    lags = np.array([e[0] for e in est], dtype=np.float64)
    rs_raw = np.array([e[1] for e in est], dtype=np.float64)
    ws = np.array([(e[2] if len(e) > 2 else 1.0) for e in est], dtype=np.float64)
    # 选择判据仍按**原始相关性**；e[2] 作为权重（多尺度=分段时长，短窗=语音活动度）
    thr = max(min_r, 0.5 * float(rs_raw.max()))
    sel = rs_raw >= thr            # 选择判据只看相关性（语音活动仅作诊断指标输出）
    if int(sel.sum()) < min_windows:                      # 放宽一次，尽量凑够窗口
        thr = float(np.percentile(rs_raw, 60))
        sel = rs_raw >= thr
    if int(sel.sum()) < 2:
        return {"ok": False, "reason": f"有效滑窗不足（{int(sel.sum())}/{len(est)}）",
                "total": len(est), "r_max": round(float(rs_raw.max()), 3)}
    l = lags[sel]
    # 权重只用相关性：实测把“活动度”乘进权重会放大离散度（活动度只在上面做软门限）
    w = rs_raw[sel] * np.clip(ws[sel] / (6.0 if float(np.max(ws[sel])) > 1.5 else 1.0), 0.25, 1.0)
    order = np.argsort(l)
    l, w = l[order], w[order]
    cw = np.cumsum(w)
    med = float(np.interp(cw[-1] / 2.0, cw, l))            # 加权中位数
    mad = float(np.median(np.abs(l - med)))
    return {"ok": True, "offset_ms": med, "ci_ms": 1.4826 * mad,
            "used": int(sel.sum()), "total": len(est),
            "weight_median": round(float(np.median(ws[sel])), 3),
            "r_median": float(np.median(rs_raw[sel])), "r_max": float(rs_raw.max()),
            "agreement": float(np.mean(np.abs(l - med) <= max(20.0, 2 * res_ms)))}


def sliding_lag_estimates(a, m, fps: int, max_lag_frames: int, win_s: float = 3.0,
                          step_s: float = 1.0, mask=None, min_cov: float = 0.8) -> list:
    """滑窗稳健估计：多个窗口各自求最佳滞后 → [(lag_ms, r)]。

    真实直播里单窗互相关很容易被 BGM/镜头切换带偏；多窗口的中位数与 MAD 才是可信估计。
    """
    np = _np()
    n = min(len(a), len(m))
    win = max(24, int(round(win_s * fps)))
    step = max(1, int(round(step_s * fps)))
    if n < win:
        win, step = n, max(1, n // 3)
    thr = float(np.percentile(a, 70)) if len(a) else 0.0     # 语音活动门控阈值
    out = []
    for s in range(0, max(1, n - win + 1), step):
        e = s + win
        if mask is not None:
            _seg = mask[s:e]
            if len(_seg) == 0 or float(np.mean(_seg)) < min_cov:
                continue
        seg_a, seg_m = a[s:e], m[s:e]
        _, _, lag, r = cross_correlate(seg_a, seg_m, max_lag_frames)
        if lag is None or r is None:
            continue
        activity = float((seg_a > thr).mean()) if len(seg_a) else 0.0
        out.append((lag / fps * 1000.0, r, activity))
    return out

def _normalize(x):
    np = _np()
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    s = x.std()
    if s < 1e-9:
        return None
    return x / s


def _corr_at(a, m, k: int) -> float:
    """corr(a[t], m[t+k])（Pearson，重叠区）。长度不一致时按最短对齐。"""
    n = min(len(a), len(m))
    a, m = a[:n], m[:n]          # 长度不等时先对齐，否则切片长度不一致会抛异常
    if k >= 0:
        aa, mm = a[:n - k], m[k:]
    else:
        aa, mm = a[-k:], m[:n + k]
    if len(aa) < 8:
        return float("nan")
    aa = aa - aa.mean()
    mm = mm - mm.mean()
    denom = math.sqrt(float((aa ** 2).sum()) * float((mm ** 2).sum()))
    if denom < 1e-12:
        return float("nan")
    return float((aa * mm).sum() / denom)


def cross_correlate(a, m, max_lag_frames: int) -> tuple:
    """返回 (lags, corrs, best_lag_float, best_r)。best_lag_float 含抛物线插值。"""
    np = _np()
    a, m = _normalize(a), _normalize(m)
    if a is None or m is None:
        return [], [], None, None
    lags = list(range(-max_lag_frames, max_lag_frames + 1))
    corrs = [_corr_at(a, m, k) for k in lags]
    valid = [(k, c) for k, c in zip(lags, corrs) if not math.isnan(c)]
    if not valid:
        return lags, corrs, None, None
    best_k, best_c = max(valid, key=lambda kc: kc[1])
    # 抛物线插值（亚帧精度）
    delta = 0.0
    if best_k in lags:
        i = lags.index(best_k)
        if 0 < i < len(corrs) - 1:
            y0, y1, y2 = corrs[i - 1], corrs[i], corrs[i + 1]
            if not (math.isnan(y0) or math.isnan(y2)):
                denom = (y0 - 2 * y1 + y2)
                if abs(denom) > 1e-9:
                    delta = 0.5 * (y0 - y2) / denom
                    delta = max(-1.0, min(1.0, delta))
    return lags, corrs, best_k + delta, best_c


def speaker_gate_eligible(gate: Optional[dict], res_ms: float, max_lag_frames: float) -> bool:
    """「说话时段」门控估计是否足以在**全段拒判**时兜底改判（严格，宁拒不假）。

    条件：有效说话样本 ≥1.5s；峰值 r ≥0.20；稳健滑窗 ≥4 且一致率 ≥0.6；
    CI ≤ max(60ms, 8×分辨力)；峰值不落在搜索边界。
    """
    if not gate or gate.get("offset_ms") is None or gate.get("r") is None:
        return False
    if float(gate.get("seconds") or 0.0) < 1.5:
        return False
    if float(gate["r"]) < 0.20:
        return False
    if int(gate.get("windows_used") or 0) < 3:
        return False
    if gate.get("agreement") is None or float(gate["agreement"]) < 0.6:
        return False
    ci = gate.get("ci_ms")
    if ci is None or float(ci) > max(80.0, 12.0 * float(res_ms)):
        return False
    pl = gate.get("peak_lag_frames")
    if pl is not None and max_lag_frames and abs(float(pl)) >= float(max_lag_frames) - 1:
        return False
    return True


def gated_correlation(env, m, weight, max_lag_frames: int):
    """加权（说话时段门控）互相关 → (best_lag_frames, r, coverage)。

    只在权重>0 的样本上算相关性（去加权均值、按权重缩放），用于「只在主播说话时段测量」。
    coverage = 有效样本占比（0..1）。无效返回 (None, None, 0.0)。
    """
    np = _np()
    if np is None or env is None or m is None or weight is None:
        return None, None, 0.0
    n = min(len(env), len(m), len(weight))
    if n < 8:
        return None, None, 0.0
    env, m, w = env[:n], m[:n], np.asarray(weight[:n], dtype=np.float64)
    sw = float(w.sum())
    if sw <= 1e-9:
        return None, None, 0.0
    cov = sw / float(n)
    e = ((env - float(np.average(env, weights=w))) * w).astype(np.float32)
    mm = ((m - float(np.average(m, weights=w))) * w).astype(np.float32)
    _, _, lag, r = cross_correlate(e, mm, max_lag_frames)
    return lag, r, cov


def segment_lags(a, m, max_lag_frames: int, segments: int = 3) -> list:
    """分段时间一致性：各段独立求最佳滞后（帧）。"""
    np = _np()
    n = min(len(a), len(m))
    if n < segments * max(8, max_lag_frames + 2):
        return []
    edges = [int(round(i * n / segments)) for i in range(segments + 1)]
    out = []
    for s, e in zip(edges[:-1], edges[1:]):
        _, _, lag, r = cross_correlate(a[s:e], m[s:e], max_lag_frames)
        if lag is not None:
            out.append((lag, r))
    return out


# ─────────────────────────── 主分析 ───────────────────────────

def analyze_media(path: str | Path, *, fps: int = DEFAULT_FPS, max_seconds: float = DEFAULT_SECONDS,
                  skip_specificity: bool = False,
                  seconds: Optional[float] = None, roi: Optional[list] = None,
                  use_face: bool = True, room_id: Optional[str] = None,
                  max_lag_ms: int = MAX_LAG_MS, fine_fps: int = FINE_FPS,
                  use_fine: bool = True) -> dict:
    np = _np()
    path = Path(path)
    result: dict[str, Any] = {
        "type": "lip_sync_analysis",
        "method": "人脸嘴部运动能量 × 音频包络 归一化互相关（内容级，不看时间戳）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(path),
        "mode": "file",
        "room_id": str(room_id) if room_id else None,
    }
    if not np:
        result.update({"verdict": "无法评估（缺少 numpy）", "verdict_level": "低"})
        return result

    ffmpeg, ffprobe = resolve_ffmpeg(), resolve_ffprobe()
    if not ffmpeg:
        result.update({"verdict": "无法评估（ffmpeg 不可用）", "verdict_level": "低"})
        return result

    meta = probe_media(path, ffprobe)
    window = float(seconds or max_seconds)
    if meta["duration"] > 0:
        window = min(window, meta["duration"])
    result["media"] = meta
    result["window_seconds"] = round(window, 2)
    result["fps"] = fps

    if not meta["has_audio"] or not meta["has_video"]:
        result.update({"verdict": "无法评估（缺音频或视频轨道）", "verdict_level": "低",
                       "verdict_detail": "内容级检测需要同时具备音频与视频轨道。"})
        return result

    size = scaled_size(meta["width"], meta["height"])
    frames = extract_video_frames(path, window, fps, size, ffmpeg)
    pcm = extract_audio_pcm(path, window, ffmpeg)
    if frames is None or pcm is None:
        result.update({"verdict": "无法评估（抽帧/抽音失败）", "verdict_level": "低"})
        return result

    result["frames"] = int(frames.shape[0])
    result["scale"] = {"width": size[0], "height": size[1]}

    # ── ROI：人脸自动 或 手动 ──
    coverage, jitter, roi_mode = 1.0, 0.0, "manual"
    face_backend_name, frontality_med, visibility_med = None, None, None
    backend_obj = None
    cand_info: dict = {}          # 多脸候选信息（人脸分支填充）
    boxes: list = []              # 逐帧人脸框（手动 ROI 时为空；供特异性对照锚定使用）
    if roi:
        rois = roi_from_spec(roi, frames.shape[1:], frames.shape[0])
        roi_xywh = [round(float(v), 4) for v in roi]
    else:
        roi_mode = "face-auto"
        backend = get_face_backend() if use_face else None
        faces_multi: list = []
        if backend is not None and backend.available:
            # YuNet 优先：5 点关键点 → 紧贴嘴唇的 ROI + 正脸度
            face_backend_name = backend.name
            backend_obj = backend
            faces_multi = backend.detect_multi(frames, top_k=2)
        else:
            cascades = _load_cascades() if use_face else []
            if not cascades:
                result.update({"verdict": "无法评估（人脸检测不可用，且未提供 --roi）",
                               "verdict_level": "低",
                               "verdict_detail": "请安装 opencv-python-headless，或用 --roi x,y,w,h 指定嘴部区域。"})
                return result
            face_backend_name = "haar"
            faces_multi = [[{"box": b, "lm": None, "score": None, "source": "haar"}
                            for b in bl] for bl in detect_faces_multi(frames, cascades, top_k=2)]
        boxes_multi = [[f["box"] for f in fl] for fl in faces_multi]
        boxes = [(fl[0]["box"] if fl else None) for fl in faces_multi]
        _FB = None
        try:
            import face_backend as _FB  # noqa: PLC0415
            rois, coverage, jitter, frontality_med = _FB.rois_from_faces_multi(
                faces_multi, frames.shape[1:], 0)
            visibility_med = _FB.visibility_median(frames, rois, faces_multi, 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("关键点 ROI 失败，退回人脸框比例: %s", exc)
            rois, coverage, jitter = boxes_to_mouth_rois(boxes, frames.shape[1:])
        cand_info: dict = {}
        if rois is not None and any(len(b) > 1 for b in boxes_multi):
            # 多脸场景（主播 + 画中画模特）：分别用两张脸做粗相关，选更可信的那个
            env_c = audio_envelope(pcm, fps)
            _mlf = max(1, int(round(max_lag_ms / 1000.0 * fps)))
            rs_k: dict = {}
            for k in (0, 1):
                r_k, _cov, _jit, _fmed = rois_from_faces_multi(faces_multi, frames.shape[1:], k)
                if r_k is None:
                    continue
                m_k, _o = mouth_motion_signal(frames, r_k)
                _nc = min(len(env_c), len(m_k))
                _, _, _lag, rr = cross_correlate(env_c[:_nc], m_k[:_nc], _mlf)
                rs_k[k] = 0.0 if rr is None else float(rr)
            if rs_k:
                best_k = max(rs_k, key=lambda k: rs_k[k])
                if best_k != 0 and rs_k.get(best_k, 0.0) < rs_k.get(0, 0.0) + 0.03:
                    best_k = 0                      # 第 2 张脸没有明显更好就不切换
                if best_k == 1:
                    rois2, cov2, jit2, fmed2 = rois_from_faces_multi(faces_multi, frames.shape[1:], 1)
                    if rois2 is not None:
                        rois, coverage, jitter = rois2, cov2, jit2
                        if fmed2 is not None:
                            frontality_med = fmed2
                        try:
                            visibility_med = _FB.visibility_median(frames, rois2, faces_multi, 1)
                        except Exception:  # noqa: BLE001
                            pass
                        cand_info["switched"] = True
                cand_info.update({"candidates": len(rs_k), "chosen": best_k,
                                  "coarse_r": {str(k): round(v, 3) for k, v in rs_k.items()}})
        if rois is None:
            result.update({"verdict": "无法评估（整窗未检出人脸）", "verdict_level": "低",
                           "confidence": "低",
                           "verdict_detail": "未检出人脸：可能画面无人物、人物过小/过偏，或为多人场景。"
                                             "可用 --roi x,y,w,h 手动框定嘴部区域。"})
            result["metrics"] = {"face_coverage": 0.0, "roi_mode": roi_mode}
            return result
        roi_xywh = None

    fps_coarse = fps
    m, openness = mouth_motion_signal(frames, rois)
    m_coarse = m.copy()
    env = audio_envelope(pcm, fps)

    # ── 嘴型特异性对照：嘴部 ROI vs 同尺寸对照区域（背景/产品/胸前）──
    spec: dict = {"mouth_r_coarse": None, "control_r_max": None, "controls": [],
                  "specific": None, "margin": None}
    try:
        _mlc = max(1, int(round(max_lag_ms / 1000.0 * fps)))
        _mouth_box = np.median(np.asarray(rois, dtype=np.float32), axis=0)
        # 对照区锚定在**人脸框**上（从而落在脸外：背景/胸前/贴纸面板），
        # 避免“脸颊/下巴跟着头动”把合法说话也判成不特异
        _valid_boxes = [b for b in boxes if b] if isinstance(boxes, list) else []
        # 注意：人脸检测给出的是 (x,y,w,h)；对照区生成需要 (x0,y0,x1,y1) —— 必须显式换算，
        # 否则 w/h 被当成 x1/y1 → 负宽高 → 对照区退化成 8×8 小噪声块（曾导致特异性系统性误判）。
        _face_xywh = (np.median(np.asarray(_valid_boxes, dtype=np.float32), axis=0)
                      if len(_valid_boxes) >= 3 else None)
        if _face_xywh is not None:
            _ctrl = control_rois_from_face(_face_xywh, _mouth_box, frames.shape[1:])
        else:
            _ctrl = control_rois_from(_mouth_box, frames.shape[1:])
        _r_mouth = cross_correlate(env, m, _mlc)[3]
        _rcs = []
        for _c in _ctrl:
            _rois_c = np.tile(np.asarray(_c, dtype=np.float32), (frames.shape[0], 1))
            _mc, _oc = mouth_motion_signal(frames, _rois_c)
            _rr = cross_correlate(env, _mc, _mlc)[3]
            _rcs.append(0.0 if _rr is None else float(abs(_rr)))
        spec["mouth_r_coarse"] = round(float(_r_mouth), 3) if _r_mouth is not None else None
        spec["controls"] = [round(v, 3) for v in _rcs]
        spec["control_r_max"] = round(max(_rcs), 3) if _rcs else None
        # 判据用**稳健参照**（对照的 75 分位）而非单个最大值：单点最大易被噪声主导
        _ref = float(np.percentile(_rcs, 75)) if _rcs else None
        spec["control_r_ref"] = round(_ref, 3) if _ref is not None else None
        if _rcs and _r_mouth is not None:
            _mi = float(abs(_r_mouth))
            spec["margin"] = round(_mi - (_ref if _ref is not None else 0.0), 3)
            # 需**明显**优于稳健参照（0.08）：边缘裕度不足以断言“测到的是嘴型”
            spec["specific"] = bool(_mi >= (_ref if _ref is not None else 1.0) + 0.08)
        if skip_specificity:
            spec["skipped"] = True
            spec["specific"] = None
    except Exception as _exc:  # noqa: BLE001
        logger.debug("特异性对照计算失败: %s", _exc)

    # ── 唇开合几何信号（可选，需 106 点模型）：几何量 → 不受美颜磨皮影响 ──
    ap_sig, ap_ctrl_sig, ap_meta = None, None, {"enabled": False}
    if (backend_obj is not None and getattr(backend_obj, "has_lip_landmarks", False)
            and faces_multi and roi_mode == "face-auto"):
        try:
            _chunks = []
            for _t0, _ch in iter_color_frames(path, window, fps, (size[0], size[1]), ffmpeg, 4.0):
                _chunks.append(_ch)
            if _chunks:
                _cols = np.concatenate(_chunks, axis=0)
                _nn = min(_cols.shape[0], frames.shape[0])
                _ap, _apc, _ = backend_obj.aperture_series(_cols[:_nn], faces_multi[:_nn])
                ap_sig = detrend_series(_ap)
                ap_ctrl_sig = detrend_series(_apc)
                ap_meta = {"enabled": ap_sig is not None,
                           "model": (Path(backend_obj._lmk_path).name
                                     if backend_obj._lmk_path else None),
                           "valid_frames": int(np.isfinite(_ap).sum()) if _ap is not None else 0}
                _mlc_frames = max(1, int(round(max_lag_ms / 1000.0 * fps)))
                if ap_sig is not None and ap_ctrl_sig is not None:
                    _nc_ap = min(len(env), len(ap_sig), len(ap_ctrl_sig))
                    _rr_ap = cross_correlate(env[:_nc_ap], ap_sig[:_nc_ap], _mlc_frames)[3]
                    _rr_ac = cross_correlate(env[:_nc_ap], ap_ctrl_sig[:_nc_ap], _mlc_frames)[3]
                    ap_meta["r"] = round(float(_rr_ap), 3) if _rr_ap is not None else None
                    ap_meta["control_r"] = round(float(_rr_ac), 3) if _rr_ac is not None else None
                    # 几何信号的「对照」（眼带跨度）与检测框抖动同源，实测与唇开合高度共模，
                    # 不足以作为特异性门禁 → 仅作参考信息；门禁仍用已验证的图像对照区。
                    ap_meta["gate"] = "informational"
        except Exception as exc:  # noqa: BLE001
            logger.warning("唇几何信号计算失败（已降级为帧差信号）: %s", exc)

    # ── 高精度通道：ROI 裁剪后高帧率抽帧（默认 100fps = 10ms） + 高分辨率音频包络 ──
    # 目的：消除「25fps 帧率混叠」，把滞后分辨力从 40ms 提升到 10ms 量级。
    fine_meta: dict = {"enabled": bool(use_fine), "fine_fps": fine_fps, "used": False}
    if use_fine and fine_fps and fine_fps > fps:
        src_shape = (meta.get("width") or 0, meta.get("height") or 0)
        src_fps = float(meta.get("fps") or 0.0)
        # 关键：ROI 抽取率取「源帧率」，**不做倍帧上采样**。
        # ffmpeg fps 滤波器在放大帧率时会复制帧，使运动脉冲相对源帧边界偏移约半帧，
        # 从而引入 ±(0.5/源帧率) 的系统偏差（25fps → ±20ms）。真正的毫秒分辨力来自
        # 「把运动信号线性插值到细网格后再与音频包络做互相关」。
        roi_fps = int(round(src_fps)) if 8 <= src_fps <= 120 else int(min(fine_fps, DEFAULT_FPS))
        roi_fps = max(8, min(roi_fps, ROI_FPS_CAP))
        # 抽取方式：face-auto → 逐帧 ROI **跟踪**（union 裁剪 + 按中心取样）；
        # 手动 ROI → 沿用固定框（保持既有口径与数值不变）。
        roi_frames, box, track_meta, tracked = None, None, {}, False
        if roi_mode == "face-auto":
            try:
                centers, union, win_src = _track_geometry(rois, frames.shape[1:], src_shape)
                if union is not None:
                    roi_frames, track_meta = extract_tracked_roi_frames(
                        path, window, roi_fps, union, TRACK_OUT_W, centers, fps, win_src, ffmpeg)
                    if roi_frames is not None:
                        box, tracked = union, True
            except Exception as exc:  # noqa: BLE001
                logger.debug("ROI 跟踪失败，回退固定框: %s", exc)
        if roi_frames is None:
            box = _source_roi_box(rois, frames.shape[1:], src_shape)
            tracked, track_meta = False, {}
            if box:
                roi_frames = extract_roi_frames(path, window, roi_fps, box, (96, 48), ffmpeg)
        if roi_frames is not None:
            # 双变体：原始帧差（相位准） vs 平移对齐残差（残余移动）
            m_raw, t_m, _ = mouth_motion_fine(roi_frames, roi_fps, align=False)
            m_al, _t_al, shift = mouth_motion_fine(roi_frames, roi_fps, align=True,
                                                   max_shift=(3 if tracked else 6))
            env_f, t_a = audio_envelope_fine(pcm, int(fine_fps))
            m_i_ap, _r_ap = None, None
            if ap_sig is not None and len(ap_sig) >= 4:
                _t_ap = (np.arange(len(ap_sig)) + 0.5) / float(fps)
                m_i_ap = np.interp(t_a, _t_ap, ap_sig.astype(np.float64)).astype(np.float32)
            # 唇线暗带变体（对美颜磨皮鲁棒）
            m_i_op, _r_op = None, None
            try:
                _m_op, _t_op = mouth_openness_signal(roi_frames, roi_fps)
                if len(_m_op) >= 4:
                    m_i_op = np.interp(t_a, _t_op, _m_op.astype(np.float64)).astype(np.float32)
            except Exception as _e:  # noqa: BLE001
                logger.debug("唇线暗带信号失败: %s", _e)
            if len(m_raw) >= 4 and len(env_f) >= MIN_FRAMES:
                m_i_raw = np.interp(t_a, t_m, m_raw)
                m_i_al = np.interp(t_a, t_m, m_al)
                _mlf = max(1, int(round(max_lag_ms / 1000.0 * fine_fps)))
                _r_raw = cross_correlate(env_f, m_i_raw, _mlf)[3]
                _r_al = cross_correlate(env_f, m_i_al, _mlf)[3]
                # 只有明显更优（≥0.03）才切到对齐变体：差在噪声范围内时保持 raw 的相位准确度
                if m_i_ap is not None and len(m_i_ap) == len(env_f):
                    _r_ap = cross_correlate(env_f, m_i_ap, _mlf)[3]
                if m_i_op is not None and len(m_i_op) == len(env_f):
                    _r_op = cross_correlate(env_f, m_i_op, _mlf)[3]
                # 变体竞争：默认基线取「唇线暗带」（物理上最直接的口型代理，对美颜磨皮鲁棒），
                # 不可用时退回帧差 raw；其余变体需明显更优（>=0.03）才覆盖基线。
                if _r_op is not None and m_i_op is not None and len(m_i_op) == len(env_f):
                    _variant, m_i, _best_r = "openness", m_i_op, _r_op
                else:
                    _variant, m_i, _best_r = "raw", m_i_raw, _r_raw
                for _name, _sig, _rr in (("raw", m_i_raw, _r_raw), ("aligned", m_i_al, _r_al),
                                        ("aperture", m_i_ap, _r_ap)):
                    if _rr is None or _sig is None or _name == _variant:
                        continue
                    if _best_r is None or _rr >= _best_r + 0.03:
                        _variant, m_i, _best_r = _name, _sig, _rr
                if len(m_i) >= MIN_FRAMES:
                    m, env, fps = m_i.astype(np.float32), env_f, int(fine_fps)
                    fine_meta.update({"used": True, "roi_box_src": list(box) if box else None,
                                      "roi_tracked": bool(tracked),
                                      "roi_fps": roi_fps, "source_fps": src_fps,
                                      "roi_frames": int(roi_frames.shape[0]),
                                      "motion_variant": _variant,
                                      "signal_r": {"raw": round(float(_r_raw), 3) if _r_raw is not None else None,
                                                   "aligned": round(float(_r_al), 3) if _r_al is not None else None,
                                                   "aperture": round(float(_r_ap), 3) if _r_ap is not None else None,
                                                   "openness": round(float(_r_op), 3) if _r_op is not None else None},
                                      "align_shift_px_median": shift,
                                      **track_meta,
                                      "r_raw": round(float(_r_raw), 3) if _r_raw is not None else None,
                                      "r_aligned": round(float(_r_al), 3) if _r_al is not None else None})
    if not fine_meta.get("used"):
        fine_meta["reason"] = "高精度通道关闭或不可用（回退逐帧）"

    # ── 语音活动（VAD-lite）：说话占比，用于诊断与片段选择（软指标，不作硬门禁）──
    va_meta: dict = {}
    va_arr = None
    try:
        import voice_activity as _VA  # noqa: PLC0415
        _va, _va_stats = _VA.voice_activity(pcm, fps)
        va_arr = _va if len(_va) else None
        va_meta = dict(_va_stats or {})
        if len(_va):
            va_meta["median"] = round(float(np.median(_va)), 3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("语音活动计算失败: %s", exc)

    # ── 嘴部遮挡（手/物体）判定：纹理塌陷（相对人脸）+ 运动塌陷（语音仍在）──
    occlusion: dict = {}
    if roi_mode == "face-auto" and rois is not None:
        try:
            import face_backend as _FBocc  # noqa: PLC0415
            _ser = _FBocc.visibility_series(frames, rois, faces_multi, 0, stride=2)
            if _ser is not None:
                _fin = np.isfinite(_ser)
                _vis = _ser[_fin]
                _mask, _thr, _ref = occlusion_mask(_ser)
                if _mask is not None and _vis.size >= 4:
                    _occ = _mask
                    # 运动塌陷：局部嘴动低于下四分位「却」仍检测到语音 → 像被挡住
                    _mm = m_coarse if len(m_coarse) else m
                    if len(_mm) >= 8 and va_arr is not None and len(va_arr) >= 8:
                        _m_sig = np.interp(np.linspace(0, 1, len(_ser)),
                                           np.linspace(0, 1, len(_mm)), _mm.astype(float))
                        _v_sig = np.interp(np.linspace(0, 1, len(_ser)),
                                           np.linspace(0, 1, len(va_arr)), va_arr.astype(float))
                        _occ |= (_m_sig < float(np.percentile(_mm, 25))) & (_v_sig >= 0.5)
                    occlusion = {
                        "threshold": round(float(_thr), 3),
                        "vis_ref": round(float(_ref), 3),
                        "vis_p10": round(float(np.percentile(_vis, 10)), 3),
                        "vis_median": round(float(np.median(_vis)), 3),
                        "occluded_fraction": round(float(_occ.mean()), 3),
                        "occluded_longest_s": round(_longest_run(_occ) / float(max(1, fps)), 2),
                    }
        except Exception as exc:  # noqa: BLE001
            logger.debug("遮挡判定失败: %s", exc)

    n_common = min(len(m), len(env))
    if n_common < MIN_FRAMES:
        result.update({"verdict": f"无法评估（有效帧仅 {n_common}，样本过短）", "verdict_level": "低"})
        return result
    m, env = m[:n_common], env[:n_common]

    max_lag_frames = max(1, int(round(max_lag_ms / 1000.0 * fps)))

    # ── 说话时段门控估计（诊断兼可用性兜底）：只在「语音活动高」的帧上测 ──
    # 带货直播里 BGM/旁白会把全段相关性稀释；限定在主播说话帧上常更可信（实测 r 提升可观），
    # 但有效样本变少，故只作为并列估计报告，不覆盖全段结论。
    speaker_gate: dict = {}
    if va_arr is not None and len(va_arr) >= 8:
        _w = np.interp(np.linspace(0, 1, len(env)), np.linspace(0, 1, len(va_arr)),
                       va_arr.astype(np.float64))
        for _thr in (0.5, 0.4):
            _mask = (_w >= _thr).astype(np.float64)
            _cov_s = float(_mask.sum()) / max(1, fps)
            if _cov_s < 1.5:                       # 至少 1.5s 有效说话样本才报告
                continue
            _lag, _r, _cov = gated_correlation(env, m, _mask, max_lag_frames)
            if _lag is None:
                continue
            # 在门控信号上同样做多尺度稳健估计（供「兜底改判」的可信度判据）
            _env_g = (env - float(np.average(env, weights=_mask))) * _mask
            _m_g = (m - float(np.average(m, weights=_mask))) * _mask
            _est = multiscale_lag_estimates(_env_g, _m_g, fps, max_lag_frames,
                                            span_s=n_common / float(fps),
                                            mask=_mask, min_cov=0.6)
            _est += sliding_lag_estimates(_env_g, _m_g, fps, max_lag_frames,
                                          win_s=max(1.5, min(3.0, _cov_s / 2.0)),
                                          step_s=1.0, mask=_mask, min_cov=0.8)
            _rob = robust_offset_from_windows(_est, 1000.0 / fps)
            speaker_gate = {
                "thr": _thr, "offset_ms": round(_lag / fps * 1000.0, 2),
                "r": round(float(_r), 3), "coverage": round(_cov, 3),
                "seconds": round(_cov_s, 2),
                "peak_lag_frames": int(_lag),
                "robust_offset_ms": (round(float(_rob["offset_ms"]), 2) if _rob.get("ok") else None),
                "ci_ms": (round(float(_rob["ci_ms"]), 2) if _rob.get("ok") else None),
                "agreement": (round(float(_rob["agreement"]), 3) if _rob.get("ok") else None),
                "windows_used": _rob.get("used"), "windows_total": _rob.get("total"),
            }
            break

    lags, corrs, best_lag, best_r = cross_correlate(env, m, max_lag_frames)
    if best_lag is None or best_r is None:
        result.update({"verdict": "无法评估（信号无变化：可能不是说话场景）", "verdict_level": "低"})
        return result

    offset_ms = best_lag / fps * 1000.0
    offset_frames = best_lag
    seg = segment_lags(env, m, max_lag_frames)
    seg_lags = [s[0] for s in seg]
    if len(seg_lags) >= 2:
        arr = np.array(seg_lags, dtype=np.float64)
        med = float(np.median(arr))
        lag_spread_ms = float(np.median(np.abs(arr - med))) / fps * 1000.0
        consistency = float(np.mean(np.abs(arr - med) <= 1.0))
    else:
        lag_spread_ms, consistency = None, None

    # 「是否在说话」用源帧率下的原始运动信号判断（细网格插值会平滑掉该指标的可比性）
    _m_act = m_coarse if len(m_coarse) else m
    motion_activity = float(_m_act.std() / (_m_act.mean() + 1e-6))
    open_activity = float(openness.std() / (openness.mean() + 1e-6))
    talking = bool(motion_activity > 0.35 and _m_act.std() > 0.4)

    # ── 滑窗稳健估计（中位数 + MAD 置信区间 + 全局/稳健一致性）──
    # 判定用「多尺度分段」稳健估计（3s 窗太短会把峰值带跑）
    _span_s = max(0.1, n_common / float(fps))
    _win_s = max(1.5, min(3.0, _span_s / 4.0))
    est = multiscale_lag_estimates(env, m, fps, max_lag_frames, span_s=_span_s)
    local_est = sliding_lag_estimates(env, m, fps, max_lag_frames, win_s=_win_s,
                                      step_s=max(0.4, _win_s / 2.0))
    local_lags = np.array([e[0] for e in local_est], dtype=np.float64) if local_est else np.array([])
    local_jitter_ms = (float(np.median(np.abs(local_lags - np.median(local_lags)))) * 1.4826
                       if local_lags.size >= 3 else None)
    local_activity = (float(np.median([e[2] for e in local_est])) if local_est else None)
    res_ms = 1000.0 / fps
    rob = robust_offset_from_windows(est, res_ms)
    robust_ms = rob.get("offset_ms") if rob.get("ok") else None
    ci_ms = rob.get("ci_ms") if rob.get("ok") else None
    robust_r = rob.get("r_median") if rob.get("ok") else None
    agree = rob.get("agreement") if rob.get("ok") else None
    media_span_s = _span_s

    metrics = {
        "roi_mode": roi_mode,
        "fps": fps,
        "fps_coarse": fps_coarse,
        "lag_resolution_ms": round(res_ms, 2),
        "fine": fine_meta,
        "robust_offset_ms": round(robust_ms, 2) if robust_ms is not None else None,
        "robust_correlation": round(robust_r, 3) if robust_r is not None else None,
        "offset_ci_ms": round(ci_ms, 2) if ci_ms is not None else None,
        "window_agreement": round(agree, 3) if agree is not None else None,
        "media_span_s": round(media_span_s, 2),
        "windows_used": rob.get("used"),
        "windows_total": rob.get("total"),
        "robust_reason": rob.get("reason"),
        "face_candidates": cand_info,
        "face_backend": face_backend_name,
        "frontality_median": (round(float(frontality_med), 3)
                              if isinstance(frontality_med, (int, float)) else None),
        "mouth_visibility_median": (round(float(visibility_med), 3)
                                    if isinstance(visibility_med, (int, float)) else None),
        "voice_activity": va_meta,
        "speaker_gate": speaker_gate or None,
        "occlusion": occlusion or None,
        "lip_aperture": ap_meta or None,
        "mouth_specificity": spec,
        "speech_activity_median": round(local_activity, 3) if local_activity is not None else None,
        "window_estimates_ms": [round(e[0], 1) for e in est[:40]],
        "scales_used": len(est),
        "local_window_estimates_ms": [round(v, 1) for v in local_lags[:40]],
        "local_jitter_ms": round(local_jitter_ms, 1) if local_jitter_ms is not None else None,
        "max_lag_frames": max_lag_frames,
        "roi_relative_xywh": roi_xywh,
        "face_coverage": round(coverage, 3),
        "face_jitter_px": round(jitter, 2),
        "mouth_motion_activity": round(motion_activity, 3),
        "mouth_open_activity": round(open_activity, 3),
        "talking_detected": talking,
        "peak_lag_ms": round(offset_ms, 2),
        "peak_lag_frames": round(offset_frames, 2),
        "peak_correlation": round(best_r, 3),
        "lag_spread_ms": round(lag_spread_ms, 2) if lag_spread_ms is not None else None,
        "segment_consistency": round(consistency, 3) if consistency is not None else None,
        "segments": [round(s[0], 2) for s in seg],
        "lag_curve": [[round(k / fps * 1000.0, 1), (round(c, 4) if not math.isnan(c) else None)]
                      for k, c in zip(lags, corrs)],
    }
    result["metrics"] = metrics
    build_verdict(result)
    return result


def build_verdict(result: dict) -> None:
    """内容级判定 + 置信度。offset > 0 = 音频超前画面。"""
    m = result.get("metrics") or {}
    if result.get("verdict"):
        return
    # 稳健值优先：滑窗中位数比单一全局峰抗噪（避免帧率混叠导致的假峰被当成结论）
    _off_global = float(m.get("peak_lag_ms") or 0.0)
    _off_robust = m.get("robust_offset_ms")
    off = float(_off_robust) if isinstance(_off_robust, (int, float)) else _off_global
    r = float(m.get("peak_correlation") or 0.0)
    _r_robust = m.get("robust_correlation")
    res_ms = float(m.get("lag_resolution_ms") or (1000.0 / DEFAULT_FPS))
    ci_ms = m.get("offset_ci_ms")
    agree = m.get("window_agreement")
    cov = float(m.get("face_coverage") or 0.0)
    cons = m.get("segment_consistency")
    talking = bool(m.get("talking_detected"))

    itu, ebu = PERCEPTUAL_ITU, PERCEPTUAL_EBU_R37
    ebu_ok = ebu["audio_lag_ms"] <= off <= ebu["audio_lead_ms"]
    itu_detect = (off >= itu["audio_lead_detect_ms"]) or (off <= itu["audio_lag_detect_ms"])
    itu_accept = (off >= itu["audio_lead_accept_ms"]) or (off <= itu["audio_lag_accept_ms"])
    direction = "音频超前画面" if off >= 0 else "音频滞后画面"

    # 置信度：相关峰强度 + 人脸覆盖 + 分段一致性
    conf = 0
    if r >= 0.30:
        conf += 2
    elif r >= 0.20:
        conf += 1
    if cov >= 0.8:
        conf += 1
    if cons is None or cons >= 0.66:
        conf += 1
    if talking:
        conf += 1
    level = "高" if conf >= 4 else ("中" if conf >= 3 else "低")

    # ── 可靠性门禁 ──
    # 真实直播常见「非说话画面」（产品特写/游戏/贴纸遮挡/强 BGM）：此时互相关很弱，
    # 峰值可能落在搜索区间边界（±max_lag）——这属于“测不出”，不能当结论输出。
    _maxlag = float(m.get("max_lag_frames") or 0)
    _fps = float(m.get("fps") or DEFAULT_FPS) or DEFAULT_FPS
    _boundary = bool(_maxlag and abs(float(m.get("peak_lag_frames") or 0)) >= _maxlag - 1)
    _weak = r < 0.15 or (isinstance(_r_robust, (int, float)) and _r_robust < 0.15)
    # 全局峰与滑窗稳健估计矛盾 → 帧率混叠/多峰，不可信
    _disagree = bool(isinstance(_off_robust, (int, float))
                     and abs(_off_global - float(_off_robust)) > max(30.0, 3 * res_ms))
    # 滑窗离散过大 → 内容本身不同步不一致（或画面无有效说话）
    _unstable = bool(isinstance(ci_ms, (int, float)) and ci_ms > max(30.0, 4 * res_ms))
    _span_s = float(m.get("media_span_s") or 0.0)
    _too_short = bool(0 < _span_s < 4.0)
    _few_windows = bool(not isinstance(_off_robust, (int, float)))
    _spec = m.get("mouth_specificity") or {}
    _not_specific = (_spec.get("specific") is False)
    # 正脸度（仅关键点分支可算）：侧脸/大转头时嘴唇透视变形、信号不可信
    _frontal = m.get("frontality_median")
    _not_frontal = bool(isinstance(_frontal, (int, float)) and _frontal < 0.35)
    # 嘴部遮挡（手/物体）：纹理塌陷 + 语音仍在的运动塌陷，占比过高即影响判定
    _occ = m.get("occlusion") or {}
    _occ_frac = float(_occ.get("occluded_fraction") or 0.0)
    _occluded = bool(_occ_frac >= OCC_FRACTION_GATE
                     and float(_occ.get("occluded_longest_s") or 0.0) >= 1.0)
    _sg = m.get("speaker_gate") or {}
    _gate_fired = (_too_short or _few_windows or _not_frontal or _occluded or _weak
                   or _not_specific or _boundary or _disagree or _unstable)
    _rescued = False
    if _gate_fired and speaker_gate_eligible(_sg, res_ms, _maxlag):
        # 全段不可信，但「主播说话时段」子集自身达标 → 以子集给出结论（明确标注覆盖范围）
        _off_sg = _sg.get("robust_offset_ms")
        off = float(_off_sg) if isinstance(_off_sg, (int, float)) else float(_sg["offset_ms"])
        direction = "音频超前画面" if off >= 0 else "音频滞后画面"
        ebu_ok = ebu["audio_lag_ms"] <= off <= ebu["audio_lead_ms"]
        itu_detect = (off >= itu["audio_lead_detect_ms"]) or (off <= itu["audio_lag_detect_ms"])
        itu_accept = (off >= itu["audio_lead_accept_ms"]) or (off <= itu["audio_lag_accept_ms"])
        level = "中"
        _rescued = True
        _tail = (f"（仅在主播说话时段测得：占 {(_sg.get('coverage') or 0) * 100:.0f}% / "
                 f"{_sg.get('seconds')}s，峰 r={_sg.get('r')}，CI±{_sg.get('ci_ms')}ms；全段不可信）")
        if ebu_ok and abs(off) <= abs(ebu["audio_lead_ms"]):
            grade = "内容级同步良好" + _tail
        elif itu_accept:
            grade = "内容级明显偏移" + _tail
        elif itu_detect:
            grade = "内容级可感知偏移" + _tail
        else:
            grade = "内容级轻微偏移" + _tail
    elif _gate_fired:
        level = "低"
        if _too_short:
            grade = f"无法确定（样本仅 {_span_s:.1f}s，过短）"
        elif _few_windows:
            _rs = m.get("robust_reason") or "有效滑窗不足"
            grade = f"无法确定（{_rs}）"
        elif _not_frontal:
            grade = (f"无法确定（主播非正脸：正脸度 {_frontal:.2f} < 0.35，"
                     "嘴唇透视变形/不可见）")
        elif _occluded:
            grade = (f"无法确定（嘴部长时间被遮挡：手/物体，占 {_occ_frac * 100:.0f}%，"
                     f"最长 {_occ.get('occluded_longest_s')}s）")
        elif _not_specific:
            grade = ("无法确定（嘴型特异性不足：对照区域相关性相当，"
                     f"嘴部 {_spec.get('mouth_r_coarse')} vs 对照 {_spec.get('control_r_max')}）")
        elif _weak:
            grade = "无法确定（相关性过低，不建议据此判读）"
        elif _boundary:
            grade = f"无法确定（相关峰落在 ±{_maxlag / _fps * 1000:.0f} ms 搜索边界）"
        elif _disagree:
            grade = "无法确定（全局峰与滑窗稳健估计矛盾）"
        else:
            grade = "无法确定（滑窗结果离散过大）"
    elif abs(off) <= abs(ebu["audio_lead_ms"]) and ebu_ok:
        grade = "内容级同步良好（在 EBU R37 广播窗口内）"
    elif itu_accept:
        grade = "内容级明显偏移（超出 ITU 可接受门限）"
    elif itu_detect:
        grade = "内容级可感知偏移（超出 ITU 可察觉门限）"
    elif not ebu_ok:
        grade = "内容级轻微偏移（超 EBU R37 建议窗口）"
    else:
        grade = "内容级同步良好"

    _fine = m.get("fine") or {}
    _lmk = m.get("lip_aperture") or None
    _va = m.get("voice_activity") or {}
    _va_line = (f"说话占比 {_va.get('active_fraction')}"
                if _va.get("active_fraction") is not None else "—")
    lines = [
        f"【内容级偏移】{off:+.2f} ms（{direction}）；相关峰 r={r:.3f}，"
        f"判定置信度 {level}",
        (f"【测量精度】滞后分辨力 ±{res_ms:.1f} ms（{m.get('fps')} fps"
         + ("，ROI 高帧率通道"
            + ("·逐帧跟踪" if _fine.get("roi_tracked") else "·固定框")
            + (f"，跟踪跨度 {_fine.get('track_span_px')}px" if _fine.get("track_span_px") else "")
            + "）"
            if _fine.get("used") else "，逐帧回退）")
         + (f"；滑窗稳健估计 {off:+.1f} ms ± {ci_ms:.1f} ms"
            f"（一致率 {agree:.0%}，{len(m.get('window_estimates_ms') or [])} 窗）"
            if isinstance(ci_ms, (int, float)) else "；滑窗样本不足，未给置信区间")
         + (f"；全局峰 {_off_global:+.1f} ms" if isinstance(_off_robust, (int, float)) else "")),
        f"【测量口径】直接比较嘴部运动与声音包络，与时间戳无关。"
        f"音频超前画面=先听到后看到。",
        (f"【稳定性】多尺度分段（{m.get('scales_used')} 段）一致率 {agree:.0%}，"
         f"离散 ±{ci_ms:.1f} ms" if isinstance(ci_ms, (int, float)) and isinstance(agree, (int, float))
         else "【稳定性】样本偏短，未做一致性评估"),
        (f"【局部抖动】3s 短窗离散 ±{m.get('local_jitter_ms')} ms（仅诊断，不参与判定）"
         if m.get('local_jitter_ms') is not None else ""),
        (f"【嘴型特异性】嘴部 r={_spec.get('mouth_r_coarse')} vs 对照最高 "
         f"{_spec.get('control_r_max')}（对照 {_spec.get('controls')}）→ "
         + ("通过" if _spec.get('specific') else "不足，结果不可信")
         if _spec.get('specific') is not None else "【嘴型特异性】无对照可比"),
        (f"【人脸覆盖】{cov * 100:.0f}%（{m.get('face_backend') or 'haar'}；"
         f"抖动 {m.get('face_jitter_px')} px）" if m.get("roi_mode") == "face-auto"
         else "【ROI】手动指定（未做人脸检测）"),
        (f"【说话时段】仅在语音活动≥{_sg.get('thr')} 的帧（{_sg.get('seconds')}s，占 "
         f"{(_sg.get('coverage') or 0) * 100:.0f}%）测得 {_sg.get('offset_ms'):+.1f} ms"
         f"（r={_sg.get('r')}，全段 r={r:.3f}）" if _sg.get("offset_ms") is not None else ""),
        (f"【唇几何信号】106 点唇开合 r={_lmk.get('r')}（参考对照 {_lmk.get('control_r')}；"
         f"仅作参考、不参与门禁）" if _lmk else ""),
        (f"【嘴部遮挡】手/物体遮挡占 {_occ_frac * 100:.0f}%（最长 "
         f"{_occ.get('occluded_longest_s')}s；可见性基线 {_occ.get('vis_ref')}、"
         f"阈 {_occ.get('threshold')}、p10 {_occ.get('vis_p10')}）" if _occ else ""),
        (f"【说话/正脸】正脸度 {m.get('frontality_median')}（1=正对镜头）；"
         f"语音活动 {_va_line}；嘴部可见性 {m.get('mouth_visibility_median')}"
         if (m.get("frontality_median") is not None
             or (m.get("voice_activity") or {}).get("active_fraction") is not None)
         else ""),
    ]
    if _weak or _boundary or _disagree or _unstable or _not_frontal or _occluded:
        _why = []
        if _too_short:
            _why.append(f"样本仅 {_span_s:.1f}s（建议 ≥8s）")
        if _few_windows:
            _why.append(f"有效滑窗 {m.get('windows_used')}/{m.get('windows_total')}"
                        f"（{m.get('robust_reason') or '不足'}）")
        if _not_frontal:
            _why.append(f"正脸度仅 {_frontal:.2f}（< 0.35，多为侧脸/转头/俯仰）")
        if _occluded:
            _why.append(f"嘴部遮挡占比 {_occ_frac * 100:.0f}%"
                        f"（阈值 {OCC_FRACTION_GATE * 100:.0f}%），"
                        f"可见性 p10={_occ.get('vis_p10')} < {_occ.get('threshold')}"
                        f"（基线 {_occ.get('vis_ref')}）")
        if _not_specific:
            _why.append(f"嘴部 r={_spec.get('mouth_r_coarse')} 未明显优于对照 "
                        f"{_spec.get('control_r_max')}（对照={_spec.get('controls')}）")
        if _weak:
            _why.append(f"相关峰仅 r={r:.3f}（< 0.15）")
        if _boundary:
            _why.append(f"峰值位于 ±{_maxlag / _fps * 1000:.0f} ms 搜索区间边界")
        if _disagree:
            _why.append(f"全局峰 {_off_global:+.1f} ms 与滑窗稳健估计 {off:+.1f} ms 矛盾"
                        f"（差 {abs(_off_global - off):.0f} ms）")
        if _unstable:
            _why.append(f"滑窗离散 ±{ci_ms:.1f} ms 过大")
        lines.append("⚠️ 测量不可靠（" + "；".join(_why) + "）：画面很可能不是「说话特写」"
                     "（产品特写/游戏画面/贴纸遮挡/多人入镜/强 BGM），"
                     "或音画本身时断时续。建议在主播正脸说话时段重新采样，或只参考时间戳级结果。")
    if not talking:
        lines.append("⚠️ 嘴部运动能量偏低：画面可能长时间无说话（如只播音乐/游戏画面），"
                     "该结果置信度下降，建议在主播说话时段重新采样。")
    if _rescued:
        lines.append("⚠️ 全段测量不可信（见下），本结论**仅在主播说话时段子集**上成立；"
                     "有效样本较短、置信度不高，请结合时间戳级结果与多次采样，"
                     "或在主播正脸说话时段重新采样复核。")
    if level == "低":
        lines.append("⚠️ 置信度低：内容级检测受人脸检测质量、镜头运动、多人入镜、BGM 等影响，"
                     "请结合多次采样与时间戳级结果综合判断。")
    lines.append("⚠️ 内容级偏移 = 观众实际感知的错位（不含播放器/音频输出链延迟）。"
                 "若时间戳级接近 0 而本值较大，说明不同步在内容层已存在（常见于推流前采集/编码链）。")
    result["verdict"], result["verdict_level"] = grade, level
    result["verdict_detail"] = "\n".join(lines)
    result["perceptual"] = {
        "content_offset_ms": round(off, 2),
        "content_offset_global_ms": round(_off_global, 2),
        "content_offset_ci_ms": round(ci_ms, 2) if isinstance(ci_ms, (int, float)) else None,
        "lag_resolution_ms": round(res_ms, 2),
        "window_agreement": round(agree, 3) if isinstance(agree, (int, float)) else None,
        "fine_channel": bool((m.get("fine") or {}).get("used")),
        "direction": direction,
        "peak_correlation": round(r, 3),
        "confidence": level,
        "face_backend": m.get("face_backend"),
        "roi_tracked": (_fine or {}).get("roi_tracked"),
        "frontality_median": m.get("frontality_median"),
        "voice_active_fraction": (m.get("voice_activity") or {}).get("active_fraction"),
        "speaker_gate": m.get("speaker_gate"),
        "occlusion": m.get("occlusion"),
        "lip_aperture": m.get("lip_aperture"),
        "speaker_gated": bool(_rescued),
        "content_offset_scope": "speaker_subset" if _rescued else "full_sample",
        "ebu_r37_ok": ebu_ok,
        "itu_detectable": itu_detect,
        "itu_acceptable": not itu_accept,
        "thresholds": {"itu": itu, "ebu_r37": ebu},
        "content_level": True,
    }


def diagnose_vs_stream_offset(lip: Optional[dict], avs: Optional[dict]) -> str:
    """内容级 vs 时间戳级 联合诊断（报告高亮用）。"""
    if not lip or not avs:
        return ""
    c = (lip.get("perceptual") or {}).get("content_offset_ms")
    s = (avs.get("perceptual") or {}).get("stream_offset_ms")
    if c is None or s is None:
        return ""
    diff = c - s
    # 对齐容差取 ITU 可察觉门限（默认 45ms）：含 AAC 编码器 priming 造成的 ~20~40ms 系统性残差
    _thr = ((lip.get("perceptual") or {}).get("thresholds") or {}).get("itu") or {}
    tol = float(_thr.get("audio_lead_detect_ms", 45) or 45)
    # 内容级被判“无法确定/无法评估”时其偏移不可信：不得据此做跨层归因，否则报告自相矛盾
    # （正文写“无法确定”，联合诊断却笃定“推流前管线失同步”）。
    _verdict = str(lip.get("verdict") or "")
    if _verdict.startswith(("无法确定", "无法评估")):
        return (f"内容级 {c:+.0f} ms（判定：{_verdict}）｜ 时间戳级 {s:+.0f} ms → "
                "内容级本次不可信，不做跨层归因；跨层结论以时间戳级为准，"
                "建议在主播正脸说话时段重新采样后复核。")
    parts = [f"内容级 {c:+.0f} ms ｜ 时间戳级 {s:+.0f} ms ｜ 差值 {diff:+.0f} ms（对齐容差 ±{tol:.0f} ms）"]
    if abs(s) <= tol and abs(c) >= 60:
        parts.append("结论：时间戳自洽、内容错位 → 不同步产生于【推流前管线】"
                     "（采集卡/摄像头/声卡/前置处理，进 OBS 之前就已烘焙进内容）。"
                     "时间戳级检测无法发现，须在采集或前置处理端校正。")
    elif abs(s) >= 60 and abs(c) <= tol:
        parts.append("结论：内容对齐、时间戳错位 → 属【封装/时间戳】层面问题，"
                     "多数播放器可自动修正，观众感知有限。")
    else:
        parts.append("结论：两者量级接近 → 偏移可能同源（既在内容层也在时间戳层），"
                     "或受样本长度/场景影响，建议延长采样复核。")
    return " ".join(parts)


# ─────────────────────────── 报告 / 落盘 ───────────────────────────

def build_text_report(result: dict) -> str:
    m = result.get("metrics") or {}
    lines = ["=" * 60, f"  人脸嘴型音画同步检测（内容级） - {result.get('source', '?')}", "=" * 60,
             f"  判定: {result.get('verdict', '—')}（等级 {result.get('verdict_level', '—')}）",
             f"  方法: {result.get('method', '')}"]
    if m:
        lines += [
            f"  内容级偏移: {m.get('peak_lag_ms')} ms（{m.get('peak_lag_frames')} 帧）"
            f" | 相关峰 r={m.get('peak_correlation')}"
            f" | 人脸覆盖 {m.get('face_coverage')}",
            f"  分段滞后(帧): {m.get('segments')} | 离散 {m.get('lag_spread_ms')} ms"
            f" | 一致性 {m.get('segment_consistency')}"
            f" | 说话检出 {m.get('talking_detected')}",
        ]
    lines.append("  " + (result.get("verdict_detail") or "").replace("\n", "\n  "))
    lines.append("=" * 60)
    return "\n".join(lines)


def save_outputs(result: dict, output_dir: Path, ts: Optional[str] = None) -> tuple:
    ensure_dirs()
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"lip_sync_analysis_{ts}.json"
    txt_path = output_dir / f"lip_sync_report_{ts}.txt"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(build_text_report(result), encoding="utf-8")
    return json_path, txt_path


def run_lip_sync_analysis(sample: str | Path, output_dir: Path, ts: str,
                          room_id: Optional[str] = None, *, fps: int = DEFAULT_FPS,
                          seconds: Optional[float] = None, roi: Optional[list] = None,
                          use_face: bool = True, max_seconds: float = DEFAULT_SECONDS,
                          fine_fps: int = FINE_FPS, use_fine: bool = True,
                          skip_specificity: bool = False) -> dict:
    """供 analyze_room / live_trace 调用的入口：分析已有样本并落盘。"""
    result = analyze_media(sample, fps=fps, max_seconds=max_seconds, seconds=seconds,
                           roi=roi, use_face=use_face, room_id=room_id,
                           fine_fps=fine_fps, use_fine=use_fine,
                           skip_specificity=skip_specificity)
    result["session_ts"] = ts
    json_path, txt_path = save_outputs(result, Path(output_dir), ts=ts)
    result["output_files"] = [str(json_path), str(txt_path)]
    return result


# ─────────────────────────── CLI ───────────────────────────

def _parse_roi(text: str):
    try:
        vals = [float(v) for v in text.replace(" ", "").split(",")]
    except ValueError:
        return None
    return vals if len(vals) == 4 else None


def main() -> int:
    ap = argparse.ArgumentParser(description="人脸嘴型音画同步检测（内容级）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="媒体文件（FLV/MP4/TS…）")
    src.add_argument("--url", help="流地址（下载样本后分析）")
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"人脸定位帧率（默认 {DEFAULT_FPS}）")
    ap.add_argument("--fine-fps", type=int, default=FINE_FPS,
                    help=f"高精度通道帧率（默认 {FINE_FPS}，即 10ms 分辨率）")
    ap.add_argument("--no-fine", action="store_true", help="关闭高精度通道（回退逐帧，精度约 40ms）")
    ap.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help="分析时长秒数")
    ap.add_argument("--no-specificity", dest="skip_specificity", action="store_true",
                    help="跳过「嘴型特异性」门禁（已知干净场景可用；报告会标注为已跳过）")
    ap.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024, help="URL 模式最大下载字节")
    ap.add_argument("--roi", help="手动嘴部 ROI，相对坐标 x,y,w,h（0..1），跳过人脸检测")
    ap.add_argument("--no-face", action="store_true", help="禁用人脸检测（需配合 --roi）")
    ap.add_argument("--room-id", help="房间号（写入 JSON 供报告关联）")
    ap.add_argument("--output-dir", default=str(OUTPUT_DIR))
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    target = args.file
    tmp_path = None
    if args.url:
        import requests  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        resp = requests.get(args.url, stream=True, timeout=15)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".flv")
        got = 0
        for chunk in resp.iter_content(65536):
            tmp.write(chunk)
            got += len(chunk)
            if got >= args.max_bytes:
                break
        tmp.close()
        tmp_path = target = tmp.name

    roi = _parse_roi(args.roi) if args.roi else None
    result = analyze_media(target, fps=args.fps, seconds=args.seconds, roi=roi,
                           use_face=not args.no_face, room_id=args.room_id,
                           fine_fps=max(20, int(args.fine_fps or FINE_FPS)),
                           use_fine=not args.no_fine,
                           skip_specificity=bool(getattr(args, "skip_specificity", False)))
    if not args.no_save:
        json_path, txt_path = save_outputs(result, Path(args.output_dir))
        logger.info("JSON 已保存: %s", json_path)
        logger.info("报告已保存: %s", txt_path)
    print(build_text_report(result))
    if tmp_path:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
