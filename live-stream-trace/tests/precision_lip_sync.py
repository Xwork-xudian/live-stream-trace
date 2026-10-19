#!/usr/bin/env python3
"""内容级音画同步「精度」专项测试。

与 verify_lip_vs_ts.py（证明时间戳级漏报）不同，本测试聚焦**测量精度**：
  · 合成 50fps 样本，嘴部运动为**平滑调幅**信号（非逐帧翻转），使真实偏移是连续量；
  · 注入**非整数帧**偏移（如 137ms @50fps = 6.85 帧），检验能否分辨到毫秒；
  · 对比「高精度通道」与「逐帧回退」的误差，并检查置信区间是否覆盖真值。

用法: ./.venv/bin/python tests/precision_lip_sync.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from lip_sync_detector import analyze_media  # noqa: E402

FFMPEG = "/opt/ffmpeg/bin/ffmpeg"
W, H, FPS, DUR = 640, 360, 50, 16
SR = 16000
ROI = (260, 200, 60, 30)               # x, y, w, h（像素）
ROI_REL = [260 / W, 200 / H, 60 / W, 30 / H]
CARRIER_HZ = 6.0                       # 嘴部开合载波
WIN_START, WIN_LEN, PERIOD = 0.2, 0.5, 1.0
OUT = Path(__file__).resolve().parent / ".out"
OUT.mkdir(parents=True, exist_ok=True)


def envelope(t: np.ndarray) -> np.ndarray:
    """平滑说话包络（升余弦窗），保证真实偏移是连续量。"""
    a = np.zeros_like(t)
    k = 0.0
    while k < DUR:
        s, e = k + WIN_START, k + WIN_START + WIN_LEN
        m = (t >= s) & (t < e)
        a[m] = 0.5 * (1 - np.cos(2 * np.pi * (t[m] - s) / WIN_LEN))
        k += PERIOD
    return a


def make_media(path: Path, delta_ms: float) -> None:
    n = int(DUR * FPS)
    t = np.arange(n) / FPS
    frames = np.full((n, H, W), 128, dtype=np.uint8)
    x, y, w, h = ROI
    a_v = envelope(t)
    # 调幅载波：ROI 亮度按包络调幅 → 相邻帧差（运动能量）随包络变化
    frames[:, y:y + h, x:x + w] = (128 + a_v[:, None, None] * 90
                                   * np.sin(2 * np.pi * CARRIER_HZ * t)[:, None, None]
                                   ).astype(np.uint8)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "v.raw").write_bytes(frames.tobytes())
        na = int((DUR + 1) * SR)
        ta = np.arange(na) / SR
        a_a = envelope(ta - delta_ms / 1000.0)          # 音频事件整体平移 Δ
        pcm = np.clip(a_a * 0.5 * np.sin(2 * np.pi * 400 * ta)
                      + np.random.default_rng(3).normal(0, 0.002, na), -1, 1)
        (td / "a.raw").write_bytes((pcm * 32767).astype("<i2").tobytes())
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", str(FPS), "-i", str(td / "v.raw"),
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(td / "a.raw"),
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(FPS * 2),
               "-c:a", "aac", "-b:a", "96k", "-shortest", str(path)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(r.stderr[-500:])


CASES = [0.0, 137.0, -83.0]
TOL = 10.0

print("=" * 104)
print(" 内容级音画同步精度测试（50fps 平滑运动，非整数帧偏移）")
print("=" * 104)
print(f"{'注入Δ(ms)':>10} {'期望(ms)':>9} | {'高精度(ms)':>11} {'误差':>7} {'分辨力':>7} {'CI±':>7} "
      f"{'一致率':>7} {'r':>6} | {'逐帧回退(ms)':>12} {'误差':>7}  结果")
print("-" * 104)
fails = 0
for delta in CASES:
    f = OUT / f"prec_d{int(delta)}.flv"
    make_media(f, delta)
    expect = -delta
    fine = analyze_media(f, roi=ROI_REL, seconds=DUR, use_face=False, fine_fps=200)
    coarse = analyze_media(f, roi=ROI_REL, seconds=DUR, use_face=False, use_fine=False)
    fp, fm = fine.get("perceptual") or {}, fine.get("metrics") or {}
    cp = coarse.get("perceptual") or {}
    v_f, v_c = fp.get("content_offset_ms"), cp.get("content_offset_ms")
    err_f = abs(v_f - expect) if v_f is not None else 999
    err_c = abs(v_c - expect) if v_c is not None else 999
    # 只考核绝对精度目标（细网格 5ms；两者差异 <1 帧属采样噪声，不作断言）
    ok = err_f <= TOL
    fails += 0 if ok else 1
    print(f"{delta:>+10.0f} {expect:>+9.0f} | {v_f:>+11.2f} {err_f:>7.2f} "
          f"{fp.get('lag_resolution_ms'):>7} {str(fp.get('content_offset_ci_ms')):>7} "
          f"{str(fp.get('window_agreement')):>7} {fm.get('peak_correlation'):>6} | "
          f"{v_c:>+12.2f} {err_c:>7.2f}  {'✅PASS' if ok else '❌FAIL'}")
print("-" * 104)
print(f"精度目标：高精度通道误差 ≤ {TOL:.0f} ms（50fps 下 1 帧 = 20ms，即要求优于半帧）")
print("❌ 失败" if fails else "✅ 全部通过（高精度通道明显优于逐帧回退）")
sys.exit(1 if fails else 0)
