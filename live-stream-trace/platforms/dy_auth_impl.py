"""
Playwright 浏览器鉴权模块。

通过无头 Chromium 访问抖音直播间，拦截 web/enter API 响应，
提取 cookies 和 msToken，自动处理 a_bogus 签名。

使用方式:
    from auth_playwright import authenticate_room
    ctx = authenticate_room("376389454958")
    if ctx["ok"]:
        print(ctx["cookies"])         # cookie 字符串
        print(ctx["stream_url_obj"])   # 流地址结构
        print(ctx["room"])             # 房间信息
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PAGE_LOAD_TIMEOUT = 30
RESPONSE_WAIT_TIMEOUT = 20
ENTER_API_PATH = "/webcast/room/web/enter/"
# 短链跳转后的移动端直播页（webcast.amemv.com/.../reflow/<room_id>）调用的是 reflow 接口
REFLOW_API_PATH = "/webcast/room/reflow/info/"
API_PATHS = (ENTER_API_PATH, REFLOW_API_PATH)


def _check_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise ImportError(
            "playwright 未安装。请执行: pip install playwright && playwright install chromium"
        )


def _extract_ms_token_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    tokens = params.get("msToken", [])
    return tokens[0] if tokens else None


def _cookies_to_string(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def save_playwright_artifact(path: Path, payload: Any) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _parse_enter_response(data: dict) -> dict[str, Any]:
    result = {
        "room": None,
        "room_id": None,
        "stream_url_obj": None,
        "selected_url": None,
        "room_status": None,
    }

    try:
        # 兼容两种接口形态：web/enter → data.data[0]；reflow/info → data.room
        payload = data.get("data") if isinstance(data, dict) else None
        room = None
        if isinstance(payload, dict):
            inner = payload.get("data")
            if isinstance(inner, list) and inner:
                room = inner[0]
            elif isinstance(payload.get("room"), dict):
                room = payload["room"]
            elif isinstance(payload.get("rooms"), list) and payload["rooms"]:
                room = payload["rooms"][0]
        if not isinstance(room, dict):
            logger.warning("接口响应中没有房间对象: keys=%s",
                           list(payload.keys())[:8] if isinstance(payload, dict) else type(payload).__name__)
            return result
        result["room"] = room
        result["room_id"] = room.get("id_str") or str(room.get("id", ""))
        result["room_status"] = room.get("status")

        if room.get("status") == 2:
            stream_url = room.get("stream_url")
            if isinstance(stream_url, dict):
                result["stream_url_obj"] = stream_url
                flv_pull_url = stream_url.get("flv_pull_url") or stream_url.get("flv_pull_url_params")
                if isinstance(flv_pull_url, dict):
                    for key in ("FULL_HD1", "HD1", "SD1", "SD2"):
                        if key in flv_pull_url:
                            result["selected_url"] = flv_pull_url[key]
                            break
                    if not result["selected_url"]:
                        first = next(iter(flv_pull_url.values()), None)
                        if first:
                            result["selected_url"] = first
                elif isinstance(flv_pull_url, str):
                    result["selected_url"] = flv_pull_url
    except (KeyError, TypeError, IndexError) as exc:
        logger.warning("解析 web/enter 响应失败: %s", exc)

    return result


def authenticate_room(
    web_room_id: str,
    headless: bool = True,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    response_timeout: int = RESPONSE_WAIT_TIMEOUT,
    page_url: Optional[str] = None,
) -> dict[str, Any]:
    """
    通过 Playwright 浏览器自动化获取直播间鉴权上下文。

    打开无头 Chromium → 访问直播间 → 拦截 web/enter 响应 → 提取 cookies/msToken

    Args:
        web_room_id: Web 房间号 (如 "376389454958")
        headless: 无头模式 (默认 True)
        page_timeout: 页面加载超时 (秒)
        response_timeout: 等待 web/enter 响应超时 (秒)

    Returns:
        {
            ok: bool,
            cookies: str | None,         # cookie 字符串 (含 httpOnly)
            ms_token: str | None,        # msToken
            room: dict | None,           # 房间信息
            room_id: str | None,         # 内部 room_id
            internal_room_id: str | None,
            stream_url_obj: dict | None, # 流地址结构
            selected_url: str | None,    # 最佳流地址
            room_status: int | None,     # 2=直播中, 4=已结束
            enter_response: dict | None, # 原始 web/enter 响应
            error: str | None,
        }
    """
    sync_playwright = _check_playwright()

    enter_response_data: Optional[dict] = None
    ms_token: Optional[str] = None
    cookie_string: Optional[str] = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()

            def on_response(response):
                nonlocal enter_response_data, ms_token
                url = response.url

                if not ms_token and "msToken=" in url:
                    extracted = _extract_ms_token_from_url(url)
                    if extracted and len(extracted) > 10:
                        ms_token = extracted
                        logger.debug("从请求中提取 msToken: %s…", ms_token[:20])

                if any(p in url for p in API_PATHS) and response.status == 200:
                    try:
                        body = response.json()
                        if isinstance(body, dict) and body.get("status_code") == 0:
                            enter_response_data = body
                            logger.info("成功拦截直播接口响应: %s", url.split("?")[0][-40:])
                        else:
                            logger.warning("web/enter 返回 status_code=%s", body.get("status_code") if isinstance(body, dict) else "?")
                    except Exception as exc:
                        logger.warning("解析 web/enter 响应体失败: %s", exc)

            page.on("response", on_response)

            # 分享短链跳转后是 webcast.amemv.com/.../reflow/<room_id>，直接打开该页最稳
            live_url = page_url or f"https://live.douyin.com/{web_room_id}"
            logger.info("正在打开直播间: %s", live_url)

            try:
                page.goto(live_url, wait_until="domcontentloaded", timeout=page_timeout * 1000)
            except Exception as exc:
                logger.warning("页面加载异常 (web/enter 可能已触发): %s", exc)

            if not enter_response_data:
                logger.info("等待 web/enter 响应 (最多 %ss)…", response_timeout)
                deadline = time.monotonic() + response_timeout
                while time.monotonic() < deadline and not enter_response_data:
                    page.wait_for_timeout(500)

            cookies_list = context.cookies()
            cookie_string = _cookies_to_string(cookies_list)
            logger.info("提取到 %d 个 cookies", len(cookies_list))

            if not ms_token:
                for c in cookies_list:
                    if c["name"] == "msToken":
                        ms_token = c["value"]
                        break

            browser.close()

    except ImportError:
        raise
    except Exception as exc:
        logger.exception("Playwright 执行异常")
        return {
            "ok": False,
            "cookies": cookie_string,
            "ms_token": ms_token,
            "room": None,
            "room_id": None,
            "internal_room_id": None,
            "stream_url_obj": None,
            "selected_url": None,
            "room_status": None,
            "enter_response": None,
            "error": f"Playwright 执行失败: {exc}",
        }

    result: dict[str, Any] = {
        "ok": False,
        "cookies": cookie_string,
        "ms_token": ms_token,
        "room": None,
        "room_id": None,
        "internal_room_id": None,
        "stream_url_obj": None,
        "selected_url": None,
        "room_status": None,
        "enter_response": None,
        "error": None,
    }

    if enter_response_data:
        result["enter_response"] = enter_response_data
        parsed = _parse_enter_response(enter_response_data)
        result["room"] = parsed["room"]
        result["room_id"] = parsed["room_id"]
        result["internal_room_id"] = parsed["room_id"]
        result["stream_url_obj"] = parsed["stream_url_obj"]
        result["selected_url"] = parsed["selected_url"]
        result["room_status"] = parsed["room_status"]
        result["ok"] = True
    elif cookie_string:
        result["ok"] = True
        result["error"] = "未拦截到 web/enter 响应，仅获取到 cookies"
    else:
        result["error"] = "未拦截到 web/enter 响应且未获取到 cookies"

    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    room_id = sys.argv[1] if len(sys.argv) > 1 else "376389454958"
    ctx = authenticate_room(room_id)

    print(f"\n{'='*50}")
    print(f"鉴权结果: {'成功' if ctx['ok'] else '失败'}")
    if ctx.get("error"):
        print(f"错误: {ctx['error']}")
    if ctx.get("cookies"):
        print(f"Cookies: {ctx['cookies'][:80]}…")
    if ctx.get("ms_token"):
        print(f"msToken: {ctx['ms_token'][:30]}…")
    if ctx.get("room_status") is not None:
        status_map = {2: "直播中", 4: "已结束"}
        print(f"房间状态: {status_map.get(ctx['room_status'], ctx['room_status'])}")
    if ctx.get("internal_room_id"):
        print(f"内部 room_id: {ctx['internal_room_id']}")
    if ctx.get("selected_url"):
        print(f"流地址: {ctx['selected_url'][:80]}…")
