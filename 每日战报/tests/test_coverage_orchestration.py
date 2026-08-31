# -*- coding: utf-8 -*-
"""Behavioral branch coverage for orchestration and its CLI control plane."""

from __future__ import annotations

import json
import logging
import runpy
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import orchestrator
import pipeline_cli


def test_model_probe_rejects_missing_key_without_network(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("network must not be opened"))
    assert pipeline_cli._probe_local_model_endpoint("http://127.0.0.1:8317/v1", "") == (
        False,
        "model endpoint API key is missing",
    )


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (503, {}, "authenticated probe returned HTTP 503"),
        (200, {"data": []}, "authenticated probe returned no models"),
        (200, ["not-a-dict"], "authenticated probe returned no models"),
    ],
)
def test_model_probe_reports_http_and_payload_failures(monkeypatch, status_code, payload, expected):
    import requests

    closed = []

    class Session:
        trust_env = True

        def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=status_code, json=lambda: payload)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(requests, "Session", Session)
    assert pipeline_cli._probe_local_model_endpoint(
        "http://localhost:8317/v1", "configured"
    ) == (False, expected)
    assert closed == [True]


def test_model_probe_converts_transport_exception_to_detail(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests,
        "Session",
        lambda: (_ for _ in ()).throw(OSError("socket unavailable")),
    )
    ok, detail = pipeline_cli._probe_local_model_endpoint(
        "http://127.0.0.1:8317/v1", "configured"
    )
    assert ok is False
    assert detail == "OSError: socket unavailable"


def test_cli_command_wrappers_forward_exact_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline_cli,
        "_run_orchestrator",
        lambda flags, *, command_name: calls.append((flags, command_name)) or 7,
    )

    assert pipeline_cli.cmd_resume(SimpleNamespace()) == 7
    assert pipeline_cli.cmd_force_run(SimpleNamespace()) == 7
    assert pipeline_cli.cmd_restart(SimpleNamespace()) == 7
    assert orchestrator._parse_flags(["--force-restart"]) == (True, True)
    assert calls == [
        ([], "resume"),
        (["--force-run"], "force-run"),
        (["--force-restart"], "restart"),
    ]
    assert pipeline_cli._bool_label(True) == "yes"
    assert pipeline_cli._bool_label(False) == "no"
    assert pipeline_cli._bool_label(None) == "unknown"


def test_manifest_run_state_handles_malformed_and_preflight_only_manifests(monkeypatch):
    monkeypatch.setattr(pipeline_cli, "summarize_stage_degradations", lambda _manifest: [])

    empty = pipeline_cli._manifest_run_state(
        {"runtime_context": "bad", "stages": "bad", "preflight": "bad"}
    )
    preflight_only = pipeline_cli._manifest_run_state(
        {"runtime_context": {}, "stages": {}, "preflight": {"exit_code": 0}}
    )
    missing_orchestrator = pipeline_cli._manifest_run_state(
        {"runtime_context": {}, "stages": {"p1": {"status": "completed"}}}
    )

    assert empty["status"] == "no_run_activity"
    assert empty["root_cause"] == "empty_manifest"
    assert preflight_only["status"] == "preflight_passed_no_run"
    assert preflight_only["main_flow_entered"] is False
    assert missing_orchestrator["status"] == "not_entered_main_flow"


def test_manifest_run_state_uses_metrics_and_infers_resume_completion(monkeypatch):
    monkeypatch.setattr(pipeline_cli, "summarize_stage_degradations", lambda _manifest: [])
    state = pipeline_cli._manifest_run_state(
        {
            "status": "completed",
            "runtime_context": {"schedule_decision": {"should_run": True}},
            "stages": {
                "orchestrator": {
                    "metrics": {
                        "resume_from": "p5",
                        "resume_start_stage": "audit_evidence",
                    }
                }
            },
        }
    )
    malformed_payload = pipeline_cli._manifest_run_state(
        {"status": "running", "stages": {"orchestrator": "bad"}}
    )
    malformed_metrics = pipeline_cli._manifest_run_state(
        {"status": "running", "stages": {"orchestrator": {"metrics": "bad"}}}
    )
    unknown = pipeline_cli._manifest_run_state(
        {"stages": {"orchestrator": {"metrics": {}}}, "preflight": {"exit_code": 1}}
    )

    assert state["completed_after_resume"] is True
    assert state["resume_from"] == "p5"
    assert state["schedule"] == {"should_run": True}
    assert malformed_payload["status"] == "running"
    assert malformed_metrics["status"] == "running"
    assert unknown["status"] == "unknown"


