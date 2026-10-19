#!/usr/bin/env python3
"""face_backend 单元测试：关键点嘴部 ROI 几何 / 正脸度 / 纹理绘画 / 多脸聚合（含 Haar 退化）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import face_backend as FB  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name} {extra}")
    if not cond:
        fails.append(name)


FRONTAL_LM = [(100, 100), (140, 100), (120, 105), (105, 120), (135, 120)]
PROFILE_LM = [(100, 100), (140, 100), (175, 105), (150, 120), (180, 120)]

print("── 1) 关键点 → 嘴部 ROI 几何 ──")
face = {"box": (90, 80, 60, 70), "lm": FRONTAL_LM, "score": 0.9, "source": "yunet"}
roi = FB.mouth_roi_from_face(face, (300, 300))
# cx=(105+135)/2=120, cy=(120+120)/2=120, mw=|135-105|*1.3=39, mh=|120-105|*1.35=20.25
check("ROI 水平居中于嘴中心", abs((roi[0] + roi[2]) / 2 - 120) < 0.6, f"→ {tuple(round(v,1) for v in roi)}")
check("ROI 宽度 ≈ 嘴角距×1.3", abs((roi[2] - roi[0]) - 39.0) < 1.0)
check("ROI 垂直紧贴嘴唇（含上下唇）", roi[1] < 120 - 5 and roi[3] > 120 + 5)

print("── 2) 无关键点时退回人脸框比例（旧行为）──")
f2 = {"box": (100, 100, 80, 100), "lm": None, "score": None, "source": "haar"}
r2 = FB.mouth_roi_from_face(f2, (400, 400))
check("ROI = 框下半部 25%~75% / 62%~95%",
      abs(r2[0] - (100 + 0.25 * 80)) < 1 and abs(r2[1] - (100 + 0.62 * 100)) < 1
      and abs(r2[2] - (100 + 0.75 * 80)) < 1 and abs(r2[3] - (100 + 0.95 * 100)) < 1,
      f"→ {tuple(round(v,1) for v in r2)}")

print("── 3) 正脸度 ──")
fF = FB.frontality(face)
fP = FB.frontality({"box": (90, 80, 90, 70), "lm": PROFILE_LM, "score": 0.9, "source": "yunet"})
fN = FB.frontality({"box": (0, 0, 10, 10), "lm": None, "score": None, "source": "haar"})
check("正脸 → 高", fF is not None and fF >= 0.9, f"→ {fF}")
check("大侧脸 → 低(<0.35)", fP is not None and fP < 0.35, f"→ {fP}")
check("无关键点 → None", fN is None, f"→ {fN}")

print("── 4) 嘴部纹理画像 ──")
rng = np.random.default_rng(0)
img = (rng.normal(128, 40, (200, 200))).clip(0, 255).astype(np.uint8)
t = FB.mouth_texture(img, (60, 60, 120, 120), face)
check("返回 contrast/edge/visible 且 ∈[0,1]",
      all(t.get(k) is not None and 0 <= t[k] <= 1 for k in ("contrast", "edge", "visible")), f"→ {t}")
flat = np.full((200, 200), 128, np.uint8)
tf = FB.mouth_texture(flat, (60, 60, 120, 120), face)
check("纯平区域 contrast=0（可判「糊/被挡」）", tf["contrast"] == 0.0, f"→ {tf}")

print("── 5) 多脸聚合（缺失帧填充 + 正脸度中位）──")
faces_multi = [[face], [], [face]]
rois, cov, jit, fmed = FB.rois_from_faces_multi(faces_multi, (300, 300), 0)
check("覆盖率 2/3", abs(cov - 2 / 3) < 1e-6, f"→ {cov:.3f}")
check("ROI 形状 (3,4)", rois.shape == (3, 4), f"→ {rois.shape}")
check("缺失帧用前一有效脸", np.allclose(rois[1], rois[0]))
check("正脸度中位数 ≈ 1.0", fmed is not None and fmed >= 0.9, f"→ {fmed}")
check("整窗无脸 → rois None", FB.rois_from_faces_multi([[], []], (300, 300), 0)[0] is None)

print("── 6) 后端选择（YuNet 优先，缺模型退化 Haar）──")
b = FB.FaceBackend()
print(f"  当前后端: {b.name}（available={b.available}, landmarks={b.has_landmarks}）")
check("后端可用或明确为 none", b.name in ("yunet", "haar", "none"))
check("name=yunet ⇒ has_landmarks", (b.name != "yunet") or b.has_landmarks)

print("=" * 60)
if fails:
    print(f"❌ 失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("✅ 全部通过")
