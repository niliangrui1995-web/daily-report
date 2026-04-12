from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from artifact_store import ensure_dir, get_run_lock_path


class PipelineLockError(RuntimeError):
    def __init__(self, info: dict[str, Any] | None = None) -> None:
        super().__init__("pipeline is already running")
        self.info = info or {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def read_run_lock() -> dict[str, Any] | None:
    lock_path = get_run_lock_path()
    if not os.path.exists(lock_path):
        return None
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def is_lock_stale(info: dict[str, Any] | None, *, stale_after_seconds: int) -> bool:
    if not info:
        return True

    pid = info.get("pid")
    started_at = info.get("started_at")
    if pid and not _pid_is_running(int(pid)):
        return True
    if not started_at:
        return True

    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return True

    age = (datetime.now(timezone.utc) - started).total_seconds()
    return age > stale_after_seconds


def clear_run_lock() -> None:
    lock_path = get_run_lock_path()
    if os.path.exists(lock_path):
        os.remove(lock_path)


@contextmanager
def acquire_run_lock(
    *,
    command: str,
    stale_after_seconds: int = 6 * 60 * 60,
) -> Iterator[dict[str, Any]]:
    lock_path = get_run_lock_path()
    ensure_dir(os.path.dirname(lock_path))

    existing = read_run_lock()
    if existing and is_lock_stale(existing, stale_after_seconds=stale_after_seconds):
        clear_run_lock()
        existing = None

    if existing:
        raise PipelineLockError(existing)

    payload = {
        "pid": os.getpid(),
        "command": command,
        "started_at": _utc_now(),
    }

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PipelineLockError(read_run_lock()) from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        yield payload
    finally:
        current = read_run_lock()
        if current and current.get("pid") == payload["pid"]:
            clear_run_lock()
