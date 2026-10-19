"""抖音直播间一键多角度分析入口。"""

import argparse
import importlib
import re
import json
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from amf0 import ci_get
from capture_manifest import append_artifact, save_manifest
from config import (
    ROOM_ID, WEB_ROOM_ID, OUTPUT_DIR, REPORT_DIR,
    COMMON_HEADERS, API_TIMEOUT, DOWNLOAD_MAX_BYTES,
    REFLOW_URLS, ENTER_URLS,
    SOFTWARE_PATTERNS, CDN_PATTERNS,
    ensure_dirs,
    CAPTURE_SECONDS,
)
from flv_analyzer import (
    build_headers,
    get_stream_url,
    download_stream_sample,
    download_stream_sample_timed,
    analyze_payload,
    analyze_audio_tracks,
    parse_stream_url_full,
    analyze_stream_url_patterns,
    ffprobe_stream,
    build_audio_only_url,
    append_extra_params,
)
from monitor_reflow import (
    poll_reflow,
    build_field_tree,
    extract_interesting_fields,
    extract_room_status,
    save_capture,
    save_field_tree,
    save_analysis_json,
)
from network import http_get, download_bytes, download_response_headers
from unified_report import (
    open_report_file,
    discover_json_results,
    scan_captures,
    merge_results_into_files_data,
    correlate_by_time,
    enrich_sessions,
    filter_sessions_by_room_id,
    generate_text_report,
    generate_html_report,
)


from av_sync_detector import run_av_sync_analysis

logger = logging.getLogger(__name__)


def make_step_result() -> dict[str, Any]:
    return {"success": False, "data": None, "files": [], "error": None}


def banner(step_no: int, title: str) -> None:
    print(f"\n════════ Step {step_no}: {title} ════════", flush=True)


