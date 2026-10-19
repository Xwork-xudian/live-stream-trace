import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from typing import Any

from amf0 import ci_get
from config import (
    DOWNLOAD_MAX_BYTES,
    CDN_PATTERNS,
    SOFTWARE_PATTERNS,
    DUAL_STREAM_SAMPLE_DIR,
    DUAL_STREAM_MANIFEST_DIR,
)
from flv_analyzer import (
    download_stream_sample,
    normalize_flv_header,
    parse_flv_tags,
    generate_encoder_fingerprint,
    analyze_stream_url_patterns,
)
from network import http_get


logger = logging.getLogger(__name__)

_VOLATILE_QUERY_KEYS = {
    "token",
    "sign",
    "expire",
    "a_bogus",
    "mstoken",
    "ttcdn_dns",
    "wssecret",
    "wstime",
    "inner_tag",
    "sflage",
    "aliyungf",
    "flv_version",
    "bu_api",
    "wsstart",
    "wsend",
}

_METADATA_HINT_KEYS = (
    "encoder",
    "metadatacreator",
    "creator",
    "processingsoftware",
    "software",
    "tool",
    "flashver",
    "playerfactory",
    "playerversion",
    "server",
    "pushurl",
    "streamurl",
    "videocodecid",
    "audiocodecid",
    "width",
    "height",
    "framerate",
    "videodatarate",
    "audiodatarate",
    "duration",
    "filesize",
    "lastkeyframetimestamp",
)

_QUALITY_HEIGHT_EXPECTATIONS = {
    "FULL_HD1": 1080,
    "HD1": 720,
    "SD1": 480,
    "SD2": 360,
}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text or "unknown"


def _collect_metadata_hints(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}

    hints: dict[str, Any] = {}
    for key in _METADATA_HINT_KEYS:
        value = ci_get(metadata, key)
        if value is not None:
            hints[key] = value

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 32:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                normalized = re.sub(r"\s+", "", str(child_key)).lower().removeprefix("onmetadata.")
                if normalized in _METADATA_HINT_KEYS and normalized not in hints:
                    hints[normalized] = child_value
                walk(child_value, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(metadata)
    return hints


def _extract_metadata_from_tags(tags: list[dict[str, Any]]) -> dict[str, Any]:
    for tag in tags:
        parsed = tag.get("parsed")
        if not isinstance(parsed, dict):
            continue
        metadata = ci_get(parsed, "onMetaData")
        if isinstance(metadata, dict):
            return metadata
    return {}


def _normalize_video_codec_family(value: Any) -> str:
    if value is None:
        return "unknown"
    numeric = _safe_float(value)
    if numeric is not None:
        return {
            2: "Sorenson H.263",
            3: "Screen Video",
            4: "VP6",
            5: "VP6 Alpha",
            6: "Screen V2",
            7: "H.264/AVC",
            8: "H.265/HEVC",
            9: "AV1",
            10: "VP9",
            11: "AV1 (enhanced)",
            12: "H.265/HEVC",
        }.get(int(numeric), f"unknown({int(numeric)})")

    text = str(value).strip().lower()
    if "264" in text or "avc" in text or "h.264" in text:
        return "H.264/AVC"
    if "265" in text or "hevc" in text or "h.265" in text:
        return "H.265/HEVC"
    if "av1" in text:
        return "AV1"
    if "vp6" in text:
        return "VP6"
    if "sorenson" in text or "263" in text:
        return "Sorenson H.263"
    return _normalize_text(value)


def _normalize_audio_codec_family(value: Any) -> str:
    if value is None:
        return "unknown"
    numeric = _safe_float(value)
    if numeric is not None:
        return {
            2: "MP3",
            7: "G.711 A-law",
            8: "G.711 mu-law",
            10: "AAC",
            11: "Speex",
            14: "Device-specific",
        }.get(int(numeric), f"unknown({int(numeric)})")

    text = str(value).strip().lower()
    if "aac" in text:
        return "AAC"
    if "mp3" in text:
        return "MP3"
    if "speex" in text:
        return "Speex"
    if "g.711" in text or "711" in text:
        return "G.711"
    return _normalize_text(value)


def _resolution_from_hints(metadata_hints: dict[str, Any]) -> str:
    width = _safe_float(ci_get(metadata_hints, "width"))
    height = _safe_float(ci_get(metadata_hints, "height"))
    if width is None or height is None:
        return "unknown"
    return f"{int(width)}x{int(height)}"


def _quality_rank(quality: str) -> tuple[int, str]:
    order = {"FULL_HD1": 0, "HD1": 1, "SD1": 2, "SD2": 3}
    return order.get(quality, 99), quality


def _make_signal(strength: str, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "strength": strength,
        "code": code,
        "description": message,
        "details": details,
    }


def _make_suppressor(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "description": message,
        "details": details,
    }


def normalize_stream_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url or "")
    stable_query: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _VOLATILE_QUERY_KEYS:
            continue
        stable_query[key] = value

    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": parsed.path,
        "stable_query": stable_query,
        "raw": url,
    }


def extract_provider_family(url: str, headers_info: dict[str, Any]) -> str:
    host = urlparse(url or "").netloc
    candidates = [host]
    if isinstance(headers_info, dict):
        candidates.extend(str(value) for value in headers_info.values())

    haystack = " | ".join(candidates).lower()
    for keyword, provider in CDN_PATTERNS:
        if keyword.lower() in haystack:
            return provider
    return "unknown"


def extract_encoder_family(fingerprint: dict[str, Any]) -> str:
    push_software = _normalize_text(fingerprint.get("push_software"))
    normalized = push_software.lower()
    for pattern, name in SOFTWARE_PATTERNS:
        if re.search(pattern, normalized):
            return name
    return push_software


def build_flv_signature(
    data: bytes,
    stream_url: str,
    url_hints: dict[str, Any],
    headers_info: dict[str, Any],
    quality: str | None = None,
) -> dict[str, Any]:
    # 校验 FLV magic bytes，非 FLV 数据不应继续解析
    is_valid_flv = len(data) >= 3 and data[:3] == b"FLV"
    if not is_valid_flv:
        logger.warning("build_flv_signature: 数据不是有效 FLV（前 3 字节: %s）", data[:6].hex() if data else "empty")
        return {
            "quality": quality,
            "header": {},
            "tags": [],
            "fingerprint": {},
            "metadata_hints": {},
            "metadata": {},
            "provider_family": extract_provider_family(stream_url, headers_info),
            "encoder_family": "unknown",
            "video_codec": "unknown",
            "audio_codec": "unknown",
            "normalized_url": normalize_stream_url(stream_url),
            "error": "not_valid_flv",
        }
    header = normalize_flv_header(data)
    tags = parse_flv_tags(data)
    metadata = _extract_metadata_from_tags(tags)
    metadata_hints = _collect_metadata_hints(metadata)
    fingerprint = generate_encoder_fingerprint(metadata_hints, tags, url_hints or {}, headers_info or {})

    return {
        "quality": quality,
        "header": header,
        "tags": tags,
        "fingerprint": fingerprint,
        "metadata_hints": metadata_hints,
        "metadata": metadata,
        "provider_family": extract_provider_family(stream_url, headers_info),
        "encoder_family": extract_encoder_family(fingerprint),
        "video_codec": _normalize_video_codec_family(fingerprint.get("video_codec")),
        "audio_codec": _normalize_audio_codec_family(fingerprint.get("audio_codec")),
        "normalized_url": normalize_stream_url(stream_url),
    }


