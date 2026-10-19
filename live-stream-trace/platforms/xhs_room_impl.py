"""小红书直播间解析：分享短链 / 主播主页 / 房间号 → 直播信息 + 拉流地址

原理（无需签名，实测路线）：
  1. 用「移动端 UA」请求直播间页面/主播主页，页面 SSR 会内嵌
     <script>window.__INITIAL_STATE__ = {...}</script>
  2. 其中 liveStream.roomData.roomInfo 含直播标题、deeplink 等；
     deeplink 的 query 里 flvUrl 形如 .../live/{room_id}.flv，取出 room_id
  3. 由 room_id 直接拼装拉流地址：
     - 源流：http://live-source-play.xhscdn.com/live/{room_id}.flv（未转码）
     - HLS ：同 ID 换 .m3u8
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import requests

from xhs_config import (
    API_TIMEOUT,
    COMMON_HEADERS,
    LIVE_PAGE_URL,
    PROFILE_PAGE_URL,
    SHORT_LINK_HOSTS,
    SOURCE_HLS_TEMPLATE,
    SOURCE_FLV_TEMPLATE,
    SSR_STATE_PATTERN,
    STREAM_HOST_CANDIDATES,
)

logger = logging.getLogger(__name__)

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
LIVESTREAM_PATH_RE = re.compile(r"/livestream/([A-Za-z0-9_-]+)")
PROFILE_PATH_RE = re.compile(r"/user/profile/([A-Za-z0-9_-]+)")


# ───────────────────────── 输入规范化 ─────────────────────────

def normalize_input(raw: str) -> dict:
    """把用户输入归类：shortlink / room_id / user_id / deeplink / url"""
    s = (raw or "").strip()
    if not s:
        raise ValueError("输入为空")
    if s.startswith("http"):
        host = urlparse(s).netloc.lower()
        if any(h in host for h in SHORT_LINK_HOSTS):
            return {"kind": "shortlink", "value": s, "url": s}
        m = LIVESTREAM_PATH_RE.search(s)
        if m:
            tail = s[m.end():].strip("/").split("?")[0].split("/")[0]
            room_part = tail or m.group(1)   # 形如 /livestream/{token}/{room_id}
            return {"kind": "room_id", "value": room_part, "url": s}
        m = PROFILE_PATH_RE.search(s)
        if m:
            return {"kind": "user_id", "value": m.group(1),
                    "url": f"{PROFILE_PAGE_URL}/{m.group(1)}"}
        q = parse_qs(urlparse(s).query)
        if "flvUrl" in q:
            return {"kind": "deeplink", "value": s, "url": s}
        return {"kind": "url", "value": s, "url": s}
    if ROOM_ID_RE.match(s):
        return {"kind": "room_id", "value": s, "url": f"{LIVE_PAGE_URL}/{s}"}
    return {"kind": "unknown", "value": s, "url": s}


# ───────────────────────── HTTP / SSR ─────────────────────────

def fetch_page(url: str, timeout: int = API_TIMEOUT, cookies: str | None = None) -> tuple[str, str]:
    """抓取页面，返回 (html, 最终URL)。"""
    headers = dict(COMMON_HEADERS)
    if cookies:
        headers["Cookie"] = cookies
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, str(resp.url)


def parse_ssr_state(html_text: str) -> dict | None:
    """提取 window.__INITIAL_STATE__ JSON。"""
    m = re.search(SSR_STATE_PATTERN, html_text, re.S)
    if not m:
        return None
    payload = m.group(1).replace("undefined", "null")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.debug("SSR 状态解析失败: %s", exc)
        return None


def extract_live_info(state: dict | None) -> dict:
    """从 SSR 状态提取直播信息。"""
    out: dict = {"live": False}
    live = (state or {}).get("liveStream")
    if not isinstance(live, dict):
        return out
    out["live_status_raw"] = live.get("liveStatus")
    if live.get("errorMessage"):
        out["error_message"] = live.get("errorMessage")
    room_data = live.get("roomData") or {}
    info = room_data.get("roomInfo") if isinstance(room_data, dict) else None
    if live.get("liveStatus") != "success" or not isinstance(info, dict) or not info:
        out["room_data_keys"] = list(room_data.keys())[:12] if isinstance(room_data, dict) else []
        return out

    deeplink = info.get("deeplink") or ""
    q = parse_qs(urlparse(deeplink).query)

    def _first(key: str) -> str:
        return (q.get(key) or [""])[0]

    flv_param = _first("flvUrl")
    room_id = ""
    if "live/" in flv_param:
        room_id = flv_param.split("live/")[1].split(".")[0]

    title = info.get("roomTitle") or ""
    out.update({
        "live": bool(room_id),
        "room_id": room_id,
        "anchor_name": _first("host_nickname"),
        "title": title,
        "is_replay": "回放" in title,
        "deeplink": deeplink,
        "flv_url_param": flv_param,
        "deeplink_params": {k: v[0] for k, v in q.items() if v},
        "raw_room_info": info,
        "pull_config": parse_pull_config(info),
        "host_id": _first("host_id"),
        "host_avatar": _first("host_avatar"),
        "viewer_count": info.get("displayViewerCount"),
        "member_count": info.get("displayMemberCount"),
        "praise_count": info.get("displayPraiseCount"),
        "room_status": info.get("status"),
    })
    return out


def parse_pull_config(room_info: dict | None) -> dict:
    """解析 roomInfo.pullConfig → 完整拉流矩阵（编码 × 线路 × 协议 × 画质）。

    pullConfig 形如:
      {"h265": [], "h264": [{"master_url": "...m3u8", "quality_type": "HD",
                            "quality_type_name": "原画"}, ...]}
    备份线路域名通常带 -bak（如 live-source-play-bak-tx.xhscdn.com）。
    """
    if not isinstance(room_info, dict):
        return {"h264": [], "h265": []}
    raw = room_info.get("pullConfig")
    cfg = {}
    if isinstance(raw, str) and raw.strip():
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            cfg = {}
    elif isinstance(raw, dict):
        cfg = raw
    out: dict = {"h264": [], "h265": []}
    for codec_key in ("h264", "h265"):
        for item in (cfg.get(codec_key) or []):
            if not isinstance(item, dict):
                continue
            url = item.get("master_url") or ""
            if not url:
                continue
            host = urlparse(url).netloc or ""
            proto = "hls" if url.endswith(".m3u8") else ("flv" if url.endswith(".flv") else "other")
            _h = host.lower()
            if _h.startswith("live-source-play."):
                line, line_label = "main", "主线"
            else:
                line = "backup"
                if "-bak-tx" in _h:
                    line_label = "备线（腾讯云）"
                elif "-hw" in _h:
                    line_label = "备线（华为云）"
                else:
                    line_label = "备线"
            out[codec_key].append({
                "url": url,
                "quality": item.get("quality_type") or "",
                "quality_name": item.get("quality_type_name") or "",
                "protocol": proto,
                "host": host,
                "line": line,
                "line_label": line_label,
                "codec": codec_key,
            })
    return out


def build_stream_urls(room_id: str, host: str | None = None) -> dict:
    """由 room_id 拼装拉流地址（源流 FLV / HLS）。"""
    template_flv = SOURCE_FLV_TEMPLATE
    template_hls = SOURCE_HLS_TEMPLATE
    if host and host != STREAM_HOST_CANDIDATES[0]:
        template_flv = f"http://{host}/live/{{room_id}}.flv"
        template_hls = f"http://{host}/live/{{room_id}}.m3u8"
    return {
        "source_flv": template_flv.format(room_id=room_id),
        "source_hls": template_hls.format(room_id=room_id),
    }


def probe_stream(url: str, timeout: int = 10) -> dict:
    """探测拉流地址是否存活（Range 请求取前若干字节，检查 FLV 魔术字节）。"""
    headers = dict(COMMON_HEADERS)
    headers["Range"] = "bytes=0-2047"
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        chunk = next(resp.iter_content(512), b"")
        result = {
            "ok": resp.status_code in (200, 206),
            "status": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "server": resp.headers.get("Server", ""),
            "magic": chunk[:3].decode("latin1", "replace"),
            "bytes_received": len(chunk),
            "cdn_hint": resp.headers.get("Via", "") or resp.headers.get("X-Cache", ""),
        }
        resp.close()
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def resolve_room(raw_input: str, cookies: str | None = None,
                 use_playwright: bool = False) -> dict:
    """主入口：解析直播间 → 直播信息 + 拉流地址。

    返回 dict：
      ok / input_kind / source / page_url / live / room_id / anchor_name / title
      / stream_urls / probe / live_info / error
    """
    meta = normalize_input(raw_input)
    result: dict = {
        "ok": False,
        "input": raw_input,
        "input_kind": meta["kind"],
        "source": "ssr",
    }
    url = meta["url"]
    try:
        html, final_url = fetch_page(url, cookies=cookies)
        state = parse_ssr_state(html)
        if state is None and use_playwright:
            from xhs_auth_playwright import fetch_page_state
            state, final_url = fetch_page_state(url, cookies=cookies)
            result["source"] = "playwright"
        if state is None:
            raise RuntimeError("页面未包含 __INITIAL_STATE__（可能被风控或需登录，试加 --playwright / --cookies）")

        live = extract_live_info(state)
        result.update(live)
        result["page_url"] = final_url
        result["ok"] = True

        if live.get("live") and live.get("room_id"):
            room_id = live["room_id"]
            urls = build_stream_urls(room_id)
            # 优先采用 SSR pullConfig 中的官方地址（含备份线路）
            pc = live.get("pull_config") or {}
            for codec_key in ("h264", "h265"):
                entries = pc.get(codec_key) or []
                for e in entries:
                    if e["protocol"] in ("flv", "hls"):
                        urls[f"{codec_key}_{e['protocol']}_{e['line']}"] = e["url"]
            probes = {name: probe_stream(u) for name, u in urls.items()}
            # 备选域名回退：首选域名不可用时尝试其它候选
            if not probes.get("source_flv", {}).get("ok"):
                for host in STREAM_HOST_CANDIDATES[1:]:
                    alt = build_stream_urls(room_id, host)
                    alt_probe = probe_stream(alt["source_flv"])
                    if alt_probe.get("ok"):
                        urls, probes = alt, {"source_flv": alt_probe, "source_hls": probe_stream(alt["source_hls"])}
                        result["host_fallback"] = host
                        break
            result["stream_urls"] = urls
            result["stream_probe"] = probes
            result["stream_alive"] = bool(probes.get("source_flv", {}).get("ok"))
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="小红书直播间解析（短链 / 主播主页 / 直播间号）")
    ap.add_argument("input", help="直播间号 / livestream URL / 主播主页 URL / xhslink 分享短链")
    ap.add_argument("--playwright", action="store_true", help="SSR 缺失时用无头 Chromium 兜底抓取")
    ap.add_argument("--cookies", help="Cookie 字符串（风控/登录场景）")
    args = ap.parse_args()
    res = resolve_room(args.input, cookies=args.cookies, use_playwright=args.playwright)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if res.get("ok") else 1)
