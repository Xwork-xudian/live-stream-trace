"""小红书直播流分析 — 共享配置

与抖音版相比，小红书的核心差异：
  1. 直播间信息通过页面 SSR 状态 window.__INITIAL_STATE__ 下发（无需复杂签名）；
  2. 页面必须使用「移动端 UA」才返回含 liveStream 的状态（PC 站在未登录时只给登录墙）；
  3. 拉流地址由 room_id 直接拼装：live-source-play.xhscdn.com/live/{room_id}.flv 即为**源流**（未转码）。
"""
import logging
import os
import sys
from pathlib import Path

from mappings_loader import load_json_mapping

logger = logging.getLogger(__name__)


def _app_dir() -> Path:
    """获取应用程序所在目录（兼容 PyInstaller 打包）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# === 直播间配置 ===
ROOM_ID = os.getenv("XHS_ROOM_ID", "")                     # 直播房间 ID（源流路径中的 ID）
USER_ID = os.getenv("XHS_USER_ID", "")                     # 主播 user_id
WEB_ROOM_ID = ROOM_ID
LIVE_URL = f"https://www.xiaohongshu.com/livestream/{ROOM_ID}" if ROOM_ID else ""

# === 目录配置 ===
BASE_DIR = _app_dir()
OUTPUT_DIR = BASE_DIR / "captures"
REPORT_DIR = BASE_DIR / "reports"
DUAL_STREAM_SAMPLE_DIR = OUTPUT_DIR / "dual_stream_samples"
DUAL_STREAM_MANIFEST_DIR = OUTPUT_DIR / "dual_stream_manifests"

# === HTTP 配置 ===
API_TIMEOUT = 15
POLL_INTERVAL = 30
DOWNLOAD_MAX_BYTES = 1024 * 1024
CAPTURE_SECONDS = 5

# 小红书 Web/SSR 需要移动端 UA（参考社区实现）
MOBILE_UA = "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))"
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COMMON_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://app.xhs.cn/",
    "xy-common-params": "platform=iOS&sid=session.1722166379345546829388",
}

# === 登录 Cookie（默认从 .xhs_cookie 读取，可用环境变量 XHS_COOKIE 覆盖）===
def _load_default_cookie() -> str:
    env = os.getenv("XHS_COOKIE")
    if env and env.strip():
        return env.strip()
    cookie_file = BASE_DIR / ".xhs_cookie"
    try:
        if cookie_file.exists():
            text = cookie_file.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError as exc:
        logger.warning("读取 .xhs_cookie 失败: %s", exc)
    return ""


DEFAULT_COOKIE = _load_default_cookie()

# === 页面与解析 ===
LIVE_PAGE_URL = "https://www.xiaohongshu.com/livestream"          # /livestream/{room_id}
PROFILE_PAGE_URL = "https://www.xiaohongshu.com/user/profile"     # /user/profile/{user_id}
SHORT_LINK_HOSTS = ("xhslink.com", "xhslink.cn")
SSR_STATE_PATTERN = r"<script>window\.__INITIAL_STATE__=(.*?)</script>"

# === 拉流地址模板（live-source-play 即源流/未转码；同 ID 换后缀得到 HLS）===
SOURCE_HOST = "live-source-play.xhscdn.com"
SOURCE_FLV_TEMPLATE = "http://" + SOURCE_HOST + "/live/{room_id}.flv"
SOURCE_HLS_TEMPLATE = "http://" + SOURCE_HOST + "/live/{room_id}.m3u8"
# 备选拉流域名（不同房间/地区可能下发不同域名，探测时依次尝试）
STREAM_HOST_CANDIDATES = ["live-source-play.xhscdn.com", "live-play.xhscdn.com"]

# 兼容抖音版模块的接口名（XHS 版不调用 douyin 接口，仅保证导入可用）
ENTER_URLS: list[str] = []

# === 推流软件检测模式 ===
SOFTWARE_PATTERNS = [
    (item["pattern"], item["name"])
    for item in load_json_mapping("software_patterns.json")
]

# === CDN 识别关键词 ===
CDN_PATTERNS = [
    (item["keyword"], item["name"])
    for item in load_json_mapping("cdn_patterns.json")
]

# === 额外流参数 ===
EXTRA_STREAM_PARAMS = load_json_mapping("extra_stream_params.json")


def get_extra_params(scope=None):
    """获取已启用的额外流参数（XHS 版默认无附加参数）。"""
    result = {}
    for item in EXTRA_STREAM_PARAMS:
        if not item.get("enabled", False):
            continue
        if scope is not None and item.get("scope") not in (scope, "all"):
            continue
        result[item["key"]] = item["value"]
    return result


# === 推流端二进制搜索关键词 ===
RTMP_BINARY_KEYWORDS = [
    b"FMLE", b"OBS", b"obs", b"XSplit", b"xsplit", b"vMix", b"vmix",
    b"Streamlabs", b"streamlabs", b"ffmpeg", b"FFmpeg", b"FFMPEG", b"librtmp",
    b"Wirecast", b"wirecast", b"PRISM", b"prism",
    b"xiaohongshu", b"XiaoHongShu", b"XHS", b"xhs", b"redlive", b"xhslive",
    b"bytedance", b"ByteDance", b"bytemedia", b"ByteMedia",
]


def ensure_dirs():
    """确保输出目录存在。"""
    for d in (OUTPUT_DIR, REPORT_DIR, DUAL_STREAM_SAMPLE_DIR, DUAL_STREAM_MANIFEST_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("无法创建目录 %s: %s", d, e)
