# -*- coding: utf-8 -*-
"""Regression tests for control-plane state, subprocess results, and run locks."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _successful_preflight(
    pipeline_cli,
    monkeypatch,
    tmp_path: Path,
    *,
    stub_model_probe: bool = True,
) -> None:
    today_dir = tmp_path / "today"
    logs_dir = tmp_path / "logs"
    output_root = tmp_path / "output"
    today_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    output_root.mkdir(exist_ok=True)

    monkeypatch.setattr(pipeline_cli, "OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(pipeline_cli, "settings_today_output_dir", lambda: str(today_dir))
    monkeypatch.setattr(pipeline_cli, "get_today_logs_dir", lambda: str(logs_dir))
    monkeypatch.setattr(pipeline_cli, "require_prompt", lambda _name: "ok")
    monkeypatch.setattr(pipeline_cli, "SEARCH_API_KEY_POOL", ["key-1"])
    monkeypatch.setattr(pipeline_cli, "ANTIGRAVITY_API_KEY", "secret")
    monkeypatch.setattr(pipeline_cli, "PRO_OPUS_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setattr(pipeline_cli, "PRO_OPUS_API_KEY", "secret")
    monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "smtp-secret")
    monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "receiver@example.com")
    if stub_model_probe:
        monkeypatch.setattr(
            pipeline_cli,
            "_probe_local_model_endpoint",
            lambda _url, _key: (True, "authenticated status=200"),
        )
    monkeypatch.setattr(pipeline_cli, "GROK_API_KEY", "optional")
    monkeypatch.setattr(pipeline_cli, "INSECURE_TLS_ALLOWLIST", ())
    monkeypatch.setattr(pipeline_cli.importlib, "import_module", lambda _module_name: object())
    monkeypatch.setattr(pipeline_cli, "get_calendar_status", lambda: {"mode": "chinese_calendar"})
    monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _day: True)


def test_strict_preflight_rejects_unverified_external_model_endpoint(monkeypatch, tmp_path):
    import pipeline_cli
    import requests

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path, stub_model_probe=False)
    monkeypatch.setattr(pipeline_cli, "PRO_OPUS_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setattr(pipeline_cli, "validate_model_base_url", lambda _url: (True, False, ""))

    class DeniedResponse:
        status_code = 403

    class DeniedSession:
        def __init__(self):
            self.trust_env = True

        def get(self, _url, **_kwargs):
            return DeniedResponse()

        def close(self):
            return None

    monkeypatch.setattr(requests, "Session", DeniedSession)

    result = pipeline_cli._collect_preflight(strict=True)

    model_endpoint = next(item for item in result["checks"] if item["name"] == "model_endpoint")
    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_CHECK_FAILED
    assert result["summary"]["opus_model_route"]["validated"] is False
    assert model_endpoint["ok"] is False
    assert "HTTP 403" in model_endpoint["detail"]


def test_model_probe_rejects_redirect_without_following_it(monkeypatch):
    import pipeline_cli
    import requests

    monkeypatch.setattr(
        pipeline_cli,
        "validate_model_base_url",
        lambda _base_url: (True, False, ""),
    )

    class RedirectResponse:
        status_code = 302

    sessions = []

    class RedirectSession:
        def __init__(self):
            self.trust_env = True
            sessions.append(self)

        def get(self, url, **kwargs):
            assert url == "https://models.example.test/v1/models"
            assert kwargs["allow_redirects"] is False
            return RedirectResponse()

        def close(self):
            return None

    monkeypatch.setattr(requests, "Session", RedirectSession)

    verified, detail = pipeline_cli._probe_local_model_endpoint(
        "https://models.example.test/v1",
        "test-api-key",
    )

    assert verified is False
    assert "HTTP 302" in detail
    assert sessions[0].trust_env is False


def test_standalone_preflight_preserves_run_timestamp(monkeypatch, tmp_path):
    import artifact_store
    import pipeline_cli

    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.delenv(artifact_store.RUN_TIMESTAMP_ENV, raising=False)
    artifact_store.update_run_manifest("p9", status="completed_email_sent", timestamp="20260709083000")
    monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, "20260710230000")

    result = {
        "strict": True,
        "checks": [],
        "summary": {"total": 0, "failed": 0, "warnings": 0, "exit_code": 0},
    }
    monkeypatch.setattr(pipeline_cli, "_collect_preflight", lambda strict: result)
    monkeypatch.setattr(pipeline_cli, "_print_preflight", lambda _result: 0)
    assert pipeline_cli._run_preflight(strict=True) == 0
    manifest = artifact_store.load_run_manifest()
    assert manifest["run_timestamp"] == "20260709083000"


def test_preflight_only_manifest_has_explicit_status():
    import pipeline_cli

    state = pipeline_cli._manifest_run_state(
        {
            "run_timestamp": "20260710220000",
            "preflight": {"exit_code": 0},
            "runtime_context": {"preflight_exit_code": 0},
            "stages": {},
        }
    )

    assert state["status"] == "preflight_passed_no_run"
    assert state["root_cause"] == "preflight_only"
    assert state["main_flow_entered"] is False


def test_run_lock_race_does_not_mutate_active_run_manifest(monkeypatch):
    import pipeline_cli

    successful_result = {
        "strict": True,
        "checks": [],
        "summary": {"total": 0, "failed": 0, "warnings": 0, "exit_code": pipeline_cli.EXIT_OK},
    }

    class LockRace:
        def __enter__(self):
            raise pipeline_cli.PipelineLockError({"pid": 1234, "command": "run"})

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(pipeline_cli, "_collect_preflight", lambda strict: successful_result)
    monkeypatch.setattr(pipeline_cli, "acquire_run_lock", lambda **_kwargs: LockRace())
    monkeypatch.setattr(
        pipeline_cli,
        "update_manifest_context",
        lambda **_kwargs: pytest.fail("lock loser must not mutate manifest context"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "record_run_state",
        lambda **_kwargs: pytest.fail("lock loser must not overwrite active run state"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "configure_run_logging",
        lambda: pytest.fail("lock loser must not configure shared run logging"),
    )

    assert pipeline_cli._run_orchestrator([], command_name="run") == pipeline_cli.EXIT_LOCK_ACTIVE


@pytest.mark.parametrize(
    "status",
    [
        "failed_empty_output_raw_p1_fallback",
        "failed_exception_fallback_empty",
        "failed_plan_fallback",
        "failed_exception_fallback",
        "fallback_raw_llm",
    ],
)
def test_fallback_statuses_are_centrally_classified_as_degraded(status):
    import artifact_store

    assert artifact_store.classify_stage_status(status) == "degraded"
    degraded = artifact_store.summarize_stage_degradations(
        {"stages": {"stage": {"status": status, "metrics": {"fallback_reason": "upstream failed"}}}}
    )
    assert degraded == [{"stage": "stage", "status": status, "reason": "upstream failed"}]


def test_normal_skip_is_not_degraded():
    import artifact_store

    assert artifact_store.classify_stage_status("skipped_no_results") == "skipped"
    assert artifact_store.summarize_stage_degradations(
        {"stages": {"research": {"status": "skipped_no_results", "metrics": {}}}}
    ) == []


@pytest.mark.parametrize("status", ["failed_timeout", "failed_empty_output"])
def test_terminal_stage_failures_are_included_in_degradation_summary(status):
    import artifact_store

    assert artifact_store.classify_stage_status(status) == "failed"
    assert artifact_store.summarize_stage_degradations(
        {"stages": {"stage": {"status": status, "metrics": {"error": "stage failed"}}}}
    ) == [{"stage": "stage", "status": status, "reason": "stage failed"}]


@pytest.mark.parametrize("status", ["skipped_missing_dependency", "skipped_no_key", "skipped_no_vipdoc"])
def test_missing_runtime_capability_is_degraded_not_a_normal_skip(status):
    import artifact_store

    assert artifact_store.classify_stage_status(status) == "degraded"
    assert artifact_store.summarize_stage_degradations(
        {"stages": {"stage": {"status": status, "metrics": {}}}}
    ) == [{"stage": "stage", "status": status, "reason": ""}]


class _CapturingQueue:
    def __init__(self, signal: dict | None = None):
        self.signal = signal
        self.items: list[dict] = []

    def put(self, item):
        self.items.append(item)

    def get(self, timeout=None):
        if self.signal is None:
            raise RuntimeError("empty")
        signal = self.signal
        self.signal = None
        return signal

    def get_nowait(self):
        return self.get()

    def close(self):
        return None

    def join_thread(self):
        return None


def test_stage_entrypoint_reports_context_write_failure(monkeypatch):
    import pipeline_runtime

    queue = _CapturingQueue()
    monkeypatch.setattr(
        pipeline_runtime,
        "_write_context_to_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    pipeline_runtime._stage_process_entrypoint(
        pipeline_runtime.StageSpec(name="report", handler=lambda context: context.update(done=True)),
        {},
        queue,
        "unused.json",
    )

    assert queue.items[-1]["status"] == "error"
    assert "disk full" in queue.items[-1]["error"]


def test_stage_subprocess_missing_result_file_is_error(monkeypatch, tmp_path):
    import pipeline_runtime

    queue = _CapturingQueue({"status": "success", "context_sha256": "expected"})

    class FakeProcess:
        pid = 12345
        exitcode = 0

        def __init__(self, target, args, name):
            pass

        def start(self):
            return None

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

    class FakeMpContext:
        def Queue(self, maxsize=0):
            return queue

        def Process(self, target, args, name):
            return FakeProcess(target, args, name)

    monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda _method: FakeMpContext())
    monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))

    result = pipeline_runtime._run_stage_in_subprocess(
        pipeline_runtime.StageSpec(name="report", handler=lambda context: None, timeout_seconds=5),
        {"parent": True},
    )

    assert result["status"] == "error"
    assert "context result" in result["error"]
    assert "context" not in result


def test_context_result_is_atomic_and_checksum_validated(tmp_path):
    import pipeline_runtime

    context_path = tmp_path / "result.json"
    digest = pipeline_runtime._write_context_to_file({"result": "ok"}, str(context_path))

    payload = json.loads(context_path.read_text(encoding="utf-8-sig"))
    payload["context"]["result"] = "tampered"
    context_path.write_text(json.dumps(payload), encoding="utf-8-sig")

    assert isinstance(digest, str) and len(digest) == 64
    assert pipeline_runtime._read_context_from_file(str(context_path), expected_sha256=digest) is None
    assert not context_path.exists()


def test_subprocess_result_paths_are_unique(monkeypatch, tmp_path):
    import pipeline_runtime

    captured_paths: list[str] = []

    class FakeProcess:
        pid = 12345
        exitcode = 0

        def __init__(self, target, args, name):
            self.args = args

        def start(self):
            result_path = self.args[3]
            captured_paths.append(result_path)
            pipeline_runtime._write_context_to_file({"child": True}, result_path)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

    class FakeMpContext:
        def Queue(self, maxsize=0):
            return _CapturingQueue({"status": "success"})

        def Process(self, target, args, name):
            return FakeProcess(target, args, name)

    monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda _method: FakeMpContext())
    monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))

    stage = pipeline_runtime.StageSpec(name="same-stage", handler=lambda context: None, timeout_seconds=5)
    assert pipeline_runtime._run_stage_in_subprocess(stage, {})["status"] == "success"
    assert pipeline_runtime._run_stage_in_subprocess(stage, {})["status"] == "success"
    assert len(captured_paths) == 2
    assert captured_paths[0] != captured_paths[1]


def test_run_lock_exposes_missing_valid_invalid_states(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))

    assert pipeline_lock.get_run_lock_state(pipeline_lock.read_run_lock()) == "missing"

    lock_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "command": "run",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    valid = pipeline_lock.read_run_lock()
    assert pipeline_lock.get_run_lock_state(valid) == "valid"

    lock_path.write_text("{", encoding="utf-8")
    invalid = pipeline_lock.read_run_lock()
    assert pipeline_lock.get_run_lock_state(invalid) == "invalid"
    assert invalid["error"]


def test_fresh_invalid_lock_blocks_but_old_invalid_lock_recovers(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    lock_path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))

    with pytest.raises(pipeline_lock.PipelineLockError) as exc_info:
        with pipeline_lock.acquire_run_lock(command="run"):
            pass
    assert pipeline_lock.get_run_lock_state(exc_info.value.info) == "invalid"

    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp()
    os.utime(lock_path, (old, old))
    with pipeline_lock.acquire_run_lock(command="resume") as payload:
        assert payload["command"] == "resume"
        assert payload["owner_token"]

    assert not lock_path.exists()


def test_live_pid_with_fresh_heartbeat_is_not_stale(monkeypatch):
    import pipeline_lock

    monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda _pid: True)
    info = {
        "pid": 1234,
        "command": "run",
        "started_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }

    assert pipeline_lock.get_lock_stale_reason(info, stale_after_seconds=60) == ""


def test_live_reused_pid_cannot_keep_an_expired_heartbeat_lock(monkeypatch):
    import pipeline_lock

    monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda _pid: True)
    info = {
        "pid": 1234,
        "command": "run",
        "started_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }

    assert pipeline_lock.get_lock_stale_reason(info, stale_after_seconds=60) == "heartbeat_expired"


def test_lock_heartbeat_refresh_is_atomic_and_keeps_run_identity(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    moments = iter(["2026-07-10T00:00:00+00:00", "2026-07-10T00:00:30+00:00"])
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))
    monkeypatch.setattr(pipeline_lock, "_utc_now", lambda: next(moments))
    monkeypatch.setenv("VCP_RUN_TS", "20260710080000")

    with pipeline_lock.acquire_run_lock(
        command="run",
        heartbeat_interval_seconds=3600,
    ) as payload:
        assert payload["run_timestamp"] == "20260710080000"
        assert payload["run_day"] == "20260710"
        assert pipeline_lock.refresh_run_lock(payload) is True
        refreshed = pipeline_lock.read_run_lock()
        assert refreshed["heartbeat_at"] == "2026-07-10T00:00:30+00:00"
        assert refreshed["owner_token"] == payload["owner_token"]


def test_stale_heartbeat_cannot_overwrite_new_owner_during_handoff(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    old_payload = {
        "pid": 1111,
        "command": "old",
        "started_at": "2026-07-10T00:00:00+00:00",
        "heartbeat_at": "2026-07-10T00:00:00+00:00",
        "run_timestamp": "20260710080000",
        "run_day": "20260710",
        "owner_token": "old-owner",
    }
    lock_path.write_text(json.dumps(old_payload), encoding="utf-8")
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))
    monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda _pid: False)

    original_write = pipeline_lock._write_lock_payload_atomic
    old_write_entered = threading.Event()
    release_old_write = threading.Event()
    new_started = threading.Event()
    new_acquired = threading.Event()
    release_new_owner = threading.Event()
    new_payload = {}
    thread_errors = []

    def blocking_write(path, payload):
        if payload.get("owner_token") == "old-owner":
            old_write_entered.set()
            release_old_write.wait(2)
        return original_write(path, payload)

    def old_heartbeat():
        try:
            pipeline_lock.refresh_run_lock(old_payload)
        except Exception as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    def new_owner():
        try:
            new_started.set()
            with pipeline_lock.acquire_run_lock(
                command="new",
                heartbeat_interval_seconds=3600,
            ) as payload:
                new_payload.update(payload)
                new_acquired.set()
                release_new_owner.wait(2)
        except Exception as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    monkeypatch.setattr(pipeline_lock, "_write_lock_payload_atomic", blocking_write)
    old_thread = threading.Thread(target=old_heartbeat)
    new_thread = threading.Thread(target=new_owner)
    old_thread.start()
    assert old_write_entered.wait(1)
    new_thread.start()
    assert new_started.wait(1)

    acquired_before_old_refresh_finished = new_acquired.wait(0.1)
    release_old_write.set()
    old_thread.join(2)
    assert new_acquired.wait(2)
    current = pipeline_lock.read_run_lock()
    release_new_owner.set()
    new_thread.join(2)

    assert acquired_before_old_refresh_finished is False
    assert not thread_errors
    assert current["owner_token"] == new_payload["owner_token"]


def test_heartbeat_loop_retries_transient_refresh_error(monkeypatch):
    import pipeline_lock

    calls = []

    def flaky_refresh(_payload):
        calls.append("refresh")
        if len(calls) == 1:
            raise OSError("temporary sharing violation")
        return False

    monkeypatch.setattr(pipeline_lock, "refresh_run_lock", flaky_refresh)

    pipeline_lock._heartbeat_loop(threading.Event(), {"owner_token": "owner"}, 0.001)

    assert calls == ["refresh", "refresh"]


def test_heartbeat_loop_resumes_on_next_cycle_after_retry_budget(monkeypatch):
    import pipeline_lock

    calls = []

    def temporarily_unavailable(_payload):
        calls.append("refresh")
        if len(calls) <= pipeline_lock.HEARTBEAT_REFRESH_RETRIES:
            raise OSError("temporary sharing violation")
        return False

    monkeypatch.setattr(pipeline_lock, "refresh_run_lock", temporarily_unavailable)

    pipeline_lock._heartbeat_loop(threading.Event(), {"owner_token": "owner"}, 0.001)

    assert len(calls) == pipeline_lock.HEARTBEAT_REFRESH_RETRIES + 1


def test_heartbeat_start_failure_cleans_owned_lock(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))
    monkeypatch.setattr(
        pipeline_lock.threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        with pipeline_lock.acquire_run_lock(command="run"):
            pytest.fail("the context must not be entered when heartbeat startup fails")

    assert not lock_path.exists()


def test_lock_cleanup_requires_matching_owner_token(monkeypatch, tmp_path):
    import pipeline_lock

    lock_path = tmp_path / "run.lock"
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))

    with pipeline_lock.acquire_run_lock(command="run") as payload:
        replacement = {**payload, "owner_token": "different-owner"}
        lock_path.write_text(json.dumps(replacement), encoding="utf-8")

    assert lock_path.exists()
    remaining = pipeline_lock.read_run_lock()
    assert remaining["owner_token"] == "different-owner"


@pytest.mark.parametrize("lock_kind", ["missing", "invalid"])
def test_status_surfaces_running_manifest_lock_inconsistency(monkeypatch, tmp_path, capsys, lock_kind):
    import pipeline_cli
    import pipeline_lock

    manifest = {
        "run_timestamp": "20260710083000",
        "status": "running",
        "root_cause": "orchestrator_started",
        "main_flow_entered": True,
        "runtime_context": {},
        "stages": {"orchestrator": {"status": "running"}},
    }
    lock_path = tmp_path / "run.lock"
    if lock_kind == "invalid":
        lock_path.write_text("{", encoding="utf-8")

    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))
    monkeypatch.setattr(pipeline_cli, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pipeline_cli, "get_manifest_path", lambda day_dir=None: str(tmp_path / "run_manifest.json"))
    monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pipeline_cli, "load_run_manifest", lambda day_dir=None: manifest)
    monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _day: True)

    assert pipeline_cli.cmd_status(SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert f"lock: {lock_kind}" in output
    assert f"control_plane_inconsistency: manifest=running lock={lock_kind}" in output


def test_preflight_treats_fresh_invalid_lock_as_active(monkeypatch, tmp_path):
    import pipeline_cli
    import pipeline_lock

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))

    result = pipeline_cli._collect_preflight(strict=True)
    run_lock = next(item for item in result["checks"] if item["name"] == "run_lock")

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_LOCK_ACTIVE
    assert run_lock["ok"] is False
    assert "invalid" in run_lock["detail"]


def test_preflight_can_explicitly_allow_only_maintenance_lock(monkeypatch, tmp_path):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    lock_info = {
        "pid": 1234,
        "command": "maintenance",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "run_timestamp": "20260710110000",
        "run_day": "20260710",
        "owner_token": "maintenance-owner",
    }
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: lock_info)
    monkeypatch.setattr(pipeline_cli, "get_run_lock_state", lambda _info: "valid")
    monkeypatch.setattr(pipeline_cli, "get_lock_stale_reason", lambda *_args, **_kwargs: "")

    blocked = pipeline_cli._collect_preflight(strict=True)
    allowed = pipeline_cli._collect_preflight(strict=True, allow_maintenance_lock=True)

    assert blocked["summary"]["exit_code"] == pipeline_cli.EXIT_LOCK_ACTIVE
    assert allowed["summary"]["exit_code"] == pipeline_cli.EXIT_OK
    run_lock = next(item for item in allowed["checks"] if item["name"] == "run_lock")
    assert run_lock == {
        "name": "run_lock",
        "ok": True,
        "detail": "maintenance_allowed pid=1234",
        "severity": "error",
    }


def test_preflight_fails_when_local_model_endpoint_is_unreachable(monkeypatch, tmp_path):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline_cli,
        "_probe_local_model_endpoint",
        lambda _url, _key: (False, "connection refused"),
    )
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)

    result = pipeline_cli._collect_preflight(strict=True)
    model_endpoint = next(item for item in result["checks"] if item["name"] == "model_endpoint")

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_CHECK_FAILED
    assert model_endpoint == {
        "name": "model_endpoint",
        "ok": False,
        "detail": "connection refused",
        "severity": "error",
    }


def test_missing_primary_model_blocks_p6_p9_before_orchestrator(monkeypatch, tmp_path):
    """A stale primary must fail preflight without entering either daily LLM stage."""
    from contextlib import contextmanager

    import pipeline_cli
    import requests

    _successful_preflight(
        pipeline_cli,
        monkeypatch,
        tmp_path,
        stub_model_probe=False,
    )
    requests_seen = []
    orchestrator_calls = []
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": pipeline_cli.OPUS_FALLBACK_MODEL}]}

    class FakeSession:
        trust_env = True

        def get(self, url, **_kwargs):
            requests_seen.append(("GET", url))
            return FakeResponse()

        def post(self, *_args, **_kwargs):
            pytest.fail("preflight must not send a P6/P9 model request")

        def close(self):
            return None

    @contextmanager
    def fake_lock(**_kwargs):
        yield {"owner_token": "test-owner"}

    real_collect_preflight = pipeline_cli._collect_preflight

    def capture_preflight(strict, **kwargs):
        result = real_collect_preflight(strict, **kwargs)
        captured["result"] = result
        return result

    monkeypatch.setattr(requests, "Session", FakeSession)
    monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)
    monkeypatch.setattr(pipeline_cli, "_resolve_preflight_resume_stage", lambda _flags: "none")
    monkeypatch.setattr(pipeline_cli, "_collect_preflight", capture_preflight)
    monkeypatch.setattr(pipeline_cli, "_print_preflight", lambda _result: pipeline_cli.EXIT_CHECK_FAILED)
    monkeypatch.setitem(
        sys.modules,
        "orchestrator",
        SimpleNamespace(orchestrator_main=lambda flags: orchestrator_calls.append(list(flags))),
    )

    result = pipeline_cli._run_orchestrator([], command_name="run")

    assert result == pipeline_cli.EXIT_CHECK_FAILED
    assert requests_seen == [("GET", "http://127.0.0.1:8317/v1/models")]
    assert orchestrator_calls == []
    model_endpoint = next(
        item
        for item in captured["result"]["checks"]
        if item["name"] == "model_endpoint"
    )
    assert model_endpoint["ok"] is False
    assert pipeline_cli.OPUS_MODEL in model_endpoint["detail"]
    assert captured["result"]["summary"]["opus_model_route"] == {
        "primary_model": pipeline_cli.OPUS_MODEL,
        "fallback_model": pipeline_cli.OPUS_FALLBACK_MODEL,
        "validated": False,
    }


def test_preflight_reports_same_endpoint_opus_fallback(monkeypatch, tmp_path):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)

    result = pipeline_cli._collect_preflight(strict=True)
    fallback_check = next(item for item in result["checks"] if item["name"] == "opus_fallback")

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_OK
    assert fallback_check == {
        "name": "opus_fallback",
        "ok": True,
        "detail": "same endpoint model failover host=127.0.0.1",
        "severity": "error",
    }


@pytest.mark.parametrize(
    ("resume_stage", "expected_checks"),
    [
        (
            "delivery_resume",
            {"output_root", "today_output", "logs_dir", "email_credentials", "run_lock"},
        ),
        ("roi_resume", {"output_root", "today_output", "logs_dir", "run_lock"}),
        ("p9_done", {"output_root", "today_output", "logs_dir", "run_lock"}),
        (
            "manual_confirmation_required",
            {"output_root", "today_output", "logs_dir", "run_lock"},
        ),
    ],
)
def test_recovery_preflight_only_checks_capabilities_needed_by_resume_stage(
    monkeypatch,
    tmp_path,
    resume_stage,
    expected_checks,
):
    import pipeline_cli

    today_dir = tmp_path / "today"
    logs_dir = tmp_path / "logs"
    output_root = tmp_path / "output"
    today_dir.mkdir()
    logs_dir.mkdir()
    output_root.mkdir()

    monkeypatch.setattr(pipeline_cli, "OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(pipeline_cli, "settings_today_output_dir", lambda: str(today_dir))
    monkeypatch.setattr(pipeline_cli, "get_today_logs_dir", lambda: str(logs_dir))
    monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "smtp-secret")
    monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "receiver@example.com")
    monkeypatch.setattr(
        pipeline_cli,
        "_probe_local_model_endpoint",
        lambda *_args: pytest.fail("recovery-only preflight must not probe the model endpoint"),
    )
    monkeypatch.setattr(
        pipeline_cli.importlib,
        "import_module",
        lambda *_args: pytest.fail("recovery-only preflight must not import unrelated providers"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "get_calendar_status",
        lambda: pytest.fail("recovery-only preflight must not probe the trading calendar"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "require_prompt",
        lambda *_args: pytest.fail("recovery-only preflight must not load generation prompts"),
    )
    owned_lock = {
        "pid": os.getpid(),
        "command": "resume",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "owner_token": "owned-token",
    }
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: dict(owned_lock))
    monkeypatch.setattr(pipeline_cli, "get_run_lock_state", lambda _info: "valid")

    result = pipeline_cli._collect_preflight(
        strict=True,
        resume_stage=resume_stage,
        expected_lock_owner_token="owned-token",
    )

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_OK
    assert {item["name"] for item in result["checks"]} == expected_checks
    run_lock = next(item for item in result["checks"] if item["name"] == "run_lock")
    assert run_lock["ok"] is True
    assert "owned" in run_lock["detail"]


def test_delivery_preflight_allows_local_only_pending_finalize_without_email(
    monkeypatch,
    tmp_path,
):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "")
    monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "")
    monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "")
    monkeypatch.setattr(
        pipeline_cli,
        "load_run_manifest",
        lambda: {
            "stages": {
                "p9": {
                    "status": "report_saved_pending_delivery",
                    "metrics": {"email_configured": False},
                }
            }
        },
    )
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)

    result = pipeline_cli._collect_preflight(strict=True, resume_stage="delivery_resume")

    email_check = next(item for item in result["checks"] if item["name"] == "email_credentials")
    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_OK
    assert email_check["ok"] is True
    assert "not required" in email_check["detail"]


def test_delivery_preflight_requires_receiver_for_original_email_delivery(
    monkeypatch,
    tmp_path,
):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "")
    monkeypatch.setattr(
        pipeline_cli,
        "load_run_manifest",
        lambda: {"stages": {"p9": {"metrics": {"email_configured": True}}}},
    )
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)

    result = pipeline_cli._collect_preflight(strict=True, resume_stage="delivery_resume")

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_CHECK_FAILED


def test_normal_preflight_keeps_full_strict_dependency_checks(monkeypatch, tmp_path):
    import pipeline_cli

    _successful_preflight(pipeline_cli, monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)

    result = pipeline_cli._collect_preflight(strict=True, resume_stage="p6_resume")
    check_names = {item["name"] for item in result["checks"]}

    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_OK
    assert {
        "search_keys",
        "model_endpoint",
        "grok_key",
        "prompt:p9_report.txt",
        "calendar_probe",
        "import:openai",
    }.issubset(check_names)


def test_run_locks_before_resolving_resume_profile_and_running_preflight(monkeypatch):
    from contextlib import contextmanager

    import pipeline_cli

    calls = []
    successful_result = {
        "checks": [],
        "summary": {
            "total": 0,
            "failed": 0,
            "warnings": 0,
            "exit_code": pipeline_cli.EXIT_OK,
        },
    }

    @contextmanager
    def fake_lock(**kwargs):
        calls.append(("lock", kwargs))
        yield {"owner_token": "owned-token"}

    def fake_preflight(strict, **kwargs):
        calls.append(("preflight", strict, kwargs))
        return successful_result

    monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
    monkeypatch.setattr(
        pipeline_cli,
        "_resolve_preflight_resume_stage",
        lambda flags: calls.append(("resume_plan", list(flags))) or "delivery_resume",
    )
    monkeypatch.setattr(pipeline_cli, "_collect_preflight", fake_preflight)
    monkeypatch.setattr(pipeline_cli, "set_preflight_summary", lambda summary: calls.append(("summary", summary)))
    monkeypatch.setattr(pipeline_cli, "configure_run_logging", lambda: calls.append(("logging",)))
    monkeypatch.setattr(pipeline_cli, "update_manifest_context", lambda **kwargs: calls.append(("context", kwargs)))
    monkeypatch.setitem(
        sys.modules,
        "orchestrator",
        SimpleNamespace(orchestrator_main=lambda flags: calls.append(("orchestrator", list(flags)))),
    )

    result = pipeline_cli._run_orchestrator([], command_name="resume")

    call_names = [item[0] for item in calls]
    assert result == pipeline_cli.EXIT_OK
    assert call_names.index("lock") < call_names.index("resume_plan") < call_names.index("preflight")
    preflight_call = next(item for item in calls if item[0] == "preflight")
    assert preflight_call == (
        "preflight",
        True,
        {
            "resume_stage": "delivery_resume",
            "expected_lock_owner_token": "owned-token",
        },
    )
    assert ("orchestrator", []) in calls


@pytest.mark.parametrize("command_name", ["run", "resume"])
def test_manual_confirmation_checkpoint_stops_after_local_preflight(
    monkeypatch,
    command_name,
    capsys,
):
    from contextlib import contextmanager

    import pipeline_cli

    calls = []
    successful_result = {
        "checks": [],
        "summary": {
            "total": 0,
            "failed": 0,
            "warnings": 0,
            "exit_code": pipeline_cli.EXIT_OK,
        },
    }

    @contextmanager
    def fake_lock(**kwargs):
        calls.append(("lock", kwargs))
        yield {"owner_token": "owned-token"}

    def fake_preflight(strict, **kwargs):
        calls.append(("preflight", strict, kwargs))
        return successful_result

    monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
    monkeypatch.setattr(
        pipeline_cli,
        "_resolve_preflight_resume_stage",
        lambda _flags: "manual_confirmation_required",
    )
    monkeypatch.setattr(pipeline_cli, "_collect_preflight", fake_preflight)
    monkeypatch.setattr(
        pipeline_cli,
        "set_preflight_summary",
        lambda _summary: pytest.fail("manual confirmation must not update run state"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "configure_run_logging",
        lambda: pytest.fail("manual confirmation must not initialize execution logging"),
    )
    monkeypatch.setattr(
        pipeline_cli,
        "update_manifest_context",
        lambda **_kwargs: pytest.fail("manual confirmation must not mutate the manifest"),
    )
    monkeypatch.setitem(
        sys.modules,
        "orchestrator",
        SimpleNamespace(
            orchestrator_main=lambda _flags: pytest.fail(
                "manual confirmation must not enter the orchestrator"
            )
        ),
    )

    result = pipeline_cli._run_orchestrator([], command_name=command_name)

    assert result == pipeline_cli.EXIT_MANUAL_CONFIRMATION_REQUIRED == 3
    assert calls == [
        ("lock", {"command": command_name}),
        (
            "preflight",
            True,
            {
                "resume_stage": "manual_confirmation_required",
                "expected_lock_owner_token": "owned-token",
            },
        ),
    ]
    assert "manual_confirmation_required" in capsys.readouterr().out.lower()


def test_force_restart_uses_full_preflight_without_reading_checkpoint(monkeypatch):
    import pipeline_cli

    monkeypatch.setitem(
        sys.modules,
        "checkpoint",
        SimpleNamespace(detect_checkpoint=lambda: pytest.fail("force restart must ignore checkpoint")),
    )

    assert pipeline_cli._resolve_preflight_resume_stage(["--force-restart"]) == "none"
