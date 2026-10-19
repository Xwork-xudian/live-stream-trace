#!/usr/bin/env python3
"""报告 + 流代理的本地 HTTP 服务。

为什么需要它：
  · 部分平台 CDN（如哔哩哔哩 bilivideo.com）**强制校验 Referer**，浏览器从 file:// 或
    localhost 打开的页面无法伪造 Referer → 播放 403。
  · 本服务把报告里的播放地址改写为 `/p?u=<原始地址>`，由服务端带正确 Referer 与
    CORS 头转发，从而让网页播放器可以播放这些流。

用法：
    python core/serve.py [端口] [报告目录] [--open]
"""
from __future__ import annotations

import argparse
import functools
import re
import sys
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "platforms"))

# 地址刷新缓存：(platform, room, quality, proto, codec) -> (ts, url)
_URL_CACHE: dict = {}
_CACHE_TTL = 30   # 秒；平台地址通常分钟级有效，缓存 30s 足够


def refresh_stream_url(platform: str, room: str, quality: str, proto: str, codec: str = ""):
    """按需向平台适配器要一个新地址（B 站等 URL 绑定推流会话会失效）。"""
    key = (platform, room, quality, proto, codec)
    hit = _URL_CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        from config import platform_registry
        adapter = platform_registry().get(platform)
        url = adapter.refresh_tier(room, quality, proto, codec) if adapter else None
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] 刷新地址失败 {platform}/{room}/{quality}: {exc}", flush=True)
        url = None
    if url:
        _URL_CACHE[key] = (time.time(), url)
        print(f"[proxy] 刷新地址 {platform} {quality} -> {url[:80]}", flush=True)
    return url
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# CDN 域名 → 该平台要求的 Referer（浏览器无法伪造，只能由代理补）
REFERER_MAP = {
    "bilivideo.com": "https://live.bilibili.com/",
    "hdslb.com": "https://live.bilibili.com/",
    "xhscdn.com": "https://www.xiaohongshu.com/",
    "douyincdn.com": "https://live.douyin.com/",
    "douyinliving.com": "https://live.douyin.com/",
    "bilivideo.cn": "https://live.bilibili.com/",
}

DATA_URL_RE = re.compile(r"data-url='(https?://[^']+)'")


def referer_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for key, ref in REFERER_MAP.items():
        if host.endswith(key) or key in host:
            return ref
    return ""


class Handler(SimpleHTTPRequestHandler):
    """静态报告 + /p?u= 流代理 + HTML 播放地址改写。"""

    def log_message(self, fmt, *args):  # 静音
        pass

    # ── 流代理 ──
    def _proxy(self, query: str) -> None:
        params = parse_qs(query)
        platform = (params.get("p") or [""])[0]
        room = (params.get("room") or [""])[0]
        quality = (params.get("q") or [""])[0]
        proto = (params.get("proto") or ["flv"])[0]
        codec = (params.get("codec") or [""])[0]
        target = unquote((params.get("u") or [""])[0])
        if platform and room and quality:
            fresh = refresh_stream_url(platform, room, quality, proto, codec)
            if fresh:
                target = fresh
        if not target.startswith("http"):
            self.send_error(400, "bad request: need u=<url> or p/room/q")
            return
        headers = {"User-Agent": UA, "Accept": "*/*"}
        ref = referer_for(target)
        if ref:
            headers["Referer"] = ref
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        # 上游偶发 403/5xx（CDN 边缘抖动）→ 重试
        upstream = None
        for attempt in range(3):
            try:
                upstream = requests.get(target, headers=headers, stream=True, timeout=20)
            except requests.RequestException as exc:
                print(f"[proxy] 上游异常 attempt={attempt+1}: {exc}", flush=True)
                continue
            if upstream.status_code in (200, 206):
                break
            print(f"[proxy] 上游 {upstream.status_code} attempt={attempt + 1} host={urlparse(target).netloc}", flush=True)
            upstream.close()
            upstream = None
        if upstream is None:
            # 注意：send_error 的 reason 只能是 latin-1（不能放中文）
            self.send_error(502, "upstream rejected (CDN 403/5xx); use ./play_source.sh with VLC/ffplay")
            return
        try:
            self.send_response(upstream.status_code)
            for k, v in upstream.headers.items():
                if k.lower() in ("content-type", "content-length", "accept-ranges", "content-range",
                                 "cache-control", "etag", "last-modified"):
                    self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()
            for chunk in upstream.iter_content(65536):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    # ── 静态文件（HTML 改写播放地址）──
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/p?"):
            return self._proxy(self.path[3:])
        path = self.path.split("?")[0]
        fs_path = Path(self.translate_path(path))
        if fs_path.is_dir():
            fs_path = fs_path / "index.html"
        if fs_path.is_file() and fs_path.suffix == ".html":
            html = fs_path.read_text(encoding="utf-8", errors="replace")
            html = DATA_URL_RE.sub(lambda m: f"data-url='/p?u={quote(m.group(1), safe='')}'", html)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


def main() -> int:
    ap = argparse.ArgumentParser(description="报告 + 流代理本地服务")
    ap.add_argument("port", nargs="?", type=int, default=8899)
    ap.add_argument("report", nargs="?", help="报告目录（含 index.html）；默认取最新")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()

    if args.report:
        report_dir = Path(args.report).resolve()
    else:
        candidates = sorted((ROOT / "reports").glob("unified_report_*/index.html"))
        if not candidates:
            print("未找到报告（reports/unified_report_*/index.html）", file=sys.stderr)
            return 2
        report_dir = candidates[-1].parent
    serve_root = report_dir.parent
    rel = report_dir.name

    handler = functools.partial(Handler, directory=str(serve_root))
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    url = f"http://localhost:{args.port}/{rel}/index.html"
    print(f"✅ 报告服务已启动: {url}")
    print(f"   报告目录: {report_dir}")
    print(f"   流代理: /p?u=<原始地址>（自动补 Referer + CORS）")
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
