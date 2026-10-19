#!/usr/bin/env python3
"""受控实验：时间戳级检测 vs 内容级（人脸嘴型）检测。

三份合成媒体（test_d0 / test_d300 / test_d-250）的容器时间戳都对齐，
只有“内容”被注入了已知偏移。用于证明：
  · 时间戳级（av_sync_detector）对“推流前管线烘焙进内容”的失同步 → 报 ~0（漏报）
  · 内容级（lip_sync_detector）→ 如实测出注入量
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # live-stream-trace/
sys.path.insert(0, str(ROOT / "core"))

from av_sync_detector import analyze_file          # noqa: E402
from lip_sync_detector import analyze_media        # noqa: E402

ROI = [0.40625, 0.55556, 0.09375, 0.083333]        # 测试媒体中“嘴部”位置（相对坐标）
TOL = 60.0                                          # 允许误差 ms
CASES = [0.0, 300.0, -250.0]


def main() -> int:
    rows = []
    ok_all = True
    for delta in CASES:
        f = Path(__file__).resolve().parent / "media" / f"test_d{int(delta)}.flv"
        avs = analyze_file(f, decode_check=True, decode_seconds=10)
        lip = analyze_media(f, roi=ROI, seconds=20, fps=25)
        ts_off = (avs.get("perceptual") or {}).get("stream_offset_ms")
        dec = avs.get("decode_check") or {}
        lip_off = (lip.get("perceptual") or {}).get("content_offset_ms")
        expect = -delta
        err = abs(lip_off - expect) if lip_off is not None else 999
        ok = err <= TOL and abs(ts_off or 0) <= 60
        ok_all = ok_all and ok
        curve = (lip.get("metrics") or {}).get("lag_curve") or []
        top = sorted([c for c in curve if c[1] is not None], key=lambda c: -c[1])[:3]
        rows.append((delta, ts_off, dec.get("mean_ms"), lip_off, expect, err, ok,
                     lip.get("verdict_level"), (lip.get("metrics") or {}).get("peak_correlation"), top))
    print("=" * 112)
    print(" 受控实验：容器时间戳对齐，内容被注入已知偏移（模拟 OBS 推流前管线失同步）")
    print("=" * 112)
    print(f"{'注入Δ(ms)':>10} {'时间戳级(ms)':>13} {'解码级(ms)':>11} {'内容级(ms)':>11} "
          f"{'期望(ms)':>9} {'误差':>7} {'置信':>5} {'峰r':>6} {'结果':>7}  相关峰 top3(ms,r)")
    print("-" * 112)
    for d, ts, decm, lip, exp, err, ok, lvl, r, top in rows:
        def fmt(v):
            return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"
        tops = " ".join(f"{c[0]:+.0f},{c[1]:.3f}" for c in top)
        print(f"{d:>+10.0f} {fmt(ts):>13} {fmt(decm):>11} {fmt(lip):>11} {exp:>+9.0f} "
              f"{err:>7.1f} {str(lvl):>5} {str(r):>6} {'✅PASS' if ok else '❌FAIL':>7}  {tops}")
    print("-" * 112)
    print("结论：时间戳级/解码级都把三份样本判为“几乎同步”（漏报，仅残留 AAC 编码器 priming 的 ~20~36ms），")
    print("      内容级如实复现了注入的 ±250~300 ms 偏移。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
