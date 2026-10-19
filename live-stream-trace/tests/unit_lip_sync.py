#!/usr/bin/env python3
"""lip_sync_detector 单元测试：互相关滞后恢复 + ROI 几何 + 包络长度。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from lip_sync_detector import (audio_envelope, boxes_to_mouth_rois,  # noqa: E402
                               cross_correlate, segment_lags)

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name} {extra}")
    if not cond:
        fails.append(name)


print("── 1) 互相关滞后恢复（已知延迟）──")
rng = np.random.default_rng(0)
base = rng.normal(0, 1, 400)
base = np.convolve(base, np.ones(5) / 5, mode="same")      # 平滑成“包络”
for true_k in (5, -7, 0, 12):
    audio = base
    video = np.roll(base, true_k)                          # 画面相对音频平移 true_k 帧
    _, _, lag, r = cross_correlate(audio, video, max_lag_frames=20)
    check(f"恢复 {true_k:+d} 帧", abs(lag - true_k) <= 0.6, f"→ {lag:+.2f}（r={r:.3f}）")

print("── 2) 嘴部 ROI 几何（人脸框 → 下部区域，缺失帧前向填充）──")
boxes = [(100, 50, 80, 100), None, (102, 52, 78, 99)]
rois, cov, jitter = boxes_to_mouth_rois(boxes, (180, 320))
check("覆盖率 = 2/3", abs(cov - 2 / 3) < 1e-6, f"→ {cov:.3f}")
check("ROI 形状 (3,4)", rois.shape == (3, 4), f"→ {rois.shape}")
check("缺失帧用前一有效框", np.allclose(rois[1], rois[0]))
check("ROI 位于人脸下部", abs(rois[0][1] - (50 + 0.62 * 100)) < 1e-3,
      f"y0={rois[0][1]:.1f}")
check("ROI 水平居中", abs(rois[0][0] - (100 + 0.25 * 80)) < 1e-3, f"x0={rois[0][0]:.1f}")
check("ROI 不越界", rois[0][2] <= 320 and rois[0][3] <= 180)
allnone, cov0, _ = boxes_to_mouth_rois([None, None], (180, 320))
check("整窗无人脸 → None", allnone is None and cov0 == 0.0)

print("── 3) 音频包络长度 ≈ 时长×fps ──")
pcm = np.zeros(16000 * 3, dtype=np.float32)
env = audio_envelope(pcm, 25)
check("3s@25fps → 75 帧", abs(len(env) - 75) <= 1, f"→ {len(env)}")

print("── 4) 分段时间一致性（稳定延迟 → 各段一致）──")
a = np.convolve(rng.normal(0, 1, 900), np.ones(5) / 5, mode="same")
m = np.roll(a, 3)
segs = segment_lags(a, m, max_lag_frames=10, segments=3)
lags = [s[0] for s in segs]
check("3 段均恢复 +3 帧", len(lags) == 3 and all(abs(l - 3) <= 1 for l in lags), f"→ {lags}")

print()
if fails:
    print(f"❌ 失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("✅ 全部通过")