def compare_quality_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    suppressors: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    for index, left in enumerate(samples):
        for right in samples[index + 1:]:
            left_quality = left.get("quality") or "unknown"
            right_quality = right.get("quality") or "unknown"

            left_url = left.get("normalized_url") or {}
            right_url = right.get("normalized_url") or {}
            left_hints = left.get("metadata_hints") or {}
            right_hints = right.get("metadata_hints") or {}

            encoder_mismatch = left.get("encoder_family") != right.get("encoder_family")
            provider_mismatch = left.get("provider_family") != right.get("provider_family")
            video_codec_mismatch = left.get("video_codec") != right.get("video_codec")
            host_mismatch = left_url.get("host") != right_url.get("host")
            path_mismatch = left_url.get("path") != right_url.get("path")

            comparison = {
                "left_quality": left_quality,
                "right_quality": right_quality,
                "encoder_family": {
                    "left": left.get("encoder_family"),
                    "right": right.get("encoder_family"),
                    "match": not encoder_mismatch,
                },
                "provider_family": {
                    "left": left.get("provider_family"),
                    "right": right.get("provider_family"),
                    "match": not provider_mismatch,
                },
                "video_codec": {
                    "left": left.get("video_codec"),
                    "right": right.get("video_codec"),
                    "match": not video_codec_mismatch,
                },
                "url": {
                    "left_host": left_url.get("host"),
                    "right_host": right_url.get("host"),
                    "left_path": left_url.get("path"),
                    "right_path": right_url.get("path"),
                    "host_match": not host_mismatch,
                    "path_match": not path_mismatch,
                },
            }
            comparisons.append(comparison)

            if encoder_mismatch and provider_mismatch:
                signals.append(_make_signal(
                    "STRONG",
                    "encoder_provider_contradiction",
                    f"不同清晰度样本出现推流软件与 CDN 家族双重矛盾: {left_quality} vs {right_quality}",
                    comparison,
                ))
            elif encoder_mismatch:
                signals.append(_make_signal(
                    "MEDIUM",
                    "encoder_family_mismatch",
                    f"不同清晰度样本的推流软件指纹不一致: {left_quality} vs {right_quality}",
                    comparison,
                ))
            elif provider_mismatch:
                signals.append(_make_signal(
                    "MEDIUM",
                    "provider_family_mismatch",
                    f"不同清晰度样本的 CDN/提供方指纹不一致: {left_quality} vs {right_quality}",
                    comparison,
                ))

            if left_quality == right_quality and video_codec_mismatch:
                signals.append(_make_signal(
                    "STRONG",
                    "same_quality_codec_conflict",
                    f"相同清晰度 {left_quality} 的视频编码发生冲突",
                    comparison,
                ))

            if host_mismatch and not path_mismatch:
                signals.append(_make_signal(
                    "WEAK",
                    "url_host_mismatch_same_path",
                    f"不同清晰度落在不同 host，但 path 一致: {left_quality} vs {right_quality}",
                    comparison,
                ))

            left_resolution = _resolution_from_hints(left_hints)
            right_resolution = _resolution_from_hints(right_hints)
            bitrate_changed = ci_get(left_hints, "videodatarate") != ci_get(right_hints, "videodatarate")
            framerate_changed = ci_get(left_hints, "framerate") != ci_get(right_hints, "framerate")
            resolution_changed = left_resolution != right_resolution
            only_quality_diff = (
                not encoder_mismatch
                and not provider_mismatch
                and not video_codec_mismatch
                and not host_mismatch
                and not path_mismatch
                and (resolution_changed or bitrate_changed or framerate_changed)
            )
            if only_quality_diff:
                suppressors.append(_make_suppressor(
                    "quality_level_difference",
                    f"{left_quality} 与 {right_quality} 仅体现为常规清晰度差异",
                    {
                        "left_resolution": left_resolution,
                        "right_resolution": right_resolution,
                        "left_bitrate": ci_get(left_hints, "videodatarate"),
                        "right_bitrate": ci_get(right_hints, "videodatarate"),
                        "left_framerate": ci_get(left_hints, "framerate"),
                        "right_framerate": ci_get(right_hints, "framerate"),
                    },
                ))

            same_normalized_url = (
                left_url.get("host") == right_url.get("host")
                and left_url.get("path") == right_url.get("path")
                and left_url.get("stable_query") == right_url.get("stable_query")
            )
            raw_differs = left_url.get("raw") != right_url.get("raw")
            if same_normalized_url and raw_differs:
                suppressors.append(_make_suppressor(
                    "cdn_cache_token_diff",
                    f"{left_quality} 与 {right_quality} 的差异仅体现在易变 CDN token",
                    {
                        "left_raw": left_url.get("raw"),
                        "right_raw": right_url.get("raw"),
                        "normalized": {
                            "host": left_url.get("host"),
                            "path": left_url.get("path"),
                            "stable_query": left_url.get("stable_query"),
                        },
                    },
                ))

    return {
        "signals": signals,
        "suppressors": suppressors,
        "comparisons": comparisons,
    }


