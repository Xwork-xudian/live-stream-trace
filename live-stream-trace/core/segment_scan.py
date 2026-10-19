#!/usr/bin/env python3
"""健康片段自动选段（内容级音画同步用）。

问题：直播样本里只有「主播正脸持续说话」的那几秒才可能测出可信的内容级偏移；
整段直接测会把不说话/走动/产品特写的时段混进来，导致相关性被打散。

做法：
  1. **单次抽帧**（不重复调 ffmpeg）→ 逐帧人脸框 + 嘴部运动 + 语音包络 + 对照区相关；
  2. 以 win_s 为窗、step_s 为步长**滑窗打分**（人脸覆盖/抖动/嘴动/语音活动/相关性/特异性 + 邻窗连续性）；
  3. 选出得分最高且满足门槛的最长连续片段 → **导出小播放器用的 MP4** 与 **人脸/嘴部截图**；
  4. 在该片段上复测偏移（复用 lip_sync_detector，手动 ROI → 指标与全样本口径一致）。
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CLIP_FFMPEG_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart")


def _ffmpeg() -> str | None:
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for c in ("/opt/ffmpeg/bin/ffmpeg",
              "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(c).exists():
            return c
    return None


def export_clip(src: Path, start: float, end: float, dest: Path, max_w: int = 480) -> bool:
    """导出片段为浏览器可播的 H.264 MP4（小播放器用）。"""
    exe = _ffmpeg()
    if not exe:
        return False
    dur = max(0.5, end - start)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-vf", f"scale='min({max_w},iw)':-2", *CLIP_FFMPEG_ARGS, str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def export_crops(src: Path, times: list, box_src, out_dir: Path, prefix: str) -> list:
    """在片段的若干时刻导出「人脸框」与「嘴部放大」截图（PNG）。

    box_src=(x, y, w, h) 为**人脸框**源像素坐标；嘴部取其人脸框下部区域。
    """
    exe = _ffmpeg()
    if not exe or not box_src:
        return []
    out_dir = Path(out_dir)
    x, y, w, h = (int(round(v)) for v in box_src)
    w -= w % 2
    h -= h % 2
    if w < 16 or h < 16:
        return []
    mx, my = x + int(w * 0.25), y + int(h * 0.60)
    mw, mh = max(16, int(w * 0.50)) // 2 * 2, max(16, int(h * 0.40)) // 2 * 2
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list = []
    for i, t in enumerate(times, 1):
        for tag, (cx, cy, cw, ch), sw in (("face", (x, y, w, h), 260),
                                          ("mouth", (mx, my, mw, mh), 220)):
            dest = out_dir / f"{prefix}_{tag}_{i}.png"
            cmd = [exe, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                   "-vf", f"crop={cw}:{ch}:{cx}:{cy},scale={sw}:-2", str(dest)]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                out.append(dest.name)
    return out


def scan_windows(src: Path, *, win_s: float = 6.0, step_s: float = 1.5, fps: int = 25,
                 max_seconds: float = 60.0, max_lag_ms: int = 600) -> dict:
    """滑窗打分，返回 {windows, best, meta}。单次抽帧，不重复解码。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import numpy as np
    import lip_sync_detector as L

    meta = L.probe_media(src)
    if not (meta.get("has_video") and meta.get("has_audio")):
        return {"windows": [], "best": None, "meta": meta, "error": "缺音视频轨道"}
    dur = min(float(meta.get("duration") or max_seconds), max_seconds)
    size = L.scaled_size(meta.get("width") or 0, meta.get("height") or 0)
    frames = L.extract_video_frames(src, dur, fps, size)
    pcm = L.extract_audio_pcm(src, dur)
    if frames is None or pcm is None or frames.shape[0] < int(2 * fps):
        return {"windows": [], "best": None, "meta": meta, "error": "抽帧/抽音失败"}
    # 人脸后端：YuNet（关键点 → 紧嘴部 ROI + 正脸度）优先，退化 Haar
    backend = L.get_face_backend()
    if backend is not None and backend.available:
        faces_multi = backend.detect_multi(frames, top_k=1)
    else:
        cascades = L._load_cascades()
        _raw = L.detect_faces_multi(frames, cascades, top_k=1) if cascades else [[]] * frames.shape[0]
        faces_multi = [[{"box": b, "lm": None, "score": None, "source": "haar"} for b in bl]
                       for bl in _raw]
    boxes = [(fl[0]["box"] if fl else None) for fl in faces_multi]
    rois, coverage, jitter, _fmed = L.rois_from_faces_multi(faces_multi, frames.shape[1:], 0)
    if rois is None:
        return {"windows": [], "best": None, "meta": meta, "error": "整段未检出人脸"}
    # 逐帧正脸度（仅关键点后端可用时非 NaN）
    front = np.full(frames.shape[0], np.nan, dtype=np.float32)
    try:
        import face_backend as _FB  # noqa: PLC0415
        for _i, _fl in enumerate(faces_multi):
            if _fl:
                _f = _FB.frontality(_fl[0])
                if _f is not None:
                    front[_i] = _f
    except Exception:  # noqa: BLE001
        pass
    env = L.audio_envelope(pcm, fps)
    m, _open = L.mouth_motion_signal(frames, rois)
    # 嘴部遮挡（手/物体）：纹理相对人脸塌陷 → 窗口降权
    vis_ser = None
    try:
        import face_backend as _FBo  # noqa: PLC0415
        vis_ser = _FBo.visibility_series(frames, rois, faces_multi, 0, stride=2)
    except Exception:  # noqa: BLE001
        vis_ser = None
    # 语音活动（VAD-lite）：判断窗口是否「主播真在说话」（区别于 BGM）
    va = None
    try:
        import voice_activity as _VA  # noqa: PLC0415
        _va_full, _ = _VA.voice_activity(pcm, fps)
        va = _va_full if len(_va_full) else None
    except Exception:  # noqa: BLE001
        va = None

    # 一个“对照区”（人脸框下方 = 胸前/产品区），用于粗略特异性打分
    np_med = np.median(np.asarray(rois, dtype=np.float32), axis=0)
    ctrls = L.control_rois_from(np_med, frames.shape[1:], max_controls=1)
    m_ctrl = None
    if ctrls:
        m_ctrl, _ = L.mouth_motion_signal(
            frames, np.tile(np.asarray(ctrls[0], dtype=np.float32), (frames.shape[0], 1)))

    n = min(len(m), len(env))
    win = max(int(round(win_s * fps)), 24)
    step = max(1, int(round(step_s * fps)))
    mlf = max(1, int(round(max_lag_ms / 1000.0 * fps)))
    boxes_arr = np.array([[b if b else (np.nan, np.nan, np.nan, np.nan)] for b in boxes],
                         dtype=np.float32).reshape(-1, 4)
    thr_speech = float(np.percentile(env, 70)) if len(env) else 0.0

    wins: list = []
    for s in range(0, max(1, n - win + 1), step):
        e = s + win
        r_m = L.cross_correlate(env[s:e], m[s:e], mlf)[3]
        r_c = (L.cross_correlate(env[s:e], m_ctrl[s:e], mlf)[3] if m_ctrl is not None else None)
        seg_b = boxes_arr[s:e]
        cov = float(np.mean(~np.isnan(seg_b[:, 0]))) if len(seg_b) else 0.0
        centers = seg_b[~np.isnan(seg_b[:, 0])]
        jit = float(np.std(centers[:, 0]) + np.std(centers[:, 1])) if len(centers) > 2 else 0.0
        act = float(m[s:e].std() / (m[s:e].mean() + 1e-6))
        sp = float((env[s:e] > thr_speech).mean())
        # 语音活动优先（区分说话 vs BGM），缺失时退回包络活动
        if va is not None and len(va) >= e:
            speech_val = float(np.mean(va[s:e]))
        else:
            speech_val = sp
        _frw = front[s:e]
        front_med = (float(np.nanmedian(_frw)) if np.isfinite(_frw).any() else None)
        if vis_ser is not None:
            _mask_w, _thr_w, _ = L.occlusion_mask(vis_ser[s:e])
            occ_frac = float(np.mean(_mask_w)) if _mask_w is not None else None
        else:
            occ_frac = None
        rm = 0.0 if r_m is None else abs(float(r_m))
        rc = 0.0 if r_c is None else abs(float(r_c))
        spec = rm >= rc + 0.08
        # 打分（0~100）：覆盖/稳定性/说话/语音/相关性/特异性/正脸度/遮挡
        front_term = (0.0 if front_med is None
                      else max(0.0, min(1.0, (front_med - 0.2) / 0.6)))
        occ_term = (1.0 if occ_frac is None else max(0.0, 1.0 - occ_frac / 0.5))
        score = (100.0 * (0.14 * min(1.0, cov / 0.7)
                          + 0.08 * max(0.0, 1.0 - jit / 12.0)
                          + 0.14 * min(1.0, act / 0.6)
                          + 0.14 * min(1.0, speech_val / 0.45)
                          + 0.20 * min(1.0, rm / 0.4)
                          + 0.10 * (1.0 if spec else 0.0)
                          + 0.10 * front_term
                          + 0.10 * occ_term))
        wins.append({"start": round(s / fps, 2), "end": round(e / fps, 2), "score": round(score, 1),
                     "face_cov": round(cov, 3), "face_jitter_px": round(jit, 1),
                     "motion_activity": round(act, 3), "speech_activity": round(speech_val, 3),
                     "env_activity": round(sp, 3),
                     "frontality": (round(front_med, 3) if front_med is not None else None),
                     "occlusion": (round(occ_frac, 3) if occ_frac is not None else None),
                     "r_mouth": round(rm, 3), "r_control": round(rc, 3),
                     "mouth_specific": bool(spec), "index": len(wins)})
    if not wins:
        return {"windows": [], "best": None, "meta": meta, "error": "样本过短，无法滑窗"}
    # 邻窗连续性加权：避免选到孤立的“碰巧高”窗口
    for w in wins:
        nb = [x["score"] for x in wins if abs(x["index"] - w["index"]) <= 1 and x is not w]
        w["score_effective"] = round(w["score"] + (0.3 * sum(nb) / len(nb) if nb else 0.0), 1)
    best = max(wins, key=lambda w: w["score_effective"])
    # 选取以 best 为中心、连续的合格窗口集合，拼成最终片段
    good = {w["index"] for w in wins if w["score_effective"] >= max(45.0, best["score_effective"] - 12)}
    i0 = best["index"]
    while i0 - 1 in good:
        i0 -= 1
    i1 = best["index"]
    while i1 + 1 in good:
        i1 += 1
    seg = {"start": wins[i0]["start"], "end": wins[i1]["end"],
           "wins": i1 - i0 + 1, "best_window": best}
    return {"windows": wins, "best": seg, "meta": meta}


