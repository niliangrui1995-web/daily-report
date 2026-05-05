# -*- coding: utf-8 -*-
"""Hardening tests for runtime control, TLS policy, and preflight checks."""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def slow_stage_handler(context: dict) -> None:
    time.sleep(float(context.get("sleep_seconds", 0)))
    context["completed"] = True


def _reload_module(name: str):
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


class TestPipelineRuntimeTimeout:
    def test_stage_timeout_marks_manifest(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VCP_STAGE_EXECUTION_MODE", "process")

        import artifact_store
        from pipeline_runtime import StageSpec, StageTimeoutError, run_stage_registry

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        with pytest.raises(StageTimeoutError):
            run_stage_registry(
                [StageSpec(name="slow_stage", handler=slow_stage_handler, timeout_seconds=1)],
                {"sleep_seconds": 2},
            )

        manifest = artifact_store.load_run_manifest()
        assert manifest["stages"]["slow_stage"]["status"] == "failed_timeout"
        assert manifest["stages"]["slow_stage"]["metrics"]["timeout_seconds"] == 1


class TestSettingsHardening:
    def test_no_proxy_merge_and_tls_allowlist(self, monkeypatch):
        original_no_proxy = os.environ.get("NO_PROXY")
        original_no_proxy_lower = os.environ.get("no_proxy")
        original_allowlist = os.environ.get("INSECURE_TLS_ALLOWLIST")

        try:
            monkeypatch.setenv("NO_PROXY", "corp.local,localhost")
            monkeypatch.setenv("INSECURE_TLS_ALLOWLIST", "self-signed.local, Example.org")

            settings = _reload_module("settings")
            http_security = _reload_module("http_security")

            merged = set(part.strip().lower() for part in settings.MERGED_NO_PROXY.split(","))
            assert {"corp.local", "localhost", "127.0.0.1", "::1"}.issubset(merged)
            assert settings.MERGED_NO_PROXY == os.environ["NO_PROXY"]
            assert settings.MERGED_NO_PROXY == os.environ["no_proxy"]

            insecure_policy = http_security.get_tls_policy_for_url("https://news.self-signed.local/article")
            assert insecure_policy["verify"] is False
            assert insecure_policy["tls_mode"] == "insecure_allowlisted"

            strict_policy = http_security.get_tls_policy_for_url("https://secure.example.net/article")
            assert strict_policy["verify"] is True
            assert strict_policy["tls_mode"] == "strict"
        finally:
            if original_no_proxy is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = original_no_proxy

            if original_no_proxy_lower is None:
                os.environ.pop("no_proxy", None)
            else:
                os.environ["no_proxy"] = original_no_proxy_lower

            if original_allowlist is None:
                os.environ.pop("INSECURE_TLS_ALLOWLIST", None)
            else:
                os.environ["INSECURE_TLS_ALLOWLIST"] = original_allowlist

            _reload_module("settings")
            _reload_module("http_security")


class TestP1WebTimeParsing:
    def test_parse_failed_items_are_dropped(self):
        from p1_web import _entries_to_p1_result

        result = _entries_to_p1_result(
            [
                {
                    "title": "Broken date item",
                    "content": "",
                    "url": "https://example.com/broken",
                    "published_at": "totally-not-a-date",
                    "source": "Example",
                }
            ],
            "网页采集",
            "Example",
            24,
        )

        assert result is None

    def test_unknown_time_is_retained_but_marked(self):
        from p1_web import _entries_to_p1_result

        result = _entries_to_p1_result(
            [
                {
                    "title": "Missing date item",
                    "content": "",
                    "url": "https://example.com/missing",
                    "published_at": "",
                    "source": "Example",
                }
            ],
            "网页采集",
            "Example",
            24,
        )

        assert result is not None
        assert result["grounding_sources"][0]["time_status"] == "unknown_time"
        assert "unknown_time" in result["text"]


class TestManifestContext:
    def test_manifest_root_contains_runtime_context_and_preflight(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        artifact_store.update_manifest_context(
            runtime_context={"python_version": "3.12.0", "cwd": "D:/workspace"},
            preflight={"exit_code": 0, "search_keys_count": 3},
            timestamp="20260413010101",
        )
        manifest = artifact_store.load_run_manifest()

        assert manifest["manifest_version"] == 3
        assert manifest["runtime_context"]["python_version"] == "3.12.0"
        assert manifest["preflight"]["exit_code"] == 0

    def test_manifest_is_written_at_day_root_and_migrates_legacy_file(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        legacy_dir = tmp_path / "meta"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / artifact_store.MANIFEST_FILENAME
        legacy_path.write_text(
            json.dumps(
                {
                    "run_timestamp": "20260413010101",
                    "updated_at": "",
                    "stages": {"p1": {"status": "completed"}},
                    "runtime_context": {},
                    "preflight": {},
                    "manifest_version": 3,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        artifact_store.update_run_manifest("p2", status="completed", timestamp="20260413010101")

        manifest_path = tmp_path / artifact_store.MANIFEST_FILENAME
        assert manifest_path.exists()
        assert not legacy_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["stages"]["p1"]["status"] == "completed"
        assert manifest["stages"]["p2"]["status"] == "completed"


class TestReportStage:
    def test_stage_report_raises_when_report_is_empty(self, monkeypatch):
        import orchestrator

        monkeypatch.setattr(orchestrator, "p9_risk_analysis", lambda **kwargs: ("", ""))
        monkeypatch.setattr(orchestrator, "send_report_email", lambda content, now_str="": {"ok": True, "status": "completed_no_email"})
        monkeypatch.setattr(orchestrator, "_run_agent_report_review", lambda context: None)

        with pytest.raises(RuntimeError, match="empty report"):
            orchestrator._stage_report({})

    def test_stage_report_raises_when_delivery_fails(self, monkeypatch):
        import orchestrator

        monkeypatch.setattr(orchestrator, "p9_risk_analysis", lambda **kwargs: ("# report", "20260413010101"))
        monkeypatch.setattr(
            orchestrator,
            "send_report_email",
            lambda content, now_str="": {"ok": False, "status": "completed_email_failed"},
        )
        monkeypatch.setattr(orchestrator, "_run_agent_report_review", lambda context: None)

        with pytest.raises(RuntimeError, match="completed_email_failed"):
            orchestrator._stage_report({})


class TestPreflightChecks:
    def test_run_blocks_before_orchestrator_when_strict_preflight_fails(self, monkeypatch):
        import pipeline_cli

        calls = []
        failing_result = {
            "strict": True,
            "checks": [
                {
                    "name": "antigravity_key",
                    "ok": False,
                    "detail": "missing",
                    "severity": "error",
                }
            ],
            "summary": {
                "total": 1,
                "failed": 1,
                "warnings": 0,
                "exit_code": pipeline_cli.EXIT_CHECK_FAILED,
            },
        }

        @contextmanager
        def fake_lock(**_kwargs):
            calls.append("lock")
            yield {}

        monkeypatch.setattr(pipeline_cli, "_collect_preflight", lambda strict: failing_result)
        monkeypatch.setattr(pipeline_cli, "_print_preflight", lambda result: result["summary"]["exit_code"])
        monkeypatch.setattr(pipeline_cli, "set_preflight_summary", lambda summary: calls.append(("summary", summary)))
        monkeypatch.setattr(pipeline_cli, "explain_preflight_and_record", lambda result: calls.append("agent"))
        monkeypatch.setattr(pipeline_cli, "configure_run_logging", lambda: calls.append("logging"))
        monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
        monkeypatch.setitem(
            sys.modules,
            "orchestrator",
            SimpleNamespace(orchestrator_main=lambda flags: calls.append(("orchestrator", flags))),
        )

        result = pipeline_cli._run_orchestrator([], command_name="run")

        assert result == pipeline_cli.EXIT_CHECK_FAILED
        assert "lock" not in calls
        assert not any(call[0] == "orchestrator" for call in calls if isinstance(call, tuple))

    def test_preflight_strict_returns_lock_exit_code(self, monkeypatch, tmp_path):
        import pipeline_cli

        today_dir = tmp_path / "today"
        logs_dir = tmp_path / "logs"
        today_dir.mkdir()
        logs_dir.mkdir()

        monkeypatch.setattr(pipeline_cli, "OUTPUT_DIR", str(tmp_path / "output"))
        monkeypatch.setattr(pipeline_cli, "settings_today_output_dir", lambda: str(today_dir))
        monkeypatch.setattr(pipeline_cli, "get_today_logs_dir", lambda: str(logs_dir))
        monkeypatch.setattr(pipeline_cli, "require_prompt", lambda name: "ok")
        monkeypatch.setattr(pipeline_cli, "SEARCH_API_KEY_POOL", ["key-1"])
        monkeypatch.setattr(pipeline_cli, "ANTIGRAVITY_API_KEY", "secret")
        monkeypatch.setattr(pipeline_cli, "GROK_API_KEY", "optional")
        monkeypatch.setattr(pipeline_cli, "INSECURE_TLS_ALLOWLIST", ("self-signed.local",))
        monkeypatch.setattr(
            pipeline_cli.importlib,
            "import_module",
            lambda module_name: object(),
        )
        monkeypatch.setattr(pipeline_cli, "get_calendar_status", lambda: {"mode": "chinese_calendar"})
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: True)
        monkeypatch.setattr(
            pipeline_cli,
            "read_run_lock",
            lambda: {"pid": 1234, "command": "run", "started_at": "2026-04-13T00:00:00+00:00"},
        )
        monkeypatch.setattr(pipeline_cli, "is_lock_stale", lambda info, stale_after_seconds: False)

        result = pipeline_cli._collect_preflight(strict=True)
        assert result["summary"]["exit_code"] == pipeline_cli.EXIT_LOCK_ACTIVE
        assert any(item["name"] == "run_lock" and not item["ok"] for item in result["checks"])


class TestPipelineLock:
    def test_acquire_run_lock_clears_dead_windows_lock(self, monkeypatch, tmp_path):
        import pipeline_lock

        lock_dir = tmp_path / "_system"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "run.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 22016,
                    "command": "run",
                    "started_at": "2026-04-22T00:30:07.282288+00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(pipeline_lock, "get_run_lock_path", lambda: str(lock_path))
        monkeypatch.setattr(pipeline_lock.os, "name", "nt")
        monkeypatch.setattr(pipeline_lock, "_pid_is_running_windows", lambda pid: False)

        with pipeline_lock.acquire_run_lock(command="resume") as payload:
            assert payload["command"] == "resume"
            current = pipeline_lock.read_run_lock()
            assert current is not None
            assert current["command"] == "resume"

        assert not lock_path.exists()


class TestTradingCalendarStatus:
    def test_calendar_status_exposes_package_and_import_names(self):
        from trading_calendar import CALENDAR_IMPORT_NAME, CALENDAR_PACKAGE_NAME, get_calendar_status

        status = get_calendar_status()

        assert status["package_name"] == CALENDAR_PACKAGE_NAME
        assert status["import_name"] == CALENDAR_IMPORT_NAME
        if status["library_available"]:
            assert status["package_version"]
