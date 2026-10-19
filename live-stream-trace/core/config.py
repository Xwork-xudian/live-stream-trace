"""live-stream-trace 全局配置：路径、HTTP 默认、平台注册、Cookie 存储。

各平台 Cookie 统一放在 cookies/<platform>.cookie（每行一个 Cookie 字符串或整行），
可用环境变量覆盖：LST_COOKIE_<PLATFORM>。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# === 目录 ===
BASE_DIR = Path(__file__).resolve().parent.parent        # live-stream-trace/
CORE_DIR = BASE_DIR / "core"
PLATFORMS_DIR = BASE_DIR / "platforms"
OUTPUT_DIR = BASE_DIR / "captures"
REPORT_DIR = BASE_DIR / "reports"
REPORT_ASSETS_DIR = BASE_DIR / "report_assets"
COOKIE_DIR = BASE_DIR / "cookies"
DUAL_STREAM_SAMPLE_DIR = OUTPUT_DIR / "dual_stream_samples"
DUAL_STREAM_MANIFEST_DIR = OUTPUT_DIR / "dual_stream_manifests"

# === HTTP ===
API_TIMEOUT = 15
POLL_INTERVAL = 30
DOWNLOAD_MAX_BYTES = 3 * 1024 * 1024
CAPTURE_SECONDS = 5

COMMON_UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
COMMON_UA_IOS = "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))"

# 通用请求头（平台适配器会按需覆盖）
COMMON_HEADERS = {
    "User-Agent": COMMON_UA_DESKTOP,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 兼容既有模块（flv_analyzer 等）的导入名
ROOM_ID = os.getenv("LST_ROOM_ID", "")
WEB_ROOM_ID = ROOM_ID
ENTER_URLS: list[str] = []

# === 映射表 ===
try:  # 兼容两种运行方式：core/ 在 sys.path（推荐）或项目根在 sys.path
    from mappings_loader import load_json_mapping  # noqa: E402
except ImportError:  # pragma: no cover
    from core.mappings_loader import load_json_mapping  # noqa: E402


SOFTWARE_PATTERNS = [(item["pattern"], item["name"]) for item in load_json_mapping("software_patterns.json")]
CDN_PATTERNS = [(item["keyword"], item["name"]) for item in load_json_mapping("cdn_patterns.json")]
EXTRA_STREAM_PARAMS = load_json_mapping("extra_stream_params.json")


def get_extra_params(scope=None):
    """获取已启用的额外流参数。"""
    result = {}
    for item in EXTRA_STREAM_PARAMS:
        if not item.get("enabled", False):
            continue
        if scope is not None and item.get("scope") not in (scope, "all"):
            continue
        result[item["key"]] = item["value"]
    return result


# === 平台注册（自动发现 platforms/*.py 中的 ADAPTER）===
_PLATFORM_CACHE: dict | None = None


def platform_registry() -> dict:
    """返回 {platform_key: adapter_instance}，自动扫描 platforms/ 目录。"""
    global _PLATFORM_CACHE
    if _PLATFORM_CACHE is not None:
        return _PLATFORM_CACHE
    registry: dict = {}
    if str(PLATFORMS_DIR) not in sys.path:
        sys.path.insert(0, str(PLATFORMS_DIR))
    for path in sorted(PLATFORMS_DIR.glob("*.py")):
        if path.stem.startswith("_") or path.stem in {"base"}:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"lst_platform_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载平台适配器失败 %s: %s", path.stem, exc)
            continue
        adapter = getattr(mod, "ADAPTER", None)
        if adapter is not None:
            registry[adapter.meta.key] = adapter
    _PLATFORM_CACHE = registry
    return registry


def detect_platform(text: str):
    """按平台声明顺序做 URL/输入识别，返回 adapter 或 None。"""
    for adapter in platform_registry().values():
        try:
            if adapter.matches(text or ""):
                return adapter
        except Exception:  # noqa: BLE001
            continue
    return None


# === Cookie 存储 ===
def cookie_path(platform: str) -> Path:
    return COOKIE_DIR / f"{platform}.cookie"


def load_cookie(platform: str) -> str:
    """读取平台 Cookie：环境变量 LST_COOKIE_<PLATFORM> 优先，其次 cookies/<platform>.cookie。"""
    env = os.getenv(f"LST_COOKIE_{platform.upper()}")
    if env and env.strip():
        return env.strip()
    path = cookie_path(platform)
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            return text
    except OSError as exc:
        logger.warning("读取 Cookie 失败 %s: %s", path, exc)
    return ""


def save_cookie(platform: str, value: str) -> Path:
    ensure_dirs()
    path = cookie_path(platform)
    path.write_text((value or "").strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def ensure_dirs():
    for d in (OUTPUT_DIR, REPORT_DIR, DUAL_STREAM_SAMPLE_DIR, DUAL_STREAM_MANIFEST_DIR, COOKIE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("无法创建目录 %s: %s", d, e)
