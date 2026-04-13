from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from settings import OUTPUT_DIR, SEARCH_API_KEY_POOL, get_tls_policy_summary, get_today_output_dir
from trading_calendar import get_calendar_mode

RUN_TIMESTAMP_ENV = "VCP_RUN_TS"
PREFLIGHT_SUMMARY_ENV = "VCP_PREFLIGHT_SUMMARY_JSON"
MANIFEST_FILENAME = "run_manifest.json"

# Why: 核心管线产出（P1~P9、战报）平铺在日期根目录，一览无余
#      辅助/元数据文件（Agent 诊断、Layer ROI）放 meta/ 子目录，避免干扰主视图
#      日志放 logs/ 子目录（持续追加写入，混在一起不好找）
ARTIFACT_CATEGORIES = ("logs", "meta")

_MANIFEST_LOCK = threading.Lock()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def get_output_root_dir() -> str:
    return ensure_dir(OUTPUT_DIR)


def get_global_system_dir() -> str:
    return ensure_dir(os.path.join(get_output_root_dir(), "_system"))


def get_run_lock_path() -> str:
    return os.path.join(get_global_system_dir(), "run.lock")


def get_day_dir(day_dir: str | None = None) -> str:
    return ensure_dir(day_dir or get_today_output_dir())


def get_day_subdir(name: str, *, day_dir: str | None = None) -> str:
    # Why: logs 和 meta 有子目录，其他所有产出物直接放日期根目录
    if name in ("logs", "meta"):
        return ensure_dir(os.path.join(get_day_dir(day_dir), name))
    return get_day_dir(day_dir)


def get_today_logs_dir() -> str:
    return get_day_subdir("logs")


def get_pipeline_log_path() -> str:
    return os.path.join(get_today_logs_dir(), "pipeline.log")


def get_error_log_path() -> str:
    return os.path.join(get_today_logs_dir(), "error.log")


def infer_artifact_category(prefix: str) -> str:
    # Why: Agent 诊断/预检/审校 和 Layer_ROI 是辅助元数据，放 meta/ 子目录
    #      核心管线产出（P1~P9、战报）平铺在日期根目录
    if prefix.startswith("Agent_") or prefix.startswith("Layer_ROI"):
        return "meta"
    return ""


def extract_timestamp_from_name(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"(\d{12,14})", text)
    return match.group(1) if match else ""


def set_run_timestamp(timestamp: str | None = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
    os.environ[RUN_TIMESTAMP_ENV] = ts
    return ts


def get_run_timestamp() -> str:
    current = os.environ.get(RUN_TIMESTAMP_ENV, "").strip()
    return current or set_run_timestamp()


def build_artifact_path(
    prefix: str,
    extension: str,
    timestamp: str | None = None,
    *,
    category: str | None = None,
) -> str:
    ext = extension.lstrip(".")
    ts = timestamp or get_run_timestamp()
    artifact_category = category or infer_artifact_category(prefix)
    # Why: meta 类文件走子目录，其他直接写日期根目录
    target_dir = get_day_subdir(artifact_category) if artifact_category else get_day_dir()
    return os.path.join(target_dir, f"{prefix}_{ts}.{ext}")


def write_text_artifact(
    prefix: str,
    content: str,
    *,
    timestamp: str | None = None,
    category: str | None = None,
) -> str:
    path = build_artifact_path(prefix, "md", timestamp=timestamp, category=category)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def write_json_artifact(
    prefix: str,
    data: Any,
    *,
    timestamp: str | None = None,
    category: str | None = None,
) -> str:
    path = build_artifact_path(prefix, "json", timestamp=timestamp, category=category)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def get_manifest_path() -> str:
    # Why: run_manifest.json 属于辅助元数据，和 Agent/LayerROI 一起放 meta/ 子目录
    return os.path.join(get_day_subdir("meta"), MANIFEST_FILENAME)


def load_run_manifest() -> dict[str, Any]:
    path = get_manifest_path()
    if not os.path.exists(path):
        return {
            "run_timestamp": get_run_timestamp(),
            "updated_at": "",
            "stages": {},
            "runtime_context": {},
            "preflight": {},
            "manifest_version": 3,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("manifest root is not a dict")
        data.setdefault("run_timestamp", get_run_timestamp())
        data.setdefault("updated_at", "")
        data.setdefault("stages", {})
        data.setdefault("runtime_context", {})
        data.setdefault("preflight", {})
        data.setdefault("manifest_version", 3)
        return data
    except Exception:
        return {
            "run_timestamp": get_run_timestamp(),
            "updated_at": datetime.now().isoformat(),
            "stages": {},
            "runtime_context": {},
            "preflight": {},
            "manifest_version": 3,
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
    run_ts = timestamp or get_run_timestamp()
    manifest_path = get_manifest_path()

    with _MANIFEST_LOCK:
        manifest = load_run_manifest()
        manifest["run_timestamp"] = run_ts
        manifest["updated_at"] = datetime.now().isoformat()
        manifest["manifest_version"] = 3

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


def set_preflight_summary(summary: dict[str, Any] | None) -> None:
    if not summary:
        os.environ.pop(PREFLIGHT_SUMMARY_ENV, None)
        return
    os.environ[PREFLIGHT_SUMMARY_ENV] = json.dumps(summary, ensure_ascii=False)


def get_preflight_summary() -> dict[str, Any]:
    raw = os.environ.get(PREFLIGHT_SUMMARY_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_runtime_context(*, cwd: str | None = None) -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cwd": cwd or os.getcwd(),
        "calendar_mode": get_calendar_mode(),
        "search_keys_count": len(SEARCH_API_KEY_POOL),
        "tls_policy": get_tls_policy_summary(),
        "executable": sys.executable,
    }


def update_manifest_context(
    *,
    runtime_context: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> str:
    run_ts = timestamp or get_run_timestamp()
    manifest_path = get_manifest_path()

    with _MANIFEST_LOCK:
        manifest = load_run_manifest()
        manifest["run_timestamp"] = run_ts
        manifest["updated_at"] = datetime.now().isoformat()
        manifest["manifest_version"] = 3

        if runtime_context:
            manifest.setdefault("runtime_context", {}).update(runtime_context)
        if preflight is not None:
            manifest["preflight"] = preflight

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest_path


def find_latest_day_dir() -> str:
    root = Path(get_output_root_dir())
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.isdigit() and len(path.name) == 8
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    return str(candidates[0]) if candidates else get_day_dir()
