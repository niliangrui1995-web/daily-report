from __future__ import annotations

import builtins
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pipeline_runtime


def test_pipeline_lock_windows_pid_probe_and_import_failure(monkeypatch):
    import pipeline_lock

    assert pipeline_lock._pid_is_running_windows(os.getpid()) is True
    assert pipeline_lock._pid_is_running_windows(2_147_483_647) is False

    original_import = builtins.__import__

    def fail_ctypes(name, *args, **kwargs):
        if name == "ctypes":
            raise ImportError("ctypes unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_ctypes)
    assert pipeline_lock._pid_is_running_windows(os.getpid()) is False


def test_pipeline_lock_pid_probe_covers_posix_outcomes(monkeypatch):
    import pipeline_lock

    assert pipeline_lock._pid_is_running(None) is False
    monkeypatch.setattr(pipeline_lock.os, "name", "posix")

    monkeypatch.setattr(pipeline_lock.os, "kill", lambda *_args: None)
    assert pipeline_lock._pid_is_running(123) is True

    monkeypatch.setattr(
        pipeline_lock.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert pipeline_lock._pid_is_running(123) is True
    monkeypatch.setattr(
        pipeline_lock.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(OSError("missing")),
    )
    assert pipeline_lock._pid_is_running(123) is False
    monkeypatch.setattr(
        pipeline_lock.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    assert pipeline_lock._pid_is_running(123) is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "root"),
        ({"pid": True, "command": "run", "started_at": "2026-01-01"}, "pid"),
        ({"pid": "bad", "command": "run", "started_at": "2026-01-01"}, "pid"),
        ({"pid": 0, "command": "run", "started_at": "2026-01-01"}, "positive"),
        ({"pid": 1, "command": "", "started_at": "2026-01-01"}, "command"),
        ({"pid": 1, "command": "run", "started_at": "bad"}, "started_at"),
        ({"pid": 1, "command": "run", "started_at": "2026-01-01", "owner_token": ""}, "owner_token"),
    ],
)
def test_pipeline_lock_payload_validation_rejects_invalid_values(payload, message):
    import pipeline_lock

    with pytest.raises(ValueError, match=message):
        pipeline_lock._validate_lock_payload(payload)


def test_pipeline_lock_payload_validation_and_invalid_file_metadata(tmp_path):
    import pipeline_lock

    valid = pipeline_lock._validate_lock_payload(
        {
            "pid": "123",
            "command": " run ",
            "started_at": "2026-07-14T09:00:00+00:00",
            "owner_token": "owner",
        }
    )
    assert valid["pid"] == 123
    assert valid["command"] == "run"
    assert pipeline_lock.get_run_lock_state(valid) == pipeline_lock.LOCK_STATE_VALID

    lock_file = tmp_path / "run.lock"
    lock_file.write_text("broken", encoding="utf-8")
    info = pipeline_lock._invalid_lock_info(str(lock_file), "invalid json")
    assert info["file_size"] == 6
    assert info["file_age_seconds"] >= 0
    lock_file.unlink()
    missing_info = pipeline_lock._invalid_lock_info(str(lock_file), "gone")
    assert "file_size" not in missing_info


def test_pipeline_lock_read_handles_disappearing_file(tmp_path, monkeypatch):
    import pipeline_lock

    path = tmp_path / "run.lock"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(path))
    original_open = builtins.open

    def disappearing_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise FileNotFoundError(str(path))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", disappearing_open)
    assert pipeline_lock.read_run_lock() is None


