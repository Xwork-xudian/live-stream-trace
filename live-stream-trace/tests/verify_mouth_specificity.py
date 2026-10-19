#!/usr/bin/env python3
"""嘴型特异性对照回归（用常驻合成样本，不依赖外网）。

验证两件事：
  1) 嘴部 ROI（有效）→ 特异性通过，能测出注入偏移；
  2) 无嘴的静态区域（无效 ROI）→ 特异性不通过，判「无法确定」，不编数字。

这正是用来防止“商铺直播里非嘴部区域也能给出像样数值”的假阳性。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from lip_sync_detector import analyze_media  # noqa: E402

MEDIA = Path(__file__).resolve().parent / "media" / "test_d300.flv"
MOUTH = [0.40625, 0.55556, 0.09375, 0.083333]      # 合成样本里“嘴部”位置
BLANK = [0.03, 0.05, 0.09, 0.08]                   # 纯静态区域（没有嘴）
fails: list[str] = []


def check(name, ok, extra=""):
    print(f"  {'✅' if ok else '❌'} {name} {extra}")
    if not ok:
        fails.append(name)


def main() -> int:
    if not MEDIA.exists():
        print(f"缺少合成样本 {MEDIA}（可运行 tests/make_test_media.py 生成）")
        return 1
    print("=" * 92)
    print(" 嘴型特异性对照回归（合成样本，注入内容偏移 = +300ms）")
    print("=" * 92)

    good = analyze_media(MEDIA, roi=MOUTH, use_face=False, seconds=16)
    gm, gp = good["metrics"], good["perceptual"]
    gs = gm.get("mouth_specificity") or {}
    check("有效 ROI：特异性通过", gs.get("specific") is True,
          f"嘴部 r={gs.get('mouth_r_coarse')} vs 对照 {gs.get('control_r_max')}")
    off = gp.get("content_offset_ms") or 0
    check("有效 ROI：偏移≈-300ms", abs(off + 300) <= 15, f"→ {off:.2f} ms")
    check("有效 ROI：给出结论（非“无法确定”）", "无法" not in (good.get("verdict") or ""),
          f"→ {good.get('verdict')}")

    blank = analyze_media(MEDIA, roi=BLANK, use_face=False, seconds=16)
    bs = (blank["metrics"].get("mouth_specificity") or {})
    check("无效 ROI（无嘴）：特异性不通过或直接判无法确定",
          bs.get("specific") is not True or "无法" in (blank.get("verdict") or ""),
          f"specific={bs.get('specific')} verdict={blank.get('verdict')}")

    print("-" * 92)
    print("❌ 失败项: " + ", ".join(fails) if fails else "✅ 特异性对照行为符合预期")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
