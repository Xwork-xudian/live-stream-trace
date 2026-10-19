"""单次运行产物索引工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_artifact(
    manifest: dict[str, Any],
    label: str,
    path: Path,
    artifact_type: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """向 session manifest 追加一个产物记录。"""
    manifest.setdefault("artifacts", []).append(
        {
            "label": label,
            "path": str(path),
            "type": artifact_type,
            "meta": meta or {},
        }
    )


def save_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    """保存 session_manifest JSON。"""
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path
