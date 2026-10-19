#!/usr/bin/env python3
"""live-stream-trace 网页系统：**一框输入 → 解析 → 生成报告 → 历史可查**。

只用标准库（http.server）+ requests，天然复用 core/serve.py 的报告静态服务与流代理。

路由：
  GET  /                    单页 UI（输入框 / 解析 / 分析 / 实时日志 / 报告 / 历史）
  POST /api/resolve         分享文本·口令·短链·链接 → 直播间链接
  POST /api/analyze         建分析任务（后台子进程跑 live_trace.py）→ job_id
  GET  /api/jobs            任务列表
  GET  /api/jobs/<id>       任务状态 + 日志尾部 + 报告地址
  GET  /api/history         历史报告（扫描 reports/）
  GET  /unified_report_*/…  报告静态文件（含 /p?u= 流代理与地址刷新）

用法：
    python core/web_ui.py [端口] [--open]
    ./start_web.sh 8899
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "platforms"))

import serve  # noqa: E402  （复用报告静态服务 + 流代理）
from sync_ui_page import SYNC_HTML  # noqa: E402  （音画同步上传页）
from live_ui_page import LIVE_HTML  # noqa: E402  （实时音画延迟监测页）
from live_monitor import LiveMonitor  # noqa: E402  （增量 FLV 解析 + 0.5s 快照）

MONITOR = LiveMonitor()   # 实时监测会话（进程内）

REPORT_DIR = ROOT / "reports"
JOBS_DIR = REPORT_DIR / ".jobs"
UPLOADS_DIR = ROOT / "uploads"                 # 上传样本落盘目录
MAX_UPLOAD_MB = 300                            # 单个上传上限（MB）
LOG_TAIL = 20000

logger = logging.getLogger("web_ui")

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>直播流分析 · 网页系统</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--card2:#11151d;--line:#2b2f3a;--fg:#e6e6e6;--muted:#8b97a8;
      --brand:#2a9d8f;--ok:#4caf50;--warn:#ffb300;--err:#ef5350}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:Segoe UI,Arial,'Microsoft YaHei',sans-serif;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h3{font-size:15px;margin:0 0 12px;color:#9fb0c8}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:16px 0}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
textarea{width:100%;min-height:96px;background:var(--card2);color:var(--fg);border:1px solid var(--line);
         border-radius:10px;padding:12px;font-family:inherit;font-size:14px;resize:vertical}
input,select{background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
button{background:var(--brand);color:#0b0d10;border:0;border-radius:8px;padding:10px 18px;font-weight:700;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line);font-weight:500}
button:disabled{opacity:.5;cursor:not-allowed}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.metric{background:var(--card2);border:1px solid #232833;border-radius:10px;padding:10px}
.muted{color:var(--muted)} .small{font-size:12.5px} .val{font-weight:600;word-break:break-all}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#0b0d10}
pre{background:var(--card2);border:1px solid #232833;border-radius:10px;padding:12px;max-height:340px;
    overflow:auto;font-size:12.5px;white-space:pre-wrap;word-break:break-word}
table{width:100%;border-collapse:collapse} th,td{border-bottom:1px solid #232833;padding:8px 6px;text-align:left;font-size:13.5px}
th{color:#9fb0c8;font-weight:600}
iframe{width:100%;height:620px;border:1px solid var(--line);border-radius:12px;background:#fff}
a{color:var(--brand)}
ol,ul{padding-left:20px;margin:6px 0}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 8px}
</style></head><body><div class="wrap">
<h1>直播流分析 · 网页系统</h1>
<div class="muted small">一框输入：直播间链接 / 分享链接 / 淘口令 / 京东口令 / 房间号 / rtmp:// 源流 —— 自动解析并生成报告</div>
<div class="small" style="margin-top:6px">🎬 <a href="/sync">音画同步检测（上传本地视频样本）</a></div>

<div class="card">
  <h3>① 输入</h3>
  <textarea id="input" placeholder="例：
https://live.bilibili.com/5440
【京东】https://3.cn/xxxxxxx 「红包雨派发中…」点击链接直接打开
【淘宝】666:/🗝AbCd1234EfGh《 示例主播的直播…
rtmp://127.0.0.1:1935/live/streamkey"></textarea>
  <div class="row" style="margin-top:12px">
    <label class="small muted">平台 <select id="platform"><option value="auto">自动识别</option>
      <option>bilibili</option><option>douyin</option><option>xiaohongshu</option>
      <option>taobao</option><option>jd</option><option>rtmp</option></select></label>
    <label class="small muted">最大下载字节 <input id="maxbytes" type="number" value="3000000" style="width:120px"></label>
    <label class="small muted">RTMP 采样秒数 <input id="capsec" type="number" value="20" style="width:80px"></label>
    <label class="small muted"><input id="avsync" type="checkbox" checked> 时间戳级音画同步</label>
    <label class="small muted"><input id="lipsync" type="checkbox" checked> 人脸嘴型（内容级）</label>
  </div>
  <div class="row" style="margin-top:12px">
    <button id="btnResolve" class="ghost">② 解析链接</button>
    <button id="btnRun">③ 开始分析</button>
    <span id="hint" class="small muted"></span>
  </div>
</div>

<div class="card" id="resolveCard" style="display:none">
  <h3>解析结果</h3><div id="resolveBody"></div>
</div>

<div class="card" id="jobCard" style="display:none">
  <h3>分析任务 <span id="jobBadge" class="badge" style="background:#8b97a8">排队中</span></h3>
  <div class="grid" id="jobMeta"></div>
  <div class="row" style="margin-top:10px"><span class="small muted">实时日志</span>
    <button class="ghost small" onclick="copyLog()">复制日志</button></div>
  <pre id="log"></pre>
  <div id="reportBox" style="display:none">
    <div class="row" style="margin:10px 0"><strong>报告</strong>
      <a id="reportLink" target="_blank">在新窗口打开</a></div>
    <iframe id="reportFrame"></iframe>
  </div>
</div>

<div class="card">
  <h3>历史报告 <button class="ghost small" onclick="loadHistory()">刷新</button></h3>
  <table><thead><tr><th>时间</th><th>平台</th><th>房间</th><th>标题</th><th>状态</th><th>操作</th></tr></thead>
  <tbody id="history"></tbody></table>
</div>
<div class="muted small">仅供学习与技术研究使用。</div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let currentJob = null, timer = null;

async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return await r.json();
}
function esc(s){ return (s===null||s===undefined)?'':String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function metric(label,val){ return `<div class="metric"><div class="muted small">${esc(label)}</div><div class="val">${esc(val??'—')}</div></div>`; }

async function doResolve(){
  const text = $('input').value.trim();
  if(!text){ $('hint').textContent='请先输入内容'; return; }
  $('hint').textContent='解析中…';
  const res = await api('/api/resolve', {text});
  $('resolveCard').style.display='';
  let h = '<div class="grid">' + metric('类型', res.kind) + metric('平台', res.platform_name||'未识别')
        + metric('房间号', res.room_id||'—') + metric('口令码', res.password_code||'—') + '</div>';
  if(res.url) h += `<div class="metric" style="margin-top:10px"><div class="muted small">直播间链接</div>
        <div class="val"><a href="${esc(res.url)}" target="_blank">${esc(res.url)}</a></div></div>`;
  if(res.steps?.length) h += `<div style="margin-top:10px"><div class="muted small">解析步骤</div><ul class="small">${res.steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></div>`;
  if(res.notes?.length) h += `<div style="margin-top:8px" class="small">${res.notes.map(s=>`ℹ️ ${esc(s)}`).join('<br>')}</div>`;
  if(res.manual?.length) h += `<div class="card" style="background:#1a1416;border-color:#5a3030;margin-top:10px">
        <div class="small"><strong>需要一步手动操作：</strong><ol>${res.manual.map(s=>`<li>${esc(s)}</li>`).join('')}</ol></div></div>`;
  $('resolveBody').innerHTML = h;
  $('hint').textContent = res.ok ? '解析成功，可开始分析' : '未能直接解析（见上方指引）';
  return res;
}

async function doRun(){
  const text = $('input').value.trim();
  if(!text){ $('hint').textContent='请先输入内容'; return; }
  $('btnRun').disabled = true; $('hint').textContent='已提交任务…';
  const res = await api('/api/analyze', {
    text, platform: $('platform').value, max_bytes: Number($('maxbytes').value)||3000000,
    capture_seconds: Number($('capsec').value)||20,
    av_sync: $('avsync').checked, lip_sync: $('lipsync').checked
  });
  if(res.error || !res.job){ $('btnRun').disabled=false; $('hint').textContent = res.error || '提交失败';
    if(res.resolution){ $('resolveCard').style.display=''; $('resolveBody').innerHTML =
      `<div class="small">${(res.resolution.manual||[]).map(s=>'👉 '+esc(s)).join('<br>')}</div>`; }
    return; }
  currentJob = res.job.id; $('jobCard').style.display='';
  $('reportBox').style.display='none'; $('log').textContent='';
  pollJob();
}

function badge(status){ const map={queued:['排队中','#8b97a8'],running:['分析中','#ffb300'],done:['完成','#4caf50'],failed:['失败','#ef5350']};
  const [t,c]=map[status]||['未知','#8b97a8']; $('jobBadge').textContent=t; $('jobBadge').style.background=c; }

async function pollJob(){
  if(!currentJob) return;
  const j = await api('/api/jobs/'+currentJob);
  if(j.error){ return; }
  badge(j.status);
  $('jobMeta').innerHTML = metric('目标', j.url) + metric('平台', j.platform) + metric('开始', j.started||'—')
     + metric('耗时', j.elapsed_s!=null ? j.elapsed_s+' s' : '—') + metric('退出码', j.returncode ?? '—');
  $('log').textContent = j.log_tail || '';
  $('log').scrollTop = $('log').scrollHeight;
  if(j.report_url && $('reportBox').style.display==='none'){
     $('reportBox').style.display=''; $('reportLink').href=j.report_url; $('reportFrame').src=j.report_url;
     loadHistory();
  }
  if(j.status==='done'||j.status==='failed'){ $('btnRun').disabled=false; $('hint').textContent='任务结束'; return; }
  timer = setTimeout(pollJob, 1500);
}
function copyLog(){ navigator.clipboard?.writeText($('log').textContent); }

async function loadHistory(){
  const data = await api('/api/history');
  $('history').innerHTML = (data.reports||[]).map(r=>`<tr>
    <td class="small">${esc(r.time)}</td><td>${esc(r.platform_name||r.platform||'—')}</td>
    <td class="small">${esc(r.room_id||'—')}</td><td class="small">${esc(r.title||'—')}</td>
    <td class="small">${esc(r.status||'—')}</td>
    <td><a href="${esc(r.report_url)}" target="_blank">查看</a></td></tr>`).join('')
    || '<tr><td colspan="6" class="muted small">暂无历史</td></tr>';
}

$('btnResolve').onclick = doResolve;
$('btnRun').onclick = doRun;
loadHistory();
setInterval(()=>{ if(!currentJob) loadHistory(); }, 10000);
</script></body></html>
"""