def test_status_rejects_invalid_day_and_omits_empty_resume_fields(monkeypatch, tmp_path, capsys):
    assert pipeline_cli.cmd_status(SimpleNamespace(day="2026-06-23")) == pipeline_cli.EXIT_CHECK_FAILED
    assert "--day must use YYYYMMDD" in capsys.readouterr().out

    monkeypatch.setattr(pipeline_cli, "get_manifest_path", lambda day_dir=None: str(tmp_path / "manifest.json"))
    monkeypatch.setattr(pipeline_cli, "get_output_root_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pipeline_cli, "load_run_manifest", lambda day_dir=None: {"stages": {}})
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: None)
    monkeypatch.setattr(pipeline_cli, "get_run_lock_state", lambda _info: "missing")
    monkeypatch.setattr(pipeline_cli, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(
        pipeline_cli,
        "_manifest_run_state",
        lambda _manifest: {
            "status": "completed",
            "root_cause": "completed",
            "reason": "",
            "main_flow_entered": True,
            "schedule": {},
            "degraded_stages": [],
            "completed_after_resume": True,
            "resume_from": "",
            "resume_start_stage": "",
        },
    )

    assert pipeline_cli.cmd_status(SimpleNamespace(day="20260623")) == 0
    output = capsys.readouterr().out
    assert "completed_after_resume=yes" in output
    assert "resume_from=" not in output
    assert "stages: none" in output


@pytest.mark.parametrize(
    ("ok", "severity", "prefix"),
    [
        (True, "error", "[OK]"),
        (False, "lock", "[LOCK]"),
        (False, "warning", "[WARN]"),
        (False, "error", "[FAIL]"),
    ],
)
def test_check_line_formats_all_severities(ok, severity, prefix):
    assert pipeline_cli._format_check_line("check", ok, "detail", severity).startswith(prefix)


def test_preflight_collects_prompt_import_calendar_and_stale_invalid_lock_errors(monkeypatch, tmp_path):
    output_root = tmp_path / "output"
    today_dir = tmp_path / "today"
    logs_dir = tmp_path / "logs"
    for path in (output_root, today_dir, logs_dir):
        path.mkdir()

    monkeypatch.setattr(pipeline_cli, "OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(pipeline_cli, "settings_today_output_dir", lambda: str(today_dir))
    monkeypatch.setattr(pipeline_cli, "get_today_logs_dir", lambda: str(logs_dir))
    monkeypatch.setattr(pipeline_cli, "REQUIRED_PROMPTS", ("missing.txt",))
    monkeypatch.setattr(
        pipeline_cli,
        "require_prompt",
        lambda _name: (_ for _ in ()).throw(FileNotFoundError("prompt missing")),
    )
    monkeypatch.setattr(pipeline_cli, "RUNTIME_IMPORTS", (("missing.module", "missing", "warning"),))
    monkeypatch.setattr(
        pipeline_cli.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("package missing")),
    )
    monkeypatch.setattr(pipeline_cli, "SEARCH_API_KEY_POOL", ["search-key"])
    monkeypatch.setattr(pipeline_cli, "ANTIGRAVITY_API_KEY", "model-key")
    monkeypatch.setattr(pipeline_cli, "PRO_OPUS_API_KEY", "model-key")
    monkeypatch.setattr(pipeline_cli, "EMAIL_ADDRESS", "sender@example.test")
    monkeypatch.setattr(pipeline_cli, "EMAIL_PASSWORD", "smtp-password")
    monkeypatch.setattr(pipeline_cli, "GROK_API_KEY", "")
    monkeypatch.setattr(pipeline_cli, "INSECURE_TLS_ALLOWLIST", ())
    monkeypatch.setattr(pipeline_cli, "_probe_local_model_endpoint", lambda *_args: (True, "ok"))
    monkeypatch.setattr(pipeline_cli, "get_calendar_status", lambda: {"mode": "fallback"})
    monkeypatch.setattr(
        pipeline_cli,
        "is_trading_day",
        lambda _day: (_ for _ in ()).throw(RuntimeError("calendar failed")),
    )
    lock = {"error": "malformed"}
    monkeypatch.setattr(pipeline_cli, "read_run_lock", lambda: lock)
    monkeypatch.setattr(pipeline_cli, "get_run_lock_state", lambda _info: "invalid")
    monkeypatch.setattr(pipeline_cli, "get_lock_stale_reason", lambda *_args, **_kwargs: "invalid old lock")

    result = pipeline_cli._collect_preflight(strict=True)
    checks = {item["name"]: item for item in result["checks"]}

    assert checks["prompt:missing.txt"]["ok"] is False
    assert checks["import:missing.module"]["severity"] == "error"
    assert checks["calendar_probe"]["detail"] == "calendar failed"
    assert checks["run_lock"]["ok"] is True
    assert "invalid_stale_reason" in checks["run_lock"]["detail"]
    assert result["summary"]["exit_code"] == pipeline_cli.EXIT_CHECK_FAILED


def test_preflight_print_run_and_maintenance_commands(monkeypatch, tmp_path, capsys):
    result = {
        "strict": True,
        "checks": [{"name": "x", "ok": False, "detail": "bad", "severity": "warning"}],
        "summary": {"total": 1, "failed": 0, "warnings": 1, "exit_code": 1},
    }
    assert pipeline_cli._print_preflight(result) == 1
    output = capsys.readouterr().out
    assert "[WARN] x: bad" in output
    assert "status=FAIL mode=strict" in output

    calls = []
    monkeypatch.setattr(
        pipeline_cli,
        "_collect_preflight",
        lambda strict, **kwargs: calls.append((strict, kwargs)) or result,
    )
    monkeypatch.setattr(pipeline_cli, "_print_preflight", lambda _result: 1)
    monkeypatch.setattr(
        pipeline_cli,
        "update_manifest_context",
        lambda **kwargs: calls.append(("manifest", kwargs)),
    )
    assert pipeline_cli._run_preflight(True, allow_maintenance_lock=True) == 1
    assert calls[0] == (True, {"allow_maintenance_lock": True})
    assert calls[-1][1]["runtime_context"] == {"preflight_exit_code": 1}

    monkeypatch.setattr(pipeline_cli, "_run_preflight", lambda strict, **kwargs: (strict, kwargs))
    assert pipeline_cli.cmd_doctor(SimpleNamespace(strict=True)) == (True, {})
    monkeypatch.setenv("VCP_PREFLIGHT_ALLOW_MAINTENANCE_LOCK", "1")
    assert pipeline_cli.cmd_preflight(SimpleNamespace()) == (
        True,
        {"allow_maintenance_lock": True},
    )

    monkeypatch.setattr(
        pipeline_cli,
        "clean_workspace",
        lambda project_root: {"removed_dirs": 2, "removed_files": 3},
    )
    monkeypatch.setattr(pipeline_cli, "prune_outputs", lambda keep_days: {"removed_dirs": 4})
    assert pipeline_cli.cmd_clean(SimpleNamespace(project_root=str(tmp_path))) == 0
    assert pipeline_cli.cmd_prune(SimpleNamespace(keep_days=9)) == 0
    maintenance_output = capsys.readouterr().out
    assert "removed_dirs=2 removed_files=3" in maintenance_output
    assert "removed_dirs=4 keep_days=9" in maintenance_output


def test_cli_main_dispatches_selected_handler(monkeypatch):
    monkeypatch.setattr(pipeline_cli, "_configure_stdio", lambda: None)
    monkeypatch.setattr(pipeline_cli, "cmd_resume", lambda _args: 23)
    parser = pipeline_cli.build_parser()
    parsed = parser.parse_args(["resume"])
    assert parsed.handler is pipeline_cli.cmd_resume
    assert pipeline_cli.main(["resume"]) == 23


def test_orchestrator_stdio_and_heartbeat_lifecycle(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(orchestrator, "configure_utf8_environment", lambda: calls.append("stdio"))
    orchestrator._configure_stdio()

    class Event:
        def __init__(self):
            self.waits = 0
            self.was_set = False

        def wait(self, _interval):
            self.waits += 1
            return self.waits > 1

        def set(self):
            self.was_set = True

    class Thread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

        def join(self, timeout):
            calls.append(("join", timeout))

    monkeypatch.setattr(orchestrator.threading, "Event", Event)
    monkeypatch.setattr(orchestrator.threading, "Thread", Thread)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 100.0)
    caplog.set_level(logging.INFO)

    with orchestrator.log_heartbeat("test-stage", interval=1):
        calls.append("body")

    assert calls == ["stdio", "body", ("join", 1.0)]
    assert "test-stage still running (0s)" in caplog.text


def test_checkpoint_readers_handle_empty_missing_invalid_and_valid_files(tmp_path, caplog):
    text_path = tmp_path / "text.md"
    text_path.write_text("checkpoint text", encoding="utf-8-sig")
    json_path = tmp_path / "data.json"
    json_path.write_text(json.dumps({"ok": True}), encoding="utf-8-sig")
    bad_json_path = tmp_path / "bad.json"
    bad_json_path.write_text("not json", encoding="utf-8-sig")
    caplog.set_level(logging.WARNING)

    assert orchestrator._read_text_checkpoint("", "empty") == ""
    assert orchestrator._read_text_checkpoint(str(text_path), "text") == "checkpoint text"
    assert orchestrator._read_text_checkpoint(str(tmp_path / "missing.md"), "missing") == ""
    assert orchestrator._read_text_checkpoint(str(tmp_path), "directory") == ""

    assert orchestrator._read_json_checkpoint("", "empty") is None
    assert orchestrator._read_json_checkpoint(str(json_path), "json") == {"ok": True}
    assert orchestrator._read_json_checkpoint(str(tmp_path / "missing.json"), "missing") is None
    assert orchestrator._read_json_checkpoint(str(bad_json_path), "bad") is None
    assert "resume failed" in caplog.text


def test_fresh_checkpoint_rejects_hash_and_timestamp_staleness(monkeypatch):
    ckpt = {"files": {"p6": "p6.md", "p7": "p7.md", "p5": "p5.md"}, "p6_input_sha256": {}}
    monkeypatch.setattr(orchestrator, "p6_input_hash_matches", lambda *_args: False)
    monkeypatch.setattr(orchestrator, "is_older_than_any", lambda *_args: False)
    assert orchestrator._read_fresh_text_checkpoint(
        ckpt, "p7", "P7", upstream_keys=("p6",)
    ) == ""

    monkeypatch.setattr(orchestrator, "p6_input_hash_matches", lambda *_args: True)
    monkeypatch.setattr(orchestrator, "is_older_than_any", lambda *_args: True)
    assert orchestrator._read_fresh_text_checkpoint(
        ckpt, "p7", "P7", upstream_keys=("p6", "p5")
    ) == ""

    monkeypatch.setattr(orchestrator, "is_older_than_any", lambda *_args: False)
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda path, label: f"{label}:{path}")
    assert orchestrator._read_fresh_text_checkpoint(
        ckpt, "p7", "P7", upstream_keys=("p6", "p5")
    ) == "P7:p7.md"


@pytest.mark.parametrize(
    ("metrics", "complete", "reason", "rejected"),
    [
        ("bad", False, "identity_validation_unversioned", 0),
        ({"rejected_targets": "bad", "identity_validation_version": "bad"}, False, "identity_validation_unversioned", 0),
        ({"rejected_targets": 2}, False, "legacy_rejections_missing_names", 2),
        ({"identity_validation_version": 1}, False, "identity_validation_outdated", 0),
        ({"identity_validation_version": 999}, True, "", 0),
    ],
)
def test_identity_validation_normalizes_legacy_and_invalid_metrics(metrics, complete, reason, rejected):
    result = orchestrator._load_p6_identity_validation(metrics)
    assert result["complete"] is complete
    assert result["reason"] == reason
    assert result["rejected_targets"] == rejected


def test_identity_validation_loads_manifest_when_metrics_omitted(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {
            "stages": {
                "p6": {
                    "metrics": {
                        "identity_validation_version": orchestrator.IDENTITY_VALIDATION_VERSION,
                        "rejected_target_names": ["Bad Co"],
                        "rejected_targets": 1,
                    }
                }
            }
        },
    )
    result = orchestrator._load_p6_identity_validation()
    assert result["complete"] is True
    assert result["rejected_target_names"] == ["BadCo"]


@pytest.mark.parametrize("resume_from", ["roi_resume", "delivery_resume"])
def test_build_context_hydrates_roi_inputs_and_p7a(monkeypatch, tmp_path, resume_from):
    files = {}
    for key in ("war_report", "p2", "p3", "p4", "p6"):
        path = tmp_path / f"{key}.md"
        path.write_text(key, encoding="utf-8-sig")
        files[key] = str(path)
    ckpt = {"stage": resume_from, "files": files}
    fresh = {"p7": "deep", "p75": "supplement", "p7a": "audited"}
    monkeypatch.setattr(
        orchestrator,
        "_read_fresh_text_checkpoint",
        lambda _ckpt, key, _label, **_kwargs: fresh[key],
    )

    context = orchestrator._build_context(
        datetime(2026, 6, 23),
        "20260623090000",
        ckpt,
        resume_from,
    )

    assert context["final_report"] == "war_report"
    assert context["p2_data"] == "p2"
    assert context["p6_mapping"] == "p6"
    assert context["p7_deep"] == "deep"
    assert context["p7a_audited"] == "audited"
    assert context["report_ts"] == "20260623090000"


def test_recover_p75_checkpoint_uses_raw_passthrough(monkeypatch, tmp_path):
    raw_path = tmp_path / "p75_raw.md"
    raw_path.write_text("raw supplement evidence", encoding="utf-8")
    ckpt = {
        "files": {"p75_raw": str(raw_path)},
        "stage_assessments": {
            "p75": {"state": "valid_passthrough", "reusable": True}
        },
    }
    monkeypatch.setattr(
        orchestrator,
        "_read_fresh_text_checkpoint",
        lambda *_args, **_kwargs: pytest.fail("passthrough must not load audited output"),
    )

    supplement, raw = orchestrator._recover_p75_checkpoint(ckpt)

    assert supplement == "raw supplement evidence"
    assert raw == "raw supplement evidence"


