#!/usr/bin/env python3
"""唇几何信号（106 点唇开合）+ 稳健特异性参照 + --no-specificity 逃生阀 回归。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import face_backend as FB  # noqa: E402
import lip_sync_detector as L  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'\u2705' if cond else '\u274c'} {name} {extra}")
    if not cond:
        fails.append(name)


print("\u2500\u2500 1) 人脸方形裁剪几何 \u2500\u2500")
c = FB.face_square_crop((400.0, 800.0, 100.0, 120.0), (1920, 1080), 1.6)
check("返回 (x0,y0,side)", c is not None and len(c) == 3, f"\u2192 {c}")
if c:
    x0, y0, side = c
    check("以人脸中心为中心", abs((x0 + side / 2) - 450) < 1.5 and abs((y0 + side / 2) - 860) < 1.5, f"\u2192 {c}")
    check("side = 1.6*max(w,h)", abs(side - 120 * 1.6) < 1.5, f"\u2192 {side}")
check("贴边时被夹紧", FB.face_square_crop((0.0, 0.0, 100.0, 100.0), (200, 200), 1.6)[2] <= 200)

print("\u2500\u2500 2) 唇开合度计算 \u2500\u2500")
pts = [(0, 100), (1, 100), (2, 100), (3, 100), (0, 120), (1, 120), (2, 120), (3, 120)]
ap, _ = FB.aperture_from_points(pts, 40.0)
check("开合度 = 下唇均值-上唇均值 / 眼距", ap is not None and abs(ap - 20 / 40.0) < 1e-6, f"\u2192 {ap}")
ap2, _ = FB.aperture_from_points([(0, 100), (1, 100), (0, 140), (1, 140)], 40.0)
check("4 点 -> 前一半/后一半", ap2 is not None and abs(ap2 - 40 / 40.0) < 1e-6, f"\u2192 {ap2}")
check("点太少(<4) -> None", FB.aperture_from_points([(0, 100), (1, 140)], 40.0)[0] is None)
check("空输入 -> None", FB.aperture_from_points([], 40.0)[0] is None)
ap3, _ = FB.aperture_from_points(pts, 0.0)
check("眼距为 0 不除零", ap3 is not None, f"\u2192 {ap3}")

print("\u2500\u2500 3) detrend_series \u2500\u2500")
x = np.linspace(0, 10, 60) + np.sin(np.arange(60)) * 0.5
d = L.detrend_series(x)
check("去漂移后均值≈0、std≈1", d is not None and abs(float(d.mean())) < 0.05 and abs(float(d.std()) - 1) < 0.05)
check("过短输入 -> None", L.detrend_series([1.0, 2.0]) is None)
check("含 nan 不崩", L.detrend_series(np.array([1.0, np.nan, 3.0, 4.0, 5.0, 6.0])) is not None)

print("\u2500\u2500 4) 关键点模型可用性 \u2500\u2500")
path = FB.resolve_lmk_model_path()
b = FB.FaceBackend()
print(f"   模型={path}  后端 has_lip_landmarks={b.has_lip_landmarks}")
if path is not None:
    check("有模型时几何信号可用", b.has_lip_landmarks)
else:
    check("无模型时优雅降级（不抛异常）", b.has_lip_landmarks is False)

print("\u2500\u2500 5) 稳健特异性参照 + --no-specificity \u2500\u2500")
MEDIA = ROOT / "tests" / "media" / "test_d0.flv"
ROI = [0.40625, 0.55556, 0.09375, 0.083333]
r = L.analyze_media(MEDIA, roi=ROI, use_face=False, seconds=16)
sp = (r.get("metrics") or {}).get("mouth_specificity") or {}
ctrls = sp.get("controls") or []
check("有稳健参照 control_r_ref", sp.get("control_r_ref") is not None, f"\u2192 ctrls={ctrls} ref={sp.get('control_r_ref')}")
if ctrls:
    check("参照 = 对照的 75 分位", abs(sp["control_r_ref"] - float(np.percentile(ctrls, 75))) < 2e-3,
          f"\u2192 ref={sp['control_r_ref']} max={sp.get('control_r_max')}")

r2 = L.analyze_media(MEDIA, roi=ROI, use_face=False, seconds=16, skip_specificity=True)
sp2 = (r2.get("metrics") or {}).get("mouth_specificity") or {}
v2 = str(r2.get("verdict"))
check("跳过时 specific=None", sp2.get("specific") is None, f"\u2192 {sp2.get('specific')}")
check("跳过标记为 True", sp2.get("skipped") is True)
check("跳过时不再因特异性拒判", "特异性不足" not in v2, f"\u2192 {v2[:40]}")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
