#!/usr/bin/env python3
"""抖音分享口令解析回归测试（分享文案 → 直播间链接 → 房间/档位）。

依赖外网 + chromium；用法：
    ./.venv/bin/python tests/verify_douyin_share.py
    ./.venv/bin/python tests/verify_douyin_share.py "<自定义分享文案或链接>"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "platforms")]

from douyin import ADAPTER          # noqa: E402
from share_resolver import resolve_share  # noqa: E402

DEFAULT = ("7- #在抖音，记录美好生活#【示例主播】正在直播，来和我一起支持Ta吧。"
           "复制下方链接，打开【抖音】，直接观看直播！ https://v.douyin.com/sh4re1d0002/ "
           "$3 CA2078 8@x.com :0pm")
fails: list[str] = []


def check(name, ok, extra=""):
    print(f"  {'✅' if ok else '❌'} {name} {extra}")
    if not ok:
        fails.append(name)


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    print("=" * 92)
    print(" 抖音分享口令解析回归")
    print("=" * 92)
    print(f"输入: {text[:96]}")

    res = resolve_share(text)
    check("识别为抖音分享", res.get("kind") == "douyin_share", f"kind={res.get('kind')}")
    check("解析出直播间链接", bool(res.get("url")), f"{(res.get('url') or '')[:80]}")
    check("解析出房间号", bool(res.get("room_id")), f"room_id={res.get('room_id')}")
    check("短链已跳转（非 v.douyin.com）", "v.douyin.com" not in (res.get("url") or ""))

    tgt = ADAPTER._resolve_target(text)
    check("目标归一（含 page_url）", bool(tgt and tgt.get("page_url")), f"{tgt}")

    room = ADAPTER.resolve(res.get("url") or text)
    check("适配器解析成功（直播中）", room.status == "直播中", f"status={room.status}")
    check("房间号/主播/标题齐全", bool(room.room_id and room.anchor),
          f"{room.room_id} | {room.anchor} | {(room.title or '')[:24]}")
    check("拿到档位", len(room.tiers) > 0, f"{len(room.tiers)} 个档位")
    for t in room.tiers[:4]:
        print(f"      · {t.label} [{t.protocol}] {(t.url or '')[:76]}")

    print("-" * 92)
    print("❌ 失败项: " + ", ".join(fails) if fails else "✅ 抖音分享口令解析链路通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
