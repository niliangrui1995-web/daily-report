from __future__ import annotations

import hashlib
import inspect
import os
from datetime import datetime
from pathlib import Path


RUN_TS = "20260710083000"


def _write(path: Path, text: str = "data", *, mtime: float | None = None) -> str:
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


def _manifest(
    report_path: str,
    *,
    root_status: str = "completed",
    p9_status: str = "completed_email_sent",
    report_status: str = "completed",
    roi_status: str | None = "completed",
) -> dict:
    stages = {
        "p9": {"status": p9_status, "files": {"md": report_path}, "metrics": {}},
        "report": {"status": report_status},
    }
    if roi_status is not None:
        stages["roi"] = {"status": roi_status}
    return {
        "run_timestamp": RUN_TS,
        "status": root_status,
        "stages": stages,
    }


def _detect(monkeypatch, tmp_path: Path, manifest: dict) -> dict:
    import checkpoint

    monkeypatch.setattr(checkpoint, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(checkpoint, "load_run_manifest", lambda: manifest)
    return checkpoint.detect_checkpoint()


def test_report_file_alone_is_not_a_terminal_checkpoint(monkeypatch, tmp_path):
    _write(tmp_path / f"\u6218\u62a5_{RUN_TS}.md", "report")

    checkpoint = _detect(monkeypatch, tmp_path, {})

    assert checkpoint["stage"] != "p9_done"


def test_terminal_checkpoint_requires_all_success_states(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")

    checkpoint = _detect(monkeypatch, tmp_path, _manifest(report_path))

    assert checkpoint["stage"] == "p9_done"


def test_delivery_failure_resumes_delivery_instead_of_marking_complete(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")
    manifest = _manifest(
        report_path,
        root_status="failed_exception",
        p9_status="completed_email_failed",
        report_status="failed_exception",
        roi_status=None,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "delivery_resume"


def test_missing_roi_resumes_only_roi(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")
    manifest = _manifest(
        report_path,
        root_status="failed_exception",
        roi_status=None,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"


def test_failed_roi_resumes_only_roi(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")
    manifest = _manifest(
        report_path,
        root_status="failed_exception",
        roi_status="failed_exception",
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"


def test_failed_root_is_not_terminal_even_after_roi_completed(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")
    manifest = _manifest(report_path, root_status="failed_exception")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"


def test_zero_byte_report_is_not_terminal(monkeypatch, tmp_path):
    report = tmp_path / f"war_report_{RUN_TS}.md"
    report.touch()
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "p8")
    manifest = _manifest(str(report))
    manifest["stages"]["p8"] = {"status": "completed", "files": {"md": p8_path}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert not checkpoint["files"].get("war_report")


def test_stale_research_files_are_dropped_relative_to_p6(monkeypatch, tmp_path):
    p6 = _write(tmp_path / f"P6_mapping_{RUN_TS}.md", "new p6", mtime=3000)
    p7 = _write(tmp_path / f"P7_research_{RUN_TS}.md", "old p7", mtime=1000)
    p7a = _write(tmp_path / f"P7a_audit_{RUN_TS}.md", "old p7a", mtime=1100)
    p75 = _write(tmp_path / f"P7.5_audit_{RUN_TS}.md", "old p75", mtime=1200)
    manifest = {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p6": {"status": "completed", "files": {"md": p6}},
            "p7": {"status": "completed", "files": {"md": p7}},
            "p7a": {"status": "completed", "files": {"md": p7a}},
            "p75": {"status": "completed", "files": {"md": p75}},
        },
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert not {"p7", "p7a", "p75"}.intersection(checkpoint["files"])


def test_research_hash_rejects_changed_p6_with_unchanged_mtime(monkeypatch, tmp_path):
    p6 = _write(tmp_path / f"P6_mapping_{RUN_TS}.md", "old p6", mtime=3000)
    expected_hash = hashlib.sha256(Path(p6).read_bytes()).hexdigest()
    p7 = _write(tmp_path / f"P7_research_{RUN_TS}.md", "p7", mtime=4000)
    p7a = _write(tmp_path / f"P7a_audit_{RUN_TS}.md", "p7a", mtime=4100)
    p75 = _write(tmp_path / f"P7.5_audit_{RUN_TS}.md", "p75", mtime=4200)
    manifest = {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p6": {"status": "completed", "files": {"md": p6}},
            "p7": {
                "status": "completed",
                "files": {"md": p7},
                "metrics": {"p6_input_sha256": expected_hash},
            },
            "p7a": {
                "status": "completed",
                "files": {"md": p7a},
                "metrics": {"p6_input_sha256": expected_hash},
            },
            "p75": {
                "status": "completed",
                "files": {"md": p75},
                "metrics": {"p6_input_sha256": expected_hash},
            },
        },
    }

    matching_checkpoint = _detect(monkeypatch, tmp_path, manifest)
    assert matching_checkpoint["stage"] == "p8_resume"
    assert {"p7", "p7a", "p75"}.issubset(matching_checkpoint["files"])

    Path(p6).write_text("new p6", encoding="utf-8")
    os.utime(p6, (3000, 3000))
    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert not {"p7", "p7a", "p75"}.intersection(checkpoint["files"])


def test_research_stages_persist_p6_input_sha256(monkeypatch, tmp_path):
    import orchestrator

    p6 = _write(tmp_path / "p6.md", "current p6", mtime=3000)
    expected_hash = hashlib.sha256(Path(p6).read_bytes()).hexdigest()
    ckpt = {"stage": "none", "run_timestamp": RUN_TS, "files": {"p6": p6}}
    context = orchestrator._build_context(datetime.now(), RUN_TS, ckpt, "none")
    context["p6_mapping"] = "current p6"
    updates: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        orchestrator,
        "build_research_plan",
        lambda *args, **kwargs: {
            "goal": "goal",
            "triggers": [],
            "deep_topics": ["topic"],
            "supplement_queries": ["query"],
            "should_run_p7": True,
            "should_run_p75": True,
            "max_rounds": 1,
            "round_backtracks": ["24h"],
            "skipped_recent_topics": [],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "run_deep_research",
        lambda plan: {"text": "deep evidence " * 30},
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda *args, **kwargs: {"text": "supplement evidence"},
    )
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda text, **kwargs: text)
    monkeypatch.setattr(orchestrator, "p7a_deep_research_audit", lambda text, **kwargs: "audited " + text)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *args, **kwargs: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *args, **kwargs: "artifact.md")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._stage_research(context)

    for stage in ("p7", "p7a", "p75"):
        assert any(
            name == stage and payload.get("metrics", {}).get("p6_input_sha256") == expected_hash
            for name, payload in updates
        )


def test_market_mapping_does_not_load_stale_research(monkeypatch, tmp_path):
    import orchestrator

    p6 = _write(tmp_path / "p6.md", "new p6", mtime=3000)
    p7 = _write(tmp_path / "p7.md", "old p7", mtime=1000)
    p7a = _write(tmp_path / "p7a.md", "old p7a", mtime=1100)
    p75 = _write(tmp_path / "p75.md", "old p75", mtime=1200)
    ckpt = {
        "stage": "p8_resume",
        "run_timestamp": RUN_TS,
        "files": {"p6": p6, "p7": p7, "p7a": p7a, "p75": p75},
        "p6_metrics": {"identity_validation_version": 2},
    }
    context = orchestrator._build_context(datetime.now(), RUN_TS, ckpt, "p8_resume")

    orchestrator._stage_market_mapping(context)

    assert context["p6_mapping"] == "new p6"
    assert context["p7_deep"] == ""
    assert context["p7a_audited"] == ""
    assert context["p75_supplement"] == ""


def test_stale_p75_path_does_not_suppress_new_supplement_search(monkeypatch, tmp_path):
    import orchestrator

    p6 = _write(tmp_path / "p6.md", "new p6", mtime=3000)
    p75 = _write(tmp_path / "p75.md", "old p75", mtime=1000)
    ckpt = {
        "stage": "p9_resume",
        "run_timestamp": RUN_TS,
        "files": {"p6": p6, "p75": p75},
    }
    context = orchestrator._build_context(datetime.now(), RUN_TS, ckpt, "p9_resume")
    context["p6_mapping"] = "mapping"
    calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "build_research_plan",
        lambda *args, **kwargs: {
            "goal": "goal",
            "triggers": [],
            "deep_topics": [],
            "supplement_queries": ["fresh query"],
            "should_run_p7": False,
            "should_run_p75": True,
            "max_rounds": 0,
            "round_backtracks": [],
            "skipped_recent_topics": [],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda category, query, backtrack, **kwargs: calls.append(query) or {"text": "fresh result"},
    )
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda text, **kwargs: text)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *args, **kwargs: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *args, **kwargs: "artifact.md")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *args, **kwargs: None)

    orchestrator._stage_research(context)

    assert calls == ["fresh query"]
    assert context["p75_supplement"] == "fresh result"


def test_delivery_resume_reuses_saved_report_without_regeneration(monkeypatch, tmp_path):
    import orchestrator

    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "saved report")
    ckpt = {
        "stage": "delivery_resume",
        "run_timestamp": RUN_TS,
        "files": {"war_report": report_path},
    }
    context = orchestrator._build_context(datetime.now(), RUN_TS, ckpt, "delivery_resume")
    deliveries = []
    updates = []

    monkeypatch.setattr(
        orchestrator,
        "p9_risk_analysis",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("report must not be regenerated")),
    )
    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda content, report_ts: deliveries.append((content, report_ts))
        or {"ok": True, "status": "completed_email_sent"},
    )
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    orchestrator._stage_delivery_resume(context)

    assert deliveries == [("saved report", RUN_TS)]
    assert any(stage == "report" and payload["status"] == "completed_recovered" for stage, payload in updates)


