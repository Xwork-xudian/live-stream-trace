"""直播流「实时音画延迟」监测：增量解析 FLV tag，按需输出 0.5s 级快照。

设计要点
- **增量**：边收边解析（流式，不在磁盘落样），只保留滚动窗口内的 tag 时间戳 → 内存恒定。
- **零定时器**：快照由前端轮询（0.5s）触发计算，刷新率即轮询率。
- **主指标**：流内 A/V 偏移（音频 PTS − 视频 PTS）。按「墙钟秒桶」配对取中位数，
  对丢包/抖动鲁棒；口径与 av_sync_detector 的「流内偏移」一致。
- 附带：码率、视频帧率、时间戳跳变、数据量、连接状态。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

WINDOW_S = 8.0          # 滚动窗口（秒）
MAX_EVENTS = 20000      # 事件上限（内存护栏）

try:  # 与 av_sync_detector 共用感知门限口径
    from av_sync_detector import PERCEPTUAL_EBU_R37 as _EBU, PERCEPTUAL_ITU as _ITU
except Exception:  # noqa: BLE001
    _EBU = {"audio_lead_ms": 40, "audio_lag_ms": -60}
    _ITU = {"audio_lead_detect_ms": 45, "audio_lag_detect_ms": -125,
            "audio_lead_accept_ms": 90, "audio_lag_accept_ms": -185}


class _FlvTagStream:
    """流式 FLV tag 解析（处理跨 chunk 的半截 tag）。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.started = False

    def feed(self, data: bytes):
        self.buf += data
        if not self.started:
            if len(self.buf) < 13:
                return []
            if bytes(self.buf[:3]) != b"FLV":
                self.buf.clear()
                return []
            del self.buf[:13]                     # 9 字节头 + 4 字节 prevTagSize
            self.started = True
        out = []
        while len(self.buf) >= 11:
            ttype = self.buf[0]
            size = int.from_bytes(self.buf[1:4], "big")
            ts = int.from_bytes(self.buf[4:7], "big") | (self.buf[7] << 24)
            total = 11 + size + 4
            if size <= 0 or len(self.buf) < total:
                break
            out.append((ttype, ts, size))
            del self.buf[:total]
        return out


