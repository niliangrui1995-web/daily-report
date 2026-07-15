from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_artifact_store_error_utf8_and_path_helpers(tmp_path, monkeypatch):
    import artifact_store

    error = ValueError("broken")
    wrapped = artifact_store.ManifestLoadError("manifest.json", error)
    assert wrapped.path == "manifest.json"
    assert wrapped.error is error
    assert "broken" in str(wrapped)

    calls = []

    class Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise OSError("cannot reconfigure")

    monkeypatch.setattr(artifact_store.sys, "stdout", Stream())
    monkeypatch.setattr(artifact_store.sys, "stderr", Stream())
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    artifact_store.configure_utf8_environment()
    assert calls[0] == {"encoding": "utf-8", "errors": "replace"}
    assert artifact_store.os.environ["PYTHONUTF8"] == "1"

    output = tmp_path / "output"
    today = output / "20260714"
    monkeypatch.setattr(artifact_store, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(today))
    assert artifact_store.get_output_root_dir() == str(output)
    assert artifact_store.get_global_system_dir() == str(output / "_system")
    assert artifact_store.get_run_lock_path().endswith("run.lock")
    assert artifact_store.get_day_subdir("unknown") == str(today)
    assert artifact_store.get_today_logs_dir() == str(today / "logs")
    assert artifact_store.get_pipeline_log_path().endswith("pipeline.log")
    assert artifact_store.get_error_log_path().endswith("error.log")
    assert artifact_store.extract_timestamp_from_name("") == ""


def test_artifact_store_json_ready_classification_and_degradation_summary():
    import artifact_store

    converted = artifact_store._json_ready(
        {
            1: date(2026, 7, 14),
            "nested": (datetime(2026, 7, 14, 9), {date(2026, 7, 15)}),
        }
    )
    assert converted["1"] == "2026-07-14"
    assert converted["nested"][0].startswith("2026-07-14T09:00:00")

    assert artifact_store.classify_stage_status("fallback_used") == "degraded"
    assert artifact_store.classify_stage_status("anything", {"structured_degraded": True}) == "degraded"
    assert artifact_store.classify_stage_status("skipped_other") == "skipped"
    assert artifact_store.classify_stage_status("completed_custom") == "success"
    assert artifact_store.classify_stage_status("timeout") == "failed"
    assert artifact_store.classify_stage_status("retrying") == "active"
    assert artifact_store.classify_stage_status("completed_email_failed") == "degraded"
    assert artifact_store.classify_stage_status("mystery") == "unknown"

    assert artifact_store.summarize_stage_degradations({"stages": []}) == []
    summary = artifact_store.summarize_stage_degradations(
        {
            "stages": {
                "invalid": "not-a-dict",
                "ok": {"status": "completed"},
                "bad-metrics": {"status": "failed", "metrics": "bad", "error": "payload error"},
                "degraded": {
                    "status": "completed_structured_degraded",
                    "metrics": {"fallback_reason": "adapter down"},
                },
            }
        }
    )
    assert summary == [
        {"stage": "bad-metrics", "status": "failed", "reason": "payload error"},
        {
            "stage": "degraded",
            "status": "completed_structured_degraded",
            "reason": "adapter down",
        },
    ]

    active_summary = artifact_store.summarize_stage_degradations(
        {
            "stages": {
                "orchestrator": {"status": "running"},
                "p2": {"status": "retrying"},
            }
        }
    )
    assert active_summary == [
        {"stage": "p2", "status": "retrying", "reason": "unfinished stage state"}
    ]


def test_reset_run_manifest_archives_previous_run_and_starts_empty(tmp_path, monkeypatch):
    import artifact_store

    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
    old_ts = "20260715070000"
    new_ts = "20260715090000"
    artifact_store.set_run_timestamp(old_ts)
    artifact_store.update_run_manifest(
        "p9",
        status="completed_email_sent",
        files={"md": str(tmp_path / f"战报_{old_ts}.md")},
        timestamp=old_ts,
    )
    artifact_store.record_run_state(
        status="completed",
        root_cause="completed",
        timestamp=old_ts,
    )

    archive_path = artifact_store.reset_run_manifest(new_ts)
    current = artifact_store.load_run_manifest()
    archived = json.loads(Path(archive_path).read_text(encoding=artifact_store.UTF8_FILE_ENCODING))

    assert current["run_timestamp"] == new_ts
    assert current["stages"] == {}
    assert current["status"] == "pending"
    assert current["runtime_context"]["previous_run_timestamp"] == old_ts
    assert current["runtime_context"]["previous_manifest_archive"] == archive_path
    assert archived["run_timestamp"] == old_ts
    assert archived["stages"]["p9"]["status"] == "completed_email_sent"


