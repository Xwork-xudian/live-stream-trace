"""哔哩哔哩直播适配器（公开接口，无需签名/登录）。

数据源（实测 2026-10）：
  1. 直播间页面 SSR：`window.__NEPTUNE_IS_MY_WAIFU__` →
       roomInitRes     : room_id / uid / live_time / live_status
       roomInfoRes.data: room_info（title/live_start_time/online/area_name…）
                         anchor_info.base_info（uname/face）
  2. 拉流：https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo
       · g_qn_desc 档位表（30000 杜比 / 20000 4K / 15000 2K / 10000 原画 / 400 蓝光 / 250 超清 / 150 高清 / 80 流畅）
       · 每个 qn 单独请求一次；host + base_url + extra 拼出地址
       · protocol_name: http_stream(FLV) / http_hls(TS·fMP4)；codec: avc / hevc
  3. 粉丝/关注：https://api.bilibili.com/x/relation/stat?vmid={uid}
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests

from base import (LiveRoom, PlatformAdapter, PlatformMeta, StreamTier,
                  format_client_version, infer_push_channel)

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
H_HTML = {"User-Agent": UA, "Referer": "https://live.bilibili.com/", "Accept": "text/html,*/*"}
H_API = {"User-Agent": UA, "Referer": "https://live.bilibili.com/", "Accept": "application/json, text/plain, */*"}

ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
PLAY_INFO_API = ("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
                 "?room_id={room_id}&protocol=0,1&format=0,1,2&codec=0,1&qn={qn}"
                 "&platform=web&ptype=8&dolby=5&panorama=1")
RELATION_API = "https://api.bilibili.com/x/relation/stat?vmid={uid}"
LIVE_PAGE = "https://live.bilibili.com/{room_id}"

NEPTUNE_RE = re.compile(r"window\.__NEPTUNE_IS_MY_WAIFU__\s*=\s*(\{.*?\})\s*</script>", re.S)
ROOM_ID_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d+)")
SHORT_URL_RE = re.compile(r"b23\.tv/([A-Za-z0-9]+)")
SHORT_ID_RE = re.compile(r"live\.bilibili\.com/(\d+)")

CODEC_LABEL = {"avc": "H.264", "hevc": "H.265"}
FMT_LABEL = {"flv": "FLV", "ts": "HLS-TS", "fmp4": "HLS-fMP4"}


class BilibiliAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="bilibili", name="哔哩哔哩", color="#FB7299",
        live_url="https://live.bilibili.com/{room_id}", cta="前往哔哩哔哩直播",
    )

    def matches(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if "bilibili.com" in t or "b23.tv" in t:
            return True
        return bool(re.fullmatch(r"\d{1,12}", t))

    def resolve(self, text: str, ctx: Optional[dict] = None, *, probe: bool = True) -> LiveRoom:
        """probe=False = 快速路径：不逐档探测（每档要拉前 1.2MB，单档 1~3s），
        只取 qn=10000（原画）档位。给 /live 这类「只要一个能播的 FLV 地址」的场景用。"""
        room = LiveRoom(platform="bilibili", room_id="")
        room_id = self._extract_room_id(text)
        if not room_id:
            room.status = "解析失败"
            room.notes.append(f"无法从输入识别 B 站房间号: {text}")
            return room
        # 短号 → 真实房间号
        info = self._api(ROOM_INFO_API.format(room_id=room_id))
        info_data = info.get("data") or {}
        rid = str(info_data.get("room_id") or room_id)
        room.room_id = rid
        room.raw["room_info_api"] = info_data

        # 1) 页面 SSR：主播/标题/开播时间/人气/分区
        ssr = self._fetch_ssr(rid)
        if ssr:
            room.raw["ssr"] = {"roomInitRes": ssr.get("roomInitRes"), "roomInfoRes": ssr.get("roomInfoRes")}
            d = ((ssr.get("roomInfoRes") or {}).get("data")) or {}
            rn = d.get("room_info") or {}
            ai = (d.get("anchor_info") or {}).get("base_info") or {}
            room.title = rn.get("title") or info_data.get("title")
            room.anchor = ai.get("uname")
            room.avatar = ai.get("face") or info_data.get("user_cover")
            room.anchor_id = str(rn.get("uid") or info_data.get("uid") or "")
            room.live_start_ts = rn.get("live_start_time") or (ssr.get("roomInitRes") or {}).get("data", {}).get("live_time")
            room.metrics.update({
                "viewers": rn.get("online") or info_data.get("online"),
                "area": "/".join(x for x in (rn.get("parent_area_name"), rn.get("area_name")) if x),
            })
        else:
            room.title = info_data.get("title")
            room.anchor_id = str(info_data.get("uid") or "")
            room.avatar = info_data.get("user_cover")
            room.metrics["viewers"] = info_data.get("online")
        live_status = info_data.get("live_status", ((ssr or {}).get("roomInitRes") or {}).get("data", {}).get("live_status"))
        room.status = {1: "直播中", 0: "未开播", 2: "轮播中"}.get(live_status, f"未知({live_status})")
        if room.live_start_ts:
            room.elapsed_seconds = max(0, int(time.time()) - int(room.live_start_ts))

        # 2) 粉丝/关注
        if room.anchor_id:
            rel = self._api(RELATION_API.format(uid=room.anchor_id)).get("data") or {}
            if rel:
                room.metrics.update({"fans": rel.get("follower"), "follows": rel.get("following")})
                room.raw["relation"] = rel
        if info_data.get("attention") is not None:
            room.metrics.setdefault("attention", info_data.get("attention"))

        # 3) 拉流档位
        base = self._play_data(rid, qn=10000)
        if not base:
            room.notes.append("getRoomPlayInfo 未返回拉流信息（可能未开播或风控）")
            room.push.update({"channel": "未知", "server": "哔哩哔哩直播 CDN", "third_party": None})
            return room
        room.raw["playurl_head"] = {k: base.get(k) for k in ("room_id", "live_status", "live_time", "encrypted", "is_portrait")}
        play = (base.get("playurl_info") or {}).get("playurl") or {}
        qn_desc = {int(q["qn"]): q.get("desc") for q in (play.get("g_qn_desc") or []) if q.get("qn") is not None}
        accept = self._accept_qn(play)
        if not room.live_start_ts and base.get("live_time"):
            room.live_start_ts = int(base["live_time"])
            room.elapsed_seconds = max(0, int(time.time()) - room.live_start_ts)

        qn_list = sorted(accept, reverse=True)
        if not probe and 10000 in accept:
            qn_list = [10000]          # 快速路径：只打一次 getRoomPlayInfo
        tiers: list[StreamTier] = []
        for qn in qn_list:
            pd = base if qn == 10000 else self._play_data(rid, qn=qn)
            if not pd:
                continue
            tiers.extend(self._tiers_from_playurl(
                (pd.get("playurl_info") or {}).get("playurl") or {}, qn, qn_desc.get(qn, f"qn{qn}")))
            if len(tiers) >= 18:
                break
        if not probe:
            for _t in tiers:            # 快速路径不做连通性探测
                _t.ok = True
                _t.status = "未探测（快速路径）"
            room.tiers = tiers
        else:
            room.tiers = self.probe_tiers(tiers, headers=H_API)

        room.push.update({
            "channel": "平台侧未暴露推流端（CDN 重封装）",
            "version": None,
            "server": "哔哩哔哩直播 CDN（bilivideo.com）",
            "third_party": None,
        })
        room.notes.append("B 站不对外暴露推流端信息（无 onMetaData encoder 字段），开播渠道无法推断")
        return room

    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> "str | None":
        """按 qn 重新请求 playurl，返回指定协议/编码的新地址（B 站地址绑定会话，需实时刷新）。"""
        import re as _re
        m = _re.search(r"(\d+)", quality_key or "")
        if not m:
            return None
        qn = int(m.group(1))
        data = self._play_data(room_id, qn=qn)
        play = (data.get("playurl_info") or {}).get("playurl") or {}
        want_proto = "http_stream" if protocol == "flv" else "http_hls"
        for st in play.get("stream") or []:
            if str(st.get("protocol_name") or "") != want_proto:
                continue
            for f in st.get("format") or []:
                for c in f.get("codec") or []:
                    accept = {int(x) for x in (c.get("accept_qn") or []) if str(x).isdigit()}
                    if qn not in accept:
                        continue
                    if codec and str(c.get("codec_name") or "") != codec:
                        continue
                    url = self._build_url(c)
                    if url.startswith("http"):
                        return url
        return None

    # ── 内部 ──
    @staticmethod
    def _extract_room_id(text: str) -> Optional[str]:
        t = (text or "").strip()
        if t.isdigit():
            return t
        m = ROOM_ID_RE.search(t) or SHORT_ID_RE.search(t)
        if m:
            return m.group(1)
        m = SHORT_URL_RE.search(t)
        if m:
            try:
                resp = requests.get(f"https://b23.tv/{m.group(1)}", headers=H_HTML, timeout=10, allow_redirects=True)
                m2 = ROOM_ID_RE.search(str(resp.url))
                if m2:
                    return m2.group(1)
            except requests.RequestException:
                pass
        return None

    @staticmethod
    def _api(url: str, timeout: int = 12) -> dict:
        try:
            data = requests.get(url, headers=H_API, timeout=timeout).json()
            return data if data.get("code") == 0 else {}
        except (requests.RequestException, ValueError) as exc:
            logger.debug("B站接口失败 %s: %s", url[:70], exc)
            return {}

    @staticmethod
    def _fetch_ssr(room_id: str) -> dict:
        try:
            html = requests.get(LIVE_PAGE.format(room_id=room_id), headers=H_HTML, timeout=15).text
        except requests.RequestException as exc:
            logger.debug("B站页面抓取失败: %s", exc)
            return {}
        m = NEPTUNE_RE.search(html)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

    def _play_data(self, room_id: str, qn: int = 10000) -> dict:
        return (self._api(PLAY_INFO_API.format(room_id=room_id, qn=qn)) or {}).get("data") or {}

    @staticmethod
    def _accept_qn(play: dict) -> set:
        out = set()
        for st in play.get("stream") or []:
            for f in st.get("format") or []:
                for c in f.get("codec") or []:
                    for q in c.get("accept_qn") or []:
                        try:
                            out.add(int(q))
                        except (TypeError, ValueError):
                            continue
        return out

    @staticmethod
    def _build_url(codec_item: dict) -> str:
        ui = (codec_item.get("url_info") or [{}])[0]
        return f"{ui.get('host', '')}{codec_item.get('base_url', '')}{ui.get('extra', '')}"

    def _tiers_from_playurl(self, play: dict, qn: int, desc: str) -> list:
        """某档位的地址展开：优先 FLV-AVC / HLS-AVC，HEVC 保留但标记不可网页播放。"""
        out: list[StreamTier] = []
        for st in play.get("stream") or []:
            proto_name = str(st.get("protocol_name") or "")
            for f in st.get("format") or []:
                fmt = str(f.get("format_name") or "")
                for c in f.get("codec") or []:
                    accept = {int(x) for x in (c.get("accept_qn") or []) if str(x).isdigit()}
                    if qn not in accept:
                        continue
                    codec = str(c.get("codec_name") or "")
                    url = self._build_url(c)
                    if not url.startswith("http"):
                        continue
                    protocol = "flv" if proto_name == "http_stream" else "hls"
                    out.append(StreamTier(
                        label=f"{desc}（{CODEC_LABEL.get(codec, codec.upper())}/{FMT_LABEL.get(fmt, fmt)}）",
                        protocol=protocol, url=url, codec=codec, quality_key=f"qn{qn}",
                        playable=(codec == "avc"),
                        note="" if codec == "avc" else "HEVC：网页播放器不支持，请用 VLC/ffplay",
                    ))
                    if len(out) >= 3:
                        break
                if len(out) >= 3:
                    break
        return out


ADAPTER = BilibiliAdapter()
