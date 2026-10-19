"""京东直播适配器（**已验证可用**：浏览器拦截 `getImmediatePlayToM` 拿拉流地址）。

实测结论（2026-10-13，房间 48409854）：
  · `api.m.jd.com` 可直连，但业务接口需要 **POST + `eid` 设备指纹**（京东风控 JS 生成）
    → 直连返回 403；`lives.jd.com` 是 hash 路由 SPA，页面无 SSR 数据。
  · 因此可行路线是 **无头浏览器**：打开 `https://lives.jd.com/#/<liveId>`，
    让页面自己完成 eid 生成与调用，我们拦截响应：
      - `getImmediatePlayToM` → `{liveId, videoUrl(.flv), h5VideoUrl(.m3u8), pcVideoUrl, status}`
      - `status == 1` 视为直播中
  · 拉流 CDN（`*.jdcloud.com`）**不需要 Referer/Cookie**：ffprobe 实测可直接读到
    `h264 1080x1920 + aac`。
  · 注意：页面还会加载「推荐直播」列表，其流地址属于其它房间 —— 必须按 liveId 过滤，
    否则会把别人的流当成当前房间（已处理）。

数据来源（按优先级）：
  1. 直连 m 接口 `liveDetailToM`（若环境已具备 eid/登录态，可直接成功）
  2. 页面 SSR / 内嵌 JSON（京东通常为空，保底）
  3. 无头浏览器拦截 XHR（**主要路线**）+ `<video>` 元素兜底
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from base import LiveRoom, PlatformAdapter, PlatformMeta, StreamTier, format_client_version
from config import COMMON_UA_DESKTOP, load_cookie
from _h5_common import (collect_stream_urls, fetch_html, iter_json_blobs, resolve_short_url,
                        sniff_page)
from _h5_room import apply_metrics, apply_streams, finalize_status

logger = logging.getLogger(__name__)

JD_API = "https://api.m.jd.com/api"
JD_APPID = "h5-live"          # 实测：京东直播 H5 前端使用的 appid
PLAY_KEYS = ("h5VideoUrl", "videoUrl", "pcVideoUrl", "playUrl", "pullUrl", "flvUrl", "m3u8Url")
ROOM_ID_PATTERNS = (
    r"lives\.jd\.com/#/?(\d{5,})",
    r"lives\.jd\.com/.*?[?&]id=(\d{5,})",
    r"lives\.jd\.com/.*?[?&]liveId=(\d{5,})",
    r"[?&]roomId=(\d{5,})",
    r"[?&]liveId=(\d{5,})",
    r"[?&]id=(\d{5,})",
    r"/(\d{7,})(?:[/?#]|$)",
)


def jd_api_get(function_id: str, body: dict, *, cookies: str = "", timeout: int = 12) -> dict:
    """直连 api.m.jd.com（无 eid/登录态时通常被风控拒绝；失败返回 {}）。"""
    try:
        import requests  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    headers = {"User-Agent": COMMON_UA_DESKTOP, "Referer": "https://lives.jd.com/",
               "Accept": "application/json, text/plain, */*", "Origin": "https://lives.jd.com"}
    if cookies:
        headers["Cookie"] = cookies
    params = {"appid": JD_APPID, "functionId": function_id,
              "body": json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")),
              "t": str(int(time.time() * 1000)), "loginType": "3", "client": "h5"}
    for method in ("post", "get"):
        try:
            fn = getattr(requests, method)
            r = fn(JD_API, params=params if method == "get" else None,
                   data=params if method == "post" else None,
                   headers=headers, timeout=timeout)
            if r.status_code >= 400:
                logger.debug("京东接口 %s HTTP %s", function_id, r.status_code)
                continue
            out = r.json()
            if isinstance(out, dict) and out.get("code") == "2":
                logger.debug("京东接口 %s 不存在: %s", function_id, out.get("echo"))
                continue
            return out if isinstance(out, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("京东接口失败 %s(%s): %s", function_id, method, exc)
    return {}


def _unwrap(body) -> Optional[dict]:
    """取 data 层（不同接口层级不一致）。"""
    if not isinstance(body, dict):
        return None
    node = body.get("data")
    return node if isinstance(node, dict) else body


def pick_play_payload(bodies: list, room_id: str) -> dict:
    """从拦截到的响应里挑出「本房间」的拉流载荷（避免混入推荐直播的其它房间）。"""
    best: dict = {}
    for body in bodies:
        node = _unwrap(body)
        if not isinstance(node, dict):
            continue
        if not any(k in node for k in PLAY_KEYS):
            continue
        lid = node.get("liveId") or node.get("roomId")
        if lid is not None and str(lid) == str(room_id):
            return node
        if not best:
            best = node
    return best


class JdAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="jd", name="京东直播", color="#E1251B",
        live_url="https://lives.jd.com/#/{room_id}", cta="前往京东直播",
    )

    def matches(self, text: str) -> bool:
        t = (text or "").lower()
        if not t:
            return False
        return any(k in t for k in ("jd.com", "lives.jd", "jd.hk", "jingdong", "3.cn/", "jd.cn"))

    def extract_room_id(self, text: str) -> str:
        raw = text or ""
        # 分享文案里短链可能在任意位置（如「【京东】https://3.cn/xxx …」）→ 先抽取再展开
        m_url = re.search(r"https?://[^\s\"'<>）)】|]+", raw)
        if m_url:
            cand = m_url.group(0)
            if re.search(r"(3\.cn|jd\.hk|jd\.cn|u\.jd\.com)/", cand, re.I):
                cand = resolve_short_url(cand, ua=COMMON_UA_DESKTOP)
            raw = cand + " " + raw
        for pat in ROOM_ID_PATTERNS:
            m = re.search(pat, raw)
            if m:
                return m.group(1)
        return ""

    # ── 解析 ──
    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        ctx = ctx or {}
        room_id = self.extract_room_id(text)
        room = LiveRoom(platform="jd", room_id=room_id)
        cookies = ctx.get("cookies") or load_cookie("jd")
        raw: dict = {}
        blobs: list = []
        if not room_id:
            room.notes.append("未能从输入中解析出直播间 ID：请使用 "
                              "https://lives.jd.com/#/123456789 形式的链接，或直接粘贴分享链接。")

        # 1) 直连 m 接口（需 eid/登录态；失败属预期）
        if room_id:
            payload = jd_api_get("liveDetailToM", {"liveId": room_id}, cookies=cookies)
            if payload:
                raw["liveDetailToM"] = payload
                blobs.append(payload)

        page_url = text if str(text).startswith("http") else (
            f"https://lives.jd.com/#/{room_id}" if room_id else "https://lives.jd.com/")

        # 2) 页面 SSR / 内嵌 JSON（保底）
        html = fetch_html(page_url, cookies=cookies, ua=COMMON_UA_DESKTOP)
        if html:
            blobs.extend(iter_json_blobs(html))

        # 3) 无头浏览器拦截 XHR（主路线：eid 由页面自己生成）
        play: dict = {}
        sniff_used = False
        if ctx.get("playwright", True) and room_id:
            sniff_used = True
            sniff = sniff_page(page_url, cookies=cookies, ua=COMMON_UA_DESKTOP,
                               timeout_ms=45000, wait_ms=int(ctx.get("sniff_wait_ms", 9000)),
                               extra_headers={"Referer": "https://lives.jd.com/"},
                               response_predicate=lambda u: ("api.m.jd.com" in u))
            if sniff.get("ok"):
                bodies = [r.get("json") if isinstance(r.get("json"), (dict, list))
                          else r.get("text") for r in sniff.get("responses", [])]
                blobs.extend([b for b in bodies if b])
                play = pick_play_payload(bodies, room_id)
                if not play and sniff.get("media_srcs"):
                    play = {"urls": list(sniff["media_srcs"])}
                _t = str(sniff.get("title") or "").strip()
                if _t and _t not in ("来京东看直播", "京东直播", "京东"):
                    room.title = room.title or _t[:200]
                raw["playwright_sniff"] = {"responses": len(sniff.get("responses", [])),
                                           "media_srcs": sniff.get("media_srcs", [])[:5]}
            else:
                room.notes.append(f"浏览器嗅探不可用：{sniff.get('error', '未知原因')}"
                                  "（安装：python -m playwright install chromium）")
        if play:
            raw["play_payload"] = play

        # ── 组装档位（按 liveId 过滤后的本房间地址）──
        urls: list = []
        if play:
            for key in PLAY_KEYS:
                u = play.get(key)
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
            for u in play.get("urls") or []:
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
        if not urls:                      # 兜底：从页面/响应里粗提取（可能含推荐位，仅保底）
            urls = collect_stream_urls(html, *blobs)

        apply_streams(room, urls)
        apply_metrics(room, blobs)
        status_code = play.get("status") if isinstance(play, dict) else None
        if status_code is not None:
            room.metrics["jd_live_status"] = status_code
            room.notes.append(f"京东 live 状态码 status={status_code}（1=直播中）")
        finalize_status(room, live_hint=("2" if status_code == 1 else None))
        if room.push.get("channel") in (None, ""):
            room.push["channel"] = "京东直播（平台侧）"
        if room.push.get("version") is None:
            room.push["version"] = format_client_version({}) or ""

        if not urls:
            room.notes.append("未获取到拉流地址：京东直播接口需 POST + eid 风控指纹，"
                              "已尝试浏览器拦截 XHR；若失败，请确认已安装 chromium "
                              "（python -m playwright install chromium），或提供登录 Cookie 后重试。")
        else:
            room.notes.append("拉流地址来自浏览器拦截的 getImmediatePlayToM；"
                              "京东 CDN（*.jdcloud.com）实测无需 Referer/Cookie。")
        if not cookies:
            room.notes.append("未配置京东 Cookie：主播/关注等账号字段通常不可见"
                              "（cookies/jd.cookie 或环境变量 LST_COOKIE_JD）。")
        room.raw = raw
        return room

    # ── 地址刷新（URL 带会话标识，下播即失效）──
    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> Optional[str]:
        try:
            room = self.resolve(f"https://lives.jd.com/#/{room_id}", ctx={"playwright": True})
        except Exception:  # noqa: BLE001
            return None
        for t in list(room.tiers) + list(room.rtmp):
            if t.quality_key == quality_key and t.protocol == protocol:
                return t.url
        return None


ADAPTER = JdAdapter()
