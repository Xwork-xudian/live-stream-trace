"""小红书直播适配器（复用 xhs_room_impl / xhs_probe_impl 的实测逻辑）。

要点：
  · 页面 SSR（移动端 UA）取直播间信息；deeplink 提供 host_id/host_nickname/头像
  · pullConfig 给官方地址矩阵（主/备线路）
  · 档位后缀实测：{room}.flv 原画 H.264 / {room}_orig.flv 原画 HEVC /
    {room}_hcv5400.flv 安卓原画 HEVC / {room}_sd.flv 标清；同名 .m3u8 为 HLS
  · RTMP 源流：rtmp://live-play.xhscdn.com/live/{room}（时好时坏）与 live-source-play
  · 直播时长：FLV tag 时间戳为流内绝对位置
  · 账号资料（小红书号/粉丝/关注）需登录 Cookie：cookies/xiaohongshu.cookie
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from base import LiveRoom, PlatformAdapter, PlatformMeta, StreamTier

import xhs_config as xc
import xhs_probe_impl as xp
import xhs_room_impl as xr

logger = logging.getLogger(__name__)


class XiaohongshuAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="xiaohongshu", name="小红书", color="#FF2442",
        live_url="https://www.xiaohongshu.com/livestream/{room_id}", cta="前往小红书直播",
    )

    def matches(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if any(k in t for k in ("xiaohongshu.com", "xhslink.com", "xhslink.cn", "xhscdn.com")):
            return True
        return False

    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> "str | None":
        """小红书档位地址由「房间号 + 后缀」构成，可即时重建（签名在路径上，不随时间失效到分钟级）。"""
        suffix_map = {"source": "", "orig": "_orig", "hcv": "_hcv5400", "sd": "_sd"}
        key = (quality_key or "").replace("_backup", "")
        if key not in suffix_map:
            return None
        ext = ".m3u8" if protocol == "hls" else ".flv"
        return f"http://{xc.SOURCE_HOST}/live/{room_id}{suffix_map[key]}{ext}"

    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        ctx = ctx or {}
        cookies = ctx.get("cookies") or None
        headless = ctx.get("playwright", True)
        info = xr.resolve_room(text, cookies=cookies, use_playwright=False)
        room = LiveRoom(platform="xiaohongshu", room_id=str(info.get("room_id") or ""))
        room.raw["live_info"] = {k: v for k, v in info.items() if k != "raw_room_info"}
        if not info.get("ok"):
            room.status = "解析失败"
            room.notes.append(f"直播间解析失败: {info.get('error')}")
            return room
        room.title = info.get("title")
        room.anchor = info.get("anchor_name")
        room.anchor_id = info.get("host_id")
        room.avatar = info.get("host_avatar")
        room.status = "直播中" if info.get("live") else "未开播/回放"
        room.metrics.update({
            "viewers": info.get("viewer_count"),
            "members": info.get("member_count"),
            "praise": info.get("praise_count"),
        })
        room_id = room.room_id

        # ── 账号资料（需 Cookie）──
        prof = {}
        try:
            prof = xp.fetch_account_profile(room.anchor_id or "", cookies=cookies) if cookies else {"need_login": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("小红书账号资料获取失败: %s", exc)
        room.raw["account_profile"] = prof
        if prof.get("ok"):
            room.metrics.update({
                "red_id": prof.get("red_id"),
                "fans": (prof.get("fans") or {}).get("count"),
                "follows": (prof.get("follows") or {}).get("count"),
                "likes_collects": (prof.get("likes_collects") or {}).get("count"),
                "desc": prof.get("desc"),
                "ip_location": prof.get("ip_location"),
            })
            if prof.get("nickname"):
                room.anchor = room.anchor or prof.get("nickname")
            room.avatar = room.avatar or prof.get("avatar")
        else:
            room.metrics["red_id_locked"] = True
            room.notes.append("粉丝/关注/小红书号需登录 Cookie（cookies/xiaohongshu.cookie）")

        # ── 房间信息接口（Cookie 可用时含累计观看 pv）──
        if cookies and room_id:
            try:
                api = xp.fetch_with_sign(
                    f"/api/sns/red/live/web/v1/room/current_room_info?room_id={room_id}&request_user_id=",
                    cookies=cookies)
                if isinstance(api, dict) and api.get("success"):
                    ri = ((api.get("data") or {}).get("room_info")) or {}
                    room.raw["room_info_api"] = ri
                    if ri.get("pv") is not None:
                        room.metrics["pv"] = ri.get("pv")
            except Exception as exc:  # noqa: BLE001
                logger.debug("current_room_info 失败: %s", exc)

        if not info.get("live") or not room_id:
            return room

        # ── 档位探测（后缀实测）──
        tiers: list[StreamTier] = []
        samples = xp.probe_stream_tiers(room_id, headers=xr.COMMON_HEADERS,
                                        sample_bytes=ctx.get("sample_bytes", 1_200_000))
        room.raw["tier_probe"] = samples
        for s in samples:
            if not s.get("ok"):
                continue
            base_label = s.get("label", s.get("key", ""))
            for proto in ("flv", "hls"):
                url = s.get(proto)
                if not url:
                    continue
                ok = s.get("ok") if proto == "flv" else s.get("hls_ok")
                codec = s.get("codec") if proto == "flv" else None
                tiers.append(StreamTier(
                    label=f"{base_label}（{proto.upper()}）",
                    protocol=proto, url=url, ok=bool(ok), quality_key=s.get("key", ""),
                    codec=codec, width=s.get("width"), height=s.get("height"), bitrate=s.get("bitrate"),
                    playable=not str(codec or "").lower().startswith("hevc"),
                    note=(s.get("meta", {}) or {}).get("encoder", "") if proto == "flv" else "",
                ))
        # 官方 pullConfig 里的备份线路（仅原画）
        for pc_codec in ("h264", "h265"):
            for e in ((info.get("pull_config") or {}).get(pc_codec) or []):
                if e.get("line") == "backup" and e.get("protocol") == "flv":
                    tiers.append(StreamTier(
                        label=f"原画·备线（{e.get('line_label', '备线')}）", protocol="flv", url=e["url"],
                        quality_key=f"{pc_codec}_backup", codec=pc_codec))
        room.tiers = tiers

        # ── RTMP 源流 ──
        try:
            rtmp_list = xp.probe_rtmp_sources(room_id)
            room.raw["rtmp_probe"] = rtmp_list
            for r in rtmp_list:
                room.rtmp.append(StreamTier(
                    label="RTMP 源流" + ("（可用）" if r.get("ok") else "（探测失败）"),
                    protocol="rtmp", url=r.get("url", ""), ok=bool(r.get("ok")),
                    status=r.get("error") or "ok", codec=r.get("codec"),
                    width=r.get("width"), height=r.get("height"), playable=False,
                    note="网页播放器不支持 RTMP，请用 VLC/ffplay"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("RTMP 探测失败: %s", exc)

        # ── 直播时长（FLV 时间戳 = 流内绝对位置）──
        try:
            src = next((t for t in tiers if t.quality_key == "source" and t.protocol == "flv"), None)
            if src:
                dur = xp.flv_stream_duration(src.url, headers=xr.COMMON_HEADERS, sample_bytes=2_000_000)
                room.raw["duration"] = dur
                if dur.get("ok"):
                    room.elapsed_seconds = round(dur["elapsed_ms"] / 1000)
                    room.live_start_ts = int(time.time()) - room.elapsed_seconds
        except Exception as exc:  # noqa: BLE001
            logger.debug("时长解析失败: %s", exc)

        # ── 开播渠道（源流 onMetaData）──
        src_sample = next((s for s in samples if s.get("key") == "source" and s.get("meta")), None)
        if src_sample:
            meta = src_sample.get("meta") or {}
            channel = xp.infer_push_channel(meta)
            room.push.update({
                "channel": channel,
                "version": xp.format_client_version(meta),
                "server": xp.infer_push_server(meta),
                "third_party": channel.startswith("第三方推流"),
                "meta": meta,
            })
            room.raw["source_meta"] = meta
        return room


ADAPTER = XiaohongshuAdapter()
