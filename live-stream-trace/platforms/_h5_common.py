"""H5 / Playwright 通用抓取工具（供淘宝、京东等「需签名或登录」的平台适配器复用）。

为什么不放进 core：
  · core 是平台无关的“分析/报告”，本模块是“平台接入的通用手艺”；
  · 淘宝/京东这类平台没有稳定公开接口，数据要么在页面 SSR（`window.__INITIAL_STATE__`、
    `__NEXT_DATA__`、`mtopjsonp(...)`）里，要么必须用浏览器拦截 XHR（带登录态/签名）。
  · 因此这里只提供“抓取 + 提取流地址/指标”的可复用零件，具体平台规律放在各自适配器里。

设计原则：任何一步失败都不抛异常到调用方（返回空结果），由适配器降级并写入 notes。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 直播流地址（flv / m3u8 / rtmp）
_STREAM_URL_RE = re.compile(r"""(?:https?|rtmps?|rtmpt)://[^\s"'<>\\]{6,400}?""", re.I)
_STREAM_SUFFIX = (".flv", ".m3u8")
_RTMP_RE = re.compile(r"rtmps?://[^\s\"'<>\\]{4,300}", re.I)

# 直播 JSON 接口常见关键词（用于判断响应是否值得抓）
INTERESTING_KEYWORDS = ("live", "room", "stream", "play", "media", "broadcast", "rtmp", "pull")


def protocol_of(url: str) -> str:
    u = (url or "").lower()
    if u.startswith("rtmp") or u.startswith("rtmps") or u.startswith("rtmpt"):
        return "rtmp"
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".flv"):
        return "flv"
    return "other"


def quality_hint(url: str) -> tuple[str, str]:
    """从地址猜档位名与档位键（各平台命名不同，这里做通用兜底）。"""
    u = (url or "").lower()
    q = urlparse(u)
    params = dict(kv.split("=", 1) for kv in q.query.split("&") if "=" in kv)
    for key in ("definition", "quality", "qn", "hd", "level", "rate"):
        if key in params:
            return f"档位 {params[key]}", f"{key}{params[key]}"
    path = q.path.lower()
    for tag, name in (("origin", "原画"), ("orig", "原画"), ("hd", "高清"), ("sd", "标清"),
                      ("ld", "流畅"), ("flv", ""), ("hls", "")):
        if tag in path:
            return (f"{name}（{protocol_of(url).upper()}）" if name else protocol_of(url).upper()), tag
    return protocol_of(url).upper() or "流", ""


def extract_stream_urls(text: str, *, limit: int = 40) -> list[str]:
    """从任意文本（HTML / JSON / JS）中提取候选直播流地址（去重、保序）。"""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _STREAM_URL_RE.finditer(text):
        url = m.group(0).rstrip("\\").rstrip(",").replace("\\/", "/")
        if url.startswith("rtmp"):
            ok = url.count("/") >= 3
        else:
            ok = any(s in url.lower().split("?")[0] for s in _STREAM_SUFFIX)
        if ok and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _balanced(text: str, start: int) -> Optional[str]:
    """从 text[start]（应为 '{' 或 '['）起取平衡的 JSON 片段（正确处理字符串与转义）。"""
    if start >= len(text) or text[start] not in "{[":
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


_SSR_ANCHORS = (
    "window.__INITIAL_STATE__",
    "window.__NUXT__",
    "window.__PRELOADED_STATE__",
    "__NEXT_DATA__",
    "window.__DATA__",
    "window.__GLOBAL_DATA__",
    "mtopjsonp",
)


def iter_json_blobs(html: str) -> list[Any]:
    """抽取页面内嵌 JSON：SSR 全局变量 + `__NEXT_DATA__` + mtopjsonp 回调。"""
    blobs: list[Any] = []
    if not html:
        return blobs
    for anchor in _SSR_ANCHORS:
        for m in re.finditer(re.escape(anchor), html):
            seg = html[m.end():m.end() + 200]
            pm = re.search(r"[\{\[]", seg)
            if not pm:
                continue
            raw = _balanced(html, m.end() + pm.start())
            if not raw:
                continue
            try:
                blobs.append(json.loads(raw))
            except (json.JSONDecodeError, ValueError):
                logger.debug("SSR JSON 解析失败（%s）", anchor)
    # JSON-LD / 通用 script[type=application/json]
    for m in re.finditer(r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
                         html, re.S | re.I):
        try:
            blobs.append(json.loads(m.group(1).strip()))
        except (json.JSONDecodeError, ValueError):
            pass
    return blobs


def deep_find(obj: Any, keys: tuple, *, max_depth: int = 8) -> Any:
    """深度优先按 key 名（不区分大小写）查找第一个非空值。"""
    want = {k.lower() for k in keys}
    stack = [(obj, 0)]
    while stack:
        node, depth = stack.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in want and v not in (None, "", [], {}):
                    return v
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(node, list):
            for v in node[:50]:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
    return None