# ─────────────────────────── 任务管理 ───────────────────────────

class JobManager:
    """单 worker 顺序执行分析任务（避免并发抢带宽/资源）。"""

    def __init__(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._queue: Queue = Queue()
        self._jobs: dict = {}
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    # ── 持久化 ──
    @property
    def _store(self) -> Path:
        return JOBS_DIR / "jobs.json"

    def _load(self) -> None:
        try:
            if self._store.exists():
                data = json.loads(self._store.read_text(encoding="utf-8"))
                for j in data if isinstance(data, list) else []:
                    if j.get("status") in ("queued", "running"):
                        j["status"] = "interrupted"
                    self._jobs[j["id"]] = j
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取任务历史失败: %s", exc)

    def _save(self) -> None:
        try:
            self._store.write_text(json.dumps(list(self._jobs.values()), ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入任务历史失败: %s", exc)

    # ── 对外 ──
    def submit(self, *, text: str, url: str, platform: str, options: dict,
               kind: str = "live", title: str = "", source: str = "") -> dict:
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        job = {"id": job_id, "kind": kind, "title": title or url, "source": source,
               "text": text[:500], "url": url, "platform": platform,
               "options": options, "status": "queued", "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "started": None, "finished": None, "returncode": None, "report_dir": None,
               "report_url": None, "log_path": str(JOBS_DIR / f"{job_id}.log")}
        with self._lock:
            self._jobs[job_id] = job
            self._save()
        self._queue.put(job_id)
        return self.detail(job_id)

    def submit_sync(self, upload_path: Path, options: dict) -> dict:
        """提交「上传样本」音画同步检测任务。"""
        return self.submit(text=f"上传样本: {upload_path.name}", url=f"file:{upload_path.name}",
                           platform="file", options=options, kind="sync",
                           title=upload_path.name, source=str(upload_path))

    def list(self) -> list:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j["created"], reverse=True)

    def detail(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {}
            out = dict(job)
        out.pop("options", None)
        log_path = Path(job["log_path"])
        if log_path.exists():
            txt = log_path.read_text(encoding="utf-8", errors="replace")
            out["log_tail"] = txt[-LOG_TAIL:]
        else:
            out["log_tail"] = ""
        if job.get("started") and (job.get("finished") or job["status"] == "running"):
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                end = datetime.strptime(job["finished"], fmt) if job.get("finished") else datetime.now()
                out["elapsed_s"] = int((end - datetime.strptime(job["started"], fmt)).total_seconds())
            except Exception:  # noqa: BLE001
                pass
        return out

    # ── worker ──
    def _cmd(self, job: dict) -> list:
        o = job.get("options") or {}
        if job.get("kind") == "sync":                      # 上传样本 → sync_cli
            cmd = [sys.executable, str(ROOT / "core" / "sync_cli.py"),
                   "--file", job.get("source") or "", "--format", "both"]
            if o.get("seconds"):
                cmd += ["--seconds", str(float(o["seconds"]))]
            if o.get("decode_seconds"):
                cmd += ["--decode-seconds", str(int(o["decode_seconds"]))]
            if o.get("fine_fps"):
                cmd += ["--fine-fps", str(int(o["fine_fps"]))]
            if o.get("roi"):
                cmd += ["--roi", str(o["roi"])]
            if o.get("use_face") is False:
                cmd.append("--no-face")
            if o.get("fine") is False:
                cmd.append("--no-fine")
            if o.get("av_sync") is False:
                cmd.append("--no-av-sync")
            if o.get("lip_sync") is False:
                cmd.append("--no-lip-sync")
            return cmd
        cmd = [sys.executable, str(ROOT / "live_trace.py"), job["url"], "--format", "both"]
        if job.get("platform") and job["platform"] != "auto":
            cmd += ["--platform", job["platform"]]
        mb = int(o.get("max_bytes") or 3_000_000)
        cmd += ["--max-bytes", str(mb), "--capture-seconds", str(int(o.get("capture_seconds") or 20))]
        if not o.get("av_sync", True):
            cmd.append("--no-av-sync")
        if not o.get("lip_sync", True):
            cmd.append("--no-lip-sync")
        if o.get("measure_timeout") is not None:
            cmd += ["--measure-timeout", str(float(o["measure_timeout"]))]
        return cmd

    def _worker(self) -> None:
        while True:
            try:
                job_id = self._queue.get()
            except Empty:  # pragma: no cover
                continue
            self._run(job_id)
            self._queue.task_done()

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
            job["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
        t0 = time.time()
        log_path = Path(job["log_path"])
        rc = -1
        try:
            with log_path.open("w", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(self._cmd(job))}\n\n")
                fh.flush()
                proc = subprocess.run(self._cmd(job), cwd=str(ROOT), stdout=fh,
                                      stderr=subprocess.STDOUT, timeout=1800,
                                      env={**os.environ, "PYTHONUNBUFFERED": "1"})
                rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("\n[web] 任务超时（1800s）已终止\n")
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务执行异常")
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n[web] 异常: {type(exc).__name__}: {exc}\n")
        report_dir = self._find_report(
            t0, "sync_report_*" if job.get("kind") == "sync" else "unified_report_*")
        with self._lock:
            job = self._jobs.get(job_id) or {}
            job["returncode"] = rc
            job["status"] = "done" if rc == 0 else "failed"
            job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if report_dir:
                job["report_dir"] = report_dir.name
                job["report_url"] = f"/{report_dir.name}/index.html"
            self._jobs[job_id] = job
            self._save()

    @staticmethod
    def _find_report(t0: float, pattern: str = "unified_report_*"):
        best = None
        for d in REPORT_DIR.glob(pattern):
            try:
                if d.is_dir() and (d / "index.html").exists() and d.stat().st_mtime >= t0 - 5:
                    if best is None or d.stat().st_mtime > best.stat().st_mtime:
                        best = d
            except OSError:
                continue
        return best


# ─────────────────────────── 历史扫描 ───────────────────────────

def scan_history(limit: int = 100) -> list:
    out: list = []
    dirs = list(REPORT_DIR.glob("unified_report_*")) + list(REPORT_DIR.glob("sync_report_*"))

    def _ts_of(p: Path) -> str:
        name = p.name
        for pre in ("unified_report_", "sync_report_"):
            if name.startswith(pre):
                return name[len(pre):]
        return name

    for d in sorted(dirs, key=_ts_of, reverse=True):
        if not d.is_dir() or not (d / "index.html").exists():
            continue
        meta: dict = {}
        try:
            ev = list((d / "evidence").glob("live_stream_info_*.json"))
            if ev:
                meta = json.loads(ev[0].read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        ts = _ts_of(d)
        try:
            time_str = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            time_str = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out.append({
            "dir": d.name, "kind": "sync" if d.name.startswith("sync_report_") else "live",
            "time": time_str, "report_url": f"/{d.name}/index.html",
            "platform": meta.get("platform"), "platform_name": meta.get("platform_name"),
            "room_id": meta.get("room_id"), "title": meta.get("title"),
            "status": meta.get("status"), "anchor": meta.get("anchor"),
            "has_text": (d / "report.txt").exists(),
        })
        if len(out) >= limit:
            break
    return out


# ─────────────────────────── HTTP ───────────────────────────

class WebHandler(serve.Handler):
    manager: JobManager = None  # 由 create_server 注入

    # ---- 工具 ----
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        # 实时监测接口需带查询串（?id=），必须在通用 /api/ 分发之前处理
        if path == "/api/live/status":
            return self._live_status()
        if path == "/api/live/list":
            return self._json({"sessions": MONITOR.list()})
        if path.startswith("/api/"):
            try:
                return self._api_get(path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("API GET 失败")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path in ("/", "/index.html"):
            return self._html(INDEX_HTML)
        if path in ("/sync", "/sync/", "/sync.html"):
            return self._html(SYNC_HTML)
        if path in ("/live", "/live/", "/live.html"):
            return self._html(LIVE_HTML)
        if path.startswith("/uploads/"):
            name = Path(path[len("/uploads/"):]).name
            return self._send_file(UPLOADS_DIR / name)
        if path.startswith("/assets/"):
            name = Path(path[len("/assets/"):]).name
            return self._send_file(ROOT / "report_assets" / name)
        return super().do_GET()

    def _api_get(self, path: str) -> None:
        if path == "/api/history":
            return self._json({"reports": scan_history()})
        if path == "/api/jobs":
            return self._json({"jobs": [
                {k: v for k, v in j.items() if k not in ("options", "text")} for j in self.manager.list()]})
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            detail = self.manager.detail(job_id)
            if not detail:
                return self._json({"error": "未找到任务"}, 404)
            return self._json(detail)
        if path.startswith("/api/sync/summary/"):
            return self._sync_summary(path[len("/api/sync/summary/"):])
        if path == "/api/resolve":
            return self._json({"error": "请用 POST /api/resolve"}, 405)
        return self._json({"error": "未知接口"}, 404)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/upload":
            # 上传是流式 body（multipart/二进制），**不能**先按 JSON 预读，否则请求体被吃光
            if (self.headers.get("Expect") or "").lower() == "100-continue":
                # curl 等客户端在大 body 前会等 100 Continue；显式回一个（HTTP/1.0 默认会 417）
                try:
                    self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    pass
            try:
                return self._upload()
            except Exception as exc:  # noqa: BLE001
                logger.exception("上传失败")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        try:
            body = self._body_json()
        except Exception:  # noqa: BLE001
            body = {}
        try:
            if path == "/api/resolve":
                import share_resolver
                return self._json(share_resolver.resolve_share(str(body.get("text") or "").strip()))
            if path == "/api/analyze":
                return self._analyze(body)
            if path == "/api/upload":
                return self._upload()
            if path == "/api/sync":
                return self._sync(body)
            if path == "/api/live/start":
                return self._live_start(body)
            if path == "/api/live/stop":
                return self._json({"ok": MONITOR.stop(str(body.get("id") or ""))})
            if path == "/api/live/stopall":
                for _s in MONITOR.list():
                    MONITOR.stop(str(_s.get("id") or ""))
                return self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            logger.exception("API POST 失败")
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "未知接口"}, 404)

    # ---- 实时监测 ----
    def _live_status(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        sid = (q.get("id") or [""])[0]
        s = MONITOR.get(sid)
        if s is None:
            return self._json({"error": "会话不存在或已停止（可重新点击「开始监测」）",
                               "alive": False, "status": "已停止"})
        return self._json(s.snapshot())

    def _live_start(self, body: dict) -> None:
        """**异步**启动监测：立即返回 id，解析/挑档位在会话线程里做（B 站接口可能数十秒）。"""
        text = str(body.get("text") or "").strip()
        if not text:
            return self._json({"error": "输入为空"}, 400)

        def _resolver() -> dict:
            from urllib.parse import quote as _q
            import share_resolver
            from config import detect_platform, COMMON_UA_DESKTOP
            target = text
            if not detect_platform(text):
                _r = share_resolver.resolve_share(text) or {}
                target = _r.get("url") or text
            adapter = detect_platform(target)
            if adapter is None:
                raise RuntimeError("无法识别平台或链接（可粘贴直播间链接/房间号/分享口令）")
            # 快速路径：/live 只要一个能播的 FLV 地址，跳过逐档探测（省 1~3s/档，档位多时可省数十秒）
            import inspect as _inspect
            if "probe" in _inspect.signature(adapter.resolve).parameters:
                room = adapter.resolve(target, probe=False)
            else:
                room = adapter.resolve(target)
            tiers = [x for x in (getattr(room, "tiers", []) or []) if x.ok and x.protocol == "flv"]
            if not tiers:
                tiers = [x for x in (getattr(room, "tiers", []) or []) if x.protocol == "flv"]
            if not tiers:
                raise RuntimeError(f"该房间没有可播放/可监测的 FLV 档位（当前状态：{room.status}）")
            tier = tiers[0]
            headers = {"User-Agent": COMMON_UA_DESKTOP, "Accept": "*/*"}
            try:
                headers["Referer"] = adapter.meta.live_url.format(room_id=room.room_id)
            except (KeyError, IndexError, ValueError):
                pass
            platform = adapter.meta.key
            room_id = str(room.room_id)
            quality = str(tier.quality_key or "")
            codec = str(tier.codec or "")

            def _refresh():
                return serve.refresh_stream_url(platform, room_id, quality, "flv", codec)

            return {
                "url": tier.url, "headers": headers, "platform": platform,
                "platform_name": getattr(adapter.meta, "name", platform),
                "room_id": room_id, "title": room.title or "", "label": tier.label or "",
                "status": room.status, "refresh_fn": _refresh,
                "play_url": "/p?p=%s&room=%s&q=%s&proto=flv" % (
                    _q(platform, safe=""), _q(room_id, safe=""), _q(quality, safe="")),
            }

        s = MONITOR.start("", resolver=_resolver)
        return self._json({"id": s.id, "async": True,
                           "note": "解析在后台进行，轮询 /api/live/status 获取 play_url 与实时指标"})

    def _analyze(self, body: dict) -> None:
        text = str(body.get("text") or "").strip()
        platform = str(body.get("platform") or "auto").strip() or "auto"
        if not text:
            return self._json({"error": "输入为空"}, 400)
        import share_resolver
        from config import detect_platform
        res = share_resolver.resolve_share(text) if not detect_platform(text) else {
            "ok": True, "url": text, "kind": "url", "platform": None, "manual": [], "steps": []}
        if not res.get("url"):
            return self._json({"error": "未能解析出可分析的地址", "resolution": res}, 400)
        options = {k: body.get(k) for k in ("max_bytes", "capture_seconds", "av_sync", "lip_sync")
                   if body.get(k) is not None}
        job = self.manager.submit(text=text, url=res["url"], platform=platform, options=options)
        return self._json({"job": job, "resolution": res})


    # ── 上传 / 检测 / 摘要 / 静态文件（Range）──

    CONTENT_TYPES = {".flv": "video/x-flv", ".mp4": "video/mp4", ".m4v": "video/mp4",
                     ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
                     ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".avi": "video/x-msvideo",
                     ".3gp": "video/3gpp", ".wmv": "video/x-ms-wmv"}

    def _send_file(self, path: Path, ctype: str = "") -> None:
        """支持 Range 的文件服务（视频拖动进度条必需）。"""
        if not path.is_file():
            return self.send_error(404, "not found")
        size = path.stat().st_size
        if not ctype:
            ctype = self.CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        start, end, status = 0, max(0, size - 1), 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start <= end:
                    status = 206
                else:
                    start, end = 0, max(0, size - 1)
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _safe_name(name: str) -> str:
        name = Path(name or "upload.bin").name
        return re.sub(r"[^\w.\-\u4e00-\u9fa5]+", "_", name)[:120] or "upload.bin"

    def _upload(self) -> None:
        """上传样本：支持 multipart/form-data（网页 <input type=file>）与原始 body。"""
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        qs = parse_qs(urlparse(self.path).query)
        hint_name = (qs.get("name") or [""])[0]
        ctype = self.headers.get("Content-Type") or ""
        upload_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        limit = MAX_UPLOAD_MB * 1024 * 1024
        total = int(self.headers.get("Content-Length") or 0)
        if total > limit:
            return self._json({"error": f"文件过大（>{MAX_UPLOAD_MB}MB）"}, 413)
        name, written = "", 0
        if "multipart/form-data" in ctype:
            m = re.search(r'boundary="?([^";]+)"?', ctype)
            if not m:
                return self._json({"error": "缺少 multipart boundary"}, 400)
            boundary = m.group(1).encode()
            delim = b"\r\n--" + boundary
            buf = b""
            remaining = total                     # 只按 Content-Length 读，绝不超额（否则会阻塞）
            while b"\r\n\r\n" not in buf and remaining > 0 and len(buf) < 65536:
                chunk = self.rfile.read(min(4096, remaining))
                if not chunk:
                    break
                buf += chunk
                remaining -= len(chunk)
            head, _, rest = buf.partition(b"\r\n\r\n")
            fm = re.search(rb'filename="([^"]*)"', head)
            name = self._safe_name((fm.group(1).decode("utf-8", "replace") if fm else "") or hint_name)
            dest = UPLOADS_DIR / f"{upload_id}__{name}"
            tail = rest
            with dest.open("wb") as fh:
                while True:
                    idx = tail.find(delim)
                    if idx >= 0:
                        fh.write(tail[:idx])
                        written += idx
                        break
                    keep = len(delim)
                    if len(tail) > keep:
                        fh.write(tail[:-keep])
                        written += len(tail) - keep
                        tail = tail[-keep:]
                    if written > limit:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        return self._json({"error": "文件过大"}, 413)
                    if remaining <= 0:               # 请求体已读完（结尾 boundary 已在 tail 中）
                        fh.write(tail)
                        written += len(tail)
                        break
                    chunk = self.rfile.read(min(262144, remaining))
                    if not chunk:
                        fh.write(tail)
                        written += len(tail)
                        break
                    remaining -= len(chunk)
                    tail += chunk
        else:                                   # 原始 body（fetch(..., {body: file})）
            name = self._safe_name(hint_name or self.headers.get("X-Filename") or "upload.bin")
            dest = UPLOADS_DIR / f"{upload_id}__{name}"
            remaining = total
            with dest.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(262144, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        if not written:
            return self._json({"error": "上传内容为空"}, 400)
        try:
            import lip_sync_detector
            media = lip_sync_detector.probe_media(dest)
        except Exception:  # noqa: BLE001
            media = {}
        return self._json({
            "upload_id": f"{upload_id}__{name}", "name": name,
            "size": written, "size_mb": round(written / 1e6, 2),
            "media": media, "preview_url": f"/uploads/{upload_id}__{name}",
        })

    def _sync(self, body: dict) -> None:
        """提交上传样本的音画同步检测任务。"""
        upload_id = str(body.get("upload_id") or "").strip()
        if not upload_id or Path(upload_id).name != upload_id:
            return self._json({"error": "缺少或非法 upload_id"}, 400)
        path = UPLOADS_DIR / upload_id
        if not path.is_file():
            return self._json({"error": "上传文件不存在（可能已被清理）"}, 404)
        opts = body.get("options") or {}
        job = self.manager.submit_sync(path, opts)
        return self._json({"job": job})

    def _sync_summary(self, job_id: str) -> None:
        """读取该任务的证据 JSON，返回页面展示所需的摘要（含相关曲线与联合诊断）。"""
        detail = self.manager.detail(job_id)
        if not detail:
            return self._json({"error": "未找到任务"}, 404)
        report_dir = REPORT_DIR / (detail.get("report_dir") or "")
        ev = report_dir / "evidence"
        if not ev.is_dir():
            return self._json({"error": "报告尚未生成", "status": detail.get("status")}, 404)
        out: dict = {"status": detail.get("status"), "report_url": detail.get("report_url")}
        src = Path(detail.get("source") or "")
        if src.is_file():
            out["preview_url"] = f"/uploads/{src.name}"
        for pat, key in (("av_sync_analysis_*.json", "av"), ("lip_sync_analysis_*.json", "lip"),
                         ("live_stream_info_*.json", "payload")):
            hits = sorted(ev.glob(pat))
            if not hits:
                continue
            try:
                data = json.loads(hits[-1].read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if key == "payload":
                out["media"] = {"name": data.get("title"), **(data.get("metrics") or {})}
                continue
            p = data.get("perceptual") or {}
            m = data.get("metrics") or {}
            if key == "av":
                out["av"] = {"verdict": data.get("verdict"), "stream_offset_ms": p.get("stream_offset_ms"),
                             "player_residual_ms": p.get("player_residual_ms"),
                             "direction": p.get("direction"), "detail": data.get("verdict_detail")}
            else:
                out["lip"] = {"verdict": data.get("verdict"), "detail": data.get("verdict_detail"),
                              "content_offset_ms": p.get("content_offset_ms"),
                              "direction": p.get("direction"),
                              "lag_resolution_ms": p.get("lag_resolution_ms"),
                              "content_offset_ci_ms": p.get("content_offset_ci_ms"),
                              "window_agreement": p.get("window_agreement"),
                              "confidence": p.get("confidence"),
                              "face_coverage": m.get("face_coverage"),
                              "lag_curve": m.get("lag_curve") or []}
        if out.get("av") and out.get("lip"):
            try:
                out["diagnosis"] = share_resolver_diag(out["lip"], out["av"])
            except Exception:  # noqa: BLE001
                pass
        return self._json(out)


def share_resolver_diag(lip: dict, av: dict) -> str:
    """联合诊断（复用 lip_sync_detector 的口径）。"""
    import lip_sync_detector
    return lip_sync_detector.diagnose_vs_stream_offset(
        {"perceptual": {"content_offset_ms": lip.get("content_offset_ms"),
                        "thresholds": {"itu": {"audio_lead_detect_ms": 45}}}},
        {"perceptual": {"stream_offset_ms": av.get("stream_offset_ms")}})


def create_server(port: int) -> ThreadingHTTPServer:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    WebHandler.manager = JobManager()
    handler = functools.partial(WebHandler, directory=str(REPORT_DIR))
    return ThreadingHTTPServer(("0.0.0.0", port), handler)


def main() -> int:
    ap = argparse.ArgumentParser(description="live-stream-trace 网页系统（输入 → 报告 → 历史）")
    ap.add_argument("port", nargs="?", type=int, default=8899)
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    httpd = create_server(args.port)
    url = f"http://localhost:{args.port}/"
    print(f"✅ 网页系统已启动: {url}")
    print(f"   报告目录: {REPORT_DIR}")
    print(f"   历史报告: {url}api/history")
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
