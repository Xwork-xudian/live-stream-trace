"""把「嗅探到的流地址 + 页面/接口 JSON」装配成归一化 LiveRoom（淘宝/京东等共用）。"""
from __future__ import annotations

from typing import Any, Optional

from base import LiveRoom, StreamTier
from _h5_common import deep_find, protocol_of, quality_hint

# 通用字段候选（各平台 key 名不同，按序取第一个非空）
COMMON_KEY_CANDIDATES: dict[str, tuple] = {
    "title": ("roomTitle", "liveTitle", "title", "subject", "roomName"),
    "anchor": ("anchorName", "nickName", "nickname", "anchorNick", "sellerNick",
               "shopName", "nick", "authorName"),
    "anchor_id": ("anchorId", "userId", "sellerId", "uid", "anchorUid", "shopId"),
    "avatar": ("avatar", "avatarUrl", "anchorAvatar", "headImg", "headPic", "logo"),
    "live_start_ts": ("startTime", "liveStartTime", "start_time", "liveStartTs", "beginTime"),
    "viewers": ("viewerCount", "onlineNum", "onlineCount", "userCount", "online", "pv",
                "watchingCount"),
    "fans": ("fansCount", "followerCount", "fansNum", "fans", "concernCount"),
    "follows": ("followCount", "followNum", "attentionCount"),
    "likes": ("likeCount", "praiseNum", "likeNum", "praiseCount"),
    "account_id": ("accountId", "sellerId", "shopId", "uid"),
}
PUSH_KEYS = ("pushClient", "pushType", "clientType", "liveSource", "thirdParty",
             "pushSoftware", "broadcastType")


def _norm_ts(value: Any) -> Optional[int]:
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    if ts > 10 ** 12:          # 毫秒 → 秒
        ts //= 1000
    return ts if 1_000_000_000 < ts < 4_000_000_000 else None


def apply_streams(room: LiveRoom, urls: list, *, note_prefix: str = "") -> int:
    """把 URL 列表装配为 tiers / rtmp（按协议分流、去重）。返回新增档位数。"""
    seen: set = set()
    for url in urls or []:
        if not url or url in seen:
            continue
        seen.add(url)
        proto = protocol_of(url)
        label, qkey = quality_hint(url)
        if proto == "rtmp":
            room.rtmp.append(StreamTier(label=f"RTMP 源流（{qkey or '源'}）", protocol="rtmp",
                                        url=url, quality_key=qkey, playable=False,
                                        note="浏览器不支持 RTMP，请用 VLC/ffplay 播放"))
        elif proto in ("flv", "hls"):
            room.tiers.append(StreamTier(label=label, protocol=proto, url=url,
                                         quality_key=qkey, playable=True))
        else:
            room.tiers.append(StreamTier(label=label, protocol="other", url=url,
                                         quality_key=qkey, playable=False,
                                         note="未识别的流地址，需人工确认"))
    if not room.tiers and not room.rtmp and note_prefix:
        room.notes.append(note_prefix)
    return len(room.tiers) + len(room.rtmp)


def apply_metrics(room: LiveRoom, blobs: list, extra_keys: Optional[dict] = None) -> None:
    """从若干 JSON/对象里补齐指标、标题、主播、开播时间与开播渠道。"""
    keys = dict(COMMON_KEY_CANDIDATES)
    keys.update(extra_keys or {})
    src = [b for b in blobs if b]
    if room.title is None:
        v = deep_find(src, keys["title"]) if src else None
        room.title = str(v)[:200] if v else None
    if room.anchor is None:
        v = deep_find(src, keys["anchor"]) if src else None
        room.anchor = str(v)[:100] if v else None
    if room.anchor_id is None:
        v = deep_find(src, keys["anchor_id"]) if src else None
        room.anchor_id = str(v) if v else None
    if room.avatar is None:
        v = deep_find(src, keys["avatar"]) if src else None
        room.avatar = str(v) if v else None
    if room.live_start_ts is None:
        room.live_start_ts = _norm_ts(deep_find(src, keys["live_start_ts"])) if src else None
    for metric in ("viewers", "fans", "follows", "likes", "account_id"):
        if room.metrics.get(metric) in (None, ""):
            v = deep_find(src, keys[metric]) if src else None
            if v not in (None, ""):
                try:
                    room.metrics[metric] = int(float(v))
                except (TypeError, ValueError):
                    room.metrics[metric] = str(v)[:60]
    if not room.push.get("channel") and src:
        v = deep_find(src, PUSH_KEYS)
        if v not in (None, ""):
            room.push["channel"] = str(v)[:80]


def finalize_status(room: LiveRoom, *, live_hint: Any = None) -> None:
    """根据是否存在流地址 / 状态字段判断直播状态。"""
    if room.tiers or room.rtmp:
        room.status = "直播中"
        return
    if live_hint is not None:
        txt = str(live_hint).lower()
        if txt in ("2", "1", "true", "live", "living", "on", "直播中"):
            room.status = "直播中（未取到流地址）"
        elif txt in ("0", "false", "off", "offline", "未开播"):
            room.status = "未开播"
        else:
            room.status = "未知"
    else:
        room.status = "未知（未取到流地址）"
