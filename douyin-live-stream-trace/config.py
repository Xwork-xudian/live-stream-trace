"""抖音直播流分析 — 共享配置"""
import logging
import os
import sys
from pathlib import Path

from mappings_loader import load_json_mapping

logger = logging.getLogger(__name__)


def _app_dir() -> Path:
    """获取应用程序所在目录（兼容 PyInstaller 打包）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# === 直播间配置 ===
ROOM_ID = os.getenv("DOUYIN_ROOM_ID", "7640506484396411698")           # 内部房间 ID
WEB_ROOM_ID = os.getenv("DOUYIN_WEB_ROOM_ID", "376389454958")              # Web 房间号
LIVE_URL = f"https://live.douyin.com/{WEB_ROOM_ID}"

# === 目录配置 ===
BASE_DIR = _app_dir()
OUTPUT_DIR = BASE_DIR / "captures"
REPORT_DIR = BASE_DIR / "reports"
DUAL_STREAM_SAMPLE_DIR = OUTPUT_DIR / "dual_stream_samples"
DUAL_STREAM_MANIFEST_DIR = OUTPUT_DIR / "dual_stream_manifests"

# === HTTP 配置 ===
API_TIMEOUT = 15                          # 秒
POLL_INTERVAL = 30                        # monitor_reflow 轮询间隔 (秒)
DOWNLOAD_MAX_BYTES = 1024 * 1024          # FLV 样本最大下载量
CAPTURE_SECONDS = 5                       # 默认流抓取时长（秒）

# === 通用 Headers ===
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://live.douyin.com/{WEB_ROOM_ID}",
}

# === API 端点 ===
# 单 URL（向后兼容）
REFLOW_URL = "https://live.douyin.com/webcast/room/reflow/info/"
ENTER_URL = "https://live.douyin.com/webcast/room/web/enter/"
# 多域名列表（脚本自动 fallback）
REFLOW_URLS = [
    "https://live.douyin.com/webcast/room/reflow/info/",
    "https://webcast-hl.amemv.com/webcast/room/reflow/info/",
]
ENTER_URLS = [
    "https://live.douyin.com/webcast/room/web/enter/",
    "https://webcast-hl.amemv.com/webcast/room/web/enter/",
]

# 探针接口 URL 列表（capture-first，多域名自动 fallback）
GIFT_LIST_URLS = [
    "https://live.douyin.com/webcast/gift/list/",
    "https://webcast-hl.amemv.com/webcast/gift/list/",
]
ROOM_INFO_URLS = [
    "https://live.douyin.com/webcast/room/info/",
    "https://webcast-hl.amemv.com/webcast/room/info/",
]
RANKLIST_AUDIENCE_URLS = [
    "https://live.douyin.com/webcast/ranklist/audience/",
    "https://webcast-hl.amemv.com/webcast/ranklist/audience/",
]

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

# === 额外流参数（通过观察发现的客户端实验/播放器参数） ===
EXTRA_STREAM_PARAMS = load_json_mapping("extra_stream_params.json")


def get_extra_params(scope=None):
    """获取已启用的额外流参数。

    Args:
        scope: 筛选作用域，"audio_only" 或 "all"。None 返回全部已启用的。
    """
    result = {}
    for item in EXTRA_STREAM_PARAMS:
        if not item.get("enabled", False):
            continue
        if scope is not None and item.get("scope") != scope and item.get("scope") != "all":
            continue
        result[item["key"]] = item["value"]
    return result

# === RTMP 推流软件二进制搜索关键词 (含大小写变体) ===
RTMP_BINARY_KEYWORDS = [
    b"FMLE", b"OBS", b"obs", b"XSplit", b"xsplit", b"vMix", b"vmix",
    b"Streamlabs", b"streamlabs",
    b"ffmpeg", b"FFmpeg", b"FFMPEG", b"librtmp",
    b"Wirecast", b"wirecast",
    b"Douyin", b"douyin", b"DOUYIN",
    b"bytedance", b"ByteDance", b"BYTEDANCE",
    b"ByteMedia", b"bytemedia", b"BYTEMEDIA",
    b"bytedmediasdk", b"ByteDmediasdk", b"BYTEDMEDIASDK",
]


def ensure_dirs():
    """确保输出目录存在，失败时记录日志但不崩溃"""
    for d in (OUTPUT_DIR, REPORT_DIR, DUAL_STREAM_SAMPLE_DIR, DUAL_STREAM_MANIFEST_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("无法创建目录 %s: %s", d, e)
