#!/usr/bin/env python3
"""合成测试媒体：把「内容级音画不同步」烘焙进画面与声音，但让容器时间戳对齐。

这模拟的正是“OBS 推流之前的采集/编码链”产生的失同步：
    · 画面里的事件（嘴动）与声音里的事件（说话）在内容上错开 Δ；
    · 但两者都从 0 开始封装 → tag/PTS 时间戳自洽。

视频：640x360@25fps，指定 ROI 在“说话窗口”内以 ~10Hz 闪烁（= 嘴部运动能量）。
音频：16k 单声道，同一“说话窗口”内为 800Hz 正弦（= 说话包络）。

用法: python make_test_media.py <out.flv> <delta_ms>
      delta_ms > 0 表示音频事件比画面事件晚 Δ（音频滞后画面）
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

FFMPEG = "/opt/ffmpeg/bin/ffmpeg"
W, H, FPS, DUR = 640, 360, 25, 20
SR = 16000
ROI = (260, 200, 60, 30)          # x, y, w, h（像素）
SPEECH_START, SPEECH_LEN, PERIOD = 0.2, 0.5, 1.0


def windows(shift_s: float):
    """返回 [(start, end)] 说话窗口（已平移 shift_s）。"""
    out = []
    t = 0.0
    while t < DUR:
        out.append((t + SPEECH_START + shift_s, t + SPEECH_START + SPEECH_LEN + shift_s))
        t += PERIOD
    return out


def make_video():
    n = int(DUR * FPS)
    frames = np.full((n, H, W), 128, dtype=np.uint8)
    x, y, w, h = ROI
    wins = windows(0.0)
    rng = np.random.default_rng(7)
    for i in range(n):
        t = i / FPS
        for s, e in wins:
            if s <= t < e:
                # 10Hz 闪烁 → ROI 内相邻帧差显著（嘴部运动能量）
                # 逐帧翻转（相邻帧差恒定）→ ROI 运动能量为与说话窗口严格对齐的方波
                frames[i, y:y + h, x:x + w] = 220 if i % 2 == 0 else 60
                break
    # 轻微全局噪声，避免完全静止
    frames = np.clip(frames.astype(np.int16) + rng.integers(-2, 3, frames.shape), 0, 255).astype(np.uint8)
    return frames


def make_audio(delta_ms: float):
    n = int(DUR * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n, dtype=np.float32)
    wins = windows(delta_ms / 1000.0)
    for s, e in wins:
        m = (t >= s) & (t < e)
        sig[m] = 0.4 * np.sin(2 * np.pi * 800 * t[m])
    rng = np.random.default_rng(11)
    sig += rng.normal(0, 0.002, n).astype(np.float32)
    return np.clip(sig, -1, 1)


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    delta_ms = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = make_video()
    pcm = make_audio(delta_ms)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "v.raw").write_bytes(frames.tobytes())
        (td / "a.raw").write_bytes((pcm * 32767).astype("<i2").tobytes())
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", str(FPS), "-i", str(td / "v.raw"),
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(td / "a.raw"),
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(FPS * 2),
               "-c:a", "aac", "-b:a", "96k", "-shortest", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-2000:])
            return 1
    print(f"生成: {out}  注入内容级偏移 delta={delta_ms:+.0f} ms"
          f"（正=音频滞后画面）  大小={out.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
