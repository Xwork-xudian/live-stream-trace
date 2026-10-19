#!/usr/bin/env python3
"""对比：商铺直播两个样本在「升级前 / 升级后」的内容级测量表现。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from lip_sync_detector import analyze_media  # noqa: E402

CASES = [
    ("抖音商铺 18:04:15", ROOT / "captures" / "lip_sample_20261014_180415_FULL_HD1.flv", 0.072, 168.68, 0.111),
    ("抖音商铺 18:07:29", ROOT / "captures" / "lip_sample_20261014_180729_FULL_HD1.flv", 0.135, 591.39, 0.000),
    # 小红书长样本不随仓库分发（第三方内容）；需要时自行把样本放回 tests/media/xhs_room_long.flv 再打开本行
    # ("小红书主播间(长样本)", ROOT / "tests" / "media" / "xhs_room_long.flv", 0.307, 20.08, 0.667),
]

print("=" * 118)
print(" 商铺直播样本：升级前 vs 升级后（内容级）")
print("=" * 118)
print(f"{'样本':<22} {'阶段':<8} {'r':>6} {'CI±':>9} {'一致率':>7} {'窗口':>7} {'偏移(ms)':>10}  {'判定'}")
print("-" * 118)
for name, f, r0, ci0, ag0 in CASES:
    if not f.exists():
        print(f"{name:<22} 样本缺失: {f.name}")
        continue
    d = analyze_media(f, seconds=20)
    m, p = d["metrics"], d["perceptual"]
    fine = m.get("fine") or {}
    cand = m.get("face_candidates") or {}
    print(f"{name:<22} {'前':<8} {r0:>6.3f} {ci0:>9.1f} {ag0:>7.3f} {'-':>7} {'-':>10}  {'（历史报告）'}")
    print(f"{'':<22} {'后':<8} {m.get('peak_correlation'):>6.3f} "
          f"{str(p.get('content_offset_ci_ms')):>9} {str(p.get('window_agreement')):>7} "
          f"{str(m.get('windows_used')) + '/' + str(m.get('windows_total')):>7} "
          f"{str(p.get('content_offset_ms')):>10}  {d.get('verdict')}")
    print(f"{'':<22} 细节: 变体={fine.get('motion_variant')} r_raw={fine.get('r_raw')} r_aligned={fine.get('r_aligned')} "
          f"对齐位移中位={fine.get('align_shift_px_median')}px | 多脸候选={cand.get('candidates')} 选中=第{cand.get('chosen')}张 "
          f"粗相关={cand.get('coarse_r')} | 语音活动={m.get('speech_activity_median')} | 抖动={m.get('face_jitter_px')}px")
    print()
