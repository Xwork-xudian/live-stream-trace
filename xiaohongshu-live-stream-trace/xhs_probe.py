"""小红书直播探测工具集：档位地址探测、直播时长、开播渠道推断、账号信息（需 Cookie）。

关键实测结论（2026-10，三个真实直播间验证）：
  1. 档位由「房间号 + 后缀」构成：
        {room}.flv        原画（H.264，实测 1080x1920 10.2Mbps）
        {room}_orig.flv   原画（HEVC，实测 hevc 1080x1920）
        {room}_sd.flv     标清/流畅（实测 200x360 300kbps）
     每档都有同名的 .m3u8（HLS）。未列出的后缀（_ld/_hd/_fhd/_uhd…）CDN 直接挂起 → 不存在。
  2. 直播时长：FLV tag 时间戳为「流内绝对位置」，末个媒体 tag ≈ 已播时长；
     开播时间 ≈ 当前时间 − 已播时长（脚本 tag 的 ts=0 需排除）。
  3. 开播渠道：onMetaData 的 encoder / platform 字段直接暴露客户端
        kasa_win_*          → 小红书 PC 开播工具（Kasa）
        platform=iOS/Android→ 小红书 App 手机开播
        obs-output module   → OBS Studio（第三方）
        server=TLSS(qcloud) → 腾讯云直播 SDK
  4. 账号信息（粉丝/关注/小红书号）在 live-room.xiaohongshu.com 的 user_card 接口，
     需 x-s 签名（已由 `xhs` 包提供）+ 登录 Cookie，无 Cookie 会返回“无登录信息”。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from config import COMMON_HEADERS, SOURCE_HOST

logger = logging.getLogger(__name__)

# 档位候选（实测有效集）
TIER_CANDIDATES = [
    ("source", "", "原画（H.264）"),
    ("orig", "_orig", "原画（HEVC/H.265）"),
    ("hcv", "_hcv5400", "安卓原画（HEVC·hcv5400）"),
    ("sd", "_sd", "标清/流畅"),
]

# RTMP 源流：安卓抓包格式为 live-play，实测该主机时好时坏（No such stream），
# 而 live-source-play 同样支持 RTMP 拉流 —— 因此多主机依次探测。
RTMP_TEMPLATE = "rtmp://live-play.xhscdn.com/live/{room_id}"
RTMP_HOST_CANDIDATES = [
    "live-play.xhscdn.com",
    "live-source-play.xhscdn.com",
    "live-source-play-bak-tx.xhscdn.com",
]
# 安卓客户端附加参数（可选，实测不影响拉流）
ANDROID_EXTRA_PARAMS = {"abr_pts": "-1000"}

# 开播渠道推断规则（按 priority 顺序匹配 encoder）
PUSH_CHANNEL_RULES = [
    (r"kasa[_-]?win", "小红书 PC 开播工具（Kasa·Windows）"),
    (r"kasa[_-]?mac", "小红书 PC 开播工具（Kasa·macOS）"),
    (r"kasa[_-]?(ios|iphone)", "小红书 App 手机开播（iOS）"),
    (r"kasa[_-]?android", "小红书 App 手机开播（Android）"),
    (r"\bkasa\b", "小红书开播工具（Kasa）"),
    (r"obs[_-]?output|libobs|\bobs\b", "第三方推流：OBS Studio"),
    (r"streamlabs", "第三方推流：Streamlabs OBS"),
    (r"ffmpeg|librtmp|libav|lavf|libavformat", "第三方推流：FFmpeg"),
    (r"xsplit", "第三方推流：XSplit"),
    (r"wirecast", "第三方推流：Wirecast"),
    (r"vmix", "第三方推流：vMix"),
]

# 推流服务器标识 → 友好名
PUSH_SERVER_NAMES = {
    "tlss": "腾讯云直播 SDK（TLSS/qcloud）",
    "mcp_vcloud": "字节 MCP 直播云（MCP_VCLOUD）",
}


def _ffprobe_bin() -> Optional[str]:
    import shutil
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    for cand in ("/opt/ffmpeg/bin/ffprobe",):
        if Path(cand).exists():
            return cand
    return None


def probe_flv_bytes(data: bytes) -> dict:
    """用 ffprobe 解析 FLV 字节切片，返回 codec/分辨率/码率/onMetaData 标签。"""
    exe = _ffprobe_bin()
    if not exe or not data:
        return {}
    with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(tmp)],
            capture_output=True, text=True, timeout=30)
        info = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}
    finally:
        tmp.unlink(missing_ok=True)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    tags = (info.get("format") or {}).get("tags") or {}
    out = {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "profile": video.get("profile"),
        "level": video.get("level"),
        "audio_codec": audio.get("codec_name"),
        "bitrate": int(info["format"]["bit_rate"]) if (info.get("format") or {}).get("bit_rate") else None,
        "meta": {k: tags[k] for k in ("encoder", "platform", "server", "server_version",
                                      "isHardEncoder", "audioBitrate", "maxBitrate", "startBitrate",
                                      "source", "region", "X-Xhs-Via", "X-Xhs-Trace-Id")
                 if k in tags},
    }
    out["push_channel"] = infer_push_channel(out["meta"])
    out["push_server"] = infer_push_server(out["meta"])
    return out


def infer_push_channel(meta: dict | None) -> str:
    """由 onMetaData 的 encoder/platform 推断开播渠道。"""
    meta = meta or {}
    enc = str(meta.get("encoder") or "").strip()
    plat = str(meta.get("platform") or "").strip()
    low = enc.lower()
    for pat, name in PUSH_CHANNEL_RULES:
        if re.search(pat, low):
            return name if plat.lower() in name.lower() else f"{name}"
    # 无已知 encoder 指纹时按 platform 兜底（如 iOS SDK 的哈希 encoder）
    if plat.lower() in ("ios", "android", "iphone", "ipad"):
        return f"手机开播（{plat}）"
    if plat.lower() in ("windows", "mac", "macos", "darwin"):
        return f"PC 开播（{plat}）"
    return f"未知（encoder={enc or '无'}）"


def format_client_version(meta: dict | None) -> str | None:
    """把 onMetaData.encoder 格式化为可读的客户端版本号。"""
    meta = meta or {}
    enc = str(meta.get("encoder") or "").strip()
    plat = str(meta.get("platform") or "").strip()
    if not enc:
        return None
    m = re.search(r"libobs version ([\d.]+)", enc, re.I)
    if m:
        return f"OBS {m.group(1)}"
    m = re.search(r"(obs|streamlabs)[^\d]*([\d.]+)", enc, re.I)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    m = re.search(r"kasa[_-]?(win|mac|ios|android)[_-]?([\d.]+)", enc, re.I)
    if m:
        return f"{m.group(2)}（Kasa {m.group(1).capitalize()}）"
    m = re.search(r"ffmpeg[^\d]*([\d.]+)", enc, re.I)
    if m:
        return f"FFmpeg {m.group(1)}"
    m = re.search(r"(?:lavf|libavformat)[^\d]*([\d.]+)", enc, re.I)
    if m:
        return f"FFmpeg（Lavf{m.group(1)}）"
    if re.fullmatch(r"[0-9a-f]{6,16}", enc, re.I):
        return f"—（客户端指纹 {enc}{('/' + plat) if plat else ''}）"
    return enc


def infer_push_server(meta: dict | None) -> str:
    srv = str((meta or {}).get("server") or "").strip()
    if not srv:
        return "—"
    low = srv.lower()
    for key, name in PUSH_SERVER_NAMES.items():
        if key in low:
            return f"{name}"
    return srv


def probe_stream_tiers(room_id: str, headers: dict | None = None, host: str | None = None,
                       sample_bytes: int = 1_200_000, verify: bool = True,
                       timeout: int = 8) -> list[dict]:
    """探测全部档位（原画 / 原画HEVC / 标清），返回带实测参数的列表。"""
    host = host or SOURCE_HOST
    hdrs = dict(headers or COMMON_HEADERS)
    tiers: list[dict] = []
    for key, suffix, label in TIER_CANDIDATES:
        flv = f"http://{host}/live/{room_id}{suffix}.flv"
        hls = f"http://{host}/live/{room_id}{suffix}.m3u8"
        entry: dict = {"key": key, "suffix": suffix, "label": label,
                       "flv": flv, "hls": hls, "ok": False, "hls_ok": False}
        try:
            resp = requests.get(flv, headers={**hdrs, "Range": "bytes=0-262143"}, timeout=timeout, stream=True)
            entry["status"] = resp.status_code
            if resp.status_code == 200:
                entry["ok"] = True
                if verify:
                    buf = bytearray()
                    for chunk in resp.iter_content(65536):
                        buf.extend(chunk)
                        if len(buf) >= sample_bytes:
                            break
                    entry["bytes_sampled"] = len(buf)
                    entry.update(probe_flv_bytes(bytes(buf)))
            resp.close()
        except requests.exceptions.Timeout:
            entry["status"] = "TIMEOUT(不存在)"
        except Exception as exc:  # noqa: BLE001
            entry["status"] = type(exc).__name__
        try:
            rh = requests.get(hls, headers=hdrs, timeout=timeout, stream=True)
            entry["hls_status"] = rh.status_code
            entry["hls_ok"] = rh.status_code == 200
            rh.close()
        except requests.exceptions.Timeout:
            entry["hls_status"] = "TIMEOUT(不存在)"
        except Exception as exc:  # noqa: BLE001
            entry["hls_status"] = type(exc).__name__
        tiers.append(entry)
    return tiers


def flv_stream_duration(url: str, headers: dict | None = None,
                        sample_bytes: int = 3_000_000, timeout: int = 12) -> dict:
    """由 FLV tag 时间戳求已播时长（tag ts = 流内绝对位置；脚本 tag ts=0 需排除）。"""
    hdrs = dict(headers or COMMON_HEADERS)
    try:
        resp = requests.get(url, headers=hdrs, timeout=timeout, stream=True)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code}
        buf = bytearray()
        for chunk in resp.iter_content(65536):
            buf.extend(chunk)
            if len(buf) >= sample_bytes:
                break
        resp.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    data = bytes(buf)
    pos, n = 13, len(data)
    first_media_ts = None
    last_media_ts = 0
    media_tags = 0
    while pos + 11 <= n:
        ttype = data[pos]
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if size <= 0 or pos + 11 + size + 4 > n:
            break
        ts = int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24)
        if ttype in (8, 9):          # 音视频 tag（排除 18=脚本）
            media_tags += 1
            if first_media_ts is None:
                first_media_ts = ts
            last_media_ts = max(last_media_ts, ts)
        pos += 11 + size + 4
    if not media_tags:
        return {"ok": False, "error": "未解析到媒体 tag"}
    return {
        "ok": True,
        "first_media_ts_ms": first_media_ts,
        "last_media_ts_ms": last_media_ts,
        "elapsed_ms": last_media_ts,          # 首个媒体 tag 接近 0，末个即已播时长
        "media_tags": media_tags,
        "sampled_bytes": len(data),
    }


# ───────────────── 需登录 Cookie 的账号信息（粉丝/关注/小红书号） ─────────────────

LIVE_API_BASE = "https://live-room.xiaohongshu.com"


def _sign(uri: str) -> dict:
    """用 xhs 包生成 x-s / x-t 签名头（未安装则返回空）。"""
    try:
        from xhs.help import sign  # type: ignore
        return dict(sign(uri))
    except Exception as exc:  # noqa: BLE001
        logger.debug("xhs 签名不可用: %s", exc)
        return {}


def fetch_with_sign(uri: str, cookies: str | None = None, base: str = LIVE_API_BASE,
                    timeout: int = 12) -> dict:
    """调用 live-room 接口（带签名 + Cookie）。无 Cookie 会返回 code=-101。"""
    headers = {
        "User-Agent": COMMON_HEADERS.get("User-Agent", ""),
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "application/json, text/plain, */*",
        **_sign(uri),
    }
    if cookies:
        headers["Cookie"] = cookies
    try:
        resp = requests.get(base + uri, headers=headers, timeout=timeout)
        try:
            return resp.json()
        except ValueError:
            return {"code": resp.status_code, "success": False, "msg": resp.text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def fetch_account_profile(host_id: str, cookies: str | None = None, timeout: int = 15) -> dict:
    """抓主播主页 SSR，取 小红书号(redId)/粉丝/关注/获赞与收藏/简介/IP属地（需登录 Cookie）。

    实测（2026-10）：带 Cookie 时主页 SSR 的 user.userPageData 直接给出这些字段；
    不带 Cookie 会跳转登录风控页。
    """
    if not cookies or not host_id:
        return {"ok": False, "need_login": True}
    url = f"https://www.xiaohongshu.com/user/profile/{host_id}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.xiaohongshu.com/",
        "Cookie": cookies,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        m = re.search(r"<script>window\.__INITIAL_STATE__=(.*?)</script>", resp.text, re.S)
        if not m:
            return {"ok": False, "error": "主页未返回 SSR（Cookie 失效或风控）"}
        state = json.loads(m.group(1).replace("undefined", "null"))
    except (requests.RequestException, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    upd = ((state.get("user") or {}).get("userPageData")) or {}
    basic = upd.get("basicInfo") or {}
    if not basic:
        return {"ok": False, "error": "主页数据为空（可能需重新登录）"}
    inter = {i.get("type"): (i.get("count"), i.get("i18nCount"))
             for i in (upd.get("interactions") or []) if isinstance(i, dict)}

    def _pair(key):
        v = inter.get(key)
        return {"count": v[0], "display": v[1]} if v else None

    return {
        "ok": True,
        "red_id": basic.get("redId") or basic.get("red_id"),
        "nickname": basic.get("nickname"),
        "avatar": basic.get("images") or basic.get("imageb"),
        "desc": basic.get("desc"),
        "ip_location": basic.get("ipLocation"),
        "gender": basic.get("gender"),
        "fans": _pair("fans"),
        "follows": _pair("follows"),
        "likes_collects": _pair("interaction"),
        "source_url": url,
    }


def probe_rtmp_sources(room_id: str, timeout: int = 15) -> list[dict]:
    """依次探测多个 RTMP 主机，返回全部尝试结果（含可用性）。"""
    out = []
    for host in RTMP_HOST_CANDIDATES:
        r = _probe_one_rtmp(f"rtmp://{host}/live/{room_id}", timeout)
        out.append(r)
        if r.get("ok"):
            break          # 找到可用即停止（其余主机不浪费时间）
    return out


def _probe_one_rtmp(url: str, timeout: int = 15) -> dict:
    exe = _ffprobe_bin()
    if not exe:
        return {"ok": False, "url": url, "error": "ffprobe 不可用"}
    try:
        proc = subprocess.run([exe, "-v", "error", "-show_entries", "stream=codec_name,width,height",
                               "-of", "csv=p=0", "-analyzeduration", "6M", "-probesize", "6M", url],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "url": url, "error": type(exc).__name__}
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    video = next((l for l in lines if l.startswith(("h26", "hevc", "avc"))), "")
    parts = video.split(",")
    out = {"ok": bool(video), "url": url, "raw": " + ".join(lines)[:120]}
    if video:
        out.update({"codec": parts[0],
                    "width": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                    "height": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None})
    else:
        err = (proc.stderr or "").strip().splitlines()
        out["error"] = err[-1][:120] if err else "无输出"
    return out


def probe_rtmp_source(room_id: str, timeout: int = 15) -> dict:
    """探测 RTMP 源流（ffprobe）。"""
    exe = _ffprobe_bin()
    url = RTMP_TEMPLATE.format(room_id=room_id)
    if not exe:
        return {"ok": False, "url": url, "error": "ffprobe 不可用"}
    try:
        proc = subprocess.run([exe, "-v", "error", "-show_entries", "stream=codec_name,width,height",
                               "-of", "csv=p=0", "-analyzeduration", "6M", "-probesize", "6M", url],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "url": url, "error": type(exc).__name__}
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    video = next((l for l in lines if l.startswith(("h26", "hevc", "avc"))), "")
    parts = video.split(",")
    out = {"ok": bool(video), "url": url, "raw": " + ".join(lines)[:120]}
    if video:
        out.update({"codec": parts[0], "width": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                    "height": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None})
    return out


def fetch_account_info(room_id: str, host_id: str, cookies: str | None = None) -> dict:
    """账号信息：粉丝/关注/小红书号（user_card）+ 房间信息（current_room_info）。"""
    out: dict = {"need_login": not bool(cookies)}
    card = fetch_with_sign(f"/api/sns/red/live/web/{room_id}/user_card?user_id={host_id}&room_id={room_id}",
                           cookies=cookies)
    out["user_card"] = card
    room = fetch_with_sign(f"/api/sns/red/live/web/v1/room/current_room_info?room_id={room_id}&request_user_id=",
                           cookies=cookies)
    out["room_info"] = room
    if isinstance(card, dict) and card.get("success"):
        d = card.get("data") or {}
        out.update({
            "nickname": (d.get("user") or {}).get("nickname") or d.get("nickname"),
            "red_id": (d.get("user") or {}).get("red_id") or d.get("red_id"),
            "fans": d.get("fans") or d.get("fans_count") or d.get("follower_count"),
            "follows": d.get("follows") or d.get("follow_count"),
        })
    return out
