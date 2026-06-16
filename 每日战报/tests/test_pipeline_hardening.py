# -*- coding: utf-8 -*-
"""Hardening tests for runtime control, TLS policy, and preflight checks."""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
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
    def test_fast_subprocess_result_is_read_before_queue_close(self, monkeypatch, tmp_path):
        import pipeline_runtime

        class FakeQueue:
            def __init__(self):
                self.closed = False
                self.signal = {"status": "success"}

            def get(self, timeout=None):
                raise AssertionError("fast process should skip blocking queue read")

            def get_nowait(self):
                if self.closed:
                    raise ValueError("queue is closed")
                if self.signal is None:
                    raise RuntimeError("empty queue")
                signal = self.signal
                self.signal = None
                return signal

            def close(self):
                self.closed = True

            def join_thread(self):
                return None

        class FakeProcess:
            pid = 12345
            exitcode = 0

            def __init__(self, target, args, name):
                self.args = args

            def start(self):
                result_file_path = self.args[3]
                assert not os.path.exists(result_file_path)
                with open(result_file_path, "w", encoding="utf-8") as handle:
                    json.dump({"value": "child"}, handle)

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
                return FakeQueue()

            def Process(self, target, args, name):
                return FakeProcess(target, args, name)

        monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda method: FakeMpContext())
        monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
        stale_path = tmp_path / f"vcp_stage_fast_stage_{os.getpid()}.json"
        stale_path.write_text(json.dumps({"value": "stale"}), encoding="utf-8")

        result = pipeline_runtime._run_stage_in_subprocess(
            pipeline_runtime.StageSpec(name="fast_stage", handler=lambda context: None, timeout_seconds=5),
            {"value": "parent"},
        )

        assert result == {"status": "success", "context": {"value": "child"}}

    def test_context_file_roundtrips_utf8_sig_chinese(self, tmp_path):
        import pipeline_runtime

        context_path = tmp_path / "stage_context.json"

        pipeline_runtime._write_context_to_file(
            {
                "report": "战报",
                "mapping": {"track": "先进封装与混合键合"},
                "non_serializable": object(),
            },
            str(context_path),
        )

        assert context_path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert pipeline_runtime._read_context_from_file(str(context_path)) == {
            "report": "战报",
            "mapping": {"track": "先进封装与混合键合"},
        }
        assert not context_path.exists()

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
    def test_artifact_and_manifest_files_are_utf8_sig(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, "20260616083006")

        report_path = Path(artifact_store.write_text_artifact("战报", "# 战报\n生益科技"))
        mapping_path = Path(
            artifact_store.write_json_artifact(
                "P6_映射",
                {
                    "events": [
                        {
                            "track_name": "先进封装与混合键合",
                            "targets": [{"name": "新益昌", "elasticity": "🔥高弹性"}],
                        }
                    ]
                },
            )
        )
        manifest_path = Path(
            artifact_store.update_run_manifest(
                "p6",
                status="completed",
                files={"json": str(mapping_path), "md": str(report_path)},
                metrics={"note": "中文 manifest 正常"},
            )
        )

        for path in (report_path, mapping_path, manifest_path):
            assert path.read_bytes().startswith(b"\xef\xbb\xbf")

        assert "生益科技" in report_path.read_text(encoding=artifact_store.UTF8_FILE_ENCODING)
        mapping = json.loads(mapping_path.read_text(encoding=artifact_store.UTF8_FILE_ENCODING))
        assert mapping["events"][0]["track_name"] == "先进封装与混合键合"

        manifest = artifact_store.load_run_manifest()
        assert manifest["stages"]["p6"]["metrics"]["note"] == "中文 manifest 正常"
        assert manifest["stages"]["p6"]["files"]["json"].endswith("P6_映射_20260616083006.json")

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

        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        assert manifest["stages"]["p1"]["status"] == "completed"
        assert manifest["stages"]["p2"]["status"] == "completed"

    def test_success_status_clears_stale_error_metrics(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        artifact_store.update_run_manifest(
            "p9",
            status="failed_parse",
            metrics={"error": "invalid json", "raw_output_chars": 100},
            timestamp="20260413010101",
        )
        artifact_store.update_run_manifest(
            "p9",
            status="completed_email_sent",
            metrics={"email_sent": True},
            timestamp="20260413010101",
        )

        manifest = artifact_store.load_run_manifest()
        metrics = manifest["stages"]["p9"]["metrics"]
        assert "error" not in metrics
        assert metrics["raw_output_chars"] == 100
        assert metrics["email_sent"] is True

    def test_manifest_update_without_env_preserves_existing_run_timestamp(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.delenv(artifact_store.RUN_TIMESTAMP_ENV, raising=False)

        artifact_store.update_run_manifest("p9", status="completed_email_sent", timestamp="20260413010101")
        artifact_store.update_run_manifest("agent_preflight", status="skipped", metrics={"ok": False})

        manifest = artifact_store.load_run_manifest()
        assert manifest["run_timestamp"] == "20260413010101"
        assert manifest["stages"]["agent_preflight"]["run_timestamp"] == "20260413010101"


class TestReportStage:
    def test_p5_empty_audit_uses_raw_p1_fallback(self, monkeypatch):
        import orchestrator

        manifest_updates = []

        monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda *args, **kwargs: "")
        monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *args, **kwargs: "fallback.md")
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
        )

        context = {
            "resume_from": "none",
            "ckpt": {"files": {}},
            "evidence_raw": "raw P1 evidence",
            "backtrack": "24h",
            "now": None,
        }

        orchestrator._stage_audit_evidence(context)

        assert "未经 P5 Pro 清洗" in context["p5_evidence"]
        assert "raw P1 evidence" in context["p5_evidence"]
        assert manifest_updates[-1][0] == ("p5",)
        assert manifest_updates[-1][1]["status"] == "failed_empty_output_raw_p1_fallback"

    def test_p5_audit_exception_uses_raw_p1_fallback(self, monkeypatch):
        import orchestrator

        manifest_updates = []

        def fail_audit(*args, **kwargs):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(orchestrator, "gemini_pro_audit", fail_audit)
        monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *args, **kwargs: "fallback.md")
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
        )

        context = {
            "resume_from": "none",
            "ckpt": {"files": {}},
            "evidence_raw": "raw P1 evidence",
            "backtrack": "24h",
            "now": None,
        }

        orchestrator._stage_audit_evidence(context)

        assert "raw P1 evidence" in context["p5_evidence"]
        assert any(update[1]["status"] == "failed_exception" for update in manifest_updates)
        assert manifest_updates[-1][1]["status"] == "failed_empty_output_raw_p1_fallback"

    def test_market_mapping_continues_when_p4_fails(self, monkeypatch):
        import orchestrator

        captured = {}
        manifest_updates = []

        def fail_p4():
            raise RuntimeError("market api down")

        def fake_generate_report(evidence):
            captured["evidence"] = evidence
            return "P6 fallback mapping"

        monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", fail_p4)
        monkeypatch.setattr(orchestrator, "generate_report", fake_generate_report)
        monkeypatch.setattr(orchestrator, "write_text_artifact", lambda prefix, content: "p6.md")
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
        )

        context = {
            "resume_from": "none",
            "ckpt": {"files": {}},
            "p5_evidence": "P5 evidence",
            "p2_data": "P2 data",
            "p3_data": "P3 data",
            "p4_data": "",
            "p6_mapping": "",
        }

        orchestrator._stage_market_mapping(context)

        assert context["p4_data"] == ""
        assert context["p6_mapping"] == "P6 fallback mapping"
        assert "P5 evidence" in captured["evidence"]
        assert any(update[0] == ("p4",) and update[1]["status"] == "failed_exception_fallback_empty" for update in manifest_updates)

    def test_research_plan_failure_skips_optional_research(self, monkeypatch):
        import orchestrator

        manifest_updates = []

        monkeypatch.setattr(orchestrator, "build_research_plan", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad plan")))
        monkeypatch.setattr(orchestrator, "write_json_artifact", lambda prefix, data: "plan.json")
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
        )

        context = {
            "resume_from": "none",
            "ckpt": {"files": {}},
            "p6_mapping": "P6 data",
            "p4_data": "",
            "p5_evidence": "",
            "p7_deep": "",
            "p7a_audited": "",
            "p75_supplement": "",
            "search_queries": [],
        }

        orchestrator._stage_research(context)

        assert context["research_plan"]["should_run_p7"] is False
        assert context["research_plan"]["should_run_p75"] is False
        assert context["p7_deep"] == ""
        assert any(update[0] == ("research",) and update[1]["status"] == "failed_plan_fallback" for update in manifest_updates)

    def test_technical_failure_sets_price_fallback(self, monkeypatch):
        import orchestrator

        manifest_updates = []

        monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda report: [("Stock", "600000")])
        monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda report: {})
        monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda report: {})
        monkeypatch.setattr(orchestrator, "p8_search_stock_prices", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vipdoc broken")))
        monkeypatch.setattr(orchestrator, "write_text_artifact", lambda prefix, content: "p8-fallback.md")
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
        )

        context = {
            "resume_from": "none",
            "p6_mapping": "P6 data",
            "price_data": "",
        }

        orchestrator._stage_technical(context)

        assert "P8 技术面环节发生异常" in context["price_data"]
        assert manifest_updates[-1][0] == ("p8",)
        assert manifest_updates[-1][1]["status"] == "failed_exception_fallback"

    def test_p9_prompt_has_p6_fallback_when_mapping_missing(self, monkeypatch):
        import p9_reporter

        captured = {}

        monkeypatch.setattr(p9_reporter, "require_prompt", lambda name: "{p6_mapping}")
        def fake_invoke(prompt):
            captured["prompt"] = prompt
            return SimpleNamespace(content="{}", thinking_content="")

        monkeypatch.setattr(p9_reporter.opus_adapter, "invoke", fake_invoke)
        monkeypatch.setattr(
            p9_reporter,
            "_parse_report_payload",
            lambda raw: {"core_signals": [], "today_advice": [], "risk_warning": []},
        )
        monkeypatch.setattr(p9_reporter, "_apply_evidence_guardrails", lambda data: (data, {}))
        monkeypatch.setattr(p9_reporter, "build_report_markdown", lambda data, today_str: "# report")
        monkeypatch.setattr(p9_reporter, "write_json_artifact", lambda prefix, data: "report.json")
        monkeypatch.setattr(p9_reporter, "update_run_manifest", lambda *args, **kwargs: None)

        report, _ = p9_reporter.p9_risk_analysis(
            p5_evidence="P5",
            p7_deep="",
            p75_supplement="",
            p6_mapping="",
            price_data="P8 data is long enough to avoid fallback text",
            p2_x_intel_data="",
            p3_intel_data="",
            p4_yfinance="",
        )

        assert report == "# report"
        assert "P6 市场映射数据" in captured["prompt"]

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
