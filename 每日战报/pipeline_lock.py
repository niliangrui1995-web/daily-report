from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from artifact_store import ensure_dir, get_run_lock_path


LOCK_STATE_FIELD = "_lock_state"
LOCK_STATE_MISSING = "missing"
LOCK_STATE_VALID = "valid"
LOCK_STATE_INVALID = "invalid"
INVALID_LOCK_GRACE_SECONDS = 30
LOCK_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_STALE_AFTER_SECONDS = 10 * 60
HEARTBEAT_REFRESH_RETRIES = 3


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


def _invalid_lock_info(lock_path: str, error: Exception | str) -> dict[str, Any]:
    info: dict[str, Any] = {
        LOCK_STATE_FIELD: LOCK_STATE_INVALID,
        "path": lock_path,
        "error": str(error),
    }
    try:
        stat = os.stat(lock_path)
        info.update(
            {
                "file_age_seconds": max(0.0, datetime.now(timezone.utc).timestamp() - stat.st_mtime),
                "file_mtime_ns": stat.st_mtime_ns,
                "file_size": stat.st_size,
            }
        )
    except OSError:
        pass
    return info


def _validate_lock_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("lock root is not an object")

    pid = data.get("pid")
    if isinstance(pid, bool):
        raise ValueError("lock pid is invalid")
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise ValueError("lock pid is missing or invalid") from exc
    if normalized_pid <= 0:
        raise ValueError("lock pid must be positive")

    command = str(data.get("command") or "").strip()
    if not command:
        raise ValueError("lock command is missing")

    started_at = data.get("started_at")
    try:
        datetime.fromisoformat(str(started_at))
    except (TypeError, ValueError) as exc:
        raise ValueError("lock started_at is missing or invalid") from exc

    owner_token = data.get("owner_token")
    if owner_token is not None and not str(owner_token).strip():
        raise ValueError("lock owner_token is invalid")

    payload = dict(data)
    payload["pid"] = normalized_pid
    payload["command"] = command
    payload[LOCK_STATE_FIELD] = LOCK_STATE_VALID
    return payload


def read_run_lock() -> dict[str, Any] | None:
    lock_path = get_run_lock_path()
    if not os.path.exists(lock_path):
        return None
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _validate_lock_payload(data)
    except FileNotFoundError:
        return None
    except Exception as exc:
        return _invalid_lock_info(lock_path, exc)


def get_run_lock_state(info: dict[str, Any] | None) -> str:
    if info is None:
        return LOCK_STATE_MISSING
    state = str(info.get(LOCK_STATE_FIELD) or "").strip().lower()
    if state == LOCK_STATE_INVALID:
        return LOCK_STATE_INVALID
    return LOCK_STATE_VALID


def is_lock_stale(info: dict[str, Any] | None, *, stale_after_seconds: int) -> bool:
    return bool(get_lock_stale_reason(info, stale_after_seconds=stale_after_seconds))


def get_lock_stale_reason(info: dict[str, Any] | None, *, stale_after_seconds: int) -> str:
    state = get_run_lock_state(info)
    if state == LOCK_STATE_MISSING:
        return "missing_lock"
    if state == LOCK_STATE_INVALID:
        try:
            file_age = float(info.get("file_age_seconds", 0.0))
        except (TypeError, ValueError):
            file_age = 0.0
        if file_age > INVALID_LOCK_GRACE_SECONDS:
            return "invalid_lock_expired"
        return ""

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

    heartbeat_at = info.get("heartbeat_at")
    if heartbeat_at:
        try:
            heartbeat = datetime.fromisoformat(str(heartbeat_at))
        except (TypeError, ValueError):
            return "invalid_heartbeat_at"
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        heartbeat_age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if heartbeat_age > stale_after_seconds:
            return "heartbeat_expired"
    elif (datetime.now(timezone.utc) - started).total_seconds() > stale_after_seconds:
        return "heartbeat_missing_expired"

    return ""


