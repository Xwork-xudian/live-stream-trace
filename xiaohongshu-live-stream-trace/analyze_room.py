#!/usr/bin/env python3
"""小红书直播流分析 — 一键编排器

流程：
  Step1 解析输入（分享短链 / 主播主页 / 直播间号 / deeplink）
  Step2 抓取直播间 SSR 状态（window.__INITIAL_STATE__）→ 直播信息
  Step3 构造并探测拉流地址（源流 FLV / HLS，含存活探测与备选域名回退）
  Step4 下载 FLV 样本（源流，未转码）
  Step5 FLV 元数据解析（推流端指纹 / 编码参数 / CDN）
  Step5d 音画同步检测（--av-sync，标签级 + 解码级）
  Step6 统一报告（HTML/文本，含内嵌播放器与拉流地址面板）

用法:
  python analyze_room.py --room 5f8a... --av-sync --format both
  python analyze_room.py --room http://xhslink.com/xxxx
  python analyze_room.py --room 5f8a... --poll-retries 10 --poll-interval 60   # 蹲守开播
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from capture_manifest import append_artifact, save_manifest
from config import COMMON_HEADERS, DOWNLOAD_MAX_BYTES, OUTPUT_DIR, REPORT_DIR, ensure_dirs
from flv_analyzer import analyze_payload, analyze_stream_url_patterns, download_stream_sample
from unified_report import (
    correlate_by_time,
    discover_json_results,
    enrich_sessions,
    filter_sessions_by_room_id,
    generate_html_report,
    generate_text_report,
    merge_results_into_files_data,
    open_report_file,
    scan_captures,
)
from xhs_probe import (flv_stream_duration, fetch_account_info, infer_push_channel,
                       infer_push_server, probe_stream_tiers)
from xhs_room import resolve_room

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))

STATUS_TEXT = {True: "直播中", False: "未开播/已结束"}


def make_step_result() -> dict:
    return {"success": False, "files": [], "data": None, "error": None, "partial": False}


def next_banner(title: str) -> None:
    print()
    print("════════ " + title + " ════════")


def format_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _save_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def run_analysis(args: argparse.Namespace) -> int:
    ensure_dirs()
    ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    report_dir = Path(args.report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    steps = {f"step{i}": make_step_result() for i in range(1, 7)}
    steps["step5d"] = make_step_result()
    steps["step5e"] = make_step_result()
    session_manifest: dict = {
        "type": "session_manifest", "platform": "xiaohongshu",
        "input": args.room, "timestamp": ts, "artifacts": [], "steps": {},
    }

    # ── Step 1: 解析直播间 ──
    next_banner("Step 1/6 解析直播间")
    room_result: dict = {}
    try:
        room_result = resolve_room(args.room, cookies=args.cookies, use_playwright=args.playwright)
        tries = 0
        while (not room_result.get("live")) and tries < max(args.poll_retries, 0):
            tries += 1
            logger.info("未检测到直播，%s 秒后重试（%s/%s）…", args.poll_interval, tries, args.poll_retries)
            time.sleep(args.poll_interval)
            room_result = resolve_room(args.room, cookies=args.cookies, use_playwright=args.playwright)
        if not room_result.get("ok"):
            raise RuntimeError(room_result.get("error") or "无法解析直播间（可能被风控，试加 --playwright）")
        room_id = str(room_result.get("room_id") or "")
        steps["step1"]["success"] = True
        steps["step1"]["data"] = room_result
        logger.info("输入类型=%s | 房间=%s | 主播=%s | 状态=%s", room_result.get("input_kind"),
                    room_id or "未知", room_result.get("anchor_name") or "未知",
                    "直播中" if room_result.get("live") else "未开播")
    except Exception as exc:
        steps["step1"]["error"] = format_error(exc)
        logger.exception("Step 1 失败")
        print(f"\n❌ 解析直播间失败: {steps['step1']['error']}")
        return 2

    room_key = room_id or "unknown"

    # ── Step 2: 直播间信息（SSR）──
    next_banner("Step 2/6 直播间 SSR 状态")
    try:
        payload = {
            "type": "xhs_live_room", "platform": "xiaohongshu", "timestamp": ts,
            "room_id": room_key, "web_room_id": room_key,
            "page_url": room_result.get("page_url"),
            "input": args.room, "input_kind": room_result.get("input_kind"),
            "source": room_result.get("source"),
            "live": bool(room_result.get("live")),
            "live_status_raw": room_result.get("live_status_raw"),
            "error_message": room_result.get("error_message"),
            "anchor_name": room_result.get("anchor_name"),
            "title": room_result.get("title"),
            "is_replay": room_result.get("is_replay"),
            "deeplink": room_result.get("deeplink"),
            "deeplink_params": room_result.get("deeplink_params") or {},
            "raw_room_info": room_result.get("raw_room_info"),
            "host_id": room_result.get("host_id"),
            "host_avatar": room_result.get("host_avatar"),
            "viewer_count": room_result.get("viewer_count"),
            "member_count": room_result.get("member_count"),
            "praise_count": room_result.get("praise_count"),
            "room_status": room_result.get("room_status"),
        }
        # 说明：旧的 live-room user_card 接口已废弃（返回“系统内部错误”），
        # 粉丝/关注/小红书号改由主播主页 SSR 获取（见下方 account_profile）。

        # 主播资料：小红书号 / 粉丝 / 关注 / 获赞与收藏（需登录 Cookie，走主页 SSR）
        try:
            from xhs_probe import fetch_account_profile, fetch_with_sign
            _prof = fetch_account_profile(room_result.get("host_id") or "", cookies=args.cookies)
            payload["account_profile"] = _prof
            if _prof.get("ok"):
                _fans = (_prof.get("fans") or {}).get("count")
                _fol = (_prof.get("follows") or {}).get("count")
                print(f"✅ 主播资料: 小红书号={_prof.get('red_id')} 粉丝={_fans} 关注={_fol}"
                      f" 获赞与收藏={(_prof.get('likes_collects') or {}).get('count')}")
                if _prof.get("desc"):
                    print(f"   简介: {str(_prof['desc'])[:60]} | IP属地: {_prof.get('ip_location') or '—'}")
            elif _prof.get("need_login"):
                print("ℹ️ 粉丝/关注/小红书号：需 --cookies（登录后主页 SSR 直出这些字段）")
            else:
                print(f"ℹ️ 主播资料不可用：{_prof.get('error')}")
            # 房间信息接口（Cookie 可用时含累计观看 pv）
            if args.cookies:
                _ri = fetch_with_sign(
                    f"/api/sns/red/live/web/v1/room/current_room_info?room_id={room_key}&request_user_id=",
                    cookies=args.cookies)
                if isinstance(_ri, dict) and _ri.get("success"):
                    payload["room_info_api"] = _ri.get("data")
                    _pv = ((_ri.get("data") or {}).get("room_info") or {}).get("pv")
                    if _pv is not None:
                        print(f"   累计观看（pv）: {_pv}")
        except Exception as _exc2:
            payload["account_profile_error"] = format_error(_exc2)
        room_json = _save_json(output_dir / f"xhs_room_{room_key}_{ts}.json", payload)
        steps["step2"]["success"] = True
        steps["step2"]["files"] = [str(room_json)]
        steps["step2"]["data"] = payload
        append_artifact(session_manifest, "xhs_room", room_json, "json")
        print(f"✅ SSR 状态: 获取成功（来源 {payload['source']}）")
        print(f"   直播状态: {'直播中' if payload['live'] else '未开播/回放'}（liveStatus={payload.get('live_status_raw')}）")
        if payload.get("title"):
            print(f"   标题: {payload['title']}")
        if payload.get("anchor_name"):
            print(f"   主播: {payload['anchor_name']}")
    except Exception as exc:
        steps["step2"]["error"] = format_error(exc)
        logger.exception("Step 2 失败")

    # ── Step 3: 拉流地址 + 探活 ──
    next_banner("Step 3/6 拉流地址探测")
    stream_payload: dict = {}
    try:
        urls = room_result.get("stream_urls") or {}
        if not urls:
            raise RuntimeError("未拿到拉流地址（房间未开播？）")
        probes = room_result.get("stream_probe") or {}
        stream_payload = {
            "type": "xhs_stream_info", "platform": "xiaohongshu", "timestamp": ts,
            "room_id": room_key, "web_room_id": room_key,
            "stream_urls": urls, "stream_probe": probes,
            "stream_alive": bool(room_result.get("stream_alive")),
            "host_fallback": room_result.get("host_fallback"),
            "pull_config": room_result.get("pull_config") or {},
            "deeplink_params": room_result.get("deeplink_params") or {},
            "note": ("源流地址由 room_id 拼装（live-source-play.xhscdn.com 即未转码源流）；"
                     "HLS 为同 ID 的 m3u8。页面 SSR 的 deeplink.flvUrl 可用于交叉验证。"),
        }
        # （延后保存：等档位/时长探测完成后一并落盘）
        # 档位探测（原画 / 原画HEVC / 标清）+ 直播时长 + 开播渠道
        if not args.no_tier_probe:
            print("   探测档位（原画/H.265原画/标清）…")
            tiers = probe_stream_tiers(room_key, headers=dict(COMMON_HEADERS),
                                       sample_bytes=args.tier_probe_bytes)
            stream_payload["tiers"] = tiers
            for t in tiers:
                mark = "✅" if t.get("ok") else "❌"
                detail = (f"{t.get('codec')} {t.get('width')}x{t.get('height')}"
                          if t.get("codec") else str(t.get("status")))
                print(f"   {mark} {t['label']:16s} {detail}")
            _first_ok = next((t for t in tiers if t.get("ok")), None)
            if _first_ok:
                _meta = _first_ok.get("meta") or {}
                stream_payload["push_channel"] = _first_ok.get("push_channel") or infer_push_channel(_meta)
                stream_payload["push_server"] = _first_ok.get("push_server") or infer_push_server(_meta)
                from xhs_probe import format_client_version
                stream_payload["client_version"] = format_client_version(_meta)
                stream_payload["is_hard_encoder"] = _meta.get("isHardEncoder")
                stream_payload["source_meta"] = _meta
                print(f"   开播渠道: {stream_payload['push_channel']} | 推流服务器: {stream_payload['push_server']}")
        # 直播时长（FLV tag 时间戳为流内绝对位置）
        try:
            dur = flv_stream_duration(source_url if False else (stream_payload.get("stream_urls") or {}).get("source_flv", ""),
                                      headers=dict(COMMON_HEADERS))
            stream_payload["duration"] = dur
            if dur.get("ok"):
                stream_payload["elapsed_seconds"] = round(dur["elapsed_ms"] / 1000)
                print(f"   已播时长: {dur['elapsed_ms']/1000/60:.0f} 分钟（开播时间 ≈ 现在 − {dur['elapsed_ms']/1000/60:.0f} 分钟）")
        except Exception as _exc:
            stream_payload["duration_error"] = format_error(_exc)

        # RTMP 源流（安卓抓包实测：rtmp://live-play.xhscdn.com/live/{room_id}）
        try:
            from xhs_probe import probe_rtmp_sources
            rt_list = probe_rtmp_sources(room_key)
            stream_payload["rtmp_sources"] = rt_list
            stream_payload["rtmp_source"] = next((r for r in rt_list if r.get("ok")), rt_list[0] if rt_list else {})
            for _r in rt_list:
                _mark = "✅" if _r.get("ok") else "❌"
                _info = (f"{_r.get('codec')} {_r.get('width')}x{_r.get('height')}"
                         if _r.get("ok") else (_r.get("error") or ""))
                print(f"   {_mark} RTMP 源流 {_r['url']} {_info}")
        except Exception as _exc:
            stream_payload["rtmp_error"] = format_error(_exc)

        # 落盘（含档位/时长/开播渠道）
        stream_json = _save_json(output_dir / f"xhs_stream_{ts}.json", stream_payload)
        steps["step3"]["success"] = bool(stream_payload["stream_alive"])
        steps["step3"]["files"] = [str(stream_json)]
        steps["step3"]["data"] = stream_payload
        append_artifact(session_manifest, "xhs_stream", stream_json, "json")

        for name, probe in probes.items():
            print(f"{'✅' if probe.get('ok') else '❌'} {name}: HTTP {probe.get('status', '?')}"
                  f" | {probe.get('content_type', '')} | 魔术字节 {probe.get('magic', '')}")
        print(f"   源流地址: {urls.get('source_flv')}")
        if room_result.get("host_fallback"):
            print(f"   （已回退到备选域名 {room_result['host_fallback']}）")
    except Exception as exc:
        steps["step3"]["error"] = format_error(exc)
        logger.exception("Step 3 失败")
        print(f"❌ 拉流地址不可用: {steps['step3']['error']}")
        return 2

    # ── Step 4: 下载 FLV 样本 ──
    next_banner("Step 4/6 下载 FLV 样本（源流）")
    source_url = (stream_payload.get("stream_urls") or {}).get("source_flv", "")
    sampled: dict = {}
    try:
        if not source_url:
            raise RuntimeError("无源流地址")
        result = download_stream_sample(source_url, headers=dict(COMMON_HEADERS),
                                       max_bytes=args.max_bytes)
        data = result.get("data") or b""
        if not data:
            raise RuntimeError("下载为空（房间可能刚刚下播）")
        sample_path = output_dir / f"flv_sample_{ts}_SOURCE.flv"
        sample_path.write_bytes(data)
        sampled = {"data": data, "path": sample_path, "headers_info": result.get("headers_info") or {},
                   "response_headers": result.get("response_headers") or {}}
        steps["step4"]["success"] = True
        steps["step4"]["files"] = [str(sample_path)]
        steps["step4"]["data"] = {"bytes": len(data), "headers_info": sampled["headers_info"]}
        append_artifact(session_manifest, "flv_sample", sample_path, "flv")
        print(f"✅ 样本已下载: {sample_path.name}（{len(data)} 字节）")
        for key in ("Content-Type", "Server", "Via", "X-Cache"):
            if (sampled["response_headers"] or {}).get(key):
                print(f"   {key}: {sampled['response_headers'][key]}")
    except Exception as exc:
        steps["step4"]["error"] = format_error(exc)
        logger.exception("Step 4 失败")
        print(f"❌ 下载样本失败: {steps['step4']['error']}")

    # ── Step 5: FLV 元数据解析 ──
    next_banner("Step 5/6 FLV 元数据解析")
    try:
        if not sampled.get("data"):
            raise RuntimeError("没有可分析的 FLV 样本")
        analysis = analyze_payload(
            data=sampled["data"],
            output_dir=output_dir,
            json_output_dir=output_dir,
            ts=ts,
            stream_url=source_url,
            room_id=room_key,
            url_hints=analyze_stream_url_patterns(source_url),
            headers_info=sampled.get("headers_info") or {},
        )
        steps["step5"]["success"] = True
        steps["step5"]["data"] = analysis
        files = [p for p in (analysis.get("report_file"), analysis.get("json_path")) if p]
        steps["step5"]["files"] = [str(p) for p in files]
        for artifact_path in files:
            append_artifact(session_manifest, "flv_analysis", Path(artifact_path), "file")
        fp = analysis.get("fingerprint") or {}
        print(f"✅ 元数据解析完成: 编码器={fp.get('push_software') or (analysis.get('metadata_hints') or {}).get('encoder') or '未知'}")
    except Exception as exc:
        steps["step5"]["error"] = format_error(exc)
        logger.exception("Step 5 失败")
        print(f"❌ FLV 解析失败: {steps['step5']['error']}")

    # ── Step 5d: 音画同步检测 ──
    if getattr(args, "av_sync", False):
        next_banner("Step 5d/6 音画同步检测")
        try:
            from av_sync_detector import run_av_sync_analysis
            sample_file = sampled.get("path")
            if not (sample_file and Path(sample_file).exists()):
                raise RuntimeError("无可用样本")
            av = run_av_sync_analysis(sample_file, output_dir, ts, room_id=room_key,
                                      decode_check=True, decode_seconds=args.av_sync_seconds)
            steps["step5d"]["success"] = True
            steps["step5d"]["data"] = av
            steps["step5d"]["files"] = av.get("output_files") or []
            for f in steps["step5d"]["files"]:
                append_artifact(session_manifest, "av_sync", Path(f), "json")
            perc = av.get("perceptual") or {}
            print(f"✅ 感知判定: {av.get('verdict')}")
            print(f"   流内偏移: {perc.get('stream_offset_ms')} ms（{perc.get('direction')}）"
                  f" | 播放器残差: {perc.get('player_residual_ms')} ms")
        except Exception as exc:
            steps["step5d"]["error"] = format_error(exc)
            logger.warning("音画同步检测失败: %s", exc)

    # ── Step 5e: 人脸嘴型音画同步（内容级）──
    if getattr(args, "lip_sync", True):
        next_banner("Step 5e 人脸嘴型音画同步（内容级）")
        try:
            from lip_sync_detector import run_lip_sync_analysis
            sample_file = sampled.get("path")
            if not (sample_file and Path(sample_file).exists()):
                raise RuntimeError("无可用样本")
            lip = run_lip_sync_analysis(sample_file, output_dir, ts, room_id=room_key,
                                        seconds=args.lip_sync_seconds)
            steps["step5e"]["success"] = True
            steps["step5e"]["data"] = lip
            steps["step5e"]["files"] = lip.get("output_files") or []
            for f in steps["step5e"]["files"]:
                append_artifact(session_manifest, "lip_sync", Path(f), "json")
            perc = lip.get("perceptual") or {}
            print(f"✅ 判定: {lip.get('verdict')}（置信度 {perc.get('confidence')}）")
            print(f"   内容级偏移: {perc.get('content_offset_ms')} ms（{perc.get('direction')}）")
        except Exception as exc:
            steps["step5e"]["error"] = format_error(exc)
            logger.warning("嘴型同步检测失败: %s", exc)

    # ── Step 6: 统一报告 ──
    next_banner("Step 6/6 生成统一报告")
    try:
        json_results = discover_json_results(output_dir)
        files_data = merge_results_into_files_data(scan_captures(output_dir), json_results)
        sessions = correlate_by_time(files_data, tolerance_seconds=300)
        sessions = enrich_sessions(sessions)
        filtered = filter_sessions_by_room_id(sessions, room_key) if room_key != "unknown" else sessions
        if filtered:
            filtered = [max(filtered, key=lambda s: s.get("start_time"))]

        report_folder = report_dir / f"unified_report_{ts}"
        report_folder.mkdir(parents=True, exist_ok=True)
        text_path = report_folder / "report.txt"
        html_path = report_folder / "index.html"
        if args.format in {"text", "both"}:
            generate_text_report(filtered, text_path)
        if args.format in {"html", "both"}:
            generate_html_report(filtered, html_path)

        # 证据文件 + 播放器依赖
        evidence_dir = report_folder / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        for item in output_dir.iterdir():
            if item.is_file() and ts in item.name:
                shutil.copy2(item, evidence_dir / item.name)
        assets_src = Path(__file__).resolve().parent / "report_assets"
        if assets_src.exists():
            assets_dir = report_folder / "assets"
            assets_dir.mkdir(exist_ok=True)
            for lib in ("flv.min.js", "hls.min.js"):
                if (assets_src / lib).exists():
                    shutil.copy2(assets_src / lib, assets_dir / lib)

        steps["step6"]["success"] = True
        report_files: list[str] = []
        if args.format in {"text", "both"}:
            report_files.append(str(text_path))
        if args.format in {"html", "both"}:
            report_files.append(str(html_path))
        steps["step6"]["files"] = report_files
        print("✅ 统一报告: 成功")
    except Exception as exc:
        steps["step6"]["error"] = format_error(exc)
        logger.exception("Step 6 失败")
        print(f"❌ 报告生成失败: {steps['step6']['error']}")

    session_manifest["steps"] = {
        k: {"success": v["success"], "files": v["files"], "error": v["error"], "partial": v["partial"]}
        for k, v in steps.items()
    }
    save_manifest(output_dir / f"session_manifest_{ts}.json", session_manifest)

    # ── 汇总输出 ──
    print()
    print("── 汇总 ──")
    for name, label in (("step1", "解析直播间"), ("step2", "SSR 状态"), ("step3", "拉流地址"),
                        ("step4", "样本下载"), ("step5", "FLV 解析"), ("step5d", "音画同步"),
                        ("step5e", "嘴型同步"), ("step6", "统一报告")):
        s = steps.get(name) or {}
        if name == "step5d" and not getattr(args, "av_sync", False):
            continue
        if name == "step5e" and not getattr(args, "lip_sync", True):
            continue
        mark = "✅" if s.get("success") else ("⚠️" if s.get("error") else "⏭")
        print(f"  {mark} {label}: {s.get('error') or ('成功' if s.get('success') else '未执行')}")
    if args.format in {"html", "both"} and steps["step6"].get("success"):
        print(f"\nHTML 报告: {report_folder / 'index.html'}")
        open_report_file(report_folder / "index.html")
    if args.format in {"text", "both"} and steps["step6"].get("success"):
        print(f"文本报告: {report_folder / 'report.txt'}")
    return 0 if steps["step1"].get("success") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小红书直播流分析 — 一键编排器")
    parser.add_argument("room", nargs="?", help="直播间号 / 短链 / 长链接 / 主播主页（位置参数）")
    parser.add_argument("--room", "-r", dest="room_opt",
                        help="同上（也可用 --room 传）")
    parser.add_argument("--no-tier-probe", action="store_true", help="跳过档位探测（加快速度）")
    parser.add_argument("--tier-probe-bytes", type=int, default=1_200_000,
                        help="档位验证采样字节数（默认 1.2MB）")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="采集输出目录")
    parser.add_argument("--report-dir", default=str(REPORT_DIR), help="报告输出目录")
    parser.add_argument("--format", choices=["html", "text", "both"], default="both", help="报告格式")
    parser.add_argument("--max-bytes", type=int, default=DOWNLOAD_MAX_BYTES, help="FLV 样本最大下载字节")
    parser.add_argument("--playwright", action="store_true", help="SSR 缺失时用无头 Chromium 兜底抓取")
    parser.add_argument("--cookies", help="Cookie 字符串（默认读取 .xhs_cookie，可用此参数覆盖）")
    parser.add_argument("--av-sync", dest="av_sync", action="store_true", default=True,
                        help="音画同步检测（默认开启；标签级 + 解码级）")
    parser.add_argument("--no-av-sync", dest="av_sync", action="store_false", help="关闭音画同步检测")
    parser.add_argument("--av-sync-seconds", type=int, default=8, help="解码级验证时长（秒）")
    parser.add_argument("--no-lip-sync", dest="lip_sync", action="store_false", default=True,
                        help="关闭人脸嘴型（内容级）音画同步检测（默认开启）")
    parser.add_argument("--lip-sync-seconds", type=int, default=20, help="嘴型检测分析时长（秒）")
    parser.add_argument("--poll-retries", type=int, default=0, help="未开播时的重试次数（蹲守开播）")
    parser.add_argument("--poll-interval", type=int, default=60, help="重试间隔（秒）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.room = args.room or args.room_opt
    # 默认使用持久化 Cookie（.xhs_cookie）
    from config import DEFAULT_COOKIE
    if not args.cookies and DEFAULT_COOKIE:
        args.cookies = DEFAULT_COOKIE
        _c = DEFAULT_COOKIE
        _masked = f"{_c[:24]}…（{len(_c)} 字符，已从 .xhs_cookie 加载）"
        print(f"[*] 使用默认 Cookie: {_masked}")
    if not args.room:
        build_parser().error("请提供直播间：位置参数或 --room（支持房间号/短链/长链接/主页）")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run_analysis(args)


if __name__ == "__main__":
    sys.exit(main())