def test_pipeline_lock_stale_reason_covers_all_invalid_and_time_paths(monkeypatch):
    import pipeline_lock

    assert pipeline_lock.get_lock_stale_reason(None, stale_after_seconds=60) == "missing_lock"
    invalid = {pipeline_lock.LOCK_STATE_FIELD: pipeline_lock.LOCK_STATE_INVALID, "file_age_seconds": "bad"}
    assert pipeline_lock.get_lock_stale_reason(invalid, stale_after_seconds=60) == ""
    invalid["file_age_seconds"] = pipeline_lock.INVALID_LOCK_GRACE_SECONDS + 1
    assert pipeline_lock.get_lock_stale_reason(invalid, stale_after_seconds=60) == "invalid_lock_expired"

    now = datetime.now(timezone.utc)
    base = {
        "pid": 123,
        "command": "run",
        "started_at": now.isoformat(),
        pipeline_lock.LOCK_STATE_FIELD: pipeline_lock.LOCK_STATE_VALID,
    }
    monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda _pid: False)
    assert pipeline_lock.get_lock_stale_reason(base, stale_after_seconds=60) == "pid_not_running"
    monkeypatch.setattr(
        pipeline_lock,
        "_pid_is_running",
        lambda _pid: (_ for _ in ()).throw(ValueError("invalid")),
    )
    assert pipeline_lock.get_lock_stale_reason(base, stale_after_seconds=60) == "invalid_pid"
    monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda _pid: True)
    assert pipeline_lock.get_lock_stale_reason({**base, "pid": None}, stale_after_seconds=60) == "missing_pid"
    assert pipeline_lock.get_lock_stale_reason({**base, "started_at": None}, stale_after_seconds=60) == "missing_started_at"
    assert pipeline_lock.get_lock_stale_reason({**base, "started_at": "bad"}, stale_after_seconds=60) == "invalid_started_at"
    assert pipeline_lock.get_lock_stale_reason({**base, "heartbeat_at": "bad"}, stale_after_seconds=60) == "invalid_heartbeat_at"

    old_naive = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120)).isoformat()
    assert pipeline_lock.get_lock_stale_reason(
        {**base, "started_at": old_naive, "heartbeat_at": old_naive},
        stale_after_seconds=60,
    ) == "heartbeat_expired"
    assert pipeline_lock.get_lock_stale_reason(
        {**base, "started_at": old_naive},
        stale_after_seconds=60,
    ) == "heartbeat_missing_expired"
    assert pipeline_lock.is_lock_stale(base, stale_after_seconds=60) is False


def test_pipeline_lock_identity_matching_covers_tokens_invalid_and_legacy():
    import pipeline_lock

    valid = {
        pipeline_lock.LOCK_STATE_FIELD: pipeline_lock.LOCK_STATE_VALID,
        "pid": 1,
        "command": "run",
        "started_at": "time",
    }
    assert pipeline_lock._lock_identity_matches(None, valid) is False
    assert pipeline_lock._lock_identity_matches(valid, valid.copy()) is True
    assert pipeline_lock._lock_identity_matches({**valid, "owner_token": "a"}, {**valid, "owner_token": "a"}) is True
    assert pipeline_lock._lock_identity_matches({**valid, "owner_token": "a"}, {**valid, "owner_token": "b"}) is False
    invalid = {
        pipeline_lock.LOCK_STATE_FIELD: pipeline_lock.LOCK_STATE_INVALID,
        "file_mtime_ns": 1,
        "file_size": 2,
    }
    assert pipeline_lock._lock_identity_matches(invalid, invalid.copy()) is True
    assert pipeline_lock._lock_identity_matches(invalid, {**invalid, "file_size": 3}) is False


def test_pipeline_lock_metadata_guard_posix_and_clear_missing(tmp_path, monkeypatch):
    import pipeline_lock

    calls = []
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda _fd, operation: calls.append(operation),
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(pipeline_lock.os, "name", "posix")
    with pipeline_lock._lock_metadata_guard(str(tmp_path / "run.lock")):
        calls.append("body")
    assert calls == [1, "body", 2]
    assert pipeline_lock._clear_run_lock_unlocked(str(tmp_path / "missing.lock")) is False


