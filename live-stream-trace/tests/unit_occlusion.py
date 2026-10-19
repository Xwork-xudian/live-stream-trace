#!/usr/bin/env python3
"""嘴部遮挡（手/物体）判定回归：纹理塌陷 → 逐帧可见性下降 → 判定与措辞。

合成序列：正常帧嘴部有纹理；中间若干帧用「平块」盖住嘴部（模拟手/物体遮挡，
同肤色低纹理），由于可见性 = 嘴部纹理 / 人脸下半部纹理，遮挡帧该值应显著下降。
"""
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


N, H, W = 40, 200, 220
rng = np.random.default_rng(0)
frames = rng.normal(128, 45, (N, H, W)).clip(0, 255).astype(np.uint8)
face = {"box": (40.0, 40.0, 140.0, 150.0), "lm": None, "score": None, "source": "haar"}
roi = (80, 120, 140, 150)          # 嘴部 ROI（含于人脸框内）
OCC = list(range(15, 30))          # 遮挡 15..29 帧（15 帧）
for i in OCC:
    frames[i, 120:150, 80:140] = 130          # 平块盖住嘴部（低纹理）

rois = np.tile(np.array(roi, dtype=np.float32), (N, 1))
faces_multi = [[face] for _ in range(N)]

print("\u2500\u2500 1) 可见性序列随遮挡下降 \u2500\u2500")
ser = FB.visibility_series(frames, rois, faces_multi, 0, stride=1)
check("返回逐帧序列", ser is not None and len(ser) == N, f"\u2192 {None if ser is None else len(ser)}")
normal = ser[[i for i in range(N) if i not in OCC]]
occ = ser[OCC]
print(f"   正常帧 visible median={np.median(normal):.3f} | 遮挡帧={np.median(occ):.3f}")
check("遮挡帧可见性显著更低", float(np.median(occ)) < float(np.median(normal)) * 0.5,
      f"\u2192 {np.median(occ):.3f} vs {np.median(normal):.3f}")
print("\u2500\u2500 2) 遮挡占比与最长连续段（相对基线阈值）\u2500\u2500")
mask, thr, ref = L.occlusion_mask(ser)
check("返回掩码/阈值/基线", mask is not None and thr is not None and ref is not None,
      f"\u2192 thr={thr} ref={ref}")
check("基线 \u2248 正常帧可见性", abs(float(ref) - float(np.median(normal))) < 0.05, f"\u2192 {ref:.3f}")
check("遮挡帧全部被标记", bool(np.all(mask[OCC])), f"\u2192 {int(mask[OCC].sum())}/{len(OCC)}")
check("正常帧不被误标", int(mask.sum()) == len(OCC), f"\u2192 标记 {int(mask.sum())} 帧")
frac = float(np.mean(mask))
check("遮挡占比 \u2248 15/40", abs(frac - 15 / 40) < 0.08, f"\u2192 {frac:.3f}")
check("_longest_run 正确", L._longest_run([True, True, False, True, True, True]) == 3)
check("_longest_run 空/全假", L._longest_run([]) == 0 and L._longest_run([False, False]) == 0)

print("\u2500\u2500 3) 门禁与措辞（build_verdict）\u2500\u2500")
base = {
    "roi_mode": "manual", "fps": 200, "lag_resolution_ms": 5.0,
    "peak_lag_ms": 20.0, "peak_lag_frames": 4, "peak_correlation": 0.10,
    "robust_offset_ms": 20.0, "robust_correlation": 0.12, "offset_ci_ms": 10.0,
    "window_agreement": 0.9, "windows_used": 5, "windows_total": 5,
    "media_span_s": 10.0, "max_lag_frames": 120, "face_coverage": 1.0,
    "talking_detected": True, "mouth_specificity": {"specific": True},
}
res_occ = {"metrics": {**base, "occlusion": {"threshold": thr, "vis_ref": ref, "occluded_fraction": 0.55,
                                             "occluded_longest_s": 2.4, "vis_p10": 0.2}},
           "perceptual": {}}
L.build_verdict(res_occ)
v_occ = str(res_occ.get("verdict"))
print("   verdict:", v_occ)
check("遮挡占比高 → 判定说明遮挡", "遮挡" in v_occ and "手/物体" in v_occ, f"\u2192 {v_occ[:40]}")
check("perceptual 暴露 occlusion", (res_occ.get("perceptual") or {}).get("occlusion") is not None)

res_ok = {"metrics": {**base, "occlusion": {"threshold": thr, "vis_ref": ref, "occluded_fraction": 0.05,
                                            "occluded_longest_s": 0.2, "vis_p10": 0.8}},
          "perceptual": {}}
L.build_verdict(res_ok)
v_ok = str(res_ok.get("verdict"))
print("   verdict(低遮挡):", v_ok)
check("低遮挡不触发遮挡判定", "遮挡" not in v_ok, f"\u2192 {v_ok[:40]}")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
