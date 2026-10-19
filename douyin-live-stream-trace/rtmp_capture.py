"""
RTMP 推流抓包分析脚本
通过 tshark (Wireshark CLI) 实时抓取 RTMP 流量，提取 Connect 命令中的 flashVer 字段
用于识别推流端使用的软件名称和版本
"""

import subprocess
import sys
import json
import argparse
import logging
import re
import os
from datetime import datetime
from pathlib import Path

from amf0 import parse_amf_value, parse_amf_object
from config import OUTPUT_DIR, RTMP_BINARY_KEYWORDS, SOFTWARE_PATTERNS, ensure_dirs

logger = logging.getLogger(__name__)
CURRENT_OUTPUT_DIR = OUTPUT_DIR


def _derive_app_from_tcurl(tc_url):
    """从 tcUrl 推导 app 作为回退"""
    if not tc_url:
        return None

    match = re.match(r"^rtmp(?:e|s|t|te)?://[^/]+/([^/?]+)", str(tc_url), re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_rtmp_connect_from_pcap(pcap_file):
    """从 pcap 二进制中提取 RTMP connect 命令及其 AMF0 属性对象"""
    standard_keys = [
        "flashVer",
        "tcUrl",
        "fpad",
        "capabilities",
        "audioCodecs",
        "videoCodecs",
        "videoFunction",
        "objectEncoding",
        "pageUrl",
        "swfUrl",
        "app",
    ]

    try:
        with open(pcap_file, "rb") as f:
            data = f.read()
    except Exception as e:
        logger.error("读取 pcap 失败: %s", e)
        return None

    pattern = b"\x02\x00\x07connect"
    start = 0

    while True:
        idx = data.find(pattern, start)
        if idx == -1:
            return None

        try:
            command, offset = parse_amf_value(data, idx)
            if command != "connect":
                raise ValueError("命令不是 connect")

            transaction_id, offset = parse_amf_value(data, offset)
            if offset < len(data) and data[offset] == 0x03:
                connect_object, offset = parse_amf_object(data, offset)
            else:
                connect_object, offset = parse_amf_value(data, offset)
            if not isinstance(connect_object, dict):
                raise ValueError("connect 属性对象不是字典")

            extracted = {k: connect_object.get(k) for k in standard_keys}
            if not extracted.get("app"):
                extracted["app"] = _derive_app_from_tcurl(extracted.get("tcUrl"))

            optional_args = None
            if offset < len(data):
                try:
                    optional_args, _ = parse_amf_value(data, offset)
                except Exception:
                    optional_args = None

            return {
                "command": command,
                "transaction_id": transaction_id,
                "connect_properties": extracted,
                "raw_connect_object": connect_object,
                "optional_args": optional_args,
                "offset": idx,
            }
        except Exception:
            start = idx + 1


def extract_rtmp_publish_name(pcap_file):
    """从 pcap 二进制中提取 RTMP publish 命令里的推流名/stream key"""
    try:
        with open(pcap_file, "rb") as f:
            data = f.read()
    except Exception as e:
        logger.error("读取 pcap 失败: %s", e)
        return None

    pattern = b"\x02\x00\x07publish"
    start = 0

    while True:
        idx = data.find(pattern, start)
        if idx == -1:
            return None

        try:
            command, offset = parse_amf_value(data, idx)
            if command != "publish":
                raise ValueError("命令不是 publish")

            _, offset = parse_amf_value(data, offset)
            _, offset = parse_amf_value(data, offset)  # command object/null

            publish_name, offset = parse_amf_value(data, offset)
            if not isinstance(publish_name, str) or not publish_name.strip():
                raise ValueError("publish stream name 无效")

            return publish_name.strip()
        except Exception:
            start = idx + 1


def generate_rtmp_report(connect_props, publish_name, flashver_findings):
    """生成结构化 RTMP 分析报告"""
    connect_properties = {}
    raw_connect = None
    transaction_id = None
    optional_args = None
    offset = None

    if connect_props:
        connect_properties = connect_props.get("connect_properties") or {}
        raw_connect = connect_props.get("raw_connect_object")
        transaction_id = connect_props.get("transaction_id")
        optional_args = connect_props.get("optional_args")
        offset = connect_props.get("offset")

    return {
        "connect_properties": connect_properties,
        "publish_info": {
            "stream_name": publish_name,
            "found": bool(publish_name),
        },
        "software_fingerprint": {
            "flashVer": connect_properties.get("flashVer"),
            "binary_matches": sorted(flashver_findings),
        },
        "raw_findings": {
            "connect_command": connect_props.get("command") if connect_props else None,
            "transaction_id": transaction_id,
            "connect_offset": offset,
            "raw_connect_object": raw_connect,
            "optional_args": optional_args,
        },
    }


def detect_push_software(connect_info, findings):
    """基于 connect 字段与二进制命中结果推断推流软件。"""
    import re
    connect_properties = (connect_info or {}).get("connect_properties") or {}
    flashver = str(connect_properties.get("flashVer") or "")
    combined = "\n".join([flashver, *sorted(findings or [])]).lower()

    # 使用 config.SOFTWARE_PATTERNS（统一维护 10 条规则）
    for pattern, name in SOFTWARE_PATTERNS:
        if re.search(pattern, combined):
            return name, "high"

    if flashver:
        return flashver, "low"
    if findings:
        return "未知(存在命中片段)", "low"
    return "未识别", "low"


def build_analysis_json(ts, interface, pcap_file, connect_info, findings):
    """生成额外的 RTMP 分析 JSON。"""
    connect_fields = {}
    if connect_info:
        connect_properties = connect_info.get("connect_properties") or {}
        connect_fields = {
            "flashVer": connect_properties.get("flashVer"),
            "tcUrl": connect_properties.get("tcUrl"),
            "app": connect_properties.get("app"),
            "pageUrl": connect_properties.get("pageUrl"),
            "swfUrl": connect_properties.get("swfUrl"),
            "publish": extract_rtmp_publish_name(pcap_file),
        }

    findings_list = sorted(findings)
    detected_software, confidence_level = detect_push_software(connect_info, findings)
    return {
        "type": "rtmp_analysis",
        "timestamp": ts,
        "interface": interface,
        "pcap_path": str(pcap_file),
        "connect_fields": connect_fields,
        "software_findings": findings_list,
        "push_software": detected_software,
        "confidence": confidence_level,
    }


def save_analysis_json(analysis):
    """保存额外的 RTMP 分析 JSON。"""
    filename = CURRENT_OUTPUT_DIR / f"rtmp_analysis_{analysis['timestamp']}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存分析 JSON 失败: %s", e)
        return None
    return filename


