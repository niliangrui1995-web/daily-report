# -*- coding: utf-8 -*-
"""Hardening tests for runtime control, TLS policy, and preflight checks."""

from __future__ import annotations

import importlib
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def slow_stage_handler(context: dict) -> None:
    time.sleep(float(context.get("sleep_seconds", 0)))
    context["completed"] = True


def stage_handler_with_lingering_thread(context: dict) -> None:
    threading.Thread(target=time.sleep, args=(30,), daemon=False).start()
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

    def test_committed_result_wins_when_child_has_lingering_threads(self, monkeypatch, tmp_path):
        import pipeline_runtime

        state = {"alive": True, "terminated": False}

        class FakeQueue:
            def __init__(self):
                self.signal = None

            def get(self, timeout=None):
                signal = self.signal
                self.signal = None
                if signal is None:
                    raise RuntimeError("empty queue")
                return signal

            def get_nowait(self):
                return self.get()

            def close(self):
                return None

            def join_thread(self):
                return None

        queue = FakeQueue()

        class FakeProcess:
            pid = 12345
            exitcode = None

            def __init__(self, target, args, name):
                self.args = args

            def start(self):
                result_path = self.args[3]
                digest = pipeline_runtime._write_context_to_file({"value": "committed"}, result_path)
                queue.signal = {"status": "success", "context_sha256": digest}

            def is_alive(self):
                return state["alive"]

            def join(self, timeout=None):
                return None

            def terminate(self):
                state["terminated"] = True
                state["alive"] = False

            def kill(self):
                state["alive"] = False

        class FakeMpContext:
            def Queue(self, maxsize=0):
                return queue

            def Process(self, target, args, name):
                return FakeProcess(target, args, name)

        monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda _method: FakeMpContext())
        monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))

        result = pipeline_runtime._run_stage_in_subprocess(
            pipeline_runtime.StageSpec(name="rss", handler=lambda context: None, timeout_seconds=5),
            {"value": "parent"},
        )

        assert result == {"status": "success", "context": {"value": "committed"}}
        assert state["terminated"] is True

    def test_spawned_stage_returns_committed_result_without_waiting_for_lingering_thread(self):
        import pipeline_runtime

        started = time.monotonic()
        result = pipeline_runtime._run_stage_in_subprocess(
            pipeline_runtime.StageSpec(
                name="lingering-thread",
                handler=stage_handler_with_lingering_thread,
                timeout_seconds=10,
            ),
            {},
        )
        elapsed = time.monotonic() - started

        assert result == {"status": "success", "context": {"completed": True}}
        assert elapsed < 15
        assert all(child.name != "stage-lingering-thread" for child in multiprocessing.active_children())

    def test_context_file_roundtrips_utf8_sig_chinese(self, tmp_path):
        import pipeline_runtime

        context_path = tmp_path / "stage_context.json"

        pipeline_runtime._write_context_to_file(
            {
                "report": "战报",
                "mapping": {"track": "先进封装与混合键合"},
                "dirty": "OpenAI\u00e2\u0080\u0099s release\u00e2\u0080\u0094read now",
                "non_serializable": object(),
            },
            str(context_path),
        )

        assert context_path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert pipeline_runtime._read_context_from_file(str(context_path)) == {
            "report": "战报",
            "mapping": {"track": "先进封装与混合键合"},
            "dirty": "OpenAI’s release—read now",
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

    def test_web_result_normalizes_common_utf8_mojibake(self):
        from p1_web import _entries_to_p1_result

        result = _entries_to_p1_result(
            [
                {
                    "title": "Investor\u00e2\u20ac\u2122s update",
                    "content": "risk\u00e2\u0080\u0094reward",
                    "url": "https://example.com/update",
                    "published_at": "",
                    "source": "Example",
                }
            ],
            "网页采集",
            "Example",
            24,
        )

        assert result is not None
        assert "Investor’s update — risk—reward" in result["text"]
        assert "\u00e2\u0080" not in result["text"]
        assert "\u00e2\u20ac" not in result["text"]
        assert result["grounding_sources"][0]["title"] == "Investor’s update"

    def test_web_phase2_published_hint_refreshes_time_stats(self, monkeypatch):
        import p1_web

        bj_tz = timezone(timedelta(hours=8))
        published_hint = (datetime.now(bj_tz) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+08:00")

        monkeypatch.setattr(p1_web, "_parser_available", True)
        monkeypatch.setattr(
            p1_web,
            "V2_WEB_SOURCES",
            [
                {
                    "name": "ExampleWeb",
                    "url": "https://example.com/source",
                    "selector": "article a",
                    "category": "网页采集",
                }
            ],
        )
        monkeypatch.setattr(p1_web.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(p1_web.random, "uniform", lambda start, end: 0)
        monkeypatch.setattr(p1_web.random, "shuffle", lambda items: None)
        monkeypatch.setattr(
            p1_web,
            "_scrape_one_source",
            lambda source_cfg: [
                {
                    "title": "Fresh article",
                    "content": "",
                    "url": "https://example.com/news/fresh-article",
                    "published_at": "",
                    "source": "ExampleWeb",
                }
            ],
        )
        monkeypatch.setattr(
            p1_web,
            "enrich_article_url",
            lambda url, source_name, return_meta=False: ("fresh article body", {"published_at": published_hint}),
        )

        results, stats = p1_web.collect_from_web("24h")

        assert stats["time_recent"] == 1
        assert stats["time_unknown"] == 0
        assert results[0]["grounding_sources"][0]["time_status"] == "recent"
        assert "unknown_time" not in results[0]["text"]


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

    def test_artifact_writes_normalize_common_mojibake(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, "20260626083007")

        report_path = Path(
            artifact_store.write_text_artifact("战报", "# 战报\nOpenAI\u00e2\u0080\u0099s release\u00e2\u0080\u0094read now")
        )
        json_path = Path(
            artifact_store.write_json_artifact(
                "P1_RSS原始数据",
                [{"text": "investor\u00e2\u20ac\u2122s update", "nested": {"body": "risk\u00e2\u0080\u0094reward"}}],
            )
        )

        assert "OpenAI’s release—read now" in report_path.read_text(encoding=artifact_store.UTF8_FILE_ENCODING)
        payload = json.loads(json_path.read_text(encoding=artifact_store.UTF8_FILE_ENCODING))
        assert payload[0]["text"] == "investor’s update"
        assert payload[0]["nested"]["body"] == "risk—reward"

    def test_atomic_text_artifact_write_keeps_existing_file_on_failure(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, "20260714090000")
        target = tmp_path / "战报_20260714090000.md"
        target.write_text("旧版完整战报", encoding=artifact_store.UTF8_FILE_ENCODING)
        monkeypatch.setattr(
            artifact_store.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("simulated flush failure")),
        )

        with pytest.raises(OSError, match="simulated flush failure"):
            artifact_store.write_text_artifact("战报", "只写了一半的新战报")

        assert target.read_text(encoding=artifact_store.UTF8_FILE_ENCODING) == "旧版完整战报"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_atomic_json_artifact_write_does_not_publish_partial_file(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setenv(artifact_store.RUN_TIMESTAMP_ENV, "20260714090001")
        target = tmp_path / "P1_RSS原始数据_20260714090001.json"
        monkeypatch.setattr(
            artifact_store.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("simulated flush failure")),
        )

        with pytest.raises(OSError, match="simulated flush failure"):
            artifact_store.write_json_artifact("P1_RSS原始数据", [{"title": "partial"}])

        assert target.exists() is False
        assert list(tmp_path.glob("*.tmp")) == []

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

    def test_preflight_context_replaces_stale_failed_exit_code(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        artifact_store.record_run_state(
            status="not_entered_main_flow",
            root_cause="preflight_failed",
            reason="preflight exit_code=1",
            source="pipeline_cli:run",
            main_flow_entered=False,
            runtime_context={"preflight_exit_code": 1},
            timestamp="20260623083006",
        )
        artifact_store.update_manifest_context(
            runtime_context={"python_version": "3.14.5"},
            preflight={"failed": 0, "warnings": 0, "exit_code": 0},
            timestamp="20260623083006",
        )

        manifest = artifact_store.load_run_manifest()
        assert manifest["preflight"]["exit_code"] == 0
        assert manifest["runtime_context"]["preflight_exit_code"] == 0

    def test_record_run_state_marks_skip_root_cause_and_context(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        artifact_store.record_run_state(
            status="skipped_non_trading_day",
            root_cause="non_trading_day",
            reason="today is not a trading day",
            source="scheduled_launcher",
            schedule={
                "is_trading_day": False,
                "should_run": False,
                "reason": "today is not a trading day",
                "week_trading_days": [date(2026, 6, 22)],
                "window_anchor_day": None,
            },
            main_flow_entered=False,
            runtime_context={"entrypoint": "scheduled_launcher"},
            timestamp="20260620083006",
        )

        manifest = artifact_store.load_run_manifest()
        assert manifest["status"] == "skipped_non_trading_day"
        assert manifest["root_cause"] == "non_trading_day"
        assert manifest["status_reason"] == "today is not a trading day"
        assert manifest["main_flow_entered"] is False
        assert manifest["schedule_decision"]["week_trading_days"] == ["2026-06-22"]
        assert manifest["runtime_context"]["run_state"] == "skipped_non_trading_day"
        assert manifest["runtime_context"]["root_cause"] == "non_trading_day"
        assert manifest["runtime_context"]["main_flow_entered"] is False

    def test_record_run_state_keeps_previous_failure_as_history(self, monkeypatch, tmp_path):
        import artifact_store

        monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(tmp_path))

        artifact_store.record_run_state(
            status="failed_timeout",
            root_cause="failed_timeout",
            reason="stage 'market_mapping' exceeded timeout_seconds=600",
            source="orchestrator",
            main_flow_entered=True,
            timestamp="20260623083006",
        )
        artifact_store.record_run_state(
            status="running",
            root_cause="orchestrator_started",
            reason="orchestrator entered main flow",
            source="orchestrator",
            main_flow_entered=True,
            runtime_context={"resume_from": "p5"},
            timestamp="20260623083006",
        )

        manifest = artifact_store.load_run_manifest()
        assert manifest["runtime_context"]["previous_run_state"] == "failed_timeout"
        assert manifest["runtime_context"]["previous_root_cause"] == "failed_timeout"
        assert manifest["runtime_context"]["resume_from"] == "p5"

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
        artifact_store.update_run_manifest("postcheck", status="skipped", metrics={"ok": False})

        manifest = artifact_store.load_run_manifest()
        assert manifest["run_timestamp"] == "20260413010101"
        assert manifest["stages"]["postcheck"]["run_timestamp"] == "20260413010101"


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
        assert any(update[0] == ("p7_plan",) and update[1]["status"] == "failed_plan_fallback" for update in manifest_updates)

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
            "_parse_report_payload_with_schema_validation",
            lambda raw: (
                {"core_signals": [], "today_advice": [], "risk_warning": []},
                {"status": "valid", "revalidated": True},
            ),
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
        with pytest.raises(RuntimeError, match="empty report"):
            orchestrator._stage_report({"p6_mapping": "mapping"})

    def test_stage_report_raises_when_delivery_fails(self, monkeypatch):
        import orchestrator

        monkeypatch.setattr(orchestrator, "p9_risk_analysis", lambda **kwargs: ("# report", "20260413010101"))
        monkeypatch.setattr(
            orchestrator,
            "send_report_email",
            lambda content, now_str="": {"ok": False, "status": "completed_email_failed"},
        )
        with pytest.raises(RuntimeError, match="completed_email_failed"):
            orchestrator._stage_report({"p6_mapping": "mapping"})


class TestPreflightChecks:
    def test_run_logging_writes_chinese_logs_as_utf8_sig(self, monkeypatch, tmp_path):
        import pipeline_cli

        pipeline_log = tmp_path / "pipeline.log"
        error_log = tmp_path / "error.log"
        root_logger = logging.getLogger()
        previous_handlers = root_logger.handlers[:]
        previous_level = root_logger.level

        monkeypatch.setattr(pipeline_cli, "get_pipeline_log_path", lambda: str(pipeline_log))
        monkeypatch.setattr(pipeline_cli, "get_error_log_path", lambda: str(error_log))

        pipeline_cli.configure_run_logging()
        try:
            logging.info("中文日志落盘验证：生益科技")
            logging.error("错误日志落盘验证：中际旭创")
            for handler in root_logger.handlers:
                handler.flush()
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
                handler.close()
            for handler in previous_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(previous_level)

        assert pipeline_log.read_bytes().startswith(b"\xef\xbb\xbf")
        assert error_log.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "中文日志落盘验证：生益科技" in pipeline_log.read_text(encoding="utf-8-sig")
        assert "错误日志落盘验证：中际旭创" in error_log.read_text(encoding="utf-8-sig")

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
            yield {"owner_token": "test-owner"}

        monkeypatch.setattr(pipeline_cli, "_resolve_preflight_resume_stage", lambda _flags: "none")
        monkeypatch.setattr(
            pipeline_cli,
            "_collect_preflight",
            lambda strict, **_kwargs: failing_result,
        )
        monkeypatch.setattr(pipeline_cli, "_print_preflight", lambda result: result["summary"]["exit_code"])
        monkeypatch.setattr(pipeline_cli, "set_preflight_summary", lambda summary: calls.append(("summary", summary)))
        monkeypatch.setattr(pipeline_cli, "update_manifest_context", lambda **kwargs: calls.append(("context", kwargs)))
        monkeypatch.setattr(pipeline_cli, "record_run_state", lambda **kwargs: calls.append(("run_state", kwargs)))
        monkeypatch.setattr(pipeline_cli, "configure_run_logging", lambda: calls.append("logging"))
        monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
        monkeypatch.setitem(
            sys.modules,
            "orchestrator",
            SimpleNamespace(orchestrator_main=lambda flags: calls.append(("orchestrator", flags))),
        )

        result = pipeline_cli._run_orchestrator([], command_name="run")

        assert result == pipeline_cli.EXIT_CHECK_FAILED
        assert "lock" in calls
        assert not any(call[0] == "orchestrator" for call in calls if isinstance(call, tuple))
        assert not any(call[0] == "run_state" for call in calls if isinstance(call, tuple))
        assert not any(call[0] == "context" for call in calls if isinstance(call, tuple))

    def test_run_command_invokes_orchestrator_after_successful_preflight(self, monkeypatch):
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
            yield {"command": kwargs.get("command"), "owner_token": "test-owner"}

        monkeypatch.setattr(pipeline_cli, "_resolve_preflight_resume_stage", lambda _flags: "none")
        monkeypatch.setattr(
            pipeline_cli,
            "_collect_preflight",
            lambda strict, **_kwargs: calls.append(("preflight", strict)) or successful_result,
        )
        monkeypatch.setattr(pipeline_cli, "set_preflight_summary", lambda summary: calls.append(("summary", summary)))
        monkeypatch.setattr(pipeline_cli, "update_manifest_context", lambda **kwargs: calls.append(("context", kwargs)))
        monkeypatch.setattr(pipeline_cli, "configure_run_logging", lambda: calls.append(("logging",)))
        monkeypatch.setattr(pipeline_cli, "acquire_run_lock", fake_lock)
        monkeypatch.setitem(
            sys.modules,
            "orchestrator",
            SimpleNamespace(orchestrator_main=lambda flags: calls.append(("orchestrator", list(flags)))),
        )

        result = pipeline_cli.main(["run"])

        call_names = [call[0] for call in calls]
        assert result == pipeline_cli.EXIT_OK
        assert ("preflight", True) in calls
        assert calls[call_names.index("lock")][1]["command"] == "run"
        assert call_names.index("lock") < call_names.index("context")
        assert call_names.index("lock") < call_names.index("orchestrator")
        assert ("orchestrator", []) in calls

    def test_status_prints_manifest_root_cause(self, monkeypatch, tmp_path, capsys):
        import pipeline_cli

        manifest = {
            "run_timestamp": "20260620083006",
            "status": "skipped_non_trading_day",
            "root_cause": "non_trading_day",
            "status_reason": "today is not a trading day",
            "main_flow_entered": False,
            "runtime_context": {
                "run_state": "skipped_non_trading_day",
                "root_cause": "non_trading_day",
                "main_flow_entered": False,
            },
            "schedule_decision": {
                "is_trading_day": False,
                "should_run": False,
                "reason": "today is not a trading day",
            },
            "stages": {},
        }

        monkeypatch.setattr(pipeline_cli, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "get_manifest_path", lambda day_dir=None: str(tmp_path / "run_manifest.json"))
        monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "load_run_manifest", lambda day_dir=None: manifest)
        monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: False)

        result = pipeline_cli.cmd_status(SimpleNamespace())

        output = capsys.readouterr().out
        assert result == 0
        assert "run_state: skipped_non_trading_day root_cause=non_trading_day main_flow_entered=no" in output
        assert "run_reason: today is not a trading day" in output
        assert "schedule_decision: is_trading_day=no should_run=no reason=today is not a trading day" in output

    def test_status_prints_degraded_stage_summary(self, monkeypatch, tmp_path, capsys):
        import pipeline_cli

        manifest = {
            "run_timestamp": "20260623083006",
            "status": "completed_with_stage_degradation",
            "root_cause": "stage_degraded",
            "status_reason": "completed with degraded stages: p7a",
            "main_flow_entered": True,
            "runtime_context": {
                "run_state": "completed_with_stage_degradation",
                "root_cause": "stage_degraded",
                "main_flow_entered": True,
            },
            "stages": {
                "p7a": {
                    "status": "completed_structured_degraded",
                    "updated_at": "2026-06-23T09:54:28",
                    "metrics": {
                        "structured_degraded": True,
                        "degrade_reason": "json_parse_failed: invalid json",
                    },
                },
                "p9": {"status": "completed_email_sent", "updated_at": "2026-06-23T09:58:03"},
            },
        }

        monkeypatch.setattr(pipeline_cli, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "get_manifest_path", lambda day_dir=None: str(tmp_path / "run_manifest.json"))
        monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "load_run_manifest", lambda day_dir=None: manifest)
        monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: True)

        result = pipeline_cli.cmd_status(SimpleNamespace())

        output = capsys.readouterr().out
        assert result == 0
        assert "run_state: completed_with_stage_degradation root_cause=stage_degraded main_flow_entered=yes" in output
        assert "run_reason: completed with degraded stages: p7a" in output
        assert "degraded_stages: p7a=completed_structured_degraded reason=json_parse_failed: invalid json" in output

    def test_status_prints_completed_resume_source(self, monkeypatch, tmp_path, capsys):
        import pipeline_cli

        manifest = {
            "run_timestamp": "20260623083006",
            "status": "completed",
            "root_cause": "completed",
            "status_reason": "orchestrator completed after resume_from=p5 start_stage=audit_evidence",
            "main_flow_entered": True,
            "runtime_context": {
                "run_state": "completed",
                "root_cause": "completed",
                "main_flow_entered": True,
                "completed_after_resume": True,
                "resume_from": "p5",
                "resume_start_stage": "audit_evidence",
                "preflight_exit_code": 0,
            },
            "preflight": {"exit_code": 0},
            "stages": {
                "orchestrator": {
                    "status": "completed",
                    "metrics": {
                        "resume_from": "p5",
                        "resume_start_stage": "audit_evidence",
                        "completed_after_resume": True,
                    },
                }
            },
        }

        monkeypatch.setattr(pipeline_cli, "get_today_output_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "get_manifest_path", lambda day_dir=None: str(tmp_path / "run_manifest.json"))
        monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(tmp_path))
        monkeypatch.setattr(pipeline_cli, "load_run_manifest", lambda day_dir=None: manifest)
        monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: True)

        result = pipeline_cli.cmd_status(SimpleNamespace())

        output = capsys.readouterr().out
        assert result == 0
        assert (
            "run_state: completed root_cause=completed main_flow_entered=yes "
            "completed_after_resume=yes resume_from=p5 resume_start_stage=audit_evidence"
        ) in output
        assert "run_reason: orchestrator completed after resume_from=p5 start_stage=audit_evidence" in output

    def test_orchestrator_completion_records_resume_metadata(self, monkeypatch):
        import orchestrator

        manifest_updates = []
        run_states = []
        contexts = []

        monkeypatch.setattr(
            orchestrator,
            "get_schedule_decision",
            lambda day: {"is_trading_day": True, "should_run": True, "reason": "ok"},
        )
        monkeypatch.setattr(
            orchestrator,
            "detect_checkpoint",
            lambda: {"stage": "p5", "files": {}, "run_timestamp": "20260623083006"},
        )
        monkeypatch.setattr(orchestrator, "set_run_timestamp", lambda timestamp=None: timestamp or "20260623083006")
        monkeypatch.setattr(orchestrator, "get_preflight_summary", lambda: {"exit_code": 0})
        monkeypatch.setattr(orchestrator, "build_runtime_context", lambda: {"cwd": "test"})
        monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: {"stages": {}})
        monkeypatch.setattr(
            orchestrator,
            "update_run_manifest",
            lambda stage, **kwargs: manifest_updates.append((stage, kwargs)),
        )
        monkeypatch.setattr(orchestrator, "update_manifest_context", lambda **kwargs: contexts.append(kwargs))
        monkeypatch.setattr(orchestrator, "record_run_state", lambda **kwargs: run_states.append(kwargs))
        monkeypatch.setattr(
            orchestrator,
            "run_stage_registry",
            lambda stages, context, start_at=None: context.update({"final_report": "# done"}),
        )

        orchestrator.orchestrator_main([])

        final_update = [item for item in manifest_updates if item[0] == "orchestrator"][-1][1]
        final_state = run_states[-1]
        assert contexts[-1]["preflight"]["exit_code"] == 0
        assert contexts[-1]["runtime_context"]["resume_from"] == "p5"
        assert final_update["metrics"]["resume_from"] == "p5"
        assert final_update["metrics"]["resume_start_stage"] == "audit_evidence"
        assert final_update["metrics"]["completed_after_resume"] is True
        assert final_state["status"] == "completed"
        assert final_state["root_cause"] == "completed"
        assert final_state["reason"] == "orchestrator completed after resume_from=p5 start_stage=audit_evidence"
        assert final_state["runtime_context"]["completed_after_resume"] is True
        assert final_state["runtime_context"]["resume_from"] == "p5"
        assert final_state["runtime_context"]["resume_start_stage"] == "audit_evidence"

    def test_status_day_closes_running_manifest_when_lock_is_stale(self, monkeypatch, tmp_path, capsys):
        import pipeline_cli

        output_root = tmp_path / "output"
        day_dir = output_root / "20260623"
        day_dir.mkdir(parents=True)
        manifest_path = day_dir / "run_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_timestamp": "20260623083006",
                    "status": "running",
                    "root_cause": "orchestrator_started",
                    "status_reason": "orchestrator entered main flow",
                    "main_flow_entered": True,
                    "runtime_context": {
                        "run_state": "running",
                        "root_cause": "orchestrator_started",
                        "main_flow_entered": True,
                    },
                    "stages": {
                        "orchestrator": {
                            "status": "started",
                            "updated_at": "2026-06-23T08:30:06",
                            "run_timestamp": "20260623083006",
                        },
                        "market_mapping": {
                            "status": "running",
                            "updated_at": "2026-06-23T08:39:09",
                            "run_timestamp": "20260623083006",
                            "metrics": {"timeout_seconds": 600},
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lock_info = {
            "pid": 22016,
            "command": "run",
            "started_at": "2026-06-23T00:30:06+00:00",
            "run_day": "20260623",
        }

        monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(output_root))
        monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: lock_info)
        monkeypatch.setattr(
            pipeline_cli,
            "get_lock_stale_reason",
            lambda info, stale_after_seconds: "pid_not_running",
        )
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: True)

        result = pipeline_cli.cmd_status(SimpleNamespace(day="20260623"))

        output = capsys.readouterr().out
        rewritten = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        assert result == 0
        assert rewritten["status"] == "failed_stale_run"
        assert rewritten["root_cause"] == "stale_run_lock"
        assert rewritten["main_flow_entered"] is True
        assert rewritten["runtime_context"]["previous_run_state"] == "running"
        assert rewritten["runtime_context"]["lock_stale_reason"] == "pid_not_running"
        assert "run_state: failed_stale_run root_cause=stale_run_lock main_flow_entered=yes" in output
        assert "lock: stale" in output
        assert "stale_reason=pid_not_running" in output

    def test_status_day_does_not_apply_unrelated_global_lock_to_history(self, monkeypatch, tmp_path, capsys):
        import pipeline_cli

        output_root = tmp_path / "output"
        day_dir = output_root / "20260623"
        day_dir.mkdir(parents=True)
        manifest_path = day_dir / "run_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_timestamp": "20260623083006",
                    "status": "running",
                    "root_cause": "orchestrator_started",
                    "main_flow_entered": True,
                    "runtime_context": {},
                    "stages": {},
                    "preflight": {},
                }
            ),
            encoding="utf-8",
        )
        unrelated_lock = {
            "pid": 9999,
            "command": "run",
            "started_at": "2026-07-10T00:00:00+00:00",
            "heartbeat_at": "2026-07-10T00:00:30+00:00",
            "run_day": "20260710",
        }

        monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(output_root))
        monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: unrelated_lock)
        monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _: True)

        assert pipeline_cli.cmd_status(SimpleNamespace(day="20260623")) == 0

        output = capsys.readouterr().out
        unchanged = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        assert unchanged["status"] == "running"
        assert "lock: unrelated" in output
        assert "target_day=20260623" in output

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
        monkeypatch.setattr(pipeline_cli, "PRO_OPUS_BASE_URL", "http://127.0.0.1:8317/v1")
        monkeypatch.setattr(pipeline_cli, "PRO_OPUS_API_KEY", "secret")
        monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "sender@example.com")
        monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "smtp-secret")
        monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "receiver@example.com")
        monkeypatch.setattr(pipeline_cli, "_probe_local_model_endpoint", lambda _url, _key: (True, "authenticated status=200"))
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
        monkeypatch.setattr(pipeline_cli, "get_lock_stale_reason", lambda info, stale_after_seconds: "")

        result = pipeline_cli._collect_preflight(strict=True)
        assert result["summary"]["exit_code"] == pipeline_cli.EXIT_LOCK_ACTIVE
        assert any(item["name"] == "run_lock" and not item["ok"] for item in result["checks"])

    def test_preflight_strict_allows_stale_lock(self, monkeypatch, tmp_path):
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
        monkeypatch.setattr(pipeline_cli, "require_prompt", lambda name: "ok")
        monkeypatch.setattr(pipeline_cli, "SEARCH_API_KEY_POOL", ["key-1"])
        monkeypatch.setattr(pipeline_cli, "ANTIGRAVITY_API_KEY", "secret")
        monkeypatch.setattr(pipeline_cli, "PRO_OPUS_BASE_URL", "http://127.0.0.1:8317/v1")
        monkeypatch.setattr(pipeline_cli, "PRO_OPUS_API_KEY", "secret")
        monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "sender@example.com")
        monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "smtp-secret")
        monkeypatch.setattr(pipeline_cli, "RECEIVER_EMAIL", "receiver@example.com")
        monkeypatch.setattr(pipeline_cli, "_probe_local_model_endpoint", lambda _url, _key: (True, "authenticated status=200"))
        monkeypatch.setattr(pipeline_cli, "GROK_API_KEY", "optional")
        monkeypatch.setattr(pipeline_cli, "INSECURE_TLS_ALLOWLIST", ())
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
            lambda: {"pid": 22016, "command": "run", "started_at": "2026-06-23T00:30:06+00:00"},
        )
        monkeypatch.setattr(
            pipeline_cli,
            "get_lock_stale_reason",
            lambda info, stale_after_seconds: "pid_not_running",
        )

        result = pipeline_cli._collect_preflight(strict=True)

        run_lock = [item for item in result["checks"] if item["name"] == "run_lock"][-1]
        assert result["summary"]["exit_code"] == pipeline_cli.EXIT_OK
        assert run_lock["ok"] is True
        assert "stale_reason=pid_not_running" in run_lock["detail"]


