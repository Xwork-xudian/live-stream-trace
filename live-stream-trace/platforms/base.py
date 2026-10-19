"""平台适配器接口（插件契约）。

新增平台只需：
  1. 在 platforms/ 下新建 <platform>.py，定义 `ADAPTER = YourAdapter()`；
  2. 实现 `matches(text)`（识别 URL/房间号）与 `resolve(text, ctx)`（返回 LiveRoom）；
  3. 报告/播放器/音画同步等全部由 core 自动处理，无需改动。

归一化结构说明见 LiveRoom.to_payload()：报告只认这个 payload。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass
class PlatformMeta:
    key: str                 # 唯一标识，如 "xiaohongshu"
    name: str                # 展示名，如 "小红书"
    color: str               # 品牌色（报告用）
    live_url: str            # 直播间链接模板，{room_id}
    cta: str = ""            # CTA 按钮文案，如 "前往小红书直播"


@dataclass
class StreamTier:
    """一个可拉取的流（档位 × 协议）。"""
    label: str                       # 展示名，如 "原画（H.264）"
    protocol: str                    # flv / hls / rtmp / other
    url: str
    ok: bool = False                 # 探测是否可用
    status: Any = None               # HTTP 状态 / 错误
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate: Optional[int] = None
    quality_key: str = ""            # 平台档位键（qn / quality_type / suffix）
    playable: bool = True            # 网页播放器能否直接播（HEVC/RTMP → False）
    note: str = ""


@dataclass
class LiveRoom:
    """解析结果（平台无关）。"""
    platform: str
    room_id: str
    status: str = "未知"
    title: Optional[str] = None
    anchor: Optional[str] = None
    anchor_id: Optional[str] = None
    avatar: Optional[str] = None
    live_start_ts: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    metrics: dict = field(default_factory=dict)     # fans/follows/likes/viewers/members/praise/pv/account_id...
    tiers: list = field(default_factory=list)       # list[StreamTier]（HTTP 拉流）
    rtmp: list = field(default_factory=list)        # list[StreamTier]（RTMP 源流）
    push: dict = field(default_factory=dict)        # channel/version/server/third_party
    raw: dict = field(default_factory=dict)         # 原始响应（证据）
    notes: list = field(default_factory=list)       # 备注/局限说明

    def live_url(self, meta: PlatformMeta) -> str:
        return meta.live_url.format(room_id=self.room_id) if self.room_id else ""

    def to_payload(self, meta: PlatformMeta) -> dict:
        """归一化 payload —— 报告的唯一输入契约。"""
        return {
            "type": "live_stream_info",
            "platform": meta.key,
            "platform_name": meta.name,
            "platform_color": meta.color,
            "platform_url": self.live_url(meta),
            "platform_cta": meta.cta or f"前往{meta.name}",
            "room_id": self.room_id,
            "status": self.status,
            "title": self.title,
            "anchor": self.anchor,
            "anchor_id": self.anchor_id,
            "avatar": self.avatar,
            "live_start_ts": self.live_start_ts,
            "elapsed_seconds": self.elapsed_seconds,
            "metrics": self.metrics,
            "tiers": [asdict(t) for t in self.tiers],
            "rtmp": [asdict(t) for t in self.rtmp],
            "push": self.push,
            "notes": self.notes,
        }


# ─────────────────────────── 共享工具（适配器可直接用） ───────────────────────────

def ffprobe_bin() -> Optional[str]:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    for cand in ("/opt/ffmpeg/bin/ffprobe",):
        if shutil.which(cand) or __import__("pathlib").Path(cand).exists():
            return cand
    return None


def ffprobe_url(url: str, timeout: int = 15, extra_args: Optional[list] = None) -> dict:
    """ffprobe 拉流/RTMP 探测 → {ok, codec, width, height, raw|error}。"""
    exe = ffprobe_bin()
    if not exe:
        return {"ok": False, "error": "ffprobe 不可用"}
    cmd = [exe, "-v", "error", "-show_entries", "stream=codec_name,width,height",
           "-of", "csv=p=0", "-analyzeduration", "6M", "-probesize", "6M"]
    cmd += (extra_args or []) + [url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    video = next((l for l in lines if l.startswith(("h26", "hevc", "avc"))), "")
    out: dict = {"ok": bool(video), "raw": " + ".join(lines)[:120]}
    if video:
        p = video.split(",")
        out.update({"codec": p[0],
                    "width": int(p[1]) if len(p) > 1 and p[1].isdigit() else None,
                    "height": int(p[2]) if len(p) > 2 and p[2].isdigit() else None})
    else:
        err = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()]
        out["error"] = err[-1][:120] if err else "无输出"
    return out


def ffprobe_bytes(data: bytes) -> dict:
    """解析 FLV 字节切片（codec/分辨率/onMetaData 标签）。"""
    exe = ffprobe_bin()
    if not exe or not data:
        return {}
    with tempfile.NamedTemporaryFile(suffix=".flv", delete=False) as fh:
        fh.write(data)
        tmp = __import__("pathlib").Path(fh.name)
    try:
        proc = subprocess.run([exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(tmp)],
                              capture_output=True, text=True, timeout=30)
        info = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}
    finally:
        tmp.unlink(missing_ok=True)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    tags = (info.get("format") or {}).get("tags") or {}
    return {
        "codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height"),
        "profile": video.get("profile"), "audio_codec": audio.get("codec_name"),
        "bitrate": int(info["format"]["bit_rate"]) if (info.get("format") or {}).get("bit_rate") else None,
        "meta": {k: tags[k] for k in ("encoder", "platform", "server", "server_version", "isHardEncoder",
                                      "audioBitrate", "maxBitrate", "startBitrate", "source", "region",
                                      "X-Xhs-Via", "X-Xhs-Trace-Id") if k in tags},
    }


def http_probe(url: str, headers: Optional[dict] = None, timeout: int = 8,
               sample_bytes: int = 1_200_000, verify: bool = True) -> dict:
    """HTTP 拉流探测：状态码 + 魔术字节（+ 可选 ffprobe 校验编码/分辨率/码率）。"""
    out: dict = {"ok": False}
    try:
        resp = requests.get(url, headers={**(headers or {}), "Range": "bytes=0-262143"},
                            timeout=timeout, stream=True)
        out["status"] = resp.status_code
        out["content_type"] = resp.headers.get("Content-Type", "")
        out["server"] = resp.headers.get("Server", "")
        # 带 Range 的探测正常会返回 206 Partial Content（不能只认 200，否则 HLS/分片 CDN 被误判失败）
        if resp.status_code in (200, 206):
            out["ok"] = True
            if verify:
                buf = bytearray()
                for chunk in resp.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) >= sample_bytes:
                        break
                out["magic"] = bytes(buf[:3]).decode("latin1", "replace")
                out["bytes_sampled"] = len(buf)
                out.update({k: v for k, v in ffprobe_bytes(bytes(buf)).items()
                            if k in ("codec", "width", "height", "bitrate", "meta", "audio_codec", "profile")})
        resp.close()
    except requests.exceptions.Timeout:
        out["status"] = "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        out["status"] = type(exc).__name__
    return out


def infer_push_channel(meta: Optional[dict]) -> str:
    """由 onMetaData 的 encoder/platform 推断开播渠道（跨平台通用）。"""
    import re
    meta = meta or {}
    enc = str(meta.get("encoder") or "").strip()
    plat = str(meta.get("platform") or "").strip()
    low = enc.lower()
    rules = [
        (r"kasa[_-]?win", "小红书 PC 开播工具（Kasa·Windows）"),
        (r"kasa[_-]?mac", "小红书 PC 开播工具（Kasa·macOS）"),
        (r"kasa[_-]?(ios|iphone)", "小红书 App 手机开播（iOS）"),
        (r"kasa[_-]?android", "小红书 App 手机开播（Android）"),
        (r"\bkasa\b", "小红书开播工具（Kasa）"),
        (r"obs[_-]?output|libobs|\bobs\b", "第三方推流：OBS Studio"),
        (r"ffmpeg|librtmp|libav|lavf|libavformat", "第三方推流：FFmpeg"),
        (r"streamlabs", "第三方推流：Streamlabs"),
        (r"xsplit", "第三方推流：XSplit"),
        (r"vmix", "第三方推流：vMix"),
        (r"wirecast", "第三方推流：Wirecast"),
    ]
    for pat, name in rules:
        if re.search(pat, low):
            return name
    if plat.lower() in ("ios", "android", "iphone", "ipad"):
        return f"手机开播（{plat}）"
    if plat.lower() in ("windows", "mac", "macos", "darwin"):
        return f"PC 开播（{plat}）"
    return f"未知（encoder={enc or '无'}）" if enc else "未知"


PUSH_SERVER_NAMES = {
    "tlss": "腾讯云直播 SDK（TLSS/qcloud）",
    "mcp_vcloud": "字节 MCP 直播云（MCP_VCLOUD）",
    "ksyun": "金山云直播",
    "aliyun": "阿里云直播",
    "agora": "声网 Agora",
}


def infer_push_server(meta: Optional[dict]) -> str:
    """由 onMetaData 的 server 字段推断推流服务器/CDN 归属。"""
    srv = str((meta or {}).get("server") or "").strip()
    if not srv:
        return ""
    low = srv.lower()
    for key, name in PUSH_SERVER_NAMES.items():
        if key in low:
            return name
    return srv


def format_client_version(meta: Optional[dict]) -> Optional[str]:
    """把 encoder 格式化为可读的客户端版本。"""
    import re
    meta = meta or {}
    enc = str(meta.get("encoder") or "").strip()
    plat = str(meta.get("platform") or "").strip()
    if not enc:
        return None
    m = re.search(r"libobs version ([\d.]+)", enc, re.I)
    if m:
        return f"OBS {m.group(1)}"
    m = re.search(r"kasa[_-]?(win|mac|ios|android)[_-]?([\d.]+)", enc, re.I)
    if m:
        return f"{m.group(2)}（Kasa {m.group(1).capitalize()}）"
    m = re.search(r"(?:lavf|libavformat)[^\d]*([\d.]+)", enc, re.I)
    if m:
        return f"FFmpeg（Lavf{m.group(1)}）"
    m = re.search(r"ffmpeg[^\d]*([\d.]+)", enc, re.I)
    if m:
        return f"FFmpeg {m.group(1)}"
    if re.fullmatch(r"[0-9a-f]{6,16}", enc, re.I):
        return f"—（客户端指纹 {enc}{('/' + plat) if plat else ''}）"
    return enc


# ─────────────────────────── 适配器基类 ───────────────────────────

class PlatformAdapter:
    """平台适配器基类：实现 matches() 与 resolve() 即可。"""

    meta: PlatformMeta = PlatformMeta("base", "Base", "#888", "")

    # 是否支持用 ffmpeg 直接录制非 HTTP 档位（RTMP 源流）作为分析样本
    samples_rtmp: bool = False

    def capture_sample(self, tier, dest_path, *, seconds: int = 20, max_bytes: int = 0,
                       headers: Optional[dict] = None, room_id: str = "") -> int:
        """抓取分析样本到 dest_path，返回写入字节数（默认不做任何事）。

        HTTP 平台沿用 CLI 内的高速下载；RTMP 等无法走 HTTP 的平台覆盖本方法
        （用 ffmpeg 录制 FLV），并将 samples_rtmp 置为 True。
        """
        return 0

    def matches(self, text: str) -> bool:
        """判断输入是否属于本平台（URL/短链/房间号）。"""
        raise NotImplementedError

    def resolve(self, text: str, ctx: Optional[dict] = None) -> LiveRoom:
        """解析直播间 → LiveRoom（含档位/账号/开播渠道）。"""
        raise NotImplementedError

    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> "str | None":
        """按需刷新某个档位的地址（URL 可能绑定会话/短时签名）。

        默认返回 None（表示不刷新，直接使用报告里的地址）。
        平台适配器可覆盖：例如 B 站需按 qn 重新请求接口，小红书可按后缀重新拼装。
        """
        return None

    # 便捷：探测档位集合（HTTP）
    def probe_tiers(self, tiers: list, headers: Optional[dict] = None,
                    sample_bytes: int = 1_200_000) -> list:
        for t in tiers:
            if t.protocol == "rtmp":
                res = ffprobe_url(t.url)
            else:
                res = http_probe(t.url, headers=headers, sample_bytes=sample_bytes)
            t.ok = bool(res.get("ok"))
            t.status = res.get("status") or res.get("error")
            t.codec = t.codec or res.get("codec")
            t.width = t.width or res.get("width")
            t.height = t.height or res.get("height")
            t.bitrate = t.bitrate or res.get("bitrate")
            if str(t.codec or "").lower().startswith("hevc"):
                t.playable = False
        return tiers
