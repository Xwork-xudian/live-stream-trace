"""统一加载可人工维护的映射表。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _resource_dir() -> Path:
    """获取资源文件目录（兼容 PyInstaller 打包）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


BASE_DIR = _resource_dir()
MAPPINGS_DIR = BASE_DIR / "mappings"


def load_json_mapping(name: str) -> Any:
    path = MAPPINGS_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    _validate_mapping(name, data)
    return data


def _validate_mapping(name: str, data: Any) -> None:
    if name == "field_descriptions.json":
        if not isinstance(data, dict):
            raise ValueError(f"{name} 必须是对象")
        for key, value in data.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} 的字段说明 key 必须是 str")
            # 支持两种格式：str（旧）或 dict（新，含 description + values）
            if isinstance(value, str):
                continue
            if isinstance(value, dict):
                if not isinstance(value.get("description", ""), str):
                    raise ValueError(f"{name}.{key} 的 description 必须是 str")
            else:
                raise ValueError(f"{name}.{key} 的值必须是 str 或 dict")
        return

    if name == "software_patterns.json":
        _validate_pattern_list(name, data, required_key="pattern")
        return

    if name == "cdn_patterns.json":
        _validate_pattern_list(name, data, required_key="keyword")
        return


def _validate_pattern_list(name: str, data: Any, required_key: str) -> None:
    if not isinstance(data, list):
        raise ValueError(f"{name} 必须是数组")
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"{name} 的每一项必须是对象")
        if not isinstance(item.get(required_key), str) or not item[required_key].strip():
            raise ValueError(f"{name} 缺少有效的 {required_key}")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError(f"{name} 缺少有效的 name")
