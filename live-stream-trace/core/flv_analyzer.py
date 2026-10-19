"""
观众端 FLV 流分析脚本。
通过拉取直播 FLV 流，解析 onMetaData 和 FLV Header 中的编码器信息，
辅助识别推流端使用的软件。
"""

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse

from amf0 import parse_flv_header, parse_flv_script_tag, extract_encoder_hints, ci_get
from config import ROOM_ID, WEB_ROOM_ID, OUTPUT_DIR, COMMON_HEADERS, API_TIMEOUT, DOWNLOAD_MAX_BYTES, SOFTWARE_PATTERNS, CDN_PATTERNS, ENTER_URLS, ensure_dirs, get_extra_params
from network import http_get, download_bytes, download_response_headers


logger = logging.getLogger(__name__)


def build_headers(room_id: str | None = None, cookies: str | None = None) -> dict:
    """构造请求头，允许按房间号覆盖 Referer，支持注入 Cookie。"""
    headers = dict(COMMON_HEADERS)
    if room_id:
        headers["Referer"] = f"https://live.douyin.com/{room_id}"
    if cookies:
        headers["Cookie"] = cookies
    return headers


def normalize_flv_header(data: bytes):
    """将共享模块返回的 Header 结果适配为旧脚本结构。"""
    if len(data) < 9:
        return None

    parsed = parse_flv_header(data)
    if parsed is None:
        return {"error": "非 FLV 格式", "magic": data[:3]}

    flags = parsed.get("flags") or {}
    return {
        "format": parsed.get("format"),
        "version": parsed.get("version"),
        "has_audio": bool(flags.get("audio")),
        "has_video": bool(flags.get("video")),
        "header_size": parsed.get("length"),
    }


def get_stream_url(room_id: str, headers: dict, cookies: str | None = None, need_double_stream: bool = False):
    """从直播间页面获取 FLV 拉流地址。自动尝试多个 API 域名。"""
    params = {
        "app_id": "1128",
        "web_rid": room_id,
        "Room-Enter-User-Login-Ab": "0",
        "is_need_double_stream": "true" if need_double_stream else "false",
        "support_drm": "false",
        "Support-Degrade-Non-Encode-Stream": "false",
    }

    for enter_url in ENTER_URLS:
        domain = enter_url.split("//")[1].split("/")[0]
        logger.info("获取直播间信息 (域名: %s)", domain)
        response = http_get(enter_url, params=params, headers=headers, timeout=API_TIMEOUT)
        if response is None:
            logger.warning("域名 %s 请求失败，尝试下一个", domain)
            continue

        try:
            data = response.json()
        except Exception as exc:
            logger.warning("域名 %s 解析 JSON 失败: %s", domain, exc)
            continue

        # 检查鉴权错误
        if isinstance(data, dict) and data.get("status_code") == 10011:
            logger.warning("域名 %s 返回鉴权错误 (10011)，尝试下一个", domain)
            continue

        break
    else:
        logger.error("所有 API 域名均请求失败")
        return None

    try:
        room = data["data"]["data"][0]
        status = room.get("status")
        status_text = "直播中" if status == 2 else "已结束" if status == 4 else "未知"
        logger.info("房间状态: %s (%s)", status, status_text)

        if status != 2:
            logger.warning("当前未开播，无法获取流地址")
            return None

        stream_url = room["stream_url"]
        flv_pull_url = stream_url.get("flv_pull_url") or stream_url.get("flv_pull_url_params")

        if isinstance(flv_pull_url, dict):
            all_quality_urls = dict(flv_pull_url)
            for quality, url in flv_pull_url.items():
                logger.info("%s: %s...", quality, str(url)[:80])
            for key in ["FULL_HD1", "HD1", "SD1", "SD2"]:
                if key in flv_pull_url:
                    return {
                        "selected_url": flv_pull_url[key],
                        "stream_url_obj": stream_url,
                        "room": room,
                        "all_quality_urls": all_quality_urls,
                    }
            return {
                "selected_url": next(iter(flv_pull_url.values())),
                "stream_url_obj": stream_url,
                "room": room,
                "all_quality_urls": all_quality_urls,
            }

        if isinstance(flv_pull_url, str):
            return {
                "selected_url": flv_pull_url,
                "stream_url_obj": stream_url,
                "room": room,
                "all_quality_urls": {"DEFAULT": flv_pull_url},
            }
    except (KeyError, TypeError, IndexError) as exc:
        logger.warning("解析直播间响应失败: %s", exc)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump = OUTPUT_DIR / f"enter_response_{ts}.json"
        try:
            with open(dump, "w", encoding="utf-8") as file_obj:
                json.dump(data, file_obj, ensure_ascii=False, indent=2)
            logger.info("响应已保存: %s", dump)
        except OSError as e:
            logger.error("保存响应转储失败: %s", e)

    return None