def test_context_hash_and_empty_persistence_short_circuits(monkeypatch):
    monkeypatch.setattr(orchestrator, "compute_file_sha256", lambda path: f"hash:{path}")
    assert orchestrator._context_p6_input_sha256({"ckpt": {"files": {"p6": "p6.md"}}}) == "hash:p6.md"

    monkeypatch.setattr(
        orchestrator,
        "write_text_artifact",
        lambda *_args: pytest.fail("empty data must not be persisted"),
    )
    orchestrator._persist_p1_artifacts([], "", [], 0, {})
    orchestrator._persist_p3_artifacts("", {})


def test_collect_stage_skips_irrelevant_resume_and_recovers_p1(monkeypatch, tmp_path):
    skipped = {"resume_from": "p5", "ckpt": {}, "now": datetime.now()}
    orchestrator._stage_collect_intel(skipped)
    assert "backtrack" not in skipped

    p1_path = tmp_path / "p1.json"
    p1_path.write_text(json.dumps([{"title": "saved"}]), encoding="utf-8-sig")
    context = {
        "resume_from": "p1",
        "ckpt": {"files": {"p1": str(p1_path)}},
        "now": datetime.now(),
        "p1_results": [],
    }
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "_recover_p2_p3_from_checkpoint", lambda _ckpt: ("p2", "p3"))
    monkeypatch.setattr(orchestrator, "format_evidence", lambda rows: f"formatted:{len(rows)}")

    orchestrator._stage_collect_intel(context)

    assert context["p1_results"] == [{"title": "saved"}]
    assert context["evidence_raw"] == "formatted:1"
    assert (context["p2_data"], context["p3_data"]) == ("p2", "p3")


def test_collect_resume_ignores_non_list_p1_and_reuses_saved_markdown(monkeypatch):
    context = {
        "resume_from": "p1",
        "ckpt": {"files": {"p1": "p1.json", "p1_md": "p1.md"}},
        "now": datetime.now(),
        "p1_results": [{"title": "existing"}],
    }
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "_read_json_checkpoint", lambda *_args: {"not": "a list"})
    monkeypatch.setattr(orchestrator, "_recover_p2_p3_from_checkpoint", lambda _ckpt: ("p2", "p3"))
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda *_args: "saved markdown")
    monkeypatch.setattr(
        orchestrator,
        "format_evidence",
        lambda _rows: pytest.fail("saved markdown must be preferred"),
    )

    orchestrator._stage_collect_intel(context)

    assert context["p1_results"] == [{"title": "existing"}]
    assert context["evidence_raw"] == "saved markdown"


def test_p2_resume_collects_only_p2_and_reuses_saved_p3_p5(monkeypatch):
    calls = []

    def fail_unrelated_source(*_args, **_kwargs):
        pytest.fail("P2 targeted resume must not rerun P1 or P3")

    monkeypatch.setattr(orchestrator, "collect_from_rss", fail_unrelated_source)
    monkeypatch.setattr(orchestrator, "collect_from_web", fail_unrelated_source)
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", fail_unrelated_source)
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", fail_unrelated_source)
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(
        orchestrator,
        "collect_from_x",
        lambda backtrack: calls.append(("p2", backtrack)) or ("fresh p2", {"status": "success"}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_read_text_checkpoint",
        lambda path, label: calls.append((label, path))
        or {"P5 audit result": "saved p5", "P3 agent intel": "saved p3", "P2 X intel": ""}[label],
    )
    context = {
        "resume_from": "p2_resume",
        "ckpt": {"files": {"p1": "p1.json", "p3": "p3.md", "p5": "p5.md"}},
        "now": datetime.now(),
        "backtrack": "",
        "p5_evidence": "",
        "p2_data": "",
        "p3_data": "",
    }

    orchestrator._stage_audit_evidence(context)

    assert orchestrator._resolve_start_stage("p2_resume") == "recover_p2"
    assert context["p2_data"] == "fresh p2"
    assert context["p3_data"] == "saved p3"
    assert context["p5_evidence"] == "saved p5"
    assert calls.count(("p2", "24h")) == 1


def test_p2_resume_failure_is_recorded_without_blocking_downstream(monkeypatch):
    updates = []
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(
        orchestrator,
        "_read_text_checkpoint",
        lambda _path, label: {"P5 audit result": "saved p5", "P3 agent intel": "saved p3", "P2 X intel": ""}[label],
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_from_x",
        lambda _backtrack: (_ for _ in ()).throw(RuntimeError("grok unavailable")),
    )
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = {
        "resume_from": "p2_resume",
        "ckpt": {"files": {"p1": "p1.json", "p3": "p3.md", "p5": "p5.md"}},
        "now": datetime.now(),
        "backtrack": "",
        "p5_evidence": "",
        "p2_data": "",
        "p3_data": "",
    }

    orchestrator._stage_audit_evidence(context)

    assert context["p2_data"] == ""
    assert context["p3_data"] == "saved p3"
    assert any(stage == "p2" and payload["status"] == "failed_exception" for stage, payload in updates)


def test_p2_resume_flows_fresh_p2_into_p6_without_rerunning_other_sources(monkeypatch):
    def fail_reused_source(*_args, **_kwargs):
        pytest.fail("P2 targeted resume must reuse P1/P3/P4/P5 checkpoints")

    monkeypatch.setattr(orchestrator, "collect_from_rss", fail_reused_source)
    monkeypatch.setattr(orchestrator, "collect_from_web", fail_reused_source)
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", fail_reused_source)
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", fail_reused_source)
    monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", fail_reused_source)
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "collect_from_x", lambda _backtrack: ("fresh p2", {"status": "success"}))
    saved = {
        "P1 markdown": "",
        "P5 audit result": "saved p5",
        "P3 agent intel": "saved p3",
        "P2 X intel": "",
        "P4 market snapshot": "saved p4",
    }
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda _path, label: saved[label])
    captured = []
    monkeypatch.setattr(orchestrator, "generate_report", lambda evidence: captured.append(evidence) or "fresh p6")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "fresh-p6.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_load_p6_identity_validation",
        lambda *_args, **_kwargs: {
            "complete": True,
            "reason": "",
            "rejected_target_names": [],
            "rejected_targets": 0,
            "identity_validation_version": orchestrator.IDENTITY_VALIDATION_VERSION,
        },
    )
    context = orchestrator._build_context(
        datetime.now(),
        "20260715083000",
        {
            "stage": "p2_resume",
            "files": {"p1": "p1.json", "p3": "p3.md", "p4": "p4.md", "p5": "p5.md"},
            "p6_metrics": {},
        },
        "p2_resume",
    )

    orchestrator._stage_audit_evidence(context)
    orchestrator._stage_market_mapping(context)

    assert captured == ["saved p5\nsaved p4\nfresh p2\nsaved p3"]
    assert context["p6_mapping"] == "fresh p6"


def test_p2_resume_before_p5_reuses_p1_p3_then_runs_p2_and_p5(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(
        orchestrator,
        "_read_json_checkpoint",
        lambda _path, _label: [{"title": "saved p1"}],
    )
    saved = {
        "P1 markdown": "saved raw p1",
        "P2 X intel": "",
        "P3 agent intel": "saved p3",
    }
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda _path, label: saved.get(label, ""))
    monkeypatch.setattr(
        orchestrator,
        "collect_from_x",
        lambda _backtrack: calls.append("p2") or ("fresh p2", {"status": "success"}),
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_pro_audit",
        lambda evidence, **_kwargs: calls.append(("p5", evidence)) or "fresh p5",
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = orchestrator._build_context(
        datetime.now(),
        "20260715083000",
        {
            "stage": "p2_resume",
            "files": {"p1": "p1.json", "p1_md": "p1.md", "p3": "p3.md"},
            "p6_metrics": {},
        },
        "p2_resume",
    )

    orchestrator._stage_audit_evidence(context)

    assert calls == ["p2", ("p5", "saved raw p1")]
    assert context["p1_results"] == [{"title": "saved p1"}]
    assert context["p2_data"] == "fresh p2"
    assert context["p3_data"] == "saved p3"
    assert context["p5_evidence"] == "fresh p5"


def test_collect_stage_tolerates_all_parallel_source_failures(monkeypatch):
    updates = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(orchestrator, "collect_from_rss", fail)
    monkeypatch.setattr(orchestrator, "collect_from_web", fail)
    monkeypatch.setattr(orchestrator, "collect_from_x", fail)
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", fail)
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = {"resume_from": "none", "ckpt": {"files": {}}, "now": datetime.now()}

    orchestrator._stage_collect_intel(context)

    assert context["p1_results"] == []
    assert context["p2_data"] == ""
    assert context["p3_data"] == ""
    assert any(stage == "p1" and payload["status"] == "failed_no_results" for stage, payload in updates)
    assert any(stage == "p2" and payload["status"] == "failed_exception" for stage, payload in updates)
    assert any(stage == "p3" and payload["status"] == "failed_exception" for stage, payload in updates)


@pytest.mark.parametrize(
    ("stats_status", "expected_status"),
    [
        ("skipped_no_key", "skipped_no_key"),
        ("empty", "skipped_no_results"),
        ("failed_all_queries", "failed_all_queries"),
    ],
)
def test_persist_p3_empty_result_keeps_explicit_business_status(
    monkeypatch,
    stats_status,
    expected_status,
):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._persist_p3_artifacts(
        "",
        {"status": stats_status, "duration_seconds": 1.2, "query_failures": 2},
    )

    assert updates[-1][0] == "p3"
    assert updates[-1][1]["status"] == expected_status


def test_persist_p1_partial_source_failure_is_degraded(monkeypatch):
    updates = []
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "p1.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "p1.md")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._persist_p1_artifacts(
        [{"title": "one surviving item", "text": "HBM supply remains tight"}],
        "evidence",
        [],
        1.0,
        {"source_failures": 1, "source_failure_details": ["rss: unavailable"]},
    )

    assert updates[-1][1]["status"] == "completed_structured_degraded"