def save_structured_rtmp_report(pcap_file, findings, interface=None):
    """提取并保存结构化 RTMP 报告"""
    connect_info = extract_rtmp_connect_from_pcap(pcap_file)
    publish_name = extract_rtmp_publish_name(pcap_file)
    report_data = generate_rtmp_report(connect_info, publish_name, findings)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    structured_path = CURRENT_OUTPUT_DIR / f"rtmp_structured_{ts}.json"
    try:
        with open(structured_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存结构化 RTMP 报告失败: %s", e)

    analysis = build_analysis_json(ts, interface, pcap_file, connect_info, findings)
    json_path = save_analysis_json(analysis)

    logging.info("\n── 结构化 RTMP 提取结果 ──")
    if connect_info and report_data["connect_properties"]:
        logging.info("  Connect 属性:")
        for key in [
            "app",
            "flashVer",
            "tcUrl",
            "pageUrl",
            "swfUrl",
            "fpad",
            "capabilities",
            "audioCodecs",
            "videoCodecs",
            "videoFunction",
            "objectEncoding",
        ]:
            value = report_data["connect_properties"].get(key)
            if value is not None:
                logging.info("    %s: %s", key, value)
    else:
        logging.info("  (未解析到 RTMP Connect 属性对象)")

    if publish_name:
        logging.info("  推流名称: %s", publish_name)
    else:
        logging.info("  推流名称: (未解析到 publish stream name)")

    logging.info("  结构化 JSON: %s", structured_path)
    logging.info("  分析 JSON: %s", json_path)
    return report_data, structured_path, analysis, json_path


def find_tshark():
    """查找 tshark 可执行文件"""
    # 常见安装路径
    common_paths = [
        r"C:\Program Files\Wireshark\tshark.exe",
        r"C:\Program Files (x86)\Wireshark\tshark.exe",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p

    # 尝试 PATH
    import shutil
    found = shutil.which("tshark")
    if found:
        return found

    return None


def check_npcap():
    """检查 Npcap 是否安装 (tshark 抓包依赖)"""
    return os.path.exists(r"C:\Program Files\Npcap\npcap.sys") or \
           os.path.exists(r"C:\Windows\System32\Npcap")


def list_interfaces(tshark_path):
    """列出可用网络接口"""
    logger.info("正在获取网络接口列表...")
    try:
        result = subprocess.run(
            [tshark_path, "-D"],
            capture_output=True, text=True, timeout=10
        )
        print(result.stdout)
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("获取接口超时")
        return ""


def capture_rtmp(tshark_path, interface=None, duration=300, pcap_file=None, filter_ip=None):
    """
    抓取 RTMP 流量
    - interface: 网卡编号或名称 (None 则自动选择)
    - duration: 抓包时长(秒)，默认5分钟
    - pcap_file: 保存的 pcap 文件路径
    """
    if pcap_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pcap_file = CURRENT_OUTPUT_DIR / f"rtmp_capture_{ts}.pcap"

    cmd = [
        tshark_path,
        "-a", f"duration:{duration}",
        "-w", str(pcap_file),
    ]
    if interface:
        cmd.extend(["-i", str(interface)])

    # 使用捕获过滤器提高效率 (BPF 语法)
    # RTMP 默认在 TCP 1935 端口
    capture_filter = "tcp port 1935"
    if filter_ip:
        capture_filter = f"{capture_filter} and host {filter_ip}"
    cmd.extend(["-f", capture_filter])

    logger.info("开始抓包 (最长 %s 秒)...", duration)
    logger.info("保存到: %s", pcap_file)
    logger.info("过滤: %s", capture_filter)
    logger.info("按 Ctrl+C 提前结束")

    try:
        subprocess.run(cmd, timeout=duration + 30)
    except KeyboardInterrupt:
        logger.warning("用户中断抓包")
    except subprocess.TimeoutExpired:
        logger.error("抓包超时 (%s秒)", duration + 30)
        return None
    except Exception as e:
        logger.error("抓包失败: %s", e)
        return None

    return str(pcap_file)


def analyze_pcap(tshark_path, pcap_file):
    """分析 pcap 文件，提取 RTMP Connect 中的关键字段"""
    logger.info("分析文件: %s", pcap_file)

    # 提取 RTMP Connect 命令的详细信息
    fields = [
        "rtmp.command",           # 命令类型 (connect)
        "frame.time",             # 时间
        "ip.src",                 # 源 IP
        "ip.dst",                 # 目标 IP
        "tcp.srcport",            # 源端口
        "tcp.dstport",            # 目标端口
    ]

    # 先尝试提取 RTMP 所有可用字段
    cmd = [
        tshark_path,
        "-r", pcap_file,
        "-Y", "rtmp",
        "-T", "json",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if not result.stdout.strip() or result.stdout.strip() == "[]":
            logger.warning("未发现 RTMP 协议数据包")
            logger.warning("可能原因:")
            logger.warning("    1. 抓包期间没有 RTMP 推流活动")
            logger.warning("    2. 推流使用了非标准端口")
            logger.warning("    3. 需要管理员权限抓包")
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("分析超时")
        return None


def extract_flashver_from_raw(pcap_file):
    """
    直接从 pcap 二进制中搜索 flashVer 字符串
    RTMP Connect 的参数以 AMF 编码，flashVer 通常以明文形式出现
    """
    findings = set()
    try:
        with open(pcap_file, "rb") as f:
            data = f.read()

        # 搜索 "flashVer" 附近的字符串
        # RTMP AMF0 编码中，字符串前有 type marker (0x02) + length (2 bytes)
        pattern = rb'flashVer\x02.{0,3}([\x20-\x7e]{4,80})'
        matches = re.findall(pattern, data)
        for m in matches:
            try:
                findings.add(m.decode("ascii", errors="ignore"))
            except Exception:
                pass

        # 也搜索 "flashver" (小写变体) 和 "FMLE" "OBS" 等常见标识（含大小写变体）
        for keyword in RTMP_BINARY_KEYWORDS:
            idx = 0
            while True:
                idx = data.find(keyword, idx)
                if idx == -1:
                    break
                # 提取关键词前后各 50 字节的可读文本
                start = max(0, idx - 20)
                end = min(len(data), idx + 80)
                snippet = data[start:end]
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in snippet)
                findings.add(f"[offset={idx}] {text}")
                idx += len(keyword)

    except Exception as e:
        logger.warning("二进制搜索异常: %s", e)

    return findings


def analyze_pcap_with_tshark_fields(tshark_path, pcap_file):
    """使用 tshark 提取 RTMP 协议特定字段"""
    logger.info("\n── tshark RTMP 字段提取 ──")

    # 尝试多种 RTMP 相关字段
    rtmp_fields = [
        "rtmp.command",
        "rtmp.transaction_id",
        "rtmp.property.string",
        "rtmp.value.string",
    ]

    field_str = ",".join(rtmp_fields)
    cmd = [
        tshark_path,
        "-r", pcap_file,
        "-Y", "rtmp.command == connect || rtmp",
        "-T", "fields",
        "-E", "separator=|",
        "-E", "header=y",
        "-e", field_str,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            print(result.stdout)
            return result.stdout
        else:
            logger.info("(无 RTMP 字段数据)")
            return None
    except subprocess.TimeoutExpired:
        logger.warning("提取超时")
        return None


def main():
    global CURRENT_OUTPUT_DIR

    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="RTMP 推流捕获与分析")
    parser.add_argument("--interface", "-i", default="", help="网络接口（默认自动检测）")
    parser.add_argument("--duration", "-d", type=int, default=30, help="捕获时长(秒)")
    parser.add_argument("--filter-ip", help="过滤目标 IP")
    parser.add_argument("--pcap", help="分析已有 pcap 文件")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    CURRENT_OUTPUT_DIR = Path(args.output_dir)
    CURRENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ensure_dirs()

    print("=" * 60)
    print("  RTMP 推流抓包分析工具")
    print("  用于识别推流端软件 (flashVer / encoder)")
    print("=" * 60)

    # 1. 检查依赖
    tshark = find_tshark()
    if not tshark:
        logger.error("未找到 tshark (Wireshark CLI)")
        logger.error("请安装 Wireshark: https://www.wireshark.org/download.html")
        logger.error("安装时勾选 tshark 和 Npcap")
        logger.error("如果已安装，可手动指定路径后重试")
        return

    logger.info("tshark: %s", tshark)

    if not check_npcap():
        logger.warning("未检测到 Npcap，抓包可能失败")
        logger.warning("安装 Wireshark 时需勾选 Npcap")

    if args.pcap:
        pcap_path = args.pcap.strip().strip('"')
        if not os.path.isfile(pcap_path):
            logger.error("文件不存在: %s", pcap_path)
            return

        logger.info("[步骤1] tshark RTMP 字段分析...")
        analyze_pcap_with_tshark_fields(tshark, pcap_path)

        logger.info("[步骤2] 二进制搜索 flashVer/推流软件标识...")
        findings = extract_flashver_from_raw(pcap_path)
        if findings:
            print("\n  发现以下内容:")
            for finding in sorted(findings):
                print(f"    → {finding}")
        else:
            logger.info("(未发现明显标识)")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report = CURRENT_OUTPUT_DIR / f"rtmp_report_{ts}.txt"
        try:
            with open(report, "w", encoding="utf-8") as f:
                f.write(f"RTMP 分析报告 - {pcap_path}\n")
                f.write("=" * 50 + "\n\n")
                for finding in sorted(findings):
                    f.write(f"{finding}\n")
            print(f"\n[*] 报告已保存: {report}")
        except OSError as e:
            logger.error("保存报告失败: %s", e)

        save_structured_rtmp_report(pcap_path, findings, interface=args.interface)
        return

    # 2. 选择模式
    print("\n── 选择模式 ──")
    print("  1. 实时抓包 (需要管理员权限运行)")
    print("  2. 分析已有 pcap 文件")
    print("  3. 查看网络接口列表")

    choice = input("\n请选择 (1/2/3): ").strip()

    if choice == "3":
        list_interfaces(tshark)
        return

    if choice == "2":
        pcap_path = input("请输入 pcap 文件路径: ").strip().strip('"')
        if not os.path.isfile(pcap_path):
            logger.error("文件不存在")
            return

        logger.info("[步骤1] tshark RTMP 字段分析...")
        analyze_pcap_with_tshark_fields(tshark, pcap_path)

        logger.info("[步骤2] 二进制搜索 flashVer/推流软件标识...")
        findings = extract_flashver_from_raw(pcap_path)
        if findings:
            print("  发现以下内容:")
            for f in sorted(findings):
                print(f"    → {f}")
        else:
            logger.info("(未发现明显标识)")

        # 保存结果
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report = CURRENT_OUTPUT_DIR / f"rtmp_report_{ts}.txt"
        try:
            with open(report, "w", encoding="utf-8") as f:
                f.write(f"RTMP 分析报告 - {pcap_path}\n")
                f.write("=" * 50 + "\n\n")
                for finding in sorted(findings):
                    f.write(f"{finding}\n")
            print(f"\n[*] 报告已保存: {report}")
        except OSError as e:
            logger.error("保存报告失败: %s", e)

        save_structured_rtmp_report(pcap_path, findings, interface=args.interface)
        return

    if choice == "1":
        print("\n── 实时抓包 ──")
        list_interfaces(tshark)

        default_iface = str(args.interface) if args.interface else ""
        iface = input(f"输入网卡编号 (留空使用 {'自动选择' if not default_iface else default_iface}): ").strip()
        duration_input = input(f"抓包时长(秒，默认{args.duration}): ").strip()
        duration = int(duration_input) if duration_input.isdigit() else args.duration

        chosen_iface = iface or (default_iface or None)

        pcap_file = capture_rtmp(
            tshark,
            interface=chosen_iface,
            duration=duration,
            filter_ip=args.filter_ip,
        )
        if pcap_file and os.path.isfile(pcap_file):
            print(f"\n[*] 抓包完成: {pcap_file}")
            logger.info("\n── 分析抓包数据 ──")
            analyze_pcap_with_tshark_fields(tshark, pcap_file)

            findings = extract_flashver_from_raw(pcap_file)
            if findings:
                print("\n  发现以下标识:")
                for f in sorted(findings):
                    print(f"    → {f}")

            save_structured_rtmp_report(pcap_file, findings, interface=chosen_iface)
        return

    logger.error("无效选择")


if __name__ == "__main__":
    main()