def test_roi_resume_loads_report_and_starts_at_roi_without_delivery(monkeypatch, tmp_path):
    import orchestrator

    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "saved report")
    ckpt = {
        "stage": "roi_resume",
        "run_timestamp": RUN_TS,
        "files": {"war_report": report_path},
    }
    context = orchestrator._build_context(datetime.now(), RUN_TS, ckpt, "roi_resume")
    captured = {}

    monkeypatch.setattr(
        orchestrator,
        "p9_risk_analysis",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("report must not be regenerated")),
    )
    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("email must not be redelivered")),
    )

    def fake_build_layer_roi_report(**kwargs):
        captured.update(kwargs)
        return {"layers": {}}

    monkeypatch.setattr(orchestrator, "build_layer_roi_report", fake_build_layer_roi_report)
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda payload: "roi")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *args, **kwargs: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *args, **kwargs: "roi.md")
    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {"stages": {"p9": {"status": "completed_email_sent"}, "report": {"status": "completed"}}},
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *args, **kwargs: None)

    orchestrator._stage_roi(context)

    assert orchestrator._resolve_start_stage("roi_resume") == "roi"
    assert context["final_report"] == "saved report"
    assert captured["final_report"] == "saved report"


def test_inner_thread_timeouts_do_not_claim_hard_deadlines():
    import orchestrator
    import search_engine

    market_source = inspect.getsource(orchestrator._stage_market_mapping)
    report_source = inspect.getsource(orchestrator._stage_report)
    research_source = inspect.getsource(orchestrator._stage_research)
    registry = {stage.name: stage for stage in orchestrator._build_stage_registry()}

    assert "ThreadPoolExecutor" not in market_source
    assert "ThreadPoolExecutor" not in report_source
    assert ".result(timeout=" not in research_source
    assert registry["market_mapping"].timeout_seconds == 900
    assert registry["research"].timeout_seconds == 900
    assert registry["report"].timeout_seconds == 900
    assert "timeout=180" in inspect.getsource(search_engine._post_search_request)