def test_pipeline_lock_refresh_mismatch_heartbeat_unexpected_and_acquire_guards(monkeypatch):
    import pipeline_lock

    @contextmanager
    def no_guard(_path):
        yield

    monkeypatch.setattr(pipeline_lock, "_lock_metadata_guard", no_guard)
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: "run.lock")
    monkeypatch.setattr(pipeline_lock, "ensure_dir", lambda path: path)
    monkeypatch.setattr(pipeline_lock, "read_run_lock", lambda: None)
    assert pipeline_lock.refresh_run_lock({"owner_token": "owner"}) is False

    class OneInterval:
        def __init__(self):
            self.calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(
        pipeline_lock,
        "refresh_run_lock",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    pipeline_lock._heartbeat_loop(OneInterval(), {"owner_token": "owner"}, 0.01)

    with pytest.raises(ValueError, match="positive"):
        with pipeline_lock.acquire_run_lock(command="run", heartbeat_interval_seconds=0):
            pass

    existing = {pipeline_lock.LOCK_STATE_FIELD: pipeline_lock.LOCK_STATE_INVALID, "file_age_seconds": 999}
    monkeypatch.setattr(pipeline_lock, "read_run_lock", lambda: existing)
    monkeypatch.setattr(pipeline_lock, "_clear_run_lock_unlocked", lambda *_args, **_kwargs: False)
    with pytest.raises(pipeline_lock.PipelineLockError):
        with pipeline_lock.acquire_run_lock(command="run"):
            pass


class _FakeLogger:
    def __init__(self, handlers=None):
        self.handlers = list(handlers or [])
        self.level = None

    def setLevel(self, level):
        self.level = level

    def addHandler(self, handler):
        self.handlers.append(handler)


class _FakeHandler:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.level = None
        self.formatter = None

    def setLevel(self, level):
        self.level = level

    def setFormatter(self, formatter):
        self.formatter = formatter


def test_pipeline_runtime_configure_child_logging_paths(monkeypatch):
    import pipeline_runtime

    monkeypatch.setattr(pipeline_runtime, "configure_utf8_environment", lambda: None)
    existing = _FakeLogger([object()])
    fake_logging = SimpleNamespace(
        INFO=logging.INFO,
        ERROR=logging.ERROR,
        Formatter=logging.Formatter,
        StreamHandler=_FakeHandler,
        FileHandler=_FakeHandler,
        getLogger=lambda: existing,
    )
    monkeypatch.setattr(pipeline_runtime, "logging", fake_logging)
    pipeline_runtime._configure_child_logging()
    assert len(existing.handlers) == 1

    logger = _FakeLogger()
    fake_logging.getLogger = lambda: logger
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test")
    pipeline_runtime._configure_child_logging()
    assert len(logger.handlers) == 1

    logger = _FakeLogger()
    fake_logging.getLogger = lambda: logger
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(pipeline_runtime, "get_pipeline_log_path", lambda: "pipeline.log")
    monkeypatch.setattr(pipeline_runtime, "get_error_log_path", lambda: "error.log")
    pipeline_runtime._configure_child_logging()
    assert len(logger.handlers) == 3

    logger = _FakeLogger()
    fake_logging.getLogger = lambda: logger
    fake_logging.FileHandler = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("cannot open log")
    )
    pipeline_runtime._configure_child_logging()
    assert len(logger.handlers) == 1


