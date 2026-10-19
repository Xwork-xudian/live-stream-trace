#!/usr/bin/env python3
"""flv_duration_s 时间戳跳变回归：直播 FLV 常带一个 ts=0 的复位 tag 后跳到上行绝对时间。

旧实现取「末 tag ts − 首 tag ts」，会把 7.4s 的样本算成 8168s，导致 ensure_lip_sample
误判“样本已够长”而跳过补采。本测试锁定“按 >1s 跳变切段取最长连续段”的新口径。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from lip_sync_detector import flv_duration_s, probe_media  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'\u2705' if cond else '\u274c'} {name} {extra}")
    if not cond:
        fails.append(name)


def make_flv(tags):
    """tags: [(type, ts_ms)] \u2192 最小可用 FLV 字节流（payload 为占位字节）。"""
    out = bytearray(b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00")
    payload = b"\x00" * 16
    size = len(payload)
    for ttype, ts in tags:
        ts &= 0xFFFFFFFF
        out += (bytes([ttype])
                + size.to_bytes(3, "big")
                + (ts & 0xFFFFFF).to_bytes(3, "big")
                + bytes([(ts >> 24) & 0xFF])
                + b"\x00\x00\x00")          # stream id
        out += payload + size.to_bytes(4, "big")   # payload + prevTagSize
    return bytes(out)


tmp = Path(tempfile.mkdtemp(prefix="flvdur_"))

print("\u2500\u2500 1) \u8fde\u7eed\u65f6\u95f4\u6233\uff08\u65e0\u8df3\u53d8\uff09\u2192 \u65f6\u957f = \u672b-\u9996 \u2500\u2500")
tags = [(9, ts) for ts in range(0, 7000, 33)]          # 0..6963 \u2248 6.96s
p1 = tmp / "continuous.flv"
p1.write_bytes(make_flv(tags))
d1 = flv_duration_s(p1)
check("\u8fde\u7eed\u6837\u672c \u2248 6.96s", 6.9 <= d1 <= 7.0, f"\u2192 {d1:.3f}s")

print("\u2500\u2500 2) \u590d\u4f4d tag(ts=0) + \u8df3\u53d8\u5230\u4e0a\u884c\u7edd\u5bf9\u65f6\u95f4 \u2192 \u53d6\u6700\u957f\u8fde\u7eed\u6bb5 \u2500\u2500")
tags = [(9, 0), (8, 0)]                                 # \u5f00\u5934\u590d\u4f4d tag
tags += [(9, 8161266 + i * 33) for i in range(0, 224)]  # \u89c6\u9891 ~7.37s
tags += [(8, 8161274 + i * 21) for i in range(0, 351)]  # \u97f3\u9891 ~7.37s
p2 = tmp / "jump.flv"
p2.write_bytes(make_flv(tags))
d2 = flv_duration_s(p2)
check("\u8df3\u53d8\u6837\u672c\u53d6\u6700\u957f\u6bb5 \u2248 7.37s\uff08\u800c\u975e ~8168s\uff09", 7.3 <= d2 <= 7.45, f"\u2192 {d2:.3f}s")

print("\u2500\u2500 3) probe_media \u4e5f\u91c7\u7528\u540c\u4e00\u53e3\u5f84 \u2500\u2500")
m2 = probe_media(p2)
check("probe_media.duration \u2248 7.4s", 7.3 <= float(m2.get("duration") or 0) <= 7.5,
      f"\u2192 {m2.get('duration')}s")

print("\u2500\u2500 4) \u9000\u5316\u8f93\u5165 \u2500\u2500")
p3 = tmp / "single.flv"
p3.write_bytes(make_flv([(9, 5000)]))
check("\u5355 tag \u2192 0.0", flv_duration_s(p3) == 0.0, f"\u2192 {flv_duration_s(p3)}")
p4 = tmp / "notflv.bin"
p4.write_bytes(b"not a flv file at all")
check("\u975e FLV \u2192 0.0", flv_duration_s(p4) == 0.0, f"\u2192 {flv_duration_s(p4)}")

print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
