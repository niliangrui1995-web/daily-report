from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from settings import OUTPUT_DIR, SEARCH_API_KEY_POOL, get_tls_policy_summary, get_today_output_dir
from trading_calendar import get_calendar_mode

RUN_TIMESTAMP_ENV = "VCP_RUN_TS"
PREFLIGHT_SUMMARY_ENV = "VCP_PREFLIGHT_SUMMARY_JSON"
MANIFEST_FILENAME = "run_manifest.json"
# Windows PowerShell 5.1 guesses ANSI for BOM-less UTF-8; these artifacts are opened locally.
UTF8_FILE_ENCODING = "utf-8-sig"

ARTIFACT_CATEGORIES = ("logs", "meta", "agent")
SUCCESS_STATUSES = {
    "completed",
    "json_ready",
    "completed_recovered",
    "completed_email_sent",
    "completed_no_email",
}

_MANIFEST_LOCK = threading.RLock()


class ManifestLoadError(RuntimeError):
    def __init__(self, path: str, error: Exception) -> None:
        super().__init__(f"failed to load manifest '{path}': {error}")
        self.path = path
        self.error = error


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def configure_utf8_environment() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def get_output_root_dir() -> str:
    return ensure_dir(OUTPUT_DIR)


def get_global_system_dir() -> str:
    return ensure_dir(os.path.join(get_output_root_dir(), "_system"))


def get_run_lock_path() -> str:
    return os.path.join(get_global_system_dir(), "run.lock")


def get_day_dir(day_dir: str | None = None) -> str:
    return ensure_dir(day_dir or get_today_output_dir())


def get_day_subdir(name: str, *, day_dir: str | None = None) -> str:
    if name in ARTIFACT_CATEGORIES:
        return ensure_dir(os.path.join(get_day_dir(day_dir), name))
    return get_day_dir(day_dir)


def get_today_logs_dir() -> str:
    return get_day_subdir("logs")


def get_pipeline_log_path() -> str:
    return os.path.join(get_today_logs_dir(), "pipeline.log")


def get_error_log_path() -> str:
    return os.path.join(get_today_logs_dir(), "error.log")


def infer_artifact_category(prefix: str) -> str:
    if prefix.startswith("Agent_"):
        return "agent"
    if prefix.startswith("Layer_ROI"):
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
    with open(path, "w", encoding=UTF8_FILE_ENCODING) as f:
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
    with open(path, "w", encoding=UTF8_FILE_ENCODING) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def get_manifest_path() -> str:
    return os.path.join(get_day_dir(), MANIFEST_FILENAME)


def _get_legacy_manifest_path() -> str:
    return os.path.join(get_day_subdir("meta"), MANIFEST_FILENAME)


def _build_default_manifest(run_ts: str | None = None) -> dict[str, Any]:
    return {
        "run_timestamp": run_ts or get_run_timestamp(),
        "updated_at": "",
        "stages": {},
        "runtime_context": {},
        "preflight": {},
        "manifest_version": 3,
    }


def _normalize_manifest(data: dict[str, Any], *, run_ts: str | None = None) -> dict[str, Any]:
    data.setdefault("run_timestamp", run_ts or get_run_timestamp())
    data.setdefault("updated_at", "")
    data.setdefault("stages", {})
    data.setdefault("runtime_context", {})
    data.setdefault("preflight", {})
    data.setdefault("manifest_version", 3)
    return data


def _lock_manifest_handle(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_manifest_handle(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _manifest_file_lock():
    lock_path = f"{get_manifest_path()}.lock"
    with open(lock_path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        _lock_manifest_handle(handle)
        try:
            yield
        finally:
            _unlock_manifest_handle(handle)


def _resolve_manifest_read_path() -> str:
    manifest_path = get_manifest_path()
    if os.path.exists(manifest_path):
        return manifest_path

    legacy_path = _get_legacy_manifest_path()
    if os.path.exists(legacy_path):
        return legacy_path

    return manifest_path


def _load_manifest_from_path(path: str, *, run_ts: str | None = None) -> dict[str, Any]:
    if not os.path.exists(path):
        return _build_default_manifest(run_ts)

    try:
        with open(path, "r", encoding=UTF8_FILE_ENCODING) as f:
            data = json.load(f)
    except Exception as exc:
        raise ManifestLoadError(path, exc) from exc

    if not isinstance(data, dict):
        raise ManifestLoadError(path, ValueError("manifest root is not a dict"))

    return _normalize_manifest(data, run_ts=run_ts)


def _backup_corrupt_manifest(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""

    backup_path = f"{path}.corrupt.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    os.replace(path, backup_path)
    return backup_path


def _write_manifest_file(path: str, manifest: dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    fd, temp_path = tempfile.mkstemp(prefix="manifest_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding=UTF8_FILE_ENCODING) as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _cleanup_legacy_manifest(current_path: str) -> None:
    legacy_path = _get_legacy_manifest_path()
    if legacy_path != current_path and os.path.exists(legacy_path):
        os.remove(legacy_path)


def _load_manifest_for_write(run_ts: str) -> dict[str, Any]:
    source_path = _resolve_manifest_read_path()
    try:
        return _load_manifest_from_path(source_path, run_ts=run_ts)
    except ManifestLoadError as exc:
        backup_path = _backup_corrupt_manifest(exc.path)
        manifest = _build_default_manifest(run_ts)
        manifest["updated_at"] = datetime.now().isoformat()
        manifest["corrupt_manifest"] = True
        if backup_path:
            manifest["corrupt_manifest_backup"] = backup_path
        return manifest


def load_run_manifest() -> dict[str, Any]:
    with _MANIFEST_LOCK:
        with _manifest_file_lock():
            source_path = _resolve_manifest_read_path()
            try:
                return _load_manifest_from_path(source_path)
            except ManifestLoadError as exc:
                manifest = _build_default_manifest()
                manifest["updated_at"] = datetime.now().isoformat()
                manifest["corrupt_manifest"] = True
                manifest["corrupt_manifest_path"] = exc.path
                return manifest


def update_run_manifest(
    stage: str,
    *,
    status: str,
    files: dict[str, str] | None = None,
    metrics: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> str:
    manifest_path = get_manifest_path()
    env_run_ts = os.environ.get(RUN_TIMESTAMP_ENV, "").strip()
    initial_run_ts = timestamp or env_run_ts or datetime.now().strftime("%Y%m%d%H%M%S")

    with _MANIFEST_LOCK:
        with _manifest_file_lock():
            manifest = _load_manifest_for_write(initial_run_ts)
            run_ts = timestamp or env_run_ts or manifest.get("run_timestamp") or initial_run_ts
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
            existing_metrics = stage_payload.setdefault("metrics", {})
            if status in SUCCESS_STATUSES and (not metrics or "error" not in metrics):
                existing_metrics.pop("error", None)
                existing_metrics.pop("failure_category", None)
                existing_metrics.pop("failure_message", None)
            if metrics:
                existing_metrics.update(metrics)

            _write_manifest_file(manifest_path, manifest)
            _cleanup_legacy_manifest(manifest_path)

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
        with _manifest_file_lock():
            manifest = _load_manifest_for_write(run_ts)
            manifest["run_timestamp"] = run_ts
            manifest["updated_at"] = datetime.now().isoformat()
            manifest["manifest_version"] = 3

            if runtime_context:
                manifest.setdefault("runtime_context", {}).update(runtime_context)
            if preflight is not None:
                manifest["preflight"] = preflight

            _write_manifest_file(manifest_path, manifest)
            _cleanup_legacy_manifest(manifest_path)

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
