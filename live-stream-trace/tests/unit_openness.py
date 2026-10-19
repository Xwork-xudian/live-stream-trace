#!/usr/bin/env python3
"""唇线暗带（mouth openness）信号回归：对「张口 → 暗带变宽」的追踪能力。

动机：美颜磨皮抹平嘴部高频纹理，但唇线是强暗边，平滑后仍存在。
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


FPS = 25
N = 100                      # 4s
H, W = 40, 30
t = np.arange(N) / FPS
band = 6.0 + 6.0 * np.sin(2 * np.pi * 2.0 * t)      # 暗带高度 2Hz 变化（模拟张口）
band = np.clip(band, 1, H - 2).astype(int)

frames = np.full((N, H, W), 200, dtype=np.uint8)
for i, bh in enumerate(band):
    y0 = (H - bh) // 2
    frames[i, y0:y0 + bh, :] = 60                   # 暗带

print("\u2500\u2500 1) 追踪暗带宽度变化 \u2500\u2500")
sig, ts = L.mouth_openness_signal(frames, FPS)
check("长度 = 帧数", len(sig) == N, f"\u2192 {len(sig)}")
check("时间轴长度一致", len(ts) == N)
exp = L.detrend_series(band.astype(np.float64), win=max(3, int(round(0.5 * FPS))))
r = float(np.corrcoef(sig.astype(np.float64), exp)[0, 1]) if exp is not None else -1
check("与注入的暗带变化高度相关（>0.8）", r > 0.8, f"\u2192 r={r:.3f}")

print("\u2500\u2500 2) 无变化 -> 信号接近常量 \u2500\u2500")
flat = np.full((N, H, W), 200, dtype=np.uint8)
for i in range(N):
    flat[i, 18:22, :] = 60
sigf, _ = L.mouth_openness_signal(flat, FPS)
check("恒定暗带 -> 去漂移后接近 0", float(np.std(sigf)) < 0.02, f"\u2192 std={float(np.std(sigf)):.4f}")

print("\u2500\u2500 3) 退化输入 \u2500\u2500")
s2, _ = L.mouth_openness_signal(frames[:2], FPS)
check("帧数<4 -> 空数组", len(s2) == 0)
s3, _ = L.mouth_openness_signal(None, FPS)
check("None -> 空数组", len(s3) == 0)

print("\u2500\u2500 4) 变体竞争接入 \u2500\u2500")
MEDIA = ROOT / "tests" / "media" / "test_d0.flv"
res = L.analyze_media(MEDIA, roi=[0.40625, 0.55556, 0.09375, 0.083333], use_face=False, seconds=16)
fine = (res.get("metrics") or {}).get("fine") or {}
sr = fine.get("signal_r") or {}
check("signal_r 含 openness", "openness" in sr, f"\u2192 {sr}")
check("motion_variant 为四种之一", fine.get("motion_variant") in ("raw", "aligned", "aperture", "openness"),
      f"\u2192 {fine.get('motion_variant')}")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
