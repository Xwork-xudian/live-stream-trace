"""抖音直播适配器。

复用 dy_auth_impl（无头 Chromium 拦截 web/enter）拿房间 + stream_url 结构：
  · flv_pull_url / hls_pull_url_map：按画质档（FULL_HD1 蓝光 / HD1 超清 / SD1 标清 / SD2 高清）
  · 源流（未转码）：由 ao（纯音频）地址去掉 only_audio / eupf_relay_mode 得到无后缀 stream-<id>.flv
  · 备份线路：live_core_sdk_data.data.<档>.backup
  · 账号：owner.nickname / follow_info.follower_count / room.user_count / create_time
  · 开播渠道：由 FLV onMetaData（encoder/flashVer）推断，由 core 采样后填充
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from base import LiveRoom, PlatformAdapter, PlatformMeta, StreamTier

import dy_auth_impl as dya

logger = logging.getLogger(__name__)

QUALITY_LABEL = {"FULL_HD1": "蓝光/原画", "HD1": "超清", "SD1": "标清", "SD2": "高清"}
ROOM_RE = re.compile(r"live\.douyin\.com/(?:u/)?(\d{6,25})")
SHORT_RE = re.compile(r"v\.douyin\.com/([A-Za-z0-9_-]+)")
# 短链跳转后的移动端直播页：webcast.amemv.com/douyin/webcast/reflow/<内部 room_id>
REFLOW_RE = re.compile(r"(https?://[^\s<>]*?reflow/(\d{6,25})[^\s<>]*)")


class DouyinAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="douyin", name="抖音", color="#FE2C55",
        live_url="https://live.douyin.com/{room_id}", cta="前往抖音直播",
    )

    def matches(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        return any(k in t for k in ("douyin.com", "iesdouyin.com", "amemv.com"))

    # ── 解析 ──
    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        ctx = ctx or {}
        room = LiveRoom(platform="douyin", room_id="")
        tgt = self._resolve_target(text)
        if not tgt:
            room.status = "解析失败"
            room.notes.append(f"无法识别抖音房间号/链接: {text}")
            return room
        web_rid = tgt.get("value") or ""
        if tgt.get("page_url"):
            room.notes.append(f"已从分享链接定位到直播页（{tgt.get('kind')}）：{tgt['page_url'][:110]}")
            room.metrics["share_url"] = tgt["page_url"]
        ctx_auth = dya.authenticate_room(web_rid or "0", headless=ctx.get("headless", True),
                                         page_url=tgt.get("page_url"))
        if not ctx_auth.get("ok"):
            room.status = "解析失败"
            room.notes.append(f"web/enter 未返回数据: {ctx_auth.get('error')}")
            return room
        room.raw["enter"] = {k: v for k, v in ctx_auth.items()
                             if k in ("room_status", "internal_room_id", "ms_token")}
        info = ctx_auth.get("room") or {}
        su = ctx_auth.get("stream_url_obj") or {}
        room.room_id = str(ctx_auth.get("internal_room_id") or info.get("id_str") or web_rid or "")
        status = ctx_auth.get("room_status")
        room.status = {2: "直播中", 4: "已结束"}.get(status, f"未知({status})")
        owner = info.get("owner") or {}
        user = owner.get("follow_info") or {}
        room.title = info.get("title")
        room.anchor = owner.get("nickname")
        room.anchor_id = str(owner.get("id_str") or owner.get("id") or "")
        avatar = ((owner.get("avatar_thumb") or {}).get("url_list") or [""])[0]
        room.avatar = avatar
        room.metrics.update({
            "fans": user.get("follower_count"),
            "follows": user.get("following_count"),
            "viewers": info.get("user_count"),
            "display_id": owner.get("display_id") or owner.get("short_id"),
        })
        if info.get("create_time"):
            room.live_start_ts = int(info["create_time"])
            import time as _t
            room.elapsed_seconds = max(0, int(_t.time()) - room.live_start_ts)
        room.raw["stream_url_obj"] = su
        if info.get("status") == 2 and su:
            room.tiers = self._tiers_from_stream_url(su)
        else:
            room.notes.append("房间未开播，无拉流地址")
        return room

    # ── 内部 ──
    @staticmethod
    def _resolve_target(text: str) -> Optional[dict]:
        """把各种抖音输入（房间号 / live.douyin.com 链接 / webcast reflow 页 / v.douyin.com 短链）
        归一为 {kind, value, page_url}。

        · kind="web_rid"  ：live.douyin.com/<web_rid>
        · kind="internal" ：短链跳转后的 reflow 页，内含**内部 room_id**，page_url 为该页地址
        """
        import requests
        t = (text or "").strip()
        if not t:
            return None
        m = REFLOW_RE.search(t)
        if m:
            return {"kind": "internal", "value": m.group(2), "page_url": m.group(1)}
        if t.isdigit():
            return {"kind": "web_rid", "value": t, "page_url": None}
        m = ROOM_RE.search(t)
        if m:
            return {"kind": "web_rid", "value": m.group(1), "page_url": None}
        m = SHORT_RE.search(t)
        if m:
            try:
                resp = requests.get(
                    f"https://v.douyin.com/{m.group(1)}/",
                    headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
                    timeout=12, allow_redirects=True)
                final = str(resp.url)
                logger.info("抖音短链跳转 → %s", final[:110])
                m2 = REFLOW_RE.search(final)
                if m2:
                    return {"kind": "internal", "value": m2.group(2), "page_url": m2.group(1)}
                m2 = ROOM_RE.search(final)
                if m2:
                    return {"kind": "web_rid", "value": m2.group(1), "page_url": None}
                # 兜底：跳转到用户主页等 → 直接把最终地址交给浏览器打开
                return {"kind": "page", "value": "", "page_url": final}
            except Exception as exc:  # noqa: BLE001
                logger.warning("抖音短链跳转失败: %s", exc)
        return None

    @staticmethod
    def _extract_room(text: str) -> Optional[str]:
        tgt = DouyinAdapter._resolve_target(text)
        return tgt.get("value") if tgt else None

    def extract_room_id(self, text: str) -> str:
        """供 share_resolver 等外部使用（对外统一命名）。"""
        return self._extract_room(text) or ""

    def _tiers_from_stream_url(self, su: dict) -> list:
        """把抖音 stream_url 结构展开为档位列表（含源流与备份线路）。"""
        res_names = su.get("resolution_name") or {}

        def qlabel(key: str) -> str:
            return res_names.get(key) or QUALITY_LABEL.get(key, key)

        tiers: list[StreamTier] = []
        flv = su.get("flv_pull_url") or {}
        hls = su.get("hls_pull_url_map") or {}
        for q, url in flv.items():
            if isinstance(url, str) and url.startswith("http"):
                tiers.append(StreamTier(label=f"{qlabel(q)}（FLV）", protocol="flv", url=url, quality_key=q))
        for q, url in hls.items():
            if isinstance(url, str) and url.startswith("http"):
                tiers.append(StreamTier(label=f"{qlabel(q)}（HLS）", protocol="hls", url=url, quality_key=q))

        # 源流：ao 纯音频地址去掉 only_audio / eupf_relay_mode
        sdk = su.get("live_core_sdk_data") or {}
        pull = sdk.get("pull_data") or {}
        sdata = None
        raw_sd = pull.get("stream_data")
        if isinstance(raw_sd, str):
            import json
            try:
                sdata = json.loads(raw_sd)
            except json.JSONDecodeError:
                sdata = None
        if isinstance(sdata, dict):
            ao = ((sdata.get("data") or {}).get("ao")) or {}
            for line in ("main", "backup"):
                u = (ao.get(line) or {}).get("flv") or ""
                if not u:
                    continue
                p = urlparse(u)
                qd = {k: v[0] for k, v in parse_qs(p.query).items()}
                removed = [k for k in ("only_audio", "eupf_relay_mode") if qd.pop(k, None) is not None]
                if not removed:
                    continue
                src = urlunparse(p._replace(query=urlencode(qd)))
                tiers.insert(0, StreamTier(
                    label="源流（未转码）" if line == "main" else "源流·备线（未转码）",
                    protocol="flv", url=src, quality_key=f"source_{line}"))
            # 备份线路（各档 backup）
            for qkey, lv in (sdata.get("data") or {}).items():
                bk = (lv or {}).get("backup") or {}
                u = bk.get("flv") or ""
                if isinstance(u, str) and u.startswith("http"):
                    tiers.append(StreamTier(label=f"{qlabel(qkey.upper())}·备线（FLV）",
                                            protocol="flv", url=u, quality_key=f"{qkey}_backup"))
        return tiers


ADAPTER = DouyinAdapter()
