"""
抖音直播流分析 — 统一报告生成器
按 room_id 与时间邻近性整合 reflow / FLV / RTMP 三类证据。
"""

import argparse
import glob
import html
import json
import logging
import os

logger = logging.getLogger(__name__)
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from amf0 import ci_get
from config import BASE_DIR, OUTPUT_DIR, REPORT_DIR, SOFTWARE_PATTERNS, CDN_PATTERNS, ensure_dirs
from mappings_loader import load_json_mapping


TIMESTAMP_PATTERN = re.compile(r"(202\d{5}_\d{6})")
ROOM_TS_PATTERN = re.compile(r"reflow_(\d+)_(\d{8}_\d{6})\.json$")
CONFIDENCE_COLORS = {
    "高": "#4caf50",
    "中": "#fbc02d",
    "低": "#ef5350",
}

FIELD_DESCRIPTIONS = load_json_mapping("field_descriptions.json")


def _extract_stream_url_info(stream_url, interesting_fields):
    """从 stream_url dict 中提取关键推流信息并注入到 interesting_fields。"""
    if not isinstance(stream_url, dict):
        return
    # 分辨率
    extra = stream_url.get("extra") or {}
    w = extra.get("width")
    h = extra.get("height")
    if w and h:
        interesting_fields["data.room.stream_url.resolution"] = f"{w}x{h}"
    # 默认清晰度
    default_res = stream_url.get("default_resolution")
    if default_res:
        res_names = stream_url.get("resolution_name") or {}
        res_label = res_names.get(default_res, default_res)
        interesting_fields["data.room.stream_url.default_quality"] = f"{default_res}（{res_label}）"
    # 视频编码 — 从 pull_url_params 中提取 VCodec
    for params_key in ("hls_pull_url_params", "rtmp_pull_url_params"):
        params_raw = stream_url.get(params_key, "")
        if isinstance(params_raw, str) and "VCodec" in params_raw:
            try:
                params_obj = json.loads(params_raw)
                vcodec = params_obj.get("VCodec", "")
                if vcodec:
                    interesting_fields["data.room.stream_url.video_codec"] = vcodec
                    break
            except (json.JSONDecodeError, TypeError):
                pass
    # 硬件编码
    hw_encode = extra.get("hardware_encode")
    if hw_encode is not None:
        interesting_fields["data.room.stream_url.hardware_encode"] = "是" if hw_encode else "否"
    # FLV 拉流地址（取默认清晰度的）
    flv_pull = stream_url.get("flv_pull_url")
    if isinstance(flv_pull, dict):
        for quality, url in flv_pull.items():
            if url and isinstance(url, str):
                interesting_fields[f"data.room.stream_url.flv_pull_url.{quality}"] = url
                break
    # RTMP 推流地址
    rtmp_push = stream_url.get("rtmp_push_url")
    if rtmp_push and isinstance(rtmp_push, str) and rtmp_push.startswith("rtmp://"):
        interesting_fields["data.room.stream_url.rtmp_push_url"] = rtmp_push
    # stream_url.provider → CDN 提供商标识
    provider = stream_url.get("provider")
    if provider is not None:
        interesting_fields["data.room.stream_url.provider"] = provider
    # 从拉流 URL 域名推断 CDN
    pull_url = stream_url.get("rtmp_pull_url") or ""
    if isinstance(flv_pull, dict):
        for v in flv_pull.values():
            if v and isinstance(v, str):
                pull_url = pull_url or v
                break
    if pull_url:
        cdn = infer_cdn(pull_url)
        if cdn:
            interesting_fields["data.room.stream_url.cdn"] = cdn


# ═══════════════ 拉流地址结构化渲染（stream_url 全景） ═══════════════

# URL 签名参数含义（key 统一小写匹配）
STREAM_URL_PARAM_DESCRIPTIONS = {
    "expire": "链接过期时间（Unix 秒），过期后 CDN 拒绝服务",
    "sign": "火山引擎防盗链签名（MD5），与 volcSecret 同值",
    "volcsecret": "火山引擎防盗链签名（volcSecret）",
    "volctime": "签名有效期时间戳（Unix 秒）",
    "arch_hrchy": "调度架构层级标记（h1 = 一层架构）",
    "exp_hrchy": "实验层级路由标记",
    "rtm_expr_tag": "URL 下发来源标记（reflow_room_info = 来自 reflow/info 接口）",
    "major_anchor_level": "主播等级（common = 普通主播，影响调度与线路优先级）",
    "unique_id": "播放唯一标识，结构: 流名_推流会话ID_协议_画质档",
    "t_id": "本次拉流会话/追踪 ID（对应 stream_data.common.session_id）",
    "fs_time": "追帧时间偏移（毫秒，负值 = 向当前时刻追，常见于极速档）",
    "only_audio": "纯音频流标记（1 = 仅音频）",
    "eupf_relay_mode": "边缘中继转发模式标记",
}

# 播放器/SDK 参数含义（key 统一小写匹配）
PLAYER_PARAM_DESCRIPTIONS = {
    "fastopenduration": "首帧快开偏移（ms，负值 = 提前起播）",
    "bufferdatams": "起播缓冲水位（ms）",
    "playingintervalms": "播放进度上报间隔（ms）",
    "p2pfastopenduration": "P2P 模式首帧快开偏移（ms）",
    "vcodec": "视频解码器",
    "enabled": "是否启用",
    "hurrymillisecond": "卡顿后追帧窗口（ms）",
    "hurrytime": "追帧触发时长（ms）",
    "hurryspeed": "追帧倍速",
    "hurrystartms": "追帧起始延迟（ms）",
    "hurrytype": "追帧策略类型",
    "hurrystoptype": "追帧停止条件",
    "slowmillisecond": "慢放触发阈值（ms）",
    "slowtime": "慢放时长（ms）",
    "slowspeed": "慢放倍速",
    "vbitrate": "目标视频码率（bps）",
    "resolution": "目标分辨率",
    "gop": "GOP 长度（秒）",
    "fps": "帧率",
    "drtype": "动态范围（sdr/hdr）",
}

# SDK 内部画质档位 → 含义
SDK_QUALITY_NAMES = {
    "source": "源流（未转码）",
    "origin": "原画", "or4": "原画（第4版模板）",
    "hd": "超清", "sd": "高清", "ld": "标清",
    "md": "极速（超低画质，仅 SDK 内部）", "ao": "纯音频",
    "Stage0T000hd": "超清（转码模板名）",
}

# 顶层画质 key → 中文名
QUALITY_KEY_NAMES = {
    "FULL_HD1": "蓝光", "HD1": "超清", "ORIGION": "原画", "SD1": "标清", "SD2": "高清",
}

PROTOCOL_NAMES = {
    "flv": "HTTP-FLV", "hls": "HLS(m3u8)", "lls": "LLS 低延迟(SDP)",
    "dash": "DASH", "cmaf": "CMAF", "ll_hls": "LL-HLS",
    "tsl": "TSL", "tile": "Tile", "http_ts": "HTTP-TS",
}


def _pick_display_stream_url(stream_url_value):
    """从 stream_url（dict 或 str）提取一个可展示的拉流 URL。返回 (标签, url)。"""
    if not stream_url_value:
        return "", ""
    if isinstance(stream_url_value, str):
        if stream_url_value.startswith(("http://", "https://", "rtmp://")):
            return "拉流地址", stream_url_value
        return "", ""
    if not isinstance(stream_url_value, dict):
        return "", ""
    res_names = stream_url_value.get("resolution_name") or {}

    def _label(key):
        return res_names.get(key) or QUALITY_KEY_NAMES.get(key, key)

    flv = stream_url_value.get("flv_pull_url")
    if isinstance(flv, dict) and flv:
        default_q = stream_url_value.get("default_resolution")
        order = [default_q] if default_q else []
        order += [q for q in ["FULL_HD1", "ORIGION", "HD1", "SD1", "SD2"] if q != default_q]
        order += [q for q in flv.keys() if q not in order]
        for q in order:
            url = flv.get(q)
            if isinstance(url, str) and url.startswith("http"):
                return f"FLV · {q}（{_label(q)}）", url
    hls_map = stream_url_value.get("hls_pull_url_map")
    if not isinstance(hls_map, dict) and isinstance(stream_url_value.get("hls_pull_url"), dict):
        hls_map = stream_url_value["hls_pull_url"]
    if isinstance(hls_map, dict):
        for q, url in hls_map.items():
            if isinstance(url, str) and url.startswith("http"):
                return f"HLS · {q}（{_label(q)}）", url
    rtmp = stream_url_value.get("rtmp_pull_url")
    if isinstance(rtmp, str) and rtmp.startswith(("rtmp://", "http")):
        return "RTMP（遗留字段）", rtmp
    return "", ""


def _decode_stream_sdk_data(stream_url_obj):
    """解析 live_core_sdk_data.pull_data。返回 (pull_data, stream_data)。"""
    if not isinstance(stream_url_obj, dict):
        return None, None
    sdk = stream_url_obj.get("live_core_sdk_data")
    if not isinstance(sdk, dict):
        return None, None
    pull = sdk.get("pull_data")
    if not isinstance(pull, dict):
        return None, None
    stream_data = None
    raw = pull.get("stream_data")
    if isinstance(raw, str) and raw.strip():
        try:
            stream_data = json.loads(raw)
        except json.JSONDecodeError:
            stream_data = None
    elif isinstance(raw, dict):
        stream_data = raw
    return pull, stream_data


def _get_session_stream_url_obj(session):
    """按优先级取出会话的 stream_url 原始 dict。"""
    for rd in session.get("reflow_data", []):
        raw = rd.get("raw")
        if isinstance(raw, dict):
            su = (((raw.get("data") or {}).get("room")) or {}).get("stream_url")
            if isinstance(su, dict) and su:
                return su
            ft = raw.get("field_tree")
            if isinstance(ft, dict):
                wrapper = ft.get("data.room.stream_url")
                if isinstance(wrapper, dict) and isinstance(wrapper.get("value"), dict):
                    return wrapper["value"]
    for su_item in session.get("stream_url_data", []):
        if su_item.get("type") == "stream_url_raw" and isinstance(su_item.get("stream_url_obj"), dict):
            return su_item["stream_url_obj"]
    return None


def _build_source_stream_urls(stream_url_obj):
    """构造源流（未转码）拉流地址。

    ao（纯音频）档的路径是不带画质后缀的 stream-<id>.flv，即主播端原生推流；
    服务端靠 query 里的 only_audio=1 剥离视频。签名按路径校验，增删 query
    参数不影响签名，因此去掉 only_audio / eupf_relay_mode 即得到完整源流
    （未经过云端转码二压）。返回 [(线路名, 源流URL), ...]。
    """
    _pull, sdata = _decode_stream_sdk_data(stream_url_obj)
    ao = ((sdata or {}).get("data") or {}).get("ao") or {}
    urls = []
    for line_name in ("main", "backup"):
        u = (ao.get(line_name) or {}).get("flv") or ""
        if not isinstance(u, str) or not u.strip():
            continue
        try:
            parts_u = urlparse(u)
            q = {k: v[0] for k, v in parse_qs(parts_u.query).items()}
            removed = [k for k in ("only_audio", "eupf_relay_mode") if q.pop(k, None) is not None]
            if not removed:
                continue
            urls.append((line_name, urlunparse(parts_u._replace(query=urlencode(q)))))
        except ValueError:
            continue
    return urls