def save_json(path: Path, data: Any) -> Path:
    try:
        with open(path, "w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2, default=str)
    except OSError as e:
        logger.error("保存 JSON 失败 (%s): %s", path, e)
    return path


def status_text(status: int | None) -> str:
    _map = {1: "未开播", 2: "直播中", 3: "准备中", 4: "已结束"}
    if status in _map:
        return _map[status]
    if status is None:
        return "未知（API 未返回）"
    return f"未知 (status={status})"


def format_error(exc: Exception) -> str:
    msg = str(exc)
    prefix = f"{type(exc).__name__}: "
    if msg.startswith(prefix):
        return msg
    return msg or type(exc).__name__


def format_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "未知"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def extract_internal_room_id(room: dict[str, Any] | None) -> str | None:
    if not isinstance(room, dict):
        return None
    room_id = room.get("id_str") or room.get("id")
    if room_id is None:
        return None
    return str(room_id)


def extract_anchor_id(room: dict[str, Any] | None) -> str | None:
    if not isinstance(room, dict):
        return None
    owner = room.get("owner")
    if isinstance(owner, dict):
        anchor_id = owner.get("id_str") or owner.get("id") or owner.get("webcast_uid")
        if anchor_id is not None:
            return str(anchor_id)

    anchor_id = (
        room.get("owner_user_id_str")
        or room.get("owner_user_id")
        or room.get("anchor_id")
    )
    if anchor_id is None:
        return None
    return str(anchor_id)


def extract_room_from_enter_response(data: Any) -> dict[str, Any] | None:
    try:
        room = data["data"]["data"][0]
    except (KeyError, TypeError, IndexError):
        return None
    return room if isinstance(room, dict) else None


def extract_room_from_reflow_response(data: Any) -> dict[str, Any] | None:
    try:
        room = data["data"]["room"]
    except (KeyError, TypeError):
        return None
    return room if isinstance(room, dict) else None


def collect_quality_options(stream_url_obj: dict[str, Any] | None) -> list[str]:
    parsed = parse_stream_url_full(stream_url_obj or {}) or {}
    quality_map = parsed.get("quality_map")
    if isinstance(quality_map, dict):
        return sorted(str(key) for key in quality_map)
    return []


def request_enter_room(web_room_id: str, headers: dict[str, str], need_double_stream: bool = False) -> dict[str, Any]:
    params = {
        "app_id": "1128",
        "web_rid": web_room_id,
        "Room-Enter-User-Login-Ab": "0",
        "is_need_double_stream": "true" if need_double_stream else "false",
        "support_drm": "false",
        "Support-Degrade-Non-Encode-Stream": "false",
    }

    last_error = "所有 web/enter 域名均请求失败"
    for enter_url in ENTER_URLS:
        response = http_get(enter_url, params=params, headers=headers, timeout=API_TIMEOUT)
        if response is None:
            last_error = f"{enter_url} 无响应"
            continue

        try:
            data = response.json()
        except Exception as exc:
            last_error = f"{enter_url} JSON 解析失败: {exc}"
            continue

        status_code = data.get("status_code") if isinstance(data, dict) else None
        if status_code == 10011:
            last_error = f"{enter_url} 返回鉴权错误 10011"
            continue

        room = extract_room_from_enter_response(data)
        return {
            "ok": True,
            "url": enter_url,
            "response": data,
            "room": room,
            "error": None,
        }

    return {
        "ok": False,
        "url": None,
        "response": None,
        "room": None,
        "error": last_error,
    }


def dedupe_sessions_by_id(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if session_id in seen:
            continue
        seen.add(session_id)
        result.append(session)
    return result


def pick_summary_conclusion(
    step5: dict[str, Any],
    step6: dict[str, Any],
) -> dict[str, Any]:
    if step5.get("success") and isinstance(step5.get("data"), dict):
        data = step5["data"]
        fingerprint = data.get("fingerprint") or {}
        metadata = data.get("metadata") or {}
        confidence_key = str(fingerprint.get("confidence") or "low")
        bitrate = (
            ci_get(metadata, "videodatarate")
            or ci_get(metadata, "videoDataRate")
            or ci_get(metadata, "videobitrate")
            or ci_get(metadata, "bitrate")
        )
        return {
            "software": fingerprint.get("push_software") or "未知",
            "confidence": {"high": "高", "medium": "中", "low": "低"}.get(confidence_key, "低"),
            "resolution": fingerprint.get("resolution") or "未知",
            "video_codec": fingerprint.get("video_codec") or "未知",
            "audio_codec": fingerprint.get("audio_codec") or "未知",
            "video_bitrate": bitrate,
            "cdn": fingerprint.get("cdn_provider") or "未知",
        }

    sessions = []
    if step6.get("success") and isinstance(step6.get("data"), dict):
        sessions = step6["data"].get("sessions") or []
    if sessions:
        # P1-3 fix: 取最新 session（sessions 按时间正序，[-1] 是最新的）
        conclusion = sessions[-1].get("push_conclusion") or {}
        return {
            "software": conclusion.get("software") or "未知",
            "confidence": conclusion.get("confidence") or "低",
            "resolution": conclusion.get("resolution") or "未知",
            "video_codec": conclusion.get("codec") or "未知",
            "audio_codec": "未知",
            "video_bitrate": None,
            "cdn": conclusion.get("cdn") or "未知",
        }

    return {
        "software": "未知",
        "confidence": "低",
        "resolution": "未知",
        "video_codec": "未知",
        "audio_codec": "未知",
        "video_bitrate": None,
        "cdn": "未知",
    }


def step_line(step_result: dict[str, Any], success_text: str, fallback_text: str | None = None) -> str:
    if step_result.get("success"):
        return f"✅ {success_text}"
    error = step_result.get("error") or fallback_text or "未执行"
    return f"❌ {error}"


def print_final_summary(
    web_room_id: str,
    internal_room_id: str | None,
    room_status: int | None,
    steps: dict[str, dict[str, Any]],
    report_format: str,
) -> None:
    summary = pick_summary_conclusion(steps["step5"], steps["step6"])
    flv_size = None
    if steps["step4"].get("success") and isinstance(steps["step4"].get("data"), dict):
        flv_size = steps["step4"]["data"].get("size")

    reflow_count = None
    if steps["step2"].get("success") and isinstance(steps["step2"].get("data"), dict):
        reflow_count = steps["step2"]["data"].get("field_tree_count")

    metadata_count = None
    if steps["step5"].get("success") and isinstance(steps["step5"].get("data"), dict):
        metadata_count = len((steps["step5"]["data"].get("metadata_hints") or {}).keys())

    text_path = None
    html_path = None
    if steps["step6"].get("success") and isinstance(steps["step6"].get("data"), dict):
        text_path = steps["step6"]["data"].get("text_path")
        html_path = steps["step6"]["data"].get("html_path")

    print("\n══════════════════════════════════════════")
    print("  抖音直播间全角度分析报告")
    print("══════════════════════════════════════════")
    print(f"房间号: {web_room_id} (Web) / {internal_room_id or '未知'} (内部)")
    print(f"直播间: https://live.douyin.com/{web_room_id}")
    print(f"状态: {status_text(room_status)}")
    print()
    print("── 采集结果 ──")
    print(step_line(
        steps["step2"],
        f"reflow/info API: 成功 ({reflow_count or 0} 个字段)",
        f"reflow/info API: {steps['step2'].get('error') or '失败'}",
    ))
    web_enter_text = "web/enter API: 成功"
    if steps["step3"].get("success") and isinstance(steps["step3"].get("data"), dict):
        if steps["step3"]["data"].get("selected_url"):
            web_enter_text += " (获取到流地址)"
        else:
            web_enter_text += " (房间未直播，未下载 FLV)"
    print(step_line(steps["step3"], web_enter_text, f"web/enter API: {steps['step3'].get('error') or '失败'}"))
    print(step_line(
        steps["step4"],
        f"FLV 样本下载: 成功 ({format_size(flv_size)})",
        f"FLV 样本下载: {steps['step4'].get('error') or '未执行'}",
    ))
    print(step_line(
        steps["step5"],
        f"FLV 元数据解析: 成功 ({metadata_count or 0} 个线索字段)",
        f"FLV 元数据解析: {steps['step5'].get('error') or '未执行'}",
    ))
    print(step_line(steps["step6"], "统一报告: 成功", f"统一报告: {steps['step6'].get('error') or '失败'}"))
    print()
    print("── 推流端识别 ──")
    print(f"推流软件: {summary['software']}")
    print(f"置信度: {summary['confidence']}")
    print(f"分辨率: {summary['resolution']}")
    print(f"视频编码: {summary['video_codec']}")
    print(f"音频编码: {summary['audio_codec']}")
    print(f"视频码率: {summary['video_bitrate'] if summary['video_bitrate'] is not None else '未知'}")
    print(f"CDN: {summary['cdn']}")

    # 双流检测结果
    if steps.get("step5b") and steps["step5b"].get("success"):
        dual_data = steps["step5b"].get("data") or {}
        print()
        print("── 双流异常检测 ──")
        verdict_map = {"none": "未发现异常", "low": "低风险", "medium": "中风险", "high": "高风险"}
        verdict = dual_data.get("verdict", "none")
        print(f"风险等级: {verdict_map.get(verdict, verdict)}")
        print(f"风险评分: {dual_data.get('score', 0)}/100")
        signals = dual_data.get("signals", [])
        if signals:
            print(f"异常信号: {len(signals)} 个")
            for sig in signals[:5]:
                print(f"  - [{sig.get('strength', '?')}] {sig.get('description', '')}")
        suppressors = dual_data.get("suppressors", [])
        if suppressors:
            print(f"抑制因素: {len(suppressors)} 个")
            for sup in suppressors[:3]:
                print(f"  - {sup.get('description', '')}")
    elif steps.get("step5b") and steps["step5b"].get("error"):
        print()
        print(f"── 双流异常检测: {steps['step5b']['error']} ──")

    # 音画同步检测结果
    if steps.get("step5d") and steps["step5d"].get("success"):
        _av = steps["step5d"].get("data") or {}
        _p = _av.get("perceptual") or {}
        _so, _pr = _p.get("stream_offset_ms"), _p.get("player_residual_ms")
        print()
        print("── 音画同步检测 ──")
        print(f"感知判定: {_av.get('verdict', '—')}")
        if isinstance(_so, (int, float)):
            print(f"流内偏移: {_so:+.2f} ms（{_p.get('direction', '—')}），峰值 {_p.get('stream_peak_abs_ms', '—')} ms")
        print(f"播放器残差: {'未测' if _pr is None else f'{_pr:+.2f} ms'}")
    elif steps.get("step5d") and steps["step5d"].get("error"):
        print()
        print(f"── 音画同步检测: {steps['step5d']['error']} ──")

    if steps.get("step5e") and steps["step5e"].get("success"):
        _lip = steps["step5e"].get("data") or {}
        _lp = _lip.get("perceptual") or {}
        print()
        print("── 人脸嘴型音画同步（内容级）──")
        print(f"判定: {_lip.get('verdict', '—')}（置信度 {_lp.get('confidence', '—')}）")
        if isinstance(_lp.get("content_offset_ms"), (int, float)):
            print(f"内容级偏移: {_lp['content_offset_ms']:+.2f} ms（{_lp.get('direction', '—')}）"
                  f" | 相关峰 r={(_lip.get('metrics') or {}).get('peak_correlation')}")
    elif steps.get("step5e") and steps["step5e"].get("error"):
        print()
        print(f"── 人脸嘴型音画同步: {steps['step5e']['error']} ──")

    print()
    print("── 报告文件 ──")
    if report_format in {"text", "both"}:
        print(f"文本报告: {text_path or '未生成'}")
    if report_format in {"html", "both"}:
        print(f"HTML报告: {html_path or '未生成'}")
        if html_path and Path(html_path).exists():
            open_report_file(html_path)

    # P1-1(Metis) fix: 房间未开播或数据不完整时的明确警告
    success_count = sum(1 for s in steps.values() if s.get("success"))
    if room_status != 2:
        print()
        print("⚠️  注意: 房间当前未直播（{}），以下数据可能不完整:".format(status_text(room_status)))
        if not steps["step4"].get("success"):
            print("   - FLV 样本未下载（仅直播中可获取）")
        if not steps["step5"].get("success"):
            print("   - 推流端指纹未提取（需要 FLV 样本）")
        print("   建议: 在直播开始后重新运行分析")
    elif success_count < 4:
        print()
        print("⚠️  注意: 部分步骤失败，推流端识别结果可能不完整")
        print("   建议: 检查网络连接或尝试添加鉴权参数")
    print("══════════════════════════════════════════")


def extract_room(raw: str) -> str | None:
    """从用户输入中提取房间号（支持纯数字或 live.douyin.com URL）。"""
    raw = raw.strip()
    if not raw:
        return None
    m = re.search(r"live\.douyin\.com/(\d+)", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    return None


def _fix_console_encoding():
    """修复 Windows 控制台编码为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def interactive_loop(parser, remaining_argv=None):
    """交互式循环：反复提示输入房间号进行分析。"""
    _fix_console_encoding()

    print()
    print("抖音直播间分析工具（输入 q 退出）")
    print("用法: 输入房间号（如 376389454958）或链接（如 https://live.douyin.com/376389454958）")
    print()

    while True:
        try:
            raw = input("房间号/链接: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break

        if raw.lower() == "q":
            print("再见")
            break

        room = extract_room(raw)
        if not room:
            print("  ! 无法识别，请输入房间号或 live.douyin.com 链接")
            continue

        print(f"  → 分析房间 {room} ...")
        # 清空 captures 目录，避免历史数据污染本次报告
        captures_dir = OUTPUT_DIR
        if captures_dir.exists():
            shutil.rmtree(captures_dir)
        captures_dir.mkdir()
        print()

        try:
            run_analysis(parser.parse_args(
                ["--room", room, "--playwright", "--format", "both"]
                + (remaining_argv or [])
            ))
        except SystemExit:
            pass
        print()


def main(argv=None):
    """入口：有 --room 直接分析，否则进入交互循环。"""
    parser = argparse.ArgumentParser(description="抖音直播间一键多角度分析器")
    parser.add_argument("--room", help="房间号或分享链接（live.douyin.com/xxx、v.douyin.com/xxx）；也可直接作为位置参数传入")
    parser.add_argument("--internal-id", help="内部 room_id，提供后跳过自动解析")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    parser.add_argument("--report-dir", default=str(REPORT_DIR), help="报告目录")
    parser.add_argument("--format", choices=["html", "text", "both"], default="both", help="报告格式")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--cookies", help="浏览器 Cookie 字符串（用于 API 鉴权）")
    parser.add_argument("--playwright", action="store_true", help="使用 Playwright 浏览器鉴权（自动获取 cookies + web/enter 响应）")
    parser.add_argument("--detect-dual-stream", action="store_true", help="启用双流异常检测")
    parser.add_argument("--capture-probes", action="store_true", help="额外采集 gift/list、room/info、ranklist/audience 原始 JSON")
    parser.add_argument("--av-sync", action="store_true", help="音画同步检测（标签级 + 解码级，毫秒精度）")
    parser.add_argument("--no-lip-sync", dest="lip_sync", action="store_false", default=True,
                        help="关闭人脸嘴型（内容级）音画同步检测（默认开启）")
    parser.add_argument("--lip-sync-seconds", type=int, default=20, help="嘴型检测分析时长（秒）")
    parser.add_argument("--dual-qualities", default="FULL_HD1,SD1", help="双流检测对比的画质 (默认: FULL_HD1,SD1)")
    parser.add_argument("--dual-monitor-seconds", type=int, default=0, help="双流时序监控: 每隔 N 秒重新采样，检测中途换流 (默认: 0=不监控)")
    parser.add_argument("--extra-stream-urls", help="额外流 URL 文件路径（每行一条，格式: 标签<TAB>URL 或纯 URL）")
    parser.add_argument("--capture-seconds", type=int, default=0, help="并发抓取各清晰度流的时长（秒），0=不抓取 (默认: 0)")
    parser.add_argument("--extra-params", action="store_true", help="向所有清晰度流 URL 附加 extra_stream_params.json 中启用的参数")

    if argv is None:
        argv = sys.argv[1:]

    # 位置参数支持：直接传 房间号 / 分享链接（live.douyin.com、v.douyin.com/xxx），等价于 --room
    if argv and not argv[0].startswith("-"):
        if not any(a == "--room" or a.startswith("--room=") for a in argv):
            argv = ["--room", argv[0]] + argv[1:]

    # --help / -h 直接交给 argparse
    if "-h" in argv or "--help" in argv:
        parser.parse_args(argv)
        return

    # 预扫描：是否提供了 --room
    has_room = False
    for i, arg in enumerate(argv):
        if arg == "--room" and i + 1 < len(argv):
            has_room = True
            break
        if arg.startswith("--room="):
            has_room = True
            break

    if has_room:
        args = parser.parse_args(argv)
        run_analysis(args)
    else:
        remaining = [a for a in argv if not a.startswith("--room")]
        interactive_loop(parser, remaining)


def run_analysis(args):
    """执行单次分析。"""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    _fix_console_encoding()

    ensure_dirs()
    output_dir = Path(args.output_dir)
    report_dir = Path(args.report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    web_room_id = str(args.room)
    _extracted = extract_room(web_room_id)
    if _extracted and _extracted != web_room_id:
        web_room_id = _extracted
        logger.info("从 URL 提取房间号: %s", web_room_id)
    internal_room_id = str(args.internal_id) if args.internal_id else None
    cookies = args.cookies
    headers = build_headers(web_room_id, cookies=cookies)

    logger.debug(
        "默认配置 ROOM_ID=%s WEB_ROOM_ID=%s REFLOW_URLS=%s ENTER_URLS=%s headers=%s timeout=%s max_bytes=%s software_rules=%s cdn_rules=%s",
        ROOM_ID,
        WEB_ROOM_ID,
        REFLOW_URLS,
        ENTER_URLS,
        COMMON_HEADERS.get("Referer"),
        API_TIMEOUT,
        DOWNLOAD_MAX_BYTES,
        len(SOFTWARE_PATTERNS),
        len(CDN_PATTERNS),
    )

    # Playwright 浏览器鉴权（可选）
    pw_ctx: dict[str, Any] | None = None
    if args.playwright:
        banner(0, "Playwright 浏览器鉴权")
        try:
            from auth_playwright import authenticate_room
            pw_ctx = authenticate_room(web_room_id)
            if pw_ctx.get("ok"):
                logger.info("Playwright 鉴权成功")
                if pw_ctx.get("cookies") and not cookies:
                    cookies = pw_ctx["cookies"]
                    headers = build_headers(web_room_id, cookies=cookies)
                    logger.info("已注入 Playwright 提取的 cookies")
                if pw_ctx.get("internal_room_id") and not internal_room_id:
                    internal_room_id = pw_ctx["internal_room_id"]
                    logger.info("已获取内部 room_id: %s", internal_room_id)
            else:
                logger.warning("Playwright 鉴权失败: %s", pw_ctx.get("error"))
        except ImportError as exc:
            logger.warning("Playwright 不可用: %s", exc)
        except Exception as exc:
            logger.exception("Playwright 鉴权异常")

    steps = {
        "step1": make_step_result(),
        "step2": make_step_result(),
        "step3": make_step_result(),
        "step4": make_step_result(),
        "step4b": make_step_result(),
        "step5": make_step_result(),
        "step5b": make_step_result(),
        "step5c": make_step_result(),
        "step5d": make_step_result(),
        "step5e": make_step_result(),
        "step6": make_step_result(),
    }

    session_manifest: dict[str, Any] = {
        "type": "session_manifest",
        "web_room_id": web_room_id,
        "timestamp": ts,
        "artifacts": [],
        "steps": {},
    }

    enter_room: dict[str, Any] | None = None
    enter_response: Any = None
    room_status_value: int | None = None
    selected_stream_url: str | None = None
    stream_url_obj: dict[str, Any] | None = None
    stream_url_struct_file: Path | None = None
    downloaded_data: bytes | None = None
    headers_info: dict[str, Any] = {}
    step1_stream_info: dict[str, Any] | None = None
    resolution_source = "user_provided" if args.internal_id else "unknown"
    _step_no = 0

    def next_banner(title: str) -> None:
        nonlocal _step_no
        _step_no += 1
        banner(_step_no, title)

    # 如果 Playwright 已提供完整鉴权上下文，直接填充 Step 1 结果
    if pw_ctx and pw_ctx.get("ok") and pw_ctx.get("room"):
        enter_room = pw_ctx["room"]
        room_status_value = pw_ctx.get("room_status")
        selected_stream_url = pw_ctx.get("selected_url")
        stream_url_obj = pw_ctx.get("stream_url_obj")
        if not internal_room_id:
            internal_room_id = pw_ctx.get("internal_room_id")
            if internal_room_id:
                resolution_source = "playwright"
        step1_stream_info = {
            "selected_url": selected_stream_url,
            "stream_url_obj": stream_url_obj or {},
            "room": enter_room,
        }
        logger.info("使用 Playwright 鉴权数据填充初始上下文")

    if pw_ctx and pw_ctx.get("ok"):
        from auth_playwright import save_playwright_artifact

        playwright_enter_path = output_dir / f"playwright_enter_{web_room_id}_{ts}.json"
        playwright_cookies_path = output_dir / f"playwright_cookies_{web_room_id}_{ts}.json"
        save_playwright_artifact(
            playwright_enter_path,
            pw_ctx.get("enter_response") or {
                "type": "playwright_enter",
                "web_room_id": web_room_id,
                "enter_response": None,
            },
        )
        save_playwright_artifact(
            playwright_cookies_path,
            {
                "type": "playwright_cookies",
                "web_room_id": web_room_id,
                "ms_token": pw_ctx.get("ms_token"),
                "cookie_string": pw_ctx.get("cookies"),
            },
        )
        pw_ctx["artifact_paths"] = {
            "playwright_enter": str(playwright_enter_path),
            "playwright_cookies": str(playwright_cookies_path),
        }
        append_artifact(session_manifest, "playwright_enter", playwright_enter_path, "json")
        append_artifact(session_manifest, "playwright_cookies", playwright_cookies_path, "json")

    next_banner("解析房间 ID")
    try:
        if internal_room_id:
            logger.info("使用用户提供的内部 room_id: %s", internal_room_id)
        else:
            stream_info = get_stream_url(web_room_id, headers, cookies=cookies, need_double_stream=args.detect_dual_stream)
            if stream_info:
                step1_stream_info = stream_info
                selected_stream_url = stream_info.get("selected_url")
                stream_url_obj = stream_info.get("stream_url_obj") or {}
                enter_room = stream_info.get("room") or {}
                room_status_value = enter_room.get("status") if isinstance(enter_room, dict) else None
                internal_room_id = extract_internal_room_id(enter_room)
                resolution_source = "get_stream_url"
                logger.info("通过 get_stream_url 解析到内部 room_id: %s", internal_room_id)

            if not internal_room_id:
                enter_lookup = request_enter_room(web_room_id, headers, need_double_stream=args.detect_dual_stream)
                enter_response = enter_lookup.get("response")
                if enter_lookup.get("ok"):
                    enter_room = enter_lookup.get("room")
                    room_status_value = enter_room.get("status") if isinstance(enter_room, dict) else room_status_value
                    internal_room_id = extract_internal_room_id(enter_room)
                    resolution_source = "web_enter_api"
                    logger.info("通过 web/enter 解析到内部 room_id: %s", internal_room_id)
                else:
                    logger.warning("web/enter 解析 room_id 失败: %s", enter_lookup.get("error"))

            if not internal_room_id:
                # reflow/info 需要内部 room_id，此时只能用 web_room_id 尝试
                # 多数情况下会失败（reflow/info 不接受 web_room_id），但某些域名的 API 可能容错
                logger.warning("尝试 reflow/info fallback（使用 web_room_id，可能失败）")
                code, reflow_data = poll_reflow(web_room_id, web_room_id, cookies=cookies)
                if reflow_data is not None:
                    reflow_room = extract_room_from_reflow_response(reflow_data)
                    internal_room_id = extract_internal_room_id(reflow_room)
                    if room_status_value is None:
                        room_status_value = extract_room_status(reflow_data)
                    resolution_source = "reflow_fallback"
                    logger.info("通过 reflow/info fallback 解析结果: status=%s internal_room_id=%s", code, internal_room_id)

        steps["step1"]["success"] = internal_room_id is not None
        steps["step1"]["data"] = {
            "web_room_id": web_room_id,
            "internal_room_id": internal_room_id,
            "room_status": room_status_value,
            "status_text": status_text(room_status_value),
        }
        room_resolution_path = output_dir / f"room_resolution_{web_room_id}_{ts}.json"
        room_resolution = {
            "type": "room_resolution",
            "timestamp": ts,
            "web_room_id": web_room_id,
            "internal_room_id": internal_room_id,
            "room_status": room_status_value,
            "status_text": status_text(room_status_value),
            "source": resolution_source,
        }
        save_json(room_resolution_path, room_resolution)
        append_artifact(session_manifest, "room_resolution", room_resolution_path, "json", {"source": resolution_source})
        if not internal_room_id:
            steps["step1"]["error"] = "未能解析内部 room_id"
    except Exception as exc:
        logger.exception("Step 1 失败")
        steps["step1"]["error"] = format_error(exc)

    next_banner("reflow/info 单次采集")
    try:
        if not internal_room_id:
            raise RuntimeError("缺少内部 room_id，跳过 reflow/info")

        code, data = poll_reflow(internal_room_id, web_room_id, cookies=cookies)
        if data is None:
            raise RuntimeError("reflow/info 未返回有效 JSON")

        room_status_value = extract_room_status(data) if room_status_value is None else room_status_value
        interesting_fields = extract_interesting_fields(data)
        field_tree = build_field_tree(data)

        raw_file = save_capture(data, ts, internal_room_id, output_dir)
        field_tree_file = save_field_tree(field_tree, ts, internal_room_id, output_dir)
        analysis = {
            "type": "reflow_monitor",
            "timestamp": ts,
            "room_id": internal_room_id,
            "web_room_id": web_room_id,
            "http_status": code,
            "room_status": room_status_value,
            "status_text": status_text(room_status_value),
            "interesting_fields": interesting_fields,
            "field_tree": field_tree,
            "field_tree_count": len(field_tree),
            "raw_response_file": str(raw_file),
            "field_tree_file": str(field_tree_file),
        }
        analysis_file = save_analysis_json(analysis, ts, internal_room_id, output_dir)

        steps["step2"]["success"] = True
        steps["step2"]["files"] = [str(raw_file), str(field_tree_file), str(analysis_file)]
        steps["step2"]["data"] = {
            "http_status": code,
            "room_status": room_status_value,
            "status_text": status_text(room_status_value),
            "interesting_fields": interesting_fields,
            "field_tree_count": len(field_tree),
        }
    except Exception as exc:
        logger.exception("Step 2 失败")
        steps["step2"]["error"] = format_error(exc)

    next_banner("web/enter 流地址解析")
    try:
        # 复用 Step 1 的 get_stream_url 结果，避免冗余请求
        if step1_stream_info:
            stream_info = step1_stream_info
            logger.info("复用 Step 1 的 web/enter 结果")
        elif selected_stream_url:
            stream_info = {"selected_url": selected_stream_url, "stream_url_obj": stream_url_obj or {}, "room": enter_room or {}}
        else:
            stream_info = get_stream_url(web_room_id, headers, cookies=cookies, need_double_stream=args.detect_dual_stream)
        if stream_info and isinstance(stream_info, dict):
            selected_stream_url = stream_info.get("selected_url")
            stream_url_obj = stream_info.get("stream_url_obj") or {}
            enter_room = stream_info.get("room") or enter_room or {}
            if isinstance(enter_room, dict):
                room_status_value = enter_room.get("status") if room_status_value is None else room_status_value
        elif enter_response is None:
            enter_lookup = request_enter_room(web_room_id, headers)
            enter_response = enter_lookup.get("response")
            enter_room = enter_lookup.get("room") or enter_room

        if isinstance(enter_room, dict) and room_status_value is None:
            room_status_value = enter_room.get("status")

        files = []
        stream_url_struct = None
        if stream_url_obj:
            stream_url_struct = parse_stream_url_full(stream_url_obj) or {}
            stream_url_struct_file = output_dir / f"stream_url_struct_{ts}.json"
            save_json(stream_url_struct_file, stream_url_struct)
            files.append(str(stream_url_struct_file))
            append_artifact(session_manifest, "stream_url_struct", stream_url_struct_file, "json")

            raw_stream_url_file = output_dir / f"stream_url_raw_{ts}.json"
            save_json(
                raw_stream_url_file,
                {
                    "type": "stream_url_raw",
                    "timestamp": ts,
                    "web_room_id": web_room_id,
                    "stream_url_obj": stream_url_obj,
                },
            )
            files.append(str(raw_stream_url_file))
            append_artifact(session_manifest, "stream_url_raw", raw_stream_url_file, "json")

        quality_options = collect_quality_options(stream_url_obj)
        steps["step3"]["success"] = enter_room is not None or selected_stream_url is not None
        steps["step3"]["files"] = files
        steps["step3"]["data"] = {
            "room_status": room_status_value,
            "status_text": status_text(room_status_value),
            "selected_url": selected_stream_url,
            "quality_options": quality_options,
            "stream_url_struct": stream_url_struct,
        }

        if not steps["step3"]["success"]:
            steps["step3"]["error"] = "web/enter 未返回可用房间信息"
        elif room_status_value != 2:
            logger.info("房间当前状态为 %s，后续跳过 FLV 下载", status_text(room_status_value))
    except Exception as exc:
        logger.exception("Step 3 失败")
        steps["step3"]["error"] = format_error(exc)

    # P0(Oracle#2) fix: Step 3 失败时，尝试从 Step 2 reflow 数据中提取流地址
    if not selected_stream_url and steps["step2"].get("success"):
        reflow_fields = (steps["step2"].get("data") or {}).get("interesting_fields") or {}
        for field_key, field_value in (reflow_fields.items() if isinstance(reflow_fields, dict) else []):
            if isinstance(field_key, str) and "stream_url" in field_key.lower():
                if isinstance(field_value, str) and field_value.startswith("http"):
                    selected_stream_url = field_value
                    logger.info("从 Step 2 reflow 数据中回退获取流地址: %s", selected_stream_url[:80])
                    break

    # 确定抓取时长：--capture-seconds 显式指定时用指定值，否则 playwright 模式用默认值
    effective_capture_seconds = args.capture_seconds
    if effective_capture_seconds <= 0 and pw_ctx and pw_ctx.get("ok") and room_status_value == 2:
        effective_capture_seconds = CAPTURE_SECONDS
        logger.info("Playwright 模式自动启用多清晰度抓取，时长 %ds", effective_capture_seconds)

    mq_dir = None
    multi_quality_samples: dict[str, Path] = {}

    next_banner("下载 FLV 样本")
    try:
        if room_status_value != 2:
            raise RuntimeError("房间未直播，跳过 FLV 下载")
        if not selected_stream_url:
            raise RuntimeError("未获取到流地址，无法下载 FLV 样本")

        if effective_capture_seconds > 0 and stream_url_obj:
            # 并发多清晰度 + 纯音频抓取
            quality_map = stream_url_obj.get("flv_pull_url") or stream_url_obj.get("flv_pull_url_params")
            if not isinstance(quality_map, dict):
                quality_map = {}

            # 从 step1_stream_info 获取 all_quality_urls
            all_q_urls = {}
            if step1_stream_info and isinstance(step1_stream_info, dict):
                all_q_urls = step1_stream_info.get("all_quality_urls") or {}
            if not all_q_urls and isinstance(quality_map, dict):
                all_q_urls = dict(quality_map)

            # 构造纯音频 URL（基于原画地址）
            base_for_audio = all_q_urls.get("FULL_HD1") or selected_stream_url
            if base_for_audio:
                all_q_urls["AUDIO_ONLY"] = build_audio_only_url(base_for_audio)

            # --extra-params: 向所有流 URL 附加启用的额外参数
            if args.extra_params:
                for q in list(all_q_urls.keys()):
                    if q != "AUDIO_ONLY":
                        all_q_urls[q] = append_extra_params(all_q_urls[q])

            if not all_q_urls:
                raise RuntimeError("未找到任何清晰度流地址")

            mq_dir = output_dir / f"multi_quality_{ts}"
            mq_dir.mkdir(parents=True, exist_ok=True)
            logger.info("开始并发抓取 %d 路流 (限时 %ds): %s", len(all_q_urls), effective_capture_seconds, list(all_q_urls.keys()))

            def _capture_one(quality: str, url: str) -> tuple[str, dict | None]:
                logger.info("抓取 %s ...", quality)
                sample = download_stream_sample_timed(url, headers, float(effective_capture_seconds))
                return quality, sample

            with ThreadPoolExecutor(max_workers=min(len(all_q_urls), 6)) as pool:
                future_map = {pool.submit(_capture_one, q, u): q for q, u in all_q_urls.items()}
                for future in as_completed(future_map):
                    quality, sample = future.result()
                    if not sample or not sample.get("data"):
                        logger.warning("清晰度 %s 下载失败", quality)
                        continue
                    path = mq_dir / f"{quality}.flv"
                    path.write_bytes(sample["data"])
                    multi_quality_samples[quality] = path
                    logger.info("清晰度 %s: %.2f MB -> %s", quality, len(sample["data"]) / 1024 / 1024, path)

            # 原画数据用于后续 ffprobe 和主分析
            first_quality = next((q for q in ["FULL_HD1", "HD1", "SD1", "SD2"] if q in multi_quality_samples), None)
            if first_quality:
                downloaded_data = multi_quality_samples[first_quality].read_bytes()
            elif multi_quality_samples:
                downloaded_data = next(iter(multi_quality_samples.values())).read_bytes()
            headers_info = {}

            steps["step4"]["success"] = True
            steps["step4"]["files"] = [str(p) for p in multi_quality_samples.values()]
            steps["step4"]["data"] = {
                "mode": "multi_quality_timed",
                "capture_seconds": effective_capture_seconds,
                "qualities_captured": list(multi_quality_samples.keys()),
                "sample_dir": str(mq_dir),
                "sizes": {q: p.stat().st_size for q, p in multi_quality_samples.items()},
            }
            logger.info("多清晰度抓取完成: %s", list(multi_quality_samples.keys()))
        else:
            # 原始单流抓取（capture_seconds=0）
            stream_sample = download_stream_sample(selected_stream_url, headers)
            if not stream_sample or not stream_sample.get("data"):
                raise RuntimeError("FLV 样本下载失败")

            raw_data = stream_sample.get("data")
            if not isinstance(raw_data, bytes):
                raise RuntimeError("FLV 样本类型异常")

            downloaded_data = raw_data
            headers_info = stream_sample.get("headers_info") or {}
            sample_file = output_dir / f"flv_sample_{ts}.flv"

            steps["step4"]["success"] = True
            steps["step4"]["files"] = [str(sample_file)]
            steps["step4"]["data"] = {
                "mode": "single_stream",
                "size": len(downloaded_data),
                "headers_info": headers_info,
                "response_headers": stream_sample.get("response_headers") or {},
                "sample_file": str(sample_file),
            }
    except Exception as exc:
        logger.exception("Step 4 失败")
        steps["step4"]["error"] = format_error(exc)

    # Step 4b: ffprobe 流探测（在线探测，不依赖 Step 4 下载的数据）
    if selected_stream_url and room_status_value == 2:
        next_banner("ffprobe 流探测")
        try:
            ffprobe_result = ffprobe_stream(
                selected_stream_url,
                output_dir=output_dir,
                ts=ts,
            )
            if ffprobe_result:
                ffprobe_files = []
                json_path = ffprobe_result.get("_json_path")
                if json_path:
                    ffprobe_files.append(json_path)
                    append_artifact(session_manifest, "ffprobe", Path(json_path), "json")

                steps["step4b"]["success"] = True
                steps["step4b"]["files"] = ffprobe_files
                steps["step4b"]["data"] = {
                    "ffprobe": ffprobe_result,
                }
            else:
                steps["step4b"]["error"] = "ffprobe 未返回结果（可能未安装或流不可达）"
        except Exception as exc:
            logger.exception("Step 4b (ffprobe) 失败")
            steps["step4b"]["error"] = format_error(exc)

    next_banner("FLV 元数据分析")
    multi_quality_analysis: dict[str, dict] = {}
    try:
        if multi_quality_samples and mq_dir:
            # 逐流分析所有清晰度
            for quality, sample_path in multi_quality_samples.items():
                sample_data = sample_path.read_bytes()
                if not sample_data:
                    logger.warning("清晰度 %s 数据为空，跳过", quality)
                    continue
                q_ts = f"{ts}_{quality}"
                stream_url_for_q = None
                if quality == "AUDIO_ONLY":
                    stream_url_for_q = build_audio_only_url(selected_stream_url or "")
                else:
                    q_urls = stream_url_obj.get("flv_pull_url") or stream_url_obj.get("flv_pull_url_params") or {}
                    stream_url_for_q = q_urls.get(quality, selected_stream_url)

                url_hints = analyze_stream_url_patterns(stream_url_for_q or "")
                analysis_result = analyze_payload(
                    data=sample_data,
                    output_dir=mq_dir,
                    json_output_dir=mq_dir,
                    ts=q_ts,
                    stream_url=stream_url_for_q or "",
                    room_id=internal_room_id or web_room_id,
                    url_hints=url_hints,
                    headers_info={},
                    stream_url_struct_file=stream_url_struct_file,
                )
                multi_quality_analysis[quality] = analysis_result
                logger.info("清晰度 %s 分析完成", quality)

            # 输出对比摘要
            logger.info("=" * 60)
            logger.info("多清晰度对比摘要:")
            for quality, ar in multi_quality_analysis.items():
                fp = ar.get("fingerprint") or {}
                mh = ar.get("metadata_hints") or {}
                res = fp.get("resolution", "N/A")
                fps = mh.get("framerate", "N/A")
                vbr = mh.get("videodatarate", "N/A")
                abr = mh.get("audiodatarate", "N/A")
                vcodec = fp.get("video_codec", "N/A")
                acodec = fp.get("audio_codec", "N/A")
                sp = multi_quality_samples.get(quality)
                size_mb = f"{sp.stat().st_size / 1024 / 1024:.2f} MB" if sp and sp.exists() else "N/A"
                logger.info("  %-12s res=%-15s fps=%-6s vbr=%-10s abr=%-10s codec=%s/%s size=%s",
                            quality, res, fps, vbr, abr, vcodec, acodec, size_mb)
            logger.info("=" * 60)

            # 用原画分析结果作为主报告
            primary = multi_quality_analysis.get("FULL_HD1") or next(iter(multi_quality_analysis.values()))
            files = []
            for key in ["raw_file", "report_file", "json_path"]:
                value = primary.get(key)
                if value is not None:
                    files.append(str(value))

            # 清理 output_dir 根目录中旧的多清晰度文件，避免历史数据污染本次报告
            import shutil as _shutil
            for _pattern in ["flv_analysis_*", "flv_report_*", "flv_sample_*", "onmetadata_*"]:
                for _old in output_dir.glob(_pattern):
                    if _old.is_file() and ts not in _old.name:
                        _old.unlink(missing_ok=True)
                        logger.debug("清理旧文件: %s", _old.name)

            # 复制所有清晰度的报告文件到 output_dir 根目录，让统一报告生成能发现
            for quality, ar in multi_quality_analysis.items():
                for key in ["report_file", "json_path"]:
                    src = ar.get(key)
                    if src and Path(src).exists():
                        dst = output_dir / Path(src).name
                        _shutil.copy2(src, dst)
                        files.append(str(dst))
            # 复制 onmetadata 文件
            primary_q = "FULL_HD1" if "FULL_HD1" in multi_quality_analysis else next(iter(multi_quality_analysis))
            for meta_src in mq_dir.glob(f"onmetadata_{ts}_{primary_q}.json"):
                dst = output_dir / f"onmetadata_{ts}.json"
                _shutil.copy2(meta_src, dst)
                files.append(str(dst))
            # 复制所有清晰度的 flv sample
            for quality, sample_path in multi_quality_samples.items():
                src_flv = mq_dir / f"flv_sample_{ts}_{quality}.flv"
                if not src_flv.exists():
                    src_flv = sample_path
                dst = output_dir / src_flv.name
                _shutil.copy2(src_flv, dst)
                files.append(str(dst))
            # 清理 multi_quality 目录中 step4 的冗余原始下载（{QUALITY}.flv）
            for quality in multi_quality_samples:
                raw = mq_dir / f"{quality}.flv"
                analyzed = mq_dir / f"flv_sample_{ts}_{quality}.flv"
                if raw.exists() and analyzed.exists():
                    raw.unlink(missing_ok=True)

            steps["step5"]["success"] = True
            steps["step5"]["files"] = files
            fingerprint = primary.get("fingerprint")
            json_path = primary.get("json_path")
            if not fingerprint and not json_path:
                logger.warning("FLV 元数据分析未产出有效指纹/JSON（可能非有效 FLV 数据）")
                steps["step5"]["success"] = True
                steps["step5"]["partial"] = True
            steps["step5"]["data"] = {
                "fingerprint": primary.get("fingerprint") or {},
                "metadata": primary.get("metadata") or {},
                "metadata_hints": primary.get("metadata_hints") or {},
                "stream_url": primary.get("stream_url"),
                "multi_quality_analysis": {q: {
                    "fingerprint": ar.get("fingerprint") or {},
                    "metadata": ar.get("metadata") or {},
                    "metadata_hints": ar.get("metadata_hints") or {},
                } for q, ar in multi_quality_analysis.items()},
            }

            # 音频轨道相似度分析
            if len(multi_quality_samples) >= 2:
                try:
                    # 使用分析后的 FLV 文件（原始 {QUALITY}.flv 可能已被清理）
                    audio_samples = {}
                    for quality in multi_quality_samples:
                        analyzed = mq_dir / f"flv_sample_{ts}_{quality}.flv"
                        audio_samples[quality] = analyzed if analyzed.exists() else multi_quality_samples[quality]
                    audio_analysis = analyze_audio_tracks(audio_samples)
                    steps["step5"]["data"]["audio_track_analysis"] = audio_analysis
                    # 保存为独立 JSON 供统一报告发现
                    audio_json = output_dir / f"audio_track_analysis_{ts}.json"
                    audio_json.write_text(json.dumps(audio_analysis, ensure_ascii=False, indent=2), encoding="utf-8")
                    steps["step5"]["files"].append(str(audio_json))
                    logger.info("音频轨道分析完成: %s → %s", audio_analysis.get("conclusion", "N/A"), audio_json.name)
                except Exception as audio_exc:
                    logger.warning("音频轨道分析失败: %s", audio_exc)
        else:
            # 原始单流分析
            if not downloaded_data:
                raise RuntimeError("没有可分析的 FLV 样本")
            if not selected_stream_url:
                raise RuntimeError("缺少流地址，无法执行元数据分析")

            url_hints = analyze_stream_url_patterns(selected_stream_url)
            analysis_result = analyze_payload(
                data=downloaded_data,
                output_dir=output_dir,
                json_output_dir=output_dir,
                ts=ts,
                stream_url=selected_stream_url,
                room_id=internal_room_id or web_room_id,
                url_hints=url_hints,
                headers_info=headers_info,
                stream_url_struct_file=stream_url_struct_file,
            )

            files = []
            for key in ["raw_file", "report_file", "json_path"]:
                value = analysis_result.get(key)
                if value is not None:
                    files.append(str(value))
            metadata_file = output_dir / f"onmetadata_{ts}.json"
            if metadata_file.exists():
                files.append(str(metadata_file))

            steps["step5"]["success"] = True
            steps["step5"]["files"] = files
            fingerprint = analysis_result.get("fingerprint")
            json_path = analysis_result.get("json_path")
            if not fingerprint and not json_path:
                logger.warning("FLV 元数据分析未产出有效指纹/JSON（可能非有效 FLV 数据）")
                steps["step5"]["success"] = True
                steps["step5"]["partial"] = True
            steps["step5"]["data"] = {
                "fingerprint": analysis_result.get("fingerprint") or {},
                "metadata": analysis_result.get("metadata") or {},
                "metadata_hints": analysis_result.get("metadata_hints") or {},
                "stream_url": analysis_result.get("stream_url"),
            }
    except Exception as exc:
        logger.exception("Step 5 失败")
        steps["step5"]["error"] = format_error(exc)

    # Step 5b: 双流检测 (可选)
    if args.detect_dual_stream and stream_url_obj:
        next_banner("双流异常检测")
        try:
            dsd_module = importlib.import_module("dual_stream_detector")
            run_dual_stream_detection = getattr(dsd_module, "run_dual_stream_detection", None)
            if not callable(run_dual_stream_detection):
                raise AttributeError("dual_stream_detector 模块未导出 run_dual_stream_detection")
            dual_qualities = [q.strip() for q in args.dual_qualities.split(",") if q.strip()]

            # 解析外部额外流 URL 文件
            extra_urls: dict[str, str] = {}
            if args.extra_stream_urls:
                try:
                    from pathlib import Path as _Path
                    url_file = _Path(args.extra_stream_urls)
                    if url_file.exists():
                        with open(url_file, "r", encoding="utf-8") as uf:
                            idx = 1
                            for line in uf:
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                if line.startswith("http"):
                                    extra_urls[f"EXTERNAL_{idx}"] = line
                                    idx += 1
                        if extra_urls:
                            logger.info("从 %s 加载了 %d 条外部流 URL", url_file, len(extra_urls))
                    else:
                        logger.warning("额外流 URL 文件不存在: %s", url_file)
                except Exception:
                    logger.debug("解析额外流 URL 文件失败", exc_info=True)

            # 双模式对比: 额外请求 need_double_stream=false 的流地址
            stream_url_obj_normal = None
            try:
                logger.info("请求 need_double_stream=false 模式用于对比")
                normal_info = get_stream_url(web_room_id, headers, cookies=cookies, need_double_stream=False)
                if normal_info:
                    stream_url_obj_normal = normal_info.get("stream_url_obj") or {}
                elif pw_ctx and pw_ctx.get("stream_url_obj"):
                    logger.info("直接 API 请求失败，使用 Playwright 鉴权数据作为 normal 模式 fallback")
                    stream_url_obj_normal = pw_ctx.get("stream_url_obj")
            except Exception:
                logger.debug("双模式对比: 获取 normal 模式流地址失败，跳过对比")

            dual_result = run_dual_stream_detection(
                web_room_id=web_room_id,
                internal_room_id=internal_room_id,
                stream_url_obj=stream_url_obj,
                headers=headers,
                cookies=cookies,
                output_dir=output_dir,
                preferred_qualities=dual_qualities,
                temporal_polls=0,
                temporal_interval=args.dual_monitor_seconds,
                stream_url_obj_normal=stream_url_obj_normal,
                extra_urls=extra_urls or None,
            )
            steps["step5b"]["success"] = True
            steps["step5b"]["data"] = dual_result
            json_path = dual_result.get("json_path")
            files = [str(json_path)] if json_path else []
            dual_artifacts = dual_result.get("artifacts") or {}
            sample_dir = dual_artifacts.get("sample_dir")
            manifest_dir = dual_artifacts.get("manifest_dir")
            if sample_dir:
                files.append(str(sample_dir))
                append_artifact(session_manifest, "dual_stream_samples", Path(sample_dir), "dir")
            if manifest_dir:
                files.append(str(manifest_dir))
                append_artifact(session_manifest, "dual_stream_manifests", Path(manifest_dir), "dir")
            steps["step5b"]["files"] = files
            logger.info("双流检测结果: verdict=%s score=%s", dual_result.get("verdict"), dual_result.get("score"))
        except Exception as exc:
            logger.exception("Step 5b (双流检测) 失败")
            steps["step5b"]["error"] = format_error(exc)
    elif args.detect_dual_stream and not stream_url_obj:
        steps["step5b"]["error"] = "缺少 stream_url_obj，无法执行双流检测"

    if args.capture_probes:
        next_banner("Capture-First 探针采集")
        try:
            probe_specs: list[tuple[str, str, dict[str, Any]]] = [
                (
                    "gift_list_probe",
                    "run_gift_list_probe",
                    {
                        "web_room_id": web_room_id,
                        "internal_room_id": internal_room_id,
                        "cookies": cookies,
                        "output_dir": output_dir,
                        "ts": ts,
                    },
                ),
                (
                    "room_info_probe",
                    "run_room_info_probe",
                    {
                        "web_room_id": web_room_id,
                        "internal_room_id": internal_room_id,
                        "cookies": cookies,
                        "output_dir": output_dir,
                        "ts": ts,
                    },
                ),
                (
                    "ranklist_probe",
                    "run_ranklist_probe",
                    {
                        "web_room_id": web_room_id,
                        "internal_room_id": internal_room_id,
                        "cookies": cookies,
                        "anchor_id": extract_anchor_id(enter_room),
                        "output_dir": output_dir,
                        "ts": ts,
                    },
                ),
            ]

            files: list[str] = []
            probe_results: dict[str, Any] = {}
            errors: list[str] = []
            success_count = 0
            for module_name, func_name, kwargs in probe_specs:
                module = importlib.import_module(module_name)
                runner = getattr(module, func_name, None)
                if not callable(runner):
                    raise AttributeError(f"{module_name} 未导出 {func_name}")

                result = runner(**kwargs)
                probe_results[module_name] = result
                probe_files = [str(path) for path in (result.get("files") or [])]
                files.extend(probe_files)
                for file_path in probe_files:
                    append_artifact(session_manifest, "probe_capture", Path(file_path), "json")

                if result.get("success"):
                    success_count += 1
                elif result.get("error"):
                    errors.append(f"{module_name}: {result.get('error')}")

            steps["step5c"]["success"] = success_count > 0
            steps["step5c"]["files"] = files
            steps["step5c"]["data"] = {
                "probe_results": probe_results,
                "success_count": success_count,
                "total": len(probe_specs),
            }
            if errors:
                steps["step5c"]["error"] = " | ".join(errors)
                if success_count > 0:
                    steps["step5c"]["partial"] = True
        except Exception as exc:
            logger.exception("Step 5c 失败")
            steps["step5c"]["error"] = format_error(exc)

    # ── Step 5d: 音画同步检测（可选，--av-sync）──
    if getattr(args, "av_sync", False):
        try:
            _mq = locals().get("multi_quality_samples") or {}
            _mq_dir = locals().get("mq_dir")
            _q = next((q for q in ["FULL_HD1", "HD1", "SD1", "SD2", "AUDIO_ONLY"] if q in _mq), None)
            _sample = None
            if _q:
                _analyzed = (_mq_dir / f"flv_sample_{ts}_{_q}.flv") if _mq_dir else None
                _sample = _analyzed if (_analyzed and _analyzed.exists()) else _mq.get(_q)
            if _sample and Path(str(_sample)).exists():
                _av = run_av_sync_analysis(_sample, output_dir, ts,
                                           room_id=internal_room_id or web_room_id,
                                           decode_check=True, decode_seconds=8)
                steps["step5d"]["success"] = True
                steps["step5d"]["data"] = _av
                _files = _av.get("output_files") or []
                steps["step5d"]["files"] = _files
                for _f in _files:
                    append_artifact(session_manifest, "av_sync", Path(_f), "json")
                logger.info("音画同步检测完成: %s（流内偏移 %s ms）", _av.get("verdict"),
                            (_av.get("perceptual") or {}).get("stream_offset_ms"))
            else:
                steps["step5d"]["error"] = "无可用 FLV 样本（房间未开播或缺采样）"
        except Exception as exc:
            logger.warning("音画同步检测失败: %s", exc)
            steps["step5d"]["error"] = format_error(exc)

    # ── Step 5e: 人脸嘴型音画同步（内容级，可选）──
    if getattr(args, "lip_sync", True):
        try:
            from lip_sync_detector import run_lip_sync_analysis
            _mq = locals().get("multi_quality_samples") or {}
            _mq_dir = locals().get("mq_dir")
            _q = next((q for q in ["FULL_HD1", "HD1", "SD1", "SD2", "AUDIO_ONLY"] if q in _mq), None)
            _sample = None
            if _q:
                _analyzed = (_mq_dir / f"flv_sample_{ts}_{_q}.flv") if _mq_dir else None
                _sample = _analyzed if (_analyzed and _analyzed.exists()) else _mq.get(_q)
            if _sample and Path(str(_sample)).exists():
                _lip = run_lip_sync_analysis(_sample, output_dir, ts,
                                             room_id=internal_room_id or web_room_id,
                                             seconds=getattr(args, "lip_sync_seconds", 20))
                steps["step5e"]["success"] = True
                steps["step5e"]["data"] = _lip
                steps["step5e"]["files"] = _lip.get("output_files") or []
                for _f in steps["step5e"]["files"]:
                    append_artifact(session_manifest, "lip_sync", Path(_f), "json")
                logger.info("嘴型同步检测完成: %s（内容级偏移 %s ms）", _lip.get("verdict"),
                            (_lip.get("perceptual") or {}).get("content_offset_ms"))
            else:
                steps["step5e"]["error"] = "无可用 FLV 样本（房间未开播或缺采样）"
        except Exception as exc:
            logger.warning("嘴型同步检测失败: %s", exc)
            steps["step5e"]["error"] = format_error(exc)

    next_banner("生成统一报告")
    try:
        # 清理 captures 根目录中不属于本次运行的文件，避免历史数据污染报告
        import shutil as _shutil
        for _old in output_dir.iterdir():
            if _old.is_file() and ts not in _old.name:
                # 只清理本次房间相关的采集产物（reflow、stream_url、ffprobe 等）
                _ext = _old.suffix.lower()
                _stem = _old.stem
                if _ext in {".json", ".txt", ".flv"} and any(
                    kw in _stem for kw in ["reflow", "stream_url", "ffprobe", "flv_analysis", "flv_report", "flv_sample", "onmetadata", "audio_track", "av_sync", "session_manifest"]
                ):
                    _old.unlink(missing_ok=True)
                    logger.debug("清理旧文件: %s", _old.name)

        json_results = discover_json_results(output_dir)
        files_data = merge_results_into_files_data(scan_captures(output_dir), json_results)
        sessions = correlate_by_time(files_data, tolerance_seconds=300)
        sessions = enrich_sessions(sessions)

        filtered_sessions: list[dict[str, Any]] = []
        if internal_room_id:
            filtered_sessions.extend(filter_sessions_by_room_id(sessions, internal_room_id))
        filtered_sessions.extend(filter_sessions_by_room_id(sessions, web_room_id))
        filtered_sessions = dedupe_sessions_by_id(filtered_sessions)
        # 只保留最新 session（当前运行），避免报告出现多个 session
        if filtered_sessions:
            filtered_sessions = [max(filtered_sessions, key=lambda s: s.get("start_time"))]
        elif sessions:
            logger.warning(
                "未找到房间 %s/%s 的匹配 session，共 %d 个历史 session 被忽略",
                web_room_id, internal_room_id, len(sessions),
            )

        report_folder = report_dir / f"unified_report_{ts}"
        report_folder.mkdir(parents=True, exist_ok=True)
        text_path = report_folder / "report.txt"
        html_path = report_folder / "index.html"
        files = []
        if args.format in {"text", "both"}:
            generate_text_report(filtered_sessions, text_path)
            files.append(str(text_path))
        if args.format in {"html", "both"}:
            generate_html_report(filtered_sessions, html_path)
            files.append(str(html_path))

        # 拷贝 captures 目录下本次相关的采集文件到报告文件夹
        import shutil as _shutil
        evidence_dir = report_folder / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        for item in output_dir.iterdir():
            if item.is_file() and ts in item.name:
                _shutil.copy2(item, evidence_dir / item.name)
        # 注入证据相对路径，让 HTML 报告中的关联文件可点击
        for session in filtered_sessions:
            for key in ["reflow_files", "flv_samples", "flv_reports", "rtmp_pcaps", "rtmp_reports"]:
                for item in session.get(key, []):
                    _src_name = Path(str(item.get("path", ""))).name
                    if _src_name and (evidence_dir / _src_name).exists():
                        item["evidence_name"] = f"evidence/{_src_name}"
            for data_list_key in ["reflow_data", "flv_data", "rtmp_data", "dual_data"]:
                for data_item in session.get(data_list_key, []):
                    _src_name = Path(str(data_item.get("path", ""))).name
                    if _src_name and (evidence_dir / _src_name).exists():
                        data_item["evidence_name"] = f"evidence/{_src_name}"

        # 内嵌播放器依赖（flv.js / hls.js）随报告分发
        _assets_src = Path(__file__).resolve().parent / "report_assets"
        if _assets_src.exists():
            _assets_dir = report_folder / "assets"
            _assets_dir.mkdir(exist_ok=True)
            for _lib in ("flv.min.js", "hls.min.js"):
                if (_assets_src / _lib).exists():
                    _shutil.copy2(_assets_src / _lib, _assets_dir / _lib)

        steps["step6"]["success"] = True
        steps["step6"]["files"] = files
        steps["step6"]["data"] = {
            "sessions": filtered_sessions,
            "text_path": str(text_path) if args.format in {"text", "both"} else None,
            "html_path": str(html_path) if args.format in {"html", "both"} else None,
        }
    except Exception as exc:
        logger.exception("Step 6 失败")
        steps["step6"]["error"] = format_error(exc)

    session_manifest["steps"] = {
        step_name: {
            "success": bool(step_result.get("success")),
            "files": list(step_result.get("files") or []),
            "error": step_result.get("error"),
            "partial": bool(step_result.get("partial")),
        }
        for step_name, step_result in steps.items()
    }
    manifest_path = output_dir / f"session_manifest_{ts}.json"
    save_manifest(manifest_path, session_manifest)

    print_final_summary(web_room_id, internal_room_id, room_status_value, steps, args.format)

    # P2-2 fix: 退出码区分 (0=全部成功, 1=部分失败, 2=严重失败)
    success_count = sum(1 for s in steps.values() if s.get("success"))
    total_steps = len(steps)
    if success_count == total_steps:
        sys.exit(0)
    if success_count >= 3:
        sys.exit(1)
    sys.exit(2)


if __name__ == "__main__":
    main()
