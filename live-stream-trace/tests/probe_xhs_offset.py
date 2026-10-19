#!/usr/bin/env python3
"""用足够长的样本实测小红书测试间的真实内容级偏移（排除“样本过短”干扰）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "platforms")]

from lip_sync_detector import analyze_media, probe_media   # noqa: E402
from xiaohongshu import ADAPTER                             # noqa: E402
from base import http_probe                                 # noqa: E402

TARGET_S = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
OUT = Path(__file__).resolve().parent / "media" / "xhs_room_long.flv"

# 具体直播间分享短链不随仓库分发：请把自己的链接作为第 2 个参数传入
TARGET_URL = sys.argv[2] if len(sys.argv) > 2 else ""
if not TARGET_URL:
    raise SystemExit("用法: python tests/probe_xhs_offset.py [时长秒=12] <小红书分享链接>\n"
                     "（示例链接已脱敏移除，请用你自己的直播间链接）")

room = ADAPTER.resolve(TARGET_URL)
room.tiers = ADAPTER.probe_tiers(room.tiers)
tier = next((t for t in room.tiers if t.ok and t.protocol == "flv" and t.playable), None) \
    or next((t for t in room.tiers if t.ok), None)
print(f"房间 {room.room_id} | 状态 {room.status} | 选用档位: {tier.label if tier else '无'}")
if not tier:
    raise SystemExit("无可用档位")
print(f"  目标码率约 {round((tier.bitrate or 0)/1000) or '—'} kbps")

# 依据码率估算字节预算（取不到码率时按 12Mbps 估）
bps = tier.bitrate or 12_000_000
budget = int(bps / 8 * TARGET_S * 1.25)
print(f"  下载预算 ≈ {budget/1e6:.1f} MB（目标 {TARGET_S:.0f}s）")
hdrs = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"), "Accept": "*/*"}
t0 = time.time()
buf = bytearray()
resp = requests.get(tier.url, headers=hdrs, timeout=20, stream=True)
for chunk in resp.iter_content(65536):
    buf.extend(chunk)
    if len(buf) >= budget:
        break
resp.close()
OUT.write_bytes(bytes(buf))
meta = probe_media(OUT)
print(f"  样本 {OUT.name}: {len(buf)/1e6:.1f} MB, 时长 {meta['duration']}s, "
      f"{meta['width']}x{meta['height']}@{meta['fps']}fps（耗时 {time.time()-t0:.0f}s）")

d = analyze_media(OUT, seconds=min(20.0, meta["duration"] or TARGET_S))
m, p = d["metrics"], d["perceptual"]
print("\n判定:", d["verdict"], "| 等级:", d["verdict_level"])
print("内容级偏移: %s ms（全局峰 %s）| 分辨力 %s ms | CI± %s | 一致率 %s"
      % (p.get("content_offset_ms"), p.get("content_offset_global_ms"), p.get("lag_resolution_ms"),
         p.get("content_offset_ci_ms"), p.get("window_agreement")))
print("相关峰 r=%s | 人脸覆盖 %.0f%% | 抖动 %.1f px | 细通道 %s"
      % (m.get("peak_correlation"), (m.get("face_coverage") or 0) * 100,
         m.get("face_jitter_px") or 0, (m.get("fine") or {}).get("used")))
print("滑窗估计(ms):", [round(x) for x in (m.get("window_estimates_ms") or [])])
print("分段时间(ms):", m.get("segments"))