def test_reset_run_manifest_advances_same_second_and_rejects_stale_caller_timestamp(
    tmp_path,
    monkeypatch,
):
    import artifact_store

    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
    previous_ts = "20260715090000"
    expected_ts = "20260715090001"
    monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, previous_ts)
    artifact_store.update_run_manifest(
        "p9",
        status="completed_email_sent",
        timestamp=previous_ts,
    )

    artifact_store.reset_run_manifest(previous_ts)

    current = artifact_store.load_run_manifest()
    assert current["run_timestamp"] == expected_ts
    assert os.environ[artifact_store.RUN_TIMESTAMP_ENV] == expected_ts

    # The orchestrator already holds the pre-reset candidate in a local variable.
    # A later explicit write with that stale value must not regress the new run.
    artifact_store.update_run_manifest(
        "orchestrator",
        status="started",
        timestamp=previous_ts,
    )
    artifact_store.update_manifest_context(
        runtime_context={"resume_from": "none"},
        timestamp=previous_ts,
    )
    artifact_store.record_run_state(
        status="running",
        root_cause="orchestrator_started",
        timestamp=previous_ts,
    )
    current = artifact_store.load_run_manifest()
    assert current["run_timestamp"] == expected_ts
    assert current["stages"]["orchestrator"]["run_timestamp"] == expected_ts


def test_clean_success_clears_transient_failure_metrics(tmp_path, monkeypatch):
    import artifact_store

    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
    artifact_store.update_run_manifest(
        "p3",
        status="failed_exception",
        metrics={
            "error": "route down",
            "failure_category": "network_error",
            "degrade_reason": "all routes failed",
            "stop_reason": "budget_exhausted",
            "structured_degraded": True,
        },
        timestamp="20260715090000",
    )

    artifact_store.update_run_manifest(
        "p3",
        status="completed",
        metrics={"content_chars": 120},
        timestamp="20260715090000",
    )

    metrics = artifact_store.load_run_manifest()["stages"]["p3"]["metrics"]
    assert metrics == {"content_chars": 120}


def test_metrics_only_bookkeeping_does_not_touch_stage_output_time(tmp_path, monkeypatch):
    import artifact_store

    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
    artifact_store.update_run_manifest(
        "p3",
        status="completed",
        files={"md": str(tmp_path / "p3.md")},
        timestamp="20260715090000",
    )
    first = artifact_store.load_run_manifest()["stages"]["p3"]["output_updated_at"]

    artifact_store.update_run_manifest(
        "p3",
        status="completed",
        metrics={"roi_score": 0.5},
        timestamp="20260715090000",
    )

    payload = artifact_store.load_run_manifest()["stages"]["p3"]
    assert payload["output_updated_at"] == first
    assert payload["metrics"]["roi_score"] == 0.5


def test_artifact_store_posix_manifest_lock_helpers(monkeypatch):
    import artifact_store

    calls = []
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda fileno, operation: calls.append((fileno, operation)),
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(artifact_store.os, "name", "posix")

    class Handle:
        def seek(self, offset):
            calls.append(("seek", offset))

        def fileno(self):
            return 9

    handle = Handle()
    artifact_store._lock_manifest_handle(handle)
    artifact_store._unlock_manifest_handle(handle)
    assert (9, 1) in calls
    assert (9, 2) in calls


