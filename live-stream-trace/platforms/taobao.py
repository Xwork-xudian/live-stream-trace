"""淘宝直播适配器（可用实现：三条数据路线 + 优雅降级）。

数据路线（按优先级）：
  1. mtop h5 接口 `mtop.taobao.media.live.room.info`：官方 H5 接口，需 `_m_h5_tk` 签名
     （h5 token 算法：sign = md5(token & t & appKey & data)，token 取 _m_h5_tk 的 `_` 前段）。
     第一次请求通常返回 FAIL_SYS_TOKEN_EMPTY 并下发 _m_h5_tk，第二次带 sign 才成功。
  2. 直播间页面 SSR / 内嵌 JSON（`window.__INITIAL_STATE__`、`mtopjsonp(...)`、JSON-LD）。
  3. 无头 Chromium 打开直播间并拦截 XHR（带登录态 Cookie，见 cookies/taobao.cookie）。

各步失败都不影响整体：拿不到流地址时会写入 notes 说明原因与下一步，绝不抛异常。

直播流 CDN 多为阿里云（alicdn / pull-live 等），档位常见于 URL 后缀或 definition 参数。
开播渠道：淘宝直播伴侣（平台侧）或第三方 OBS 推流。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

from base import LiveRoom, PlatformAdapter, PlatformMeta, format_client_version
from config import COMMON_UA_DESKTOP, COMMON_UA_IOS, load_cookie
from _h5_common import (collect_stream_urls, fetch_html, iter_json_blobs,
                        resolve_short_url, sniff_page)
from _h5_room import apply_metrics, apply_streams, finalize_status

logger = logging.getLogger(__name__)

APP_KEY = "12574478"                     # 淘宝 H5 mtop 通用 appKey
MTOP_ROOM_API = "mtop.taobao.media.live.room.info"
ROOM_ID_PATTERNS = (
    r"[?&]roomId=(\d+)",
    r"[?&]room_id=(\d+)",
    r"[?&]liveId=(\d+)",
    r"tbzb\.taobao\.com/(?:live/)?(\d{5,})",
    r"/live/room/(\d{5,})",
    r"/(\d{8,})(?:[/?#]|$)",
)


def mtop_sign(token: str, t: str, app_key: str, data: str) -> str:
    """mtop h5 签名：md5(token&t&appKey&data)。"""
    return hashlib.md5(f"{token}&{t}&{app_key}&{data}".encode("utf-8")).hexdigest()


def _cookie_jar(cookies: str) -> dict:
    jar: dict = {}
    for part in (cookies or "").split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = value
    return jar


def mtop_h5_get(api: str, data: dict, *, version: str = "1.0", cookies: str = "",
                app_key: str = APP_KEY, timeout: int = 12) -> dict:
    """调用 mtop h5 接口（自动处理 token 两次握手）。失败返回 {}。"""
    try:
        import requests  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    url = f"https://acs.m.taobao.com/h5/{api}/{version}/"
    data_str = json.dumps(data or {}, separators=(",", ":"), ensure_ascii=False)
    jar = _cookie_jar(cookies)
    session = requests.Session()
    session.headers.update({
        "User-Agent": COMMON_UA_DESKTOP,
        "Referer": "https://tbzb.taobao.com/",
        "Accept": "application/json",
    })
    if jar:
        try:
            session.cookies.update(jar)
        except Exception:  # noqa: BLE001
            pass
    payload: dict = {}
    for attempt in (1, 2):
        t = str(int(time.time() * 1000))
        token = (jar.get("_m_h5_tk") or "").split("_")[0]
        params = {
            "jsv": "2.5.1", "appKey": app_key, "t": t,
            "sign": mtop_sign(token, t, app_key, data_str) if token else "",
            "api": api, "v": version, "type": "originaljson", "dataType": "json",
            "data": data_str,
        }
        try:
            r = session.get(url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mtop 请求失败: %s", exc)
            return {}
        for name in ("_m_h5_tk", "_m_h5_tk_enc"):
            val = session.cookies.get(name)
            if val:
                jar[name] = val
        try:
            payload = r.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        ret = " ".join(payload.get("ret") or [])
        if "SUCCESS" in ret.upper():
            return payload
        if "TOKEN" in ret.upper() and attempt == 1:
            continue                      # 第一次拿 token，第二次带签名重试
        logger.debug("mtop 返回非成功: %s", ret[:120])
        return payload
    return payload


class TaobaoAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="taobao", name="淘宝直播", color="#FF5000",
        live_url="https://market.m.taobao.com/app/tb-chat/live-room/index.html?roomId={room_id}",
        cta="前往淘宝直播",
    )

    # ── 识别 ──
    def matches(self, text: str) -> bool:
        t = (text or "").lower()
        if not t:
            return False
        markers = ("taobao.com", "tbzb", "taobao.live", "tb.cn/", "m.tb.cn", "taobaolive")
        return any(k in t for k in markers)

    def extract_room_id(self, text: str) -> str:
        raw = text or ""
        # 分享文案里短链可能在任意位置 → 先抽取再展开
        m_url = re.search(r"https?://[^\s\"'<>）)】|]+", raw)
        if m_url:
            cand = m_url.group(0)
            if re.search(r"(tb\.cn|m\.tb\.cn|dwz)", cand, re.I):
                cand = resolve_short_url(cand, ua=COMMON_UA_DESKTOP)
            expanded = cand + " " + raw
        else:
            expanded = raw
        for pat in ROOM_ID_PATTERNS:
            m = re.search(pat, expanded)
            if m:
                return m.group(1)
        return ""

    # ── 解析 ──
    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        ctx = ctx or {}
        room_id = self.extract_room_id(text)
        room = LiveRoom(platform="taobao", room_id=room_id)
        cookies = ctx.get("cookies") or load_cookie("taobao")
        raw: dict = {}
        blobs: list = []
        if not room_id:
            room.notes.append("未能从输入中解析出 roomId：请使用形如 "
                              "https://tbzb.taobao.com/?roomId=123456789 的链接，"
                              "或用 --platform taobao 显式指定。")

        # 1) mtop 接口（官方路线；无登录态时可能返回鉴权失败，属预期）
        if room_id:
            payload = mtop_h5_get(MTOP_ROOM_API, {"roomId": room_id}, cookies=cookies)
            if payload:
                raw["mtop_room_info"] = payload
                blobs.append(payload)

        # 2) 页面 SSR / 内嵌 JSON
        page_url = text if str(text).startswith("http") else (
            f"https://tbzb.taobao.com/?roomId={room_id}" if room_id else "https://tbzb.taobao.com/")
        html = fetch_html(page_url, cookies=cookies, ua=COMMON_UA_DESKTOP)
        if html:
            blobs.extend(iter_json_blobs(html))

        urls = collect_stream_urls(html, *blobs)

        # 3) 无头 Chromium 拦截 XHR（仍未拿到流地址时）
        sniff_used = False
        if not urls and ctx.get("playwright", True):
            sniff_used = True
            sniff = sniff_page(page_url, cookies=cookies, ua=COMMON_UA_DESKTOP,
                               extra_headers={"Referer": "https://tbzb.taobao.com/"})
            if sniff.get("ok"):
                bodies = [(r.get("json") or r.get("text")) for r in sniff.get("responses", [])]
                blobs.extend([b for b in bodies if b])
                urls = collect_stream_urls(sniff.get("html"), *bodies, sniff.get("urls"))
                raw["playwright_sniff"] = {"responses": len(sniff.get("responses", [])),
                                           "urls": sniff.get("urls", [])[:20]}
            else:
                room.notes.append(f"浏览器嗅探不可用：{sniff.get('error', '未知原因')}"
                                  "（安装：python -m playwright install chromium）")

        apply_streams(room, urls)
        apply_metrics(room, blobs)
        if not room.push.get("channel"):
            room.push["channel"] = "淘宝直播（平台侧）"
        if room.push.get("version") is None:
            room.push["version"] = format_client_version({}) or ""
        finalize_status(room)

        if not urls:
            room.notes.append("未获取到拉流地址：淘宝直播间多数需要登录态 Cookie（cookies/taobao.cookie）"
                              "且 H5 接口带签名；已尝试 mtop 接口 + 页面 SSR"
                              + ("+ 浏览器拦截 XHR" if sniff_used else "")
                              + "。建议配置登录 Cookie 后重试，或用 --no-playwright 排查网络。")
        if not cookies:
            room.notes.append("未配置淘宝 Cookie：粉丝/关注等账号字段与部分房间信息不可见"
                              "（cookies/taobao.cookie 或环境变量 LST_COOKIE_TAOBAO）。")
        room.raw = raw
        return room

    # ── 地址刷新（URL 常带短时签名）──
    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> Optional[str]:
        try:
            room = self.resolve(f"https://tbzb.taobao.com/?roomId={room_id}",
                                ctx={"playwright": False})
        except Exception:  # noqa: BLE001
            return None
        pool = list(room.tiers) + list(room.rtmp)
        for t in pool:
            if t.quality_key == quality_key and t.protocol == protocol:
                return t.url
        return None


ADAPTER = TaobaoAdapter()
