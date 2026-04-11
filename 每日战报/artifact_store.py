"""运行产物管理工具。

统一管理：
- 同批次运行时间戳
- 各阶段 JSON/Markdown 落盘路径
- run_manifest.json 阶段状态记录
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Any

from settings import get_today_output_dir

RUN_TIMESTAMP_ENV = "VCP_RUN_TS"
MANIFEST_FILENAME = "run_manifest.json"

_MANIFEST_LOCK = threading.Lock()


def extract_timestamp_from_name(text: str) -> str:
    """从文件名中提取 12/14 位运行时间戳。"""
    if not text:
        return ""
    match = re.search(r"(\d{12,14})", text)
    return match.group(1) if match else ""


def set_run_timestamp(timestamp: str | None = None) -> str:
    """设置当前运行统一时间戳。"""
    ts = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
    os.environ[RUN_TIMESTAMP_ENV] = ts
    return ts


def get_run_timestamp() -> str:
    """获取当前运行统一时间戳。"""
    current = os.environ.get(RUN_TIMESTAMP_ENV, "").strip()
    if current:
        return current
    return set_run_timestamp()


def build_artifact_path(prefix: str, extension: str, timestamp: str | None = None) -> str:
    ext = extension.lstrip(".")
    ts = timestamp or get_run_timestamp()
    return os.path.join(get_today_output_dir(), f"{prefix}_{ts}.{ext}")


def write_text_artifact(prefix: str, content: str, *, timestamp: str | None = None) -> str:
    path = build_artifact_path(prefix, "md", timestamp=timestamp)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def write_json_artifact(prefix: str, data: Any, *, timestamp: str | None = None) -> str:
    path = build_artifact_path(prefix, "json", timestamp=timestamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def get_manifest_path() -> str:
    return os.path.join(get_today_output_dir(), MANIFEST_FILENAME)


def load_run_manifest() -> dict[str, Any]:
    path = get_manifest_path()
    if not os.path.exists(path):
        return {"run_timestamp": get_run_timestamp(), "updated_at": "", "stages": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("manifest 根节点不是对象")
        data.setdefault("run_timestamp", get_run_timestamp())
        data.setdefault("updated_at", "")
        data.setdefault("stages", {})
        return data
    except Exception:
        return {
            "run_timestamp": get_run_timestamp(),
            "updated_at": datetime.now().isoformat(),
            "stages": {},
            "corrupt_manifest": True,
        }


def update_run_manifest(
    stage: str,
    *,
    status: str,
    files: dict[str, str] | None = None,
    metrics: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> str:
    """更新当日运行 manifest。"""
    run_ts = timestamp or get_run_timestamp()
    manifest_path = get_manifest_path()

    with _MANIFEST_LOCK:
        manifest = load_run_manifest()
        manifest["run_timestamp"] = run_ts
        manifest["updated_at"] = datetime.now().isoformat()
        stages = manifest.setdefault("stages", {})
        stage_payload = stages.setdefault(stage, {})
        stage_payload["status"] = status
        stage_payload["updated_at"] = datetime.now().isoformat()
        stage_payload["run_timestamp"] = run_ts

        if files:
            stage_payload.setdefault("files", {}).update(files)
        if metrics:
            stage_payload.setdefault("metrics", {}).update(metrics)

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest_path