def test_persist_p1_semantic_empty_records_are_not_saved(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "write_json_artifact",
        lambda *_args, **_kwargs: pytest.fail("semantic-empty P1 JSON must not be saved"),
    )
    monkeypatch.setattr(
        orchestrator,
        "write_text_artifact",
        lambda *_args, **_kwargs: pytest.fail("semantic-empty P1 Markdown must not be saved"),
    )
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._persist_p1_artifacts(
        [{"title": "empty", "text": "暂无相关证据。"}, {"title": "missing text"}],
        "",
        [],
        1.0,
        {},
    )

    assert updates[-1][1]["status"] == "skipped_no_results"
    assert updates[-1][1]["metrics"]["records"] == 0
    assert updates[-1][1]["metrics"]["semantic_rejected_records"] == 2


def test_persist_p3_nonempty_shell_with_zero_items_is_failed(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "write_text_artifact",
        lambda *_args, **_kwargs: pytest.fail("incomplete P3 Markdown must not be saved"),
    )
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._persist_p3_artifacts(
        "# P3 结构化机动情报\n\n采集时间: 2026-07-15",
        {"status": "success", "structured_items": 0},
    )

    assert updates[-1][1]["status"] == "failed_incomplete_output"


def test_collect_stage_counts_internal_p1_source_failures(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "collect_from_rss",
        lambda _value: (
            [{"title": "surviving item", "text": "usable RSS evidence"}],
            {"failed": [("rss-source", "down")], "success": [], "failed_urls": []},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_from_web",
        lambda _value: ([], {"failed": [("web-source", "timeout")], "success": []}),
    )
    monkeypatch.setattr(orchestrator, "collect_from_x", lambda _value: ("p2", {}))
    monkeypatch.setattr(
        orchestrator,
        "collect_p3_parallel_only",
        lambda _value: ("p3", {"status": "success"}),
    )
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "format_evidence", lambda _rows: "evidence")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "p1.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "artifact.md")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = {"resume_from": "none", "ckpt": {"files": {}}, "now": datetime.now()}

    orchestrator._stage_collect_intel(context)

    p1_update = next(payload for stage, payload in updates if stage == "p1")
    assert p1_update["status"] == "completed_structured_degraded"
    assert p1_update["metrics"]["source_failures"] == 2
    assert "rss-source" in p1_update["metrics"]["degrade_reason"]


def test_intel_resume_reruns_only_failed_p3_branch(monkeypatch):
    calls = []

    def fail_reused(*_args, **_kwargs):
        pytest.fail("successful P1/P2 branches must be reused")

    monkeypatch.setattr(orchestrator, "collect_from_rss", fail_reused)
    monkeypatch.setattr(orchestrator, "collect_from_web", fail_reused)
    monkeypatch.setattr(orchestrator, "collect_from_x", fail_reused)
    monkeypatch.setattr(
        orchestrator,
        "collect_p3_parallel_only",
        lambda _value: calls.append("p3") or ("fresh p3", {"status": "success"}),
    )
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "p3.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = {
        "resume_from": "intel_resume",
        "ckpt": {
            "files": {"p1": "p1.json", "p2": "p2.md", "p5": "p5.md"},
            "intel_resume_stages": ["p3"],
        },
        "now": datetime.now(),
        "backtrack": "",
        "p1_results": [{"title": "saved p1"}],
        "p1_stats": {},
        "evidence_raw": "saved p1 evidence",
        "p2_data": "saved p2",
        "p3_data": "",
        "p3_stats": {},
        "p1_duration": 0.0,
    }

    orchestrator._stage_collect_intel(context)

    assert calls == ["p3"]
    assert context["p1_results"] == [{"title": "saved p1"}]
    assert context["p2_data"] == "saved p2"
    assert context["p3_data"] == "fresh p3"


def test_intel_p1_resume_does_not_reload_stale_p5_after_fresh_audit_is_empty(monkeypatch):
    captured = []

    def read_checkpoint(_path, label):
        if label == "P5 audit":
            pytest.fail("old P5 must not be reused after P1 was recollected")
        if label == "P4 market snapshot":
            return "saved p4"
        return ""

    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", read_checkpoint)
    monkeypatch.setattr(orchestrator, "generate_report", lambda evidence: captured.append(evidence) or "fresh p6")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "p6.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_load_p6_identity_validation",
        lambda *_args, **_kwargs: {
            "complete": True,
            "reason": "",
            "rejected_target_names": [],
            "rejected_targets": 0,
            "identity_validation_version": orchestrator.IDENTITY_VALIDATION_VERSION,
        },
    )
    context = {
        "resume_from": "intel_resume",
        "intel_resume_stages": ["p1"],
        "ckpt": {
            "files": {"p4": "p4.md", "p5": "old-p5.md"},
            "p6_metrics": {},
        },
        "p5_evidence": "",
        "p4_data": "",
        "p2_data": "saved p2",
        "p3_data": "saved p3",
        "p6_mapping": "",
    }

    orchestrator._stage_market_mapping(context)

    assert captured == ["saved p4\nsaved p2\nsaved p3"]


def test_collect_stage_merges_jinnang_recovery_into_non_dict_raw_results(monkeypatch):
    failed_urls = [{"url": "https://example.test"}]
    monkeypatch.setattr(
        orchestrator,
        "collect_from_rss",
        lambda _backtrack: ([{"title": "rss"}], {"failed_urls": failed_urls}),
    )
    monkeypatch.setattr(orchestrator, "collect_from_web", lambda _backtrack: ([], {}))
    monkeypatch.setattr(orchestrator, "collect_from_x", lambda _backtrack: ("p2", {}))
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", lambda _backtrack: ("old p3", {"raw_results": []}))
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "format_evidence", lambda _rows: "evidence")
    monkeypatch.setattr(orchestrator, "run_jinnang_1", lambda _urls: [{"text": "recovered"}])
    monkeypatch.setattr(orchestrator, "render_p3_output", lambda raw: ("new p3", [{"raw": raw}]))
    persisted = []
    monkeypatch.setattr(orchestrator, "_persist_p1_artifacts", lambda *args: persisted.append("p1"))
    monkeypatch.setattr(orchestrator, "_persist_p3_artifacts", lambda data, stats: persisted.append((data, stats.copy())))
    context = {"resume_from": "none", "ckpt": {"files": {}}, "now": datetime.now()}

    orchestrator._stage_collect_intel(context)

    assert context["p3_data"] == "new p3"
    assert context["p3_stats"]["raw_results"]["jinnang_1"] == [{"text": "recovered"}]
    assert persisted[-1][0] == "new p3"


def test_collect_stage_extends_existing_jinnang_raw_result_mapping(monkeypatch):
    failed_urls = [{"url": "https://example.test"}]
    monkeypatch.setattr(orchestrator, "collect_from_rss", lambda _value: ([], {"failed_urls": failed_urls}))
    monkeypatch.setattr(orchestrator, "collect_from_web", lambda _value: ([], {}))
    monkeypatch.setattr(orchestrator, "collect_from_x", lambda _value: ("", {}))
    monkeypatch.setattr(
        orchestrator,
        "collect_p3_parallel_only",
        lambda _value: ("old", {"raw_results": {"jinnang_2": [{"text": "existing"}]}}),
    )
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "run_jinnang_1", lambda _urls: [{"text": "recovered"}])
    monkeypatch.setattr(orchestrator, "render_p3_output", lambda raw: ("merged", list(raw)))
    monkeypatch.setattr(orchestrator, "_persist_p3_artifacts", lambda *_args: None)
    context = {"resume_from": "none", "ckpt": {"files": {}}, "now": datetime.now()}

    orchestrator._stage_collect_intel(context)

    assert set(context["p3_stats"]["raw_results"]) == {"jinnang_1", "jinnang_2"}


def test_collect_stage_logs_jinnang_recovery_failure(monkeypatch, caplog):
    failed_urls = [{"url": "https://example.test"}]
    monkeypatch.setattr(orchestrator, "collect_from_rss", lambda _value: ([], {"failed_urls": failed_urls}))
    monkeypatch.setattr(orchestrator, "collect_from_web", lambda _value: ([], {}))
    monkeypatch.setattr(orchestrator, "collect_from_x", lambda _value: ("", {}))
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", lambda _value: ("", {}))
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(
        orchestrator,
        "run_jinnang_1",
        lambda _urls: (_ for _ in ()).throw(RuntimeError("recovery failed")),
    )
    context = {"resume_from": "none", "ckpt": {"files": {}}, "now": datetime.now()}
    caplog.set_level(logging.WARNING)

    orchestrator._stage_collect_intel(context)

    assert "jinnang_1 recovery failed" in caplog.text


