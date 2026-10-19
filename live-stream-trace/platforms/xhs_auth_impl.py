"""小红书直播间鉴权/抓取兜底：无头 Chromium 打开页面并取回 SSR 状态。

当纯 HTTP 路线拿不到 window.__INITIAL_STATE__（风控/登录墙/动态渲染）时使用。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

SSR_STATE_PATTERN = r"<script>window\.__INITIAL_STATE__=(.*?)</script>"


def _check_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise ImportError("playwright 未安装。请执行: pip install playwright && playwright install chromium")


def fetch_page_state(url: str, cookies: str | None = None,
                     headless: bool = True, timeout: int = 30000) -> tuple[Optional[dict], str]:
    """用浏览器打开页面，返回 (SSR 状态 dict 或 None, 最终 URL)。"""
    sync_playwright = _check_playwright()
    state = None
    final_url = url
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            viewport={"width": 430, "height": 932},
        )
        if cookies:
            cookie_list = []
            for pair in cookies.split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    cookie_list.append({"name": k.strip(), "value": v.strip(),
                                        "domain": ".xiaohongshu.com", "path": "/"})
            if cookie_list:
                context.add_cookies(cookie_list)
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            final_url = page.url
            html = page.content()
            m = re.search(SSR_STATE_PATTERN, html, re.S)
            if m:
                try:
                    state = json.loads(m.group(1).replace("undefined", "null"))
                except json.JSONDecodeError as exc:
                    logger.debug("SSR 状态解析失败: %s", exc)
            if state is None:
                # 页面可能动态注入，尝试从 window 对象直接读
                state = page.evaluate("() => window.__INITIAL_STATE__ || null")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playwright 抓取失败: %s", exc)
        finally:
            context.close()
            browser.close()
    return state, final_url


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.xiaohongshu.com/livestream"
    st, fu = fetch_page_state(target)
    print("最终URL:", fu)
    print("状态块:", "有" if st else "无")
    if st:
        print("liveStream:", json.dumps(st.get("liveStream"), ensure_ascii=False)[:300])
