"""小红书平台常量（供 platforms/xhs_*_impl.py 使用；由适配器内部依赖）。"""
from config import (BASE_DIR, API_TIMEOUT, OUTPUT_DIR, REPORT_DIR, COMMON_HEADERS,  # noqa: F401
                   ensure_dirs)

MOBILE_UA = "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))"
DESKTOP_UA = COMMON_HEADERS.get("User-Agent")

XHS_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://app.xhs.cn/",
    "xy-common-params": "platform=iOS&sid=session.1722166379345546829388",
}
COMMON_HEADERS = XHS_HEADERS          # xhs_*_impl.py 里的 COMMON_HEADERS 语义

LIVE_PAGE_URL = "https://www.xiaohongshu.com/livestream"
PROFILE_PAGE_URL = "https://www.xiaohongshu.com/user/profile"
SHORT_LINK_HOSTS = ("xhslink.com", "xhslink.cn")
SSR_STATE_PATTERN = r"<script>window\.__INITIAL_STATE__=(.*?)</script>"

SOURCE_HOST = "live-source-play.xhscdn.com"
SOURCE_FLV_TEMPLATE = "http://" + SOURCE_HOST + "/live/{room_id}.flv"
SOURCE_HLS_TEMPLATE = "http://" + SOURCE_HOST + "/live/{room_id}.m3u8"
STREAM_HOST_CANDIDATES = ["live-source-play.xhscdn.com", "live-play.xhscdn.com"]

def get_extra_params(scope=None):
    return {}