def test_audit_stage_loads_resume_checkpoints_when_intel_is_missing(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_read_text_checkpoint",
        lambda path, label: f"{label}:{path}",
    )
    monkeypatch.setattr(orchestrator, "_recover_p2_p3_from_checkpoint", lambda _ckpt: ("p2 recovered", "p3 recovered"))
    context = {
        "resume_from": "p5",
        "ckpt": {"files": {"p5": "p5.md"}},
        "p2_data": "",
        "p3_data": "",
    }

    orchestrator._stage_audit_evidence(context)

    assert context["p5_evidence"] == "P5 audit result:p5.md"
    assert context["p2_data"] == "p2 recovered"
    assert context["p3_data"] == "p3 recovered"


def test_audit_resume_keeps_already_loaded_intel(monkeypatch):
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda *_args: "saved p5")
    monkeypatch.setattr(
        orchestrator,
        "_recover_p2_p3_from_checkpoint",
        lambda _ckpt: pytest.fail("loaded intel must not be replaced"),
    )
    context = {
        "resume_from": "p5",
        "ckpt": {"files": {"p5": "p5.md"}},
        "p2_data": "p2 loaded",
        "p3_data": "p3 loaded",
    }
    orchestrator._stage_audit_evidence(context)
    assert (context["p2_data"], context["p3_data"]) == ("p2 loaded", "p3 loaded")


def test_market_mapping_reruns_incomplete_checkpoint_and_raises_on_mapping_failure(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "_load_p6_identity_validation",
        lambda *_args, **_kwargs: {
            "complete": False,
            "reason": "identity_validation_unversioned",
            "rejected_target_names": [],
            "rejected_targets": 0,
            "identity_validation_version": 0,
        },
    )
    monkeypatch.setattr(orchestrator, "is_older_than_any", lambda *_args: False)
    monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", lambda: "market")
    monkeypatch.setattr(
        orchestrator,
        "generate_report",
        lambda _evidence: (_ for _ in ()).throw(RuntimeError("mapping failed")),
    )
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = {
        "resume_from": "p6",
        "ckpt": {"files": {"p6": "old-p6.md"}, "p6_metrics": {}},
        "p5_evidence": "evidence",
        "p2_data": "p2",
        "p3_data": "p3",
        "p4_data": "",
        "p6_mapping": "",
    }

    with pytest.raises(RuntimeError, match="P6 mapping failed: mapping failed"):
        orchestrator._stage_market_mapping(context)

    assert context["p4_data"] == "market"
    assert context["p6_mapping"] == ""
    assert not any(stage == "p6" and payload.get("status") == "completed" for stage, payload in updates)


def test_empty_p6_mapping_stops_registry_before_report(monkeypatch):
    import pipeline_runtime

    downstream_calls = []
    monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", lambda: "market")
    monkeypatch.setattr(orchestrator, "generate_report", lambda _evidence: "")
    monkeypatch.setattr(pipeline_runtime, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = {
        "resume_from": "p6_resume",
        "ckpt": {"files": {}},
        "p5_evidence": "saved p5",
        "p2_data": "saved p2",
        "p3_data": "saved p3",
        "p4_data": "",
        "p6_mapping": "",
    }
    stages = [
        pipeline_runtime.StageSpec("market_mapping", orchestrator._stage_market_mapping),
        pipeline_runtime.StageSpec("report", lambda _context: downstream_calls.append("P9")),
    ]

    with pytest.raises(RuntimeError, match="P6 mapping returned empty output"):
        pipeline_runtime.run_stage_registry(stages, context)

    assert downstream_calls == []


def test_p6_resume_reuses_upstream_artifacts_and_runs_only_market_mapping(monkeypatch):
    def fail_unrelated_stage(*_args, **_kwargs):
        pytest.fail("P6 targeted resume must not rerun P1, P2, P3, or P5")

    monkeypatch.setattr(orchestrator, "collect_from_rss", fail_unrelated_stage)
    monkeypatch.setattr(orchestrator, "collect_from_web", fail_unrelated_stage)
    monkeypatch.setattr(orchestrator, "collect_from_x", fail_unrelated_stage)
    monkeypatch.setattr(orchestrator, "collect_p3_parallel_only", fail_unrelated_stage)
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", fail_unrelated_stage)
    monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", fail_unrelated_stage)
    saved = {
        "P5 audit": "saved p5",
        "P2 X intel": "saved p2",
        "P3 agent intel": "saved p3",
        "P4 market snapshot": "saved p4",
    }
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda _path, label: saved[label])
    captured = []
    monkeypatch.setattr(orchestrator, "generate_report", lambda evidence: captured.append(evidence) or "fresh p6")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "fresh-p6.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_load_p6_identity_validation",
        lambda *_args, **_kwargs: {
            "complete": True,
            "reason": "",
            "rejected_target_names": [],
            "rejected_targets": 0,
            "identity_validation_version": orchestrator.IDENTITY_VALIDATION_VERSION,
        },
    )
    context = orchestrator._build_context(
        datetime.now(),
        "20260715083000",
        {
            "stage": "p6_resume",
            "files": {"p2": "p2.md", "p3": "p3.md", "p4": "p4.md", "p5": "p5.md"},
            "p6_metrics": {},
        },
        "p6_resume",
    )

    orchestrator._stage_market_mapping(context)

    assert orchestrator._resolve_start_stage("p6_resume") == "market_mapping"
    assert captured == ["saved p5\nsaved p4\nsaved p2\nsaved p3"]
    assert context["p6_mapping"] == "fresh p6"


def test_market_mapping_reuses_complete_checkpoint_and_dependent_research(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_load_p6_identity_validation",
        lambda *_args, **_kwargs: {
            "complete": True,
            "reason": "",
            "rejected_target_names": ["Rejected"],
            "rejected_targets": 1,
            "identity_validation_version": orchestrator.IDENTITY_VALIDATION_VERSION,
        },
    )
    monkeypatch.setattr(orchestrator, "is_older_than_any", lambda *_args: False)
    values = {
        "P6 mapping": "saved p6",
        "P5 audit": "saved p5",
        "P4 market snapshot": "saved p4",
        "P8 technical data": "saved p8",
    }
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda _path, label: values[label])
    monkeypatch.setattr(orchestrator, "_recover_p2_p3_from_checkpoint", lambda _ckpt: ("saved p2", "saved p3"))
    monkeypatch.setattr(
        orchestrator,
        "_read_fresh_text_checkpoint",
        lambda _ckpt, key, _label, **_kwargs: {"p7": "saved p7", "p75": "saved p75", "p7a": "saved p7a"}[key],
    )
    context = {
        "resume_from": "p9_resume",
        "ckpt": {
            "files": {key: f"{key}.md" for key in ("p2", "p3", "p4", "p5", "p6", "p7", "p75", "p7a", "p8")},
            "p6_metrics": {},
        },
        "p5_evidence": "",
        "p2_data": "loaded p2",
        "p3_data": "loaded p3",
        "p4_data": "",
        "p6_mapping": "",
    }

    orchestrator._stage_market_mapping(context)

    assert context["p6_mapping"] == "saved p6"
    assert context["p7a_audited"] == "saved p7a"
    assert context["price_data"] == "saved p8"
    assert context["rejected_target_names"] == ["Rejected"]


def _research_context(**overrides):
    context = {
        "resume_from": "none",
        "ckpt": {"files": {"p6": "p6.md"}, "p6_metrics": {}},
        "p6_mapping": "P6 mapping",
        "p4_data": "",
        "p5_evidence": "",
        "p7_deep": "",
        "p7a_audited": "",
        "p75_supplement": "",
        "search_queries": [],
        "rejected_target_names": [],
    }
    context.update(overrides)
    return context


def test_research_stage_short_circuits_without_mapping():
    context = _research_context(p6_mapping="")
    orchestrator._stage_research(context)
    assert "research_plan" not in context


def test_research_plan_write_failure_and_existing_audit_short_circuit(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["preserved query"],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        orchestrator,
        "write_json_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    context = _research_context(
        p7a_audited="already audited",
        rejected_target_names=["Rejected"],
    )

    orchestrator._stage_research(context)

    assert context["p7_plan_path"] == ""
    assert context["p7a_audited"] == "already audited"
    assert context["search_queries"] == ["preserved query"]


def test_research_plan_failure_is_recorded_on_p7_plan_not_clean_research(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "build_research_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("plan unavailable")),
    )
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "plan.json")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))

    orchestrator._stage_research(_research_context())

    assert any(
        stage == "p7_plan" and payload["status"] == "failed_plan_fallback"
        for stage, payload in updates
    )
    assert not any(stage == "research" for stage, _payload in updates)


def test_research_p7_only_path_skips_supplement_and_omits_missing_plan_file(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["unused"],
        "should_run_p7": True,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        orchestrator,
        "write_json_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("plan unavailable")),
    )
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: {"text": "short research", "rejected_identity_lines_removed": 0},
    )
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    updates = []
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = _research_context()

    orchestrator._stage_research(context)

    p7_update = next(payload for stage, payload in updates if stage == "p7")
    assert p7_update["files"] == {"md": "p7.md"}
    assert not any(stage == "p75" for stage, _payload in updates)
    assert context["p7a_audited"] == "short research"


