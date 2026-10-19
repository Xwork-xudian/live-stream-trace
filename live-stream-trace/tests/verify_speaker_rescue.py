#!/usr/bin/env python3
"""说话时段「兜底改判」回归：全段被 BGM 稀释时，用说话子集恢复可信估计。

构造信号（不依赖 ffmpeg）：前半段=干净语音（嘴动与音频包络严格同源，注入已知滞后），
后半段=响但无关的 BGM（嘴部几乎不动）→ 全段互相关被稀释；说话子集应恢复出正确偏移。
同时校验 `speaker_gate_eligible` 的门槛（秒数/相关/滑窗/CI/边界）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import lip_sync_detector as L  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'\u2705' if cond else '\u274c'} {name} {extra}")
    if not cond:
        fails.append(name)


FPS = 200
DUR = 12.0
SPEECH_END = 6.0
TRUE_FRAMES = 8                      # 注入滞后 8 帧 = 40ms
n = int(DUR * FPS)
t = np.arange(n) / FPS
rng = np.random.default_rng(3)

# 语音段：音频包络 = 平滑非周期噪声（避免周期信号造成滞后多峰），嘴动 = 同一包络平移 TRUE_FRAMES
sp = t < SPEECH_END
env = np.zeros(n, dtype=np.float64)
raw = rng.normal(1.0, 0.6, int(sp.sum()))
k = 8                                   # ~40ms 平滑
raw = np.convolve(raw, np.ones(k) / k, mode="same")
env[sp] = np.clip(raw, 0.0, None)
motion = np.roll(env, TRUE_FRAMES)
motion += rng.normal(0, 0.03, n)
# BGM 段：音频很响，且画面「类嘴动」与音频在**另一个滞后**上强相关（BGM 驱动的伪相关，
# 这正是真实带货直播里把全段结果带偏的成因）→ 全段估计会被这半段污染
_bgm = np.zeros(n, dtype=np.float64)
_bgm[~sp] = rng.normal(1.0, 0.6, int((~sp).sum()))
_bgm[~sp] = np.convolve(_bgm[~sp], np.ones(8) / 8, mode="same")
env[~sp] = np.clip(_bgm[~sp], 0.0, None)
motion[~sp] = 0.9 * np.roll(env, -60)[~sp] + rng.normal(0, 0.03, int((~sp).sum()))
env = env.astype(np.float32)
motion = motion.astype(np.float32)
weight = sp.astype(np.float64)       # 模拟「语音活动」掩码
mlf = max(1, int(round(600 / 1000.0 * FPS)))

print("\u2500\u2500 1) 全段 vs 说话子集 ─\u2500")
_, _, pl, pr = L.cross_correlate(env, motion, mlf)
print(f"   全段: 峰 r={pr:.3f} lag={pl * 1000.0 / FPS:+.1f}ms")
plain_est = L.multiscale_lag_estimates(env, motion, FPS, mlf, span_s=DUR)
plain_rob = L.robust_offset_from_windows(plain_est, 1000.0 / FPS)
print(f"   全段稳健: ok={plain_rob.get('ok')} off={plain_rob.get('offset_ms')} "
      f"CI={plain_rob.get('ci_ms')} used={plain_rob.get('used')}/{plain_rob.get('total')} "
      f"agree={plain_rob.get('agreement')}")

gl, gr, gcov = L.gated_correlation(env, motion, weight, mlf)
env_g = (env - float(np.average(env, weights=weight))) * weight
m_g = (motion - float(np.average(motion, weights=weight))) * weight
gest = L.multiscale_lag_estimates(env_g, m_g, FPS, mlf, span_s=DUR, mask=weight, min_cov=0.6)
gest += L.sliding_lag_estimates(env_g, m_g, FPS, mlf, win_s=3.0, step_s=1.0,
                               mask=weight, min_cov=0.8)
grob = L.robust_offset_from_windows(gest, 1000.0 / FPS)
print(f"   子集: 峰 r={gr:.3f} lag={gl * 1000.0 / FPS:+.1f}ms | 稳健={grob.get('offset_ms')} CI={grob.get('ci_ms')}")

expect_ms = TRUE_FRAMES / FPS * 1000.0
check("说话子集峰相关高于全段", gr > pr, f"-> {gr:.3f} vs {pr:.3f}")
check("说话子集恢复正确偏移（±6ms）", abs(grob.get("offset_ms", 1e9) - expect_ms) <= 6.0,
      f"-> {grob.get('offset_ms')} vs {expect_ms:.0f}")

print("\u2500\u2500 2) speaker_gate_eligible 门槛 \u2500\u2500")
gate_ok = {
    "offset_ms": round(grob.get("offset_ms"), 2), "r": round(float(gr), 3),
    "seconds": round(float(weight.sum()) / FPS, 2), "coverage": round(float(gcov), 3),
    "peak_lag_frames": int(gl),
    "robust_offset_ms": round(grob.get("offset_ms"), 2),
    "ci_ms": round(grob.get("ci_ms"), 2), "agreement": round(grob.get("agreement"), 3),
    "windows_used": grob.get("used"), "windows_total": grob.get("total"),
}
res_ms = 1000.0 / FPS
print("   gate:", gate_ok)
check("达标 → 可兜底改判", L.speaker_gate_eligible(gate_ok, res_ms, mlf) is True)
check("样本过短 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "seconds": 1.0}, res_ms, mlf) is False)
check("相关过低 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "r": 0.12}, res_ms, mlf) is False)
check("滑窗不足 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "windows_used": 2}, res_ms, mlf) is False)
check("一致率过低 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "agreement": 0.3}, res_ms, mlf) is False)
check("CI 过大 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "ci_ms": 500.0}, res_ms, mlf) is False)
check("峰值在搜索边界 → 拒绝",
      L.speaker_gate_eligible({**gate_ok, "peak_lag_frames": mlf}, res_ms, mlf) is False)
check("空/缺字段 → 拒绝",
      L.speaker_gate_eligible({}, res_ms, mlf) is False and L.speaker_gate_eligible(None, res_ms, mlf) is False)

print("\u2500\u2500 3) 端到端：全段被拒时按子集改判 \u2500\u2500")
result = {
    "metrics": {
        "roi_mode": "manual", "fps": FPS, "lag_resolution_ms": res_ms,
        "peak_lag_ms": pl * 1000.0 / FPS, "peak_lag_frames": pl, "peak_correlation": float(pr),
        "robust_offset_ms": plain_rob.get("offset_ms"), "robust_correlation": plain_rob.get("r_median"),
        "offset_ci_ms": plain_rob.get("ci_ms"), "window_agreement": plain_rob.get("agreement"),
        "windows_used": plain_rob.get("used"), "windows_total": plain_rob.get("total"),
        "media_span_s": DUR, "max_lag_frames": mlf, "face_coverage": 1.0,
        "talking_detected": True, "mouth_specificity": {"specific": True},
        "speaker_gate": gate_ok, "voice_activity": {"active_fraction": 0.5},
    },
    "perceptual": {},
}
L.build_verdict(result)
v, p = result.get("verdict"), result.get("perceptual") or {}
print("   verdict:", v)
print("   scope:", p.get("content_offset_scope"), "| speaker_gated:", p.get("speaker_gated"),
      "| offset:", p.get("content_offset_ms"))
check("全段不可信 → 触发说话子集兜底", p.get("speaker_gated") is True, f"-> {v}")
check("结论标注「仅在主播说话时段」", "仅在主播说话时段" in str(v), f"-> {v}")
check("偏移取自子集（≈40ms）", abs(float(p.get("content_offset_ms") or 1e9) - expect_ms) <= 6.0,
      f"-> {p.get('content_offset_ms')}")
check("作用域标为 speaker_subset", p.get("content_offset_scope") == "speaker_subset")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