class TestPipelineLock:
    def test_acquire_run_lock_clears_dead_lock(self, monkeypatch, tmp_path):
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
        monkeypatch.setattr(pipeline_lock, "_pid_is_running", lambda pid: False)

        with pipeline_lock.acquire_run_lock(command="resume") as payload:
            assert payload["command"] == "resume"
            current = pipeline_lock.read_run_lock()
            assert current is not None
            assert current["command"] == "resume"

        assert not lock_path.exists()


class TestModelEndpointPolicy:
    def test_probe_accepts_loopback_http_after_authenticated_model_list(self, monkeypatch):
        import pipeline_cli
        import requests

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"id": "gpt-test"}]}

        class FakeSession:
            trust_env = True

            @staticmethod
            def get(*_args, **_kwargs):
                return FakeResponse()

            @staticmethod
            def close():
                return None

        monkeypatch.setattr(requests, "Session", FakeSession)

        assert pipeline_cli._probe_local_model_endpoint(
            "http://127.0.0.1:8317/v1",
            "configured-key",
        ) == (True, "authenticated status=200 models=1 host=127.0.0.1")

    def test_probe_rejects_external_plain_http_without_network_call(self):
        import pipeline_cli

        ok, detail = pipeline_cli._probe_local_model_endpoint(
            "http://api.example.invalid/v1",
            "configured-key",
        )

        assert ok is False
        assert "HTTPS" in detail

    def test_probe_accepts_external_https_without_live_probe(self):
        import pipeline_cli

        ok, detail = pipeline_cli._probe_local_model_endpoint(
            "https://api.example.invalid/v1",
            "configured-key",
        )

        assert ok is True
        assert "live probe skipped" in detail


class TestTradingCalendarStatus:
    def test_calendar_status_exposes_package_and_import_names(self):
        from trading_calendar import CALENDAR_IMPORT_NAME, CALENDAR_PACKAGE_NAME, get_calendar_status

        status = get_calendar_status()

        assert status["package_name"] == CALENDAR_PACKAGE_NAME
        assert status["import_name"] == CALENDAR_IMPORT_NAME
        if status["library_available"]:
            assert status["package_version"]