def sniff_page(url: str, *, cookies: str = "", timeout_ms: int = 25000,
               wait_ms: int = 6000, ua: str = "",
               extra_headers: Optional[dict] = None,
               response_predicate: Optional[Callable[[str], bool]] = None) -> dict:
    """用无头 Chromium 打开页面，抓取 HTML、内嵌 JSON、以及流地址相关的 XHR 响应体。

    返回 {'ok', 'html', 'urls', 'responses': [{url, json|text}]}；失败时 ok=False + error。
    需要 playwright（未安装则返回 ok=False，适配器据此降级）。
    """
    out: dict[str, Any] = {"ok": False, "html": "", "urls": [], "responses": [],
                           "media_srcs": [], "title": ""}
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"playwright 不可用: {exc}"
        return out
    pred = response_predicate or (lambda u: any(k in u.lower() for k in INTERESTING_KEYWORDS))
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}
    if ua:
        headers["User-Agent"] = ua
    headers.update(extra_headers or {})
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=ua or None, extra_http_headers=headers)
            if cookies:
                cookies_list = []
                for part in cookies.split(";"):
                    if "=" in part:
                        name, _, value = part.strip().partition("=")
                        cookies_list.append({"name": name, "value": value,
                                             "domain": urlparse(url).hostname or "",
                                             "path": "/"})
                if cookies_list:
                    try:
                        ctx.add_cookies(cookies_list)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("注入 Cookie 失败: %s", exc)
            page = ctx.new_page()
            captured: list[dict] = []

            def _on_response(resp):  # noqa: ANN001
                try:
                    rurl = resp.url
                    out["urls"].extend(extract_stream_urls(rurl))
                    if not pred(rurl):
                        return
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" in ctype or "text" in ctype:
                        body = resp.text()
                        if body and len(body) < 3_000_000:
                            entry: dict[str, Any] = {"url": rurl}
                            try:
                                entry["json"] = json.loads(body)
                            except (json.JSONDecodeError, ValueError):
                                entry["text"] = body
                            captured.append(entry)
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                # hash 路由 SPA（京东 lives.jd.com/#/xxx 等）常在 domcontentloaded 上超时，
                # 但页面其实已加载并发出业务 XHR → 不能因此整体失败，继续抓取。
                logger.debug("goto 超时/异常，继续抓取: %s", exc)
            page.wait_for_timeout(wait_ms)
            out["html"] = page.content()
            out["urls"].extend(extract_stream_urls(out["html"]))
            try:
                out["title"] = page.title() or ""
            except Exception:  # noqa: BLE001
                out["title"] = ""
            try:
                srcs = page.eval_on_selector_all(
                    "video,source", "els => els.map(e => e.src || e.currentSrc || '')")
                out["media_srcs"] = [s for s in srcs if s]
                out["urls"].extend(out["media_srcs"])
            except Exception:  # noqa: BLE001
                out["media_srcs"] = []
            out["responses"] = captured[:200]
            out["urls"] = list(dict.fromkeys(out["urls"]))
            browser.close()
            out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def collect_stream_urls(*sources: Any) -> list[str]:
    """把 HTML / JSON / 文本 混合来源统一提取为流地址（去重、保序）。"""
    urls: list[str] = []
    for src in sources:
        if not src:
            continue
        if isinstance(src, str):
            urls.extend(extract_stream_urls(src))
        else:
            try:
                urls.extend(extract_stream_urls(json.dumps(src, ensure_ascii=False)))
            except (TypeError, ValueError):
                continue
    return list(dict.fromkeys(urls))


def fetch_html(url: str, *, cookies: str = "", ua: str = "", timeout: int = 15,
               headers: Optional[dict] = None) -> str:
    """抓取页面 HTML（失败返回空串，不抛异常）。"""
    try:
        import requests  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    hdrs = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9"}
    if ua:
        hdrs["User-Agent"] = ua
    if cookies:
        hdrs["Cookie"] = cookies
    hdrs.update(headers or {})
    try:
        r = requests.get(url, headers=hdrs, timeout=timeout, allow_redirects=True)
        return r.text if r.status_code < 400 else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_html 失败 %s: %s", url, exc)
        return ""


def resolve_short_url(url: str, *, ua: str = "", timeout: int = 10) -> str:
    """展开短链（tb.cn / 3.cn / jd.hk 等）→ 最终地址；失败返回原值。"""
    try:
        import requests  # noqa: PLC0415
        r = requests.get(url, allow_redirects=True, timeout=timeout,
                         headers={"User-Agent": ua or "Mozilla/5.0"})
        return r.url or url
    except Exception:  # noqa: BLE001
        return url
