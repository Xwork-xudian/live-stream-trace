#!/usr/bin/env python3
"""ROI 逐帧跟踪回归：跟踪几何（分位包络 + 保证取样窗可居中）+ union 取样映射。

背景：带货直播里主播走动/镜头推拉，嘴部在源画面里会移动数百像素；旧高精度通道用
「固定中位 ROI + ±6px 平移补偿」会导致嘴部整体滑出裁剪框。本测试锁定：
  1) `_track_geometry` 用 5~95 分位求 union（抗离群误检）并给出逐帧中心；
  2) union 边距足够大，取样窗在中心极值处也不会被夹（映射无偏）；
  3) `extract_tracked_roi_frames` 的取样窗口中心确实跟随跟踪中心（x 斜坡图验证）。
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


sw, sh = 320, 569
n = 40
rois = np.zeros((n, 4), dtype=np.float32)
cx_s = np.linspace(100, 220, n)
cy_s = np.linspace(200, 320, n)
rois[:, 0] = cx_s - 15
rois[:, 1] = cy_s - 10
rois[:, 2] = cx_s + 15
rois[:, 3] = cy_s + 10
sx, sy = 1080 / sw, 1920 / sh

print("\u2500\u2500 1) 跟踪几何：中心 + 分位包络 \u2500\u2500")
centers, union, win_src = L._track_geometry(rois, (sh, sw), (1080, 1920))
check("中心数 = 帧数", centers.shape == (n, 2), f"\u2192 {centers.shape}")
check("中心换算到源坐标", abs(centers[0, 0] - 100 * sx) < 1.0 and abs(centers[-1, 1] - 320 * sy) < 1.0,
      f"\u2192 x0={centers[0,0]:.0f} yN={centers[-1,1]:.0f}")
check("union 覆盖中心跨度", union is not None and union[2] > (220 - 100) * sx, f"\u2192 union={union}")
check("返回取样窗(源像素)", win_src is not None and win_src[0] > 0, f"\u2192 {win_src}")

print("\u2500\u2500 2) 分位包络抗离群 \u2500\u2500")
rois2 = rois.copy()
rois2[7] = [-500, -500, -480, -480]
c2, u2, w2 = L._track_geometry(rois2, (sh, sw), (1080, 1920))
check("离群不显著放大 union", u2 is not None and u2[2] <= union[2] + 40, f"\u2192 {u2} vs \u57fa\u7ebf {union}")

print("\u2500\u2500 3) union 取样映射（x 斜坡图验证取样中心跟随）\u2500\u2500")


def fake_extract(path, seconds, fps, box, size, ffmpeg=None):
    ow, oh = size
    ramp = np.linspace(0, 200, ow)
    arr = np.tile(ramp, (max(2, int(round(seconds * fps))), 1))
    arr = np.tile(arr[:, None, :], (1, oh, 1)).astype(np.uint8)
    return arr


orig = L.extract_roi_frames
L.extract_roi_frames = fake_extract
try:
    crops, meta = L.extract_tracked_roi_frames("x", 4.0, 30, union, 192, centers, 25.0, win_src, None)
finally:
    L.extract_roi_frames = orig

check("返回取样序列", crops is not None and crops.ndim == 3, f"\u2192 {None if crops is None else crops.shape}")
if crops is not None:
    ow, oh = meta["extract_size"]
    x, y, w, h = meta["union_box_src"]
    nf = crops.shape[0]
    t_uf = (np.arange(nf) + 0.5) / 30.0
    t_c = (np.arange(len(centers)) + 0.5) / 25.0
    exp_kx = (np.interp(t_uf, t_c, centers[:, 0]) - x) * (ow / float(w))
    ramp = np.linspace(0, 200, ow)
    err = [abs(float(crops[i].mean()) - float(np.interp(exp_kx[i], np.arange(ow), ramp))) for i in range(nf)]
    check("取样中心跟随跟踪中心（偏差 < 2px）", float(np.median(err)) < 2.0, f"\u2192 \u4e2d\u4f4d\u504f\u5dee {np.median(err):.2f}")
    check("无窗口被边缘夹住", meta.get("track_win_clipped") == 0, f"\u2192 clipped={meta.get('track_win_clipped')}")
    check("meta 含跟踪跨度", meta.get("track_span_px") is not None, f"\u2192 {meta.get('track_span_px')}")

print("\u2500\u2500 4) 抽取开销上限（高而窄的 union 不放大面积）\u2500\u2500")
L.extract_roi_frames = fake_extract
try:
    tall_crops, tall_meta = L.extract_tracked_roi_frames(
        "x", 3.0, 30, (100, 50, 120, 1800), 192, centers, 25.0, (150.0, 108.0), None)
finally:
    L.extract_roi_frames = orig
check("极高 union 仍返回结果", tall_crops is not None, f"\u2192 {None if tall_crops is None else tall_crops.shape}")
if tall_crops is not None:
    ow, oh = tall_meta["extract_size"]
    check("抽取高度受限 (\u2264TRACK_MAX_OH)", oh <= L.TRACK_MAX_OH, f"\u2192 {ow}x{oh}")
    check("窗口不超过抽取图", tall_meta["tracked_win"][1] <= oh and tall_meta["tracked_win"][0] <= ow,
          f"\u2192 win={tall_meta['tracked_win']}")

print("\u2500\u2500 5) 退化输入 \u2500\u2500")
check("rois=None \u2192 (None,None,None)", L._track_geometry(None, (sh, sw), (1080, 1920)) == (None, None, None))
check("帧数<2 \u2192 (None,None,None)", L._track_geometry(rois[:1], (sh, sw), (1080, 1920)) == (None, None, None))
check("源尺寸缺失 \u2192 (None,None,None)", L._track_geometry(rois, (sh, sw), (0, 0)) == (None, None, None))

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
