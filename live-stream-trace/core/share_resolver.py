#!/usr/bin/env python3
"""分享文本/口令 → 直播间链接 的解析器（一个入口，多路回退）。

支持输入：
  · 直播间链接（http/https/rtmp）
  · 短链（m.tb.cn / tb.cn / 3.cn / jd.hk / jd.cn）→ 跟随跳转取最终地址
  · 淘口令（`666:/🗝XXXXXXXX《` 形态）
  · 京东口令/分享文案
  · 裸房间号（交给平台适配器识别）

设计原则：**能解析就解析，不能就明确降级**并给出可执行的下一步（绝不假装成功）。

关于淘口令的现状（实测结论，勿误解）：
  · 淘宝 h5 mtop 网关（acs.m.taobao.com/h5）对通用 appKey 12574478 关闭了业务 API 白名单
    —— 连 `mtop.taobao.media.live.room.info` 都返回 FAIL_SYS_API_NOT_FOUNDED；
  · 因此淘口令无法在服务端离线解码。代码保留 `_try_taobao_password()` 调用路径，
    一旦配置了有权限的 appKey / 登录态 Cookie 即可自动生效；
  · 降级时返回提取到的口令码 + 主播名线索 + 明确的下一步。
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"(?:https?|rtmps?)://[^\s\"'<>）)】\|]+", re.I)
# 短链域名（需要跟随跳转）
SHORT_HOSTS = ("m.tb.cn", "tb.cn", "3.cn", "jd.hk", "jd.cn", "s.click.taobao.com",
               "u.jd.com", "v.douyin.com", "v.kuaishou.com", "xhslink.com")
# 口令形态：前缀 + 分隔符/表情 + 码 + 结束符
TAO_PW_PATTERNS = (
    r"[:：]/[^\w\s]{0,3}([A-Za-z0-9]{8,16})[^\w\s]{0,3}",
    r"([A-Za-z0-9]{8,16})[《￥¥$]",
    r"[￥¥$]([A-Za-z0-9]{8,16})[￥¥$]",
    r"\(([A-Za-z0-9]{8,16})\)",
)
JD_MARKERS = ("京东", "jd.com", "lives.jd", "3.cn", "jd.hk", "jingdong")
TAO_MARKERS = ("淘宝", "淘口令", "天猫", "tmall", "taobao", "tb.cn", "tbzb")


def url_of(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;，。") if m else None


def is_short_link(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SHORT_HOSTS)


def extract_tao_password(text: str) -> str:
    """从淘口令文案里提取口令码。"""
    for pat in TAO_PW_PATTERNS:
        m = re.search(pat, text or "")
        if m:
            return m.group(1)
    return ""


def extract_anchor_hint(text: str) -> str:
    """从口令文案里提取主播名线索（如「示例主播的直播」）。"""
    t = text or ""
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9_.\-·]{2,24})的直播", t)
    return m.group(1) if m else ""


def _follow_redirect(url: str, ua: str = "", timeout: int = 12) -> tuple[str, list]:
    """跟随短链跳转，返回 (最终地址, 步骤日志)。"""
    steps = [f"检测到短链，跟随跳转：{url}"]
    try:
        import requests  # noqa: PLC0415
        from config import COMMON_UA_DESKTOP  # noqa: PLC0415
        r = requests.get(url, allow_redirects=True, timeout=timeout, headers={
            "User-Agent": ua or COMMON_UA_DESKTOP,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        final = r.url or url
        steps.append(f"跳转后：{final[:160]}")
        return final, steps
    except Exception as exc:  # noqa: BLE001
        logger.warning("短链跳转失败 %s: %s", url, exc)
        steps.append(f"跳转失败（{type(exc).__name__}）：将按原地址处理")
        return url, steps


def _try_taobao_password(password: str, cookies: str = "") -> tuple[Optional[str], list]:
    """尝试解码淘口令（当前环境通常不可用，保留路径以便配置有权限的 appKey/Cookie）。"""
    steps: list = []
    try:
        from taobao import mtop_h5_get  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        steps.append(f"淘口令解码不可用（{exc}）")
        return None, steps
    raw = password if re.search(r"[:：]/", password) else f"666:/🗝{password}《"
    for api, ver, data in (
        ("mtop.taobao.sharepassword.query", "1.0", {"password_content": raw}),
        ("mtop.taobao.sharepassword.query", "2.0", {"password_content": raw}),
    ):
        try:
            out = mtop_h5_get(api, data, version=ver, cookies=cookies)
        except Exception as exc:  # noqa: BLE001
            steps.append(f"{api} v{ver} 异常：{type(exc).__name__}")
            continue
        ret = " ".join((out or {}).get("ret") or [])
        steps.append(f"{api} v{ver} → {ret or '空返回'}")
        payload = (out or {}).get("data") or {}
        for key in ("url", "targetUrl", "resultUrl", "itemUrl", "shortUrl", "landUrl"):
            val = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(val, str) and val.startswith("http"):
                return val, steps
        if isinstance(payload, dict) and payload.get("resultData"):
            inner = payload["resultData"]
            if isinstance(inner, dict):
                for key in ("url", "targetUrl", "itemUrl"):
                    val = inner.get(key)
                    if isinstance(val, str) and val.startswith("http"):
                        return val, steps
    return None, steps


def resolve_share(text: str, *, cookies: str = "", cookie_for: Optional[dict] = None,
                  use_playwright: bool = False) -> dict:
    """解析分享文本 → 直播间链接。返回结构化结果（永不抛异常）。"""
    text = (text or "").strip()
    out: dict = {
        "ok": False, "kind": "unknown", "platform": None, "platform_name": None,
        "url": None, "room_id": None, "raw": text, "steps": [], "manual": [], "notes": [],
        "password_code": "", "anchor_hint": "",
    }
    if not text:
        out["manual"].append("请输入分享文本、直播间链接或房间号")
        return out

    # 载入平台注册表
    try:
        from config import detect_platform, platform_registry  # noqa: PLC0415
        registry = platform_registry()
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"平台注册表不可用：{exc}")
        detect_platform, registry = (lambda _t: None), {}

    # ── 1) 文本里的 URL ──
    url = url_of(text)
    if url:
        out["kind"] = "url"
        if is_short_link(url):
            url, steps = _follow_redirect(url)
            out["steps"].extend(steps)
        adapter = detect_platform(url)
        if adapter:
            out["platform"] = adapter.meta.key
            out["platform_name"] = adapter.meta.name
            if adapter.meta.key == "douyin":
                out["kind"] = "douyin_share"
            extract = getattr(adapter, "extract_room_id", None)
            if callable(extract):
                try:
                    out["room_id"] = extract(url) or None
                except Exception:  # noqa: BLE001
                    pass
        out["url"] = url
        out["ok"] = True
        out["steps"].append(f"识别平台：{out['platform_name'] or '未识别（将按 URL 直连）'}")
        if not out["platform"] and "amemv.com" in url:
            out["steps"].append("（webcast.amemv.com 为抖音移动端直播页）")
        if not out["platform"]:
            out["notes"].append("未能从最终地址识别出平台：可用 --platform 显式指定，"
                                "或使用通用 RTMP 适配器（rtmp:// 开头）。")
        return out

    # ── 2) 淘口令 ──
    low = text.lower()
    if any(m in text for m in TAO_MARKERS) or re.search(r"[:：]/[^\w\s]{0,3}[A-Za-z0-9]{8,16}", text):
        out["kind"] = "taopassword"
        out["password_code"] = extract_tao_password(text)
        out["anchor_hint"] = extract_anchor_hint(text)
        out["steps"].append(f"识别为淘口令（口令码：{out['password_code'] or '未提取到'}）")
        found, steps = _try_taobao_password(out["password_code"] or text, cookies=cookies)
        out["steps"].extend(steps)
        if found:
            out["url"] = found
            out["ok"] = True
            adapter = detect_platform(found)
            if adapter:
                out["platform"], out["platform_name"] = adapter.meta.key, adapter.meta.name
            return out
        out["manual"].extend([
            "淘口令需在淘宝 App 内打开：打开后点右上角「分享」→「复制链接」，把 http(s) 链接粘贴回来即可直接分析。",
            "或直接粘贴直播间地址（形如 https://tbzb.taobao.com/?roomId=123456789）或房间号。",
            "如需服务端直接解码：配置 cookies/taobao.cookie 登录态，并让 taobao.py 使用有权限的 appKey"
            "（当前通用 appKey 被淘宝网关白名单拒绝，返回 FAIL_SYS_API_NOT_FOUNDED）。",
        ])
        if out["anchor_hint"]:
            out["notes"].append(f"文案中的主播线索：「{out['anchor_hint']}」，"
                                "可在淘宝直播搜索该主播后复制房间链接。")
        return out

    # ── 3) 京东口令 / 分享文案 ──
    if any(m in low for m in JD_MARKERS):
        out["kind"] = "jdpassword"
        out["steps"].append("识别为京东分享文案，但其中不含可解析的链接")
        out["manual"].append("京东口令需含链接（如 https://3.cn/xxxxx）。"
                             "请在京东 App 内「分享 → 复制链接」后粘贴回来。")
        return out

    # ── 4) 裸房间号 / 其它 ──
    if re.fullmatch(r"[A-Za-z0-9_\-]{4,32}", text):
        out["kind"] = "room_id"
        out["room_id"] = text
        out["manual"].append("这是房间号（不是链接）：请用 --platform 指定平台，"
                             "或在网页端选择平台后再分析。")
        return out

    out["manual"].append("未识别出链接/口令：请粘贴直播间链接、分享链接或房间号。")
    return out


def format_resolution(res: dict) -> str:
    """人类可读的解析结果（CLI 用）。"""
    lines = [f"输入：{res.get('raw', '')[:120]}",
             f"类型：{res.get('kind')}"]
    if res.get("platform_name"):
        lines.append(f"平台：{res['platform_name']}")
    if res.get("room_id"):
        lines.append(f"房间号：{res['room_id']}")
    if res.get("url"):
        lines.append(f"直播间链接：{res['url']}")
    for s in res.get("steps", []):
        lines.append(f"· {s}")
    for n in res.get("notes", []):
        lines.append(f"ℹ️ {n}")
    for m in res.get("manual", []):
        lines.append(f"👉 {m}")
    return "\n".join(lines)
