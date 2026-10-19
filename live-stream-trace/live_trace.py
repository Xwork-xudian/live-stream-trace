#!/usr/bin/env python3
"""live-stream-trace 统一入口 —— 多平台直播流分析。

用法：
    python live_trace.py <直播间链接|房间号>              # 自动识别平台
    python live_trace.py <链接> --platform xiaohongshu     # 指定平台
    python live_trace.py --list-platforms                  # 查看已接入平台
    python live_trace.py <链接> --no-av-sync --format html  # 关闭音画同步 / 只出 HTML

流水线（平台无关）：
    解析直播间 → 探活档位（FLV/HLS/RTMP）→ 下载样本 → FLV 元数据解析
    → 音画同步（默认开启）→ 归一化 payload → 统一报告（含内嵌播放器）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "platforms"))

from config import (DOWNLOAD_MAX_BYTES, OUTPUT_DIR, REPORT_DIR, REPORT_ASSETS_DIR,  # noqa: E402
                    detect_platform, ensure_dirs, load_cookie, platform_registry)
from capture_manifest import append_artifact, save_manifest  # noqa: E402
from base import format_client_version, infer_push_channel, infer_push_server, http_probe  # noqa: E402

logger = logging.getLogger(__name__)


def _load_core():
    """延迟导入 core 模块（避免与平台同名模块冲突）。"""
    import flv_analyzer
    import report as core_report
    return flv_analyzer, core_report


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="多平台直播流分析（抖音/小红书/哔哩哔哩…）")
    ap.add_argument("target", nargs="?", help="直播间链接或房间号")
    ap.add_argument("--platform", "-p", default="auto",
                    help="平台（auto 自动识别）；可选值见 --list-platforms")
    ap.add_argument("--format", choices=["html", "text", "both"], default="both")
    ap.add_argument("--max-bytes", type=int, default=DOWNLOAD_MAX_BYTES, help="FLV 样本最大下载字节")
    ap.add_argument("--capture-seconds", type=int, default=20,
                    help="非 HTTP 档位（RTMP）录制时长秒数（默认 20）")
    ap.add_argument("--no-av-sync", dest="av_sync", action="store_false", default=True,
                    help="关闭音画同步检测（默认开启）")
    ap.add_argument("--av-sync-seconds", type=int, default=8, help="解码级验证秒数")
    ap.add_argument("--no-lip-sync", dest="lip_sync", action="store_false", default=True,
                    help="关闭人脸嘴型（内容级）音画同步检测（默认开启）")
    ap.add_argument("--lip-sync-seconds", type=int, default=20, help="嘴型检测分析时长（秒）")
    ap.add_argument("--no-segment-scan", dest="segment_scan", action="store_false", default=True,
                    help="关闭「健康片段自动选段」（默认开启）")
    ap.add_argument("--segment-seconds", type=float, default=40.0,
                    help="为选段持续捕获的时长（秒，默认 40；不足会按码率补采）")
    ap.add_argument("--segment-window", type=float, default=6.0, help="候选片段窗口长度（秒，默认 6）")
    ap.add_argument("--measure-timeout", type=float, default=300.0,
                    help="内容级测不出来时「持续测量」的总预算（秒，默认 300=5 分钟；"
                         "0=只测一次，不做续采重测）")
    ap.add_argument("--no-specificity", dest="skip_specificity", action="store_true",
                    help="跳过「嘴型特异性」门禁（已知干净场景；报告会标注）")
    ap.add_argument("--adaptive-attempts", type=int, default=2,
                    help="内容级测不出时自动续采的最大次数（0=关闭；每轮采 adaptive-chunk 秒）")
    ap.add_argument("--adaptive-seconds", type=float, default=120.0,
                    help="采样端规避的总时间预算（秒）")
    ap.add_argument("--adaptive-chunk", type=float, default=20.0,
                    help="采样端规避每轮采集的时长（秒）")
    ap.add_argument("--lip-min-seconds", type=float, default=8.0,
                    help="嘴型分析所需的最短样本时长（不足会自动补采；默认 8s）")
    ap.add_argument("--max-bytes-cap", type=int, default=24 * 1024 * 1024,
                    help="补采样本的字节上限（默认 24MB）")
    ap.add_argument("--no-tier-probe", action="store_true", help="跳过档位探活（更快）")
    ap.add_argument("--tier-probe-bytes", type=int, default=1_200_000, help="档位验证采样字节")
    ap.add_argument("--playwright", dest="playwright", action="store_true", default=True,
                    help="需要浏览器时使用无头 Chromium（默认开）")
    ap.add_argument("--no-playwright", dest="playwright", action="store_false")
    ap.add_argument("--cookies", help="Cookie 字符串（默认读取 cookies/<platform>.cookie）")
    ap.add_argument("--poll-retries", type=int, default=0, help="未开播时重试次数")
    ap.add_argument("--poll-interval", type=int, default=60, help="重试间隔（秒）")
    ap.add_argument("--list-platforms", action="store_true", help="列出已接入平台")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap


def _media_span_seconds(path) -> float:
    """媒体时长（秒）；失败返回 0。"""
    try:
        import lip_sync_detector
        return float(lip_sync_detector.probe_media(path).get("duration") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _content_verdict_ok(verdict) -> bool:
    """内容级是否给出「确定结论」（与 lip_sync_detector 口径一致）。"""
    v = str(verdict or "")
    return bool(v) and not v.startswith(("无法确定", "无法评估"))


def _segment_rank(seg) -> float:
    """一轮「健康片段」结果的质量分：0=完全没测出；≥1000=测出了确定结论。"""
    best = (seg or {}).get("best") or {}
    if not best:
        return 0.0
    bw = best.get("best_window") or {}
    try:
        score = float(bw.get("score_effective") or bw.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    meas = (seg or {}).get("measurement") or {}
    return (1000.0 if _content_verdict_ok(meas.get("verdict")) else 0.0) + score


def _estimate_bps(sample) -> float:
    """估算码率（bit/s），用于续采时按秒数换算字节预算。"""
    try:
        path = Path(sample)
        span = _media_span_seconds(path)
        size = path.stat().st_size
        return (size * 8 / span) if (span > 0.5 and size) else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _refresh_tier_url(adapter, tier, room) -> None:
    """续采前刷新档位地址（B 站等短时签名地址会过期）；拿不到就沿用旧地址。"""
    try:
        fresh = adapter.refresh_tier(str(room.room_id), str(tier.quality_key or ""),
                                     tier.protocol, getattr(tier, "codec", "") or "")
        if fresh:
            tier.url = fresh
    except Exception:  # noqa: BLE001
        pass


def _make_capturer(adapter, tier, room, args, bps: float = 0.0):
    """返回 capture_fn(dest, seconds) -> bool：从当前档位续采一段（HTTP 直取 / RTMP 录制）。"""
    cap_bytes = int(getattr(args, "max_bytes_cap", 24 * 1024 * 1024) or 24 * 1024 * 1024)

    def _capture(dest, seconds) -> bool:
        dest = Path(dest)
        if tier.protocol == "rtmp":
            return bool(adapter.capture_sample(tier, dest, seconds=int(seconds) + 2,
                                               room_id=room.room_id))
        import requests
        from config import COMMON_UA_DESKTOP
        budget = int(bps / 8 * float(seconds) * 1.25) if bps > 0 else 3_000_000
        budget = max(1_500_000, min(budget, cap_bytes))
        hdr = {"User-Agent": COMMON_UA_DESKTOP, "Accept": "*/*"}
        try:
            hdr["Referer"] = adapter.meta.live_url.format(room_id=room.room_id)
        except (KeyError, IndexError, ValueError):
            pass
        resp = requests.get(tier.url, headers=hdr, timeout=20, stream=True)
        buf = bytearray()
        for chunk in resp.iter_content(65536):
            buf.extend(chunk)
            if len(buf) >= budget:
                break
        resp.close()
        if not buf:
            return False
        dest.write_bytes(bytes(buf))
        return True

    return _capture


def _adopt_segment_round(out_dir, ts: str, tag: str, seg: dict, all_tags) -> dict:
    """把「选定轮」的结果落成规范名（segment_scan_{ts}.json + segment_{ts}_*），清掉其余轮。"""
    out_dir = Path(out_dir)
    final = dict(seg or {})
    keep: set = set()

    def _rename(name):
        if not name:
            return name
        new = str(name).replace(f"segment_{tag}", f"segment_{ts}")
        src_path, dst_path = out_dir / str(name), out_dir / new
        if src_path.exists():
            shutil.copy2(src_path, dst_path)
            return new
        if dst_path.exists():
            return new
        keep.add(str(name))
        return str(name)

    if final.get("clip"):
        final["clip"] = _rename(final["clip"])
    final["crops"] = [_rename(c) for c in (final.get("crops") or [])]
    (out_dir / f"segment_scan_{ts}.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    for other in set(all_tags):
        if other == tag:
            continue
        try:
            (out_dir / f"segment_scan_{other}.json").unlink()
        except OSError:
            pass
        for item in out_dir.glob(f"segment_{other}*"):
            try:
                item.unlink()
            except OSError:
                pass
    # 选定轮的原始（rN 命名）文件已复制成规范名 → 清理（含它自己那份 JSON）
    if tag != ts:
        try:
            (out_dir / f"segment_scan_{tag}.json").unlink()
        except OSError:
            pass
    for item in out_dir.glob(f"segment_{tag}*"):
        if item.name not in keep:
            try:
                item.unlink()
            except OSError:
                pass
    return final


def ensure_lip_sample(primary, tier, adapter, args, room, output_dir, ts,
                      need_override=None, tag_suffix=""):
    """保证样本足够长（不足则补采一份更长样本，返回用于分析的路径）。

    need_override：本次需要的时长（秒），用于「健康片段」这类需要更长连续捕获的场景。
    """
    need = float(need_override or getattr(args, "lip_min_seconds", 8) or 8)
    span = _media_span_seconds(primary)
    if span >= need or tier is None:
        if span:
            print(f"   嘴型样本: 复用 {primary.name}（{span:.1f}s）")
        return primary
    size = primary.stat().st_size if primary.exists() else 0
    bps = (size * 8 / span) if (span > 0.5 and size) else 0
    budget = int(bps / 8 * need * 1.25) if bps else int(size * (need / max(span, 0.3)))
    cap = int(getattr(args, "max_bytes_cap", 24 * 1024 * 1024) or 24 * 1024 * 1024)
    budget = max(size, min(budget, cap))
    dest = output_dir / f"lip_sample_{ts}_{tier.quality_key or 'x'}{tag_suffix}.flv"
    got = 0
    if tier.protocol == "rtmp":
        got = adapter.capture_sample(tier, dest, seconds=int(need) + 2, room_id=room.room_id)
    else:
        try:
            import requests
            from config import COMMON_UA_DESKTOP
            headers = {"User-Agent": COMMON_UA_DESKTOP, "Accept": "*/*"}
            try:
                headers["Referer"] = adapter.meta.live_url.format(room_id=room.room_id)
            except (KeyError, IndexError, ValueError):
                pass
            resp = requests.get(tier.url, headers=headers, timeout=20, stream=True)
            buf = bytearray()
            for chunk in resp.iter_content(65536):
                buf.extend(chunk)
                if len(buf) >= budget:
                    break
            resp.close()
            dest.write_bytes(bytes(buf))
            got = len(buf)
        except Exception as exc:  # noqa: BLE001
            logger.warning("嘴型样本补采失败: %s", exc)
    if got:
        s2 = _media_span_seconds(dest)
        print(f"   嘴型样本: 补采 {dest.name}（{got / 1e6:.1f}MB, {s2:.1f}s；"
              f"原样本仅 {span:.1f}s，码率≈{bps / 1e6:.1f}Mbps）")
        return dest
    print(f"   ⚠️ 嘴型样本补采未成功，沿用原样本（{span:.1f}s，可能过短会被判“无法确定”）")
    return primary


def pick_sample_tier(room, adapter=None) -> "object | None":
    """挑选用于采样的档位：优先「源流/原画 + FLV + 可播放」，其次任意可用 FLV/HLS。

    当没有任何 HTTP 档位、且适配器支持录制 RTMP（samples_rtmp=True）时，
    回退到 RTMP 源流（由适配器用 ffmpeg 录制样本）。
    """
    tiers = [t for t in (room.tiers or []) if t.ok and t.protocol in ("flv", "hls")]
    if not tiers:
        # 未探活（如 --no-tier-probe）时按标签优先级回退：源流/原画 FLV 优先
        tiers = [t for t in (room.tiers or []) if t.protocol in ("flv", "hls")]
    if not tiers and adapter is not None and getattr(adapter, "samples_rtmp", False):
        tiers = [t for t in (room.rtmp or []) if t.ok]

    def score(t):
        label = t.label or ""
        s = 0
        if "源流" in label or "原画" in label:
            s += 100
        if t.protocol == "flv":
            s += 50
        if t.playable:
            s += 20
        if "备线" in label:
            s -= 10
        return s

    return max(tiers, key=score) if tiers else None


def run(args) -> int:
    ensure_dirs()
    flv_analyzer, core_report = _load_core()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir, report_dir = Path(OUTPUT_DIR), Path(REPORT_DIR)
    steps = {}

    # ── 1) 平台识别 ──
    registry = platform_registry()
    if not registry:
        print("❌ 未发现任何平台适配器（platforms/*.py）")
        return 2
    target = (args.target or "").strip()
    if not target:
        print("❌ 请提供直播间链接或房间号（或 --list-platforms 查看已接入平台）")
        return 2
    # 分享文本 / 口令 / 短链 → 直播间链接
    # 注意：分享文案里往往内嵌短链（如「【京东】https://3.cn/xxx …」），
    # 直接交给平台适配器会拿到整段文案；这里先统一解析成干净的直播间链接。
    if args.platform == "auto":
        _clean = bool(re.fullmatch(r"(?:https?|rtmps?)://\S+", target)) \
            or bool(re.fullmatch(r"[A-Za-z0-9_\-]{4,32}", target))
        if not _clean:
            try:
                import share_resolver
                _res = share_resolver.resolve_share(target)
                if _res.get("url"):
                    print("── 分享文本解析 ──")
                    print(share_resolver.format_resolution(_res))
                    target = _res["url"]
                elif _res.get("kind") not in ("unknown", "url"):
                    print("❌ 无法直接解析成直播间链接：")
                    print(share_resolver.format_resolution(_res))
                    return 2
            except Exception as exc:  # noqa: BLE001
                logger.debug("分享文本解析跳过：%s", exc)

    if args.platform != "auto":
        adapter = registry.get(args.platform)
        if not adapter:
            print(f"❌ 未知平台 {args.platform}；已接入: {list(registry)}")
            return 2
    else:
        adapter = detect_platform(target)
        if not adapter:
            print(f"❌ 无法自动识别平台（{target[:60]}）；用 --platform 指定，已接入: {list(registry)}")
            return 2
    print(f"[*] 平台: {adapter.meta.name}（{adapter.meta.key}）")

    cookies = args.cookies or load_cookie(adapter.meta.key)
    if cookies:
        print(f"[*] 已加载 {adapter.meta.name} Cookie（{len(cookies)} 字符）")

    # ── 2) 解析直播间 ──
    print(f"\n════ Step 1/5 解析直播间 ════")
    room = None
    try:
        for attempt in range(max(args.poll_retries, 0) + 1):
            room = adapter.resolve(target, ctx={"cookies": cookies, "playwright": args.playwright,
                                                "sample_bytes": args.tier_probe_bytes})
            if room.status == "直播中" or attempt >= args.poll_retries:
                break
            logger.info("未检测到直播（%s），%ss 后重试…", room.status, args.poll_interval)
            time.sleep(args.poll_interval)
        steps["resolve"] = {"ok": bool(room and room.room_id), "status": room.status if room else "失败"}
        print(f"✅ 房间 {room.room_id or '—'} | {room.status} | {room.title or '—'}")
        if room.anchor:
            print(f"   主播: {room.anchor}" + (f"（UID {room.anchor_id}）" if room.anchor_id else ""))
        if room.elapsed_seconds:
            print(f"   已播: {room.elapsed_seconds // 60} 分钟")
        if room.metrics:
            _show = {k: v for k, v in room.metrics.items() if k not in ("desc",) and v not in (None, "")}
            if _show:
                print(f"   指标: {json.dumps(_show, ensure_ascii=False)[:200]}")
    except Exception as exc:
        logger.exception("解析失败")
        print(f"❌ 解析失败: {type(exc).__name__}: {exc}")
        return 2

    # ── 3) 档位探活（适配器内部可能已探）──
    print(f"\n════ Step 2/5 档位探活 ════")
    try:
        if not args.no_tier_probe and room.tiers and not any(t.status for t in room.tiers):
            room.tiers = adapter.probe_tiers(room.tiers, headers=None, sample_bytes=args.tier_probe_bytes)
        _all_streams = list(room.tiers) + list(room.rtmp)
        ok_cnt = sum(1 for t in _all_streams if t.ok)
        for t in room.tiers:
            info = (f"{t.codec or ''} {t.width}x{t.height}" if t.ok else str(t.status))
            print(f"   {'✅' if t.ok else '❌'} {t.label:34s} {t.protocol:4s} {info}")
        for t in room.rtmp:
            print(f"   {'✅' if t.ok else '❌'} {t.label:34s} rtmp {t.url}")
        print(f"   可用档位 {ok_cnt}/{len(room.tiers)}")
        steps["tiers"] = {"ok": ok_cnt > 0, "available": ok_cnt, "total": len(_all_streams)}
    except Exception as exc:
        logger.warning("档位探活异常: %s", exc)
        steps["tiers"] = {"ok": False, "error": str(exc)}

    # ── 4) 采样 + FLV 分析 + 音画同步 ──
    print(f"\n════ Step 3/5 采样与分析 ════")
    sample_tier = pick_sample_tier(room, adapter)
    sampled_path = None
    if sample_tier:
        try:
            print(f"   采样档位: {sample_tier.label}（{sample_tier.protocol}）")
            # 拉流请求头：UA + 平台 Referer（部分 CDN 校验来源）
            from config import COMMON_UA_DESKTOP
            _dl_headers = {"User-Agent": COMMON_UA_DESKTOP, "Accept": "*/*"}
            try:
                _dl_headers["Referer"] = adapter.meta.live_url.format(room_id=room.room_id)
            except (KeyError, IndexError, ValueError):
                pass
            # 采样：HTTP 档位直接下载；RTMP 档位交给适配器用 ffmpeg 录制
            probe = {}
            data = b""
            tag = sample_tier.quality_key or "sample"
            sampled_path = output_dir / f"flv_sample_{ts}_{tag}.flv"
            if sample_tier.protocol == "rtmp":
                got = adapter.capture_sample(sample_tier, sampled_path,
                                             seconds=args.capture_seconds, room_id=room.room_id)
                data = sampled_path.read_bytes() if got else b""
            else:
                probe = http_probe(sample_tier.url, headers=_dl_headers, timeout=12,
                                   sample_bytes=args.max_bytes, verify=False)
                import requests
                resp = requests.get(sample_tier.url, headers=_dl_headers, timeout=12, stream=True)
                buf = bytearray()
                for chunk in resp.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) >= args.max_bytes:
                        break
                resp.close()
                data = bytes(buf)
                if data:
                    sampled_path.write_bytes(data)
            if data:
                print(f"✅ 样本: {sampled_path.name}（{len(data)} 字节）")
                analysis = flv_analyzer.analyze_payload(
                    data=data, output_dir=output_dir, json_output_dir=output_dir, ts=ts,
                    stream_url=sample_tier.url, room_id=room.room_id,
                    url_hints=flv_analyzer.analyze_stream_url_patterns(sample_tier.url),
                    headers_info={"Server": probe.get("server", "")})
                steps["flv"] = {"ok": True, "files": [str(x) for x in
                                                      (analysis.get("report_file"), analysis.get("json_path")) if x]}
                fp = analysis.get("fingerprint") or {}
                print(f"   推流端识别: {fp.get('push_software') or '未知'}")
        except Exception as exc:
            logger.warning("采样/分析失败: %s", exc)
            steps["flv"] = {"ok": False, "error": str(exc)}
    else:
        print("❌ 无可用档位，跳过采样")
        steps["flv"] = {"ok": False, "error": "无可用档位"}

    # 用采样到的 onMetaData 补全推流端信息（适配器未提供时）
    try:
        onmeta = sorted(output_dir.glob("onmetadata_*.json"))[-1]
        meta_tags = {}
        if onmeta.exists():
            _d = json.loads(onmeta.read_text(encoding="utf-8"))
            meta_tags = (_d.get("onMetaData") if isinstance(_d, dict) and "onMetaData" in _d else _d) or {}
            if not isinstance(meta_tags, dict):
                meta_tags = {}
        if meta_tags:
            if not (room.push or {}).get("channel") or (room.push or {}).get("channel", "").startswith("平台侧"):
                room.push["channel"] = infer_push_channel(meta_tags)
            room.push.setdefault("version", format_client_version(meta_tags))
            if not (room.push or {}).get("server"):
                room.push["server"] = infer_push_server(meta_tags)
            room.push["meta"] = {k: meta_tags[k] for k in
                                 ("encoder", "platform", "server", "isHardEncoder") if k in meta_tags}
            room.push["third_party"] = room.push.get("third_party", str(room.push.get("channel") or "").startswith("第三方推流"))
            print(f"   开播渠道: {room.push.get('channel')} | 版本: {room.push.get('version') or '—'}")
    except Exception as exc:
        logger.debug("补全推流信息失败: %s", exc)

    # 内容级人脸嘴型音画同步（不看时间戳；可与时间戳级互补，识别推流前管线失同步）
    if getattr(args, "lip_sync", True) and sampled_path and sampled_path.exists():
        try:
            from lip_sync_detector import run_lip_sync_analysis
            # 让补采目标 ≥ 内容级分析窗口（默认 20s），否则分析窗口大于样本会削弱统计
            _lip_need = max(float(getattr(args, "lip_min_seconds", 8) or 8),
                            float(getattr(args, "lip_sync_seconds", 20) or 20))
            lip_path = ensure_lip_sample(sampled_path, sample_tier, adapter, args, room,
                                         output_dir, ts, need_override=_lip_need)
            lip = run_lip_sync_analysis(lip_path, output_dir, ts, room_id=room.room_id,
                                        seconds=args.lip_sync_seconds,
                                        skip_specificity=bool(getattr(args, "skip_specificity", False)))
            steps["lip_sync"] = {"ok": True, "files": lip.get("output_files") or []}
            _lp = lip.get("perceptual") or {}
            print(f"✅ 嘴型同步: {lip.get('verdict')}（内容级 {_lp.get('content_offset_ms')} ms）")
        except Exception as exc:
            logger.warning("嘴型同步失败: %s", exc)
            steps["lip_sync"] = {"ok": False, "error": str(exc)}

    # ── 内容级「持续测量」总预算：测不出来就一直续采重测，直到测出确定结论或超时（默认 5 分钟）──
    _measure_budget = float(getattr(args, "measure_timeout", 300.0) or 0.0)
    _measure_t0 = time.monotonic()

    def _measure_elapsed() -> float:
        return time.monotonic() - _measure_t0

    def _remaining_budget() -> float:
        if _measure_budget <= 0:
            return 0.0
        return max(0.0, _measure_budget - _measure_elapsed())

    # ── 采样端规避：首次内容级测不出（非正脸/遮挡/未说话）时，自动续采直到「可测」 ──
    _adaptive_n = int(getattr(args, "adaptive_attempts", 0) or 0)
    _lip_verdict = ""
    try:
        _lip_verdict = str((locals().get("lip") or {}).get("verdict") or "")
    except Exception:  # noqa: BLE001
        _lip_verdict = ""
    if (_adaptive_n > 0 and sample_tier is not None and sampled_path and sampled_path.exists()
            and _remaining_budget() > 0
            and (not steps.get("lip_sync", {}).get("ok")
                 or _lip_verdict.startswith(("无法确定", "无法评估")))):
        try:
            import segment_scan as _seg
            from lip_sync_detector import run_lip_sync_analysis as _rl

            _span0 = _media_span_seconds(sampled_path)
            _sz0 = sampled_path.stat().st_size if sampled_path.exists() else 0
            _bps = (_sz0 * 8 / _span0) if (_span0 > 0.5 and _sz0) else 0.0
            _budget = int(_bps / 8 * args.adaptive_chunk * 1.25) if _bps else 3_000_000
            _budget = max(1_500_000, min(_budget, int(getattr(args, "max_bytes_cap", 24 * 1024 * 1024))))

            def _capture(dest, seconds):
                dest = Path(dest)
                if sample_tier.protocol == "rtmp":
                    return bool(adapter.capture_sample(sample_tier, dest,
                                                       seconds=int(seconds) + 2, room_id=room.room_id))
                import requests as _rq
                from config import COMMON_UA_DESKTOP as _UA
                hdr = {"User-Agent": _UA, "Accept": "*/*"}
                try:
                    hdr["Referer"] = adapter.meta.live_url.format(room_id=room.room_id)
                except (KeyError, IndexError, ValueError):
                    pass
                resp = _rq.get(sample_tier.url, headers=hdr, timeout=20, stream=True)
                buf = bytearray()
                for chunk in resp.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) >= _budget:
                        break
                resp.close()
                if not buf:
                    return False
                dest.write_bytes(bytes(buf))
                return True

            print(f"\n════ 采样端规避（最多 {_adaptive_n} 轮 × {args.adaptive_chunk:.0f}s）════")
            acq = _seg.acquire_ready_sample(_capture, out_dir=output_dir, ts=ts,
                                            chunk_s=args.adaptive_chunk,
                                            win_s=args.segment_window,
                                            max_attempts=_adaptive_n,
                                            # 采样端规避最多吃掉总预算的 40%，
                                            # 剩下 ≥60% 留给「健康片段持续测量」（那才是报告展示的口径）
                                            max_seconds=min(float(args.adaptive_seconds),
                                                            _remaining_budget() * 0.4),
                                            log=lambda m: print(m))
            steps["adaptive"] = {"ok": bool(acq.get("sample")), "ready": acq.get("ready"),
                                 "attempts": len(acq.get("attempts") or []),
                                 "reason": acq.get("reason"),
                                 "window": acq.get("window")}
            # 写进房间指标（会进入 live_stream_info 与报告）
            try:
                room.metrics = dict(room.metrics or {})
                _w = acq.get("window") or {}
                room.metrics["采样端规避"] = (
                    ("就绪" if acq.get("ready") else "回退") + "｜"
                    + f"{len(acq.get('attempts') or [])} 轮｜"
                    + (f"{_w.get('start')}~{_w.get('end')}s（正脸 {_w.get('frontality')}，"
                       f"遮挡 {_w.get('occlusion')}，说话 {_w.get('speech_activity')}）"
                       if _w else acq.get("reason") or ""))
            except Exception:  # noqa: BLE001
                pass
            if acq.get("sample"):
                lip2 = _rl(acq["sample"], output_dir, ts + "_adaptive",
                           room_id=room.room_id, seconds=args.lip_sync_seconds)
                _v2 = str(lip2.get("verdict") or "")
                _off2 = (lip2.get("perceptual") or {}).get("content_offset_ms")
                print(f"🔁 采样规避后嘴型同步: {_v2}（内容级 {_off2} ms）")
                steps["lip_sync_adaptive"] = {"ok": True, "verdict": _v2,
                                              "file": str(acq.get("sample"))}
                if not _v2.startswith(("无法确定", "无法评估")):
                    # 规避后得到确定结论 → 以 ts 再跑一次，覆盖规范文件（报告按会话取用）
                    _rl(acq["sample"], output_dir, ts, room_id=room.room_id,
                        seconds=args.lip_sync_seconds)
                    print("   ✅ 已采用规避后的结论（覆盖内容级证据文件）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("采样端规避失败: %s", exc)
            steps["adaptive"] = {"ok": False, "error": str(exc)}

    # ── 健康片段「持续测量」：测不出来就续采重测，直到测出确定结论或超时 ──
    # 「测出来」= 选段成功且片段判定不以「无法确定 / 无法评估」开头（与 lip_sync_detector 同口径）。
    if (getattr(args, "segment_scan", True) and getattr(args, "lip_sync", True)
            and sampled_path and sampled_path.exists()):
        try:
            import segment_scan as segmod
            _rounds: list = []
            _best_round = None                      # (rank, tag, seg)
            _cap = None
            _struct_fail = ("缺音视频轨道", "抽帧/抽音失败", "ffmpeg 不可用", "样本过短")
            while True:
                n = len(_rounds) + 1
                if n == 1:
                    seg_src = ensure_lip_sample(sampled_path, sample_tier, adapter, args, room,
                                                output_dir, ts,
                                                need_override=args.segment_seconds,
                                                tag_suffix="_segment")
                else:
                    left = _remaining_budget()
                    if sample_tier is None or left <= 10:
                        print(f"⏱ 内容级持续测量结束：预算不足（已用 {_measure_elapsed():.0f}s，"
                              f"{len(_rounds)} 轮）")
                        break
                    _refresh_tier_url(adapter, sample_tier, room)
                    if _cap is None:
                        _cap = _make_capturer(adapter, sample_tier, room, args,
                                              _estimate_bps(sampled_path))
                    seg_src = output_dir / f"measure_{ts}_r{n}.flv"
                    # 单轮采集不超过剩余预算的 60%，给紧随其后的「扫描 + 片段测量」留时间，
                    # 避免一轮就把预算吃光（超时只会小幅越界）。
                    _secs = float(min(float(args.segment_seconds), max(8.0, left * 0.6)))
                    print(f"🔁 第 {n} 轮续采 {_secs:.0f}s 重测内容级"
                          f"（剩余预算 {left:.0f}s）")
                    if not _cap(seg_src, _secs):
                        print("   ⚠️ 续采失败，结束重测")
                        break
                tag = f"{ts}_r{n}"
                seg = segmod.run_segment_scan(seg_src, tag, output_dir,
                                              win_s=args.segment_window,
                                              max_seconds=args.segment_seconds)
                rank = _segment_rank(seg)
                best = seg.get("best")
                _rounds.append({"round": n, "tag": tag, "rank": rank,
                                "best": (best or {}).get("start"),
                                "verdict": (seg.get("measurement") or {}).get("verdict"),
                                "error": seg.get("error")})
                if best:
                    bw = best.get("best_window") or {}
                    meas = seg.get("measurement") or {}
                    print(f"{'✅' if rank >= 1000 else '⚠️'} 第 {n} 轮健康片段: "
                          f"{best['start']}~{best['end']}s（{best.get('wins')} 窗，"
                          f"最佳分 {bw.get('score_effective')}）"
                          f" | 片段测量: {meas.get('verdict')}"
                          f" | 偏移 {meas.get('content_offset_ms')} ms"
                          f" | 截图 {len(seg.get('crops') or [])} 张"
                          f" | 播放片段 {'✓' if seg.get('clip') else '✗'}")
                else:
                    print(f"⚠️ 第 {n} 轮未选出健康片段: {seg.get('error') or '样本不足'}")
                if _best_round is None or rank > _best_round[0]:
                    _best_round = (rank, tag, seg)
                if rank >= 1000:                    # 测出确定结论 → 收工
                    break
                err = str(seg.get("error") or "")
                if err and any(k in err for k in _struct_fail):
                    print(f"   ⏭ 结构性失败（{err}），重测无意义，停止")
                    break
                if _remaining_budget() <= 0:
                    print(f"⏱ 内容级持续测量超时（预算 {_measure_budget:.0f}s 用尽，"
                          f"{len(_rounds)} 轮）")
                    break
            if _best_round:
                final_seg = _adopt_segment_round(output_dir, ts, _best_round[1], _best_round[2],
                                                 [r["tag"] for r in _rounds])
                for _r in _rounds:                  # 续采的原始 chunk 不留（避免报告目录膨胀）
                    if _r["round"] > 1:
                        try:
                            (output_dir / f"measure_{ts}_r{_r['round']}.flv").unlink()
                        except OSError:
                            pass
                _ok = _segment_rank(final_seg) >= 1000
                _used = next((r["round"] for r in _rounds if r["tag"] == _best_round[1]),
                             len(_rounds))
                steps["segment"] = {"ok": _ok, "file": final_seg.get("clip"),
                                    "rounds": len(_rounds), "used_round": _used,
                                    "elapsed_s": round(_measure_elapsed(), 1)}
                if not _ok:
                    steps["segment"]["error"] = (
                        final_seg.get("error")
                        or (final_seg.get("measurement") or {}).get("verdict") or "样本不足")
                try:
                    _mv = (final_seg.get("measurement") or {}).get("verdict") or "未测出"
                    room.metrics = dict(room.metrics or {})
                    room.metrics["内容级持续测量"] = (
                        f"{len(_rounds)} 轮｜{_measure_elapsed():.0f}s｜采用第 {_used} 轮｜{_mv}"
                        + ("" if _ok else "（未测出确定结论）"))
                except Exception:  # noqa: BLE001
                    pass
                if len(_rounds) > 1:
                    print(f"📊 内容级持续测量：{len(_rounds)} 轮 / {_measure_elapsed():.0f}s，"
                          f"采用第 {_used} 轮结果")
        except Exception as exc:  # noqa: BLE001
            logger.warning("健康片段选段失败: %s", exc)
            steps["segment"] = {"ok": False, "error": str(exc)}

    if args.av_sync and sampled_path and sampled_path.exists():
        try:
            from av_sync_detector import run_av_sync_analysis
            av = run_av_sync_analysis(sampled_path, output_dir, ts, room_id=room.room_id,
                                      decode_check=True, decode_seconds=args.av_sync_seconds)
            steps["av_sync"] = {"ok": True, "files": av.get("output_files") or []}
            print(f"✅ 音画同步: {av.get('verdict')}（流内 {av.get('perceptual', {}).get('stream_offset_ms')} ms）")
        except Exception as exc:
            logger.warning("音画同步失败: %s", exc)
            steps["av_sync"] = {"ok": False, "error": str(exc)}

    # ── 5) payload + 报告 ──
    print(f"\n════ Step 4/5 生成归一化数据 ════")
    payload = room.to_payload(adapter.meta)
    payload["timestamp"] = ts
    payload["room_id"] = room.room_id
    info_path = output_dir / f"live_stream_info_{ts}.json"
    info_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"✅ 归一化数据: {info_path.name}")

    print(f"\n════ Step 5/5 统一报告 ════")
    json_results = core_report.discover_json_results(output_dir)
    files_data = core_report.merge_results_into_files_data(core_report.scan_captures(output_dir), json_results)
    sessions = core_report.enrich_sessions(core_report.correlate_by_time(files_data, tolerance_seconds=600))
    sessions = core_report.filter_sessions_by_room_id(sessions, room.room_id) or sessions
    if sessions:
        sessions = [max(sessions, key=lambda s: s.get("start_time"))]

    report_folder = report_dir / f"unified_report_{ts}"
    report_folder.mkdir(parents=True, exist_ok=True)
    text_path, html_path = report_folder / "report.txt", report_folder / "index.html"
    if args.format in {"text", "both"}:
        core_report.generate_text_report(sessions, text_path)
    if args.format in {"html", "both"}:
        core_report.generate_html_report(sessions, html_path)
    # 证据 + 播放器依赖
    evidence_dir = report_folder / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    for item in output_dir.iterdir():
        if item.is_file() and ts in item.name:
            shutil.copy2(item, evidence_dir / item.name)
    if REPORT_ASSETS_DIR.exists():
        assets_dir = report_folder / "assets"
        assets_dir.mkdir(exist_ok=True)
        for lib in ("flv.min.js", "hls.min.js"):
            if (REPORT_ASSETS_DIR / lib).exists():
                shutil.copy2(REPORT_ASSETS_DIR / lib, assets_dir / lib)
    print("✅ 报告生成完成")

    print("\n── 汇总 ──")
    for key, label in (("resolve", "解析直播间"), ("tiers", "档位探活"), ("flv", "FLV 分析"),
                       ("av_sync", "音画同步"), ("lip_sync", "嘴型同步"),
                       ("segment", "健康片段选段")):
        s = steps.get(key)
        if not s:
            continue
        extra = ""
        if key == "segment" and s.get("rounds"):
            extra = f"（持续测量 {s.get('rounds')} 轮 / {s.get('elapsed_s')}s，采用第 {s.get('used_round')} 轮）"
        print(f"  {'✅' if s.get('ok') else '❌'} {label}: {s.get('error') or '成功'}{extra}")
    _ad = steps.get("adaptive")
    if _ad:
        _st = "✅" if _ad.get("ready") else ("⚠️" if _ad.get("ok") else "➖")
        _txt = "就绪" if _ad.get("ready") else ("回退最佳窗口" if _ad.get("ok") else "未找到可测窗口")
        print(f"  {_st} 采样端规避: {_txt}（{_ad.get('attempts', 0)} 轮）"
              f"{_ad.get('reason') or _ad.get('error') or ''}")
    if args.format in {"html", "both"}:
        print(f"\nHTML 报告: {html_path}")
        try:
            core_report.open_report_file(html_path)
        except Exception:  # noqa: BLE001
            pass
    if args.format in {"text", "both"}:
        print(f"文本报告: {text_path}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if args.list_platforms:
        for key, ad in platform_registry().items():
            print(f"  {key:14s} {ad.meta.name:10s} 直播间模板: {ad.meta.live_url}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
