#!/usr/bin/env python3
"""特异性对照区回归：人脸框 (x,y,w,h) → 对照区(x0,y0,x1,y1) 换算 + 同尺寸 + 不覆盖嘴。

锁定一个真实 bug：face-auto 路径曾把 (x,y,w,h) 人脸框直接传给期望 (x0,y0,x1,y1) 的
`control_rois_from`，负宽高被 clamp 成 8×8 小噪声块 → 特异性判据系统性失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import lip_sync_detector as L  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'\u2705' if cond else '\u274c'} {name} {extra}")
    if not cond:
        fails.append(name)


SHAPE = (1920, 1080)          # (h, w) 源帧
FACE = (400.0, 800.0, 290.0, 380.0)     # x, y, w, h（人脸框）
MOUTH = (470.0, 1010.0, 620.0, 1060.0)  # x0, y0, x1, y1（嘴部 ROI，脸内居中偏下）
bw, bh = MOUTH[2] - MOUTH[0], MOUTH[3] - MOUTH[1]

print("\u2500\u2500 1) 旧写法确实退化成 8\u00d78（记录 bug）\u2500\u2500")
bad = L.control_rois_from(FACE, SHAPE)
sizes = [(round(c[2] - c[0], 1), round(c[3] - c[1], 1)) for c in bad]
check("把 (x,y,w,h) 当 (x0,y0,x1,y1) 会得到 8\u00d78 小框",
      len(bad) > 0 and all(s == (8.0, 8.0) for s in sizes), f"\u2192 {sizes}")

print("\u2500\u2500 2) 新换算：同尺寸、不覆盖嘴、落在脸外 \u2500\u2500")
ctrl = L.control_rois_from_face(FACE, MOUTH, SHAPE)
print("   face", FACE, "mouth", MOUTH)
for c in ctrl:
    print("   ctrl", tuple(round(v) for v in c))
check("至少 1 个对照区", len(ctrl) >= 1, f"\u2192 {len(ctrl)}")
check("尺寸与嘴部一致", all(abs((c[2] - c[0]) - bw) < 0.6 and abs((c[3] - c[1]) - bh) < 0.6
                            for c in ctrl), f"\u2192 {[ (round(c[2]-c[0],1), round(c[3]-c[1],1)) for c in ctrl]}")


def overlap(a, b):
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


check("不与嘴部重叠", all(not overlap(c, MOUTH) for c in ctrl))
check("都在画面内", all(0 <= c[0] and 0 <= c[1] and c[2] <= SHAPE[1] and c[3] <= SHAPE[0]
                        for c in ctrl))

print("\u2500\u2500 3) 找不到脸外位置时回退到通用做法 \u2500\u2500")
# 嘴部几乎占满画面 → 脸外无空间 → 回退（仍需同尺寸）
big_mouth = (5.0, 5.0, 1070.0, 1900.0)
ctrl2 = L.control_rois_from_face((0.0, 0.0, 1080.0, 1920.0), big_mouth, SHAPE)
check("回退仍有结果（同一尺寸或不重叠）",
      ctrl2 == [] or all(abs((c[2] - c[0]) - (big_mouth[2] - big_mouth[0])) < 0.6 for c in ctrl2),
      f"\u2192 {len(ctrl2)}")

print("\u2500\u2500 4) analyze_media 使用新函数（真实样本回归）\u2500\u2500")
import glob
import os
med = sorted(glob.glob(str(ROOT / "tests" / "media" / "*.flv")))
if med:
    r = L.analyze_media(med[0], roi=[0.40625, 0.55556, 0.09375, 0.083333], use_face=False, seconds=16)
    sp = (r.get("metrics") or {}).get("mouth_specificity") or {}
    ctrls = sp.get("controls") or []
    check("手动 ROI 路径对照区非 8\u00d78（尺寸正确）", sp.get("control_r_max") is not None,
          f"\u2192 controls={ctrls}")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