def test_pipeline_runtime_registry_skips_until_start_and_retries_failures(monkeypatch):
    import pipeline_runtime

    calls = []
    original_run_single_stage = pipeline_runtime._run_single_stage
    monkeypatch.setattr(pipeline_runtime, "_run_single_stage", lambda stage, _context: calls.append(stage.name))
    stages = [
        pipeline_runtime.StageSpec("one", lambda _ctx: None),
        pipeline_runtime.StageSpec("two", lambda _ctx: None),
    ]
    pipeline_runtime.run_stage_registry(stages, {}, start_at="two")
    assert calls == ["two"]
    monkeypatch.setattr(pipeline_runtime, "_run_single_stage", original_run_single_stage)

    updates = []
    sleeps = []
    attempts = {"timeout": 0, "error": 0}
    monkeypatch.setattr(
        pipeline_runtime,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    monkeypatch.setattr(pipeline_runtime.time, "sleep", lambda seconds: sleeps.append(seconds))

    def timeout_once(_stage, context):
        attempts["timeout"] += 1
        if attempts["timeout"] == 1:
            raise pipeline_runtime.StageTimeoutError("timeout", 1)
        context["done"] = True

    monkeypatch.setattr(pipeline_runtime, "_execute_stage_handler", timeout_once)
    context = {}
    pipeline_runtime._run_single_stage(
        pipeline_runtime.StageSpec("timeout", lambda _ctx: None, timeout_seconds=1, retry_limit=1),
        context,
    )
    assert context["done"] is True

    def error_once(_stage, context):
        attempts["error"] += 1
        if attempts["error"] == 1:
            raise RuntimeError("transient")
        context["recovered"] = True

    monkeypatch.setattr(pipeline_runtime, "_execute_stage_handler", error_once)
    pipeline_runtime._run_single_stage(
        pipeline_runtime.StageSpec("error", lambda _ctx: None, retry_limit=1),
        context,
    )
    assert context["recovered"] is True
    assert sleeps == [1, 1]


def test_pipeline_runtime_rejects_unknown_resume_stage(monkeypatch):
    import pipeline_runtime

    calls = []
    monkeypatch.setattr(pipeline_runtime, "_run_single_stage", lambda stage, _context: calls.append(stage.name))

    with pytest.raises(ValueError, match="unknown start_at stage"):
        pipeline_runtime.run_stage_registry(
            [pipeline_runtime.StageSpec("known", lambda _ctx: None)],
            {},
            start_at="typo",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (pipeline_runtime.StageTimeoutError("optional", 1), "failed_timeout"),
        (RuntimeError("optional failed"), "failed_exception"),
    ],
)
def test_pipeline_runtime_optional_stage_failure_allows_downstream(
    monkeypatch,
    error,
    expected_status,
):
    import pipeline_runtime

    updates = []
    downstream = []
    monkeypatch.setattr(
        pipeline_runtime,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    def execute(stage, _context):
        if stage.name == "optional":
            raise error
        downstream.append(stage.name)

    monkeypatch.setattr(pipeline_runtime, "_execute_stage_handler", execute)
    stages = [
        pipeline_runtime.StageSpec(
            "optional",
            lambda _ctx: None,
            timeout_seconds=1,
            continue_on_error=True,
        ),
        pipeline_runtime.StageSpec("report", lambda _ctx: None),
    ]

    pipeline_runtime.run_stage_registry(stages, {})

    assert downstream == ["report"]
    optional_statuses = [payload["status"] for stage, payload in updates if stage == "optional"]
    assert optional_statuses[-1] == expected_status
    assert "completed" not in optional_statuses


def test_pipeline_runtime_single_stage_reraises_terminal_timeout_and_error(monkeypatch):
    import pipeline_runtime

    monkeypatch.setattr(pipeline_runtime, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline_runtime,
        "_execute_stage_handler",
        lambda *_args: (_ for _ in ()).throw(pipeline_runtime.StageTimeoutError("stage", 1)),
    )
    with pytest.raises(pipeline_runtime.StageTimeoutError):
        pipeline_runtime._run_single_stage(
            pipeline_runtime.StageSpec("stage", lambda _ctx: None, timeout_seconds=1),
            {},
        )
    monkeypatch.setattr(
        pipeline_runtime,
        "_execute_stage_handler",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fatal")),
    )
    with pytest.raises(RuntimeError, match="fatal"):
        pipeline_runtime._run_single_stage(pipeline_runtime.StageSpec("stage", lambda _ctx: None), {})


def test_pipeline_runtime_execute_handler_inline_timeout_error_and_context(monkeypatch):
    import pipeline_runtime

    context = {}
    pipeline_runtime._execute_stage_handler(
        pipeline_runtime.StageSpec("inline", lambda ctx: ctx.update(inline=True)),
        context,
    )
    assert context["inline"] is True

    monkeypatch.setattr(pipeline_runtime, "_stage_execution_mode", lambda: "inline")
    pipeline_runtime._execute_stage_handler(
        pipeline_runtime.StageSpec("inline-timeout", lambda ctx: ctx.update(second=True), timeout_seconds=1),
        context,
    )
    assert context["second"] is True

    monkeypatch.setattr(pipeline_runtime, "_stage_execution_mode", lambda: "process")
    monkeypatch.setattr(pipeline_runtime, "_run_stage_in_subprocess", lambda *_args: {"status": "timeout"})
    with pytest.raises(pipeline_runtime.StageTimeoutError):
        pipeline_runtime._execute_stage_handler(
            pipeline_runtime.StageSpec("timeout", lambda _ctx: None, timeout_seconds=1),
            context,
        )

    monkeypatch.setattr(
        pipeline_runtime,
        "_run_stage_in_subprocess",
        lambda *_args: {"status": "error", "error": "child failed", "traceback": "trace"},
    )
    with pytest.raises(RuntimeError, match="child failed"):
        pipeline_runtime._execute_stage_handler(
            pipeline_runtime.StageSpec("error", lambda _ctx: None, timeout_seconds=1),
            context,
        )

    monkeypatch.setattr(
        pipeline_runtime,
        "_run_stage_in_subprocess",
        lambda *_args: {"status": "success", "context": "not-a-dict"},
    )
    pipeline_runtime._execute_stage_handler(
        pipeline_runtime.StageSpec("no-update", lambda _ctx: None, timeout_seconds=1),
        context,
    )


def test_pipeline_runtime_resolve_signals_and_process_stopping(tmp_path):
    import pipeline_runtime

    path = tmp_path / "context.json"
    assert pipeline_runtime._resolve_stage_result_signal("bad", str(path))["status"] == "error"
    assert pipeline_runtime._resolve_stage_result_signal({"status": "error", "error": "x"}, str(path))["error"] == "x"
    assert pipeline_runtime._resolve_stage_result_signal({"status": "mystery"}, str(path))["status"] == "error"
    assert pipeline_runtime._resolve_stage_result_signal({"status": "success"}, str(path))["status"] == "error"

    events = []

    class Process:
        def __init__(self, alive_sequence, fail=False):
            self.alive = iter(alive_sequence)
            self.fail = fail

        def join(self, timeout):
            events.append(("join", timeout))
            if self.fail:
                raise RuntimeError("join failed")

        def is_alive(self):
            return next(self.alive, False)

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    pipeline_runtime._stop_process(Process([False]))
    pipeline_runtime._stop_process(Process([True, True, False]))
    pipeline_runtime._stop_process(Process([], fail=True))
    assert "terminate" in events
    assert "kill" in events


def test_pipeline_runtime_context_envelope_validation_and_cleanup(tmp_path, monkeypatch):
    import pipeline_runtime

    path = tmp_path / "context.json"
    assert pipeline_runtime._read_context_from_file(str(path)) is None

    path.write_text(json.dumps({"context_file_version": 1, "context": "bad", "sha256": "x"}), encoding="utf-8")
    assert pipeline_runtime._read_context_from_file(str(path)) is None

    path.write_text(json.dumps({"context_file_version": 1, "context": {"a": 1}, "sha256": "bad"}), encoding="utf-8")
    assert pipeline_runtime._read_context_from_file(str(path)) is None

    digest = pipeline_runtime._write_context_to_file({"a": 1}, str(path))
    assert pipeline_runtime._read_context_from_file(str(path), expected_sha256="wrong") is None
    digest = pipeline_runtime._write_context_to_file({"a": 1}, str(path))
    assert pipeline_runtime._read_context_from_file(str(path), expected_sha256=digest) == {"a": 1}

    path.write_text(json.dumps({"legacy": True}), encoding="utf-8")
    assert pipeline_runtime._read_context_from_file(str(path), expected_sha256="digest") is None
    path.write_text(json.dumps({"legacy": True}), encoding="utf-8")
    assert pipeline_runtime._read_context_from_file(str(path)) == {"legacy": True}

    cleanup_path = tmp_path / "cleanup.txt"
    cleanup_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_runtime.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(OSError("busy")),
    )
    pipeline_runtime._cleanup_temp_file(str(cleanup_path))