@pytest.mark.parametrize(
    ("primary_source_count", "expected_tier"),
    [
        (0, "summary_lead_pending_verification"),
        (1, "primary_source_backed"),
    ],
)
def test_research_persists_p7_topic_and_aggregate_evidence_artifact(
    monkeypatch,
    primary_source_count,
    expected_tier,
):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": True,
        "should_run_p75": False,
    }
    topic_provenance = {
        "topic": "示例主题",
        "primary_source_count": primary_source_count,
        "summary_source_count": 2,
        "source_url_count": 3,
        "evidence_tier": expected_tier,
    }
    aggregate_provenance = {
        "primary_source_count": primary_source_count,
        "summary_source_count": 2,
        "source_url_count": 3,
        "evidence_tier": expected_tier,
        "topics": [topic_provenance],
    }
    json_writes = []
    p7a_calls = []
    updates = []

    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: {
            "text": "P7 research " * 30,
            "evidence_provenance": aggregate_provenance,
            "rejected_identity_lines_removed": 0,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "write_json_artifact",
        lambda prefix, data, **_kwargs: json_writes.append((prefix, data)) or f"{prefix}.json",
    )
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda prefix, *_args, **_kwargs: f"{prefix}.md")
    monkeypatch.setattr(
        orchestrator,
        "p7a_deep_research_audit",
        lambda _text, **kwargs: p7a_calls.append(kwargs) or "audited P7",
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = _research_context()

    orchestrator._stage_research(context)

    evidence_payload = next(data for prefix, data in json_writes if prefix == "P7_证据元数据")
    assert evidence_payload["aggregate"]["evidence_tier"] == expected_tier
    assert evidence_payload["topics"] == [topic_provenance]
    assert context["p7_evidence_provenance"]["topics"] == [topic_provenance]
    assert p7a_calls[-1]["p7_evidence_provenance"]["topics"] == [topic_provenance]
    p7_update = next(payload for stage, payload in updates if stage == "p7")
    assert p7_update["files"]["evidence"] == "P7_证据元数据.json"


def test_research_resume_reuses_both_optional_layers(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["query"],
        "should_run_p7": True,
        "should_run_p75": True,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: pytest.fail("saved P7 must be reused"),
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda *_args: pytest.fail("saved P7.5 must be reused"),
    )
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = _research_context(
        resume_from="p6",
        p7_deep="saved deep",
        p75_supplement="saved supplement",
    )

    orchestrator._stage_research(context)

    assert context["p7_deep"] == "saved deep"
    assert context["p75_supplement"] == "saved supplement"


def test_research_resume_does_not_rerun_valid_empty_optional_layers(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["query"],
        "should_run_p7": True,
        "should_run_p75": True,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: pytest.fail("valid-empty P7 checkpoint must not be rerun"),
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda *_args: pytest.fail("valid-empty P7.5 checkpoint must not be rerun"),
    )
    context = _research_context(
        resume_from="p8_resume",
        ckpt={
            "files": {"p6": "p6.md"},
            "p6_metrics": {},
            "stage_assessments": {
                "p7": {"state": "valid_empty", "reusable": True},
                "p75": {"state": "valid_empty", "reusable": True},
            },
        },
    )

    orchestrator._stage_research(context)

    assert context["p7_deep"] == ""
    assert context["p75_supplement"] == ""


def test_research_new_p6_hash_clears_untriggered_stale_optional_layers(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "new-hash")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: pytest.fail("the new plan must not run P7"),
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda *_args: pytest.fail("the new plan must not run P7.5"),
    )
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = _research_context(
        resume_from="p6",
        ckpt={
            "files": {"p6": "p6.md"},
            "p6_metrics": {},
            "p6_input_sha256": {
                "p7": "old-hash",
                "p7a": "old-hash",
                "p75": "old-hash",
            },
            "stage_assessments": {
                stage: {
                    "status": "completed",
                    "state": "invalid",
                    "reusable": False,
                    "reason": "stale_p6_input_hash",
                }
                for stage in ("p7", "p7a", "p75")
            },
        },
    )

    orchestrator._stage_research(context)

    refreshed = {
        stage: payload
        for stage, payload in updates
        if stage in {"p7", "p7a", "p75"}
    }
    assert set(refreshed) == {"p7", "p7a", "p75"}
    for payload in refreshed.values():
        assert payload["status"] == "skipped_not_triggered"
        assert payload["metrics"]["data_state"] == "valid_empty"
        assert payload["metrics"]["p6_input_sha256"] == "new-hash"


def test_research_resume_does_not_rerun_valid_empty_p7a(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "p7a_deep_research_audit",
        lambda *_args, **_kwargs: pytest.fail("valid-empty P7a checkpoint must not be rerun"),
    )
    context = _research_context(
        resume_from="p8_resume",
        p7_deep="research evidence " * 20,
        ckpt={
            "files": {"p6": "p6.md", "p7": "p7.md"},
            "p6_metrics": {},
            "stage_assessments": {
                "p7": {"state": "valid", "reusable": True},
                "p7a": {"state": "valid_empty", "reusable": True},
            },
        },
    )

    orchestrator._stage_research(context)

    assert context["p7a_audited"] == ""


def test_research_resume_reuses_short_p7a_passthrough(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "p7a_deep_research_audit",
        lambda *_args, **_kwargs: pytest.fail("short P7a passthrough must not be rerun"),
    )
    raw_research = "short research"
    context = _research_context(
        resume_from="p8_resume",
        p7_deep=raw_research,
        ckpt={
            "files": {"p6": "p6.md", "p7": "p7.md"},
            "p6_metrics": {},
            "stage_assessments": {
                "p7": {"state": "valid", "reusable": True},
                "p7a": {
                    "state": "valid_passthrough",
                    "status": "skipped_short_input",
                    "reusable": True,
                },
            },
        },
    )

    orchestrator._stage_research(context)

    assert context["p7a_audited"] == raw_research


def test_research_resume_marks_failed_p7a_passthrough(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "p7a_deep_research_audit",
        lambda *_args, **_kwargs: pytest.fail("failed P7a passthrough must not be rerun on resume"),
    )
    raw_research = "research evidence " * 20
    context = _research_context(
        resume_from="p8_resume",
        p7_deep=raw_research,
        ckpt={
            "files": {"p6": "p6.md", "p7": "p7.md"},
            "p6_metrics": {},
            "stage_assessments": {
                "p7": {"state": "valid", "reusable": True},
                "p7a": {
                    "state": "valid_passthrough",
                    "status": "failed_semantic_output",
                    "reusable": True,
                },
            },
        },
    )

    orchestrator._stage_research(context)

    assert context["p7a_audited"].startswith("> 【P7a结构化降级】")
    assert context["p7a_audited"].endswith(raw_research)


def test_research_short_input_records_passthrough_checkpoint(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    updates = []
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = _research_context(p7_deep="short research")

    orchestrator._stage_research(context)

    p7a_update = next(payload for stage, payload in updates if stage == "p7a")
    assert p7a_update["status"] == "skipped_short_input"
    assert p7a_update["metrics"]["data_state"] == "valid_passthrough"
    assert p7a_update["metrics"]["p6_input_sha256"] == "hash"


def test_research_long_audit_without_p6_hash_skips_running_manifest(monkeypatch):
    raw = "X" * 250
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(orchestrator, "p7a_deep_research_audit", lambda *_args, **_kwargs: "audited")
    updates = []
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = _research_context(p7_deep=raw)

    orchestrator._stage_research(context)

    assert context["p7a_audited"] == "audited"
    assert not any(stage == "p7a" and payload["status"] == "running" for stage, payload in updates)


def test_research_optional_layers_handle_search_and_future_failures(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["not-found", "raises"],
        "should_run_p7": True,
        "should_run_p75": True,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: (_ for _ in ()).throw(RuntimeError("deep failed")),
    )

    def search(_layer, query, _window):
        if query == "raises":
            raise RuntimeError("search failed")
        return {"text": "not found."}

    updates = []
    monkeypatch.setattr(orchestrator, "gemini_search", search)
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = _research_context()

    orchestrator._stage_research(context)

    assert context["p7_deep"] == ""
    assert context["p75_supplement"] == ""
    assert context["search_queries"] == ["not-found", "raises"]
    assert any(stage == "p7" and payload["status"] == "failed_exception" for stage, payload in updates)
    assert any(stage == "p75" and payload["status"] == "failed_query_errors" for stage, payload in updates)


def test_research_empty_result_preserves_provider_failure_status(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": True,
        "should_run_p75": False,
    }
    updates = []
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda _plan: {"status": "failed_empty_output", "text": ""},
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))

    orchestrator._stage_research(_research_context())

    failure = next(payload for stage, payload in updates if stage == "p7")
    assert failure["status"] == "failed_empty_output"
    assert failure["metrics"]["p6_input_sha256"] == "hash"


def test_research_p7a_failure_passes_through_long_raw_research(monkeypatch):
    raw_research = "R" * 250
    plan = {
        "goal": "goal",
        "triggers": ["trigger"],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "p7.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "hash")
    monkeypatch.setattr(
        orchestrator,
        "p7a_deep_research_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    updates = []
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = _research_context(p7_deep=raw_research)

    orchestrator._stage_research(context)

    assert context["p7a_audited"].startswith("> 【P7a结构化降级】")
    assert context["p7a_audited"].endswith(raw_research)
    assert any(stage == "p7a" and payload["status"] == "failed_exception_passthrough" for stage, payload in updates)


def test_technical_fallback_survives_artifact_write_failure(monkeypatch):
    updates = []
    monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda _report: [("Stock", "600000")])
    monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda _report: {})
    monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda _report: {})
    monkeypatch.setattr(
        orchestrator,
        "p8_search_stock_prices",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("prices unavailable")),
    )
    monkeypatch.setattr(
        orchestrator,
        "write_text_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = {"resume_from": "none", "ckpt": {"files": {}}, "p6_mapping": "mapping", "price_data": ""}

    orchestrator._stage_technical(context)

    assert "降级占位" in context["price_data"]
    assert updates[-1][1]["files"] is None
    assert updates[-1][1]["status"] == "failed_exception_fallback"


def test_report_wraps_generation_exception(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "p9_risk_analysis",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("invalid model response")),
    )
    with pytest.raises(RuntimeError, match="report generation failed: invalid model response"):
        orchestrator._stage_report({"p6_mapping": "mapping"})


