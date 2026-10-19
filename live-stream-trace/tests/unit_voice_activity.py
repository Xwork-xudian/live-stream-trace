#!/usr/bin/env python3
"""voice_activity 单元测试：语音/静音/BGM 区分、归一化范围、resample、边界。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import voice_activity as VA  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name} {extra}")
    if not cond:
        fails.append(name)


SR, FPS = 16000, 25


def synth():
    """0–2s 静音 | 2–4s 语音(180Hz 谐波 + 5Hz 音节调制) | 4–6s 低频 BGM(80Hz)。"""
    t = np.arange(0, 6, 1.0 / SR)
    sig = np.zeros_like(t)
    sp = (t >= 2) & (t < 4)
    ts = t[sp]
    harm = sum(np.sin(2 * np.pi * 180 * k * ts) / k for k in range(1, 8))
    sig[sp] = harm * (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * ts))
    bg = t >= 4
    sig[bg] = np.sin(2 * np.pi * 80 * t[bg]) * 0.5
    return sig.astype(np.float32)


print("── 1) 语音段活动度最高 ──")
v, st = VA.voice_activity(synth(), FPS, SR)
check("长度 ≈ 6s×25fps", abs(len(v) - 150) <= 2, f"→ {len(v)}")
seg = lambda a, b: float(np.mean(v[int(a * FPS):int(b * FPS)]))
s_sil, s_sp, s_bg = seg(0, 2), seg(2, 4), seg(4, 6)
check("语音 > BGM", s_sp > s_bg + 0.2, f"→ 语音 {s_sp:.2f} vs BGM {s_bg:.2f}")
check("语音 > 静音", s_sp > s_sil + 0.2, f"→ 语音 {s_sp:.2f} vs 静音 {s_sil:.2f}")
check("取值 ∈ [0,1]", float(v.min()) >= 0 and float(v.max()) <= 1, f"→ [{v.min():.2f}, {v.max():.2f}]")
check("stats 含 active_fraction", st.get("active_fraction") is not None, f"→ {st}")

print("── 2) 高帧率同样可用 ──")
v2, _ = VA.voice_activity(synth(), 200, SR)
seg2 = lambda a, b: float(np.mean(v2[int(a * 200):int(b * 200)]))
check("200fps 语音 > BGM", seg2(2, 4) > seg2(4, 6) + 0.2, f"→ {seg2(2,4):.2f} vs {seg2(4,6):.2f}")

print("── 3) resample ──")
rs = VA.resample_to(v, FPS, 300)
check("线性插值到目标长度", rs is not None and len(rs) == 300, f"→ {None if rs is None else len(rs)}")
check("过短输入 → None", VA.resample_to(np.zeros(1, np.float32), FPS, 100) is None)

print("── 4) 边界：太短/空 ──")
v3, st3 = VA.voice_activity(np.zeros(100, np.float32), FPS, SR)
check("过短输入 → 空数组", len(v3) == 0, f"→ {len(v3)}")
v4, _ = VA.voice_activity(None, FPS, SR)
check("None → 空数组", len(v4) == 0)

print("=" * 60)
if fails:
    print(f"❌ 失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("✅ 全部通过")
