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


def _pid_is_running_windows(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            return True
        if error == ERROR_INVALID_PARAMETER:
            return False
        return False

    exit_code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(int(pid))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    except Exception:
        return False
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
    return bool(get_lock_stale_reason(info, stale_after_seconds=stale_after_seconds))


def get_lock_stale_reason(info: dict[str, Any] | None, *, stale_after_seconds: int) -> str:
    if not info:
        return "missing_lock"

    pid = info.get("pid")
    started_at = info.get("started_at")
    if pid:
        try:
            running = _pid_is_running(int(pid))
        except (TypeError, ValueError):
            return "invalid_pid"
        if not running:
            return "pid_not_running"
    else:
        return "missing_pid"
    if not started_at:
        return "missing_started_at"

    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return "invalid_started_at"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - started).total_seconds()
    if age > stale_after_seconds:
        return "age_exceeded"
    return ""


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