class LiveSession:
    def __init__(self, sid: str, url: str, *, meta: Optional[dict] = None,
                 headers: Optional[dict] = None, window_s: float = WINDOW_S,
                 title: str = "", room_id: str = "", label: str = "",
                 refresh_fn=None, resolver=None) -> None:
        self.id = sid
        self.url = url
        # resolver：后台线程里先解析（B 站接口可能数十秒）→ 立刻返回 id，界面不卡
        self.resolver = resolver
        # 直播地址常带短时签名；断线重连时用 refresh_fn 取新地址
        self.refresh_fn = refresh_fn
        self.title = title
        self.room_id = room_id
        self.label = label
        self.meta = meta or {}
        self.headers = {"User-Agent": UA, "Accept": "*/*"}
        if headers:
            self.headers.update(headers)
        self.window_s = float(window_s)
        self._parser = _FlvTagStream()
        self._a: deque = deque(maxlen=MAX_EVENTS)      # (wall, ts)
        self._v: deque = deque(maxlen=MAX_EVENTS)
        self._buckets: deque = deque(maxlen=120)       # (sec, a_ts, v_ts)
        self._cur: Optional[dict] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.total_bytes = 0
        self.total_tags = 0
        self.started_at = time.time()
        self.last_error = ""
        self.status = "连接中"
        self._last_poll = (0, time.time())        # (已收字节, 时刻)

    # ── 生命周期 ──
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"live-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── 采集 ──
    def _resolve_meta(self) -> None:
        """把「解析 + 挑档位」放到会话线程里做（可能很慢）。"""
        if self.resolver is None:
            return
        try:
            self.status = "解析中"
            info = self.resolver() or {}
            self.url = info.get("url") or self.url
            self.headers = dict(info.get("headers") or self.headers)
            self.title = info.get("title") or self.title
            self.room_id = info.get("room_id") or self.room_id
            self.label = info.get("label") or self.label
            self.meta = {"platform": info.get("platform", ""),
                         "platform_name": info.get("platform_name", ""),
                         "play_url": info.get("play_url", ""),
                         "stream_status": info.get("status", "")}
            if info.get("refresh_fn"):
                self.refresh_fn = info["refresh_fn"]
            self.resolver = None                 # 只解析一次
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"解析失败：{exc}"
            self.status = "解析失败"

    def _run(self) -> None:
        self._resolve_meta()
        if self.status == "解析失败":
            return
        while not self._stop.is_set():
            if self.refresh_fn is not None:
                try:
                    fresh = self.refresh_fn()
                    if fresh:
                        self.url = fresh
                except Exception:  # noqa: BLE001
                    pass
            try:
                r = requests.get(self.url, headers=self.headers, timeout=(10, 20), stream=True)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.status = "连接失败"
                if self._stop.wait(1.0):
                    return
                continue
            code = getattr(r, "status_code", 0)
            if code not in (200, 206):
                self.last_error = f"上游 HTTP {code}"
                self.status = "上游拒绝"
                r.close()
                if self._stop.wait(1.5):
                    return
                continue
            self.status = "监测中"
            self.last_error = ""
            try:
                for chunk in r.iter_content(65536):
                    if self._stop.is_set():
                        break
                    if not chunk:
                        continue
                    with self._lock:
                        self.total_bytes += len(chunk)
                        for ttype, ts, size in self._parser.feed(chunk):
                            self._ingest(ttype, ts)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                r.close()
            self.status = "重连中"
            if self._stop.wait(0.8):
                return

    def _ingest(self, ttype: int, ts: int) -> None:
        now = time.time()
        if ttype == 9:
            self._v.append((now, ts))
        elif ttype == 8:
            self._a.append((now, ts))
        else:
            return
        self.total_tags += 1
        sec = int(now)
        if self._cur is None or self._cur["sec"] != sec:
            if self._cur is not None:
                self._flush_bucket()
            self._cur = {"sec": sec, "a": [], "v": []}
        self._cur["a" if ttype == 8 else "v"].append(ts)

    def _flush_bucket(self) -> None:
        """把一个墙钟秒桶折算成 (秒, 音频PTS中位, 视频PTS中位)。"""
        if not self._cur:
            return
        a, v = self._cur["a"], self._cur["v"]
        ma = sorted(a)[len(a) // 2] if a else None
        mv = sorted(v)[len(v) // 2] if v else None
        self._buckets.append((self._cur["sec"], ma, mv))

    # ── 快照（前端每 0.5s 拉一次）──
    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            a = list(self._a)
            v = list(self._v)
            buckets = list(self._buckets)   # 只统计已封口的整秒桶（当前桶下一秒封口）
            tb = self.total_bytes
        t0 = now - self.window_s
        a_win = [(w, t) for w, t in a if w >= t0]
        v_win = [(w, t) for w, t in v if w >= t0]

        a_ts = [t for _, t in a_win]
        v_ts = [t for _, t in v_win]
        a_jumps = sum(1 for i in range(1, len(a_ts)) if abs(a_ts[i] - a_ts[i - 1]) > 1000)
        v_jumps = sum(1 for i in range(1, len(v_ts)) if abs(v_ts[i] - v_ts[i - 1]) > 1000)

        # 偏移：墙钟秒桶配对（同秒内的首音频 ts − 首视频 ts），取窗口内中位数
        pairs = [(a_b - v_b) for (_s, a_b, v_b) in buckets if a_b is not None and v_b is not None]
        pairs = pairs[-int(max(2, self.window_s)):]
        off = None
        if pairs:
            srt = sorted(pairs)
            off = float(srt[len(srt) // 2])

        fps = 0.0
        if len(v_win) >= 2 and v_jumps == 0 and v_ts[-1] > v_ts[0]:
            pts_span = (v_ts[-1] - v_ts[0]) / 1000.0          # 优先：真实流时间跨度
            if pts_span > 0.2:
                fps = (len(v_win) - 1) / pts_span
        if fps <= 0 and len(v_win) >= 2:
            span = max(0.2, min(self.window_s, v_win[-1][0] - v_win[0][0]))
            fps = len(v_win) / span
        lb, lt = self._last_poll
        kbps = (tb - lb) * 8 / max(0.05, now - lt) / 1000.0
        self._last_poll = (tb, now)     # (已收字节, 时刻)

        verdict, level = self._verdict(off)
        return {
            "id": self.id, "status": self.status, "alive": self.alive,
            "title": self.title, "room_id": self.room_id, "label": self.label,
            "play_url": (self.meta or {}).get("play_url", ""),
            "platform": (self.meta or {}).get("platform", ""),
            "platform_name": (self.meta or {}).get("platform_name", ""),
            "uptime_s": round(now - self.started_at, 1),
            "offset_ms": round(off, 1) if off is not None else None,
            "direction": ("音频超前画面" if (off or 0) >= 0 else "音频滞后画面") if off is not None else None,
            "verdict": verdict, "verdict_level": level,
            "fps": round(fps, 1), "kbps": round(kbps, 1),
            "frames_a": len(a_win), "frames_v": len(v_win),
            "ts_jumps": a_jumps + v_jumps,
            "window_s": self.window_s,
            "total_mb": round(tb / 1e6, 2),
            "error": self.last_error,
            "paired_buckets": len(pairs),
            "time": now,
        }

    @staticmethod
    def _verdict(off):
        if off is None:
            return "等待数据", "低"
        a = abs(off)
        if _EBU["audio_lag_ms"] <= off <= _EBU["audio_lead_ms"]:
            return "同步良好（EBU R37 内）", "高"
        if a <= max(abs(_EBU["audio_lead_ms"]), abs(_EBU["audio_lag_ms"])) + 20:
            return "轻微偏移", "中"
        if off >= _ITU["audio_lead_detect_ms"] or off <= _ITU["audio_lag_detect_ms"]:
            if off >= _ITU["audio_lead_accept_ms"] or off <= _ITU["audio_lag_accept_ms"]:
                return "明显偏移（超出 ITU 可接受）", "低"
            return "可感知偏移（超出 ITU 可察觉）", "中"
        return "轻微偏移", "中"


class LiveMonitor:
    """会话管理（start/status/stop）。"""

    def __init__(self) -> None:
        self._sessions: dict = {}
        self._lock = threading.Lock()

    def start(self, url: str, *, meta=None, headers=None, title="", room_id="", label="",
              refresh_fn=None, resolver=None) -> LiveSession:
        sid = f"live_{int(time.time())}_{len(self._sessions):02d}"
        s = LiveSession(sid, url, meta=meta, headers=headers, title=title,
                        room_id=room_id, label=label, refresh_fn=refresh_fn, resolver=resolver)
        with self._lock:
            self._sessions[sid] = s
        s.start()
        return s

    def get(self, sid: str) -> Optional[LiveSession]:
        with self._lock:
            return self._sessions.get(sid)

    def stop(self, sid: str) -> bool:
        s = self.get(sid)
        if not s:
            return False
        s.stop()
        with self._lock:
            self._sessions.pop(sid, None)
        return True

    def list(self) -> list:
        with self._lock:
            return [s.snapshot() for s in self._sessions.values()]
