"""探针采集共享辅助 — 纯 capture-first，不做分析/报告聚合。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR

logger = logging.getLogger(__name__)


def probe_filename(probe_type: str, room_id: str, ts: str) -> str:
    """构建探针 JSON 文件名。

    格式: ``{probe_type}_{room_id}_{ts}.json``
    与仓库现有命名 ``{type}_{room_id}_{ts}.json`` 对齐。
    """
    return f"{probe_type}_{room_id}_{ts}.json"


def save_probe_json(path: Path, data: dict[str, Any]) -> Path:
    """将原始探针数据写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("探针数据已保存: %s", path)
    return path


def make_probe_result(
    probe_type: str,
    room_id: str,
    raw: dict[str, Any] | None = None,
    *,
    ts: str | None = None,
    ok: bool = False,
    url_used: str = "",
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造标准探针结果字典。

    所有探针脚本采集的 JSON 顶层结构应通过此函数生成，
    保证 ``type`` / ``room_id`` / ``timestamp`` 字段命名一致。
    """
    ts = ts or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result: dict[str, Any] = {
        "type": probe_type,
        "room_id": room_id,
        "timestamp": ts,
        "ok": ok,
        "url_used": url_used,
    }
    if raw is not None:
        result["raw"] = raw
    if error:
        result["error"] = error
    if extra:
        result.update(extra)
    return result


def probe_output_path(probe_type: str, room_id: str, ts: str) -> Path:
    """快捷获取探针输出的完整路径（``captures/<filename>``）。"""
    return OUTPUT_DIR / probe_filename(probe_type, room_id, ts)