def test_report_rejects_missing_p6_before_calling_p9(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "p9_risk_analysis", lambda **_kwargs: calls.append("P9"))

    with pytest.raises(RuntimeError, match="P6 mapping is missing"):
        orchestrator._stage_report({"p6_mapping": ""})

    assert calls == []


def test_report_p9_resume_keeps_initial_stage_assessments_and_refreshes_p8(monkeypatch):
    captured = {}
    checkpoint_snapshot = {
        "run_timestamp": "20260728083002",
        "stage_assessments": {"p7a": {"state": "valid", "reusable": True}},
        "p8_assessment": {"state": "invalid", "reusable": False},
    }
    refreshed_p8 = {"state": "valid", "reusable": True}
    monkeypatch.setattr(
        orchestrator,
        "detect_checkpoint",
        lambda: {
            "stage_assessments": {"p7a": {"state": "stale", "reusable": False}},
            "p8_assessment": refreshed_p8,
        },
    )

    def fake_p9(**kwargs):
        captured["snapshot"] = kwargs["checkpoint_snapshot"]
        return "# fallback report", "20260728083002"

    monkeypatch.setattr(orchestrator, "p9_risk_analysis", fake_p9)
    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda *_args, **_kwargs: {"ok": True, "status": "completed_no_email"},
    )

    orchestrator._stage_report(
        {
            "resume_from": "p9_resume",
            "ckpt": checkpoint_snapshot,
            "p6_mapping": "mapping",
        }
    )

    assert captured["snapshot"] is not checkpoint_snapshot
    assert captured["snapshot"]["stage_assessments"] is checkpoint_snapshot["stage_assessments"]
    assert captured["snapshot"]["p8_assessment"] is refreshed_p8


def test_delivery_resume_validates_mode_report_and_delivery(monkeypatch):
    calls = []
    context = {"resume_from": "none"}
    orchestrator._stage_delivery_resume(context)
    assert calls == []

    with pytest.raises(RuntimeError, match="saved report is missing"):
        orchestrator._stage_delivery_resume({"resume_from": "delivery_resume", "final_report": ""})

    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda *_args, **_kwargs: {"ok": False, "status": "smtp_failed"},
    )
    with pytest.raises(RuntimeError, match="smtp_failed"):
        orchestrator._stage_delivery_resume(
            {"resume_from": "delivery_resume", "final_report": "# report", "run_ts": "ts"}
        )


def test_raw_p1_unreviewed_p5_forces_p9_manual_review_and_local_only_delivery(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {
            "stages": {
                "p5": {
                    "status": "failed_empty_output_raw_p1_fallback",
                    "metrics": {"fallback": "raw_p1_unreviewed", "data_state": "invalid"},
                }
            }
        },
    )
    monkeypatch.setattr(orchestrator, "_resolve_p9_checkpoint_snapshot", lambda _context: {})

    def fake_p9(**kwargs):
        captured["manual_review_required"] = kwargs.get("manual_review_required")
        return "# manual review", "20260830083002"

    def fake_delivery(_content, _report_ts, *, email_required=None):
        captured["email_required"] = email_required
        return {"ok": True, "status": "completed_no_email", "email_sent": False}

    monkeypatch.setattr(orchestrator, "p9_risk_analysis", fake_p9)
    monkeypatch.setattr(orchestrator, "send_report_email", fake_delivery)

    orchestrator._stage_report(
        {
            "p5_evidence": "unreviewed P1 raw evidence",
            "p6_mapping": "P6 mapping",
            "p7a_audited": "P7 audit",
            "p75_supplement": "P7.5 audit",
            "price_data": "P8 data",
            "p2_data": "",
            "p3_data": "",
            "p4_data": "",
            "rejected_target_names": [],
            "ckpt": {},
        }
    )

    assert captured == {"manual_review_required": True, "email_required": False}


@pytest.mark.parametrize(
    "manifest_loader",
    [
        lambda: (_ for _ in ()).throw(OSError("manifest unavailable")),
        lambda: {"stages": {"p5": {"metrics": "malformed"}}},
    ],
)
def test_p5_review_state_read_failure_fails_closed_and_is_read_once(monkeypatch, manifest_loader):
    captured = {"manifest_reads": 0}

    def load_manifest():
        captured["manifest_reads"] += 1
        return manifest_loader()

    monkeypatch.setattr(orchestrator, "load_run_manifest", load_manifest)
    monkeypatch.setattr(orchestrator, "_resolve_p9_checkpoint_snapshot", lambda _context: {})

    def fake_p9(**kwargs):
        captured["manual_review_required"] = kwargs["manual_review_required"]
        return "# local review", "20260830083002"

    def fake_delivery(_content, _report_ts, *, email_required=None):
        captured["email_required"] = email_required
        return {"ok": True, "status": "completed_no_email", "email_sent": False}

    monkeypatch.setattr(
        orchestrator,
        "p9_risk_analysis",
        fake_p9,
    )
    monkeypatch.setattr(orchestrator, "send_report_email", fake_delivery)

    orchestrator._stage_report({"p6_mapping": "P6 mapping", "ckpt": {}})

    assert captured == {
        "manifest_reads": 1,
        "manual_review_required": True,
        "email_required": False,
    }


def test_delivery_resume_does_not_downgrade_required_email_to_local_only(monkeypatch):
    monkeypatch.setattr(orchestrator, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(orchestrator, "EMAIL_PASSWORD", "smtp-secret")
    monkeypatch.setattr(orchestrator, "RECEIVER_EMAIL", "")
    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda *_args: pytest.fail("missing required email config must stop before delivery"),
    )

    with pytest.raises(RuntimeError, match="email configuration is incomplete"):
        orchestrator._stage_delivery_resume(
            {
                "resume_from": "delivery_resume",
                "final_report": "# report",
                "run_ts": "ts",
                "ckpt": {"p9_metrics": {"email_configured": True}},
            }
        )


def test_delivery_resume_preserves_saved_local_only_policy(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(orchestrator, "EMAIL_PASSWORD", "smtp-secret")
    monkeypatch.setattr(orchestrator, "RECEIVER_EMAIL", "receiver@example.com")

    def fake_delivery(content, report_ts, *, email_required=None):
        captured.update(
            {"content": content, "report_ts": report_ts, "email_required": email_required}
        )
        return {"ok": True, "status": "completed_no_email"}

    monkeypatch.setattr(orchestrator, "send_report_email", fake_delivery)
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)

    orchestrator._stage_delivery_resume(
        {
            "resume_from": "delivery_resume",
            "final_report": "# report",
            "run_ts": "20260715090000",
            "ckpt": {"p9_metrics": {"email_configured": False}},
        }
    )

    assert captured["email_required"] is False


def test_roi_resume_requires_report_and_recovers_p1_with_invalid_manifest_metric(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="ROI cannot resume"):
        orchestrator._stage_roi(
            {"resume_from": "roi_resume", "final_report": "", "ckpt": {"files": {}}}
        )

    p1_path = tmp_path / "p1.json"
    p1_path.write_text(json.dumps([{"title": "saved"}]), encoding="utf-8-sig")
    manifests = [
        {"stages": {"p1": {"metrics": {"duration_seconds": "bad"}}}},
        {"stages": {"p1": {"status": "completed"}}},
    ]
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: manifests.pop(0))
    captured = {}

    def build_report(**kwargs):
        captured.update(kwargs)
        return {
            "layers": {
                "p1": {
                    "units": 1,
                    "unique_units": 1,
                    "unique_ratio": 1.0,
                    "adopted_units": 1,
                    "adoption_ratio": 1.0,
                    "p6_hits": 1,
                    "p9_hits": 1,
                    "score": 1.0,
                    "recommendation": "keep",
                },
                "p3": {
                    "units": 0,
                    "unique_units": 0,
                    "unique_ratio": 0.0,
                    "adopted_units": 0,
                    "adoption_ratio": 0.0,
                    "p6_hits": 0,
                    "p9_hits": 0,
                    "score": 0.0,
                    "recommendation": "drop",
                },
            }
        }

    monkeypatch.setattr(orchestrator, "build_layer_roi_report", build_report)
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda _payload: "roi")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "roi.md")
    updates = []
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: updates.append((stage, kwargs)))
    context = {
        "resume_from": "roi_resume",
        "final_report": "# saved report",
        "ckpt": {"files": {"p1": str(p1_path)}},
        "p1_results": [],
        "p3_stats": {},
        "p3_data": "",
        "p4_data": "",
        "p75_supplement": "",
        "p75_raw_text": "",
        "search_queries": [],
        "p6_mapping": "",
        "p1_stats": {},
    }

    orchestrator._stage_roi(context)

    assert captured["p1_results"] == [{"title": "saved"}]
    assert captured["timings"]["p1"] == 0.0
    assert any(stage == "report" and payload["status"] == "completed_recovered" for stage, payload in updates)
    assert updates[-1][0] == "p1"
    assert updates[-1][1]["metrics"]["roi_score"] == 1.0
    assert not any(stage == "p3" for stage, _payload in updates)


