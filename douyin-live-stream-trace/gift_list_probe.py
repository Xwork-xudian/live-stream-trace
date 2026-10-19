"""gift/list 原始探针。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    API_TIMEOUT,
    COMMON_HEADERS,
    GIFT_LIST_URLS,
    OUTPUT_DIR,
    WEB_ROOM_ID,
    ensure_dirs,
)
from network import http_get
from probe_common import make_probe_result, probe_output_path, save_probe_json

logger = logging.getLogger(__name__)


def build_probe_headers(web_room_id: str, cookies: str | None = None) -> dict[str, str]:
    """构造 gift/list 请求头。"""
    headers = dict(COMMON_HEADERS)
    headers["Referer"] = f"https://live.douyin.com/{web_room_id}"
    if cookies:
        headers["Cookie"] = cookies
    return headers


def _base_params(room_id: str) -> dict[str, Any]:
    return {
        "aid": "6383",
        "app_name": "douyin_web",
        "device_platform": "web",
        "room_id": room_id,
        "webcast_sdk_version": "2450",
    }


def run_gift_list_probe(
    web_room_id: str,
    internal_room_id: str | None = None,
    cookies: str | None = None,
    output_dir: Path | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """抓取 gift/list 原始 JSON 并落盘。"""
    ensure_dirs()
    output_dir = output_dir or OUTPUT_DIR
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")

    if not internal_room_id:
        return {
            "success": False,
            "data": None,
            "files": [],
            "error": "gift/list 需要 internal_room_id，当前未提供",
        }

    headers = build_probe_headers(web_room_id, cookies)
    params = _base_params(internal_room_id)
    context = internal_room_id or web_room_id
    cookies_source = "provided" if cookies else "not_required"

    last_error = ""
    for url in GIFT_LIST_URLS:
        logger.info("gift/list probe 请求: %s", url)
        response = http_get(
            url,
            params=params,
            headers=headers,
            timeout=API_TIMEOUT,
            retries=2,
        )
        if response is None:
            last_error = f"请求失败: {url}"
            continue

        try:
            raw = response.json()
        except Exception as exc:
            last_error = f"响应非 JSON: {exc}"
            logger.warning("gift/list 解析 JSON 失败 (%s): %s", url, exc)
            continue

        payload = make_probe_result(
            "gift_list_probe",
            context,
            raw,
            ts=ts,
            ok=True,
            url_used=url,
            extra={
                "web_room_id": web_room_id,
                "internal_room_id": internal_room_id,
                "request": {
                    "params": params,
                    "cookies_source": cookies_source,
                },
            },
        )
        output_path = probe_output_path("gift_list_probe", context, ts)
        save_probe_json(output_path if output_dir == OUTPUT_DIR else output_dir / output_path.name, payload)
        return {
            "success": True,
            "data": payload,
            "files": [str(output_path if output_dir == OUTPUT_DIR else output_dir / output_path.name)],
            "error": None,
        }

    payload = make_probe_result(
        "gift_list_probe",
        context,
        None,
        ts=ts,
        ok=False,
        error=last_error or "gift/list 所有域名请求失败",
        extra={
            "web_room_id": web_room_id,
            "internal_room_id": internal_room_id,
            "request": {
                "params": params,
                "cookies_source": cookies_source,
            },
        },
    )
    return {
        "success": False,
        "data": payload,
        "files": [],
        "error": payload.get("error"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 gift/list 原始 JSON")
    parser.add_argument("--room", default=WEB_ROOM_ID, help="Web 房间号")
    parser.add_argument("--internal-id", help="内部 room_id（gift/list 必需）")
    parser.add_argument("--cookies", help="可选 Cookie 字符串（gift/list 默认无需）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run_gift_list_probe(
        web_room_id=args.room,
        internal_room_id=args.internal_id,
        cookies=args.cookies,
    )
    if result["success"]:
        print(f"[OK] gift_list_probe: {result['files'][0]}")
        return 0

    print(f"[FAIL] gift_list_probe: {result['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