def test_pipeline_runtime_entrypoint_error_and_string_serialization_fallback(tmp_path, monkeypatch):
    import pipeline_runtime

    signals = []
    queue = SimpleNamespace(put=lambda value: signals.append(value))
    monkeypatch.setattr(pipeline_runtime, "_configure_child_logging", lambda: None)
    pipeline_runtime._stage_process_entrypoint(
        pipeline_runtime.StageSpec(
            "error",
            lambda _ctx: (_ for _ in ()).throw(RuntimeError("handler failed")),
        ),
        {},
        queue,
        str(tmp_path / "context.json"),
    )
    assert signals[-1]["status"] == "error"
    assert "handler failed" in signals[-1]["error"]

    original_dumps = pipeline_runtime.json.dumps

    def flaky_dumps(value, *args, **kwargs):
        if value == "recoverable":
            raise TypeError("temporary encoder failure")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(pipeline_runtime.json, "dumps", flaky_dumps)
    path = tmp_path / "serialized.json"
    pipeline_runtime._write_context_to_file(
        {"string": "recoverable", "object": object(), "normal": 1},
        str(path),
    )
    payload = json.loads(path.read_text(encoding=pipeline_runtime.UTF8_FILE_ENCODING))
    assert payload["context"] == {"string": "recoverable", "normal": 1}
