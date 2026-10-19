"""
抖音直播间 reflow/info API 自动监控脚本
监控直播间 reflow/info 响应，开播时自动抓取完整响应并保存分析
"""

import json
import argparse
import logging
import time
import sys
from datetime import datetime
from pathlib import Path

from mappings_loader import load_json_mapping

try:
    import requests
except ImportError:
    print("需要安装 requests 库: pip install requests")
    sys.exit(1)

from config import ROOM_ID, WEB_ROOM_ID, OUTPUT_DIR, COMMON_HEADERS, API_TIMEOUT, POLL_INTERVAL, REFLOW_URLS, ensure_dirs
from network import http_get

logger = logging.getLogger(__name__)

CURRENT_ROOM_ID = ROOM_ID
CURRENT_WEB_ROOM_ID = WEB_ROOM_ID
CURRENT_OUTPUT_DIR = OUTPUT_DIR

# 关键字段路径，抓到后高亮显示
INTERESTING_KEYS = [
    "app_id",
    "client_version",
    "live_type_third_party",
    "live_type_normal",
    "live_type_audio",
    "live_type_linkmic",
    "live_type_vs_live",
    "live_type_sandbox",
    "live_type_screenshot",
    "live_type_official",
    "live_type_vs_premiere",
    "live_platform_source",
    "os_type",
    "use_filter",
    "stream_url",
    "provider_name",
    "software",
    "push_url",
    "flashVer",
    "video_codec",
    "encoder",
    "third_party",
    "stream_provider",
    "flv_pull_url",
    "rtmp_push_url",
    "status",
    "title",
    "user_count",
    "room_id",
    "web_rid",
    "resolution",
    "width",
    "height",
]

# 关键字段的中文说明，用于报告中解读字段含义
FIELD_DESCRIPTIONS = load_json_mapping("field_descriptions.json")


