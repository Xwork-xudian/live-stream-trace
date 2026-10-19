#!/usr/bin/env python3
"""京东直播可行性验证（**依赖外网 + 已安装 chromium**）。

验证链路：分享文本 → 短链跳转 → 直播间 → 浏览器拦截 getImmediatePlayToM → 档位 → ffprobe 拉流。

背景（实测结论，2026-10）：
  · `api.m.jd.com` 可直连，但业务接口需要 POST + `eid`（京东风控 JS 生成）→ 直连 403；
  · `lives.jd.com` 是 hash 路由 SPA，无 SSR 数据；
  · 可行路线 = 无头浏览器（页面自己生成 eid 并调用），拦截响应拿 `videoUrl/h5VideoUrl`；
  · 拉流 CDN `*.jdcloud.com` 不需要 Referer/Cookie。

用法：
    ./.venv/bin/python tests/verify_jd.py                 # 用内置分享文本
    ./.venv/bin/python tests/verify_jd.py "https://lives.jd.com/#/123456789"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "platforms")]

from jd import ADAPTER                      # noqa: E402
from share_resolver import resolve_share    # noqa: E402

DEFAULT_TEXT = ("【京东】https://3.cn/xxxxxxx 「红包雨派发中，速速来抢【京东相机巨出片】」"
                "点击链接直接打开")
fails: list[str] = []


def check(name, ok, extra=""):
    print(f"  {'✅' if ok else '❌'} {name} {extra}")
    if not ok:
        fails.append(name)


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    print("=" * 88)
    print(" 京东直播可行性验证")
    print("=" * 88)
    print(f"输入: {text[:90]}")

    res = resolve_share(text)
    check("分享文本解析出直播间链接", bool(res.get("url")), f"→ room_id={res.get('room_id')}")
    target = res.get("url") or text

    room = ADAPTER.resolve(target)
    check("适配器识别为京东直播", room.platform == "jd")
    check("解析出房间号", bool(room.room_id), f"→ {room.room_id}")
    check("获取到拉流档位", len(room.tiers) > 0, f"→ {len(room.tiers)} 个")
    for t in room.tiers:
        print(f"      · {t.label} [{t.protocol}] {(t.url or '')[:88]}")
    check("房间在播", room.status == "直播中", f"→ {room.status}")

    if room.tiers:
        room.tiers = ADAPTER.probe_tiers(room.tiers)
        ok_tiers = [t for t in room.tiers if t.ok]
        check("档位可拉流（ffprobe）", bool(ok_tiers), f"→ {len(ok_tiers)}/{len(room.tiers)} 可用")
        for t in room.tiers:
            info = f"{t.codec} {t.width}x{t.height}" if t.ok else str(t.status)
            print(f"      {'✅' if t.ok else '❌'} {t.label:14s} {t.protocol:4s} {info}")

    print("-" * 88)
    print("❌ 失败项: " + ", ".join(fails) if fails else "✅ 京东直播链路验证通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