def test_legacy_p6_rejection_without_names_is_incomplete_identity_validation(monkeypatch):
    import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {
            "stages": {
                "p6": {
                    "status": "completed",
                    "metrics": {"rejected_targets": 1},
                }
            }
        },
    )

    validation = orchestrator._load_p6_identity_validation()

    assert validation["rejected_target_names"] == []
    assert validation["complete"] is False
    assert validation["reason"] == "legacy_rejections_missing_names"


def test_versioned_p6_identity_validation_accepts_unnamed_malformed_targets():
    import orchestrator

    validation = orchestrator._load_p6_identity_validation(
        {
            "rejected_targets": 2,
            "rejected_target_names": [],
            "identity_validation_version": 2,
        }
    )

    assert validation["complete"] is True
    assert validation["reason"] == ""


def test_previous_identity_validation_version_is_stale_after_canonical_alias_upgrade():
    import orchestrator

    validation = orchestrator._load_p6_identity_validation(
        {
            "rejected_targets": 1,
            "rejected_target_names": ["宏和电子"],
            "identity_validation_version": 1,
        }
    )

    assert validation["complete"] is False
    assert validation["rejected_target_names"] == []
    assert validation["reason"] == "identity_validation_outdated"


def test_unversioned_p6_identity_validation_is_not_reused():
    import orchestrator

    validation = orchestrator._load_p6_identity_validation({})

    assert validation["complete"] is False
    assert validation["reason"] == "identity_validation_unversioned"


def test_research_and_report_drop_known_canonical_alias_from_rejected_names(monkeypatch):
    import orchestrator

    context = orchestrator._build_context(
        datetime.now(),
        RUN_TS,
        {"stage": "none", "run_timestamp": RUN_TS, "files": {}},
        "none",
    )
    context.update(
        {
            "p6_mapping": "DEEP_RESEARCH: 中国巨石/宏和电子/国际复材供给弹性",
            "p5_evidence": "P5 evidence",
            "p4_data": "P4 data",
            "price_data": "P8 data " * 20,
            "p2_data": "P2 data",
            "p3_data": "P3 data",
            "rejected_target_names": ["宏和电子"],
        }
    )
    captured = {}

    def fake_build_plan(*args, **kwargs):
        captured["research_names"] = kwargs.get("rejected_target_names")
        return {
            "goal": "",
            "triggers": [],
            "deep_topics": [],
            "supplement_queries": [],
            "should_run_p7": False,
            "should_run_p75": False,
            "max_rounds": 0,
            "round_backtracks": [],
            "skipped_recent_topics": [],
        }

    monkeypatch.setattr(orchestrator, "build_research_plan", fake_build_plan)
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *args, **kwargs: "plan.json")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *args, **kwargs: None)

    orchestrator._stage_research(context)

    def fake_p9(*args, **kwargs):
        captured["report_names"] = kwargs.get("rejected_target_names")
        return "final report", RUN_TS

    monkeypatch.setattr(orchestrator, "p9_risk_analysis", fake_p9)
    monkeypatch.setattr(
        orchestrator,
        "send_report_email",
        lambda *args, **kwargs: {"ok": True, "status": "completed_email_sent"},
    )
    monkeypatch.setattr(orchestrator, "_run_agent_report_review", lambda _context: None)

    orchestrator._stage_report(context)

    assert captured["research_names"] == []
    assert captured["report_names"] == []