def test_roi_ignores_non_list_p1_checkpoint(monkeypatch):
    monkeypatch.setattr(orchestrator, "_read_json_checkpoint", lambda *_args: {"not": "a list"})
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: {"stages": {}})
    captured = {}

    def build_report(**kwargs):
        captured.update(kwargs)
        return {"layers": {}}

    monkeypatch.setattr(orchestrator, "build_layer_roi_report", build_report)
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda _payload: "roi")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "roi.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = {
        "resume_from": "none",
        "ckpt": {"files": {"p1": "p1.json"}},
        "p1_results": [],
        "p3_stats": {},
        "p3_data": "",
        "p4_data": "",
        "p75_supplement": "",
        "p75_raw_text": "",
        "search_queries": [],
        "p6_mapping": "",
        "final_report": "",
        "p1_stats": {},
    }

    orchestrator._stage_roi(context)

    assert captured["p1_results"] == []


def _patch_main_runtime(monkeypatch, *, checkpoint, registry_effect=None, degraded=None):
    manifest_updates = []
    run_states = []
    contexts = []
    monkeypatch.setattr(orchestrator, "detect_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        orchestrator,
        "set_run_timestamp",
        lambda timestamp=None: timestamp or "20260623090000",
    )
    monkeypatch.setattr(orchestrator, "get_preflight_summary", lambda: {"exit_code": 0})
    monkeypatch.setattr(orchestrator, "build_runtime_context", lambda: {"cwd": "test"})
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda stage, **kwargs: manifest_updates.append((stage, kwargs)))
    monkeypatch.setattr(orchestrator, "update_manifest_context", lambda **kwargs: contexts.append(kwargs))
    monkeypatch.setattr(orchestrator, "record_run_state", lambda **kwargs: run_states.append(kwargs))
    monkeypatch.setattr(orchestrator, "_build_context", lambda *_args: {"final_report": "# report"})
    if registry_effect is None:
        registry_effect = lambda _stages, _context, start_at=None: None
    monkeypatch.setattr(orchestrator, "run_stage_registry", registry_effect)
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: {"stages": {}})
    monkeypatch.setattr(orchestrator, "summarize_stage_degradations", lambda _manifest: degraded or [])
    return manifest_updates, run_states, contexts


@pytest.mark.parametrize(
    ("schedule", "expected_status"),
    [
        ({"is_trading_day": False, "should_run": False, "reason": "holiday"}, "skipped_non_trading_day"),
        ({"is_trading_day": True, "should_run": False, "reason": "rule"}, "skipped_schedule_rule"),
    ],
)
def test_orchestrator_skips_non_runnable_schedule(monkeypatch, schedule, expected_status):
    states = []
    monkeypatch.setattr(orchestrator, "get_schedule_decision", lambda _day: schedule)
    monkeypatch.setattr(orchestrator, "record_run_state", lambda **kwargs: states.append(kwargs))
    monkeypatch.setattr(orchestrator, "build_runtime_context", lambda: {"cwd": "test"})

    orchestrator.orchestrator_main([])

    assert states[-1]["status"] == expected_status
    assert states[-1]["main_flow_entered"] is False


def test_orchestrator_p9_done_checkpoint_records_existing_report(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "get_schedule_decision",
        lambda _day: {"is_trading_day": True, "should_run": True, "reason": "ok"},
    )
    updates, states, _contexts = _patch_main_runtime(
        monkeypatch,
        checkpoint={"stage": "p9_done", "files": {}, "run_timestamp": "20260623090000"},
    )

    orchestrator.orchestrator_main([])

    assert states[-1]["status"] == "completed_existing_report"
    assert updates[-1][1]["status"] == "completed"


def test_orchestrator_manual_confirmation_checkpoint_does_not_run_stages(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "get_schedule_decision",
        lambda _day: {"is_trading_day": True, "should_run": True, "reason": "ok"},
    )
    updates, states, _contexts = _patch_main_runtime(
        monkeypatch,
        checkpoint={
            "stage": "manual_confirmation_required",
            "files": {"war_report": "saved-report.md"},
            "run_timestamp": "20260623090000",
        },
        registry_effect=lambda *_args, **_kwargs: pytest.fail("manual confirmation must not start any stage"),
    )

    orchestrator.orchestrator_main([])

    assert states[-1]["status"] == "manual_confirmation_required"
    assert states[-1]["main_flow_entered"] is False
    assert updates[-1][1]["status"] == "manual_confirmation_required"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("stage crashed"), "failed_exception"),
        (orchestrator.StageTimeoutError("research", 10), "failed_timeout"),
    ],
)
def test_orchestrator_records_stage_failures_and_reraises(monkeypatch, error, expected_status):
    monkeypatch.setattr(
        orchestrator,
        "get_schedule_decision",
        lambda _day: {"is_trading_day": True, "should_run": True, "reason": "ok"},
    )

    def fail_registry(_stages, _context, start_at=None):
        raise error

    updates, states, _contexts = _patch_main_runtime(
        monkeypatch,
        checkpoint={"stage": "none", "files": {}, "run_timestamp": ""},
        registry_effect=fail_registry,
    )

    with pytest.raises(type(error), match=str(error).replace("'", "\\'") if "'" not in str(error) else None):
        orchestrator.orchestrator_main([])

    assert updates[-1][1]["status"] == expected_status
    assert states[-1]["status"] == expected_status


def test_orchestrator_force_restart_skips_checkpoint_and_records_degradation(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "get_schedule_decision",
        lambda _day: pytest.fail("force run bypasses schedule"),
    )
    monkeypatch.setattr(
        orchestrator,
        "detect_checkpoint",
        lambda: pytest.fail("force restart bypasses checkpoint"),
    )
    degraded = [{"stage": "p4", "status": "failed_exception", "reason": "api down"}]
    resets = []
    monkeypatch.setattr(orchestrator, "reset_run_manifest", lambda run_ts: resets.append(run_ts) or "archive.json")
    updates, states, _contexts = _patch_main_runtime(
        monkeypatch,
        checkpoint={"stage": "unused", "files": {}, "run_timestamp": ""},
        degraded=degraded,
    )

    orchestrator.orchestrator_main(["--force-run", "--force-restart"])

    assert resets == ["20260623090000"]
    assert updates[-1][1]["status"] == "completed_with_stage_degradation"
    assert states[-1]["root_cause"] == "stage_degraded"
    assert states[-1]["runtime_context"]["degraded_stages"] == degraded


def test_orchestrator_file_logging_configures_handlers_and_noisy_loggers(monkeypatch):
    calls = []

    class Handler:
        def __init__(self, path, **kwargs):
            calls.append(("handler", path, kwargs))

        def setLevel(self, level):
            calls.append(("handler_level", level))

        def setFormatter(self, _formatter):
            calls.append("formatter")

    class Logger:
        def __init__(self, name):
            self.name = name

        def addHandler(self, _handler):
            calls.append(("add", self.name))

        def setLevel(self, level):
            calls.append(("logger_level", self.name, level))

    fake_logging = SimpleNamespace(
        INFO=logging.INFO,
        ERROR=logging.ERROR,
        WARNING=logging.WARNING,
        Formatter=logging.Formatter,
        basicConfig=lambda **kwargs: calls.append(("basic", kwargs)),
        getLogger=lambda name="root": Logger(name),
    )
    monkeypatch.setattr(orchestrator, "_configure_stdio", lambda: calls.append("stdio"))
    monkeypatch.setattr(orchestrator, "logging", fake_logging)
    monkeypatch.setattr(orchestrator, "RotatingFileHandler", Handler)
    monkeypatch.setattr(orchestrator, "get_pipeline_log_path", lambda: "pipeline.log")
    monkeypatch.setattr(orchestrator, "get_error_log_path", lambda: "error.log")

    orchestrator._configure_file_logging()

    assert calls[0] == "stdio"
    assert any(item == ("add", "root") for item in calls)
    assert ("logger_level", "google", logging.WARNING) in calls


def test_pipeline_cli_script_entrypoint_exposes_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [pipeline_cli.__file__, "--help"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(pipeline_cli.__file__, run_name="__main__")

    assert exc_info.value.code == 0
    assert "VCP Hunter pipeline control plane" in capsys.readouterr().out


def test_orchestrator_script_entrypoint_wires_logging_and_main_safely(monkeypatch):
    import artifact_store
    import logging.handlers
    import run_config

    states = []
    handlers = []

    class NoopHandler(logging.Handler):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            handlers.append(self)

        def emit(self, _record):
            return None

    monkeypatch.setattr(
        run_config,
        "get_schedule_decision",
        lambda _day: {"is_trading_day": False, "should_run": False, "reason": "test holiday"},
    )
    monkeypatch.setattr(artifact_store, "record_run_state", lambda **kwargs: states.append(kwargs))
    monkeypatch.setattr(artifact_store, "build_runtime_context", lambda: {"cwd": "test"})
    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", NoopHandler)
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)
    monkeypatch.setattr(sys, "argv", [orchestrator.__file__])

    root_logger = logging.getLogger()
    before_handlers = list(root_logger.handlers)
    try:
        runpy.run_path(orchestrator.__file__, run_name="__main__")
    finally:
        for handler in list(root_logger.handlers):
            if handler not in before_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    assert states[-1]["status"] == "skipped_non_trading_day"
    assert len(handlers) == 2
