"""ranklist/audience 原始探针。

当前能力边界：该端点依赖 internal_room_id、anchor_id、cookies，且在已结束房间
或上下文不完整时可能返回空体。首版将其视为受限能力，失败时结构化返回。
"""

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
    OUTPUT_DIR,
    RANKLIST_AUDIENCE_URLS,
    WEB_ROOM_ID,
    ensure_dirs,
)
from network import http_get
from probe_common import make_probe_result, probe_output_path, save_probe_json

logger = logging.getLogger(__name__)


def build_probe_headers(web_room_id: str, cookies: str) -> dict[str, str]:
    """构造 ranklist/audience 请求头。"""
    headers = dict(COMMON_HEADERS)
    headers["Referer"] = f"https://live.douyin.com/{web_room_id}"
    headers["Cookie"] = cookies
    return headers


def _base_params(
    room_id: str,
    anchor_id: str,
    sec_anchor_id: str | None = None,
    ms_token: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "131.0.0.0",
        "cookie_enabled": "true",
        "screen_width": "1280",
        "screen_height": "720",
        "room_id": room_id,
        "anchor_id": anchor_id,
        "rank_type": "30",
        "webcast_sdk_version": "2450",
        "ignoreToast": "true",
    }
    if sec_anchor_id:
        params["sec_anchor_id"] = sec_anchor_id
    if ms_token:
        params["msToken"] = ms_token
    return params


def run_ranklist_probe(
    web_room_id: str,
    internal_room_id: str | None = None,
    cookies: str | None = None,
    anchor_id: str | None = None,
    sec_anchor_id: str | None = None,
    ms_token: str | None = None,
    output_dir: Path | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """抓取 ranklist/audience 原始 JSON 并落盘。

    注意：该端点通常需要直播中房间、可用主播 ID，以及较完整的 Web 请求上下文。
    """
    ensure_dirs()
    output_dir = output_dir or OUTPUT_DIR
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")

    if not internal_room_id:
        return {
            "success": False,
            "data": None,
            "files": [],
            "error": "ranklist/audience 需要 internal_room_id，当前未提供",
        }
    if not cookies:
        return {
            "success": False,
            "data": None,
            "files": [],
            "error": "ranklist/audience 需要 cookies，当前未提供",
        }
    if not anchor_id:
        return {
            "success": False,
            "data": None,
            "files": [],
            "error": "ranklist/audience 需要 anchor_id，当前未提供",
        }

    headers = build_probe_headers(web_room_id, cookies)
    params = _base_params(internal_room_id, anchor_id, sec_anchor_id=sec_anchor_id, ms_token=ms_token)
    context = internal_room_id

    last_error = ""
    for url in RANKLIST_AUDIENCE_URLS:
        logger.info("ranklist/audience probe 请求: %s", url)
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
            logger.warning("ranklist/audience 解析 JSON 失败 (%s): %s", url, exc)
            continue

        payload = make_probe_result(
            "ranklist_probe",
            context,
            raw,
            ts=ts,
            ok=True,
            url_used=url,
            extra={
                "web_room_id": web_room_id,
                "internal_room_id": internal_room_id,
                "anchor_id": anchor_id,
                "sec_anchor_id": sec_anchor_id,
                "request": {
                    "params": params,
                    "cookies_source": "required",
                },
            },
        )
        output_path = probe_output_path("ranklist_probe", context, ts)
        actual_path = output_path if output_dir == OUTPUT_DIR else output_dir / output_path.name
        save_probe_json(actual_path, payload)
        return {
            "success": True,
            "data": payload,
            "files": [str(actual_path)],
            "error": None,
        }

    payload = make_probe_result(
        "ranklist_probe",
        context,
        None,
        ts=ts,
        ok=False,
        error=last_error or "ranklist/audience 所有域名请求失败",
        extra={
            "web_room_id": web_room_id,
            "internal_room_id": internal_room_id,
            "anchor_id": anchor_id,
            "sec_anchor_id": sec_anchor_id,
            "request": {
                "params": params,
                "cookies_source": "required",
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
    parser = argparse.ArgumentParser(description="抓取 ranklist/audience 原始 JSON（受主播上下文/房间状态限制）")
    parser.add_argument("--room", default=WEB_ROOM_ID, help="Web 房间号")
    parser.add_argument("--internal-id", help="内部 room_id（ranklist 必需）")
    parser.add_argument("--anchor-id", help="主播用户 ID（ranklist 必需；当前自动提取不保证所有房间可用）")
    parser.add_argument("--sec-anchor-id", help="主播 sec_anchor_id（可选）")
    parser.add_argument("--ms-token", help="可选 msToken")
    parser.add_argument("--cookies", help="登录态 Cookie 字符串（ranklist 必需，建议直播中房间使用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run_ranklist_probe(
        web_room_id=args.room,
        internal_room_id=args.internal_id,
        cookies=args.cookies,
        anchor_id=args.anchor_id,
        sec_anchor_id=args.sec_anchor_id,
        ms_token=args.ms_token,
    )
    if result["success"]:
        print(f"[OK] ranklist_probe: {result['files'][0]}")
        return 0

    print(f"[FAIL] ranklist_probe: {result['error']}（可能因房间已结束、缺主播上下文或端点未返回有效 JSON）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