def measure_segment(src: Path, start: float, end: float, *, fit_rel_roi=None,
                    seconds: float | None = None, fine_fps: int = 200) -> dict:
    """用统一口径在片段上复测内容级偏移（手动 ROI 优先，保证 ROI 就是主播嘴部）。"""
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lip_sync_detector as L

    exe = _ffmpeg()
    if not exe:
        return {}
    with tempfile.TemporaryDirectory() as td:
        clip = Path(td) / "seg.flv"
        cmd = [exe, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
               "-t", f"{max(1.0, end - start):.3f}", "-c", "copy", "-f", "flv", str(clip)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
        except subprocess.TimeoutExpired:
            return {}
        if r.returncode != 0 or not clip.exists():
            return {}
        res = L.analyze_media(clip, use_face=True, seconds=seconds or (end - start),
                             fine_fps=fine_fps)
        if "未检出人脸" in (res.get("verdict") or "") and fit_rel_roi:
            res = L.analyze_media(clip, roi=fit_rel_roi, use_face=False,
                                  seconds=seconds or (end - start), fine_fps=fine_fps)
    return res


def _face_box_src(src: Path, start: float, end: float, meta: dict) -> tuple | None:
    """片段内的人脸框中位（换算到源像素），供截图裁剪使用。"""
    import numpy as np
    import lip_sync_detector as L
    size = L.scaled_size(meta.get("width") or 0, meta.get("height") or 0)
    frames = L.extract_video_frames(src, end, 25, size)
    if frames is None:
        return None
    i0 = max(0, int(start * 25))
    sub = frames[i0:]
    if sub.shape[0] < 3:
        return None
    # 优先用后端（YuNet）的真实人脸框；退化时用 Haar 框
    backend = L.get_face_backend()
    fboxes: list = []
    if backend is not None and backend.available:
        fm = backend.detect_multi(sub, top_k=1)
        fboxes = [fl[0]["box"] for fl in fm if fl]
    if not fboxes:
        fboxes = [(x[0] if x else None) for x in L.detect_faces_multi(sub, L._load_cascades(), 1)]
        fboxes = [b for b in fboxes if b]
    if not fboxes:
        return None
    sx = (meta.get("width") or size[0]) / float(size[0])
    sy = (meta.get("height") or size[1]) / float(size[1])
    fb = np.median(np.asarray(fboxes, dtype=np.float32), axis=0)
    return (float(fb[0]) * sx, float(fb[1]) * sy, float(fb[2]) * sx, float(fb[3]) * sy)


def run_segment_scan(src, ts: str, out_dir, *, win_s: float = 6.0, step_s: float = 1.5,
                     max_seconds: float = 60.0, measure: bool = True) -> dict:
    """完整流程：滑窗选段 → 导出小播放器片段与人脸/嘴部截图 → 在片段上复测 → 落盘 JSON。"""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scan = scan_windows(src, win_s=win_s, step_s=step_s, max_seconds=max_seconds)
    out: dict = {"type": "segment_scan", "timestamp": ts, "source": str(src),
                 "window_seconds": win_s, "step_seconds": step_s,
                 "scan_meta": {k: (v if not isinstance(v, float) else round(v, 2))
                               for k, v in (scan.get("meta") or {}).items()
                               if k in ("duration", "width", "height", "fps", "vcodec", "acodec")},
                 "windows": scan.get("windows") or [], "best": scan.get("best"),
                 "error": scan.get("error")}
    b = scan.get("best")
    if not b:
        (out_dir / f"segment_scan_{ts}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out
    meta = scan.get("meta") or {}
    box_src = _face_box_src(src, b["start"], b["end"], meta)
    prefix = f"segment_{ts}"
    span = max(1.0, float(b["end"]) - float(b["start"]))
    times = [float(b["start"]) + span * f for f in (0.2, 0.5, 0.8)]
    crops = export_crops(src, times, box_src, out_dir, prefix) if box_src else []
    clip_path = out_dir / f"{prefix}_clip.mp4"
    clip_ok = export_clip(src, b["start"], b["end"], clip_path)
    out["clip"] = clip_path.name if clip_ok else None
    out["crops"] = crops
    out["face_box_src"] = [int(v) for v in box_src] if box_src else None
    if measure:
        res = measure_segment(src, b["start"], b["end"])
        m, p = (res.get("metrics") or {}), (res.get("perceptual") or {})
        sp = m.get("mouth_specificity") or {}
        out["measurement"] = {
            "verdict": res.get("verdict"), "verdict_level": res.get("verdict_level"),
            "content_offset_ms": p.get("content_offset_ms"),
            "ci_ms": p.get("content_offset_ci_ms"),
            "window_agreement": p.get("window_agreement"),
            "resolution_ms": p.get("lag_resolution_ms"),
            "direction": p.get("direction"),
            "r": m.get("peak_correlation"), "r_robust": m.get("robust_correlation"),
            "mouth_r": sp.get("mouth_r_coarse"), "control_r": sp.get("control_r_max"),
            "specific": sp.get("specific"), "face_coverage": m.get("face_coverage"),
            "talking": m.get("talking_detected"), "media_span_s": m.get("media_span_s"),
            "lag_curve": m.get("lag_curve") or [], "detail": res.get("verdict_detail"),
        }
    (out_dir / f"segment_scan_{ts}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ─────────────────────────── 采样端规避：等到「可测」窗口再采样 ───────────────────────────
# 动机：内容级测不准的头号原因是「采到的这段内容不适合」（非正脸 / 嘴被手挡 / 没在说话）。
# 与其采一段就下结论，不如**持续采样 + 就绪判据**，找到正脸、未遮挡、在说话的窗口再测。

READY_MIN_FRONT = 0.5      # 正脸度下限
READY_MAX_OCC = 0.30       # 遮挡帧占比上限
READY_MIN_SPEECH = 0.20    # 语音活动（说话占比）下限
READY_MIN_MOTION = 0.30    # 嘴部运动活动度下限


def readiness_of_window(w, *, min_front: float = READY_MIN_FRONT, max_occ: float = READY_MAX_OCC,
                        min_speech: float = READY_MIN_SPEECH,
                        min_motion: float = READY_MIN_MOTION):
    """单窗口「可测就绪」判据 → (ready, reasons)。缺失的指标不参与否决。"""
    reasons = []
    fr = w.get("frontality")
    if fr is not None and float(fr) < min_front:
        reasons.append(f"非正脸 {float(fr):.2f}<{min_front}")
    occ = w.get("occlusion")
    if occ is not None and float(occ) > max_occ:
        reasons.append(f"可能遮挡 {float(occ) * 100:.0f}%>{max_occ * 100:.0f}%")
    sp = w.get("speech_activity")
    if sp is not None and float(sp) < min_speech:
        reasons.append(f"说话少 {float(sp):.2f}<{min_speech}")
    mv = w.get("motion_activity")
    if mv is not None and float(mv) < min_motion:
        reasons.append(f"嘴动弱 {float(mv):.2f}<{min_motion}")
    return (len(reasons) == 0), reasons


def export_span_copy(src, start: float, end: float, dest) -> bool:
    """按时间区间**流拷贝**截取子样本（不重编码，保留嘴部纹理细节）。

    注意：不能用 libx264 重编码——那会把唇形所需要的高频纹理抹掉。
    """
    exe = _ffmpeg()
    if not exe:
        return False
    dur = max(1.0, end - start)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
           "-t", f"{dur:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def acquire_ready_sample(capture_fn, *, out_dir, ts: str, chunk_s: float = 20.0,
                         win_s: float = 10.0, max_attempts: int = 3,
                         max_seconds: float = 120.0, scan_fn=None, log=print) -> dict:
    """持续采样，直到找到「正脸 + 未遮挡 + 在说话」的窗口；找不到则给出最佳候选。

    capture_fn(dest: Path, seconds: float) -> bool   # 采集一段 chunk 写入 dest
    scan_fn(src, win_s=..., max_seconds=...) -> dict # 默认 scan_windows
    返回 {"ready", "attempts", "sample", "window", "reason", "elapsed_s"}
    """
    import time as _time
    out_dir = Path(out_dir)
    scan_fn = scan_fn or scan_windows
    t0 = _time.monotonic()
    attempts: list = []
    best_fallback = None            # (score, chunk_path, window)
    for i in range(max(1, int(max_attempts))):
        if _time.monotonic() - t0 >= max_seconds:
            break
        chunk = out_dir / f"acq_{ts}_a{i}.flv"
        try:
            ok = capture_fn(chunk, chunk_s)
        except Exception as exc:  # noqa: BLE001
            log(f"   [采样规避] 第 {i + 1} 次采集失败: {exc}")
            ok = False
        if not ok or not Path(chunk).exists() or Path(chunk).stat().st_size < 4096:
            attempts.append({"attempt": i + 1, "captured": False})
            continue
        try:
            sc = scan_fn(chunk, win_s=win_s, max_seconds=chunk_s)
        except Exception as exc:  # noqa: BLE001
            log(f"   [采样规避] 第 {i + 1} 次扫描失败: {exc}")
            attempts.append({"attempt": i + 1, "captured": True, "scan_error": str(exc)})
            continue
        if sc.get("error"):
            log(f"   [采样规避] 第 {i + 1} 轮：无法打分（{sc.get('error')}），继续")
        wins = sc.get("windows") or []
        ready = []
        for w in wins:
            ok_w, reasons = readiness_of_window(w)
            w = {**w, "reasons": reasons}
            if ok_w:
                ready.append(w)
        best = max(wins, key=lambda w: w.get("score") or 0, default=None)
        rec = {"attempt": i + 1, "captured": True, "windows": len(wins),
               "best_score": (best or {}).get("score"), "best_start": (best or {}).get("start"),
               "ready_windows": len(ready),
               "best_reasons": (best or {}).get("reasons") if best else None}
        attempts.append(rec)
        log(f"   [采样规避] 第 {i + 1} 轮：窗口 {len(wins)} 个，就绪 {len(ready)} 个"
            + (f"，最佳分 {rec['best_score']}"
               + (f"（{best.get('start')}~{best.get('end')}s）" if best else "") if best else "，无窗口")
            + (f"；最佳窗口未达标原因 {rec['best_reasons']}" if (best and rec['best_reasons']) else ""))
        if best is not None:
            if best_fallback is None or (best.get("score") or 0) > best_fallback[0]:
                best_fallback = (best.get("score") or 0, chunk, best)
        if ready:
            pick = max(ready, key=lambda w: w.get("score") or 0)
            dest = out_dir / f"acq_{ts}_ready.flv"
            if export_span_copy(chunk, pick["start"], pick["end"], dest):
                log(f"   [采样规避] 第 {i + 1} 次找到可测窗口 "
                    f"{pick['start']}~{pick['end']}s（分 {pick.get('score')}，"
                    f"正脸 {pick.get('frontality')}，遮挡 {pick.get('occlusion')}，"
                    f"说话 {pick.get('speech_activity')}）")
                return {"ready": True, "attempts": attempts, "sample": dest, "window": pick,
                        "chunk": chunk, "reason": "已找到正脸/未遮挡/在说话的窗口",
                        "elapsed_s": round(_time.monotonic() - t0, 1)}
            log(f"   [采样规避] 第 {i + 1} 次找到可测窗口但截取失败，继续")
    # 未找到：回退到历史最佳窗口（若其分数尚可），否则失败
    if best_fallback and (best_fallback[0] or 0) > 0:
        _sc, chunk, w = best_fallback
        dest = out_dir / f"acq_{ts}_best.flv"
        if export_span_copy(chunk, w["start"], w["end"], dest):
            return {"ready": False, "attempts": attempts, "sample": dest, "window": w,
                    "chunk": chunk, "reason": "未找到完全达标的窗口，回退历史最佳窗口",
                    "elapsed_s": round(_time.monotonic() - t0, 1)}
    return {"ready": False, "attempts": attempts, "sample": None, "window": None,
            "reason": "多次采样仍未找到可测窗口", "elapsed_s": round(_time.monotonic() - t0, 1)}
