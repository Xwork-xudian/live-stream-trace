# -*- coding: utf-8 -*-
"""FLV 元数据解析器。"""

import argparse
import glob
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from amf0 import parse_flv_header, parse_flv_script_tag, extract_encoder_hints, ci_get
from config import OUTPUT_DIR, SOFTWARE_PATTERNS, ensure_dirs


logger = logging.getLogger(__name__)


def normalize_flv_header(data: bytes):
    """适配共享 Header 结构为旧输出字段。"""
    if len(data) < 9:
        return None
    parsed = parse_flv_header(data)
    if parsed is None:
        return None
    flags = parsed.get("flags") or {}
    return {
        "version": parsed.get("version"),
        "has_audio": bool(flags.get("audio")),
        "has_video": bool(flags.get("video")),
        "header_size": parsed.get("length"),
    }


def categorize_metadata(metadata: dict):
    """按视频、音频、编码器等维度拆分元数据字段。"""
    video_fields = {}
    audio_fields = {}
    encoder_fields = {}
    other_fields = {}

    video_keys = {
        "width", "height", "framerate", "videodatarate", "videocodecid",
        "duration", "fps", "totalframes", "totalduration", "displayheight",
        "displaywidth",
    }
    audio_keys = {
        "audiodatarate", "audiocodecid", "audiosamplerate", "audiosamplesize",
        "stereo", "audiochannels",
    }
    encoder_keys = {
        "encoder", "software", "flashver", "creator", "metadatacreator",
        "processingsoftware", "tool", "playerfactory", "playerversion",
        "server", "pushurl", "streamurl",
    }

    for key, value in sorted(metadata.items()):
        lowered = key.lower()
        if lowered in video_keys:
            video_fields[key] = value
        elif lowered in audio_keys:
            audio_fields[key] = value
        elif lowered in encoder_keys:
            encoder_fields[key] = value
        else:
            other_fields[key] = value

    return {
        "video": video_fields,
        "audio": audio_fields,
        "encoder": encoder_fields,
        "other": other_fields,
    }


def detect_push_software(metadata: dict):
    """结合共享规则推断推流软件。"""
    candidates = {
        "encoder": ci_get(metadata, "encoder"),
        "software": ci_get(metadata, "software"),
        "flashVer": ci_get(metadata, "flashVer"),
        "metadatacreator": ci_get(metadata, "metadatacreator"),
        "creator": ci_get(metadata, "creator"),
        "processingSoftware": ci_get(metadata, "processingSoftware"),
    }

    push_software = "unknown"
    confidence = "low"
    matched_source = "none"

    for source, candidate in candidates.items():
        if not candidate:
            continue
        candidate_text = str(candidate).lower()
        for pattern, name in SOFTWARE_PATTERNS:
            if re.search(pattern, candidate_text):
                push_software = name
                confidence = "high"
                matched_source = source
                return {
                    "push_software": push_software,
                    "confidence": confidence,
                    "matched_source": matched_source,
                    "candidates": candidates,
                }

    for source in ["encoder", "software", "flashVer", "metadatacreator", "creator", "processingSoftware"]:
        value = candidates.get(source)
        if value and str(value).strip() and len(str(value).strip()) >= 4:
            push_software = str(value).strip()
            confidence = "low"
            matched_source = source
            break

    return {
        "push_software": push_software,
        "confidence": confidence,
        "matched_source": matched_source,
        "candidates": candidates,
    }