def _lock_identity_matches(current: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
    current_state = get_run_lock_state(current)
    expected_state = get_run_lock_state(expected)
    if current_state != expected_state or current_state == LOCK_STATE_MISSING:
        return False

    if current_state == LOCK_STATE_INVALID:
        return all(
            current.get(key) == expected.get(key)
            for key in ("file_mtime_ns", "file_size")
        )

    current_token = str(current.get("owner_token") or "").strip()
    expected_token = str(expected.get("owner_token") or "").strip()
    if current_token or expected_token:
        return bool(current_token and current_token == expected_token)
    return all(current.get(key) == expected.get(key) for key in ("pid", "command", "started_at"))


@contextmanager
def _lock_metadata_guard(lock_path: str) -> Iterator[None]:
    guard_path = f"{lock_path}.guard"
    ensure_dir(os.path.dirname(guard_path))
    with open(guard_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _clear_run_lock_unlocked(
    lock_path: str,
    *,
    expected: dict[str, Any] | None = None,
) -> bool:
    if expected is not None and not _lock_identity_matches(read_run_lock(), expected):
        return False
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        return False
    return True


def clear_run_lock(*, expected: dict[str, Any] | None = None) -> bool:
    lock_path = get_run_lock_path()
    with _lock_metadata_guard(lock_path):
        return _clear_run_lock_unlocked(lock_path, expected=expected)


def _write_lock_payload_atomic(lock_path: str, payload: dict[str, Any]) -> None:
    lock_dir = ensure_dir(os.path.dirname(lock_path))
    fd, temp_path = tempfile.mkstemp(prefix=".run-lock-", suffix=".tmp", dir=lock_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, lock_path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def refresh_run_lock(expected: dict[str, Any]) -> bool:
    lock_path = get_run_lock_path()
    with _lock_metadata_guard(lock_path):
        current = read_run_lock()
        if not _lock_identity_matches(current, expected):
            return False

        heartbeat_at = _utc_now()
        updated = {key: value for key, value in current.items() if key != LOCK_STATE_FIELD}
        updated["heartbeat_at"] = heartbeat_at
        _write_lock_payload_atomic(lock_path, updated)
        expected["heartbeat_at"] = heartbeat_at
        return True


def _heartbeat_loop(
    stop_event: threading.Event,
    payload: dict[str, Any],
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        for attempt in range(HEARTBEAT_REFRESH_RETRIES):
            try:
                if not refresh_run_lock(payload):
                    return
                break
            except OSError as exc:
                logging.warning(
                    "pipeline lock heartbeat refresh failed (%s/%s): %s",
                    attempt + 1,
                    HEARTBEAT_REFRESH_RETRIES,
                    exc,
                )
                if attempt + 1 >= HEARTBEAT_REFRESH_RETRIES:
                    logging.error(
                        "pipeline lock heartbeat refresh exhausted retries; "
                        "retrying on the next interval"
                    )
                    break
                if stop_event.wait(min(1.0, max(0.01, interval_seconds))):
                    return
            except Exception:
                logging.exception("pipeline lock heartbeat stopped after unexpected refresh failure")
                return


@contextmanager
def acquire_run_lock(
    *,
    command: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_interval_seconds: float = LOCK_HEARTBEAT_INTERVAL_SECONDS,
) -> Iterator[dict[str, Any]]:
    lock_path = get_run_lock_path()
    ensure_dir(os.path.dirname(lock_path))

    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds must be positive")

    now = _utc_now()
    run_timestamp = os.environ.get("VCP_RUN_TS", "").strip() or datetime.now().strftime("%Y%m%d%H%M%S")
    payload = {
        "pid": os.getpid(),
        "command": command,
        "started_at": now,
        "heartbeat_at": now,
        "run_timestamp": run_timestamp,
        "run_day": run_timestamp[:8],
        "owner_token": secrets.token_urlsafe(24),
    }

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_event, payload, float(heartbeat_interval_seconds)),
        name="pipeline-lock-heartbeat",
        daemon=True,
    )
    with _lock_metadata_guard(lock_path):
        existing = read_run_lock()
        if get_run_lock_state(existing) != LOCK_STATE_MISSING and is_lock_stale(
            existing,
            stale_after_seconds=stale_after_seconds,
        ):
            if not _clear_run_lock_unlocked(lock_path, expected=existing):
                raise PipelineLockError(read_run_lock())
            existing = None

        if get_run_lock_state(existing) != LOCK_STATE_MISSING:
            raise PipelineLockError(existing)

        _write_lock_payload_atomic(lock_path, payload)

    heartbeat_started = False
    try:
        heartbeat_thread.start()
        heartbeat_started = True
        yield payload
    finally:
        stop_event.set()
        if heartbeat_started:
            heartbeat_thread.join(timeout=5)
        clear_run_lock(expected=payload)