def run_consistency_checks(flv_signature: dict[str, Any]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    suppressors: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    quality = flv_signature.get("quality") or "unknown"
    metadata_hints = flv_signature.get("metadata_hints") or {}
    header = flv_signature.get("header") or {}
    tags = flv_signature.get("tags") or []

    first_video = next((tag for tag in tags if tag.get("type") == "video" and tag.get("codec")), None)
    first_audio = next((tag for tag in tags if tag.get("type") == "audio" and tag.get("audio_codec")), None)
    has_video_tag = first_video is not None
    has_audio_tag = first_audio is not None

    metadata_video_codec = _normalize_video_codec_family(ci_get(metadata_hints, "videocodecid"))
    actual_video_codec = _normalize_video_codec_family(first_video.get("codec") if first_video else None)
    # 仅当样本中有实际 video tag 时才比较；否则无法判断一致性
    video_match = metadata_video_codec == "unknown" or actual_video_codec == "unknown" or metadata_video_codec == actual_video_codec
    if not has_video_tag:
        video_match = True  # 样本不完整，无法比较
    checks.append({
        "check": "metadata_video_codec_vs_actual",
        "passed": video_match,
        "metadata": metadata_video_codec,
        "actual": actual_video_codec,
    })
    if not video_match:
        signals.append(_make_signal(
            "MEDIUM",
            "metadata_video_codec_mismatch",
            f"{quality} 的 onMetaData videocodecid 与实际视频 tag 编码不一致",
            {"quality": quality, "metadata": metadata_video_codec, "actual": actual_video_codec},
        ))

    metadata_audio_codec = _normalize_audio_codec_family(ci_get(metadata_hints, "audiocodecid"))
    actual_audio_codec = _normalize_audio_codec_family(first_audio.get("audio_codec") if first_audio else None)
    audio_match = metadata_audio_codec == "unknown" or actual_audio_codec == "unknown" or metadata_audio_codec == actual_audio_codec
    if not has_audio_tag:
        audio_match = True  # 样本不完整，无法比较
    checks.append({
        "check": "metadata_audio_codec_vs_actual",
        "passed": audio_match,
        "metadata": metadata_audio_codec,
        "actual": actual_audio_codec,
    })
    if not audio_match:
        signals.append(_make_signal(
            "MEDIUM",
            "metadata_audio_codec_mismatch",
            f"{quality} 的 onMetaData audiocodecid 与实际音频 tag 编码不一致",
            {"quality": quality, "metadata": metadata_audio_codec, "actual": actual_audio_codec},
        ))

    expected_height = _QUALITY_HEIGHT_EXPECTATIONS.get(quality)
    actual_height = _safe_float(ci_get(metadata_hints, "height"))
    quality_height_match = True
    if expected_height is not None and actual_height is not None:
        tolerance = min(int(expected_height * 0.15), 60)
        quality_height_match = actual_height >= max(expected_height - tolerance, 1)
    checks.append({
        "check": "metadata_resolution_vs_quality",
        "passed": quality_height_match,
        "quality": quality,
        "metadata_width": ci_get(metadata_hints, "width"),
        "metadata_height": ci_get(metadata_hints, "height"),
        "expected_min_height": expected_height,
    })
    if not quality_height_match:
        signals.append(_make_signal(
            "MEDIUM",
            "quality_resolution_mismatch",
            f"{quality} 的 onMetaData 分辨率低于该清晰度的预期",
            {
                "quality": quality,
                "metadata_width": ci_get(metadata_hints, "width"),
                "metadata_height": ci_get(metadata_hints, "height"),
                "expected_min_height": expected_height,
            },
        ))

    # Header audio flag vs actual tags
    # 仅当样本中确实包含 audio tag 时才检查一致性；样本不完整时跳过
    if has_audio_tag:
        header_audio_match = header.get("has_audio") in {None, has_audio_tag}
        checks.append({
            "check": "header_has_audio_vs_tags",
            "passed": header_audio_match,
            "header_has_audio": header.get("has_audio"),
            "has_audio_tag": has_audio_tag,
        })
        if not header_audio_match:
            signals.append(_make_signal(
                "MEDIUM",
                "header_audio_flag_mismatch",
                f"{quality} 的 FLV Header has_audio 与实际 tags 不一致",
                {"quality": quality, "header_has_audio": header.get("has_audio"), "has_audio_tag": has_audio_tag},
            ))
    else:
        checks.append({
            "check": "header_has_audio_vs_tags",
            "passed": True,
            "note": "sample_too_small_no_audio_tags",
        })

    # Header video flag vs actual tags
    if has_video_tag:
        header_video_match = header.get("has_video") in {None, has_video_tag}
        checks.append({
            "check": "header_has_video_vs_tags",
            "passed": header_video_match,
            "header_has_video": header.get("has_video"),
            "has_video_tag": has_video_tag,
        })
        if not header_video_match:
            signals.append(_make_signal(
                "MEDIUM",
                "header_video_flag_mismatch",
                f"{quality} 的 FLV Header has_video 与实际 tags 不一致",
                {"quality": quality, "header_has_video": header.get("has_video"), "has_video_tag": has_video_tag},
            ))
    else:
        checks.append({
            "check": "header_has_video_vs_tags",
            "passed": True,
            "note": "sample_too_small_no_video_tags",
        })

    return {
        "signals": signals,
        "suppressors": suppressors,
        "checks": checks,
    }


def score_dual_stream_risk(analysis: dict[str, Any]) -> dict[str, Any]:
    multi_quality = analysis.get("multi_quality") or {}
    consistency = analysis.get("consistency") or {}
    temporal = analysis.get("temporal") or {}
    double_mode = analysis.get("double_mode") or {}
    protocols = analysis.get("protocols") or {}

    signals = list(multi_quality.get("signals") or [])
    for item in consistency.get("per_sample") or []:
        signals.extend(item.get("signals") or [])
    # 时序监控信号
    temporal_signals = temporal.get("signals") or []
    signals.extend(temporal_signals)
    # 双模式对比信号
    signals.extend(double_mode.get("signals") or [])
    # 跨协议对比信号
    signals.extend(protocols.get("signals") or [])

    suppressor_count = len(multi_quality.get("suppressors") or []) + sum(
        len(item.get("suppressors") or []) for item in (consistency.get("per_sample") or [])
    )

    strong = sum(1 for signal in signals if signal.get("strength") == "STRONG")
    medium = sum(1 for signal in signals if signal.get("strength") == "MEDIUM")
    weak = sum(1 for signal in signals if signal.get("strength") == "WEAK")

    if strong:
        verdict = "high"
        score = min(100, 85 + strong * 5 + medium * 2)
        reasoning = f"存在 {strong} 个强信号，已满足高风险判定。"
    elif medium >= 2 or (medium >= 1 and weak >= 2):
        verdict = "medium"
        score = min(79, 55 + medium * 8 + weak * 4)
        reasoning = f"存在 {medium} 个中信号和 {weak} 个弱信号，达到中风险阈值。"
    elif medium == 1 or weak >= 2:
        verdict = "low"
        score = min(49, 25 + medium * 15 + weak * 6)
        reasoning = f"存在 {medium} 个中信号和 {weak} 个弱信号，提示低风险异常。"
    else:
        verdict = "none"
        score = 0
        reasoning = "未发现有效双流异常信号。"
        if suppressor_count:
            reasoning += f" 当前仅命中 {suppressor_count} 个抑制项。"

    return {
        "verdict": verdict,
        "score": score,
        "reasoning": reasoning,
    }


def _compare_double_stream_modes(
    stream_url_obj_double: dict[str, Any],
    stream_url_obj_normal: dict[str, Any],
    headers: dict[str, Any],
) -> dict[str, Any]:
    """对比 need_double_stream=true 和 false 两种模式返回的流地址。

    检查:
    1. URL 集是否有差异（不同 host/path → 强信号）
    2. 如果有额外 URL，下载样本对比 encoder/provider 指纹
    """
    signals: list[dict[str, Any]] = []
    suppressors: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    urls_double = _extract_quality_urls(stream_url_obj_double)
    urls_normal = _extract_quality_urls(stream_url_obj_normal)

    if not urls_double or not urls_normal:
        details["note"] = "insufficient_urls"
        details["double_stream_qualities"] = list(urls_double.keys())
        details["normal_qualities"] = list(urls_normal.keys())
        return {"signals": signals, "suppressors": suppressors, "details": details}

    # 对比同清晰度 URL 的归一化结构
    common_qualities = set(urls_double.keys()) & set(urls_normal.keys())
    only_in_double = set(urls_double.keys()) - set(urls_normal.keys())
    only_in_normal = set(urls_normal.keys()) - set(urls_double.keys())

    details["common_qualities"] = sorted(common_qualities)
    details["only_in_double"] = sorted(only_in_double)
    details["only_in_normal"] = sorted(only_in_normal)

    url_structure_changes: list[str] = []
    for quality in sorted(common_qualities):
        norm_double = normalize_stream_url(urls_double[quality])
        norm_normal = normalize_stream_url(urls_normal[quality])
        host_match = norm_double.get("host") == norm_normal.get("host")
        path_match = norm_double.get("path") == norm_normal.get("path")
        if not host_match or not path_match:
            url_structure_changes.append(quality)
            details[f"{quality}_double_url"] = norm_double
            details[f"{quality}_normal_url"] = norm_normal

    if url_structure_changes:
        signals.append(_make_signal(
            "STRONG",
            "double_mode_url_divergence",
            f"双模式对比: {', '.join(url_structure_changes)} 清晰度在 double_stream=true/false 下返回不同 URL 结构",
            {"changed_qualities": url_structure_changes},
        ))

    # 对比 only_in_double 的样本（可能有额外的第二路流）
    if only_in_double:
        details["extra_in_double_note"] = f"double_stream=true 模式额外返回了 {len(only_in_double)} 个清晰度: {sorted(only_in_double)}"
        # 尝试下载 extra quality 样本，与 normal 模式同清晰度对比
        for quality in sorted(only_in_double):
            url = urls_double[quality]
            sample = download_stream_sample(url, headers, max_bytes=DOWNLOAD_MAX_BYTES)
            if not sample or not sample.get("data"):
                details[f"extra_{quality}_error"] = "download_failed"
                continue
            url_hints = analyze_stream_url_patterns(url)
            sig = build_flv_signature(
                data=sample["data"],
                stream_url=url,
                url_hints=url_hints,
                headers_info=sample.get("headers_info") or {},
                quality=f"double_extra_{quality}",
            )
            details[f"extra_{quality}_encoder"] = sig.get("encoder_family")
            details[f"extra_{quality}_provider"] = sig.get("provider_family")

    if not url_structure_changes and not only_in_double and not only_in_normal:
        suppressors.append({
            "type": "double_mode_urls_identical",
            "description": "双模式对比: need_double_stream=true 和 false 返回完全相同的 URL 集",
        })

    return {"signals": signals, "suppressors": suppressors, "details": details}


def _run_temporal_polls(
    primary_quality: str,
    primary_url: str,
    headers: dict[str, Any],
    temporal_polls: int,
    temporal_interval: int,
) -> dict[str, Any]:
    """对指定清晰度做间隔采样，检测中途换流。

    返回 {"events": [...], "polls": [...], "signals": [...]}
    events 是连续两轮以上确认的变化；polls 是每轮快照。
    """
    polls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    effective_polls = temporal_polls if temporal_polls > 0 else 2
    overall_deadline = time.monotonic() + effective_polls * (temporal_interval + 60)

    prev_signature: dict[str, Any] | None = None
    pending_changes: dict[str, Any] | None = None  # 未确认的变化

    for poll_index in range(1, effective_polls + 1):
        if time.monotonic() > overall_deadline:
            logger.warning("时序监控达到总超时，停止后续采样")
            break

        logger.info(
            "时序监控: 第 %d/%d 轮，等待 %d 秒后采样...",
            poll_index, effective_polls, temporal_interval,
        )
        time.sleep(temporal_interval)

        sample = download_stream_sample(primary_url, headers, max_bytes=DOWNLOAD_MAX_BYTES)
        if not sample or not sample.get("data"):
            logger.warning("时序监控第 %d 轮: 下载失败（URL 可能已过期）", poll_index)
            polls.append({"poll": poll_index, "error": "download_failed", "ts": datetime.now().isoformat()})
            break  # URL 过期，停止后续 poll

        url_hints = analyze_stream_url_patterns(primary_url)
        signature = build_flv_signature(
            data=sample["data"],
            stream_url=primary_url,
            url_hints=url_hints,
            headers_info=sample.get("headers_info") or {},
            quality=primary_quality,
        )

        poll_snapshot = {
            "poll": poll_index,
            "ts": datetime.now().isoformat(),
            "encoder_family": signature.get("encoder_family"),
            "provider_family": signature.get("provider_family"),
            "video_codec": signature.get("video_codec"),
            "audio_codec": signature.get("audio_codec"),
            "normalized_url": signature.get("normalized_url"),
        }
        polls.append(poll_snapshot)

        if prev_signature is not None:
            changes: list[str] = []
            for field in ("encoder_family", "provider_family", "video_codec", "audio_codec"):
                prev_val = prev_signature.get(field)
                curr_val = signature.get(field)
                if prev_val != curr_val and prev_val != "unknown" and curr_val != "unknown":
                    changes.append(field)

            url_changed = (
                prev_signature.get("normalized_url", {}).get("host") != signature.get("normalized_url", {}).get("host")
                or prev_signature.get("normalized_url", {}).get("path") != signature.get("normalized_url", {}).get("path")
            )
            if url_changed:
                changes.append("url_structure")

            if changes:
                change_record = {
                    "poll": poll_index,
                    "changes": changes,
                    "prev": {k: prev_signature.get(k) for k in changes if k != "url_structure"},
                    "curr": {k: signature.get(k) for k in changes if k != "url_structure"},
                }

                # 需要连续两轮确认
                if pending_changes and set(pending_changes.get("changes", [])) & set(changes):
                    # 与上一轮变化有交集 → 确认为持续变化
                    confirmed_fields = list(set(pending_changes.get("changes", [])) & set(changes))
                    event = {
                        "poll_start": pending_changes["poll"],
                        "poll_end": poll_index,
                        "confirmed_changes": confirmed_fields,
                        "prev": pending_changes.get("prev", {}),
                        "curr": change_record.get("curr", {}),
                    }
                    events.append(event)

                    # 核心字段变化 → STRONG 信号
                    core_fields = {"encoder_family", "provider_family", "video_codec"}
                    if core_fields & set(confirmed_fields):
                        signals.append(_make_signal(
                            "STRONG",
                            "temporal_stream_identity_change",
                            f"时序监控检测到流身份持续变化: {', '.join(confirmed_fields)}",
                            event,
                        ))
                    else:
                        signals.append(_make_signal(
                            "MEDIUM",
                            "temporal_stream_change",
                            f"时序监控检测到流属性变化: {', '.join(confirmed_fields)}",
                            event,
                        ))
                    pending_changes = None
                else:
                    pending_changes = change_record
            else:
                # 无变化，清除未确认记录
                pending_changes = None

        prev_signature = signature

    return {"events": events, "polls": polls, "signals": signals}


# ── HLS/DASH 跨协议对比 ────────────────────────────────────────────────


def _extract_protocol_urls(
    stream_url_obj: dict[str, Any] | None, protocol_keys: tuple[str, ...]
) -> dict[str, str]:
    """从 stream_url_obj 提取指定协议的 quality→URL 映射。"""
    if not isinstance(stream_url_obj, dict):
        return {}
    result: dict[str, str] = {}
    for source_key in protocol_keys:
        payload = stream_url_obj.get(source_key)
        if not isinstance(payload, dict):
            continue
        for quality, value in payload.items():
            if isinstance(value, str) and value.strip():
                result.setdefault(str(quality), value)
            elif isinstance(value, dict):
                for nested_key in ("url", "main", "pull_url"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        result.setdefault(str(quality), nested)
                        break
    return result


def _fetch_manifest(url: str, headers: dict[str, str]) -> str | None:
    """抓取 manifest 内容 (m3u8 / mpd)。"""
    try:
        resp = http_get(url, headers=headers, timeout=10)
        if resp and resp.status_code == 200:
            return resp.text
        logger.warning("抓取 manifest 失败: status=%s url=%s", getattr(resp, 'status_code', None), url[:80])
    except Exception as e:
        logger.warning("抓取 manifest 异常: %s url=%s", e, url[:80])
    return None


def _parse_m3u8(content: str) -> dict[str, Any]:
    """解析 HLS m3u8 playlist，提取 stream variants 信息。

    返回:
        {
            "is_master": bool,
            "variants": [{"bandwidth": int, "resolution": str, "codecs": str, "url": str}],
            "segment_count": int,  # 非 master 时
            "target_duration": int | None,
        }
    """
    lines = content.strip().splitlines()
    variants: list[dict[str, Any]] = []
    segments: list[str] = []
    target_duration = None
    is_master = False

    current_info: dict[str, Any] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = int(line.split(":")[1])
            except (ValueError, IndexError):
                pass

        elif line.startswith("#EXT-X-STREAM-INF:"):
            is_master = True
            attrs_str = line.split(":", 1)[1]
            attrs: dict[str, str] = {}
            # 简单解析 KEY=VALUE 对（处理引号内逗号）
            for m in re.finditer(r'([A-Z0-9_-]+)=("(?:[^"\\]|\\.)*"|[^,]*)', attrs_str):
                key, val = m.group(1), m.group(2)
                attrs[key] = val.strip('"')
            current_info = {
                "bandwidth": int(attrs.get("BANDWIDTH", 0)),
                "resolution": attrs.get("RESOLUTION", ""),
                "codecs": attrs.get("CODECS", ""),
            }

        elif line.startswith("#EXTINF:"):
            pass  # segment duration line

        elif line.startswith("#"):
            pass  # other tags

        elif not line.startswith("#"):
            # URL line
            if current_info:
                current_info["url"] = line
                variants.append(current_info)
                current_info = {}
            else:
                segments.append(line)

    result: dict[str, Any] = {
        "is_master": is_master,
        "target_duration": target_duration,
    }
    if is_master:
        result["variants"] = variants
    else:
        result["segment_count"] = len(segments)
        result["segments_sample"] = segments[:5]
    return result


def _parse_mpd(content: str) -> dict[str, Any]:
    """解析 DASH MPD manifest，提取 adaptation set 信息。

    返回:
        {
            "periods": int,
            "video_representations": [{"id": str, "codec": str, "bandwidth": int, "width": int, "height": int}],
            "audio_representations": [{"id": str, "codec": str, "bandwidth": int, "samplerate": int}],
        }
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {"error": "xml_parse_failed"}

    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    periods = root.findall(".//mpd:Period", ns) or root.findall(".//Period")

    video_reps: list[dict[str, Any]] = []
    audio_reps: list[dict[str, Any]] = []

    for period in periods:
        # Video adaptations
        for as_elem in period.findall(".//mpd:AdaptationSet", ns) or period.findall(".//AdaptationSet"):
            content_type = (as_elem.get("contentType") or as_elem.get("mimeType") or "").lower()
            for rep in as_elem.findall("mpd:Representation", ns) or as_elem.findall("Representation"):
                rep_info = {
                    "id": rep.get("id", ""),
                    "codec": rep.get("codecs", ""),
                    "bandwidth": int(rep.get("bandwidth", 0)),
                }
                if content_type.startswith("video") or rep.get("width"):
                    rep_info["width"] = int(rep.get("width", 0))
                    rep_info["height"] = int(rep.get("height", 0))
                    video_reps.append(rep_info)
                elif content_type.startswith("audio") or rep.get("audioSamplingRate"):
                    rep_info["samplerate"] = int(rep.get("audioSamplingRate", 0))
                    audio_reps.append(rep_info)
                else:
                    # 通过 codec 前缀判断
                    codec = (rep_info["codec"] or "").lower()
                    if codec.startswith(("avc", "hev", "h.26", "vp", "av01")):
                        rep_info["width"] = int(rep.get("width", 0))
                        rep_info["height"] = int(rep.get("height", 0))
                        video_reps.append(rep_info)
                    elif codec.startswith(("aac", "mp4a", "opus")):
                        rep_info["samplerate"] = int(rep.get("audioSamplingRate", 0))
                        audio_reps.append(rep_info)

    return {
        "periods": len(periods),
        "video_representations": video_reps,
        "audio_representations": audio_reps,
    }


def _extract_codecs_from_m3u8_variants(variants: list[dict]) -> set[str]:
    """从 HLS variants 的 CODECS 字段提取 codec family 集合。"""
    families: set[str] = set()
    for v in variants:
        codecs_str = v.get("codecs", "")
        for codec in codecs_str.split(","):
            codec = codec.strip().lower()
            if codec.startswith("avc") or codec.startswith("h264"):
                families.add("h264")
            elif codec.startswith("hev") or codec.startswith("h265"):
                families.add("hevc")
            elif codec.startswith("av01"):
                families.add("av1")
            elif codec.startswith("aac") or codec.startswith("mp4a"):
                families.add("aac")
            elif codec.startswith("opus"):
                families.add("opus")
    return families


def _extract_codecs_from_mpd_reps(reps: list[dict]) -> set[str]:
    """从 DASH Representation 的 codecs 字段提取 codec family 集合。"""
    families: set[str] = set()
    for r in reps:
        codec = (r.get("codec") or "").lower()
        if codec.startswith("avc") or codec.startswith("h264"):
            families.add("h264")
        elif codec.startswith("hev") or codec.startswith("h265"):
            families.add("hevc")
        elif codec.startswith("av01"):
            families.add("av1")
        elif codec.startswith("aac") or codec.startswith("mp4a"):
            families.add("aac")
        elif codec.startswith("opus"):
            families.add("opus")
    return families


def _compare_protocols(
    stream_url_obj: dict[str, Any],
    flv_signatures: list[dict[str, Any]],
    headers: dict[str, str],
    manifest_dir: Path | None = None,
) -> dict[str, Any]:
    """对比 FLV、HLS、DASH 三种协议的编码参数和 CDN 来源。

    检测信号:
    - 同房间不同协议使用不同 codec family → MEDIUM
    - 同房间不同协议使用不同 CDN provider → MEDIUM
    - DASH manifest 暴露了 FLV 中不存在的额外 representation → WEAK
    """
    signals: list[dict[str, Any]] = []
    suppressors: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    # 1. 从 FLV 签名中提取已知的 codec 和 provider
    flv_codecs: set[str] = set()
    flv_providers: set[str] = set()
    for sig in flv_signatures:
        enc = (sig.get("encoder_family") or "").lower()
        if enc:
            flv_codecs.add(enc)
        prov = (sig.get("provider_family") or "").lower()
        if prov:
            flv_providers.add(prov)
        # 也从 tag codec 提取
        tag_codec = (sig.get("tag_facts") or {}).get("video_codec") or ""
        if tag_codec:
            flv_codecs.add(tag_codec.lower())

    # 2. 提取 HLS URLs 并抓取 manifest
    hls_urls = _extract_protocol_urls(stream_url_obj, ("hls_pull_url", "hls_pull_url_params"))
    hls_data: dict[str, Any] = {}
    if hls_urls:
        details["hls_qualities"] = sorted(hls_urls.keys())
        # 取第一个可用 HLS URL 抓取 manifest
        first_quality = next(iter(sorted(hls_urls.keys())))
        hls_url = hls_urls[first_quality]
        manifest = _fetch_manifest(hls_url, headers)
        if manifest:
            if manifest_dir is not None:
                (manifest_dir / f"hls_{first_quality}.m3u8").write_text(manifest, encoding="utf-8")
            hls_data = _parse_m3u8(manifest)
            details["hls_manifest"] = hls_data
            # 检查 HLS CDN
            hls_hints = analyze_stream_url_patterns(hls_url)
            hls_provider = extract_provider_family(hls_url, {})
            if hls_provider and hls_provider != "unknown":
                details["hls_provider"] = hls_provider
                # 对比 CDN provider
                if flv_providers and hls_provider.lower() not in flv_providers:
                    signals.append(_make_signal(
                        "MEDIUM",
                        "protocol_provider_divergence",
                        f"FLV CDN ({flv_providers}) vs HLS CDN ({hls_provider}) 不一致",
                        {"flv_providers": sorted(flv_providers), "hls_provider": hls_provider},
                    ))
            # 提取 HLS codec family
            if hls_data.get("is_master") and hls_data.get("variants"):
                hls_codecs = _extract_codecs_from_m3u8_variants(hls_data["variants"])
                details["hls_codecs"] = sorted(hls_codecs)
                if flv_codecs and hls_codecs and not flv_codecs & hls_codecs:
                    # 没有交集 → 不同 codec family
                    signals.append(_make_signal(
                        "MEDIUM",
                        "protocol_codec_divergence",
                        f"FLV codec ({sorted(flv_codecs)}) vs HLS codec ({sorted(hls_codecs)}) 无交集",
                        {"flv_codecs": sorted(flv_codecs), "hls_codecs": sorted(hls_codecs)},
                    ))
        else:
            details["hls_error"] = "manifest_fetch_failed"
    else:
        details["hls_note"] = "no_hls_urls_available"

    # 3. 提取 DASH URLs 并抓取 manifest
    dash_urls = _extract_protocol_urls(stream_url_obj, ("dash_pull_url", "dash_pull_url_params"))
    dash_data: dict[str, Any] = {}
    if dash_urls:
        details["dash_qualities"] = sorted(dash_urls.keys())
        first_quality = next(iter(sorted(dash_urls.keys())))
        dash_url = dash_urls[first_quality]
        manifest = _fetch_manifest(dash_url, headers)
        if manifest:
            if manifest_dir is not None:
                (manifest_dir / f"dash_{first_quality}.mpd").write_text(manifest, encoding="utf-8")
            dash_data = _parse_mpd(manifest)
            details["dash_manifest"] = dash_data
            # 检查 DASH CDN
            dash_hints = analyze_stream_url_patterns(dash_url)
            dash_provider = extract_provider_family(dash_url, {})
            if dash_provider and dash_provider != "unknown":
                details["dash_provider"] = dash_provider
                if flv_providers and dash_provider.lower() not in flv_providers:
                    signals.append(_make_signal(
                        "MEDIUM",
                        "protocol_provider_divergence",
                        f"FLV CDN ({flv_providers}) vs DASH CDN ({dash_provider}) 不一致",
                        {"flv_providers": sorted(flv_providers), "dash_provider": dash_provider},
                    ))
            # 提取 DASH codec family
            if dash_data.get("video_representations"):
                dash_codecs = _extract_codecs_from_mpd_reps(dash_data["video_representations"])
                details["dash_codecs"] = sorted(dash_codecs)
                if flv_codecs and dash_codecs and not flv_codecs & dash_codecs:
                    signals.append(_make_signal(
                        "MEDIUM",
                        "protocol_codec_divergence",
                        f"FLV codec ({sorted(flv_codecs)}) vs DASH codec ({sorted(dash_codecs)}) 无交集",
                        {"flv_codecs": sorted(flv_codecs), "dash_codecs": sorted(dash_codecs)},
                    ))
                # 检查 DASH 是否有 FLV 没有的额外 representation
                dash_rep_count = len(dash_data["video_representations"])
                flv_quality_count = len(_extract_quality_urls(stream_url_obj))
                if dash_rep_count > flv_quality_count + 1:
                    signals.append(_make_signal(
                        "WEAK",
                        "dash_extra_representations",
                        f"DASH manifest 有 {dash_rep_count} 个 video representation，FLV 只有 {flv_quality_count} 个清晰度",
                        {"dash_rep_count": dash_rep_count, "flv_quality_count": flv_quality_count},
                    ))
        else:
            details["dash_error"] = "manifest_fetch_failed"
    else:
        details["dash_note"] = "no_dash_urls_available"

    # 4. 无差异 → suppressor
    if not signals:
        suppressors.append({
            "type": "protocols_consistent",
            "description": "HLS/DASH 与 FLV 的编码参数和 CDN 来源一致",
        })

    return {"signals": signals, "suppressors": suppressors, "details": details}


def _extract_quality_urls(stream_url_obj: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(stream_url_obj, dict):
        return {}

    quality_urls: dict[str, str] = {}

    # 1. 标准画质 (flv_pull_url / flv_pull_url_params)
    for source_key in ("flv_pull_url", "flv_pull_url_params"):
        payload = stream_url_obj.get(source_key)
        if not isinstance(payload, dict):
            continue
        for quality, value in payload.items():
            if isinstance(value, str) and value.strip():
                quality_urls.setdefault(str(quality), value)
            elif isinstance(value, dict):
                for nested_key in ("url", "main", "flv", "pull_url"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        quality_urls.setdefault(str(quality), nested)
                        break

    # 2. SDK 内部画质 + ao 纯音频流 (live_core_sdk_data.pull_data.stream_data)
    try:
        lcsd = stream_url_obj.get("live_core_sdk_data", {})
        pull_data = lcsd.get("pull_data", {})
        stream_data_str = pull_data.get("stream_data")
        if isinstance(stream_data_str, str) and stream_data_str:
            import json as _json
            stream_data = _json.loads(stream_data_str)
            for qk, qv in stream_data.get("data", {}).items():
                if not isinstance(qv, dict):
                    continue
                main = qv.get("main", {})
                flv_url = main.get("flv")
                if not (isinstance(flv_url, str) and flv_url.strip()):
                    continue
                if qk == "ao":
                    quality_urls["AUDIO_ONLY"] = flv_url
                else:
                    quality_urls.setdefault(qk, flv_url)
    except Exception:
        pass

    return quality_urls


def _select_quality_urls(
    quality_urls: dict[str, str],
    preferred_qualities: list[str],
    include_audio_only: bool = True,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for quality in preferred_qualities:
        url = quality_urls.get(quality)
        if url and (quality, url) not in selected:
            selected.append((quality, url))

    if len(selected) >= 2:
        selected = selected[:2]
    else:
        ordered = sorted(quality_urls.items(), key=lambda item: _quality_rank(item[0]))
        if not ordered:
            return []
        if not selected:
            selected.append(ordered[0])
        if len(ordered) > 1:
            fallback = ordered[-1]
            if fallback[0] != selected[0][0]:
                selected.append(fallback)
            else:
                for candidate in ordered[1:]:
                    if candidate[0] != selected[0][0]:
                        selected.append(candidate)
                        break
        selected = selected[:2]

    # 附加 AUDIO_ONLY 纯音频流（用于与 FLV 音轨比对）
    if include_audio_only:
        ao_url = quality_urls.get("AUDIO_ONLY")
        if ao_url and ("AUDIO_ONLY", ao_url) not in selected:
            selected.append(("AUDIO_ONLY", ao_url))

    return selected


def _build_base_result(web_room_id: str, internal_room_id: str | None, ts: str) -> dict[str, Any]:
    return {
        "type": "dual_stream_analysis",
        "timestamp": ts,
        "room_id": internal_room_id,
        "web_room_id": web_room_id,
        "verdict": "none",
        "score": 0,
        "signals": [],
        "suppressors": [],
        "multi_quality": {
            "samples_collected": 0,
            "qualities_tested": [],
            "comparison": {"signals": [], "suppressors": [], "comparisons": []},
            "sample_details": [],
        },
        "temporal": {},
        "consistency": {"per_sample": []},
    }


def _save_result(result: dict[str, Any], output_dir: Path, internal_room_id: str | None, web_room_id: str, ts: str) -> dict[str, Any]:
    output_path = output_dir / f"dual_stream_analysis_{internal_room_id or web_room_id}_{ts}.json"
    result["json_path"] = str(output_path)
    try:
        with open(output_path, "w", encoding="utf-8") as file_obj:
            json.dump(result, file_obj, ensure_ascii=False, indent=2, default=str)
    except OSError as e:
        logger.error("保存双流检测结果失败: %s", e)
    return result


def _extract_audio_tags_info(signature: dict[str, Any]) -> dict[str, Any]:
    """从 FLV signature 中提取音频相关信息。"""
    tags = signature.get("tags") or []
    audio_tags = [t for t in tags if t.get("type") == "audio"]
    info: dict[str, Any] = {
        "audio_tag_count": len(audio_tags),
        "audio_codec": signature.get("audio_codec", "unknown"),
        "has_audio": signature.get("header", {}).get("has_audio", False),
    }
    metadata_hints = signature.get("metadata_hints") or {}
    if "audiodatarate" in metadata_hints:
        info["audio_bitrate_kbps"] = metadata_hints["audiodatarate"]
    # 从第一个 audio tag 提取编码细节
    if audio_tags:
        first = audio_tags[0]
        info["first_audio_timestamp"] = first.get("timestamp", 0)
        info["first_audio_size"] = first.get("data_size", 0)
        raw_codec = first.get("codec") or first.get("audio_codec")
        if raw_codec:
            info["audio_tag_codec"] = raw_codec
    return info


def _collect_audio_data(signature: dict[str, Any]) -> list[bytes]:
    """从 FLV signature 中收集所有 audio tag 的原始数据。"""
    tags = signature.get("tags") or []
    chunks: list[bytes] = []
    for t in tags:
        if t.get("type") == "audio" and t.get("raw_data"):
            chunks.append(t["raw_data"])
    return chunks


def compare_audio_only_stream(
    flv_signature: dict[str, Any],
    ao_signature: dict[str, Any],
) -> dict[str, Any]:
    """比对正常 FLV 音轨与纯音频 (only_audio=1) 流。

    Returns:
        包含 signals / suppressors / details 的对比结果。
    """
    signals: list[dict[str, Any]] = []
    suppressors: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    flv_audio = _extract_audio_tags_info(flv_signature)
    ao_audio = _extract_audio_tags_info(ao_signature)

    details["flv"] = flv_audio
    details["ao"] = ao_audio

    flv_quality = flv_signature.get("quality", "unknown")
    ao_quality = ao_signature.get("quality", "unknown")

    # 1. 音频编码一致性
    flv_codec = flv_audio.get("audio_codec", "unknown")
    ao_codec = ao_audio.get("audio_codec", "unknown")
    details["codec_match"] = flv_codec == ao_codec

    if flv_codec != ao_codec and flv_codec != "unknown" and ao_codec != "unknown":
        signals.append(_make_signal(
            "MEDIUM",
            "audio_codec_mismatch",
            f"FLV 音轨编码 ({flv_codec}) 与纯音频流编码 ({ao_codec}) 不一致",
            {"flv_codec": flv_codec, "ao_codec": ao_codec},
        ))

    # 2. 音频码率一致性
    flv_br = flv_audio.get("audio_bitrate_kbps")
    ao_br = ao_audio.get("audio_bitrate_kbps")
    if flv_br is not None and ao_br is not None:
        details["bitrate_match"] = abs(flv_br - ao_br) < 10
        if abs(flv_br - ao_br) >= 50:
            signals.append(_make_signal(
                "WEAK",
                "audio_bitrate_divergence",
                f"FLV 音频码率 ({flv_br:.1f}kbps) 与纯音频流 ({ao_br:.1f}kbps) 差异显著",
                {"flv_bitrate": flv_br, "ao_bitrate": ao_br},
            ))

    # 3. 是否存在音频 tag
    if ao_audio["audio_tag_count"] == 0:
        if ao_audio["has_audio"]:
            signals.append(_make_signal(
                "MEDIUM",
                "audio_only_stream_missing_audio_tags",
                f"纯音频流 ({ao_quality}) header 声明有音频但未解析到 audio tag",
                {},
            ))
        else:
            signals.append(_make_signal(
                "MEDIUM",
                "audio_only_stream_no_audio",
                f"纯音频流 ({ao_quality}) 不包含音频数据",
                {},
            ))

    # 4. 纯音频流不应有视频
    tags = ao_signature.get("tags") or []
    video_tags = [t for t in tags if t.get("type") == "video"]
    details["ao_video_tag_count"] = len(video_tags)
    if video_tags:
        details["ao_has_video_unexpected"] = True
        # 这不一定是异常，但值得记录

    # 无异常 → 抑制项
    if not signals:
        suppressors.append({
            "type": "audio_only_matches_flv_track",
            "description": f"纯音频流 ({ao_quality}) 与 FLV ({flv_quality}) 音轨编码一致",
            "details": details,
        })

    return {"signals": signals, "suppressors": suppressors, "details": details}


def run_dual_stream_detection(
    web_room_id,
    internal_room_id,
    stream_url_obj,
    headers,
    cookies,
    output_dir,
    preferred_qualities=None,
    temporal_polls=0,
    temporal_interval=30,
    request_double_stream=True,
    stream_url_obj_normal=None,
    extra_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    _cookies = cookies  # TODO(P1): 用于未来下载时的额外 cookie 传递
    del _cookies, request_double_stream  # noqa: F841

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_dir = DUAL_STREAM_SAMPLE_DIR / ts
    manifest_dir = DUAL_STREAM_MANIFEST_DIR / ts
    sample_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    preferred = preferred_qualities or ["FULL_HD1", "SD1"]
    result = _build_base_result(str(web_room_id), internal_room_id, ts)

    quality_urls = _extract_quality_urls(stream_url_obj)
    if not quality_urls:
        result["error"] = "no_quality_urls"
        logger.info("双流检测结束: 无可用清晰度 URL")
        return _save_result(result, output_dir, internal_room_id, str(web_room_id), ts)

    selected_quality_urls = _select_quality_urls(quality_urls, preferred)

    # 附加外部传入的额外流 URL（如 Android 客户端抓到的流）
    if extra_urls:
        for label, url in extra_urls.items():
            if url and (label, url) not in selected_quality_urls:
                selected_quality_urls.append((label, url))

    logger.info("双流检测选择清晰度: %s", [quality for quality, _ in selected_quality_urls])

    if len(selected_quality_urls) < 2:
        result["note"] = "insufficient_qualities"
        result["multi_quality"]["qualities_tested"] = [quality for quality, _ in selected_quality_urls]
        logger.info("双流检测结束: 清晰度数量不足")
        return _save_result(result, output_dir, internal_room_id, str(web_room_id), ts)

    valid_samples: list[dict[str, Any]] = []
    sample_details: list[dict[str, Any]] = []

    def _download_one(quality: str, url: str) -> tuple[str, str, dict[str, Any] | None]:
        logger.info("开始下载样本: %s", quality)
        sample = download_stream_sample(url, headers, max_bytes=DOWNLOAD_MAX_BYTES)
        return quality, url, sample

    with ThreadPoolExecutor(max_workers=min(len(selected_quality_urls), 5)) as pool:
        future_map = {
            pool.submit(_download_one, q, u): q
            for q, u in selected_quality_urls
        }
        for future in as_completed(future_map):
            quality, url, sample = future.result()
            if not sample or not sample.get("data"):
                logger.warning("样本 %s 下载失败", quality)
                sample_details.append({"quality": quality, "stream_url": url, "error": "download_failed"})
                continue

            logger.info("样本 %s 下载完成: %s bytes", quality, len(sample["data"]))
            sample_path = sample_dir / f"{quality}.flv"
            sample_path.write_bytes(sample["data"])
            url_hints = analyze_stream_url_patterns(url)
            signature = build_flv_signature(
                data=sample["data"],
                stream_url=url,
                url_hints=url_hints,
                headers_info=sample.get("headers_info") or {},
                quality=quality,
            )
            valid_samples.append(signature)
            sample_details.append({
                "quality": quality,
                "sample_path": str(sample_path),
                "provider_family": signature.get("provider_family"),
                "encoder_family": signature.get("encoder_family"),
                "video_codec": signature.get("video_codec"),
                "audio_codec": signature.get("audio_codec"),
                "fingerprint": signature.get("fingerprint"),
                "header": signature.get("header"),
                "metadata_hints": signature.get("metadata_hints"),
                "normalized_url": signature.get("normalized_url"),
            })

    result["multi_quality"]["samples_collected"] = len(valid_samples)
    result["multi_quality"]["qualities_tested"] = [item["quality"] for item in sample_details]
    result["multi_quality"]["sample_details"] = sample_details

    if not valid_samples:
        result["error"] = "all_downloads_failed"
        logger.info("双流检测结束: 所有样本下载失败")
        return _save_result(result, output_dir, internal_room_id, str(web_room_id), ts)

    comparison_result = {"signals": [], "suppressors": [], "comparisons": []}
    consistency_per_sample: list[dict[str, Any]] = []

    if len(valid_samples) >= 2:
        comparison_result = compare_quality_samples(valid_samples)
        logger.info("多清晰度比较结果: %s 个信号, %s 个抑制项", len(comparison_result["signals"]), len(comparison_result["suppressors"]))

    for signature in valid_samples:
        consistency_result = run_consistency_checks(signature)
        consistency_per_sample.append({
            "quality": signature.get("quality"),
            "signals": consistency_result.get("signals") or [],
            "suppressors": consistency_result.get("suppressors") or [],
            "checks": consistency_result.get("checks") or [],
        })

    analysis = {
        "multi_quality": comparison_result,
        "consistency": {"per_sample": consistency_per_sample},
    }
    scored = score_dual_stream_risk(analysis)

    all_signals = list(comparison_result.get("signals") or [])
    all_suppressors = list(comparison_result.get("suppressors") or [])
    for item in consistency_per_sample:
        all_signals.extend(item.get("signals") or [])
        all_suppressors.extend(item.get("suppressors") or [])

    result["verdict"] = scored["verdict"]
    result["score"] = scored["score"]
    result["reasoning"] = scored["reasoning"]
    result["signals"] = all_signals
    result["suppressors"] = all_suppressors
    result["multi_quality"]["comparison"] = comparison_result
    result["consistency"] = {"per_sample": consistency_per_sample}

    # 纯音频流比对: FLV 音轨 vs only_audio=1 流
    ao_signature = next((s for s in valid_samples if s.get("quality") == "AUDIO_ONLY"), None)
    flv_signature = next((s for s in valid_samples if s.get("quality") != "AUDIO_ONLY"), None)
    audio_comparison: dict[str, Any] = {}
    if ao_signature and flv_signature:
        logger.info("开始纯音频流比对: FLV(%s) vs AUDIO_ONLY", flv_signature.get("quality"))
        audio_comparison = compare_audio_only_stream(flv_signature, ao_signature)
        ao_signals = audio_comparison.get("signals") or []
        if ao_signals:
            all_signals.extend(ao_signals)
            all_suppressors.extend(audio_comparison.get("suppressors") or [])
            analysis["audio_only"] = audio_comparison
            rescored = score_dual_stream_risk(analysis)
            result["verdict"] = rescored["verdict"]
            result["score"] = rescored["score"]
            result["reasoning"] = rescored["reasoning"]
            logger.info("音频比对发现差异，重评分: verdict=%s score=%s", rescored["verdict"], rescored["score"])
        else:
            logger.info("音频比对: FLV 音轨与纯音频流一致")
    elif ao_signature and not flv_signature:
        logger.info("仅有 AUDIO_ONLY 样本，缺少 FLV 样本，跳过音频比对")
    result["audio_only_comparison"] = audio_comparison

    # 双模式对比: need_double_stream=true vs false
    double_mode_result: dict[str, Any] = {}
    if stream_url_obj_normal and isinstance(stream_url_obj_normal, dict):
        logger.info("开始双模式对比: double_stream=true vs false")
        double_mode_result = _compare_double_stream_modes(stream_url_obj, stream_url_obj_normal, headers)
        dm_signals = double_mode_result.get("signals") or []
        if dm_signals:
            all_signals.extend(dm_signals)
            all_suppressors.extend(double_mode_result.get("suppressors") or [])
            # 重新评分
            analysis["double_mode"] = double_mode_result
            rescored = score_dual_stream_risk(analysis)
            result["verdict"] = rescored["verdict"]
            result["score"] = rescored["score"]
            result["reasoning"] = rescored["reasoning"]
            logger.info("双模式对比发现差异，重评分: verdict=%s score=%s", rescored["verdict"], rescored["score"])
        else:
            logger.info("双模式对比: 两种模式 URL 集一致")
    result["double_mode"] = double_mode_result

    # HLS/DASH 跨协议对比
    protocol_result: dict[str, Any] = {}
    if valid_samples:
        logger.info("开始 HLS/DASH 跨协议对比")
        protocol_result = _compare_protocols(stream_url_obj, valid_samples, headers, manifest_dir=manifest_dir)
        proto_signals = protocol_result.get("signals") or []
        if proto_signals:
            all_signals.extend(proto_signals)
            all_suppressors.extend(protocol_result.get("suppressors") or [])
            analysis["protocols"] = protocol_result
            rescored = score_dual_stream_risk(analysis)
            result["verdict"] = rescored["verdict"]
            result["score"] = rescored["score"]
            result["reasoning"] = rescored["reasoning"]
            logger.info("跨协议对比发现差异，重评分: verdict=%s score=%s", rescored["verdict"], rescored["score"])
        else:
            logger.info("跨协议对比: HLS/DASH 与 FLV 一致")
    result["protocols"] = protocol_result

    # 时序监控: 间隔采样检测中途换流
    temporal_result: dict[str, Any] = {}
    if temporal_interval > 0 and valid_samples:
        # 取第一个有效样本的清晰度和 URL 作为监控目标
        primary_quality = selected_quality_urls[0][0] if selected_quality_urls else None
        primary_url = selected_quality_urls[0][1] if selected_quality_urls else None
        if primary_quality and primary_url:
            logger.info("启动时序监控: 清晰度=%s, 间隔=%ds, 轮次=%d", primary_quality, temporal_interval, temporal_polls or 2)
            temporal_result = _run_temporal_polls(
                primary_quality=primary_quality,
                primary_url=primary_url,
                headers=headers,
                temporal_polls=temporal_polls,
                temporal_interval=temporal_interval,
            )
            # 时序信号加入总信号
            temporal_signals = temporal_result.get("signals") or []
            all_signals.extend(temporal_signals)
            result["temporal"] = {
                "events": temporal_result.get("events") or [],
                "polls": temporal_result.get("polls") or [],
            }
            # 有时序信号时重新评分
            if temporal_signals:
                analysis["temporal"] = temporal_result
                rescored = score_dual_stream_risk(analysis)
                result["verdict"] = rescored["verdict"]
                result["score"] = rescored["score"]
                result["reasoning"] = rescored["reasoning"]
                logger.info("时序监控检测到变化，重评分: verdict=%s score=%s", rescored["verdict"], rescored["score"])
            else:
                logger.info("时序监控完成: 未检测到中途换流信号")

    result["artifacts"] = {
        "sample_dir": str(sample_dir),
        "manifest_dir": str(manifest_dir),
    }
    saved = _save_result(result, output_dir, internal_room_id, str(web_room_id), ts)
    logger.info("双流检测完成: verdict=%s score=%s file=%s", saved["verdict"], saved["score"], saved["json_path"])
    return saved