def _fmt_unix_ts(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _fmt_bitrate_k(bps):
    try:
        v = float(bps)
        if v <= 0:
            return ""
        return f"{v / 1000:.0f} kbps"
    except (TypeError, ValueError):
        return ""


def _render_stream_url_panorama(session, parts):
    """拉流地址全景：把 reflow 下发的 stream_url 结构化渲染（画质/线路/协议/签名参数）。"""
    su = _get_session_stream_url_obj(session)
    if not isinstance(su, dict) or not su:
        return False

    pull, sdata = _decode_stream_sdk_data(su)
    common = (sdata or {}).get("common") or {}
    levels = (sdata or {}).get("data") or {}
    options = (pull or {}).get("options") or {}
    menu = [q for q in (options.get("qualities") or []) if isinstance(q, dict)]
    menu_by_key = {str(q.get("sdk_key")): q for q in menu}
    res_names = su.get("resolution_name") or {}

    def q_name(key):
        key = str(key)
        if key in menu_by_key:
            return menu_by_key[key].get("name") or key
        if key in res_names:
            return res_names[key]
        return SDK_QUALITY_NAMES.get(key, key)

    def level_sort(item):
        key = str(item[0])
        q = menu_by_key.get(key)
        return (0, q.get("level", 99), key) if q else (1, 0, key)

    parts.append("<details open id='stream-panorama'><summary><h3 style='display:inline'>🔗 拉流地址全景</h3> "
                 "<span class='muted small'>stream_url 结构化解析 · 画质 / 线路 / 协议 / 签名参数</span></summary>")
    parts.append("<div class='card'>")

    def metric(label, value, note=""):
        note_html = f" <span class='muted small'>{escape_cell(note)}</span>" if note else ""
        return (f"<div class='metric'><div class='muted small'>{escape_cell(label)}</div>"
                f"<div style='font-weight:600'>{escape_cell(str(value))}{note_html}</div></div>")

    # ── 1. 调度概览 ──
    lines_info = common.get("lines") or {}
    cdn_hit = ""
    rule_raw = common.get("rule_ids")
    if isinstance(rule_raw, str) and rule_raw.strip():
        try:
            rule = json.loads(rule_raw)
            sched_raw = rule.get("sched")
            sched = json.loads(sched_raw) if isinstance(sched_raw, str) else (sched_raw or {})
            hit = (((sched or {}).get("result")) or {}).get("cdn")
            if hit is not None:
                cdn_hit = f"cdn:{hit}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    default_q = su.get("default_resolution") or ""
    main_push = common.get("main_push_id")
    backup_push = common.get("backup_push_id")
    push_note = f"（备 {backup_push}）" if backup_push and backup_push != main_push else ""
    dq = options.get("default_quality") or {}

    parts.append("<div class='grid'>")
    parts.append(metric("流名称", common.get("stream_name") or su.get("id_str") or "未知", "URL 中的 stream-<id>"))
    if default_q:
        parts.append(metric("默认画质", f"{default_q}（{q_name(default_q)}）", "default_resolution"))
    if dq.get("sdk_key"):
        parts.append(metric("SDK 默认档", f"{dq.get('sdk_key')}（{dq.get('name')}）", "options.default_quality"))
    cand = ", ".join(su.get("candidate_resolution") or [])
    if cand:
        parts.append(metric("候选画质", cand, "服务端判定该客户端可切换档位"))
    if lines_info:
        parts.append(metric("主线路", lines_info.get("main") or "未知", "lines.main"))
        parts.append(metric("备线路", lines_info.get("backup") or "未知", "lines.backup"))
    if cdn_hit:
        parts.append(metric("调度命中", cdn_hit, "rule_ids.sched.result.cdn"))
    if common.get("app_id"):
        parts.append(metric("业务 app_id", common["app_id"], "stream_data.common"))
    if main_push:
        parts.append(metric("推流会话 ID", f"{main_push}{push_note}", "unique_id 中段"))
    if common.get("mode"):
        parts.append(metric("调度模式", common["mode"]))
    parts.append(metric("P2P/PCDN", "启用（火山 PCDN）" if common.get("p2p_params") else "未下发", "p2p_params"))
    secret = common.get("secret_key") or ""
    if secret:
        parts.append(metric("播放密钥", secret[:8] + "…（已脱敏）", "secret_key"))
    extra = su.get("extra") or {}
    if extra.get("width") and extra.get("height"):
        parts.append(metric("推流上报分辨率", f"{extra['width']}x{extra['height']}", "stream_url.extra"))
    enc = None
    for lv in levels.values():
        if not isinstance(lv, dict):
            continue
        for ln in lv.values():
            if isinstance(ln, dict) and "enableEncryption" in ln:
                enc = ln.get("enableEncryption")
                break
        if enc is not None:
            break
    if enc is not None:
        parts.append(metric("流加密", "是" if enc else "否", "enableEncryption"))
    parts.append("</div>")

    # ── 2. 画质梯度 ──
    if levels:
        parts.append("<h4 style='margin-top:18px'>画质梯度（转码档位）</h4>")
        parts.append("<table><thead><tr><th>档位</th><th>名称</th><th>分辨率</th><th>目标码率</th>"
                     "<th>实测码率</th><th>帧率</th><th>GOP(秒)</th><th>编码</th></tr></thead><tbody>")
        for key, lv in sorted(levels.items(), key=level_sort):
            if not isinstance(lv, dict):
                continue
            main_ln = lv.get("main") or {}
            sdk_p = main_ln.get("sdk_params") or {}
            if isinstance(sdk_p, str):
                try:
                    sdk_p = json.loads(sdk_p)
                except json.JSONDecodeError:
                    sdk_p = {}
            tri = main_ln.get("templateRealTimeInfo") or {}
            menu_q = menu_by_key.get(str(key)) or {}
            is_audio = str(key) == "ao" or "only_audio=1" in str(main_ln.get("flv", ""))
            if is_audio:
                res, vbr_t, codec = "纯音频", "—", "—"
            else:
                res = sdk_p.get("resolution") or menu_q.get("resolution") or "—"
                vbr_t = _fmt_bitrate_k(sdk_p.get("vbitrate") or menu_q.get("v_bit_rate")) or "—"
                codec = sdk_p.get("VCodec") or (f"h{menu_q.get('v_codec')}" if menu_q.get("v_codec") else "—")
            vbr_r = f"{tri['bitrateKbps']:.0f} kbps" if isinstance(tri.get("bitrateKbps"), (int, float)) else "—"
            fps = sdk_p.get("fps") or menu_q.get("fps") or "—"
            gop = sdk_p.get("gop") if sdk_p.get("gop") not in (None, "") else "—"
            parts.append(f"<tr><td><code>{escape_cell(str(key))}</code></td>"
                         f"<td>{escape_cell(q_name(key))}</td><td>{escape_cell(str(res))}</td>"
                         f"<td>{escape_cell(str(vbr_t))}</td><td>{escape_cell(str(vbr_r))}</td>"
                         f"<td>{escape_cell(str(fps))}</td><td>{escape_cell(str(gop))}</td>"
                         f"<td>{escape_cell(str(codec))}</td></tr>")
        parts.append("</tbody></table>")
        parts.append("<div class='muted small'>目标码率 = sdk_params.vbitrate / qualities.v_bit_rate；"
                     "实测码率 = templateRealTimeInfo.bitrateKbps（转码模板实时上报）。"
                     "md 为 SDK 内部超低画质档（不进清晰度菜单），ao 为纯音频流。</div>")

    # ── 3. 拉流地址总表 ──
    parts.append("<h4 style='margin-top:18px'>拉流地址（画质 × 协议 × 线路）</h4>")
    parts.append("<table><thead><tr><th>画质</th><th>协议</th><th>线路</th><th>域名</th>"
                 "<th>URL（完整可复制）</th><th>操作</th></tr></thead><tbody>")

    def url_row(qkey, proto, line_name, url):
        if not isinstance(url, str) or not url.strip():
            return
        try:
            host = urlparse(url).netloc or "—"
        except ValueError:
            host = "—"
        proto_label = PROTOCOL_NAMES.get(proto, proto)
        pcls = {"flv": "proto-flv", "hls": "proto-hls", "lls": "proto-lls"}.get(proto, "proto-other")
        lcls = "line-main" if line_name == "main" else "line-backup"
        ltxt = "主线" if line_name == "main" else "备线"
        row_label = f"{q_name(qkey)} · {ltxt}"
        play_btn = ""
        if proto in ("flv", "hls"):
            play_btn = (f"<button class='play-btn' data-url='{escape_cell(url)}' data-proto='{proto}' "
                        f"data-label='{escape_cell(row_label + ' · ' + proto_label)}'>▶ 播放</button>")
        parts.append(
            f"<tr><td><span class='qbadge'>{escape_cell(q_name(qkey))}</span>"
            f"<div class='muted small'>{escape_cell(str(qkey))}</div></td>"
            f"<td><span class='proto-tag {pcls}'>{escape_cell(proto_label)}</span></td>"
            f"<td class='{lcls}'>{ltxt}</td>"
            f"<td class='small'>{escape_cell(host)}</td>"
            f"<td><code class='url-cell' style='cursor:pointer' title='点击播放' "
            f"data-url='{escape_cell(url)}' data-proto='{proto}' data-label='{escape_cell(row_label)}'>{escape_cell(url)}</code></td>"
            f"<td style='white-space:nowrap'>{play_btn}"
            f"<button class='copy-btn' data-copy='{escape_cell(url)}'>复制</button></td></tr>")

    if levels:
        for key, lv in sorted(levels.items(), key=level_sort):
            if not isinstance(lv, dict):
                continue
            for line_name in ("main", "backup"):
                ln = lv.get(line_name) or {}
                if not isinstance(ln, dict):
                    continue
                for proto in ("flv", "hls", "lls", "dash", "cmaf", "ll_hls", "http_ts"):
                    url_row(key, proto, line_name, ln.get(proto))
    else:
        for q, u in (su.get("flv_pull_url") or {}).items():
            url_row(q, "flv", "main", u)
        for q, u in (su.get("hls_pull_url_map") or {}).items():
            url_row(q, "hls", "main", u)
        url_row("rtmp", "flv", "main", su.get("rtmp_pull_url"))
    for _src_line, _src_url in _build_source_stream_urls(su):
        url_row("source", "flv", _src_line, _src_url)
    parts.append("</tbody></table>")
    parts.append("<div class='muted small'>主线/备线 = stream_data.data.&lt;档位&gt;.main/.backup，"
                 "对应 lines.main / lines.backup 两条独立 CDN 线路（播放器卡顿时自动切换）。"
                 "同档位不同后缀（如 or4 / Stage0T000hd / ld）为不同转码模板名。</div>")
    if _build_source_stream_urls(su):
        parts.append("<div class='muted small'>💡 <strong>源流（未转码）</strong> = 无画质后缀的 "
                     "stream-&lt;id&gt;.flv，即主播端推流的原生数据，不经云端转码二压；由 ao 纯音频地址"
                     "去掉 only_audio 参数构造（防盗链签名按路径校验，依然有效）。若主播以 H.265/HEVC 推流，"
                     "网页播放器 flv.js 无法解码（会提示改用转码档或 VLC / ffplay）；H.264 房间可直接播放。"
                     "源流与蓝光(or4)的编码差异可用 ffprobe 对比确认。备线地址可能 302 跳转，播放器会自动跟随。</div>")

    # ── 4. URL 签名参数解析 ──
    sample_url = ""
    if levels:
        for key in ("origin", "or4", "hd", "Stage0T000hd", "sd", "ld"):
            lv = levels.get(key)
            if isinstance(lv, dict) and isinstance(lv.get("main"), dict):
                u = lv["main"].get("flv")
                if isinstance(u, str) and u:
                    sample_url = u
                    break
    if not sample_url:
        flv_map = su.get("flv_pull_url") or {}
        for u in flv_map.values():
            if isinstance(u, str) and u:
                sample_url = u
                break
    if sample_url:
        try:
            q = parse_qs(urlparse(sample_url).query)
        except ValueError:
            q = {}
        if q:
            parts.append("<h4 style='margin-top:18px'>URL 签名参数解析 <span class='muted small'>（以默认档 FLV 地址为例）</span></h4>")
            parts.append("<table><thead><tr><th>参数</th><th>值</th><th>含义</th></tr></thead><tbody>")
            for pk in sorted(q.keys(), key=str.lower):
                pv = q[pk][0] if q[pk] else ""
                desc = STREAM_URL_PARAM_DESCRIPTIONS.get(pk.lower(), "")
                display = pv
                note = ""
                if pk.lower() in {"expire", "volctime"}:
                    t = _fmt_unix_ts(pv)
                    if t:
                        try:
                            remain = int(pv) - int(datetime.now().timestamp())
                            note = f"（{t}，{'已过期' if remain < 0 else f'剩余 {max(remain // 86400, 0)} 天'}）"
                        except ValueError:
                            note = f"（{t}）"
                elif pk.lower() in {"sign", "volcsecret"} and len(pv) > 16:
                    display = pv[:16] + "…"
                parts.append(f"<tr><td><code>{escape_cell(pk)}</code></td>"
                             f"<td class='small' style='word-break:break-all'>{escape_cell(display)}{escape_cell(note)}</td>"
                             f"<td class='muted small'>{escape_cell(desc)}</td></tr>")
            parts.append("</tbody></table>")

    # ── 4.5 播放器 SDK 参数 ──
    pparams_raw = None
    fpp = su.get("flv_pull_url_params")
    if isinstance(fpp, dict):
        if default_q and isinstance(fpp.get(default_q), str):
            pparams_raw = fpp.get(default_q)
        else:
            pparams_raw = next((v for v in fpp.values() if isinstance(v, str) and v), None)
    elif isinstance(su.get("hls_pull_url_params"), str):
        pparams_raw = su.get("hls_pull_url_params")
    if pparams_raw:
        try:
            pparams = json.loads(pparams_raw)
        except json.JSONDecodeError:
            pparams = None
        if isinstance(pparams, dict):
            flat = flatten_dict(pparams)
            parts.append("<details style='margin-top:14px'><summary>🎛️ 播放器 SDK 参数（flv_pull_url_params，已解码）</summary>")
            parts.append("<table><thead><tr><th>参数</th><th>值</th><th>含义</th></tr></thead><tbody>")
            for k in sorted(flat.keys(), key=str.lower):
                desc = PLAYER_PARAM_DESCRIPTIONS.get(k.lower(), "")
                parts.append(f"<tr><td><code>{escape_cell(k)}</code></td><td>{escape_cell(str(flat[k]))}</td>"
                             f"<td class='muted small'>{escape_cell(desc)}</td></tr>")
            parts.append("</tbody></table></details>")

    # ── 5. 原始 JSON（stream_data 已解码）──
    su_display = json.loads(json.dumps(su, ensure_ascii=False, default=str))
    sdk_d = su_display.get("live_core_sdk_data")
    if isinstance(sdk_d, dict) and isinstance(sdk_d.get("pull_data"), dict) and sdata is not None:
        sdk_d["pull_data"] = dict(sdk_d["pull_data"])
        sdk_d["pull_data"]["stream_data"] = sdata
    parts.append("<details style='margin-top:14px'><summary>📦 原始 stream_url JSON（stream_data 已解码展开）</summary>")
    parts.append(f"<pre style='max-height:520px;overflow:auto'>"
                 f"{escape_cell(json.dumps(su_display, ensure_ascii=False, indent=2, default=str))}</pre></details>")
    parts.append("</div></details>")
    return True


# ═══════════════ WSL 报告打开辅助 ═══════════════

def _is_wsl():
    try:
        version_text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        return "microsoft" in version_text and "wsl" in version_text
    except OSError:
        return False


def _wsl_windows_path(path):
    """WSL 下返回 Windows 可访问的 UNC 路径，非 WSL 或失败返回 None。"""
    if not _is_wsl():
        return None
    try:
        import subprocess
        proc = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _print_report_path_hints(path):
    """打印报告路径提示（WSL 下额外输出 Windows 可访问路径）。"""
    win_path = _wsl_windows_path(path)
    if win_path:
        print(f"[*] Windows 访问路径: {win_path}")


def open_report_file(path):
    """打开报告文件：WSL 下转 Windows 路径用默认程序打开；否则用 webbrowser。"""
    path = Path(path)
    win_path = _wsl_windows_path(path)
    if win_path:
        print(f"  → Windows 访问路径: {win_path}")
        opened = False
        import subprocess
        for exe_name, args in (("explorer.exe", [win_path]),
                               ("/mnt/c/Windows/explorer.exe", [win_path]),
                               ("wslview", [str(path)])):
            exe = shutil.which(exe_name) or (exe_name if Path(exe_name).exists() else None)
            if not exe:
                continue
            try:
                subprocess.run([exe] + args, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = True
                break
            except (OSError, subprocess.SubprocessError):
                continue
        print("  → 已尝试用 Windows 默认浏览器打开报告" if opened
              else "  → 将上面的 Windows 路径粘贴到资源管理器/浏览器地址栏即可打开")
        return
    import webbrowser
    webbrowser.open(str(path))
    print("  → 已在浏览器中打开报告")


def _extract_field_name(key_path):
    """从字段路径中提取末端字段名"""
    last_part = key_path.rsplit(".", 1)[-1] if "." in key_path else key_path
    return last_part.split("[")[0]


def get_field_description(key_path):
    """从字段路径中提取末端字段名，返回中文说明"""
    field = _extract_field_name(key_path)
    entry = FIELD_DESCRIPTIONS.get(field, "")
    if isinstance(entry, dict):
        return entry.get("description", "")
    return entry if isinstance(entry, str) else ""


def get_value_description(key_path, value):
    """根据字段名和值返回中文解释"""
    field = _extract_field_name(key_path)
    entry = FIELD_DESCRIPTIONS.get(field)
    if not isinstance(entry, dict):
        return ""
    value_map = entry.get("values", {})
    if not value_map:
        return ""
    # 尝试多种匹配方式：原值、小写、字符串形式
    raw = str(value)
    for key in (raw, raw.lower(), str(value).lower()):
        if key in value_map:
            return value_map[key]
    # 布尔值匹配
    if isinstance(value, bool):
        bool_key = "true" if value else "false"
        if bool_key in value_map:
            return value_map[bool_key]
    return ""


# 用于 interesting_fields 过滤的精确字段名集合 (lowercase)
_INTERESTING_KEYS_FOR_FILTER = {k.lower() for k in FIELD_DESCRIPTIONS.keys()}


def parse_timestamp(filename):
    """从文件名提取 YYYYMMDD_HHMMSS 时间戳。"""
    name = os.path.basename(str(filename))
    matches = TIMESTAMP_PATTERN.findall(name)
    if not matches:
        # 回退：宽泛匹配任意 8+6 数字（兼容历史数据）
        matches = re.findall(r"(\d{8}_\d{6})", name)
    if not matches:
        return None
    # 从后往前找，优先选择年份合理的时间戳
    for ts_str in reversed(matches):
        try:
            dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if dt.year >= 2020:
                return dt
        except ValueError:
            continue
    # 兜底：尝试最后一个
    try:
        return datetime.strptime(matches[-1], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def parse_json_timestamp(value, fallback=None):
    """解析 JSON 中的时间戳字符串，失败时回退到 fallback。"""
    if isinstance(value, datetime):
        return value
    text = safe_text(value).strip()
    if not text:
        return fallback

    candidates = [
        "%Y%m%d_%H%M%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    if text.endswith("Z"):
        try:
            return datetime.fromisoformat(text[:-1] + "+00:00").replace(tzinfo=None)
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return fallback


def discover_json_results(output_dir, room_id=None):
    """发现采集脚本输出的 JSON 结果文件。"""
    results = {
        "flv": [], "reflow": [], "rtmp": [], "metadata": [], "dual": [],
        "ffprobe": [], "audio_track": [], "probe": [],
        "stream_url": [], "room_resolution": [], "playwright": [], "session_manifest": [],
        "av_sync": [],
        "lip_sync": [],
    }
    output_path = Path(output_dir)
    for json_file in sorted(output_path.glob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("跳过无效 JSON 文件 %s: %s", json_file.name, exc)
            continue
        except OSError as exc:
            logger.warning("无法读取文件 %s: %s", json_file.name, exc)
            continue

        rtype = safe_text(data.get("type", ""))
        if room_id and data.get("room_id") not in (None, "") and str(data.get("room_id", "")) != str(room_id):
            continue

        item = {
            "path": json_file,
            "file": json_file,
            "name": json_file.name,
            "timestamp": parse_json_timestamp(data.get("timestamp"), parse_timestamp(json_file.name)),
            "timestamp_str": safe_text(data.get("timestamp")) or (parse_timestamp(json_file.name) or datetime.min).strftime("%Y%m%d_%H%M%S"),
            "room_id": safe_text(data.get("room_id")) or None,
            "data": data,
            "source": "json",
        }
        if item["timestamp"] is None:
            continue
        if rtype == "session_manifest":
            results["session_manifest"].append(item)
            continue

        # dual_stream_analysis / metadata 均需优先匹配，避免误归类或遗漏
        if "dual_stream" in rtype:
            results["dual"].append(item)
        elif "metadata" in rtype:
            results["metadata"].append(item)
        elif "flv_analysis" in rtype:
            results["flv"].append(item)
        elif "ffprobe" in rtype:
            results["ffprobe"].append(item)
        elif "reflow" in rtype:
            results["reflow"].append(item)
        elif "rtmp" in rtype:
            results["rtmp"].append(item)
        elif "audio_track" in rtype or "audio_track_analysis" in json_file.name:
            results["audio_track"].append(item)
        elif "lip_sync" in rtype or json_file.name.startswith("lip_sync_"):
            results["lip_sync"].append(item)
        elif "av_sync" in rtype or json_file.name.startswith("av_sync_"):
            results["av_sync"].append(item)
        elif "_probe" in rtype or re.search(r'_(gift_list|room_info|ranklist)_probe\b', json_file.name):
            results["probe"].append(item)
        elif "stream_url" in rtype or re.search(r'stream_url_(struct|raw)', json_file.name):
            results["stream_url"].append(item)
        elif "room_resolution" in rtype or "room_resolution" in json_file.name:
            results["room_resolution"].append(item)
        elif "playwright" in rtype or "playwright" in json_file.name:
            results["playwright"].append(item)
    return results


def scan_captures(captures_dir):
    """扫描 captures 目录并按类型归类。"""
    captures_path = Path(captures_dir)
    result = {
        "reflow_json": [],
        "flv_samples": [],
        "flv_reports": [],
        "rtmp_pcaps": [],
        "rtmp_reports": [],
    }

    patterns = {
        "reflow_json": "reflow_*.json",
        "flv_samples": "flv_sample_*.flv",
        "flv_reports": "flv_report_*.txt",
        "rtmp_pcaps": "rtmp_capture_*.pcap",
        "rtmp_reports": "rtmp_report_*.txt",
    }

    for file_type, pattern in patterns.items():
        for path_str in sorted(glob.glob(str(captures_path / pattern))):
            path = Path(path_str)
            if file_type == "reflow_json" and path.name.endswith("_field_tree.json"):
                continue
            timestamp = parse_timestamp(path.name)
            if not timestamp:
                continue
            item = {
                "path": path,
                "name": path.name,
                "timestamp": timestamp,
                "timestamp_str": timestamp.strftime("%Y%m%d_%H%M%S"),
            }
            if file_type == "reflow_json":
                match = ROOM_TS_PATTERN.match(path.name)
                if match:
                    item["room_id"] = match.group(1)
            result[file_type].append(item)

    return result


def flatten_dict(data, prefix=""):
    """将嵌套 JSON 拍平成 path -> value。"""
    items = {}
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else key
            items.update(flatten_dict(value, child))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            child = f"{prefix}[{index}]" if prefix else f"[{index}]"
            items.update(flatten_dict(value, child))
    else:
        items[prefix or "<root>"] = data
    return items


def pick_first_non_empty(values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def safe_text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _truncate_display_value(value, max_len=160):
    """渲染时截断长值，不修改原始数据。"""
    text = safe_text(value)
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def _expand_dict_field(key_path, d, fields):
    """将 dict 展开为摘要 + 一层子字段。所有值均保留，空值/URL/嵌套结构通过 _expand_complex_field 递归处理。"""
    if not isinstance(d, dict):
        return
    n_keys = len(d)
    fields[key_path] = f"(dict, {n_keys} 个键)"
    for sub_key, sub_val in d.items():
        if not isinstance(sub_key, str):
            continue
        child_path = f"{key_path}.{sub_key}"
        if sub_val is None:
            fields[child_path] = "(null)"
        elif sub_val == "":
            fields[child_path] = "(空)"
        elif isinstance(sub_val, dict):
            fields[child_path] = f"(dict, {len(sub_val)} 个键)"
        elif isinstance(sub_val, list):
            fields[child_path] = f"(list, {len(sub_val)} 项)"
        else:
            fields[child_path] = sub_val


def _expand_list_field(key_path, lst, fields, max_preview=3):
    """将 list 展开为摘要 + 预览。"""
    if not isinstance(lst, list):
        return
    n_items = len(lst)
    fields[key_path] = f"(list, {n_items} 项)"
    if n_items == 0:
        return
    preview_items = []
    for item in lst[:max_preview]:
        text = safe_text(item)
        if len(text) > 100:
            text = text[:97] + "..."
        preview_items.append(text)
    fields[f"{key_path}.preview"] = ", ".join(preview_items)


def _expand_complex_field(key_path, value, fields):
    """统一处理所有值类型，保留全部字段。"""
    if value is None:
        fields[key_path] = "(null)"
        return
    if value == "":
        fields[key_path] = "(空)"
        return
    if isinstance(value, dict):
        _expand_dict_field(key_path, value, fields)
        return
    if isinstance(value, list):
        _expand_list_field(key_path, value, fields)
        return
    # 标量值（int/float/bool/str）直接存入
    fields[key_path] = value


def resolve_item_path(value):
    """从路径或条目字典中解析 Path。"""
    if isinstance(value, dict):
        candidate = value.get("file") or value.get("path")
        if candidate is None:
            raise ValueError("缺少 path/file 字段")
        return Path(candidate)
    return Path(value)


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = safe_text(value).strip().lower()
    if text in {"1", "true", "yes", "y", "是", "第三方"}:
        return True
    if text in {"0", "false", "no", "n", "否", "官方"}:
        return False
    return None


def infer_software(text):
    value = safe_text(text)
    if not value:
        return None
    for pattern, software in SOFTWARE_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE):
            return software
    return None


def infer_cdn(text):
    value = safe_text(text).lower()
    if not value:
        return None
    for keyword, name in CDN_PATTERNS:
        if keyword in value:
            return name
    return None


def extract_room_id_from_data(data, fallback_room_id=None):
    flattened = flatten_dict(data)
    candidates = []
    for path, value in flattened.items():
        if not isinstance(value, (str, int)):
            continue
        if any(key in path.lower() for key in ["room_id", "roomid", "id_str"]):
            text = str(value)
            if re.fullmatch(r"\d{6,}", text):
                candidates.append(text)
    return pick_first_non_empty(candidates + [fallback_room_id])


def load_reflow_data(json_path):
    """读取 reflow JSON 并提取关键信息。"""
    json_data = None
    if isinstance(json_path, dict):
        path = resolve_item_path(json_path)
        if "data" in json_path:
            json_data = json_path["data"]
    else:
        path = Path(json_path)
    result = {
        "path": path,
        "timestamp": parse_timestamp(path.name),
        "room_id": None,
        "status": None,
        "status_text": "未知",
        "stream_url": None,
        "interesting_fields": {},
        "provider_name": None,
        "software": None,
        "third_party": None,
        "video_codec": None,
        "error": None,
    }

    if json_data is not None:
        data = json_data
        result["timestamp"] = parse_json_timestamp(data.get("timestamp"), result["timestamp"])
    else:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            result["error"] = "文件不存在"
            return result
        except json.JSONDecodeError as exc:
            result["error"] = f"JSON 解析失败: {exc}"
            return result
        except OSError as exc:
            result["error"] = f"读取失败: {exc}"
            return result

    if data.get("type") == "reflow_monitor":
        field_tree = data.get("field_tree") if isinstance(data.get("field_tree"), dict) else {}
        key_changes = data.get("key_changes") if isinstance(data.get("key_changes"), dict) else {}
        result["room_id"] = safe_text(data.get("room_id")) or None
        # P1-1 fix: 兼容 analyze_room.py 写的 room_status 和直接 status 两种字段名
        result["status"] = data.get("status") or data.get("room_status")
        result["status_text"] = safe_text(data.get("status_text") or data.get("status")) or "未知"
        # 仅保留 data.room 下直接子字段的精确匹配，排除深层嵌套中的同名噪音
        for key_path, wrapper in field_tree.items():
            if not isinstance(wrapper, dict) or "value" not in wrapper:
                continue
            value = wrapper["value"]
            # 只匹配 "data.room.<field>" 三段路径（直接子字段）
            if not re.match(r'^data\.room\.[^.]+$', key_path, re.IGNORECASE):
                continue
            # 展开所有值（None/空/dict/list/URL 均保留）
            try:
                _expand_complex_field(key_path, value, result["interesting_fields"])
            except Exception:
                result["interesting_fields"][key_path] = safe_text(value)
        for key, value in key_changes.items():
            if value is not None and value != "":
                result["interesting_fields"][f"key_changes.{key}"] = value
        # 从 field_tree 中提取 stream_url dict 并展开关键子字段
        for key_path, wrapper in field_tree.items():
            if key_path.lower() == "data.room.stream_url" and isinstance(wrapper, dict):
                _extract_stream_url_info(wrapper.get("value"), result["interesting_fields"])
                break
        result["stream_url"] = pick_first_non_empty([
            ci_get(result["interesting_fields"], "stream_url"),
            ci_get(result["interesting_fields"], "push_url"),
            ci_get(result["interesting_fields"], "flv_pull_url"),
            ci_get(result["interesting_fields"], "rtmp_push_url"),
        ])
        # 从 interesting_fields 中按后缀匹配提取值
        def _suffix_get(fields, suffix):
            for k, v in fields.items():
                if k.lower().endswith('.' + suffix.lower()):
                    return v
            return None

        result["provider_name"] = pick_first_non_empty([
            ci_get(result["interesting_fields"], "provider_name"),
            ci_get(result["interesting_fields"], "stream_provider"),
            _suffix_get(result["interesting_fields"], "stream_url.cdn"),
        ])
        result["software"] = pick_first_non_empty([
            ci_get(result["interesting_fields"], "software"),
            ci_get(result["interesting_fields"], "encoder"),
            ci_get(result["interesting_fields"], "flashVer"),
        ])
        result["third_party"] = normalize_bool(pick_first_non_empty([
            ci_get(result["interesting_fields"], "third_party"),
            ci_get(result["interesting_fields"], "live_type_third_party"),
        ]))
        result["video_codec"] = pick_first_non_empty([
            ci_get(result["interesting_fields"], "video_codec"),
            ci_get(result["interesting_fields"], "videocodecid"),
            _suffix_get(result["interesting_fields"], "stream_url.video_codec"),
        ])
        # 分辨率从 stream_url 展开字段中提取
        _su_res = _suffix_get(result["interesting_fields"], "stream_url.resolution")
        if _su_res:
            result["resolution"] = safe_text(_su_res)
        result["raw"] = data
        return result

    match = ROOM_TS_PATTERN.match(path.name)
    fallback_room_id = match.group(1) if match else None
    result["room_id"] = extract_room_id_from_data(data, fallback_room_id)

    flattened = flatten_dict(data)

    for key_path, value in flattened.items():
        # 只匹配 data.room 直接子字段，排除深层嵌套同名噪音
        m = re.search(r'data\.room\.([a-z_0-9]+)$', key_path.lower())
        if not m:
            continue
        # 展开所有值（None/空/dict/list/URL 均保留）
        try:
            _expand_complex_field(key_path, value, result["interesting_fields"])
        except Exception:
            result["interesting_fields"][key_path] = safe_text(value)

    room = {}
    if isinstance(data, dict):
        room = (((data.get("data") or {}).get("room")) or {})
    if not isinstance(room, dict):
        room = {}

    # 从 stream_url dict 中展开关键子字段
    stream_url_obj = room.get("stream_url")
    if isinstance(stream_url_obj, dict):
        _extract_stream_url_info(stream_url_obj, result["interesting_fields"])

    status = pick_first_non_empty([
        room.get("status"),
        result["interesting_fields"].get("data.room.status"),
    ])
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        pass
    result["status"] = status
    if status == 2:
        result["status_text"] = "直播中"
    elif status == 4:
        result["status_text"] = "已结束"
    elif status is not None:
        result["status_text"] = f"未知({status})"
    else:
        result["status_text"] = "未知"
    # 拉流地址：取真正的 URL 字符串（默认画质 FLV），完整 dict 保留在 stream_url_obj
    _su_raw = room.get("stream_url")
    if isinstance(_su_raw, dict):
        result["stream_url_obj"] = _su_raw
        _ignored_label, _su_display_url = _pick_display_stream_url(_su_raw)
    else:
        _su_display_url = None
    result["stream_url"] = pick_first_non_empty([
        _su_display_url,
        _su_raw if isinstance(_su_raw, str) else None,
        result["interesting_fields"].get("data.room.stream_url"),
    ])
    result["provider_name"] = pick_first_non_empty([
        room.get("provider_name"),
        room.get("stream_provider"),
        result["interesting_fields"].get("data.room.provider_name"),
        result["interesting_fields"].get("data.room.stream_provider"),
        result["interesting_fields"].get("data.room.stream_url.cdn"),
    ])
    result["software"] = pick_first_non_empty([
        room.get("software"),
        result["interesting_fields"].get("data.room.software"),
    ])
    result["third_party"] = normalize_bool(pick_first_non_empty([
        room.get("third_party"),
        room.get("live_type_third_party"),
        result["interesting_fields"].get("data.room.third_party"),
        result["interesting_fields"].get("data.room.live_type_third_party"),
    ]))
    result["video_codec"] = pick_first_non_empty([
        room.get("video_codec"),
        result["interesting_fields"].get("data.room.video_codec"),
        result["interesting_fields"].get("data.room.stream_url.video_codec"),
    ])
    result["raw"] = data
    return result


def parse_key_value_line(line):
    match = re.match(r"^\s*([^:：]+)[:：]\s*(.*)$", line)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def parse_inline_json_block(lines, start_index, first_payload):
    payload_lines = [first_payload]
    balance = first_payload.count("{") - first_payload.count("}")
    balance += first_payload.count("[") - first_payload.count("]")
    index = start_index
    while balance > 0 and index + 1 < len(lines):
        index += 1
        payload_lines.append(lines[index].rstrip("\n"))
        chunk = lines[index]
        balance += chunk.count("{") - chunk.count("}")
        balance += chunk.count("[") - chunk.count("]")
    payload = "\n".join(payload_lines).strip()
    try:
        return json.loads(payload), index
    except json.JSONDecodeError:
        return payload, index


def recursive_collect_hints(data, bucket, prefix=""):
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if any(token in key.lower() for token in [
                "encoder", "creator", "software", "tool", "flashver",
                "codec", "width", "height", "framerate", "provider",
                "datarate", "samplerate",
            ]):
                bucket[path] = value
            recursive_collect_hints(value, bucket, path)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            recursive_collect_hints(value, bucket, f"{prefix}[{index}]" if prefix else f"[{index}]")


def parse_flv_report_from_text(txt_path):
    """解析 FLV 文本报告（兼容旧版输出）。"""
    path = resolve_item_path(txt_path)
    result = {
        "path": path,
        "timestamp": parse_timestamp(path.name),
        "room_id": None,
        "stream_url": None,
        "header": {},
        "tags": [],
        "script_data": [],
        "metadata": {},
        "fingerprint": {},
        "encoder_hints": {},
        "video_codec": None,
        "audio_codec": None,
        "resolution": None,
        "error": None,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        result["error"] = "文件不存在"
        return result
    except OSError as exc:
        result["error"] = f"读取失败: {exc}"
        return result

    lines = text.splitlines()
    current_section = None
    current_tag = None
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("直播流 URL:"):
            result["stream_url"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("房间号:"):
            result["room_id"] = stripped.split(":", 1)[1].strip()
        elif stripped == "FLV Header:":
            current_section = "header"
        elif re.match(r"^Tag\s+#\d+:", stripped):
            current_section = "tag"
            current_tag = {
                "title": stripped,
                "video_codec": None,
                "audio_codec": None,
                "parsed": None,
            }
            result["tags"].append(current_tag)
        elif current_section == "header" and line.startswith("  "):
            key, value = parse_key_value_line(line)
            if key:
                result["header"][key] = value
        elif current_section == "tag" and current_tag and line.startswith("  "):
            key, value = parse_key_value_line(line)
            if key == "视频编码":
                current_tag["video_codec"] = value
                result["video_codec"] = result["video_codec"] or value
            elif key == "音频编码":
                current_tag["audio_codec"] = value
                result["audio_codec"] = result["audio_codec"] or value
            elif key == "Script Data":
                parsed, index = parse_inline_json_block(lines, index, value)
                current_tag["parsed"] = parsed
                result["script_data"].append(parsed)
        index += 1

    for script in result["script_data"]:
        if isinstance(script, dict) and not result["metadata"]:
            result["metadata"] = script
        recursive_collect_hints(script, result["encoder_hints"])

    width = ci_get(result["encoder_hints"], "width")
    height = ci_get(result["encoder_hints"], "height")
    if width and height:
        result["resolution"] = f"{width}x{height}"

    return result


def load_flv_report(path_or_data):
    """加载 FLV 分析结果（优先 JSON，回退文本）。"""
    if isinstance(path_or_data, dict) and "data" in path_or_data:
        data = path_or_data["data"]
        result = {
            "path": Path(path_or_data["file"]),
            "timestamp": parse_json_timestamp(data.get("timestamp"), parse_timestamp(path_or_data["file"].name)),
            "room_id": data.get("room_id", ""),
            "stream_url": data.get("stream_url", ""),
            "header": data.get("header", {}) if isinstance(data.get("header"), dict) else {},
            "tags": [],
            "script_data": [],
            "metadata": data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {},
            "fingerprint": data.get("fingerprint", {}) if isinstance(data.get("fingerprint"), dict) else {},
            "encoder_hints": data.get("encoder_hints", {}) if isinstance(data.get("encoder_hints"), dict) else {},
            "resolution": data.get("resolution", ""),
            "video_codec": data.get("video_codec", "unknown"),
            "audio_codec": data.get("audio_codec", "unknown"),
            "error": None,
        }
        if not result["encoder_hints"] and result["metadata"]:
            recursive_collect_hints(result["metadata"], result["encoder_hints"])
        # P1-4 fix: 兼容 flv_metadata_analysis 的字段结构
        # 如果没有 fingerprint 但有 push_software，构建一个兼容的 fingerprint
        if not result["fingerprint"] and isinstance(data.get("push_software"), dict):
            ps = data["push_software"]
            result["fingerprint"] = {
                "push_software": ps.get("name") or ps.get("push_software") or "unknown",
                "confidence": ps.get("confidence", "low"),
            }
        # 如果没有 video_codec/audio_codec，从 encoder_hints 尝试提取
        if result["video_codec"] == "unknown" and result["encoder_hints"]:
            result["video_codec"] = ci_get(result["encoder_hints"], "videocodecid") or "unknown"
        if result["audio_codec"] == "unknown" and result["encoder_hints"]:
            result["audio_codec"] = ci_get(result["encoder_hints"], "audiocodecid") or "unknown"
        if not result["resolution"]:
            width = ci_get(result["encoder_hints"], "width")
            height = ci_get(result["encoder_hints"], "height")
            if width and height:
                result["resolution"] = f"{width}x{height}"
        return result

    return parse_flv_report_from_text(path_or_data)


def parse_rtmp_report_from_text(txt_path):
    """解析 RTMP 文本报告（兼容旧版输出）。"""
    path = resolve_item_path(txt_path)
    result = {
        "path": path,
        "timestamp": parse_timestamp(path.name),
        "pcap_path": None,
        "interface": None,
        "connect_fields": {},
        "findings": [],
        "flashver_candidates": [],
        "software_candidates": [],
        "push_software": None,
        "confidence": None,
        "error": None,
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        result["error"] = "文件不存在"
        return result
    except OSError as exc:
        result["error"] = f"读取失败: {exc}"
        return result

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("RTMP 分析报告 -"):
            result["pcap_path"] = stripped.split("-", 1)[1].strip()
            continue
        if set(stripped) == {"="}:
            continue
        result["findings"].append(stripped)
        flashver_match = re.search(r"flashver[^\w]*([\w./ -]{3,80})", stripped, re.IGNORECASE)
        if flashver_match:
            result["flashver_candidates"].append(flashver_match.group(1).strip())
        software = infer_software(stripped)
        if software:
            result["software_candidates"].append(software)

    result["flashver_candidates"] = sorted(set(result["flashver_candidates"]))
    result["software_candidates"] = sorted(set(result["software_candidates"]))
    return result


def load_rtmp_report(path_or_data):
    """加载 RTMP 分析结果（优先 JSON，回退文本）。"""
    if isinstance(path_or_data, dict) and "data" in path_or_data:
        data = path_or_data["data"]
        connect_fields = data.get("connect_fields", {}) if isinstance(data.get("connect_fields"), dict) else {}
        findings = []
        software_findings = data.get("software_findings", [])
        if isinstance(software_findings, list):
            findings.extend(safe_text(item) for item in software_findings if safe_text(item))
        elif software_findings:
            findings.append(safe_text(software_findings))
        for key, value in connect_fields.items():
            findings.append(f"{key}: {safe_text(value)}")

        result = {
            "path": Path(path_or_data["file"]),
            "timestamp": parse_json_timestamp(data.get("timestamp"), parse_timestamp(path_or_data["file"].name)),
            "pcap_path": data.get("pcap_path"),
            "interface": data.get("interface"),
            "connect_fields": connect_fields,
            "findings": findings,
            "flashver_candidates": [],
            "software_candidates": [],
            "push_software": data.get("push_software"),
            "confidence": data.get("confidence"),
            "error": None,
        }
        flashver = pick_first_non_empty([
            ci_get(connect_fields, "flashVer"),
            ci_get(connect_fields, "flashver"),
        ])
        if flashver:
            result["flashver_candidates"] = [safe_text(flashver)]
        candidates = [infer_software(item) for item in result["findings"]]
        if result["push_software"]:
            candidates.append(safe_text(result["push_software"]))
        result["software_candidates"] = sorted({candidate for candidate in candidates if candidate})
        return result

    return parse_rtmp_report_from_text(path_or_data)


def load_dual_data(path_or_data):
    """加载双流检测结果。"""
    if isinstance(path_or_data, dict) and "data" in path_or_data:
        data = path_or_data["data"]
        path = resolve_item_path(path_or_data)
    else:
        path = resolve_item_path(path_or_data)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            return {"path": path, "error": str(exc), "verdict": "none", "score": 0, "signals": [], "suppressors": []}

    return {
        "path": path,
        "timestamp": parse_json_timestamp(data.get("timestamp"), parse_timestamp(path.name)),
        "room_id": safe_text(data.get("room_id")) or None,
        "web_room_id": safe_text(data.get("web_room_id")) or None,
        "verdict": data.get("verdict", "none"),
        "score": data.get("score", 0),
        "signals": data.get("signals", []),
        "suppressors": data.get("suppressors", []),
        "multi_quality": data.get("multi_quality", {}),
        "temporal": data.get("temporal", {}),
        "consistency": data.get("consistency", {}),
        "error": None,
    }


def load_ffprobe_data(path_or_data):
    """加载 ffprobe 探测结果。"""
    if isinstance(path_or_data, dict) and "data" in path_or_data:
        data = path_or_data["data"]
        path = resolve_item_path(path_or_data)
    else:
        path = resolve_item_path(path_or_data)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            return {"path": path, "error": str(exc)}

    summary = data.get("summary") or data
    return {
        "path": path,
        "timestamp": parse_json_timestamp(data.get("timestamp"), parse_timestamp(path.name)),
        "stream_url": data.get("stream_url"),
        "push_software": summary.get("push_software"),
        "push_protocol": summary.get("push_protocol"),
        "hardware_encode": summary.get("hardware_encode"),
        "device_info": summary.get("device_info"),
        "streams": summary.get("streams", []),
        "format_tags": summary.get("format_tags", {}),
        "error": None,
    }


def find_nearest_session(sessions, item_timestamp, tolerance_seconds):
    chosen = None
    chosen_delta = None
    for session in sessions:
        start = session["start_time"] - timedelta(seconds=tolerance_seconds)
        end = session["end_time"] + timedelta(seconds=tolerance_seconds)
        if start <= item_timestamp <= end:
            center = session["start_time"] + (session["end_time"] - session["start_time"]) / 2
            delta = abs((item_timestamp - center).total_seconds())
            if chosen is None or delta < chosen_delta:
                chosen = session
                chosen_delta = delta
    return chosen


def correlate_by_time(files_data, tolerance_seconds=300):
    """按时间邻近性把多类抓取结果合并为会话。"""
    items = []
    type_mapping = {
        "reflow_json": "reflow_files",
        "flv_samples": "flv_samples",
        "flv_reports": "flv_reports",
        "rtmp_pcaps": "rtmp_pcaps",
        "rtmp_reports": "rtmp_reports",
        "dual_reports": "dual_files",
        "ffprobe_files": "ffprobe_files",
        "audio_track": "audio_track_files",
        "av_sync": "av_sync_files",
        "lip_sync": "lip_sync_files",
        "probe_files": "probe_files",
        "stream_url": "stream_url_files",
        "room_resolution": "room_resolution_files",
        "playwright": "playwright_files",
        "session_manifest": "session_manifest_files",
    }
    for file_type, target_key in type_mapping.items():
        for entry in files_data.get(file_type, []):
            items.append({
                "category": target_key,
                "timestamp": entry["timestamp"],
                "entry": entry,
            })

    items.sort(key=lambda item: item["timestamp"])
    sessions = []
    for item in items:
        session = find_nearest_session(sessions, item["timestamp"], tolerance_seconds)
        if session is None:
            session = {
                "session_id": f"session_{len(sessions) + 1:03d}",
                "start_time": item["timestamp"],
                "end_time": item["timestamp"],
                "timestamps": [item["timestamp"]],
                "reflow_files": [],
                "flv_samples": [],
                "flv_reports": [],
                "rtmp_pcaps": [],
                "rtmp_reports": [],
                "dual_files": [],
                "ffprobe_files": [],
                "audio_track_files": [],
                "av_sync_files": [],
                "lip_sync_files": [],
                "probe_files": [],
                "stream_url_files": [],
                "room_resolution_files": [],
                "playwright_files": [],
                "session_manifest_files": [],
                "room_ids": set(),
            }
            sessions.append(session)
        session[item["category"]].append(item["entry"])
        session["timestamps"].append(item["timestamp"])
        session["start_time"] = min(session["start_time"], item["timestamp"])
        session["end_time"] = max(session["end_time"], item["timestamp"])
        room_id = item["entry"].get("room_id")
        if room_id:
            session["room_ids"].add(room_id)

    for session in sessions:
        session["timestamps"] = sorted(set(session["timestamps"]))
        session["room_ids"] = sorted(session["room_ids"])

    return sessions


def append_evidence(evidence_list, source, value, interpretation, level):
    if value is None:
        return
    text_value = safe_text(value)
    # 去重：相同 source + value 的证据不重复添加
    for existing in evidence_list:
        if existing.get("source") == source and existing.get("value") == text_value:
            return
    evidence_list.append({
        "source": source,
        "value": text_value,
        "interpretation": interpretation,
        "level": level,
    })


def generate_push_conclusion(session_data):
    """综合 RTMP / FLV / reflow 证据生成推流端识别结论。"""
    evidence = []
    scores = {}
    third_party = None
    cdn = None
    resolution = None
    codec = None

    def add_candidate(name, weight):
        if not name:
            return
        scores[name] = scores.get(name, 0) + weight

    for rtmp in session_data.get("rtmp_data", []):
        if rtmp.get("push_software"):
            push_software = infer_software(rtmp.get("push_software")) or safe_text(rtmp.get("push_software"))
            append_evidence(evidence, "RTMP JSON push_software", rtmp.get("push_software"), f"采集脚本已识别推流软件：{push_software}", "高")
            add_candidate(push_software, 12)
        if isinstance(rtmp.get("connect_fields"), dict):
            flashver = pick_first_non_empty([
                ci_get(rtmp["connect_fields"], "flashVer"),
                ci_get(rtmp["connect_fields"], "flashver"),
            ])
            if flashver:
                software = infer_software(flashver)
                if software:
                    append_evidence(evidence, "RTMP connect flashVer", flashver, f"connect 字段指向 {software}", "高")
                    add_candidate(software, 10)
        for finding in rtmp.get("findings", []):
            software = infer_software(finding)
            if software:
                append_evidence(evidence, "RTMP flashVer / 原始抓包", finding, f"识别到推流软件特征：{software}", "高")
                add_candidate(software, 10)
            elif "flashver" in finding.lower():
                append_evidence(evidence, "RTMP flashVer / 原始抓包", finding, "存在 flashVer 线索，但软件名未完全命中规则", "高")
        for flashver in rtmp.get("flashver_candidates", []):
            software = infer_software(flashver)
            if software:
                add_candidate(software, 10)

    for flv in session_data.get("flv_data", []):
        if flv.get("video_codec"):
            codec = codec or flv.get("video_codec")
        if flv.get("resolution"):
            resolution = resolution or flv.get("resolution")
        metadata = flv.get("metadata", {}) if isinstance(flv.get("metadata"), dict) else {}
        if metadata:
            for lookup_key in ["encoder", "software", "creator", "metadatacreator", "flashVer", "provider_name", "stream_url"]:
                meta_value = ci_get(metadata, lookup_key)
                software = infer_software(meta_value)
                if software:
                    append_evidence(evidence, f"FLV metadata ({lookup_key})", meta_value, f"metadata 字段指向 {software}", "中")
                    add_candidate(software, 6)
                if lookup_key in {"provider_name", "stream_url"} and not cdn:
                    cdn = infer_cdn(meta_value) or cdn
        for key, value in flv.get("encoder_hints", {}).items():
            software = infer_software(value)
            if software:
                append_evidence(evidence, f"FLV onMetaData ({key})", value, f"onMetaData 编码器字段指向 {software}", "中")
                add_candidate(software, 6)
            if key.lower().endswith("provider") and not cdn:
                cdn = infer_cdn(value)
        stream_url = flv.get("stream_url")
        if stream_url:
            inferred_cdn = infer_cdn(stream_url)
            if inferred_cdn and not cdn:
                cdn = inferred_cdn
            if "third" in stream_url.lower():
                third_party = True
                append_evidence(evidence, "FLV 拉流 URL", stream_url, "URL 含 third，倾向第三方推流链路", "低")

    # ffprobe 证据
    for fp in session_data.get("ffprobe_data", []):
        if fp.get("error"):
            continue
        for s in fp.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width"), s.get("height")
                if w and h:
                    resolution = resolution or f"{w}x{h}"
                    append_evidence(evidence, "ffprobe 视频流", f"{w}x{h} {s.get('codec_name', '')}", f"分辨率 {w}x{h}", "中")
                vcodec = s.get("codec_name")
                if vcodec:
                    codec = codec or vcodec
        fp_push = fp.get("push_software")
        if fp_push:
            software = infer_software(fp_push)
            if software:
                append_evidence(evidence, "ffprobe Encoder tag", fp_push, f"ffprobe 检测到推流软件：{software}", "高")
                add_candidate(software, 10)
            else:
                append_evidence(evidence, "ffprobe Encoder tag", fp_push, f"ffprobe 检测到 Encoder 字段（未匹配已知软件）", "中")
                add_candidate(fp_push[:40], 4)
        fp_proto = fp.get("push_protocol")
        if fp_proto:
            append_evidence(evidence, "ffprobe push_protocol", fp_proto, f"推流协议: {fp_proto}", "中")
        dev = fp.get("device_info")
        if dev and any(dev.values()):
            dev_str = ", ".join(f"{k}={v}" for k, v in dev.items() if v)
            append_evidence(evidence, "ffprobe 设备信息", dev_str, f"推流设备: {dev_str}", "中")
        if fp.get("hardware_encode") is not None:
            append_evidence(evidence, "ffprobe 硬件编码", str(fp["hardware_encode"]),
                            f"硬件编码: {'是' if fp['hardware_encode'] else '否（软编码）'}", "中")
        tags = fp.get("format_tags", {})
        for tag_key in ["default_bitrate", "min_bitrate", "max_bitrate"]:
            if tags.get(tag_key):
                append_evidence(evidence, f"ffprobe {tag_key}", tags[tag_key],
                                f"码率参数 {tag_key}: {tags[tag_key]} kbps", "低")

    # 用于从 reflow interesting_fields 中提取推流环境关键字的辅助
    def _get_ifield(interesting_fields, field_name):
        """从 interesting_fields 中查找指定字段名的值（支持后缀匹配，大小写不敏感）"""
        target = field_name.lower()
        for k, v in interesting_fields.items():
            # 精确匹配末端字段名
            if _extract_field_name(k).lower() == target:
                return v
            # 后缀匹配（如 stream_url.resolution 匹配 data.room.stream_url.resolution）
            if k.lower().endswith('.' + target):
                return v
        return None

    # app_id → 推流渠道映射（从 APP_ID_MAP 派生，string key 版本）
    _APP_ID_CHANNELS = {str(k): v for k, v in APP_ID_MAP.items()}

    # os_type → 环境映射
    _OS_TYPE_ENV = {
        0: "未上报", 1: "iOS 设备", 2: "Android 设备",
        3: "Windows PC", 4: "macOS", 5: "Linux",
    }

    for reflow in session_data.get("reflow_data", []):
        ifields = reflow.get("interesting_fields", {})
        if reflow.get("video_codec"):
            codec = codec or safe_text(reflow.get("video_codec"))
        if reflow.get("provider_name") and not cdn:
            cdn = infer_cdn(reflow.get("provider_name")) or safe_text(reflow.get("provider_name"))
        if reflow.get("third_party") is not None and third_party is None:
            third_party = reflow.get("third_party")
        if reflow.get("software"):
            software = infer_software(reflow.get("software")) or safe_text(reflow.get("software"))
            append_evidence(evidence, "reflow API software 字段", reflow.get("software"), f"接口返回软件线索：{software}", "中")
            add_candidate(software, 4)
        if reflow.get("provider_name"):
            append_evidence(evidence, "reflow API provider_name", reflow.get("provider_name"), "可辅助判断 CDN / 服务商", "低")

        # ── 推流渠道识别 ──
        app_id_val = _get_ifield(ifields, "app_id")
        if app_id_val is not None:
            app_id_str = str(app_id_val)
            channel = _APP_ID_CHANNELS.get(app_id_str)
            if channel:
                append_evidence(evidence, "reflow API app_id", app_id_val,
                                f"开播渠道：{channel}（app_id={app_id_str}）", "高")
            else:
                append_evidence(evidence, "reflow API app_id", app_id_val,
                                f"开播渠道：未知应用（app_id={app_id_str}）", "中")
            # 直播伴侣 / PC 端开播 → 可能第三方推流
            if app_id_str == "2079" and third_party is None:
                third_party = False  # 直播伴侣是官方PC工具，不算第三方
                append_evidence(evidence, "reflow API app_id", app_id_val, "直播伴侣为官方 PC 推流工具", "中")

        # ── 推流环境识别 ──
        os_val = _get_ifield(ifields, "os_type")
        if os_val is not None:
            try:
                os_int = int(os_val)
            except (TypeError, ValueError):
                os_int = None
            env_desc = _OS_TYPE_ENV.get(os_int, f"未知({os_val})")
            append_evidence(evidence, "reflow API os_type", os_val,
                            f"推流环境：{env_desc}", "高")
            # PC 端开播 + 未识别软件 → 猜测直播伴侣或第三方
            if os_int in (3, 4, 5):
                add_candidate("PC 推流", 2)

        # ── 平台来源 ──
        platform_val = _get_ifield(ifields, "live_platform_source")
        if platform_val is not None and str(platform_val).strip() not in ("", "None", "null", "(空)", "(null)"):
            _PLATFORM_MAP = {0: "抖音 App", 1: "直播伴侣", 2: "第三方工具"}
            try:
                p_int = int(platform_val)
            except (TypeError, ValueError):
                p_int = None
            p_desc = _PLATFORM_MAP.get(p_int, f"未知({platform_val})")
            append_evidence(evidence, "reflow API live_platform_source", platform_val,
                            f"开播来源：{p_desc}", "高")
            if p_int == 2 and third_party is None:
                third_party = True
        else:
            append_evidence(evidence, "reflow API live_platform_source", platform_val or "(空)",
                            "开播来源：未上报", "低")

        # ── 直播类型组合判断 ──
        is_normal = _get_ifield(ifields, "live_type_normal")
        is_audio = _get_ifield(ifields, "live_type_audio")
        is_linkmic = _get_ifield(ifields, "live_type_linkmic")
        is_vs = _get_ifield(ifields, "live_type_vs_live")
        is_sandbox = _get_ifield(ifields, "live_type_sandbox")
        is_screenshot = _get_ifield(ifields, "live_type_screenshot")

        type_parts = []
        if normalize_bool(is_normal):
            type_parts.append("常规手机直播")
        if normalize_bool(is_audio):
            type_parts.append("语音直播")
        if normalize_bool(is_linkmic):
            type_parts.append("连麦中")
        if normalize_bool(is_vs):
            type_parts.append("PK 对战")
        if normalize_bool(is_sandbox):
            type_parts.append("沙盒/测试环境")
        if normalize_bool(is_screenshot):
            type_parts.append("截图直播")
        if type_parts:
            append_evidence(evidence, "reflow API live_type_*", " / ".join(type_parts),
                            f"直播类型：{' + '.join(type_parts)}", "中")

        # ── 美颜滤镜 ──
        use_filter_val = _get_ifield(ifields, "use_filter")
        if use_filter_val is not None and normalize_bool(use_filter_val):
            append_evidence(evidence, "reflow API use_filter", use_filter_val,
                            "启用了美颜滤镜（通常为手机端推流）", "低")

        # ── stream_url 展开字段 ──
        su_resolution = _get_ifield(ifields, "stream_url.resolution")
        if su_resolution:
            resolution = resolution or safe_text(su_resolution)
            append_evidence(evidence, "reflow stream_url.extra", su_resolution,
                            f"推流分辨率：{su_resolution}", "中")
        su_codec = _get_ifield(ifields, "stream_url.video_codec")
        if su_codec:
            codec = codec or safe_text(su_codec)
            append_evidence(evidence, "reflow stream_url params", su_codec,
                            f"视频编码：{su_codec}", "中")
        su_cdn = _get_ifield(ifields, "stream_url.cdn")
        if su_cdn and not cdn:
            cdn = safe_text(su_cdn)
            append_evidence(evidence, "reflow stream_url 域名", su_cdn,
                            f"CDN 推断：{cdn}", "中")
        su_quality = _get_ifield(ifields, "stream_url.default_quality")
        if su_quality:
            append_evidence(evidence, "reflow stream_url.default_resolution", su_quality,
                            f"默认清晰度：{su_quality}", "低")
        su_hw = _get_ifield(ifields, "stream_url.hardware_encode")
        if su_hw:
            append_evidence(evidence, "reflow stream_url.hardware_encode", su_hw,
                            f"硬件编码：{su_hw}", "低")

        for key, value in ifields.items():
            # 跳过 URL、图片、大 JSON 等
            if isinstance(value, str) and value.startswith(("http://", "https://", "sslocal://")):
                continue
            if isinstance(value, str) and len(value) > 500:
                continue
            if isinstance(value, (dict, list)):
                continue
            software = infer_software(value)
            if software:
                append_evidence(evidence, f"reflow API ({key})", value, f"接口字段包含软件特征：{software}", "低")
                add_candidate(software, 3)
            if third_party is None and "third_party" in key.lower():
                third_party = normalize_bool(value)
            if not cdn and any(token in key.lower() for token in ["provider", "stream_url", "push_url"]):
                cdn = infer_cdn(value) or cdn

    software = None
    if scores:
        software = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]

    # 回退：所有规则均未命中时，从 FLV encoder_hints 提取原始值（大小写不敏感 + onMetaData. 前缀）
    if not software:
        _fallback_keys = ["encoder", "ENCONDERVERSION", "software",
                          "metadatacreator", "creator", "flashVer"]
        for flv in session_data.get("flv_data", []):
            eh = flv.get("encoder_hints", {})
            for _fk in _fallback_keys:
                _fv = ci_get(eh, _fk)
                if _fv and str(_fv).strip():
                    software = str(_fv).strip()
                    append_evidence(evidence, f"FLV onMetaData ({_fk})", _fv,
                                    "未命中已知规则，使用原始 encoder 字段值作为推测", "低")
                    break
            if software:
                break

    high_hits = sum(1 for item in evidence if item["level"] == "高")
    medium_hits = sum(1 for item in evidence if item["level"] == "中")
    if high_hits >= 1 or (software and scores.get(software, 0) >= 10):
        confidence = "高"
    elif medium_hits >= 1 or (software and scores.get(software, 0) >= 6):
        confidence = "中"
    else:
        confidence = "低"

    if third_party is None:
        third_party = False if cdn in {"抖音", "字节 CDN", "字节跳动", "TikTok Live / 字节直播"} else None

    return {
        "software": software or "未能明确识别",
        "confidence": confidence,
        "evidence_list": evidence,
        "third_party": third_party,
        "cdn": cdn or "未知",
        "resolution": resolution or "未知",
        "codec": codec or "未知",
    }


def format_bool_cn(value):
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def session_title(session):
    room = session.get("primary_room_id") or "未识别房间"
    start = session["start_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("start_time") else "未知时间"
    return f"{session['session_id']} | room_id={room} | {start}"


def generate_dual_conclusion(dual_data_list):
    """从双流检测结果中提取最高风险结论。"""
    if not dual_data_list:
        return {"verdict": "none", "score": 0, "signals": [], "suppressors": [], "source": None}

    verdict_order = {"high": 3, "medium": 2, "low": 1, "none": 0}
    best = max(
        dual_data_list,
        key=lambda d: verdict_order.get(d.get("verdict", "none"), 0),
    )
    return {
        "verdict": best.get("verdict", "none"),
        "score": best.get("score", 0),
        "signals": best.get("signals", []),
        "suppressors": best.get("suppressors", []),
        "source": str(best.get("path", "")),
    }


def _extract_probe_fields(probe_type, raw):
    """从探针原始数据中提取关键字段用于报告展示。"""
    fields = []
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if probe_type == "gift_list_probe":
        gifts = data.get("gifts", [])
        fields.append(("礼物总数", len(gifts)))
        for i, g in enumerate(gifts[:10]):
            name = g.get("name", "?")
            diamond = g.get("diamond_count", g.get("diamond", "?"))
            fields.append((f"  礼物[{i}]", f"{name} ({diamond} 钻石)"))
        if len(gifts) > 10:
            fields.append(("  ...", f"共 {len(gifts)} 项"))
    elif probe_type == "room_info_probe":
        for key in ("room_id", "title", "status", "user_count", "create_time",
                     "stream_url", "owner"):
            val = data.get(key)
            if val is not None and val != "":
                if isinstance(val, dict):
                    fields.append((key, f"(dict, {len(val)} 键)"))
                else:
                    fields.append((key, val))
    elif probe_type == "ranklist_probe":
        ranks = data.get("ranks", [])
        fields.append(("排行榜人数", len(ranks)))
        for i, r in enumerate(ranks[:10]):
            user = r.get("user", {})
            nickname = user.get("nickname", "?")
            score = r.get("score", "?")
            fields.append((f"  #{i+1}", f"{nickname} (score={score})"))
        if len(ranks) > 10:
            fields.append(("  ...", f"共 {len(ranks)} 项"))
    else:
        for k, v in raw.items():
            if k in ("status_code", "status_msg"):
                fields.append((k, v))
    return fields


def generate_text_report(sessions, output_path):
    """生成文本报告。"""
    lines = []
    lines.append("抖音直播流分析 — 统一报告")
    lines.append("=" * 72)
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"会话数量: {len(sessions)}")
    lines.append("")

    for index, session in enumerate(sessions, 1):
        conclusion = session.get("push_conclusion", {})
        lines.append("-" * 72)
        lines.append(f"[{index}] {session_title(session)}")
        lines.append("-" * 72)
        lines.append(f"时间范围: {session['start_time']} ~ {session['end_time']}")
        lines.append(f"关联文件: reflow={len(session['reflow_files'])} / flv_report={len(session['flv_reports'])} / rtmp_report={len(session['rtmp_reports'])}")
        lines.append("")
        lines.append("推流端识别结论")
        lines.append(f"  软件判断: {conclusion.get('software', '未知')}")
        lines.append(f"  置信度: {conclusion.get('confidence', '低')}")
        lines.append(f"  第三方推流: {format_bool_cn(conclusion.get('third_party'))}")
        lines.append(f"  CDN / 服务商: {conclusion.get('cdn', '未知')}")
        lines.append(f"  分辨率: {conclusion.get('resolution', '未知')}")
        lines.append(f"  编码: {conclusion.get('codec', '未知')}")
        # 补充：从证据链中提取开播渠道和推流环境摘要
        channel_summary = []
        env_summary = []
        source_summary = []
        for ev in (conclusion.get("evidence_list") or []):
            interp = ev.get("interpretation", "")
            if "开播渠道" in interp:
                channel_summary.append(interp)
            if "推流环境" in interp:
                env_summary.append(interp)
            if "开播来源" in interp:
                source_summary.append(interp)
        if channel_summary:
            lines.append(f"  开播渠道: {'; '.join(channel_summary)}")
        if source_summary:
            lines.append(f"  平台来源: {'; '.join(source_summary)}")
        if env_summary:
            lines.append(f"  推流环境: {'; '.join(env_summary)}")
        lines.append("")
        dual_conclusion = session.get("dual_conclusion", {})
        if dual_conclusion and dual_conclusion.get("verdict", "none") != "none":
            verdict_map = {"none": "未发现异常", "low": "低风险", "medium": "中风险", "high": "高风险"}
            lines.append("双流异常检测")
            lines.append(f"  风险等级: {verdict_map.get(dual_conclusion.get('verdict', 'none'), '未知')}")
            lines.append(f"  风险评分: {dual_conclusion.get('score', 0)}/100")
            signals = dual_conclusion.get("signals", [])
            if signals:
                lines.append(f"  异常信号 ({len(signals)} 个):")
                for sig in signals[:10]:
                    lines.append(f"    - [{sig.get('strength', '?')}] {sig.get('description', '')}")
            suppressors = dual_conclusion.get("suppressors", [])
            if suppressors:
                lines.append(f"  抑制因素 ({len(suppressors)} 个):")
                for sup in suppressors[:5]:
                    lines.append(f"    - {sup.get('description', '')}")
            lines.append("")
        lines.append("证据链")
        if conclusion.get("evidence_list"):
            for evidence in conclusion["evidence_list"]:
                lines.append(f"  - 来源: {evidence['source']}")
                lines.append(f"    原值: {evidence['value']}")
                lines.append(f"    解读: {evidence['interpretation']}")
                lines.append(f"    权级: {evidence['level']}")
        else:
            lines.append("  - 暂无足够证据")
        lines.append("")

        if session.get("reflow_data"):
            lines.append("reflow 数据")
            for reflow in session["reflow_data"]:
                lines.append(f"  文件: {reflow['path'].name}")
                lines.append(f"  room_id: {reflow.get('room_id') or '未知'} | 状态: {reflow.get('status_text', '未知')}")
                lines.append(f"  provider_name: {safe_text(reflow.get('provider_name')) or '无'}")
                lines.append(f"  software: {safe_text(reflow.get('software')) or '无'}")
                if reflow.get("error"):
                    lines.append(f"  错误: {reflow['error']}")
                if reflow.get("interesting_fields"):
                    lines.append("  关键字段:")
                    for key, value in sorted(reflow["interesting_fields"].items()):
                        field_desc = get_field_description(key)
                        value_desc = get_value_description(key, value)
                        parts = []
                        if field_desc:
                            parts.append(field_desc)
                        if value_desc:
                            parts.append(f"→ {value_desc}")
                        desc_str = f"  # {' | '.join(parts)}" if parts else ""
                        lines.append(f"    {key} = {_truncate_display_value(value)}{desc_str}")
                lines.append("")

        if session.get("flv_data"):
            lines.append("FLV 数据")
            for flv in session["flv_data"]:
                lines.append(f"  文件: {flv['path'].name}")
                lines.append(f"  stream_url: {flv.get('stream_url') or '未知'}")
                lines.append(f"  video_codec: {flv.get('video_codec') or '未知'} | audio_codec: {flv.get('audio_codec') or '未知'}")
                lines.append(f"  resolution: {flv.get('resolution') or '未知'}")
                if flv.get("error"):
                    lines.append(f"  错误: {flv['error']}")
                if flv.get("encoder_hints"):
                    lines.append("  编码器线索:")
                    _flv_sensitive = {"signResult", "signinfo", "signvalue", "signversion",
                                     "fp_coderate", "fp_amode", "fp_vmode", "fp_nb",
                                     "e_id", "h_id", "s_id", "p_id", "link_info"}
                    for key, value in sorted(flv["encoder_hints"].items()):
                        if key in _flv_sensitive:
                            continue
                        lines.append(f"    {key} = {safe_text(value)}")
                else:
                    lines.append("  编码器线索: 无（onMetaData 中未找到 encoder/flashVer/metadatacreator 等字段）")
                lines.append("")

        # 多清晰度对比表（当有多条 FLV 数据时自动输出）
        if len(session.get("flv_data", [])) > 1:
            lines.append("多清晰度对比")
            lines.append(f"  {'清晰度':<14} {'分辨率':<16} {'帧率':<8} {'视频码率':<12} {'音频码率':<12} {'编码':<12} {'文件':<12}")
            lines.append("  " + "-" * 90)
            for flv in session["flv_data"]:
                fname = Path(str(flv.get("path", ""))).stem if flv.get("path") else ""
                quality = "未知"
                for tag in ["FULL_HD1", "HD1", "SD1", "SD2", "AUDIO_ONLY", "DEFAULT"]:
                    if tag in fname.upper():
                        quality = tag
                        break
                res = flv.get("resolution") or "N/A"
                eh = flv.get("encoder_hints") or {}
                fps_val = eh.get("framerate")
                fps = f"{int(fps_val)}" if isinstance(fps_val, (int, float)) else "N/A"
                vbr_val = eh.get("videodatarate")
                vbr = f"{vbr_val:.0f} Kbps" if isinstance(vbr_val, (int, float)) else "N/A"
                abr_val = eh.get("audiodatarate")
                abr = f"{abr_val:.0f} Kbps" if isinstance(abr_val, (int, float)) else "N/A"
                vcodec = flv.get("video_codec") or "N/A"
                acodec = flv.get("audio_codec") or "N/A"
                lines.append(f"  {quality:<14} {str(res):<16} {fps:<8} {vbr:<12} {abr:<12} {str(vcodec) + '/' + str(acodec):<12} {fname:<12}")
            lines.append("")

            # 音频轨道相似度分析
            audio_analysis = session.get("audio_track_analysis")
            if audio_analysis:
                lines.append("音频轨道对比")
                streams = audio_analysis.get("streams", {})
                if streams:
                    lines.append(f"  {'清晰度':<14} {'AAC Profile':<14} {'采样率':<12} {'声道':<8} {'帧数':<8} {'平均帧大小':<12} {'内容指纹':<16}")
                    lines.append("  " + "-" * 84)
                    for q, info in streams.items():
                        cfg = info.get("aac_config") or {}
                        profile = cfg.get("profile", "N/A")
                        sr = cfg.get("sample_rate", "N/A")
                        ch = cfg.get("channels", "N/A")
                        frames = info.get("aac_frames", 0)
                        avg_size = info.get("frame_size_avg", 0)
                        fp = info.get("content_fingerprint", "N/A")
                        fp_short = fp[:12] if fp and fp != "N/A" else "N/A"
                        lines.append(f"  {q:<14} {profile:<14} {sr:<12} {str(ch):<8} {frames:<8} {avg_size:<12.0f} {fp_short:<16}")
                    lines.append("")

                    sim_matrix = audio_analysis.get("similarity_matrix", {})
                    if sim_matrix and len(sim_matrix) > 1:
                        qualities = list(sim_matrix.keys())
                        header = "  " + "".join(f"{q[:12]:>14}" for q in qualities)
                        lines.append("  音频相似度矩阵")
                        lines.append(header)
                        for q1 in qualities:
                            row = f"  {q1[:12]:<14}" + "".join(
                                f"{sim_matrix[q1].get(q2, 0):>14.3f}" for q2 in qualities
                            )
                            lines.append(row)
                        lines.append("")

                conclusion = audio_analysis.get("conclusion", "")
                if conclusion:
                    consistent = audio_analysis.get("consistent", False)
                    tag = "PASS" if consistent else "WARN"
                    lines.append(f"  [{tag}] {conclusion}")
                    lines.append("")

            # 音画同步检测
            avs = session.get("av_sync_analysis")
            if avs:
                _p = avs.get("perceptual") or {}
                _so, _pr = _p.get("stream_offset_ms"), _p.get("player_residual_ms")
                _dr = _p.get("audio_timeline_drift_per_min_ms")
                lines.append("音画同步检测")
                lines.append(f"  感知判定: {avs.get('verdict', '—')}")
                if isinstance(_so, (int, float)):
                    lines.append(f"  流内偏移: {_so:+.2f} ms（{_p.get('direction', '—')}）"
                                 f" | 峰值 {_p.get('stream_peak_abs_ms', '—')} ms"
                                 f" | 播放器残差 {'未测' if _pr is None else f'{_pr:+.2f} ms'}")
                lines.append(f"  漂移: {'样本过短未评估' if _dr is None else f'{_dr:+.2f} ms/min'}"
                             f" | EBU R37: {'通过' if _p.get('ebu_r37_ok') else '超出'}"
                             f" | ITU 可察觉: {'超出' if _p.get('itu_detectable') else '未超出'}")
                lines.append("")
        else:
            lines.append("FLV 数据")
            has_stream_url = bool(session.get("push_conclusion", {}).get("cdn"))
            if has_stream_url:
                lines.append("  未获取到 FLV 样本（直播间可能未在推流，或 FLV 下载失败）")
            else:
                lines.append("  未获取到 FLV 样本（无可用流地址）")
            lines.append("")

        # 人脸嘴型音画同步（内容级）—— 独立于 FLV 分支，只要有嘴型结果就展示
        lips = session.get("lip_sync_analysis")
        if lips:
            _lp = lips.get("perceptual") or {}
            _lm = lips.get("metrics") or {}
            lines.append("人脸嘴型音画同步（内容级）")
            lines.append(f"  判定: {lips.get('verdict', '—')}（置信度 {_lp.get('confidence', '—')}）")
            _co = _lp.get("content_offset_ms")
            if isinstance(_co, (int, float)):
                lines.append(f"  内容级偏移: {_co:+.2f} ms（{_lp.get('direction', '—')}）"
                             f" | 相关峰 r={_lm.get('peak_correlation')}"
                             f" | 人脸覆盖 {(_lm.get('face_coverage') or 0) * 100:.0f}%")
            _ld = escape_cell(lips.get("verdict_detail", ""))
            if _ld:
                lines.append("  " + _ld.replace(chr(10), chr(10) + "  "))
            try:
                from lip_sync_detector import diagnose_vs_stream_offset
                _diag = diagnose_vs_stream_offset(lips, session.get("av_sync_analysis"))
            except Exception:
                _diag = ""
            if _diag:
                lines.append(f"  联合诊断: {_diag}")
            lines.append("")

        if session.get("ffprobe_data"):
            lines.append("ffprobe 探测")
            for fp in session["ffprobe_data"]:
                if fp.get("error"):
                    lines.append(f"  错误: {fp['error']}")
                    continue
                for s in fp.get("streams", []):
                    if s.get("codec_type") == "video":
                        w, h = s.get("width", "?"), s.get("height", "?")
                        fps = s.get("r_frame_rate", "?")
                        vparts = [f"{s.get('codec_name', '?')} {s.get('profile', '')}".strip()]
                        vparts.append(f"{w}x{h}")
                        vparts.append(f"@ {fps}fps")
                        if s.get("pix_fmt"):
                            vparts.append(f"pix_fmt={s['pix_fmt']}")
                        if s.get("level") is not None:
                            vparts.append(f"level={s['level']}")
                        if s.get("has_b_frames") is not None:
                            vparts.append(f"b_frames={s['has_b_frames']}")
                        lines.append(f"  视频: {' '.join(str(p) for p in vparts)}")
                    elif s.get("codec_type") == "audio":
                        aparts = [f"{s.get('codec_name', '?')}", f"{s.get('sample_rate', '?')}Hz"]
                        aparts.append(f"{s.get('channels', '?')}ch")
                        if s.get("channel_layout"):
                            aparts.append(s["channel_layout"])
                        if s.get("bit_rate"):
                            try:
                                aparts.append(f"{int(s['bit_rate']) // 1000}kbps")
                            except (ValueError, TypeError):
                                pass
                        lines.append(f"  音频: {' '.join(aparts)}")
                if fp.get("push_software"):
                    lines.append(f"  推流端: {fp['push_software']}")
                if fp.get("push_protocol"):
                    lines.append(f"  推流协议: {fp['push_protocol']}")
                if fp.get("hardware_encode") is not None:
                    lines.append(f"  硬件编码: {'是' if fp['hardware_encode'] else '否'}")
                dev = fp.get("device_info")
                if dev:
                    parts = [f"{k}={v}" for k, v in dev.items() if v]
                    if parts:
                        lines.append(f"  设备: {', '.join(parts)}")
                tags = fp.get("format_tags", {})
                _skip_tags_map = {
                    "Encoder": fp.get("push_software"),
                    "platform": fp.get("device_info", {}).get("platform") if isinstance(fp.get("device_info"), dict) else None,
                    "os_version": fp.get("device_info", {}).get("os_version") if isinstance(fp.get("device_info"), dict) else None,
                    "model": fp.get("device_info", {}).get("model") if isinstance(fp.get("device_info"), dict) else None,
                    "sdk_version": fp.get("device_info", {}).get("sdk_version") if isinstance(fp.get("device_info"), dict) else None,
                    "push_protocol": fp.get("push_protocol"),
                    "is_hardware_encode": fp.get("hardware_encode"),
                }
                _sensitive_tags = {"signResult", "signinfo", "signvalue", "signversion"}
                for k, v in tags.items():
                    if k in _skip_tags_map and _skip_tags_map[k] is not None and _skip_tags_map[k] is not False:
                        continue
                    if k in _sensitive_tags:
                        continue
                    # 码率字段加单位
                    if "bitrate" in k.lower() and v:
                        try:
                            lines.append(f"  {k}: {int(v)} kbps")
                        except (ValueError, TypeError):
                            lines.append(f"  {k}: {v}")
                    else:
                        lines.append(f"  {k}: {v}")
                lines.append("")

        if session.get("probe_data"):
            _probe_labels = {
                "gift_list_probe": "礼物列表 (gift/list)",
                "room_info_probe": "房间信息 (room/info)",
                "ranklist_probe": "观众排行榜 (ranklist/audience)",
            }
            lines.append("探针采集数据")
            for probe in session["probe_data"]:
                probe_type = probe.get("type", "unknown")
                label = _probe_labels.get(probe_type, probe_type)
                ok = probe.get("ok", False)
                status = "成功" if ok else "失败"
                lines.append(f"  [{label}] {status}")
                if probe.get("url_used"):
                    lines.append(f"    URL: {probe['url_used']}")
                if probe.get("error"):
                    lines.append(f"    错误: {probe['error']}")
                raw = probe.get("raw")
                if isinstance(raw, dict):
                    # 提取关键字段展示
                    _probe_fields = _extract_probe_fields(probe_type, raw)
                    for k, v in _probe_fields:
                        lines.append(f"    {k}: {v}")
                lines.append("")

        if session.get("rtmp_data"):
            lines.append("RTMP 数据")
            for rtmp in session["rtmp_data"]:
                lines.append(f"  文件: {rtmp['path'].name}")
                lines.append(f"  pcap: {rtmp.get('pcap_path') or '未知'}")
                if rtmp.get("error"):
                    lines.append(f"  错误: {rtmp['error']}")
                lines.append("  抓包发现:")
                if rtmp.get("findings"):
                    for finding in rtmp["findings"]:
                        lines.append(f"    - {finding}")
                else:
                    lines.append("    - 无")
                lines.append("")

        # 流URL结构数据
        if session.get("stream_url_data"):
            lines.append("流URL结构解析")
            for su in session["stream_url_data"]:
                su_type = su.get("type", "未知")
                ts = su.get("timestamp", "")
                lines.append(f"  [{su_type}] {ts}")
                # stream_url_struct
                if su.get("default_quality"):
                    lines.append(f"    默认清晰度: {su['default_quality']}")
                if su.get("quality_description"):
                    qd = su["quality_description"]
                    if isinstance(qd, list):
                        for q in qd:
                            lines.append(f"    清晰度选项: {q}")
                    elif isinstance(qd, dict):
                        for qk, qv in qd.items():
                            lines.append(f"    清晰度选项 {qk}: {qv}")
                if su.get("quality_map"):
                    for quality, urls in su["quality_map"].items():
                        lines.append(f"    {quality}:")
                        for u in urls:
                            lines.append(f"      URL: {u.get('url', '?')}")
                            if u.get("params"):
                                for pk, pv in u["params"].items():
                                    lines.append(f"        {pk} = {pv}")
                if su.get("protocols"):
                    for proto_key, proto_val in su["protocols"].items():
                        if isinstance(proto_val, str):
                            lines.append(f"    {proto_key}: {proto_val}")
                        elif isinstance(proto_val, list):
                            for pv in proto_val:
                                lines.append(f"    {proto_key}: {pv}")
                if su.get("all_urls"):
                    lines.append(f"    共发现 {len(su['all_urls'])} 个流URL")
                lines.append("")
            # stream_url_raw
            raw_items = [s for s in session["stream_url_data"] if s.get("type") == "stream_url_raw"]
            if raw_items:
                lines.append("流URL原始响应")
                for raw in raw_items:
                    obj = raw.get("stream_url_obj")
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            lines.append(f"  {k}: {safe_text(v)[:200]}")
                    lines.append("")

        # 房间解析信息
        if session.get("room_resolution_data"):
            lines.append("房间解析信息")
            for rr in session["room_resolution_data"]:
                lines.append(f"  Web房间号: {rr.get('web_room_id', '—')}")
                lines.append(f"  内部房间号: {rr.get('internal_room_id', '—')}")
                lines.append(f"  直播状态: {rr.get('status_text', '—')} (status={rr.get('room_status', '—')})")
                lines.append(f"  解析来源: {rr.get('source', '—')}")
                ts = rr.get("timestamp", "")
                if ts:
                    lines.append(f"  时间: {ts}")
                lines.append("")

        # Playwright认证信息
        if session.get("playwright_data"):
            lines.append("浏览器认证信息")
            for pw in session["playwright_data"]:
                pw_type = pw.get("type") or ""
                # 文件名辅助识别（部分playwright_enter文件无type字段）
                pw_path = str(pw.get("_source_path", pw.get("path", pw.get("file", ""))))
                if not pw_type:
                    if "playwright_enter" in pw_path:
                        pw_type = "playwright_enter"
                    elif "playwright_cookies" in pw_path:
                        pw_type = "playwright_cookies"
                    else:
                        pw_type = "playwright_unknown"
                ts = pw.get("timestamp", "")
                lines.append(f"  [{pw_type}] {ts}")
                if pw_type == "playwright_enter":
                    for k, v in pw.items():
                        if k not in ("type", "timestamp", "_source_path"):
                            lines.append(f"    {k}: {safe_text(v)[:200].replace(chr(10), ' ').replace(chr(13), ' ')}")
                elif pw_type == "playwright_cookies":
                    cookies = pw.get("cookies", [])
                    cookie_str = pw.get("cookie_string", "")
                    ms_token = pw.get("ms_token", "")
                    if isinstance(cookies, list) and cookies:
                        lines.append(f"    获取到 {len(cookies)} 个Cookie")
                        for c in cookies[:10]:
                            if isinstance(c, dict):
                                lines.append(f"      {c.get('name', '?')}: {str(c.get('value', ''))[:50]}...")
                        if len(cookies) > 10:
                            lines.append(f"      ... 共 {len(cookies)} 个")
                    if cookie_str:
                        lines.append(f"    Cookie字符串: {safe_text(cookie_str)[:200]}")
                    if ms_token:
                        lines.append(f"    ms_token: {safe_text(ms_token)[:100]}")
                    if pw.get("web_room_id"):
                        lines.append(f"    Web房间号: {pw['web_room_id']}")
                lines.append("")

        # Session Manifest 步骤执行信息
        if session.get("manifest_data"):
            manifest = session["manifest_data"]
            lines.append("采集步骤执行记录")
            steps = manifest.get("steps", {})
            for step_name, step_info in steps.items():
                success = step_info.get("success", False)
                status = "成功" if success else "失败"
                label = step_info.get("label", step_name)
                lines.append(f"  {label}: {status}")
                step_data = step_info.get("data", {})
                if isinstance(step_data, dict):
                    for k, v in step_data.items():
                        if k in ("multi_quality_analysis", "stream_url_struct"):
                            continue  # 已单独展示
                        sv = safe_text(v)
                        if sv and sv != "None" and len(sv) < 200:
                            lines.append(f"    {k}: {sv}")
                if step_info.get("error"):
                    lines.append(f"    错误: {step_info['error']}")
            artifacts = manifest.get("artifacts", [])
            if artifacts:
                lines.append(f"  采集产物: {len(artifacts)} 个文件")
                for a in artifacts[:10]:
                    lines.append(f"    {a.get('label', '?')} ({a.get('type', '?')}): {a.get('path', '?')}")
            lines.append("")

    lines.append("=" * 72)
    lines.append("房间摘要")
    lines.append("=" * 72)
    for session in sessions:
        conclusion = session.get("push_conclusion", {})
        dual_verdict_map = {"none": "无", "low": "低", "medium": "中", "high": "高"}
        dual_v = dual_verdict_map.get(session.get("dual_conclusion", {}).get("verdict", "none"), "-")
        ch_items = [e.get("interpretation", "") for e in (conclusion.get("evidence_list") or []) if "开播渠道" in e.get("interpretation", "")]
        env_items = [e.get("interpretation", "") for e in (conclusion.get("evidence_list") or []) if "推流环境" in e.get("interpretation", "")]
        channel_str = "; ".join(ch_items) if ch_items else "-"
        env_str = "; ".join(env_items) if env_items else "-"
        lines.append(f"  软件判断: {conclusion.get('software', '未知')}")
        lines.append(f"  置信度:   {conclusion.get('confidence', '低')}")
        lines.append(f"  第三方:   {format_bool_cn(conclusion.get('third_party'))}")
        lines.append(f"  开播渠道: {channel_str}")
        lines.append(f"  推流环境: {env_str}")
        lines.append(f"  平台来源: {'; '.join([e.get('interpretation', '') for e in (conclusion.get('evidence_list') or []) if '开播来源' in e.get('interpretation', '')]) or '-'}")
        lines.append(f"  CDN:      {conclusion.get('cdn', '未知')}")
        lines.append(f"  分辨率:   {conclusion.get('resolution', '未知')}")
        lines.append(f"  编码:     {conclusion.get('codec', '未知')}")
        lines.append(f"  双流风险: {dual_v}")
        lines.append("")

    try:
        Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        logger.error("保存文本报告失败: %s", e)


def escape_cell(value):
    return html.escape(safe_text(value))


APP_ID_MAP = {
    1128: "抖音",
    9323: "抖音极速版",
    1340: "抖音 Lite",
    2329: "抖音火山版",
    5495: "西瓜视频",
    1112: "今日头条",
    1129: "今日头条（极速版）",
    1130: "西瓜视频（旧）",
    351: "穿山甲SDK",
    2079: "直播伴侣（PC）",
    8663: "直播伴侣（PC，新版）",
    5666: "抖音 PC 端/网页版",
    5656: "抖音 PC 端/网页版（旧）",
    5430: "抖音商家后台",
    5399: "抖音企业号",
    3878: "抖店商家版",
    3529: "直播伴侣（Mac）",
    1364: "剪映",
}


def _format_count(n):
    """格式化数量：None→"—"，<10000原值，1万~1亿"X万"，>=1亿"X亿"。降级返回"—"。"""
    if n is None:
        return "—"
    if n < 10000:
        return str(n)
    if n < 100000000:
        return f"{n/10000:.1f}万"
    return f"{n/100000000:.1f}亿"


def _format_duration(seconds):
    """格式化时长：None→"—"，<60秒，<3600分秒，>=3600时分秒。降级返回"—"。"""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}秒"
    m = s // 60
    s = s % 60
    if m < 60:
        return f"{m}分{s}秒"
    h = m // 60
    m = m % 60
    return f"{h}时{m}分{s}秒"


def _extract_room_profile(reflow_raw):
    """从 reflow_raw 提取主播资料，支持 monitor 和原始 API 响应两种格式。"""
    if not reflow_raw:
        return {
            "nickname": "未知主播",
            "display_id": "",
            "uid": "—",
            "web_rid": "—",
            "avatar_url": "",
            "follower_count": None,
            "following_count": None,
            "create_time": None,
            "user_count": None,
            "app_id": None,
            "live_duration": None,
        }
    
    if reflow_raw.get("type") == "reflow_monitor":
        ft = reflow_raw.get("field_tree", {})
        nickname = ft.get("data.room.owner.nickname", {}).get("value", "未知主播") or "未知主播"
        display_id = ft.get("data.room.owner.display_id", {}).get("value", "") or ft.get("data.room.owner.short_id", {}).get("value", "") or ""
        uid = ft.get("data.room.owner.id", {}).get("value", "—") or "—"
        web_rid = ft.get("data.room.owner.web_rid", {}).get("value", "—") or "—"
        follower_count = ft.get("data.room.owner.follow_info.follower_count", {}).get("value")
        following_count = ft.get("data.room.owner.follow_info.following_count", {}).get("value")
        avatar_url = ft.get("data.room.owner.avatar_thumb.url_list[0]", {}).get("value", "") or ""
        create_time = ft.get("data.room.create_time", {}).get("value")
        user_count = ft.get("data.room.user_count", {}).get("value")
        app_id = ft.get("data.room.app_id", {}).get("value")
    else:
        owner = (((reflow_raw.get("data") or {}).get("room") or {}).get("owner")) or {}
        room = (((reflow_raw.get("data") or {}).get("room")) or {})
        nickname = owner.get("nickname", "未知主播") or "未知主播"
        display_id = owner.get("display_id", "") or owner.get("short_id", "") or ""
        uid = str(owner.get("id", "—")) if owner.get("id") else "—"
        web_rid = str(owner.get("web_rid", "—")) if owner.get("web_rid") else "—"
        follower_count = owner.get("follow_info", {}).get("follower_count")
        following_count = owner.get("follow_info", {}).get("following_count")
        avatar_url = owner.get("avatar_thumb", {}).get("url_list", [""])[0] or ""
        create_time = room.get("create_time")
        user_count = room.get("user_count")
        app_id = room.get("app_id")
    
    live_duration = None
    if create_time:
        try:
            create_ts = int(create_time)
            # 对 reflow_monitor 使用数据自身时间戳作为基准
            if reflow_raw.get("type") == "reflow_monitor" and reflow_raw.get("timestamp"):
                _ts = reflow_raw["timestamp"]
                if isinstance(_ts, (int, float)):
                    ref_ts = _ts
                elif isinstance(_ts, str):
                    ref_ts = datetime.fromisoformat(_ts.replace("Z", "+00:00").replace("+00:00", "")).timestamp()
                else:
                    ref_ts = datetime.now().timestamp()
            else:
                ref_ts = datetime.now().timestamp()
            live_duration = int(ref_ts - create_ts)
        except (ValueError, TypeError):
            pass
    
    return {
        "nickname": nickname or "未知主播",
        "display_id": display_id or "",
        "uid": str(uid) if uid else "—",
        "web_rid": str(web_rid) if web_rid else "—",
        "avatar_url": avatar_url or "",
        "follower_count": follower_count,
        "following_count": following_count,
        "create_time": create_time,
        "user_count": user_count,
        "app_id": app_id,
        "live_duration": live_duration,
    }


def generate_html_report(sessions, output_path):
    """生成暗色主题 HTML 报告。"""
    parts = []
    _global_web_rid = ""
    parts.append("<!DOCTYPE html>")
    parts.append("<html lang='zh-CN'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append("<script src='assets/flv.min.js'></script>")
    parts.append("<script src='assets/hls.min.js'></script>")
    parts.append("<title>抖音直播流分析 - 统一报告</title>")
    parts.append("<style>")
    parts.append("body{margin:0;padding:20px;background:#0f1115;color:#e6e6e6;font-family:Segoe UI,Arial,sans-serif;line-height:1.6;}")
    parts.append("h1,h2,h3{margin:0 0 12px;} .wrap{max-width:1200px;margin:0 auto;} .card{background:#171a21;border:1px solid #2b2f3a;border-radius:14px;padding:16px;margin:16px 0;box-shadow:0 10px 30px rgba(0,0,0,.2);}")
    parts.append("details{background:#171a21;border:1px solid #2b2f3a;border-radius:14px;padding:14px;margin:16px 0;} summary{cursor:pointer;font-weight:700;outline:none;}")
    parts.append("table{width:100%;border-collapse:collapse;margin:12px 0;display:block;overflow-x:auto;} th,td{border:1px solid #303644;padding:10px;text-align:left;vertical-align:top;} th{background:#1f2430;} code,pre{background:#11151d;border-radius:8px;padding:2px 6px;} pre{padding:12px;white-space:pre-wrap;word-break:break-word;}")
    parts.append(".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;} .metric{background:#11151d;border-radius:10px;padding:12px;border:1px solid #2d3340;} .badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;color:#111;} .muted{color:#9aa4b2;} .small{font-size:13px;} ul{padding-left:20px;} @media (max-width:640px){body{padding:12px;} th,td{font-size:13px;padding:8px;}}")
    parts.append(".profile-card{display:flex;align-items:center;gap:16px;padding:20px;} .profile-avatar{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid #2a9d8f;flex-shrink:0;background:#171a21;} .profile-info{display:flex;flex-direction:column;gap:4px;} .profile-name{font-size:1.2em;font-weight:600;color:#e6e6e6;} .profile-stats{display:flex;gap:16px;color:#999;font-size:0.85em;} .profile-id{font-size:0.85em;color:#666;} .link-box{background:#11151d;border:1px solid #555;border-radius:8px;padding:8px 12px;display:flex;align-items:center;gap:8px;font-family:monospace;font-size:0.85em;word-break:break-all;margin-top:8px;} .link-box a{color:#2a9d8f;text-decoration:none;} .copy-hint{font-size:0.75em;color:#555;margin-left:auto;white-space:nowrap;} .cta-btn{display:block;text-align:center;background:#2a9d8f;color:#e6e6e6;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:24px;} .fold-section{margin-top:16px;} .fold-section summary{cursor:pointer;padding:8px 0;color:#2a9d8f;font-weight:600;font-size:0.95em;} .metric-grid-extended{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:16px;}")
    parts.append(".copy-btn{background:#2a9d8f;border:none;color:#0f1115;font-weight:700;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap;}")
    parts.append(".copy-btn:hover{background:#3cbfae;} .url-cell{display:block;max-width:460px;max-height:76px;overflow:auto;overflow-wrap:anywhere;word-break:break-all;font-size:12px;padding:6px 8px;}")
    parts.append(".proto-tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;font-weight:700;white-space:nowrap;}")
    parts.append(".proto-flv{background:#2a9d8f;color:#08131a;} .proto-hls{background:#e76f51;color:#1f0a06;} .proto-lls{background:#8ab4f8;color:#0b1220;} .proto-other{background:#555b66;color:#eee;}")
    parts.append(".line-main{color:#4ecdc4;font-weight:600;} .line-backup{color:#f4a261;font-weight:600;}")
    parts.append(".qbadge{display:inline-block;min-width:40px;text-align:center;padding:2px 8px;border-radius:8px;background:#1f2430;border:1px solid #303644;font-weight:600;}")
    parts.append(".play-btn{background:#264653;border:1px solid #2a9d8f;color:#9ae6df;font-weight:700;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap;margin-right:6px;}")
    parts.append(".play-btn:hover{background:#2a9d8f;color:#08131a;} #player-modal{position:fixed;inset:0;z-index:999;display:none;align-items:center;justify-content:center;}")
    parts.append(".pm-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.72);} .pm-panel{position:relative;background:#171a21;border:1px solid #2b2f3a;border-radius:14px;padding:14px;max-width:min(920px,94vw);width:100%;box-shadow:0 20px 60px rgba(0,0,0,.5);}")
    parts.append(".pm-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;font-weight:700;} .pm-close{margin-left:auto;background:#2b2f3a;border:none;color:#e6e6e6;border-radius:8px;padding:6px 12px;cursor:pointer;}")
    parts.append("#pm-video{width:100%;max-height:60vh;background:#000;border-radius:10px;} .pm-status{margin-top:8px;font-family:monospace;font-size:13px;color:#9ae6df;word-break:break-all;}")
    parts.append("</style></head><body><div class='wrap'>")
    parts.append(f"<div class='card'><h1>抖音直播流分析 — 统一报告</h1><div class='muted'>生成时间：{escape_cell(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div><div class='muted'>会话数量：{len(sessions)}</div></div>")

    # --- 仪表盘头部：per-session 身份卡 + 指标网格 + 链接区 ---
    _session_profiles = []  # 每项: (profile_or_None, stream_url, room_id, version, sess, status)
    for _sidx, _sess in enumerate(sessions):
        _s_profile = None
        _s_stream_url = ""
        _s_room_id = "—"
        _s_version = "—"
        _s_status = "no_data"  # 默认：无数据
        for _rdata in _sess.get("reflow_data", []):
            if _rdata.get("raw") and not _rdata.get("error"):
                _s_profile = _extract_room_profile(_rdata["raw"])
                _s_stream_url = _rdata.get("stream_url", "") or ""
                _s_room_id = str(_rdata.get("room_id", "—"))
                _ifields = _rdata.get("interesting_fields") or {}
                for _key, _val in _ifields.items():
                    if "client_version" in _key.lower():
                        _s_version = str(_val) if _val else "—"
                        break
                _s_status = "ok"
                break
            elif _rdata.get("error"):
                _s_status = "error"  # 有数据但有 error
        # reflow 没有拉流地址时，从 FLV 数据回退
        if not _s_stream_url:
            for _fdata in _sess.get("flv_data", []):
                _furl = _fdata.get("stream_url") or ""
                if _furl:
                    _s_stream_url = _furl
                    break
        _session_profiles.append((_s_profile, _s_stream_url, _s_room_id, _s_version, _sess, _s_status))

    # 设置 _global_web_rid（用第一个有效的）
    for _sp, _, _, _, _, _ in _session_profiles:
        if _sp:
            _global_web_rid = _sp.get("web_rid", "")
            break

    def _render_identity_card(_profile, _stream_url, _room_id, _version, _target, _status="ok"):
        """内联渲染单个身份卡 + 指标网格 + 链接区到 _target 列表。"""
        if _status != "ok" or _profile is None:
            # 降级：无数据或获取失败
            _label = "无房间数据" if _status == "no_data" else "获取失败"
            _target.append(f"<div class='card profile-card'>")
            _target.append(f"<div class='profile-info'><div class='profile-name' style='color:#666'>&#128269; {_label}</div>")
            _target.append(f"<div class='muted small'>该会话未获取到有效的直播间数据</div></div></div>")
            # 全 "—" 的 9 指标网格
            _target.append("<div class='grid metric-grid-extended'>")
            _fallback_labels = ["昵称", "抖音号", "UID", "房间ID", "开播时间", "直播时长", "开播渠道", "版本号", "实时人数"]
            _fallback_val = "—" if _status == "no_data" else "获取失败"
            for _lb in _fallback_labels:
                _target.append(f"<div class='metric'><div class='muted small'>{escape_cell(_lb)}</div><div class='muted'>{escape_cell(_fallback_val)}</div></div>")
            _target.append("</div>")
            return
        # 身份卡
        _target.append("<div class='card profile-card'>")
        if _profile["avatar_url"]:
            _av = escape_cell(_profile["avatar_url"])
            if _av.startswith("//"):
                _av = "https:" + _av
            _target.append(f"<img class='profile-avatar' src='{_av}' alt='头像'>")
        else:
            _target.append("<div class='profile-avatar' style='background:#555;display:flex;align-items:center;justify-content:center;font-size:1.5em;color:#666'>&#128100;</div>")
        _target.append("<div class='profile-info'>")
        _target.append(f"<div class='profile-name'>{escape_cell(_profile['nickname'])}</div>")
        _target.append(f"<div class='profile-stats'><span>粉丝 {_format_count(_profile['follower_count'])}</span><span>关注 {_format_count(_profile['following_count'])}</span></div>")
        _did = escape_cell(_profile['display_id']) or '未设置'
        _target.append(f"<div class='profile-id'>抖音号: {_did} · UID: {escape_cell(str(_profile['uid']))}</div>")
        _target.append("</div></div>")

        # 扩展指标网格（9 指标）
        _metrics = []
        _metrics.append({"label": "昵称", "value": _profile["nickname"], "icon": "&#128100;", "color": "#4ecdc4"})
        _metrics.append({"label": "抖音号", "value": _profile["display_id"] or "未设置", "icon": "&#129395;", "color": "#45b7d1"})
        _metrics.append({"label": "UID", "value": str(_profile["uid"]), "icon": "&#128290;", "color": "#96ceb4"})
        _metrics.append({"label": "房间ID", "value": _room_id, "icon": "&#127968;", "color": "#ffeaa7"})
        # 开播时间
        _ct = _profile.get("create_time")
        if _ct:
            try:
                _ct_str = datetime.fromtimestamp(int(_ct)).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, OSError):
                _ct_str = "未知"
        else:
            _ct_str = "未知"
        _metrics.append({"label": "开播时间", "value": _ct_str, "icon": "&#128336;", "color": "#dda0dd"})
        _metrics.append({"label": "直播时长", "value": _format_duration(_profile.get("live_duration")), "icon": "&#9201;", "color": "#98d8c8"})
        # 开播渠道
        _aid = _profile.get("app_id")
        _channel = APP_ID_MAP.get(_aid, f"未知({_aid})") if _aid else "未知"
        _metrics.append({"label": "开播渠道", "value": _channel, "icon": "&#128241;", "color": "#f7dc6f"})
        _metrics.append({"label": "版本号", "value": _version, "icon": "&#128204;", "color": "#bb8fce"})
        _metrics.append({"label": "实时人数", "value": _format_count(_profile.get("user_count")), "icon": "&#128101;", "color": "#f1948a"})

        _target.append("<div class='grid metric-grid-extended'>")
        for _m in _metrics:
            _target.append(f"<div class='metric'><div style='font-size:1.3em'>{_m['icon']}</div><div class='muted small'>{escape_cell(_m['label'])}</div><div style='font-size:1.1em;color:{_m['color']};font-weight:600'>{escape_cell(str(_m['value']))}</div></div>")
        _target.append("</div>")

        # 链接区
        _web_rid = _profile.get("web_rid", "—")
        if _web_rid and _web_rid != "—":
            _live_url = f"https://live.douyin.com/{_web_rid}"
            _target.append(f"<div class='link-box'><span class='muted small'>&#128279; 直播间:</span> <a href='{escape_cell(_live_url)}' target='_blank'>{escape_cell(_live_url)}</a><span class='copy-hint'>长按复制</span></div>")
        _su_label, _su_url = _pick_display_stream_url(_stream_url)
        if _su_url:
            _short = _su_url[:110] + ("…" if len(_su_url) > 110 else "")
            _target.append(f"<div class='link-box'><span class='muted small'>&#128279; 拉流地址:</span> "
                           f"<span class='muted small'>{escape_cell(_su_label)}</span> "
                           f"<a href='{escape_cell(_su_url)}' target='_blank'>{escape_cell(_short)}</a>"
                           f"<button class='play-btn' data-url='{escape_cell(_su_url)}' data-proto='flv' data-label='{escape_cell(_su_label)}'>▶ 播放</button>"
                           f"<button class='copy-btn' style='margin-left:auto;flex-shrink:0' data-copy='{escape_cell(_su_url)}'>复制</button></div>")
        else:
            _target.append("<div class='link-box'><span class='muted'>未获取到拉流地址</span></div>")

    if _session_profiles:
        if len(_session_profiles) == 1:
            # 单 session：直接渲染（无 details 包裹）
            _prof, _surl, _rid, _ver, _sess, _stat = _session_profiles[0]
            _render_identity_card(_prof, _surl, _rid, _ver, parts, _stat)
        else:
            # 多 session：每个用 details 包裹，第一个 open
            for _i, (_prof, _surl, _rid, _ver, _sess, _stat) in enumerate(_session_profiles):
                _open_attr = " open" if _i == 0 else ""
                parts.append(f"<details class='fold-section'{_open_attr}><summary>🎙️ {escape_cell(session_title(_sess))}</summary>")
                _render_identity_card(_prof, _surl, _rid, _ver, parts, _stat)
                parts.append("</details>")
    else:
        parts.append("<div class='card'><div class='muted'>&#128269; 未获取到直播间身份信息（无 reflow 数据）</div></div>")

    for session in sessions:
        conclusion = session.get("push_conclusion", {})
        color = CONFIDENCE_COLORS.get(conclusion.get("confidence", "低"), "#ef5350")
        dual_colors = {"high": "#ef5350", "medium": "#fbc02d", "low": "#ff9800", "none": "#4caf50"}
        _s_ts = session["start_time"].strftime("%Y-%m-%d %H:%M:%S") if session.get("start_time") else ""
        _rid = escape_cell(session.get('primary_room_id') or '未知')
        parts.append(f"<div class='card' style='margin-top:20px'>")
        parts.append(f"<div style='display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px'>")
        parts.append(f"<h2 style='margin:0'>房间摘要</h2>")
        parts.append(f"<span class='muted small'>{_rid} | {_s_ts}</span>")
        parts.append(f"</div>")
        parts.append("<div class='grid'>")
        parts.append(f"<div class='metric'><div class='muted small'>软件判断</div><div style='font-weight:600'>{escape_cell(conclusion.get('software', '未知'))}</div></div>")
        parts.append(f"<div class='metric'><div class='muted small'>置信度</div><div><span class='badge' style='background:{color}'>{escape_cell(conclusion.get('confidence', '低'))}</span></div></div>")
        parts.append(f"<div class='metric'><div class='muted small'>第三方推流</div><div>{escape_cell(format_bool_cn(conclusion.get('third_party')))}</div></div>")
        parts.append(f"<div class='metric'><div class='muted small'>CDN / 服务商</div><div>{escape_cell(conclusion.get('cdn', '未知'))}</div></div>")
        parts.append(f"<div class='metric'><div class='muted small'>分辨率</div><div>{escape_cell(conclusion.get('resolution', '未知'))}</div></div>")
        parts.append(f"<div class='metric'><div class='muted small'>编码</div><div>{escape_cell(conclusion.get('codec', '未知'))}</div></div>")
        for ev_label, ev_keyword in [("开播渠道", "开播渠道"), ("平台来源", "开播来源"), ("推流环境", "推流环境")]:
            ev_items = [e.get("interpretation", "") for e in (conclusion.get("evidence_list") or []) if ev_keyword in e.get("interpretation", "")]
            ev_val = "; ".join(ev_items) if ev_items else "—"
            parts.append(f"<div class='metric'><div class='muted small'>{ev_label}</div><div>{escape_cell(ev_val)}</div></div>")
        dual_verdict = session.get("dual_conclusion", {}).get("verdict", "none")
        dual_v = {"none": "无", "low": "低", "medium": "中", "high": "高"}.get(dual_verdict, "-")
        dual_color = dual_colors.get(dual_verdict, "#4caf50")
        parts.append(f"<div class='metric'><div class='muted small'>双流风险</div><div><span class='badge' style='background:{dual_color}'>{escape_cell(dual_v)}</span></div></div>")
        parts.append("</div>")
        parts.append("</div>")
        # 拉流地址全景（stream_url 结构化解析）
        _render_stream_url_panorama(session, parts)

        dual_conclusion = session.get("dual_conclusion", {})
        if dual_conclusion and dual_conclusion.get("verdict", "none") != "none":
            dual_color = dual_colors.get(dual_conclusion.get("verdict", "none"), "#9aa4b2")
            parts.append(f"<details><summary><h3 style='display:inline'>🔬 双流异常检测</h3> <span class='badge' style='background:{dual_color}'>{escape_cell(dual_conclusion.get('verdict', 'none'))}</span></summary>")
            parts.append("<div>")
            parts.append("<div class='grid'>")
            parts.append(f"<div class='metric'><div class='muted small'>风险等级</div><div><span class='badge' style='background:{dual_color}'>{escape_cell(dual_conclusion.get('verdict', 'none'))}</span></div></div>")
            parts.append(f"<div class='metric'><div class='muted small'>风险评分</div><div>{dual_conclusion.get('score', 0)}/100</div></div>")
            parts.append("</div>")
            signals = dual_conclusion.get("signals", [])
            if signals:
                parts.append("<table><thead><tr><th>强度</th><th>描述</th><th>详情</th></tr></thead><tbody>")
                for sig in signals[:10]:
                    parts.append(f"<tr><td>{escape_cell(sig.get('strength', '?'))}</td><td>{escape_cell(sig.get('description', ''))}</td><td><pre>{escape_cell(safe_text(sig.get('detail', '')))}</pre></td></tr>")
                parts.append("</tbody></table>")
            parts.append("</div></details>")

        ev_count = len(conclusion.get("evidence_list") or [])
        parts.append(f"<details><summary><h3 style='display:inline'>📋 证据链</h3> ({ev_count} 条)</summary>")
        parts.append("<table><thead><tr><th>来源</th><th>原值</th><th>解读</th><th>权级</th></tr></thead><tbody>")
        if conclusion.get("evidence_list"):
            for evidence in conclusion["evidence_list"]:
                parts.append(
                    "<tr>"
                    f"<td>{escape_cell(evidence['source'])}</td>"
                    f"<td><pre>{escape_cell(evidence['value'])}</pre></td>"
                    f"<td>{escape_cell(evidence['interpretation'])}</td>"
                    f"<td>{escape_cell(evidence['level'])}</td>"
                    "</tr>"
                )
        else:
            parts.append("<tr><td colspan='4'>暂无足够证据</td></tr>")
        parts.append("</tbody></table></details>")

        parts.append("<details><summary><h3 style='display:inline'>📁 关联文件</h3></summary><ul>")
        for key in ["reflow_files", "flv_samples", "flv_reports", "rtmp_pcaps", "rtmp_reports"]:
            files = session.get(key, [])
            if not files:
                continue
            parts.append(f"<li><strong>{escape_cell(key)}</strong><ul>")
            for item in files:
                ename = item.get("evidence_name")
                if ename:
                    parts.append(f"<li><a href='./{escape_cell(ename)}'>{escape_cell(item['name'])}</a></li>")
                else:
                    parts.append(f"<li>{escape_cell(item['name'])}</li>")
            parts.append("</ul></li>")
        parts.append("</ul></details>")

        if session.get("reflow_data"):
            parts.append("<details><summary><h3 style='display:inline'>📡 Reflow 关键字段</h3></summary>")
            for reflow in session["reflow_data"]:
                ename = reflow.get("evidence_name")
                fname_html = f"<a href='./{escape_cell(ename)}'>{escape_cell(reflow['path'].name)}</a>" if ename else escape_cell(reflow['path'].name)
                parts.append(f"<div class='card'><div><strong>{fname_html}</strong></div>")
                parts.append(f"<div class='small muted'>room_id={escape_cell(reflow.get('room_id') or '未知')} | 状态={escape_cell(reflow.get('status_text', '未知'))}</div>")
                # 关键字段表放在可折叠区域，默认收起（数据量大）
                field_count = len(reflow.get("interesting_fields", {}))
                parts.append(f"<details><summary>关键字段 ({field_count} 项，点击展开)</summary>")
                parts.append("<table><thead><tr><th>字段</th><th>值</th><th>说明</th></tr></thead><tbody>")
                if reflow.get("interesting_fields"):
                    for key, value in sorted(reflow["interesting_fields"].items()):
                        field_desc = get_field_description(key)
                        value_desc = get_value_description(key, value)
                        desc_parts = []
                        if field_desc:
                            desc_parts.append(f"<span class='muted'>{escape_cell(field_desc)}</span>")
                        if value_desc:
                            desc_parts.append(f"<span style='color:#1565c0'>→ {escape_cell(value_desc)}</span>")
                        desc_html = "<br>".join(desc_parts) if desc_parts else ""
                        parts.append(f"<tr><td>{escape_cell(key)}</td><td><pre>{escape_cell(_truncate_display_value(value))}</pre></td><td>{desc_html}</td></tr>")
                else:
                    parts.append("<tr><td colspan='3'>无关键字段</td></tr>")
                parts.append("</tbody></table></details></div>")
            parts.append("</details>")

        parts.append("<details><summary><h3 style='display:inline'>🎬 FLV 编码分析</h3></summary>")
        if session.get("flv_data"):
            for flv in session["flv_data"]:
                ename = flv.get("evidence_name")
                fname_html = f"<a href='./{escape_cell(ename)}'>{escape_cell(flv['path'].name)}</a>" if ename else escape_cell(flv['path'].name)
                codec_line = f"{flv.get('video_codec', '?')}/{flv.get('audio_codec', '?')}"
                res_line = flv.get('resolution', '')
                parts.append(f"<div class='card'>")
                parts.append(f"<details><summary><strong>{fname_html}</strong> {escape_cell(codec_line)} {escape_cell(res_line)}</summary>")
                parts.append(f"<div class='small muted'>URL: {escape_cell(flv.get('stream_url') or '未知')}</div>")
                parts.append("<table><thead><tr><th>线索字段</th><th>值</th></tr></thead><tbody>")
                if flv.get("encoder_hints"):
                    _flv_sensitive = {"signResult", "signinfo", "signvalue", "signversion",
                                     "fp_coderate", "fp_amode", "fp_vmode", "fp_nb",
                                     "e_id", "h_id", "s_id", "p_id", "link_info"}
                    for key, value in sorted(flv["encoder_hints"].items()):
                        if key in _flv_sensitive:
                            continue
                        parts.append(f"<tr><td>{escape_cell(key)}</td><td><pre>{escape_cell(value)}</pre></td></tr>")
                else:
                    parts.append("<tr><td colspan='2'>无编码器线索（onMetaData 中未找到 encoder/flashVer/metadatacreator 等字段）</td></tr>")
                parts.append("</tbody></table></details></div>")
        else:
            parts.append("<div class='card'><div class='muted'>未获取到 FLV 样本（直播间可能未在推流，或 FLV 下载失败）</div></div>")
        parts.append("</details>")

        if session.get("ffprobe_data"):
            parts.append("<details><summary><h3 style='display:inline'>🔍 ffprobe 探测</h3></summary>")
            for fp in session["ffprobe_data"]:
                if fp.get("error"):
                    parts.append(f"<div class='card'><div class='muted'>ffprobe 错误: {escape_cell(fp['error'])}</div></div>")
                    continue
                # 摘要行
                _summary_parts = []
                for s in fp.get("streams", []):
                    if s.get("codec_type") == "video":
                        _summary_parts.append(f"视频: {s.get('codec_name', '?')} {s.get('width', '?')}x{s.get('height', '?')}")
                    elif s.get("codec_type") == "audio":
                        _summary_parts.append(f"音频: {s.get('codec_name', '?')}")
                _summary = " | ".join(_summary_parts) if _summary_parts else "详情"
                parts.append(f"<div class='card'><details><summary>{escape_cell(_summary)}</summary>")
                parts.append("<table><thead><tr><th>属性</th><th>值</th></tr></thead><tbody>")
                for s in fp.get("streams", []):
                    if s.get("codec_type") == "video":
                        w, h = s.get("width", "?"), s.get("height", "?")
                        fps = s.get("r_frame_rate", "?")
                        vparts = [f"{escape_cell(s.get('codec_name', '?'))} {escape_cell(s.get('profile', ''))}".strip()]
                        vparts.append(f"{w}x{h}")
                        vparts.append(f"@ {escape_cell(fps)}fps")
                        if s.get("pix_fmt"):
                            vparts.append(f"pix_fmt={escape_cell(s['pix_fmt'])}")
                        if s.get("level") is not None:
                            vparts.append(f"level={s['level']}")
                        if s.get("has_b_frames") is not None:
                            vparts.append(f"b_frames={s['has_b_frames']}")
                        parts.append(f"<tr><td>视频流</td><td>{' '.join(str(p) for p in vparts)}</td></tr>")
                    elif s.get("codec_type") == "audio":
                        aparts = [escape_cell(s.get('codec_name', '?')), f"{s.get('sample_rate', '?')}Hz"]
                        aparts.append(f"{s.get('channels', '?')}ch")
                        if s.get("channel_layout"):
                            aparts.append(escape_cell(s["channel_layout"]))
                        if s.get("bit_rate"):
                            try:
                                aparts.append(f"{int(s['bit_rate']) // 1000}kbps")
                            except (ValueError, TypeError):
                                pass
                        parts.append(f"<tr><td>音频流</td><td>{' '.join(aparts)}</td></tr>")
                if fp.get("push_software"):
                    parts.append(f"<tr><td>推流端</td><td><pre>{escape_cell(fp['push_software'])}</pre></td></tr>")
                if fp.get("push_protocol"):
                    parts.append(f"<tr><td>推流协议</td><td>{escape_cell(fp['push_protocol'])}</td></tr>")
                if fp.get("hardware_encode") is not None:
                    parts.append(f"<tr><td>硬件编码</td><td>{'是' if fp['hardware_encode'] else '否'}</td></tr>")
                dev = fp.get("device_info")
                if dev:
                    parts.append(f"<tr><td>设备</td><td>{escape_cell(', '.join(f'{k}={v}' for k, v in dev.items() if v))}</td></tr>")
                tags = fp.get("format_tags", {})
                # 只跳过已通过结构化字段展示的 tag（若结构化字段缺失则保留 tag 作为降级）
                _skip_tags_map = {
                    "Encoder": fp.get("push_software"),
                    "platform": fp.get("device_info", {}).get("platform") if isinstance(fp.get("device_info"), dict) else None,
                    "os_version": fp.get("device_info", {}).get("os_version") if isinstance(fp.get("device_info"), dict) else None,
                    "model": fp.get("device_info", {}).get("model") if isinstance(fp.get("device_info"), dict) else None,
                    "sdk_version": fp.get("device_info", {}).get("sdk_version") if isinstance(fp.get("device_info"), dict) else None,
                    "push_protocol": fp.get("push_protocol"),
                    "is_hardware_encode": fp.get("hardware_encode"),
                }
                _sensitive_tags = {"signResult", "signinfo", "signvalue", "signversion"}
                for k, v in tags.items():
                    if k in _skip_tags_map and _skip_tags_map[k] is not None and _skip_tags_map[k] is not False:
                        continue
                    if k in _sensitive_tags:
                        continue
                    parts.append(f"<tr><td>{escape_cell(k)}</td><td>{escape_cell(str(v))}</td></tr>")
                parts.append("</tbody></table></details></div>")
            parts.append("</details>")

        if session.get("probe_data"):
            _probe_labels = {
                "gift_list_probe": "🎁 礼物列表 (gift/list)",
                "room_info_probe": "📋 房间信息 (room/info)",
                "ranklist_probe": "🏆 观众排行榜 (ranklist/audience)",
            }
            parts.append(f"<details><summary><h3 style='display:inline'>📡 探针采集数据</h3> ({len(session['probe_data'])} 项)</summary>")
            for probe in session["probe_data"]:
                probe_type = probe.get("type", "unknown")
                label = _probe_labels.get(probe_type, probe_type)
                ok = probe.get("ok", False)
                status_cls = "success" if ok else "error"
                status_text = "成功" if ok else "失败"
                parts.append(f"<div class='card'><h4>{escape_cell(label)}</h4><span class='{status_cls}'>{status_text}</span>")
                if probe.get("error"):
                    parts.append(f"<div class='muted'>错误: {escape_cell(probe['error'])}</div>")
                raw = probe.get("raw")
                if isinstance(raw, dict):
                    probe_fields = _extract_probe_fields(probe_type, raw)
                    if probe_fields:
                        parts.append("<table><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>")
                        for k, v in probe_fields:
                            parts.append(f"<tr><td>{escape_cell(str(k))}</td><td>{escape_cell(str(v))}</td></tr>")
                        parts.append("</tbody></table>")
                parts.append("</div>")
            parts.append("</details>")
            parts.append("")

        if session.get("rtmp_data"):
            parts.append(f"<details><summary><h3 style='display:inline'>🔧 RTMP 分析</h3> ({len(session['rtmp_data'])} 项)</summary>")
            for rtmp in session["rtmp_data"]:
                ename = rtmp.get("evidence_name")
                fname_html = f"<a href='./{escape_cell(ename)}'>{escape_cell(rtmp['path'].name)}</a>" if ename else escape_cell(rtmp['path'].name)
                parts.append(f"<div class='card'><div><strong>{fname_html}</strong></div><ul>")
                if rtmp.get("findings"):
                    for finding in rtmp["findings"]:
                        parts.append(f"<li><pre>{escape_cell(finding)}</pre></li>")
                else:
                    parts.append("<li>无抓包发现</li>")
                parts.append("</ul></div>")
            parts.append("</details>")
            parts.append("")

        # 音频轨道对比
        audio_analysis = session.get("audio_track_analysis")
        if audio_analysis:
            streams = audio_analysis.get("streams", {})
            if streams:
                _conclusion = audio_analysis.get("conclusion", "")
                _consistent = "PASS" if audio_analysis.get("consistent") else "WARN"
                parts.append(f"<details><summary><h3 style='display:inline'>🔊 音频轨道对比</h3> {_consistent}</summary>")
                parts.append("<div class='card'><table><thead><tr><th>清晰度</th><th>AAC Profile</th><th>采样率</th><th>声道</th><th>帧数</th><th>平均帧大小</th><th>内容指纹</th></tr></thead><tbody>")
                for q, info in streams.items():
                    cfg = info.get("aac_config") or {}
                    fp = info.get("content_fingerprint", "N/A")
                    fp_short = fp[:12] if fp and fp != "N/A" else "N/A"
                    parts.append(f"<tr><td>{escape_cell(q)}</td><td>{escape_cell(cfg.get('profile', 'N/A'))}</td><td>{escape_cell(cfg.get('sample_rate', 'N/A'))}</td><td>{escape_cell(str(cfg.get('channels', 'N/A')))}</td><td>{info.get('aac_frames', 0)}</td><td>{info.get('frame_size_avg', 0):.0f}</td><td><code>{escape_cell(fp_short)}</code></td></tr>")
                parts.append("</tbody></table>")

                sim_matrix = audio_analysis.get("similarity_matrix", {})
                if sim_matrix and len(sim_matrix) > 1:
                    qualities = list(sim_matrix.keys())
                    parts.append("<table style='margin-top:8px'><thead><tr><th></th>")
                    for q in qualities:
                        parts.append(f"<th>{escape_cell(q[:12])}</th>")
                    parts.append("</tr></thead><tbody>")
                    for q1 in qualities:
                        parts.append(f"<tr><td><strong>{escape_cell(q1[:12])}</strong></td>")
                        for q2 in qualities:
                            val = sim_matrix[q1].get(q2, 0)
                            cls = "style='color:green'" if val >= 0.9 else "style='color:orange'" if val >= 0.5 else "style='color:red'"
                            parts.append(f"<td {cls}>{val:.3f}</td>")
                        parts.append("</tr>")
                    parts.append("</tbody></table>")

                conclusion = audio_analysis.get("conclusion", "")
                consistent = audio_analysis.get("consistent", False)
                tag_cls = "success" if consistent else "warning"
                parts.append(f"<div class='{tag_cls}' style='padding:6px;margin-top:6px'>{escape_cell(conclusion)}</div>")
                parts.append("</div></details>")

        # 音画同步检测（流内偏移 + 播放器残差，按人耳感知门限判定）
        avs = session.get("av_sync_analysis")
        if avs:
            _p = avs.get("perceptual") or {}
            _mm = avs.get("metrics") or {}
            _vcolor = CONFIDENCE_COLORS.get(avs.get("verdict_level", "低"), "#ef5350")
            parts.append(f"<details open><summary><h3 style='display:inline'>⏱ 音画同步检测</h3> "
                         f"<span class='badge' style='background:{_vcolor}'>{escape_cell(avs.get('verdict', '—'))}</span></summary>")
            parts.append("<div class='card'><div class='grid'>")
            _so, _pr = _p.get("stream_offset_ms"), _p.get("player_residual_ms")
            _dr = _p.get("audio_timeline_drift_per_min_ms")
            _rows = [
                ("流内偏移（主指标）", f"{_so:+.2f} ms" if isinstance(_so, (int, float)) else "—"),
                ("偏移方向", _p.get("direction", "—")),
                ("峰值偏移", f"{_p.get('stream_peak_abs_ms', '—')} ms"),
                ("播放器残差", f"{_pr:+.2f} ms" if isinstance(_pr, (int, float)) else "未测"),
                ("音频时间线漂移", f"{_dr:+.2f} ms/min" if isinstance(_dr, (int, float)) else "样本过短未评估"),
                ("EBU R37 广播窗口", "通过" if _p.get("ebu_r37_ok") else "超出"),
                ("ITU 可察觉门限", "超出（可感）" if _p.get("itu_detectable") else "未超出"),
            ]
            if _mm.get("video"):
                _v = _mm["video"]
                _rows.append(("视频轨道", f"{_v.get('fps_est', '?')} fps · 抖动 {_v.get('jitter_ms', 0):.1f} ms"
                                          f" · 丢帧 {_v.get('dropped_frames', 0)}"))
            _tsh = _mm.get("ts_health") or {}
            if _tsh.get("audio_jumps") or _tsh.get("video_jumps"):
                _rows.append(("时间戳跳变", f"音频 {_tsh.get('audio_jumps', 0)} 处 / 视频 {_tsh.get('video_jumps', 0)} 处"))
            for _label, _val in _rows:
                parts.append(f"<div class='metric'><div class='muted small'>{escape_cell(_label)}</div>"
                             f"<div style='font-weight:600'>{escape_cell(str(_val))}</div></div>")
            parts.append("</div>")
            _detail_html = escape_cell(avs.get("verdict_detail", "")).replace(chr(10), "<br>")
            parts.append(f"<div class='card' style='margin-top:10px;background:#11151d'>"
                         f"<div class='small'>{_detail_html}</div></div>")
            parts.append("</div></details>")

        # 人脸嘴型音画同步（内容级，与时间戳级互补）
        lips = session.get("lip_sync_analysis")
        if lips:
            _lp = lips.get("perceptual") or {}
            _lm = lips.get("metrics") or {}
            _lcolor = CONFIDENCE_COLORS.get(lips.get("verdict_level", "低"), "#ef5350")
            parts.append(f"<details open><summary><h3 style='display:inline'>🗣 人脸嘴型音画同步（内容级）</h3> "
                         f"<span class='badge' style='background:{_lcolor}'>{escape_cell(lips.get('verdict', '—'))}</span></summary>")
            parts.append("<div class='card'><div class='grid'>")
            _co = _lp.get("content_offset_ms")
            _lrows = [
                ("内容级偏移（不看时间戳）", f"{_co:+.2f} ms" if isinstance(_co, (int, float)) else "—"),
                ("偏移方向", _lp.get("direction", "—")),
                ("相关峰 r", str(_lm.get("peak_correlation", "—"))),
                ("测量分辨力", f"{_lm.get('lag_resolution_ms', '—')} ms"),
                ("偏移置信区间", (f"±{_lp.get('content_offset_ci_ms')} ms"
                             if _lp.get("content_offset_ci_ms") is not None else "样本不足未给出")),
                ("滑窗一致率", (f"{(_lp.get('window_agreement') or 0) * 100:.0f}%"
                            f"（{_lm.get('windows_used')}/{_lm.get('windows_total')} 窗）"
                            if _lp.get("window_agreement") is not None else "—")),
                ("样本时长", f"{_lm.get('media_span_s', '—')} s"),
                ("判定置信度", _lp.get("confidence", "—")),
                ("人脸覆盖", f"{(_lm.get('face_coverage') or 0) * 100:.0f}%"),
                ("ROI 来源", "人脸自动" if _lm.get("roi_mode") == "face-auto" else "手动指定"),
                ("分段一致性", str(_lm.get("segment_consistency")) if _lm.get("segment_consistency") is not None else "样本偏短未评估"),
                ("说话检出", "是" if _lm.get("talking_detected") else "否（置信度下降）"),
                ("EBU R37 广播窗口", "通过" if _lp.get("ebu_r37_ok") else "超出"),
                ("ITU 可察觉门限", "超出（可感）" if _lp.get("itu_detectable") else "未超出"),
            ]
            for _label, _val in _lrows:
                parts.append(f"<div class='metric'><div class='muted small'>{escape_cell(_label)}</div>"
                             f"<div style='font-weight:600'>{escape_cell(str(_val))}</div></div>")
            parts.append("</div>")
            try:
                from lip_sync_detector import diagnose_vs_stream_offset
                _diag = diagnose_vs_stream_offset(lips, session.get("av_sync_analysis"))
            except Exception:
                _diag = ""
            if _diag:
                parts.append(f"<div class='card' style='margin-top:10px;background:#111820;border-left:3px solid {_lcolor}'>"
                             f"<div class='small'><strong>联合诊断（内容级 vs 时间戳级）：</strong>{escape_cell(_diag)}</div></div>")
            _ldetail = escape_cell(lips.get("verdict_detail", "")).replace(chr(10), "<br>")
            parts.append(f"<div class='card' style='margin-top:10px;background:#11151d'>"
                         f"<div class='small'>{_ldetail}</div></div>")
            parts.append("</div></details>")

        # 流URL结构解析
        if session.get("stream_url_data"):
            parts.append(f"<details><summary><h3 style='display:inline'>🔗 流URL结构解析</h3> ({len(session['stream_url_data'])} 项)</summary>")
            for su in session["stream_url_data"]:
                su_type = su.get("type", "未知")
                ts = su.get("timestamp", "")
                parts.append(f"<div class='card'><h4>{escape_cell(su_type)}</h4><div class='muted small'>{escape_cell(ts)}</div>")
                if su.get("default_quality"):
                    parts.append(f"<div><strong>默认清晰度:</strong> {escape_cell(str(su['default_quality']))}</div>")
                if su.get("quality_description"):
                    qd = su["quality_description"]
                    if isinstance(qd, list):
                        parts.append(f"<div><strong>清晰度选项:</strong> {escape_cell('; '.join(str(q) for q in qd))}</div>")
                    elif isinstance(qd, dict):
                        parts.append(f"<div><strong>清晰度选项:</strong> {escape_cell(str(qd))}</div>")
                if su.get("quality_map"):
                    parts.append("<table><thead><tr><th>清晰度</th><th>URL</th><th>参数</th></tr></thead><tbody>")
                    for quality, urls in su["quality_map"].items():
                        for u in urls:
                            params_str = ""
                            if u.get("params"):
                                params_str = "<br>".join(f"{escape_cell(pk)}={escape_cell(str(pv)[:60])}" for pk, pv in u["params"].items())
                            parts.append(f"<tr><td>{escape_cell(quality)}</td><td><code>{escape_cell(u.get('url', '?')[:120])}</code></td><td class='small'>{params_str}</td></tr>")
                    parts.append("</tbody></table>")
                if su.get("all_urls"):
                    parts.append(f"<div class='muted small'>共发现 {len(su['all_urls'])} 个流URL</div>")
                parts.append("</div>")
            parts.append("</details>")

        # 房间解析信息
        if session.get("room_resolution_data"):
            parts.append(f"<details><summary><h3 style='display:inline'>🏠 房间解析信息</h3></summary>")
            for rr in session["room_resolution_data"]:
                parts.append("<div class='card'><div class='grid'>")
                parts.append(f"<div class='metric'><div class='muted small'>Web房间号</div><div>{escape_cell(str(rr.get('web_room_id', '—')))}</div></div>")
                parts.append(f"<div class='metric'><div class='muted small'>内部房间号</div><div>{escape_cell(str(rr.get('internal_room_id', '—')))}</div></div>")
                parts.append(f"<div class='metric'><div class='muted small'>直播状态</div><div>{escape_cell(str(rr.get('status_text', '—')))} (status={escape_cell(str(rr.get('room_status', '—')))})</div></div>")
                parts.append(f"<div class='metric'><div class='muted small'>解析来源</div><div>{escape_cell(str(rr.get('source', '—')))}</div></div>")
                parts.append("</div></div>")
            parts.append("</details>")

        # Playwright认证信息
        if session.get("playwright_data"):
            parts.append(f"<details><summary><h3 style='display:inline'>🌐 浏览器认证信息</h3> ({len(session['playwright_data'])} 项)</summary>")
            for pw in session["playwright_data"]:
                pw_type = pw.get("type") or ""
                pw_path = str(pw.get("_source_path", pw.get("path", pw.get("file", ""))))
                if not pw_type:
                    if "playwright_enter" in pw_path:
                        pw_type = "playwright_enter"
                    elif "playwright_cookies" in pw_path:
                        pw_type = "playwright_cookies"
                    else:
                        pw_type = "playwright_unknown"
                ts = pw.get("timestamp", "")
                parts.append(f"<div class='card'><h4>{escape_cell(pw_type)}</h4><div class='muted small'>{escape_cell(ts)}</div>")
                if pw_type == "playwright_cookies":
                    cookies = pw.get("cookies", [])
                    cookie_str = pw.get("cookie_string", "")
                    ms_token = pw.get("ms_token", "")
                    web_rid = pw.get("web_room_id", "")
                    if isinstance(cookies, list) and cookies:
                        parts.append(f"<div>获取到 {len(cookies)} 个Cookie</div>")
                        parts.append("<table><thead><tr><th>名称</th><th>值</th><th>域名</th></tr></thead><tbody>")
                        for c in cookies[:20]:
                            if isinstance(c, dict):
                                parts.append(f"<tr><td>{escape_cell(str(c.get('name', '')))}</td><td class='small'>{escape_cell(str(c.get('value', ''))[:50])}</td><td>{escape_cell(str(c.get('domain', '')))}</td></tr>")
                        parts.append("</tbody></table>")
                        if len(cookies) > 20:
                            parts.append(f"<div class='muted small'>... 共 {len(cookies)} 个Cookie</div>")
                    if cookie_str or ms_token or web_rid:
                        parts.append("<table><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>")
                        if web_rid:
                            parts.append(f"<tr><td>Web房间号</td><td>{escape_cell(str(web_rid))}</td></tr>")
                        if ms_token:
                            parts.append(f"<tr><td>ms_token</td><td class='small'>{escape_cell(str(ms_token)[:100])}</td></tr>")
                        if cookie_str:
                            parts.append(f"<tr><td>Cookie字符串</td><td class='small'>{escape_cell(str(cookie_str)[:200])}</td></tr>")
                        parts.append("</tbody></table>")
                elif pw_type == "playwright_enter":
                    parts.append("<table><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>")
                    for k, v in pw.items():
                        if k not in ("type", "timestamp", "_source_path"):
                            parts.append(f"<tr><td>{escape_cell(k)}</td><td>{escape_cell(str(v)[:200])}</td></tr>")
                    parts.append("</tbody></table>")
                parts.append("</div>")
            parts.append("</details>")

        # Session Manifest 步骤执行记录
        if session.get("manifest_data"):
            manifest = session["manifest_data"]
            steps = manifest.get("steps", {})
            parts.append(f"<details><summary><h3 style='display:inline'>📊 采集步骤执行记录</h3></summary>")
            parts.append("<div class='card'><table><thead><tr><th>步骤</th><th>状态</th><th>详情</th></tr></thead><tbody>")
            for step_name, step_info in steps.items():
                success = step_info.get("success", False)
                status_cls = "success" if success else "error"
                status_text = "成功" if success else "失败"
                label = step_info.get("label", step_name)
                detail_parts = []
                step_data = step_info.get("data", {})
                if isinstance(step_data, dict):
                    for k, v in step_data.items():
                        if k in ("multi_quality_analysis", "stream_url_struct"):
                            continue
                        sv = str(v)
                        if sv and sv != "None" and len(sv) < 150:
                            detail_parts.append(f"{escape_cell(k)}: {escape_cell(sv)}")
                if step_info.get("error"):
                    detail_parts.append(f"<span class='error'>错误: {escape_cell(step_info['error'])}</span>")
                detail_html = "<br>".join(detail_parts) if detail_parts else "—"
                parts.append(f"<tr><td>{escape_cell(label)}</td><td><span class='{status_cls}'>{status_text}</span></td><td class='small'>{detail_html}</td></tr>")
            parts.append("</tbody></table>")
            artifacts = manifest.get("artifacts", [])
            if artifacts:
                parts.append(f"<div style='margin-top:8px' class='muted small'>采集产物: {len(artifacts)} 个文件</div>")
            parts.append("</div></details>")

        parts.append("")

    parts.append("<div id='player-modal' style='display:none'>")
    parts.append(" <div class='pm-backdrop' onclick='closePlayer()'></div>")
    parts.append(" <div class='pm-panel'>")
    parts.append("  <div class='pm-head'><span id='pm-title'>▶ 实时播放</span><span id='pm-speed' class='muted small'></span><button class='pm-close' onclick='closePlayer()'>✕ 关闭</button></div>")
    parts.append("  <video id='pm-video' controls autoplay playsinline></video>")
    parts.append("  <div id='pm-status' class='pm-status'>点击任意「▶ 播放」按钮或拉流地址即可实时播放（flv.js / hls.js 双内核）</div>")
    parts.append("  <div id='pm-hint' class='muted small' style='margin-top:6px'></div>")
    parts.append(" </div>")
    parts.append("</div>")
    parts.append("<script>")
    parts.append("(function(){function copyText(t,b){function ok(){b.textContent='已复制 ✓';setTimeout(function(){b.textContent='复制';},1600);}")
    parts.append("function fail(){b.textContent='复制失败';setTimeout(function(){b.textContent='复制';},1600);}")
    parts.append("function legacy(){var ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity='0';")
    parts.append("document.body.appendChild(ta);ta.focus();ta.select();var d=false;try{d=document.execCommand('copy');}catch(e){d=false;}")
    parts.append("document.body.removeChild(ta);d?ok():fail();}")
    parts.append("if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(ok,function(){legacy();});}else{legacy();}}")
    parts.append("document.addEventListener('click',function(e){var b=e.target;")
    parts.append("while(b&&b!==document.body&&!(b.classList&&b.classList.contains('copy-btn'))){b=b.parentElement;}")
    parts.append("if(b&&b.classList&&b.classList.contains('copy-btn')){copyText(b.getAttribute('data-copy')||'',b);}});})();")
    parts.append("</script>")
    parts.append("<script>")
    parts.append("var PM={v:null,kind:null,pl:null};")
    parts.append("function pmEl(i){return document.getElementById(i)}")
    parts.append("function pmStatus(t){pmEl('pm-status').textContent=t}")
    parts.append("function loadScript(src,cb){var s=document.createElement('script');s.src=src;s.onload=function(){cb(true)};s.onerror=function(){cb(false)};document.head.appendChild(s)}")
    parts.append("function pmDestroy(){var P=PM.pl;try{if(P){if(PM.kind==='flv'){P.pause();P.unload();P.detachMediaElement();P.destroy();}else if(PM.kind==='hls'){P.destroy();}}}catch(e){}PM.pl=null;PM.kind=null;var v=PM.v;if(v){try{v.pause();v.removeAttribute('src');v.load();}catch(e){}}var sp=pmEl('pm-speed');if(sp){sp.textContent=''}}")
    parts.append("function closePlayer(){pmDestroy();pmEl('player-modal').style.display='none'}")
    parts.append("function pmHuman(d){d=String(d||'');if(/403|forbidden|expired|signature/i.test(d)){return '拉流地址已过期或签名失效（地址一般7天有效），请重新运行分析获取新地址'}if(/404|notfound/i.test(d)){return '流不存在：直播间可能已下播'}if(/codec|hevc|h265|hvc1|notsupport/i.test(d)){return '该流为 H.265/HEVC 等编码，网页端 flv.js 无法解码——请改用蓝光/超清等 H.264 转码档，或复制地址用 VLC / ffplay 播放'}if(/network|cors|fetch|abort|timeout|neterr/i.test(d)){return '网络错误或被浏览器拦截：请运行 ./serve_report.sh 后用 http://localhost 打开'}return d||'未知错误'}")
    parts.append("function playUrl(url,proto,label){var m=pmEl('player-modal');m.style.display='flex';PM.v=pmEl('pm-video');pmEl('pm-title').textContent='▶ 实时播放 — '+label;pmEl('pm-hint').textContent=(location.protocol==='file:')?'提示：当前以 file:// 打开。抖音拉流 CDN 已放行跨域（Access-Control-Allow-Origin: *），一般可直接播放；若失败请运行 ./serve_report.sh 改用 http://localhost 打开。':'';pmDestroy();if(!url){pmStatus('❌ 无效地址');return}if(!window.MediaSource){pmStatus('❌ 浏览器不支持 MSE，请使用 Chrome / Edge / Firefox');return}pmStatus('连接中… '+url.slice(0,90));if(proto==='flv'){if(typeof flvjs==='undefined'){pmStatus('flv.js 未加载，尝试从 CDN 回退加载…');loadScript('https://cdn.jsdelivr.net/npm/flv.js@1.6.2/dist/flv.min.js',function(ok){ok?startFlv(url):pmStatus('❌ flv.js 加载失败：assets/flv.min.js 缺失且 CDN 不可达')})}else{startFlv(url)}}else if(proto==='hls'){if(typeof Hls==='undefined'){pmStatus('hls.js 未加载，尝试从 CDN 回退加载…');loadScript('https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js',function(ok){ok?startHls(url):pmStatus('❌ hls.js 加载失败：assets/hls.min.js 缺失且 CDN 不可达')})}else{startHls(url)}}else{pmStatus('❌ 协议 '+proto+' 暂不支持网页直接播放（LLS/DASH/CMAF 需专用播放器）')}}")
    parts.append("function startFlv(url){PM.kind='flv';if(!flvjs.isSupported()){pmStatus('❌ 浏览器不支持 MSE 流式 FLV 播放，建议 Chrome/Edge/Firefox');return}var P=flvjs.createPlayer({type:'flv',isLive:true,url:url},{enableWorker:false,enableStashBuffer:false,stashInitialSize:128,lazyLoad:false,autoCleanupSourceBuffer:true,fixAudioTimestampGap:false});PM.pl=P;P.attachMediaElement(PM.v);P.on(flvjs.Events.MEDIA_INFO,function(mi){pmStatus('✅ 解码中: '+(mi.width||'?')+'x'+(mi.height||'?')+' | '+(mi.videoCodec||'?')+' / '+(mi.audioCodec||'?')+(mi.hasVideo===false?' | 纯音频流':''))});P.on(flvjs.Events.STATISTICS_INFO,function(s){var el=pmEl('pm-speed');if(el){el.textContent='⬇ '+(s.speed||0)+' KB/s'}});P.on(flvjs.Events.ERROR,function(t,d){pmStatus('❌ ['+t+'/'+d+'] '+pmHuman(d))});P.load();var pr=PM.v.play();if(pr&&pr.catch){pr.catch(function(){PM.v.muted=true;PM.v.play().then(function(){pmStatus('🔇 已静音自动播放（点击视频音量图标取消静音）')}).catch(function(e2){pmStatus('⚠️ 请手动点击视频播放按钮：'+pmHuman(e2.name))})})}}")
    parts.append("function startHls(url){PM.kind='hls';var v=PM.v;if(v.canPlayType('application/vnd.apple.mpegurl')&&typeof Hls==='undefined'){v.src=url;var pp=v.play();if(pp&&pp.catch){pp.catch(function(){})}pmStatus('✅ 浏览器原生 HLS 播放');return}if(typeof Hls==='undefined'||!Hls.isSupported()){pmStatus('❌ 浏览器不支持 MSE HLS 播放');return}var h=new Hls({lowLatencyMode:true,liveSyncDurationCount:3,maxBufferLength:12,backBufferLength:30});PM.pl=h;h.attachMedia(v);h.on(Hls.Events.MANIFEST_PARSED,function(){var pp=v.play();if(pp&&pp.catch){pp.catch(function(){})}});h.on(Hls.Events.LEVEL_SWITCHED,function(_,d){var lv=(h.levels||[])[d.level];var w=(lv&&lv.width)||PM.v.videoWidth||'?',hh=(lv&&lv.height)||PM.v.videoHeight||'?';pmStatus('✅ HLS 播放中: '+w+'x'+hh+((lv&&lv.codecs)?' '+lv.codecs:''))});h.on(Hls.Events.ERROR,function(_,d){if(!d.fatal){return}var msg='❌ ['+d.details+'] '+pmHuman(d.details);if(d.type===Hls.ErrorTypes.NETWORK_ERROR){pmStatus(msg);h.destroy();PM.pl=null;PM.kind=null}else if(d.type===Hls.ErrorTypes.MEDIA_ERROR){pmStatus('⚠️ 媒体错误，自动恢复中…');h.recoverMediaError()}else{pmStatus(msg);h.destroy();PM.pl=null;PM.kind=null}});h.loadSource(url)}")
    parts.append("document.addEventListener('click',function(e){var b=e.target;while(b&&b!==document.body&&!(b.classList&&(b.classList.contains('play-btn')||b.classList.contains('url-cell')))){b=b.parentElement}if(b&&b.classList&&(b.classList.contains('play-btn')||b.classList.contains('url-cell'))){if(b.tagName==='CODE'){e.preventDefault()}playUrl(b.getAttribute('data-url')||'',b.getAttribute('data-proto')||'flv',b.getAttribute('data-label')||'拉流地址')}});")
    parts.append("</script>")
    _cta_href = f"https://live.douyin.com/{_global_web_rid}" if _global_web_rid and _global_web_rid != "—" else "https://live.douyin.com/"
    parts.append(f"<a class='cta-btn' href='{escape_cell(_cta_href)}' target='_blank'>前往抖音直播</a>")
    parts.append("</div></body></html>")
    try:
        Path(output_path).write_text("".join(parts), encoding="utf-8")
    except OSError as e:
        logger.error("保存 HTML 报告失败: %s", e)


def enrich_sessions(sessions):
    for session in sessions:
        session["reflow_data"] = []
        session["flv_data"] = []
        session["rtmp_data"] = []
        session["dual_data"] = []
        session["ffprobe_data"] = []
        session["probe_data"] = []
        session["stream_url_data"] = []
        session["room_resolution_data"] = []
        session["playwright_data"] = []
        session["manifest_data"] = None

        for item in session.get("reflow_files", []):
            data = load_reflow_data(item)
            session["reflow_data"].append(data)
            if data.get("room_id"):
                session.setdefault("room_ids", [])
                if data["room_id"] not in session["room_ids"]:
                    session["room_ids"].append(data["room_id"])

        seen_stream_urls = set()
        # 优先使用 JSON 数据项（含完整 encoder_hints），文本报告作为兜底
        sorted_reports = sorted(
            session.get("flv_reports", []),
            key=lambda x: 0 if isinstance(x, dict) and x.get("source") == "json" else 1,
        )
        for item in sorted_reports:
            data = load_flv_report(item)
            # 去重：同一个 stream_url 只保留一条（避免 flv_analysis + flv_metadata_analysis 重复输出）
            url_key = data.get("stream_url") or ""
            if url_key and url_key in seen_stream_urls:
                continue
            if url_key:
                seen_stream_urls.add(url_key)
            session["flv_data"].append(data)
            if data.get("room_id"):
                session.setdefault("room_ids", [])
                if data["room_id"] not in session["room_ids"]:
                    session["room_ids"].append(data["room_id"])

        for item in session.get("rtmp_reports", []):
            session["rtmp_data"].append(load_rtmp_report(item))

        for item in session.get("dual_files", []):
            session["dual_data"].append(load_dual_data(item))

        for item in session.get("ffprobe_files", []):
            session["ffprobe_data"].append(load_ffprobe_data(item))

        # 加载音频轨道分析（取最新一份）
        for item in reversed(session.get("audio_track_files", [])):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["audio_track_analysis"] = json.load(f)
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效音频分析文件 %s: %s", path, exc)


        # 加载人脸嘴型音画同步（内容级，取最新一份）
        for item in reversed(session.get("lip_sync_files", [])):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["lip_sync_analysis"] = json.load(f)
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效嘴型同步文件 %s: %s", path, exc)
        # 加载音画同步检测（取最新一份）
        for item in reversed(session.get("av_sync_files", [])):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["av_sync_analysis"] = json.load(f)
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效音画同步文件 %s: %s", path, exc)

        # 加载探针数据（gift_list / room_info / ranklist）
        for item in session.get("probe_files", []):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        probe = json.load(f)
                    # 从文件名和 discover 数据补全缺失字段
                    if not probe.get("type"):
                        probe["type"] = item.get("data", {}).get("type", "unknown")
                    if "ok" not in probe and probe.get("raw") is not None:
                        probe["ok"] = True
                    session["probe_data"].append(probe)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效探针文件 %s: %s", path, exc)

        # 加载流URL结构数据
        for item in session.get("stream_url_files", []):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["stream_url_data"].append(json.load(f))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效流URL文件 %s: %s", path, exc)

        # 加载房间解析数据
        for item in session.get("room_resolution_files", []):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["room_resolution_data"].append(json.load(f))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效房间解析文件 %s: %s", path, exc)

        # 加载Playwright认证数据
        for item in session.get("playwright_files", []):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        pw_data = json.load(f)
                    pw_data["_source_path"] = str(path)
                    session["playwright_data"].append(pw_data)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效Playwright文件 %s: %s", path, exc)

        # 加载session manifest（取最新一份）
        for item in reversed(session.get("session_manifest_files", [])):
            path = item.get("path") or item.get("file")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        session["manifest_data"] = json.load(f)
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("跳过无效manifest文件 %s: %s", path, exc)

        session["room_ids"] = sorted(set(session.get("room_ids", [])))
        session["primary_room_id"] = session["room_ids"][0] if session["room_ids"] else None
        session["push_conclusion"] = generate_push_conclusion(session)
        session["dual_conclusion"] = generate_dual_conclusion(session.get("dual_data", []))
    return sessions


def merge_results_into_files_data(files_data, json_results):
    """将结构化 JSON 结果并入统一会话输入。"""
    merged = {key: list(value) for key, value in files_data.items()}
    merged.setdefault("reflow_json", [])
    merged.setdefault("flv_reports", [])
    merged.setdefault("rtmp_reports", [])
    merged.setdefault("dual_reports", [])
    merged.setdefault("ffprobe_files", [])

    merged["reflow_json"].extend(json_results.get("reflow", []))
    merged["flv_reports"].extend(json_results.get("flv", []))
    merged["flv_reports"].extend(json_results.get("metadata", []))
    merged["rtmp_reports"].extend(json_results.get("rtmp", []))
    merged["dual_reports"].extend(json_results.get("dual", []))
    merged["ffprobe_files"].extend(json_results.get("ffprobe", []))
    merged.setdefault("audio_track", [])
    merged["audio_track"].extend(json_results.get("audio_track", []))
    merged.setdefault("av_sync", [])
    merged["av_sync"].extend(json_results.get("av_sync", []))
    merged.setdefault("lip_sync", [])
    merged["lip_sync"].extend(json_results.get("lip_sync", []))
    merged.setdefault("probe_files", [])
    merged["probe_files"].extend(json_results.get("probe", []))
    merged.setdefault("stream_url", [])
    merged["stream_url"].extend(json_results.get("stream_url", []))
    merged.setdefault("room_resolution", [])
    merged["room_resolution"].extend(json_results.get("room_resolution", []))
    merged.setdefault("playwright", [])
    merged["playwright"].extend(json_results.get("playwright", []))
    merged.setdefault("session_manifest", [])
    merged["session_manifest"].extend(json_results.get("session_manifest", []))

    for key in merged:
        merged[key] = sorted(merged[key], key=lambda item: item["timestamp"])
    return merged


def _item_matches_room(item, target_room_id):
    """检查单个数据条目是否匹配目标房间号。"""
    rid = str(item.get("room_id", "") or "").strip()
    return rid == target_room_id


def filter_sessions_by_room_id(sessions, room_id=None):
    """按房间号过滤会话，并清理 session 内不匹配的数据条目。"""
    if not room_id:
        return sessions
    room_id = str(room_id)
    filtered = []
    for session in sessions:
        room_ids = {str(item) for item in session.get("room_ids", []) if item}
        for flv in session.get("flv_data", []):
            if flv.get("room_id"):
                room_ids.add(str(flv.get("room_id")))
        for reflow in session.get("reflow_data", []):
            if reflow.get("room_id"):
                room_ids.add(str(reflow.get("room_id")))
        if room_id not in room_ids:
            continue
        # session 匹配 → 清理内部数据，只保留匹配的条目
        if len(room_ids) > 1:
            import copy
            session = copy.deepcopy(session)
            session["reflow_data"] = [d for d in session.get("reflow_data", []) if _item_matches_room(d, room_id)]
            session["flv_data"] = [d for d in session.get("flv_data", []) if _item_matches_room(d, room_id)]
            session["rtmp_data"] = [d for d in session.get("rtmp_data", []) if _item_matches_room(d, room_id)]
            session["dual_data"] = [d for d in session.get("dual_data", []) if _item_matches_room(d, room_id)]
            session["ffprobe_data"] = [d for d in session.get("ffprobe_data", []) if _item_matches_room(d, room_id)]
            # probe_data 可能不含 room_id，无法按房间过滤，保留全部
            session["room_ids"] = [room_id]
            session["primary_room_id"] = room_id
            session["push_conclusion"] = generate_push_conclusion(session)
        filtered.append(session)
    return filtered


def main():
    parser = argparse.ArgumentParser(description="抖音直播采集结果统一报告生成器")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="采集结果目录")
    parser.add_argument("--report-dir", default=str(REPORT_DIR), help="报告输出目录")
    parser.add_argument("--room-id", help="按房间号过滤")
    parser.add_argument("--format", choices=["html", "text", "both"], default="both", help="报告格式")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    try:
        stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
        stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
        if callable(stdout_reconfigure):
            stdout_reconfigure(encoding="utf-8")
        if callable(stderr_reconfigure):
            stderr_reconfigure(encoding="utf-8")
    except Exception:
        pass

    ensure_dirs()
    output_dir = Path(args.output_dir)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    if not output_dir.exists():
        logging.warning("未找到采集目录: %s", output_dir)
        logging.warning("请先运行 monitor_reflow.py / flv_analyzer.py / rtmp_capture.py 生成抓取结果")
        return

    json_results = discover_json_results(output_dir, room_id=args.room_id)
    files_data = merge_results_into_files_data(scan_captures(output_dir), json_results)
    total_files = sum(len(items) for items in files_data.values())
    if total_files == 0:
        logging.warning("采集目录为空: %s", output_dir)
        logging.warning("请先生成 JSON 结果、reflow JSON、FLV 报告或 RTMP 报告后再运行本脚本")
        return

    logging.info("采集目录: %s", output_dir)
    logging.info("共发现文件: %s", total_files)
    for key, items in files_data.items():
        logging.info("  - %s: %s", key, len(items))

    sessions = correlate_by_time(files_data, tolerance_seconds=300)
    sessions = enrich_sessions(sessions)
    sessions = filter_sessions_by_room_id(sessions, args.room_id)
    report_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_folder = report_dir / f"unified_report_{report_ts}"
    report_folder.mkdir(parents=True, exist_ok=True)

    # 收集所有证据文件并复制到报告文件夹的 evidence/ 子目录
    evidence_dir = report_folder / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    evidence_filenames = {}  # original_path_str → 相对路径（evidence/<fname>）
    for session in sessions:
        for key in ["reflow_files", "flv_reports", "rtmp_reports", "dual_files"]:
            for item in session.get(key, []):
                src = item.get("path")
                if not src or not Path(src).exists():
                    continue
                fname = item.get("name") or Path(src).name
                # 避免同名文件冲突
                dest = evidence_dir / fname
                if dest.exists() and str(src) not in evidence_filenames:
                    stem = Path(fname).stem
                    suffix = Path(fname).suffix
                    counter = 1
                    while dest.exists():
                        fname = f"{stem}_{counter}{suffix}"
                        dest = evidence_dir / fname
                        counter += 1
                if str(src) not in evidence_filenames:
                    shutil.copy2(str(src), str(dest))
                    evidence_filenames[str(src)] = f"evidence/{fname}"

    # 将证据文件名注入到各 session 的数据项中，供 HTML 生成时使用
    for session in sessions:
        for key in ["reflow_files", "flv_reports", "rtmp_reports", "dual_files"]:
            for item in session.get(key, []):
                src = str(item.get("path", ""))
                if src in evidence_filenames:
                    item["evidence_name"] = evidence_filenames[src]
        for data_list_key in ["reflow_data", "flv_data", "rtmp_data", "dual_data"]:
            for data_item in session.get(data_list_key, []):
                src = str(data_item.get("path", ""))
                if src in evidence_filenames:
                    data_item["evidence_name"] = evidence_filenames[src]

    # 内嵌播放器依赖（flv.js / hls.js）随报告分发
    assets_dir = report_folder / "assets"
    assets_dir.mkdir(exist_ok=True)
    for _lib in ("flv.min.js", "hls.min.js"):
        _src = BASE_DIR / "report_assets" / _lib
        if _src.exists():
            shutil.copy2(str(_src), str(assets_dir / _lib))
        else:
            logging.warning("缺少内嵌播放器依赖: %s（应位于 report_assets/）", _lib)

    text_path = report_folder / "report.txt"
    html_path = report_folder / "index.html"

    if args.format in {"text", "both"}:
        generate_text_report(sessions, text_path)
        logging.info("文本报告已生成: %s", text_path)
    if args.format in {"html", "both"}:
        generate_html_report(sessions, html_path)
        logging.info("HTML 报告已生成: %s", html_path)

    if not sessions:
        logging.warning("按当前条件未筛选到可输出的会话")

    print(f"[*] 已生成会话数: {len(sessions)}")
    if args.format in {"text", "both"}:
        print(f"[*] 文本报告: {text_path}")
    if args.format in {"html", "both"}:
        print(f"[*] HTML 报告: {html_path}")
        _print_report_path_hints(html_path)
    print("\n── 推流端识别摘要 ──")
    for session in sessions:
        conclusion = session.get("push_conclusion", {})
        print(
            f"  {session['session_id']} | room_id={session.get('primary_room_id') or '未知'} | "
            f"软件={conclusion.get('software', '未知')} | 置信度={conclusion.get('confidence', '低')} | "
            f"第三方={format_bool_cn(conclusion.get('third_party'))} | CDN={conclusion.get('cdn', '未知')}"
        )


if __name__ == "__main__":
    main()