def analyze_file(flv_path: Path, output_dir: Path, json_output_dir: Path):
    """分析单个 FLV 文件。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not flv_path.exists():
        logger.error("文件不存在: %s", flv_path)
        return None
    if not flv_path.is_file():
        logger.error("路径不是文件: %s", flv_path)
        return None

    try:
        data = flv_path.read_bytes()
    except OSError as e:
        logger.error("读取文件失败: %s", e)
        return None

    if len(data) < 13:
        logger.error("文件过小 (%s bytes)，不像有效 FLV", len(data))
        return None

    logger.info("Analyzing: %s", flv_path.name)
    logger.info("File size: %s bytes", len(data))

    if data[:3] != b"FLV":
        raise ValueError("Not an FLV file!")

    header = normalize_flv_header(data)
    if header is None:
        raise ValueError("FLV Header 解析失败")

    logger.info("FLV Version: %s, Audio: %s, Video: %s", header["version"], header["has_audio"], header["has_video"])

    offset = int(header["header_size"]) + 4
    video_tags = 0
    audio_tags = 0
    script_tags = 0
    all_metadata = {}
    first_video_tag = None
    first_audio_tag = None

    while offset + 11 < len(data):
        tag_type = data[offset]
        data_size = (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]
        if offset + 11 + data_size > len(data):
            break

        tag_data_start = offset + 11
        tag_data = data[tag_data_start:tag_data_start + data_size]

        if tag_type == 18 and data_size > 0:
            script_tags += 1
            if script_tags == 1:
                parsed_tag = parse_flv_script_tag(tag_data)
                if parsed_tag and parsed_tag.get("name") == "onMetaData" and isinstance(parsed_tag.get("value"), dict):
                    all_metadata = parsed_tag["value"]
                    logger.info("onMetaData 字段数: %s", len(all_metadata))
        elif tag_type == 9:
            video_tags += 1
            if video_tags == 1 and data_size > 1:
                codec_id = tag_data[0] & 0x0F
                frame_type = (tag_data[0] >> 4) & 0x0F
                codec_map = {
                    2: "Sorenson H.263", 3: "Screen Video", 4: "VP6",
                    5: "VP6 Alpha", 6: "Screen V2", 7: "H.264/AVC", 12: "HEVC/H.265",
                }
                frame_map = {1: "keyframe", 2: "inter frame", 3: "disposable", 4: "generated", 5: "command"}
                first_video_tag = {
                    "frame_type": frame_map.get(frame_type, "?"),
                    "codec": codec_map.get(codec_id, f"unknown({codec_id})"),
                }
                if codec_id == 7 and data_size > 5:
                    avc_type = tag_data[1]
                    first_video_tag["avc_type"] = {0: "AVC sequence header", 1: "AVC NALU", 2: "AVC end of sequence"}.get(avc_type)
        elif tag_type == 8:
            audio_tags += 1
            if audio_tags == 1 and data_size > 1:
                sound_format = (tag_data[0] >> 4) & 0x0F
                sound_rate = (tag_data[0] >> 2) & 0x03
                sound_size = (tag_data[0] >> 1) & 0x01
                sound_type = tag_data[0] & 0x01
                format_map = {0: "PCM", 2: "MP3", 10: "AAC", 11: "Speex"}
                rate_map = {0: "5.5kHz", 1: "11kHz", 2: "22kHz", 3: "44kHz"}
                first_audio_tag = {
                    "format": format_map.get(sound_format, f"format({sound_format})"),
                    "rate": rate_map.get(sound_rate, "?"),
                    "size": "16bit" if sound_size else "8bit",
                    "channels": "stereo" if sound_type else "mono",
                }
                if sound_format == 10 and data_size > 2 and tag_data[1] == 0:
                    first_audio_tag["aac_type"] = "AAC sequence header"

        offset += 11 + data_size + 4
        if script_tags >= 5 and video_tags >= 20 and audio_tags >= 20:
            break

    categorized = categorize_metadata(all_metadata) if all_metadata else {"video": {}, "audio": {}, "encoder": {}, "other": {}}
    encoder_hints = extract_encoder_hints({"onMetaData": all_metadata} if all_metadata else {})
    push_result = detect_push_software(all_metadata) if all_metadata else {
        "push_software": "unknown",
        "confidence": "low",
        "matched_source": "none",
        "candidates": {},
    }

    text_report = output_dir / f"flv_metadata_report_{ts}.txt"
    metadata_json = output_dir / f"onmetadata_{ts}.json"
    analysis_json = json_output_dir / f"flv_metadata_analysis_{ts}.json"

    try:
        with open(text_report, "w", encoding="utf-8") as file_obj:
            file_obj.write("FLV 元数据解析报告\n")
            file_obj.write(f"文件: {flv_path}\n")
            file_obj.write(f"时间: {datetime.now()}\n")
            file_obj.write("=" * 60 + "\n")
            file_obj.write(f"FLV Version: {header['version']}, Audio: {header['has_audio']}, Video: {header['has_video']}\n")
            file_obj.write(f"Tag counts: video={video_tags}, audio={audio_tags}, script={script_tags}\n")
            if all_metadata:
                file_obj.write(f"onMetaData fields: {len(all_metadata)}\n")
                file_obj.write(json.dumps(all_metadata, ensure_ascii=False, indent=2))
                file_obj.write("\n")
            else:
                file_obj.write("No onMetaData found!\n")
    except OSError as e:
        logger.error("保存文本报告失败: %s", e)

    if all_metadata:
        try:
            with open(metadata_json, "w", encoding="utf-8") as file_obj:
                json.dump(all_metadata, file_obj, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("保存元数据 JSON 失败: %s", e)

    result = {
        "type": "flv_metadata_analysis",
        "timestamp": ts,
        "file": str(flv_path),
        "header": header,
        "tag_counts": {"video": video_tags, "audio": audio_tags, "script": script_tags},
        "first_video_tag": first_video_tag,
        "first_audio_tag": first_audio_tag,
        "metadata": all_metadata or None,
        "categorized_metadata": categorized,
        "encoder_hints": encoder_hints,
        "push_software": push_result,
        "text_report": str(text_report),
        "metadata_json": str(metadata_json) if all_metadata else None,
    }
    try:
        with open(analysis_json, "w", encoding="utf-8") as file_obj:
            json.dump(result, file_obj, ensure_ascii=False, indent=2, default=str)
    except OSError as e:
        logger.error("保存分析 JSON 失败: %s", e)

    logger.info("文本报告已保存: %s", text_report)
    if all_metadata:
        logger.info("完整元数据已保存: %s", metadata_json)
    logger.info("JSON 结果已保存: %s", analysis_json)

    return result | {"analysis_json": str(analysis_json)}


def main():
    parser = argparse.ArgumentParser(description="FLV 元数据解析器")
    parser.add_argument("--file", "-f", help="指定 FLV 文件路径")
    parser.add_argument("--glob", "-g", default=str(OUTPUT_DIR / "flv_sample_*.flv"), help="FLV 文件 glob 模式")
    parser.add_argument("--verbose", "-v", action="store_true")
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
    output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output_dir = OUTPUT_DIR  # 直接保存到 captures/，避免 captures/captures/ 双重嵌套

    if args.file:
        flv_path = Path(args.file)
    else:
        flv_files = sorted(glob.glob(args.glob))
        if not flv_files:
            print("No FLV samples found!")
            return
        flv_path = Path(flv_files[-1])

    result = analyze_file(flv_path, output_dir, json_output_dir)
    metadata = result.get("metadata") or {}
    push_result = result.get("push_software") or {}

    print("=" * 60)
    print("  ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"  FLV Version: {result['header']['version']}")
    print(f"  Tag counts: video={result['tag_counts']['video']}, audio={result['tag_counts']['audio']}, script={result['tag_counts']['script']}")
    if metadata:
        width = ci_get(metadata, "width", "?")
        height = ci_get(metadata, "height", "?")
        fps = ci_get(metadata, "framerate", ci_get(metadata, "fps", "?"))
        vbr = ci_get(metadata, "videodatarate", "?")
        abr = ci_get(metadata, "audiodatarate", "?")
        print(f"  Resolution: {width}x{height}")
        print(f"  Framerate: {fps}")
        print(f"  Video bitrate: {vbr} kbps")
        print(f"  Audio bitrate: {abr} kbps")
        print(f"  Encoder: {ci_get(metadata, 'encoder') or 'not set'}")
        print(f"  Software: {ci_get(metadata, 'software') or 'not set'}")
        print(f"  flashVer: {ci_get(metadata, 'flashVer') or 'not set'}")
        print(f"  metadatacreator: {ci_get(metadata, 'metadatacreator') or 'not set'}")
        if push_result.get("push_software") != "unknown":
            print(f"  >>> Push software: {push_result['push_software']}  (置信度: {push_result['confidence']}, 来源: {push_result['matched_source']})")
        else:
            print("  >>> Push software: 未检测到已知推流软件特征")
    else:
        print("  No onMetaData found!")
    print(f"  Text report: {result['text_report']}")
    print(f"  JSON report: {result['analysis_json']}")
    print("\n[Done]")


if __name__ == "__main__":
    main()