def _inject_params(flv_url: str, scope: str) -> str:
    parsed = urlparse(flv_url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    params.update(get_extra_params(scope=scope))
    return urlunparse(parsed._replace(query=urlencode(params)))


def build_audio_only_url(flv_url: str) -> str:
    """从 FLV 流地址构造纯音频版本（从配置文件读取附加参数）。"""
    return _inject_params(flv_url, "audio_only")


def append_extra_params(flv_url: str) -> str:
    """向 FLV 流地址追加所有已启用的额外参数（scope=all）。"""
    return _inject_params(flv_url, "all")


def _parse_aac_profile(byte0: int) -> str:
    profiles = {1: "AAC-LC", 2: "AAC-LC", 3: "AAC-SSR", 4: "AAC-LTP", 5: "AAC-SBR", 6: "AAC-Scalable"}
    idx = (byte0 >> 3) & 0x1F
    return profiles.get(idx, f"profile({idx})")


def _parse_aac_sample_rate(byte0: int, byte1: int) -> str:
    rates = {0: "96000", 1: "88200", 2: "64000", 3: "48000", 4: "44100",
             5: "32000", 6: "24000", 7: "22050", 8: "16000", 9: "12000",
             10: "11025", 11: "8000", 12: "7350"}
    idx = ((byte0 & 0x07) << 1) | ((byte1 >> 7) & 0x01)
    return rates.get(idx, f"unknown({idx})")


def _parse_aac_channels(byte1: int) -> int:
    return (byte1 >> 3) & 0x0F


def parse_flv_tags(data, max_tags=50):
    """解析 FLV 标签，提取 script data、音视频编码信息。"""
    tags = []
    header = normalize_flv_header(data) or {}
    offset = int(header.get("header_size") or 9)
    offset += 4

    tag_count = 0
    while offset + 11 < len(data) and tag_count < max_tags:
        tag_type = data[offset]
        data_size = int.from_bytes(data[offset + 1:offset + 4], "big")
        timestamp = int.from_bytes(data[offset + 4:offset + 7], "big")
        timestamp_ext = data[offset + 7]

        tag_data_start = offset + 11
        tag_data_end = tag_data_start + data_size
        if tag_data_end > len(data):
            break

        tag_info = {
            "index": tag_count,
            "type": {8: "audio", 9: "video", 18: "script"}.get(tag_type, f"unknown({tag_type})"),
            "type_code": tag_type,
            "data_size": data_size,
            "timestamp": timestamp + (timestamp_ext << 24),
        }

        if tag_type == 18:
            raw = data[tag_data_start:tag_data_end]
            parsed_payload = parse_flv_script_tag(raw)
            if parsed_payload:
                if isinstance(parsed_payload.get("name"), str):
                    parsed = {parsed_payload["name"]: parsed_payload.get("value")}
                else:
                    parsed = parsed_payload
                tag_info["parsed"] = parsed
                tags.append(tag_info)
                if isinstance(parsed, dict) and "onMetaData" in parsed:
                    return tags

        if tag_type == 9 and data_size > 1:
            first_byte = data[tag_data_start]
            codec_id = first_byte & 0x0F
            frame_type = (first_byte >> 4) & 0x0F
            codec_names = {
                1: "JPEG", 2: "Sorenson H.263", 3: "Screen Video",
                4: "VP6", 5: "VP6 Alpha", 6: "Screen V2",
                7: "H.264/AVC", 8: "H.265/HEVC", 9: "AV1",
                10: "VP9", 11: "AV1 (enhanced)", 12: "H.265/HEVC",
            }
            tag_info["codec"] = codec_names.get(codec_id, f"unknown({codec_id})")
            tag_info["frame_type"] = {1: "keyframe", 2: "inter", 3: "disposable"}.get(frame_type)
            tags.append(tag_info)

        if tag_type == 8 and data_size > 1:
            first_byte = data[tag_data_start]
            sound_codec = first_byte & 0x0F
            sound_rate = (first_byte >> 2) & 0x03
            sound_size = (first_byte >> 1) & 0x01
            sound_type = first_byte & 0x01
            codec_names = {
                0: "PCM/PE", 1: "ADPCM", 2: "MP3", 4: "Nellymoser 16kHz",
                5: "Nellymoser 8kHz", 6: "Nellymoser", 7: "G.711 A-law",
                8: "G.711 mu-law", 10: "AAC", 11: "Speex", 13: "MP3 8kHz",
                14: "Device-specific",
            }
            rate_names = {0: "5.5kHz", 1: "11kHz", 2: "22kHz", 3: "44kHz"}
            tag_info["audio_codec"] = codec_names.get(sound_codec, f"unknown({sound_codec})")
            tag_info["sound_rate"] = rate_names.get(sound_rate)
            tag_info["sound_size"] = "16bit" if sound_size else "8bit"
            tag_info["sound_type"] = "stereo" if sound_type else "mono"

            if sound_codec == 10 and data_size > 2:
                aac_packet_type = data[tag_data_start + 1]
                raw_payload = data[tag_data_start + 2:tag_data_end]
                if aac_packet_type == 0:
                    tag_info["aac_type"] = "sequence_header"
                    tag_info["audio_specific_config"] = raw_payload.hex()
                    if len(raw_payload) >= 2:
                        tag_info["aac_profile"] = _parse_aac_profile(raw_payload[0])
                        tag_info["aac_sample_rate"] = _parse_aac_sample_rate(raw_payload[0], raw_payload[1])
                        tag_info["aac_channels"] = _parse_aac_channels(raw_payload[1])
                elif aac_packet_type == 1:
                    tag_info["aac_type"] = "frame"
                    tag_info["audio_payload_size"] = len(raw_payload)
            tags.append(tag_info)

        offset = tag_data_end + 4
        tag_count += 1

    return tags


def _format_nested_key_values(data, indent=0, prefix=""):
    """将嵌套结构格式化为可读 key=value 文本。"""
    lines = []
    pad = "  " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}=")
                lines.extend(_format_nested_key_values(value, indent + 1, full_key))
            else:
                lines.append(f"{pad}{key}={value}")
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            item_key = f"[{idx}]"
            full_key = f"{prefix}{item_key}" if prefix else item_key
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{item_key}=")
                lines.extend(_format_nested_key_values(value, indent + 1, full_key))
            else:
                lines.append(f"{pad}{item_key}={value}")
    else:
        lines.append(f"{pad}{prefix or 'value'}={data}")

    return lines


