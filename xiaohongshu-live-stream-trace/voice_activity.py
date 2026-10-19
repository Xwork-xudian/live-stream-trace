"""语音活动（VAD-lite）：逐帧「像人在说话」的软指标，用于带货直播的「说话时段」识别。

动机：商铺/带货直播的音频常被 BGM/旁白主导，嘴部运动与「声音包络」的相关性因此被稀释。
在**真正的主播说话片段**上测量，才有机会得到可信的内容级偏移。本模块给出一个不依赖
外部模型、纯 numpy 的软指标 v(t) ∈ [0,1]，供片段选择与滑窗加权使用（不作为硬门禁）。

证据来源（两条互补）：
  1) 语音频带能量占比：人声能量集中在 ~300–3400Hz，纯 BGM 低频轰鸣/宽带噪声占比低；
  2) 音节率调制：语音包络在 2–8Hz（音节切换率）有明显调制能量。

二者相乘并做稳健归一化（p90）→ 0..1。
"""
from __future__ import annotations

from typing import Optional

AUDIO_RATE_DEFAULT = 16000
VOICE_LO = 300.0
VOICE_HI = 3400.0
MOD_LO = 2.0
MOD_HI = 8.0


def _np():
    try:
        import numpy as np  # noqa: PLC0415
        return np
    except Exception:  # noqa: BLE001
        return None


def _band_pass(x, lo: float, hi: float, sr: float):
    """FFT 频带限（零相位，长度不变）。"""
    np = _np()
    n = len(x)
    if n < 64:
        return x
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    keep = (freqs >= lo) & (freqs <= hi)
    spec[~keep] = 0.0
    return np.fft.irfft(spec, n=n).astype(np.float32)


def _rms_frames(x, hop: int):
    """按 hop 分窗 RMS（对齐 audio_envelope 的口径）。"""
    np = _np()
    hop = max(1, int(hop))
    k = len(x) // hop
    if k < 2:
        return np.zeros(0, dtype=np.float32)
    seg = x[:k * hop].reshape(k, hop)
    return np.sqrt((seg.astype(np.float64) ** 2).mean(axis=1)).astype(np.float32)


def _modulation_energy(env, fps: float, lo: float, hi: float):
    """包络在 [lo, hi] Hz 的调制能量（去均值 + 频带限 + 平方平滑）。"""
    np = _np()
    n = len(env)
    if n < 16 or fps <= 2 * hi:
        return np.ones(n, dtype=np.float32)
    e = env.astype(np.float64)
    e = e - e.mean()
    mod = _band_pass(e, lo, hi, fps).astype(np.float64)
    p = mod ** 2
    # 轻度时间平滑（约 0.25s），避免逐帧抖动
    k = max(1, int(round(0.25 * fps)))
    if k > 1 and n > k:
        ker = np.ones(k) / k
        p = np.convolve(p, ker, mode="same")
    return p.astype(np.float32)


def voice_activity(pcm, fps: float, sr: int = AUDIO_RATE_DEFAULT,
                   lo: float = VOICE_LO, hi: float = VOICE_HI,
                   mod_lo: float = MOD_LO, mod_hi: float = MOD_HI):
    """逐帧语音活动 v(t) ∈ [0,1]（长度 ≈ 秒数×fps）。返回 (v, stats)。

    stats 含 voice_ratio_median（语音带占比）、syllabic_median（音节调制）、
    active_fraction（v ≥ 0.5 的帧占比，即「说话占比」）。
    """
    np = _np()
    empty = (np.zeros(0, dtype=np.float32), {"voice_ratio_median": None,
                                             "syllabic_median": None,
                                             "active_fraction": None})
    if np is None or pcm is None or len(pcm) < max(1024, sr // 2) or fps <= 4:
        return empty
    hop = max(1, int(round(sr / float(fps))))
    p_voice = _band_pass(pcm, lo, hi, sr)
    e_v = _rms_frames(p_voice, hop)
    e_a = _rms_frames(pcm, hop)
    n = min(len(e_v), len(e_a))
    if n < 4:
        return empty
    e_v, e_a = e_v[:n], e_a[:n]
    ratio = e_v / (e_a + 1e-6)
    syll = _modulation_energy(e_v, fps, mod_lo, mod_hi)
    # 归一化到可比尺度：各自除掉中位数，乘积后按 p90 收敛到 1
    r_med = float(np.median(ratio)) + 1e-6
    s_med = float(np.median(syll)) + 1e-9
    raw = (ratio / r_med) * (syll / s_med)
    p90 = float(np.percentile(raw, 90)) + 1e-9
    v = np.clip(raw / p90, 0.0, 1.0).astype(np.float32)
    stats = {
        "voice_ratio_median": round(float(np.median(ratio)), 3),
        "syllabic_median": round(float(np.median(syll)), 6),
        "active_fraction": round(float((v >= 0.5).mean()), 3),
    }
    return v, stats


def resample_to(v, src_fps: float, dst_len: int):
    """把 v(t) 线性插值到 dst_len 长度（用于与另一路信号对齐）。"""
    np = _np()
    if np is None or v is None or len(v) < 2 or dst_len < 2:
        return None
    x = np.linspace(0.0, 1.0, len(v))
    xi = np.linspace(0.0, 1.0, dst_len)
    return np.interp(xi, x, v).astype(np.float32)


if __name__ == "__main__":  # 自测
    import numpy as np
    sr = 16000
    fps = 25
    # 构造：0–2s 静音，2–4s「语音」(300Hz 基频 + 谐波 + 音节调制)，4–6s 纯低频 BGM(80Hz 正弦)
    t = np.arange(0, 6, 1.0 / sr)
    sig = np.zeros_like(t)
    sp = (t >= 2) & (t < 4)
    if sp.any():
        ts = t[sp]
        f0 = 180.0
        harm = sum(np.sin(2 * np.pi * f0 * k * ts) / k for k in range(1, 8))
        sig[sp] = harm * (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * ts))   # 5Hz 音节调制
    bg = (t >= 4)
    sig[bg] = np.sin(2 * np.pi * 80 * t[bg]) * 0.5
    v, st = voice_activity(sig.astype(np.float32), fps, sr)
    print("stats", st)
    seg = lambda a, b: float(np.mean(v[int(a * fps):int(b * fps)]))
    print("silence 0-2s v=%.2f | speech 2-4s v=%.2f | bgm 4-6s v=%.2f" % (seg(0, 2), seg(2, 4), seg(4, 6)))
    ok = seg(2, 4) > seg(4, 6) and seg(2, 4) > seg(0, 2)
    print("✅ 语音段活动度最高" if ok else "❌ 未区分")