def test_artifact_store_manifest_load_backup_and_corruption_paths(tmp_path, monkeypatch):
    import artifact_store

    missing = tmp_path / "missing.json"
    default = artifact_store._load_manifest_from_path(str(missing), run_ts="20260714090000")
    assert default["run_timestamp"] == "20260714090000"

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("not json", encoding=artifact_store.UTF8_FILE_ENCODING)
    with pytest.raises(artifact_store.ManifestLoadError):
        artifact_store._load_manifest_from_path(str(invalid_json))

    invalid_root = tmp_path / "root.json"
    invalid_root.write_text("[]", encoding=artifact_store.UTF8_FILE_ENCODING)
    with pytest.raises(artifact_store.ManifestLoadError, match="root is not a dict"):
        artifact_store._load_manifest_from_path(str(invalid_root))

    assert artifact_store._backup_corrupt_manifest("") == ""
    backup_source = tmp_path / "backup.json"
    backup_source.write_text("broken", encoding="utf-8")
    backup_path = artifact_store._backup_corrupt_manifest(str(backup_source))
    assert backup_path
    assert not backup_source.exists()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("broken", encoding=artifact_store.UTF8_FILE_ENCODING)
    monkeypatch.setattr(artifact_store, "_resolve_manifest_read_path", lambda _day_dir=None: str(corrupt))
    recovered = artifact_store._load_manifest_for_write("20260714090000")
    assert recovered["corrupt_manifest"] is True
    assert recovered["corrupt_manifest_backup"]

    corrupt_read = tmp_path / "corrupt-read.json"
    corrupt_read.write_text("broken", encoding=artifact_store.UTF8_FILE_ENCODING)
    monkeypatch.setattr(artifact_store, "_resolve_manifest_read_path", lambda _day_dir=None: str(corrupt_read))

    @contextmanager
    def no_lock(_day_dir=None):
        yield

    monkeypatch.setattr(artifact_store, "_manifest_file_lock", no_lock)
    loaded = artifact_store.load_run_manifest()
    assert loaded["corrupt_manifest"] is True
    assert loaded["corrupt_manifest_path"] == str(corrupt_read)


def test_artifact_store_preflight_environment_variants(monkeypatch):
    import artifact_store

    artifact_store.set_preflight_summary({"exit_code": 0})
    assert artifact_store.get_preflight_summary() == {"exit_code": 0}
    monkeypatch.setenv(artifact_store.PREFLIGHT_SUMMARY_ENV, "[]")
    assert artifact_store.get_preflight_summary() == {}
    monkeypatch.setenv(artifact_store.PREFLIGHT_SUMMARY_ENV, "not-json")
    assert artifact_store.get_preflight_summary() == {}
    artifact_store.set_preflight_summary(None)
    assert artifact_store.get_preflight_summary() == {}


def test_artifact_store_context_and_run_state_optional_branches(monkeypatch):
    import artifact_store

    written = []

    @contextmanager
    def no_lock(_day_dir=None):
        yield

    monkeypatch.setattr(artifact_store, "get_manifest_path", lambda _day_dir=None: "manifest.json")
    monkeypatch.setattr(artifact_store, "_manifest_file_lock", no_lock)
    monkeypatch.setattr(artifact_store, "_cleanup_legacy_manifest", lambda *_args: None)
    monkeypatch.setattr(
        artifact_store,
        "_write_manifest_file",
        lambda path, manifest: written.append((path, json.loads(json.dumps(manifest)))),
    )

    manifest = {
        "run_timestamp": "20260714090000",
        "runtime_context": {"preflight_exit_code": 1},
        "preflight": {},
        "stages": {},
    }
    monkeypatch.setattr(artifact_store, "_load_manifest_for_write", lambda *_args, **_kwargs: manifest)
    artifact_store.update_manifest_context(preflight="invalid", timestamp="20260714090000")
    assert "preflight_exit_code" not in written[-1][1]["runtime_context"]

    manifest = {
        "run_timestamp": "20260714090000",
        "runtime_context": {},
        "preflight": {},
        "stages": {},
    }
    monkeypatch.setattr(artifact_store, "_load_manifest_for_write", lambda *_args, **_kwargs: manifest)
    artifact_store.update_manifest_context(
        runtime_context={"cwd": "repo"},
        preflight={"exit_code": 0},
        timestamp="20260714090000",
    )
    assert written[-1][1]["runtime_context"] == {"cwd": "repo", "preflight_exit_code": 0}

    failed_manifest = {
        "run_timestamp": "20260714090000",
        "status": "failed_timeout",
        "root_cause": "timeout",
        "status_reason": "old reason",
        "runtime_context": {},
        "stages": {},
    }
    monkeypatch.setattr(artifact_store, "_load_manifest_for_write", lambda *_args, **_kwargs: failed_manifest)
    artifact_store.record_run_state(
        status="running",
        root_cause="resume",
        timestamp="20260714090000",
    )
    context = written[-1][1]["runtime_context"]
    assert context["previous_run_state"] == "failed_timeout"
    assert context["previous_root_cause"] == "timeout"
    assert context["previous_status_reason"] == "old reason"
    assert "run_state_source" not in context
    assert "main_flow_entered" not in context
    assert "schedule_decision" not in context