def get_type_name(value):
    """返回统一的字段类型名"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def ensure_output_dir(output_dir=None):
    """确保输出目录存在。"""
    if output_dir is None:
        ensure_dirs()
        return CURRENT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def poll_reflow(room_id, web_room_id, cookies=None):
    """请求 reflow/info 接口，返回 (status_code, json_data) 或 (None, None)。
    自动尝试多个 API 域名（live.douyin.com → webcast-hl.amemv.com）。"""
    params = {
        "live_id": "1",
        "room_id": room_id,
        "app_id": "1128",
    }
    headers = dict(COMMON_HEADERS)
    headers["Referer"] = f"https://live.douyin.com/{web_room_id}"
    if cookies:
        headers["Cookie"] = cookies

    for url in REFLOW_URLS:
        logger.debug("尝试 API 域名: %s", url.split("//")[1].split("/")[0])
        try:
            resp = http_get(
                url,
                params=params,
                headers=headers,
                timeout=API_TIMEOUT,
                retries=2,
            )
            if resp is None:
                logger.warning("请求 %s 失败: 未获得响应", url.split("//")[1].split("/")[0])
                continue
            data = resp.json()
            status_code = resp.status_code
            # 检查是否返回有效数据（非 10011 鉴权错误）
            if status_code == 200 and isinstance(data, dict):
                sc = data.get("status_code", 0)
                if sc == 10011:
                    logger.warning("域名 %s 返回鉴权错误 (10011)，尝试下一个", url.split("//")[1].split("/")[0])
                    continue
            return status_code, data
        except json.JSONDecodeError:
            logger.warning("响应非 JSON (%s)", url.split("//")[1].split("/")[0])
            continue

    logger.error("所有 API 域名均请求失败")
    return None, None


def extract_room_status(data):
    """从响应中提取房间状态码，2=已结束，其他=直播中"""
    try:
        room = data["data"]["room"]
        return room.get("status")
    except (KeyError, TypeError):
        return None


def _match_interesting_key(key_name):
    """精确匹配字段名是否属于感兴趣字段（避免子串匹配误判）"""
    kl = key_name.lower()
    for ik in INTERESTING_KEYS:
        if kl == ik.lower():
            return True
    return False


def extract_interesting_fields(data, prefix=""):
    """递归提取感兴趣的字段（仅叶子节点，排除 URL/图片/大 JSON 等噪音）"""
    results = {}
    if isinstance(data, dict):
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            # 只对叶子节点（非 dict/list）做 key 匹配
            if not isinstance(v, (dict, list)):
                if _match_interesting_key(k):
                    # 跳过 URL 和图片地址
                    if isinstance(v, str) and v.startswith(("http://", "https://", "sslocal://")):
                        continue
                    results[full_key] = v
            else:
                results.update(extract_interesting_fields(v, full_key))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            results.update(extract_interesting_fields(item, f"{prefix}[{i}]"))
    return results


def build_field_tree(data, prefix=""):
    """递归构建完整字段树，返回 扁平路径 -> {value, type} 映射"""
    field_tree = {}
    current_path = prefix or "<root>"
    value_type = get_type_name(data)

    field_tree[current_path] = {
        "value": data,
        "type": value_type,
    }

    if isinstance(data, dict):
        for key, value in data.items():
            child_path = f"{prefix}.{key}" if prefix else key
            field_tree.update(build_field_tree(value, child_path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            child_path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            field_tree.update(build_field_tree(item, child_path))

    return field_tree


def is_interesting_field(path, interesting_fields=None):
    """判断字段路径是否属于感兴趣字段"""
    keys = interesting_fields or INTERESTING_KEYS
    path_lower = path.lower()
    return any(key.lower() in path_lower for key in keys)


def get_parent_path(path):
    """返回字段所属的父级分类路径"""
    if path == "<root>":
        return "<root>"
    if ".[" in path:
        dot_index = path.rfind(".")
        bracket_index = path.rfind("[")
        split_index = max(dot_index, bracket_index)
    else:
        split_index = max(path.rfind("."), path.rfind("["))

    if split_index == -1:
        return "<root>"
    return path[:split_index]


def format_field_value(value):
    """格式化字段值，便于终端展示"""
    rendered = json.dumps(value, ensure_ascii=False)
    if len(rendered) > 160:
        return rendered[:157] + "..."
    return rendered


def generate_field_report(field_tree, interesting_fields=None):
    """生成完整字段树报告"""
    grouped = {}
    type_stats = {}

    for path, info in sorted(field_tree.items()):
        group_name = get_parent_path(path)
        grouped.setdefault(group_name, []).append((path, info))
        value_type = info["type"]
        type_stats[value_type] = type_stats.get(value_type, 0) + 1

    lines = ["  ── 完整字段树报告 ──"]

    for group_name, items in grouped.items():
        lines.append(f"\n  [{group_name}]")
        for path, info in items:
            marker = "★ " if is_interesting_field(path, interesting_fields) else "  "
            value = format_field_value(info["value"])
            lines.append(f"{marker}{path} = {value} ({info['type']})")

    lines.append("\n  ── 字段类型统计 ──")
    lines.append(f"  总字段数: {len(field_tree)}")
    for type_name in sorted(type_stats):
        lines.append(f"  {type_name}: {type_stats[type_name]}")

    return "\n".join(lines)


def save_capture(data, ts=None, room_id=None, output_dir=None):
    """保存完整响应到文件"""
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    room_id = room_id or CURRENT_ROOM_ID
    output_dir = output_dir or CURRENT_OUTPUT_DIR
    filename = output_dir / f"reflow_{room_id}_{ts}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存响应失败: %s", e)
        return None
    return filename


def save_field_tree(field_tree, ts, room_id=None, output_dir=None):
    """保存字段树到单独文件"""
    room_id = room_id or CURRENT_ROOM_ID
    output_dir = output_dir or CURRENT_OUTPUT_DIR
    filename = output_dir / f"reflow_{room_id}_{ts}_field_tree.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(field_tree, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存字段树失败: %s", e)
        return None
    return filename


def save_analysis_json(analysis, ts, room_id=None, output_dir=None):
    """保存结构化分析结果。"""
    room_id = room_id or CURRENT_ROOM_ID
    output_dir = output_dir or CURRENT_OUTPUT_DIR
    filename = output_dir / f"reflow_analysis_{room_id}_{ts}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存分析结果失败: %s", e)
        return None
    return filename


def print_banner(room_id, web_room_id, interval, output_dir):
    print("=" * 60)
    print(f"  抖音直播间监控  room_id={room_id}")
    print(f"  直播间地址: https://live.douyin.com/{web_room_id}")
    print(f"  轮询间隔: {interval}s")
    print(f"  保存目录: {output_dir}")
    print("=" * 60)


def main():
    global CURRENT_ROOM_ID, CURRENT_WEB_ROOM_ID, CURRENT_OUTPUT_DIR

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="抖音直播间 reflow 接口监控")
    parser.add_argument("--room-id", default=ROOM_ID, help="内部房间 ID（reflow/info 使用，非 Web 房间号）")
    parser.add_argument("--web-room-id", default=WEB_ROOM_ID, help="Web 房间号")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔(秒)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    CURRENT_ROOM_ID = args.room_id
    CURRENT_WEB_ROOM_ID = args.web_room_id
    CURRENT_OUTPUT_DIR = ensure_output_dir(Path(args.output_dir))
    print_banner(args.room_id, args.web_room_id, args.interval, CURRENT_OUTPUT_DIR)

    was_live = False
    poll_count = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10

    try:
        while True:
            poll_count += 1
            now = datetime.now().strftime("%H:%M:%S")
            logger.info("[%s] 第 %s 次轮询...", now, poll_count)

            code, data = poll_reflow(args.room_id, args.web_room_id)
            if data is None:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("连续 %s 次请求失败，停止监控", consecutive_failures)
                    break
                # 连续失败时指数退避，避免 hammering API
                backoff = min(args.interval * (2 ** min(consecutive_failures - 1, 5)), 300)
                logger.info("跳过 (请求失败 %s/%s)，%s秒后重试", consecutive_failures, MAX_CONSECUTIVE_FAILURES, backoff)
                time.sleep(backoff)
                continue

            consecutive_failures = 0

        status = extract_room_status(data)
        key_changes = extract_interesting_fields(data)
        field_tree = build_field_tree(data)

        if status == 2:
            state = "直播中"
            if not was_live:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                event_status = "live_start"
                logger.info("%s", "=" * 50)
                logger.info("*** 检测到开播! 保存完整响应... ***")
                logger.info("%s", "=" * 50)
                saved = save_capture(data, ts, args.room_id, CURRENT_OUTPUT_DIR)
                logger.info("已保存: %s", saved)

                field_tree_saved = save_field_tree(field_tree, ts, args.room_id, CURRENT_OUTPUT_DIR)
                logger.info("字段树已保存: %s", field_tree_saved)

                analysis = {
                    "type": "reflow_monitor",
                    "timestamp": ts,
                    "room_id": args.room_id,
                    "status": event_status,
                    "field_tree": field_tree,
                    "key_changes": key_changes,
                }
                analysis_path = save_analysis_json(analysis, ts, args.room_id, CURRENT_OUTPUT_DIR)
                logger.info("分析 JSON 已保存: %s", analysis_path)

                if key_changes:
                    logger.info("── 关键字段 ──")
                    for key, value in key_changes.items():
                        logger.info("%s = %s", key, value)
                else:
                    logger.info("(未找到预定义的感兴趣字段，完整数据已保存)")

                logger.info("\n%s", generate_field_report(field_tree, INTERESTING_KEYS))
                was_live = True
            else:
                event_status = "polling"
        elif status == 4:
            state = "已结束"
            event_status = "stream_change" if was_live else "polling"
            if was_live:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                analysis = {
                    "type": "reflow_monitor",
                    "timestamp": ts,
                    "room_id": args.room_id,
                    "status": event_status,
                    "field_tree": field_tree,
                    "key_changes": key_changes,
                }
                analysis_path = save_analysis_json(analysis, ts, args.room_id, CURRENT_OUTPUT_DIR)
                logger.info("直播已结束 (之前检测到开播)")
                logger.info("分析 JSON 已保存: %s", analysis_path)
                was_live = False
        elif status is not None:
            state = f"其他状态 (status={status})"
            event_status = "polling"
        else:
            state = "未知"
            event_status = "polling"
            # 保存非标准响应以便排查
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved = save_capture(data, ts, args.room_id, CURRENT_OUTPUT_DIR)
            analysis = {
                "type": "reflow_monitor",
                "timestamp": ts,
                "room_id": args.room_id,
                "status": event_status,
                "field_tree": field_tree,
                "key_changes": key_changes,
            }
            analysis_path = save_analysis_json(analysis, ts, args.room_id, CURRENT_OUTPUT_DIR)
            logger.info("状态未知，响应已保存: %s", saved)
            logger.info("分析 JSON 已保存: %s", analysis_path)

        logger.info("状态: %s", state)
        time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("监控已停止 (共轮询 %s 次)", poll_count)


if __name__ == "__main__":
    main()