def save_full_onmetadata(parsed_data, output_dir, ts):
    """保存完整 onMetaData，不做字段过滤。"""
    if not parsed_data:
        return None

    metadata = parsed_data.get("onMetaData") if isinstance(parsed_data, dict) and "onMetaData" in parsed_data else parsed_data
    if metadata is None:
        return None

    full_file = output_dir / f"onmetadata_{ts}.json"
    readable_file = output_dir / f"onmetadata_readable_{ts}.txt"

    try:
        with open(full_file, "w", encoding="utf-8") as file_obj:
            json.dump(metadata, file_obj, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存 onMetaData JSON 失败: %s", e)

    try:
        with open(readable_file, "w", encoding="utf-8") as file_obj:
            file_obj.write("完整 onMetaData 可读视图\n")
            file_obj.write("=" * 50 + "\n")
            for line in _format_nested_key_values(metadata):
                file_obj.write(line + "\n")
    except OSError as e:
        logger.error("保存 onMetaData 可读文本失败: %s", e)

    logger.info("完整 onMetaData 已保存: %s", full_file)
    logger.info("可读 onMetaData 已保存: %s", readable_file)
    return {
        "metadata": metadata,
        "full_file": full_file,
        "readable_file": readable_file,
    }


def _try_parse_json_string(value):
    """尽量解析嵌入在 stream_url 中的 JSON 字符串。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _deep_normalize_structure(value):
    """递归展开 JSON 字符串，保留原始层级结构。"""
    value = _try_parse_json_string(value)
    if isinstance(value, dict):
        return {k: _deep_normalize_structure(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_normalize_structure(v) for v in value]
    return value


def _find_urls_in_structure(value, path="root", results=None):
    """递归扫描所有 URL，便于汇总协议信息。"""
    if results is None:
        results = []

    if isinstance(value, dict):
        for key, child in value.items():
            _find_urls_in_structure(child, f"{path}.{key}" if path else str(key), results)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _find_urls_in_structure(item, f"{path}[{idx}]", results)
    elif isinstance(value, str) and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        parsed = urlparse(value)
        results.append({
            "path": path,
            "url": value,
            "scheme": parsed.scheme,
            "host": parsed.netloc,
            "path_only": parsed.path,
            "query": dict(parse_qsl(parsed.query, keep_blank_values=True)),
        })
    return results


def parse_stream_url_full(stream_url_obj):
    """完整解析 web/enter 返回的 stream_url 结构。"""
    if not isinstance(stream_url_obj, dict):
        return {}

    normalized = _deep_normalize_structure(stream_url_obj)
    if not isinstance(normalized, dict):
        return {"raw": normalized, "all_urls": _find_urls_in_structure(normalized)}

    protocols = {}
    for key in [
        "flv_pull_url", "flv_pull_url_params", "hls_pull_url", "hls_pull_url_params",
        "rtmp_pull_url", "pull_urls", "stream_list", "dash_pull_url", "dash_pull_url_params",
    ]:
        value = normalized.get(key)
        if value is not None:
            protocols[key] = value

    discovered_urls = _find_urls_in_structure(normalized)
    quality_map = {}
    for item in discovered_urls:
        path_parts = item["path"].split(".")
        quality_candidates = [part for part in path_parts if re.match(r"^[A-Z_]+\d*$", part)]
        quality = quality_candidates[-1] if quality_candidates else None
        if quality:
            quality_map.setdefault(quality, []).append(item)

    return {
        "default_quality": normalized.get("default_quality"),
        "quality_description": normalized.get("quality_description"),
        "protocols": protocols,
        "quality_map": quality_map,
        "all_urls": discovered_urls,
        "raw": normalized,
    }


def _infer_cdn_provider(url_hints, headers_info):
    """根据域名和响应头推断 CDN/服务提供方。"""
    candidates = []
    if isinstance(url_hints, dict) and url_hints.get("domain"):
        candidates.append(str(url_hints["domain"]))
    if isinstance(headers_info, dict):
        candidates.extend(str(value) for value in headers_info.values())

    joined = " | ".join(candidates).lower()
    for keyword, provider in CDN_PATTERNS:
        if keyword.lower() in joined:
            return provider
    return "unknown"


def generate_encoder_fingerprint(hints, tags, url_hints, headers_info):
    """汇总编码器线索，生成结构化指纹摘要。"""
    hints = hints or {}
    tags = tags or []
    url_hints = url_hints or {}
    headers_info = headers_info or {}

    evidence = []
    push_software = "unknown"
    confidence = "low"

    def detect_software(value):
        if value is None:
            return None
        text = str(value).lower()
        for pattern, name in SOFTWARE_PATTERNS:
            if re.search(pattern, text):
                return name
        return None

    for source, value in hints.items():
        detected = detect_software(value)
        if detected:
            push_software = detected
            confidence = "high"
            evidence.append({"source": source, "value": value})
            break

    # 注意：不使用 url_hints 匹配「推流软件」——CDN 域名（如 live-source-play.xhscdn.com）
    # 只反映分发侧，不代表推流端软件；域名仅用于 CDN 归属判断。

    if push_software == "unknown":
        for source, value in headers_info.items():
            detected = detect_software(value)
            if detected:
                push_software = detected
                confidence = "medium"
                evidence.append({"source": f"header.{source}", "value": value})
                break

    if push_software == "unknown":
        for fallback_key in [
            "Encoder", "encoder", "ENCONDERVERSION", "software", "Software",
            "metadatacreator", "creator", "flashVer",
        ]:
            fallback_value = ci_get(hints, fallback_key)
            if fallback_value and str(fallback_value).strip():
                push_software = str(fallback_value).strip()
                confidence = "low"
                evidence.append({"source": fallback_key, "value": fallback_value, "note": "未命中已知规则，使用原始 encoder 字段值"})
                break

        if push_software == "unknown":
            for source, value in hints.items():
                text = str(value).strip()
                if len(text) < 4 or text.lower() in {"true", "false", "none"}:
                    continue
                try:
                    float(text)
                    continue
                except ValueError:
                    pass
                push_software = text
                confidence = "low"
                evidence.append({"source": source, "value": value, "note": "未命中已知规则，使用首个非数值字段值"})
                break

    if url_hints.get("url_third_party"):
        evidence.append({"source": "url", "value": "URL 包含 third"})

    for key, value in headers_info.items():
        if any(token in key.lower() for token in ["provider", "server", "cdn", "via"]):
            evidence.append({"source": f"header.{key}", "value": value})

    video_codec = "unknown"
    audio_codec = "unknown"
    tag_summary = []
    for tag in tags:
        tag_summary.append({
            "type": tag.get("type"),
            "timestamp": tag.get("timestamp"),
            "codec": tag.get("codec"),
            "audio_codec": tag.get("audio_codec"),
        })
        if video_codec == "unknown" and tag.get("codec"):
            video_codec = tag["codec"]
        if audio_codec == "unknown" and tag.get("audio_codec"):
            audio_codec = tag["audio_codec"]

    # Fallback: 当 tags 中没有 video/audio tag（onMetaData 提前返回）时，
    # 从 onMetaData 的 videocodecid/audiocodecid 字段映射到人类可读名称
    _video_codec_map = {
        1: "JPEG", 2: "Sorenson H.263", 3: "Screen Video",
        4: "VP6", 5: "VP6 Alpha", 6: "Screen V2",
        7: "H.264/AVC", 8: "H.265/HEVC", 9: "AV1",
        10: "VP9", 11: "AV1 (enhanced)", 12: "H.265/HEVC",
    }
    _audio_codec_map = {
        0: "PCM/PE", 1: "ADPCM", 2: "MP3", 4: "Nellymoser 16kHz",
        5: "Nellymoser 8kHz", 6: "Nellymoser", 7: "G.711 A-law",
        8: "G.711 mu-law", 10: "AAC", 11: "Speex", 13: "MP3 8kHz",
        14: "Device-specific",
    }
    if video_codec == "unknown":
        vcid = ci_get(hints, "videocodecid")
        if vcid is not None:
            try:
                video_codec = _video_codec_map.get(int(float(vcid)), f"unknown({vcid})")
            except (ValueError, TypeError):
                pass
    if audio_codec == "unknown":
        acid = ci_get(hints, "audiocodecid")
        if acid is not None:
            try:
                audio_codec = _audio_codec_map.get(int(float(acid)), f"unknown({acid})")
            except (ValueError, TypeError):
                pass

    width = ci_get(hints, "width")
    height = ci_get(hints, "height")
    framerate = ci_get(hints, "framerate")
    if framerate is None:
        framerate = ci_get(hints, "fps")

    resolution = "unknown"
    if width and height:
        try:
            resolution = f"{int(float(width))}x{int(float(height))}"
        except Exception:
            resolution = f"{width}x{height}"

    return {
        "push_software": push_software,
        "confidence": confidence,
        "evidence": evidence,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "resolution": resolution,
        "framerate": framerate,
        "is_third_party": bool(url_hints.get("url_third_party")),
        "cdn_provider": _infer_cdn_provider(url_hints, headers_info),
        "raw_hints": {
            "metadata_hints": hints,
            "tag_summary": tag_summary,
            "url_hints": url_hints,
            "headers_info": headers_info,
        },
    }


def ffprobe_stream(url, output_dir=None, ts=None):
    """使用 ffprobe 探测流元数据，返回解析后的结构化结果。

    ffprobe 直接连接流 URL，能获取：编码参数、分辨率、帧率、码率、FLV tags 等。
    作为 Python FLV 解析的补充，提供交叉验证。
    """
    if not shutil.which("ffprobe"):
        logger.warning("ffprobe 未安装或不在 PATH 中，跳过 ffprobe 探测")
        return None

    logger.info("ffprobe 探测流: %s", url[:80])
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-read_intervals", "%+5",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
        )
        if result.returncode != 0:
            logger.warning("ffprobe 返回非零退出码: %s", result.returncode)
            logger.debug("ffprobe stderr: %s", result.stderr[:500] if result.stderr else "")
            return None

        probe_data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe 探测超时 (20s)")
        return None
    except json.JSONDecodeError as exc:
        logger.warning("ffprobe 输出 JSON 解析失败: %s", exc)
        return None
    except Exception as exc:
        logger.warning("ffprobe 探测异常: %s", exc)
        return None

    summary = {
        "type": "ffprobe",
        "streams": [],
        "format_tags": {},
        "push_software": None,
        "device_info": None,
        "push_protocol": None,
        "hardware_encode": None,
    }

    for stream in probe_data.get("streams") or []:
        s = {
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "codec_long_name": stream.get("codec_long_name"),
            "profile": stream.get("profile"),
        }
        if stream.get("codec_type") == "video":
            s.update({
                "width": stream.get("width"),
                "height": stream.get("height"),
                "r_frame_rate": stream.get("r_frame_rate"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
                "pix_fmt": stream.get("pix_fmt"),
                "level": stream.get("level"),
                "has_b_frames": stream.get("has_b_frames"),
                "is_avc": stream.get("is_avc"),
            })
        elif stream.get("codec_type") == "audio":
            s.update({
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "bit_rate": stream.get("bit_rate"),
            })
        summary["streams"].append(s)

    fmt = probe_data.get("format") or {}
    tags = fmt.get("tags") or {}
    summary["format_tags"] = tags

    if tags.get("Encoder"):
        summary["push_software"] = tags["Encoder"]
    if tags.get("platform"):
        summary["device_info"] = {
            "platform": tags.get("platform"),
            "os_version": tags.get("os_version"),
            "model": tags.get("model"),
            "sdk_version": tags.get("sdk_version"),
        }
    if tags.get("push_protocol"):
        summary["push_protocol"] = tags["push_protocol"]
    if tags.get("is_hardware_encode") is not None:
        summary["hardware_encode"] = tags["is_hardware_encode"] == "1"

    logger.info("ffprobe 探测完成: %d streams, push_software=%s",
                len(summary["streams"]),
                summary["push_software"][:60] if summary["push_software"] else "N/A")

    if output_dir and ts:
        json_path = Path(output_dir) / f"ffprobe_{ts}.json"
        full_result = {
            "type": "ffprobe",
            "timestamp": ts,
            "stream_url": url,
            "summary": summary,
        }
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(full_result, f, ensure_ascii=False, indent=2, default=str)
            logger.info("ffprobe 结果已保存: %s", json_path)
            summary["_json_path"] = str(json_path)
        except OSError as exc:
            logger.warning("保存 ffprobe 结果失败: %s", exc)

    return summary


def download_stream_sample(url, headers, max_bytes=DOWNLOAD_MAX_BYTES):
    """下载流数据前 N 字节并尽量获取响应头信息。"""
    logger.info("拉取流数据 (最多 %s 字节)", max_bytes)
    response_headers = download_response_headers(url, headers=headers, timeout=API_TIMEOUT) or {}
    provider_hints = {}
    for key, value in response_headers.items():
        if any(token in key.lower() for token in ["provider", "server", "push", "stream", "encoder", "tool", "via", "cdn"]):
            provider_hints[key] = value

    if response_headers:
        logger.info("HTTP 响应头已获取")
        for key in [
            "Content-Type", "Server", "X-Push-Stream-App", "X-Push-Stream-Info",
            "X-TT-Provider", "Via", "X-Cache", "X-Provider", "X-Stream-Provider", "X-Live-Provider",
        ]:
            if response_headers.get(key):
                logger.info("%s: %s", key, response_headers[key])

    if provider_hints:
        logger.info("推流相关响应头: %s", provider_hints)

    result = download_bytes(url, headers=headers, max_bytes=max_bytes, timeout=API_TIMEOUT)
    if result is None:
        logger.warning("拉流失败")
        return None

    data, is_complete = result
    if not is_complete:
        logger.warning("拉流数据不完整 (已获取 %s 字节)", len(data))

    return {
        "data": data,
        "is_complete": is_complete,
        "headers_info": provider_hints,
        "response_headers": response_headers,
        "status_code": None,
    }


def download_stream_sample_timed(url: str, headers: dict, seconds: float) -> dict | None:
    """按时间限制下载 FLV 流数据，返回格式与 download_stream_sample 一致。"""
    logger.info("拉取流数据 (限时 %.1f 秒)", seconds)
    response_headers = download_response_headers(url, headers=headers, timeout=API_TIMEOUT) or {}
    provider_hints = {}
    for key, value in response_headers.items():
        if any(token in key.lower() for token in ["provider", "server", "push", "stream", "encoder", "tool", "via", "cdn"]):
            provider_hints[key] = value

    downloaded = bytearray()
    is_complete = True

    try:
        import requests as _requests
        with _requests.Session().get(url, headers=headers, timeout=API_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            start = time.monotonic()
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                if time.monotonic() - start >= seconds:
                    break
                downloaded.extend(chunk)
    except Exception as exc:
        logger.warning("限时拉流异常: %s", exc)
        if downloaded:
            is_complete = False
        else:
            return None

    if not downloaded:
        logger.warning("限时拉流无数据")
        return None

    logger.info("限时拉流: 获取 %d 字节 (%s)", len(downloaded), "完整" if is_complete else "部分")

    return {
        "data": bytes(downloaded),
        "is_complete": is_complete,
        "headers_info": provider_hints,
        "response_headers": response_headers,
        "status_code": None,
    }


def analyze_stream_url_patterns(url):
    """分析流 URL 本身的特征。"""
    logger.info("开始分析流 URL")
    hints = {}

    if "third" in url:
        hints["url_third_party"] = True
        logger.warning("URL 包含 'third'，可能为第三方推流")
    if "rtmp" in url.lower():
        hints["url_rtmp"] = True
        logger.info("URL 包含 'rtmp'")

    domain_match = re.search(r"https?://([^/]+)", url)
    if domain_match:
        domain = domain_match.group(1)
        hints["domain"] = domain
        logger.info("域名: %s", domain)
        for keyword, provider in CDN_PATTERNS:
            if keyword.lower() in domain.lower():
                logger.info("域名特征: %s", provider)

    if "?" in url:
        params_str = url.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in params_str.split("&") if "=" in part)
        for key in ["app", "flashVer", "encoder", "provider", "stream", "vhost"]:
            if key in params:
                hints[f"url_param_{key}"] = params[key]
                logger.info("参数 %s: %s", key, params[key])
    return hints


def _extract_all_audio_tags(data):
    """从 FLV 字节流中提取所有音频 tag 的原始 payload，返回结构化列表。"""
    results = []
    if len(data) < 13:
        return results
    offset = 9 + 4  # FLV header + PreviousTagSize0
    while offset + 11 < len(data):
        tag_type = data[offset]
        data_size = int.from_bytes(data[offset + 1:offset + 4], "big")
        timestamp = int.from_bytes(data[offset + 4:offset + 7], "big")
        timestamp_ext = data[offset + 7]
        tag_data_start = offset + 11
        tag_data_end = tag_data_start + data_size
        if tag_data_end > len(data):
            break
        if tag_type == 8 and data_size > 2:
            first_byte = data[tag_data_start]
            sound_codec = first_byte & 0x0F
            aac_packet_type = data[tag_data_start + 1]
            payload = data[tag_data_start + 2:tag_data_end]
            ts_ms = timestamp + (timestamp_ext << 24)
            results.append({
                "timestamp_ms": ts_ms,
                "sound_codec": sound_codec,
                "aac_packet_type": aac_packet_type,
                "payload_size": len(payload),
                "payload_md5": hashlib.md5(payload).hexdigest(),
                "payload_head_hex": payload[:16].hex() if payload else "",
            })
        offset = tag_data_end + 4
    return results


def analyze_audio_tracks(samples: dict[str, Path]) -> dict:
    """分析多个清晰度 FLV 样本的音频轨道相似度。

    Args:
        samples: {quality_name: flv_file_path} 映射

    Returns:
        包含各流音频配置、内容指纹、相似度矩阵的分析结果。
    """
    logger.info("开始音频轨道相似度分析，共 %d 个样本", len(samples))
    stream_analysis = {}

    for quality, flv_path in samples.items():
        if not flv_path.exists():
            logger.warning("音频分析: %s 文件不存在: %s", quality, flv_path)
            continue
        data = flv_path.read_bytes()
        if len(data) < 13:
            logger.warning("音频分析: %s 文件过小 (%d bytes)", quality, len(data))
            continue

        tags = _extract_all_audio_tags(data)
        if not tags:
            logger.warning("音频分析: %s 未找到音频 tag", quality)
            continue

        aac_config = None
        aac_raw = None
        frame_md5s = []
        frame_sizes = []
        for t in tags:
            if t["aac_packet_type"] == 0:
                aac_config = t["payload_head_hex"]
                aac_raw = bytes.fromhex(t["payload_head_hex"]) if t["payload_head_hex"] else None
            elif t["aac_packet_type"] == 1:
                frame_md5s.append(t["payload_md5"])
                frame_sizes.append(t["payload_size"])

        # 直接从 raw payload 解析 AudioSpecificConfig
        aac_details = {}
        if aac_raw and len(aac_raw) >= 2:
            aac_details = {
                "profile": _parse_aac_profile(aac_raw[0]),
                "sample_rate": _parse_aac_sample_rate(aac_raw[0], aac_raw[1]),
                "channels": _parse_aac_channels(aac_raw[1]),
                "audio_specific_config": aac_raw.hex(),
            }

        stream_analysis[quality] = {
            "aac_config": aac_details,
            "aac_config_fingerprint": aac_config,
            "total_audio_tags": len(tags),
            "aac_frames": len(frame_md5s),
            "frame_size_min": min(frame_sizes) if frame_sizes else 0,
            "frame_size_max": max(frame_sizes) if frame_sizes else 0,
            "frame_size_avg": round(sum(frame_sizes) / len(frame_sizes), 1) if frame_sizes else 0,
            "content_fingerprint": hashlib.md5(
                "".join(frame_md5s).encode()
            ).hexdigest() if frame_md5s else None,
            "all_frame_md5s": frame_md5s,
        }
        logger.info("音频分析: %s — %d 帧, AAC config=%s, 指纹=%s",
                     quality, len(frame_md5s),
                     aac_details.get("profile", "N/A"),
                     stream_analysis[quality]["content_fingerprint"][:12] if stream_analysis[quality]["content_fingerprint"] else "N/A")

    # 构建相似度矩阵
    qualities = list(stream_analysis.keys())
    similarity_matrix = {}
    for i, q1 in enumerate(qualities):
        similarity_matrix[q1] = {}
        a1 = stream_analysis[q1]
        for j, q2 in enumerate(qualities):
            if i == j:
                similarity_matrix[q1][q2] = 1.0
                continue
            a2 = stream_analysis[q2]
            score = _compute_audio_similarity(a1, a2)
            similarity_matrix[q1][q2] = round(score, 3)

    # 一致性判断
    all_same = all(
        similarity_matrix[q1][q2] >= 0.9
        for q1 in qualities for q2 in qualities if q1 != q2
    ) if len(qualities) >= 2 else True

    # 清理：移除大数据字段，只保留摘要
    for info in stream_analysis.values():
        info.pop("all_frame_md5s", None)

    result = {
        "streams": stream_analysis,
        "similarity_matrix": similarity_matrix,
        "consistent": all_same,
        "conclusion": "所有流的音频轨道内容一致（来自同一音频源）" if all_same
                      else "音频轨道内容存在差异，可能来自不同音频源或存在转码/混流",
    }
    logger.info("音频相似度分析完成: %s", result["conclusion"])
    return result


def _compute_audio_similarity(a1: dict, a2: dict) -> float:
    """计算两个流的音频相似度分数（0~1）。

    不同清晰度在不同时间抓取，整体 MD5 必然不同。
    判断依据：AAC 配置一致 + 帧级内容大量重叠。
    """
    scores = []

    # 1. AAC 配置一致（权重 0.3）
    fp1 = a1.get("aac_config_fingerprint")
    fp2 = a2.get("aac_config_fingerprint")
    if fp1 and fp2:
        scores.append((1.0 if fp1 == fp2 else 0.0, 0.3))

    # 2. 帧级指纹重叠率（权重 0.7）— 核心判断
    #    并发抓取的流大部分帧内容相同，少数尾部帧因时间偏移不同
    frames1 = set(a1.get("all_frame_md5s", []))
    frames2 = set(a2.get("all_frame_md5s", []))
    if frames1 and frames2:
        overlap = len(frames1 & frames2) / min(len(frames1), len(frames2))
        scores.append((overlap, 0.7))

    if not scores:
        return 0.0
    total_weight = sum(w for _, w in scores)
    return sum(s * w for s, w in scores) / total_weight


def analyze_payload(data, output_dir, json_output_dir, ts, stream_url, room_id, url_hints, headers_info, stream_url_struct_file=None):
    """分析已获取的 FLV 字节流，生成文本与 JSON 结果。"""
    raw_file = output_dir / f"flv_sample_{ts}.flv"
    try:
        with open(raw_file, "wb") as file_obj:
            file_obj.write(data)
        logger.info("原始数据已保存: %s (%s bytes)", raw_file, len(data))
    except OSError as e:
        logger.error("保存原始数据失败: %s", e)

    header = normalize_flv_header(data)
    if header:
        logger.info("FLV Header: %s", header)

    if header and header.get("format") != "FLV":
        logger.warning("非 FLV 格式，尝试搜索文本特征")
        text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data[:4096])
        for keyword in ["encoder", "flashVer", "FMLE", "OBS", "ffmpeg", "x264", "libx264", "bytedance", "douyin", "ByteMedia", "live", "provider"]:
            if keyword.lower() in text.lower():
                idx = text.lower().find(keyword.lower())
                snippet = text[max(0, idx - 20):idx + 60]
                logger.info("发现 %s: ...%s...", keyword, snippet)
        return {
            "raw_file": raw_file,
            "report_file": None,
            "json_path": None,
            "header": header,
            "tags": [],
            "metadata_hints": {},
            "metadata": None,
            "fingerprint": None,
            "stream_url": stream_url,
        }

    tags = parse_flv_tags(data)
    metadata_hints = {}
    metadata_save_result = None
    parsed_metadata = None
    if tags:
        for tag in tags:
            logger.info("Tag #%s: type=%s, size=%s, ts=%sms", tag["index"], tag["type"], tag["data_size"], tag["timestamp"])
            if "codec" in tag:
                logger.info("视频编码: %s, 帧类型: %s", tag["codec"], tag.get("frame_type"))
            if "audio_codec" in tag:
                logger.info("音频编码: %s", tag["audio_codec"])
            if "parsed" in tag:
                parsed = tag["parsed"]
                logger.info("Script Data: %s", parsed)
                metadata_save_result = metadata_save_result or save_full_onmetadata(parsed, output_dir, ts)
                parsed_metadata = metadata_save_result["metadata"] if metadata_save_result else parsed
                metadata_hints.update(extract_encoder_hints(parsed))
                if metadata_hints:
                    logger.info("编码器线索: %s", metadata_hints)
    else:
        logger.info("未解析到 FLV tags")

    fingerprint = generate_encoder_fingerprint(metadata_hints, tags, url_hints, headers_info)
    report_file = output_dir / f"flv_report_{ts}.txt"
    try:
        with open(report_file, "w", encoding="utf-8") as file_obj:
            file_obj.write("FLV 流分析报告\n")
            file_obj.write(f"直播流 URL: {stream_url}\n")
            file_obj.write(f"房间号: {room_id}\n")
            file_obj.write(f"内部房间 ID: {room_id}\n")
            file_obj.write(f"时间: {datetime.now()}\n")
            file_obj.write("=" * 50 + "\n\n")
            if stream_url_struct_file:
                file_obj.write(f"完整 stream_url 结构: {stream_url_struct_file}\n")
            if metadata_save_result:
                file_obj.write(f"完整 onMetaData JSON: {metadata_save_result['full_file']}\n")
                file_obj.write(f"可读 onMetaData: {metadata_save_result['readable_file']}\n")
            file_obj.write("\n")
            if header:
                file_obj.write("FLV Header:\n")
                for key, value in header.items():
                    file_obj.write(f"  {key}: {value}\n")
                file_obj.write("\n")
            if tags:
                for tag in tags:
                    file_obj.write(f"\nTag #{tag['index']}: {tag['type']}\n")
                    if "codec" in tag:
                        file_obj.write(f"  视频编码: {tag['codec']}\n")
                    if "audio_codec" in tag:
                        file_obj.write(f"  音频编码: {tag['audio_codec']}\n")
                    if "parsed" in tag:
                        file_obj.write(f"  Script Data: {json.dumps(tag['parsed'], ensure_ascii=False, indent=2)}\n")
            file_obj.write("\n编码器指纹摘要:\n")
            file_obj.write(f"  推流软件: {fingerprint['push_software']}\n")
            file_obj.write(f"  置信度: {fingerprint['confidence']}\n")
            file_obj.write(f"  视频编码: {fingerprint['video_codec']}\n")
            file_obj.write(f"  音频编码: {fingerprint['audio_codec']}\n")
            file_obj.write(f"  分辨率: {fingerprint['resolution']}\n")
            file_obj.write(f"  帧率: {fingerprint['framerate']}\n")
            file_obj.write(f"  第三方推流: {'是' if fingerprint['is_third_party'] else '否'}\n")
            file_obj.write(f"  CDN/服务提供方: {fingerprint['cdn_provider']}\n")
            if fingerprint["evidence"]:
                file_obj.write("  证据:\n")
                for item in fingerprint["evidence"]:
                    file_obj.write(f"    - {item['source']}: {item['value']}\n")
            file_obj.write("  原始线索:\n")
            file_obj.write(json.dumps(fingerprint["raw_hints"], ensure_ascii=False, indent=2))
            file_obj.write("\n")
    except OSError as e:
        logger.error("保存文本报告失败: %s", e)
        report_file = None

    json_result = {
        "type": "flv_analysis",
        "timestamp": ts,
        "room_id": room_id,
        "stream_url": stream_url,
        "header": header,
        "metadata": parsed_metadata,
        "encoder_hints": metadata_hints,
        "fingerprint": fingerprint,
        "resolution": fingerprint.get("resolution"),
        "video_codec": fingerprint.get("video_codec"),
        "audio_codec": fingerprint.get("audio_codec"),
        "stream_url_structure": str(stream_url_struct_file) if stream_url_struct_file else None,
        "raw_file": str(raw_file),
        "report_file": str(report_file),
    }
    json_path = json_output_dir / f"flv_analysis_{ts}.json"
    try:
        with open(json_path, "w", encoding="utf-8") as file_obj:
            json.dump(json_result, file_obj, ensure_ascii=False, indent=2, default=str)
        logger.info("报告已保存: %s", report_file)
        logger.info("JSON 已保存: %s", json_path)
    except OSError as e:
        logger.error("保存 JSON 结果失败: %s", e)
    return {
        "raw_file": raw_file,
        "report_file": report_file,
        "json_path": json_path,
        "header": header,
        "tags": tags,
        "metadata_hints": metadata_hints,
        "metadata": parsed_metadata,
        "fingerprint": fingerprint,
        "stream_url": stream_url,
    }


def main():
    parser = argparse.ArgumentParser(description="抖音直播 FLV 流分析器")
    parser.add_argument("--room-id", default=WEB_ROOM_ID, help="Web 房间号 (默认: %(default)s)")
    parser.add_argument("--url", help="直接分析指定 FLV 流地址")
    parser.add_argument("--file", help="分析本地 FLV 文件")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    ensure_dirs()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output_dir = output_dir  # 直接保存到 captures/，避免 captures/captures/ 双重嵌套

    headers = build_headers(args.room_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stream_url = args.url
    stream_url_obj = {}
    url_hints = {}
    headers_info = {}
    stream_url_struct_file = None

    if args.file:
        file_path = Path(args.file)
        data = file_path.read_bytes()
        logger.info("分析本地文件: %s", file_path)
    else:
        if not stream_url:
            stream_info = get_stream_url(args.room_id, headers)
            if not stream_info:
                print("\n[!] 无法获取流地址")
                print("[提示] 可能原因:")
                print("  1. 直播间未开播")
                print("  2. 需要登录 Cookie")
                print("  3. 需要补充 msToken / a_bogus 参数")
                return
            stream_url = stream_info["selected_url"]
            stream_url_obj = stream_info.get("stream_url_obj") or {}

        url_hints = analyze_stream_url_patterns(stream_url)
        stream_url_struct = parse_stream_url_full(stream_url_obj)
        if stream_url_struct:
            stream_url_struct_file = output_dir / f"stream_url_struct_{ts}.json"
            with open(stream_url_struct_file, "w", encoding="utf-8") as file_obj:
                json.dump(stream_url_struct, file_obj, ensure_ascii=False, indent=2)
            logger.info("完整 stream_url 结构已保存: %s", stream_url_struct_file)

        stream_sample = download_stream_sample(stream_url, headers)
        if not stream_sample:
            return
        data = stream_sample["data"]
        headers_info = stream_sample.get("headers_info") or {}

    result = analyze_payload(
        data=data,
        output_dir=output_dir,
        json_output_dir=json_output_dir,
        ts=ts,
        stream_url=stream_url or f"file://{args.file}",
        room_id=args.room_id,
        url_hints=url_hints,
        headers_info=headers_info,
        stream_url_struct_file=stream_url_struct_file,
    )

    fingerprint = result.get("fingerprint") or {}
    print("=" * 60)
    print("  FLV 直播流分析结果")
    print("=" * 60)
    print(f"房间号: {args.room_id}")
    print(f"流地址: {result.get('stream_url')}")
    if result.get("report_file"):
        print(f"文本报告: {result['report_file']}")
    if result.get("json_path"):
        print(f"JSON 报告: {result['json_path']}")
    if fingerprint:
        print(f"推流软件: {fingerprint.get('push_software', 'unknown')}")
        print(f"视频编码: {fingerprint.get('video_codec', 'unknown')}")
        print(f"音频编码: {fingerprint.get('audio_codec', 'unknown')}")
        print(f"分辨率: {fingerprint.get('resolution', 'unknown')}")


if __name__ == "__main__":
    main()
