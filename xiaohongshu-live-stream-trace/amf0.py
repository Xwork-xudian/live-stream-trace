"""AMF0 与 FLV 脚本标签共享解析模块。"""

import logging
import re
import struct

logger = logging.getLogger(__name__)

MAX_CONTAINER_ITERATIONS = 8192
MAX_RECURSION_DEPTH = 256

_CANONICAL_HINT_KEYS = (
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

# 解析截断/异常时的哨兵标记
_TRUNCATED = object()


def _read_u16(data: bytes, offset: int):
    """读取大端 uint16，不足时返回 None。"""
    if offset + 2 > len(data):
        return None
    return struct.unpack(">H", data[offset:offset + 2])[0]


def _read_u32(data: bytes, offset: int):
    """读取大端 uint32，不足时返回 None。"""
    if offset + 4 > len(data):
        return None
    return struct.unpack(">I", data[offset:offset + 4])[0]


def _read_double(data: bytes, offset: int):
    """读取大端 double，不足时返回 None。"""
    if offset + 8 > len(data):
        return None
    return struct.unpack(">d", data[offset:offset + 8])[0]


def _read_utf8(data: bytes, offset: int, length: int):
    """按指定长度读取 UTF-8 文本。"""
    end = offset + length
    if end > len(data):
        return None, offset
    return data[offset:end].decode("utf-8", errors="replace"), end


def _parse_u16_string(data: bytes, offset: int):
    """解析 AMF0 使用的 2 字节长度字符串。"""
    length = _read_u16(data, offset)
    if length is None:
        return None, offset
    return _read_utf8(data, offset + 2, length)


def _parse_u32_string(data: bytes, offset: int):
    """解析 AMF0 使用的 4 字节长度字符串。"""
    length = _read_u32(data, offset)
    if length is None:
        return None, offset
    return _read_utf8(data, offset + 4, length)


def _parse_object_body(data: bytes, offset: int, depth: int):
    """解析 Object/Typed Object 的键值体。"""
    if depth > MAX_RECURSION_DEPTH:
        logger.warning("AMF0 object 递归深度超限 (>%s)", MAX_RECURSION_DEPTH)
        return {}, offset

    obj = {}
    pos = offset
    iterations = 0

    while pos + 2 <= len(data):
        iterations += 1
        if iterations > MAX_CONTAINER_ITERATIONS:
            logger.warning("AMF0 object 迭代超限 (>%s)，返回截断数据", MAX_CONTAINER_ITERATIONS)
            return obj, pos

        if pos + 3 <= len(data) and data[pos:pos + 3] == b"\x00\x00\x09":
            return obj, pos + 3

        key, next_pos = _parse_u16_string(data, pos)
        if key is None or next_pos == pos:
            return obj, pos

        if next_pos >= len(data):
            return obj, next_pos

        value, value_pos = _parse_amf_value(data, next_pos, depth + 1)
        if value_pos == next_pos:
            return obj, next_pos

        obj[key] = value
        pos = value_pos

    return obj, pos


def _parse_amf_value(data: bytes, offset: int, depth: int) -> tuple:
    """递归解析单个 AMF0 值。"""
    if depth > MAX_RECURSION_DEPTH:
        return None, offset

    if offset >= len(data):
        return None, offset

    marker = data[offset]

    if marker == 0x00:
        value = _read_double(data, offset + 1)
        return (value, offset + 9) if value is not None else (None, offset)

    if marker == 0x01:
        if offset + 2 > len(data):
            return None, offset
        return data[offset + 1] != 0, offset + 2

    if marker == 0x02:
        return parse_amf_string(data, offset)

    if marker == 0x03:
        return _parse_object_body(data, offset + 1, depth + 1)

    if marker == 0x05:
        return None, offset + 1

    if marker == 0x06:
        return None, offset + 1

    if marker == 0x07:
        index = _read_u16(data, offset + 1)
        return (("AMF0_REF", index), offset + 3) if index is not None else (None, offset)

    if marker == 0x08:
        return _parse_amf_ecma_array(data, offset, depth + 1)

    if marker == 0x0A:
        count = _read_u32(data, offset + 1)
        if count is None:
            return None, offset
        items = []
        pos = offset + 5
        limit = min(count, MAX_CONTAINER_ITERATIONS)
        for _ in range(limit):
            value, next_pos = _parse_amf_value(data, pos, depth + 1)
            if next_pos == pos:
                return items, pos
            items.append(value)
            pos = next_pos
        if count > MAX_CONTAINER_ITERATIONS:
            logger.warning("AMF0 strict array 截断: count=%s, 只读取 %s 项", count, limit)
        return items, pos

    if marker == 0x0B:
        timestamp_ms = _read_double(data, offset + 1)
        timezone = _read_u16(data, offset + 9)
        if timestamp_ms is None or timezone is None:
            return None, offset
        return ("AMF0_DATE", timestamp_ms, timezone), offset + 11

    if marker == 0x0C:
        value, next_pos = _parse_u32_string(data, offset + 1)
        return (value, next_pos) if value is not None else (None, offset)

    if marker == 0x0D:
        return None, offset + 1

    if marker == 0x0F:
        value, next_pos = _parse_u32_string(data, offset + 1)
        return (value, next_pos) if value is not None else (None, offset)

    if marker == 0x10:
        class_name, pos = _parse_u16_string(data, offset + 1)
        if class_name is None:
            return None, offset
        body, pos = _parse_object_body(data, pos, depth + 1)
        return ("AMF0_TYPED_OBJECT", class_name, body), pos

    if marker == 0x11:
        return "AMF3_SWITCH", offset + 1

    logger.debug("AMF0 未知 marker 0x%02x at offset %s", marker, offset)
    return None, offset + 1


def parse_amf_value(data: bytes, offset: int) -> tuple:
    """解析 offset 位置处的单个 AMF0 值，返回 (值, 新偏移)。"""
    return _parse_amf_value(data, offset, 0)


def parse_amf_string(data: bytes, offset: int) -> tuple:
    """解析 AMF0 短字符串（0x02），返回 (字符串, 新偏移)。"""
    if offset >= len(data) or data[offset] != 0x02:
        return None, offset
    value, next_pos = _parse_u16_string(data, offset + 1)
    return (value, next_pos) if value is not None else (None, offset)


def parse_amf_object(data: bytes, offset: int) -> tuple:
    """解析 AMF0 Object（0x03），返回 (字典, 新偏移)。"""
    if offset >= len(data) or data[offset] != 0x03:
        return None, offset
    return _parse_object_body(data, offset + 1, 0)


def _parse_amf_ecma_array(data: bytes, offset: int, depth: int) -> tuple:
    """递归解析 AMF0 ECMA Array。"""
    if depth > MAX_RECURSION_DEPTH:
        logger.warning("AMF0 ECMA array 递归深度超限 (>%s)", MAX_RECURSION_DEPTH)
        return {}, offset

    if offset >= len(data) or data[offset] != 0x08:
        return None, offset

    count = _read_u32(data, offset + 1)
    if count is None:
        return None, offset

    obj = {}
    pos = offset + 5
    iterations = 0

    while pos + 2 <= len(data):
        iterations += 1
        if iterations > MAX_CONTAINER_ITERATIONS:
            logger.warning("AMF0 ECMA array 迭代超限 (>%s)，返回截断数据", MAX_CONTAINER_ITERATIONS)
            return obj, pos

        if pos + 3 <= len(data) and data[pos:pos + 3] == b"\x00\x00\x09":
            return obj, pos + 3

        key, next_pos = _parse_u16_string(data, pos)
        if key is None or next_pos == pos:
            return obj, pos

        if next_pos >= len(data):
            return obj, next_pos

        value, value_pos = _parse_amf_value(data, next_pos, depth + 1)
        if value_pos == next_pos:
            return obj, next_pos

        obj[key] = value
        pos = value_pos

    return obj, pos


def parse_amf_ecma_array(data: bytes, offset: int) -> tuple:
    """解析 AMF0 ECMA Array（0x08），返回 (字典, 新偏移)。"""
    return _parse_amf_ecma_array(data, offset, 0)


def parse_flv_header(data: bytes):
    """解析 FLV 文件头前 9 字节，失败时返回 None。"""
    if len(data) < 9 or data[:3] != b"FLV":
        return None

    flags = data[4]
    header_length = _read_u32(data, 5)
    if header_length is None:
        return None

    return {
        "format": "FLV",
        "version": data[3],
        "flags": {
            "raw": flags,
            "audio": bool(flags & 0x04),
            "video": bool(flags & 0x01),
        },
        "length": header_length,
    }


def parse_flv_script_tag(data: bytes):
    """解析 FLV script tag 负载，提取 AMF0 名称和值。"""
    name, offset = parse_amf_value(data, 0)
    if offset == 0 and name is None:
        return None

    value, value_offset = parse_amf_value(data, offset)
    # offset 未推进 = 解析失败，不依赖 offset >= len(data) 判断
    if value_offset == offset:
        value = None

    return {
        "name": name,
        "value": value,
    }


def ci_get(d: dict, key: str, default=None):
    """大小写不敏感地取值，并额外尝试 onMetaData.key 前缀。"""
    if not isinstance(d, dict):
        return default

    target = str(key).lower()
    prefixed_target = f"onmetadata.{target}"

    for candidate in d:
        if str(candidate).lower() == target:
            return d[candidate]

    for candidate in d:
        if str(candidate).lower() == prefixed_target:
            return d[candidate]

    return default


def _canonical_hint_key(key: str):
    """将候选键名标准化为编码提示字段名。"""
    normalized = re.sub(r"\s+", "", str(key)).lower()
    normalized = normalized.removeprefix("onmetadata.")
    return normalized if normalized in _CANONICAL_HINT_KEYS else None


def extract_encoder_hints(metadata: dict) -> dict:
    """从 onMetaData 中提取编码器相关线索并返回标准化小写键。"""
    if not isinstance(metadata, dict):
        return {}

    hints = {}

    for key in _CANONICAL_HINT_KEYS:
        value = ci_get(metadata, key, None)
        if value is not None:
            hints[key] = value

    def walk(value, depth=0):
        if depth > MAX_RECURSION_DEPTH:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                canonical = _canonical_hint_key(child_key)
                if canonical and canonical not in hints:
                    hints[canonical] = child_value
                walk(child_value, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(metadata)
    return hints


__all__ = [
    "ci_get",
    "extract_encoder_hints",
    "parse_amf_ecma_array",
    "parse_amf_object",
    "parse_amf_string",
    "parse_amf_value",
    "parse_flv_header",
    "parse_flv_script_tag",
]
