#!/usr/bin/env python3
"""本地视频样本的音画同步检测（网页「上传样本」入口的后端）。

对上传的任意视频文件做**两层**音画同步检测：
  · 时间戳级（av_sync_detector）：FLV 标签级 + ffmpeg 解码级 A-V。
    非 FLV 上传会先尝试**无损重封装为 FLV**（-c copy）；失败则只做解码级并注明。
  · 内容级（lip_sync_detector）：人脸嘴型 × 音频包络，与时间戳无关 →
    能发现「推流前管线（采集卡/声卡/前置处理）」引入的失同步。

产物：<report_dir>/sync_report_<ts>/{index.html, report.txt}
      + <output_dir>/ 下的 av_sync_* / lip_sync_* 证据与 flv_analysis

用法:
    python core/sync_cli.py --file uploads/xxx.flv --seconds 20
    python core/sync_cli.py --file sample.mp4 --roi 0.30,0.55,0.40,0.35 --no-face
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "platforms"))

from config import OUTPUT_DIR, REPORT_DIR, REPORT_ASSETS_DIR, ensure_dirs  # noqa: E402

logger = logging.getLogger("sync_cli")

FFMPEG_CANDIDATES = ("/opt/ffmpeg/bin/ffmpeg",
                     "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
FFPROBE_CANDIDATES = ("/opt/ffmpeg/bin/ffprobe",
                      "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")


def _bin(name: str, cands: tuple):
    exe = shutil.which(name)
    if exe:
        return exe
    for c in cands:
        if Path(c).exists():
            return c
    return None


def is_flv(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(3) == b"FLV"
    except OSError:
        return False


def remux_to_flv(src: Path, dest: Path) -> bool:
    """无损重封装为 FLV（h264/hevc + aac/mp3 可直接 copy）。"""
    exe = _bin("ffmpeg", FFMPEG_CANDIDATES)
    if not exe:
        return False
    cmd = [exe, "-y", "-v", "error", "-i", str(src), "-c", "copy", "-f", "flv", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def clip_flv(src: Path, dest: Path, seconds: float) -> bool:
    """截取前 N 秒为 FLV（标签级分析只需要前段，避免把整个大文件读进内存）。"""
    exe = _bin("ffmpeg", FFMPEG_CANDIDATES)
    if not exe:
        return False
    cmd = [exe, "-y", "-v", "error", "-i", str(src), "-t", f"{seconds:.2f}",
           "-c", "copy", "-f", "flv", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def build_payload(src: Path, meta: dict, ts: str, notes: list, remuxed: bool) -> dict:
    """写一份最小归一化 payload，让统一报告正常渲染。"""
    vcodec, acodec = meta.get("vcodec"), meta.get("acodec")
    return {
        "type": "live_stream_info",
        "platform": "file",
        "platform_name": "本地样本",
        "platform_color": "#7c9cff",
        "platform_url": "",
        "platform_cta": "",
        "report_title": "音画同步检测（上传样本）",
        "room_id": f"file:{src.name}",
        "status": "本地样本",
        "title": src.name,
        "anchor": "",
        "live_start_ts": None,
        "elapsed_seconds": int(meta.get("duration") or 0),
        "metrics": {
            "文件名": src.name,
            "大小MB": round(src.stat().st_size / 1e6, 2),
            "时长s": round(float(meta.get("duration") or 0), 2),
            "分辨率": (f"{meta.get('width')}x{meta.get('height')}"
                    if meta.get("width") and meta.get("height") else "—"),
            "帧率": meta.get("fps") or "—",
            "视频编码": vcodec or "—",
            "音频编码": acodec or "—",
            "容器": "FLV（原生）" if not remuxed else "已无损重封装为 FLV",
        },
        "tiers": [], "rtmp": [],
        "push": {},
        "notes": notes,
        "timestamp": ts,
    }


def run(args) -> int:
    ensure_dirs()
    ts = args.ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    src = Path(args.file).resolve()
    output_dir = Path(args.output_dir)
    report_dir_root = Path(args.report_dir)
    report_dir = report_dir_root / f"sync_report_{ts}"
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        print(f"❌ 文件不存在: {src}")
        return 2

    import lip_sync_detector
    import av_sync_detector
    import flv_analyzer
    import report as core_report

    meta = lip_sync_detector.probe_media(src)
    notes: list = []
    print(f"════ 样本 ════\n{src.name}（{src.stat().st_size / 1e6:.1f} MB）")
    print(f"  时长 {meta.get('duration')}s | {meta.get('width')}x{meta.get('height')}@{meta.get('fps')}fps"
          f" | 视频 {meta.get('vcodec')} | 音频 {meta.get('acodec')}")
    if not meta.get("has_audio") or not meta.get("has_video"):
        notes.append("样本缺少音频或视频轨道：音画同步检测需要两者同时存在。")

    # ── 1) 时间戳级 ──
    steps: dict = {}
    tag_src: Path | None = None
    remuxed = False
    if is_flv(src):
        tag_src = src
        print("   容器: FLV（原生）")
    else:
        dest = output_dir / f"remux_{ts}.flv"
        if remux_to_flv(src, dest):
            tag_src, remuxed = dest, True
            notes.append("上传样本非 FLV：已用 ffmpeg -c copy 无损重封装为 FLV 做标签级分析。")
            print("   容器: 非 FLV → 已无损重封装为 FLV")
        else:
            notes.append("上传样本无法无损重封装为 FLV（编码不兼容）：跳过标签级，仅做解码级 A-V。")
            print("   容器: 非 FLV 且重封装失败 → 仅解码级")

    flv_sample = None
    if tag_src is not None and args.av_sync:
        clip = output_dir / f"flv_sample_{ts}.flv"
        if clip_flv(tag_src, clip, max(float(args.seconds), 20.0)):
            flv_sample = clip
        else:
            flv_sample = tag_src
        try:
            data = flv_sample.read_bytes()
            print(f"════ 时间戳级 ════\n  标签级分析样本 {flv_sample.name}（{len(data) / 1e6:.1f} MB）")
            analysis = flv_analyzer.analyze_payload(
                data=data, output_dir=output_dir, json_output_dir=output_dir, ts=ts,
                stream_url="", room_id=f"file:{src.name}", url_hints=[], headers_info={},
                stream_url_struct_file=None)
            fp = (analysis or {}).get("fingerprint") or {}
            print(f"   推流端识别: {fp.get('push_software') or '未能明确识别'}")
            av = av_sync_detector.run_av_sync_analysis(
                flv_sample, output_dir, ts, room_id=f"file:{src.name}",
                decode_check=True, decode_seconds=int(args.decode_seconds))
            p = av.get("perceptual") or {}
            print(f"   感知判定: {av.get('verdict')}")
            print(f"   流内偏移: {p.get('stream_offset_ms')} ms | 播放器残差: {p.get('player_residual_ms')} ms")
            steps["av_sync"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("时间戳级检测失败: %s", exc)
            notes.append(f"时间戳级检测失败：{type(exc).__name__}: {exc}")
            print(f"   ⚠️ 时间戳级失败: {exc}")
    elif not args.av_sync:
        print("════ 时间戳级 ════\n  （已按参数跳过）")

    # ── 2) 内容级（人脸嘴型）──
    if args.lip_sync:
        try:
            roi = None
            if args.roi:
                roi = [float(v) for v in args.roi.replace(" ", "").split(",")]
                if len(roi) != 4:
                    roi = None
            print("════ 内容级（人脸嘴型）════")
            lip = lip_sync_detector.run_lip_sync_analysis(
                src, output_dir, ts, room_id=f"file:{src.name}",
                seconds=float(args.seconds), roi=roi, use_face=not args.no_face,
                fine_fps=int(args.fine_fps), use_fine=not args.no_fine)
            p = lip.get("perceptual") or {}
            m = lip.get("metrics") or {}
            print(f"   判定: {lip.get('verdict')}（置信度 {p.get('confidence')}）")
            print(f"   内容级偏移: {p.get('content_offset_ms')} ms（{p.get('direction')}）"
                  f" | 分辨力 {p.get('lag_resolution_ms')} ms"
                  f" | CI± {p.get('content_offset_ci_ms')}"
                  f" | 一致率 {p.get('window_agreement')}"
                  f" | 人脸覆盖 {(m.get('face_coverage') or 0) * 100:.0f}%")
            steps["lip_sync"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("内容级检测失败: %s", exc)
            notes.append(f"内容级检测失败：{type(exc).__name__}: {exc}")
            print(f"   ⚠️ 内容级失败: {exc}")

    # ── 3) payload + 统一报告 ──
    (output_dir / f"live_stream_info_{ts}.json").write_text(
        json.dumps(build_payload(src, meta, ts, notes, remuxed), ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("════ 报告 ════")
    files_data = core_report.merge_results_into_files_data(
        core_report.scan_captures(output_dir), core_report.discover_json_results(output_dir))
    sessions = core_report.correlate_by_time(files_data, tolerance_seconds=600)
    sessions = core_report.enrich_sessions(sessions)
    sessions = sessions[-1:] if sessions else []
    save_html = args.format in ("html", "both")
    save_text = args.format in ("text", "both")
    if save_html:
        core_report.generate_html_report(sessions, report_dir / "index.html")
    if save_text:
        core_report.generate_text_report(sessions, report_dir / "report.txt")
    # 证据 + 播放器依赖
    ev = report_dir / "evidence"
    for item in output_dir.iterdir():
        if item.is_file() and ts in item.name:
            shutil.copy2(item, ev / item.name)
    if REPORT_ASSETS_DIR.exists():
        assets = report_dir / "assets"
        assets.mkdir(exist_ok=True)
        for lib in ("flv.min.js", "hls.min.js"):
            if (REPORT_ASSETS_DIR / lib).exists():
                shutil.copy2(REPORT_ASSETS_DIR / lib, assets / lib)
    if save_html:
        print(f"HTML 报告: {report_dir / 'index.html'}")
    if save_text:
        print(f"文本报告: {report_dir / 'report.txt'}")
    return 0 if (flv_sample or steps.get("lip_sync")) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="本地视频样本音画同步检测")
    ap.add_argument("--file", required=True, help="视频样本路径")
    ap.add_argument("--seconds", type=float, default=20.0, help="内容级分析时长（秒，默认 20）")
    ap.add_argument("--decode-seconds", type=int, default=10, help="解码级 A-V 验证时长（秒）")
    ap.add_argument("--fine-fps", type=int, default=200, help="内容级互相关细网格帧率（默认 200=5ms）")
    ap.add_argument("--roi", help="手动嘴部 ROI：相对坐标 x,y,w,h（跳过人脸检测）")
    ap.add_argument("--no-face", action="store_true", help="禁用人脸检测（需配合 --roi）")
    ap.add_argument("--no-fine", action="store_true", help="关闭内容级高精度通道")
    ap.add_argument("--no-av-sync", dest="av_sync", action="store_false", default=True,
                    help="跳过时间戳级检测")
    ap.add_argument("--no-lip-sync", dest="lip_sync", action="store_false", default=True,
                    help="跳过内容级检测")
    ap.add_argument("--output-dir", default=str(OUTPUT_DIR), help="证据输出目录")
    ap.add_argument("--report-dir", default=str(REPORT_DIR), help="报告输出目录")
    ap.add_argument("--format", choices=["html", "text", "both"], default="both")
    ap.add_argument("--ts", help="时间戳（默认当前时间）")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
