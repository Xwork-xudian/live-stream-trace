#!/usr/bin/env python3
"""采样端规避回归：持续采样直到找到「正脸 + 未遮挡 + 在说话」的窗口。

用可注入的 capture_fn / scan_fn 驱动，离线确定；截取用真实 ffmpeg 流拷贝（不重编码）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
import segment_scan as S  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'\u2705' if cond else '\u274c'} {name} {extra}")
    if not cond:
        fails.append(name)


SRC = ROOT / "tests" / "media" / "test_d0.flv"      # 20s 常驻合成样本（可作 chunk 源）
tmp = Path(tempfile.mkdtemp(prefix="acq_"))

print("\u2500\u2500 1) readiness_of_window \u2500\u2500")
good = {"frontality": 0.9, "occlusion": 0.05, "speech_activity": 0.4, "motion_activity": 0.6}
ok, why = S.readiness_of_window(good)
check("达标窗口 -> ready", ok and not why, f"\u2192 {why}")
for field, val, kw in (("frontality", 0.3, "\u975e\u6b63\u8138"), ("occlusion", 0.5, "\u906e\u6321"),
                       ("speech_activity", 0.05, "\u8bf4\u8bdd"), ("motion_activity", 0.1, "\u5634\u52a8")):
    ok2, why2 = S.readiness_of_window({**good, field: val})
    check(f"{field}={val} -> 否决({kw})", (not ok2) and len(why2) == 1, f"\u2192 {why2}")
ok3, why3 = S.readiness_of_window({"frontality": None, "occlusion": None,
                                   "speech_activity": None, "motion_activity": None})
check("指标缺失不否决", ok3, f"\u2192 {why3}")

print("\u2500\u2500 2) 首次不可测 \u2192 自动续采直到就绪 \u2500\u2500")

def cap(dest, seconds):
    shutil.copyfile(SRC, dest)
    return True


calls = {"n": 0}
SEQ = [
    [{"start": 0.0, "end": 10.0, "score": 90, "frontality": 0.8, "occlusion": 0.55,
      "speech_activity": 0.4, "motion_activity": 0.6}],                       # 遮挡 -> 不可测
    [{"start": 1.0, "end": 11.0, "score": 95, "frontality": 0.3, "occlusion": 0.1,
      "speech_activity": 0.4, "motion_activity": 0.6}],                       # 非正脸 -> 不可测
    [{"start": 2.0, "end": 12.0, "score": 70, "frontality": 0.9, "occlusion": 0.05,
      "speech_activity": 0.35, "motion_activity": 0.5, "env_activity": 0.3}], # 可测
]

def scan(src, **kw):
    i = calls["n"]; calls["n"] += 1
    return {"windows": SEQ[min(i, len(SEQ) - 1)], "best": None, "meta": {}}


logs = []
r = S.acquire_ready_sample(cap, out_dir=tmp, ts="T1", chunk_s=5.0, win_s=5.0,
                           max_attempts=5, max_seconds=60, scan_fn=scan, log=logs.append)
print("   result:", {k: r[k] for k in ("ready", "reason", "sample", "elapsed_s")})
check("第 3 次采到可测窗口", r.get("ready") is True and calls["n"] == 3, f"\u2192 attempts={calls['n']}")
check("选中窗口为可测窗口", (r.get("window") or {}).get("start") == 2.0, f"\u2192 {r.get('window')}")
check("产出子样本文件（流拷贝）", r.get("sample") is not None and Path(r["sample"]).exists()
      and Path(r["sample"]).stat().st_size > 4096, f"\u2192 {r.get('sample')}")
check("尝试记录 3 条", len(r.get("attempts") or []) == 3, f"\u2192 {len(r.get('attempts') or [])}")
check("就绪窗口 reasons 为空", (r.get("window") or {}).get("reasons") == [])

print("\u2500\u2500 3) 始终不可测 \u2192 回退历史最佳窗口 \u2500\u2500")
calls2 = {"n": 0}

def scan2(src, **kw):
    calls2["n"] += 1
    return {"windows": [{"start": 0.5, "end": 8.0, "score": 60, "frontality": 0.4,
                         "occlusion": 0.6, "speech_activity": 0.1, "motion_activity": 0.2}],
            "best": None, "meta": {}}

r2 = S.acquire_ready_sample(cap, out_dir=tmp, ts="T2", chunk_s=5.0, win_s=5.0,
                            max_attempts=3, max_seconds=60, scan_fn=scan2, log=logs.append)
check("未就绪但回退最佳窗口", r2.get("ready") is False and r2.get("sample") is not None,
      f"\u2192 {r2.get('reason')}")
check("回退窗口被导出", Path(r2["sample"]).exists() and Path(r2["sample"]).stat().st_size > 4096)

print("\u2500\u2500 4) 完全采不到 \u2192 明确失败 \u2500\u2500")

def cap_fail(dest, seconds):
    return False


r3 = S.acquire_ready_sample(cap_fail, out_dir=tmp, ts="T3", chunk_s=5.0, win_s=5.0,
                            max_attempts=2, max_seconds=60, scan_fn=scan, log=logs.append)
check("无样本 -> ready=False 且 sample=None", r3.get("ready") is False and r3.get("sample") is None,
      f"\u2192 {r3.get('reason')}")
check("失败尝试被记录", all(not a.get("captured") for a in r3.get("attempts") or []),
      f"\u2192 {r3.get('attempts')}")

print("\u2500\u2500 5) 时间预算生效 \u2500\u2500")
calls3 = {"n": 0}

def scan3(src, **kw):
    calls3["n"] += 1
    return {"windows": [], "best": None, "meta": {}}

r4 = S.acquire_ready_sample(cap, out_dir=tmp, ts="T4", chunk_s=1.0, win_s=1.0,
                            max_attempts=50, max_seconds=0.001, scan_fn=scan3, log=logs.append)
check("预算耗尽提前停止（远小于 max_attempts）",
      r4.get("ready") is False and 0 < calls3["n"] < 50, f"\u2192 calls={calls3['n']}/50")

shutil.rmtree(tmp, ignore_errors=True)
print("=" * 60)
if fails:
    print(f"\u274c \u5931\u8d25 {len(fails)} \u9879: {fails}")
    sys.exit(1)
print("\u2705 \u5168\u90e8\u901a\u8fc7")
