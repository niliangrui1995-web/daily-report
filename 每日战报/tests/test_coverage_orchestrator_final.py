# -*- coding: utf-8 -*-
"""Final offline branch coverage for orchestrator recovery paths."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest

import orchestrator


def test_text_checkpoint_handles_non_file_markdown_path(tmp_path, caplog):
    broken_path = tmp_path / "broken.md"
    broken_path.mkdir()

    assert orchestrator._read_text_checkpoint(str(broken_path), "broken") == ""
    assert "broken resume failed" in caplog.text


def test_research_hash_refresh_tolerates_legacy_container_shapes():
    assert (
        orchestrator._research_stage_needs_p6_hash_refresh(
            {"ckpt": []},
            "p7",
            "current-hash",
        )
        is False
    )
    assert (
        orchestrator._research_stage_needs_p6_hash_refresh(
            {
                "ckpt": {
                    "p6_input_sha256": [],
                    "stage_assessments": [],
                    "files": [],
                }
            },
            "p7",
            "current-hash",
        )
        is False
    )
    assert (
        orchestrator._research_stage_needs_p6_hash_refresh(
            {
                "ckpt": {
                    "stage_assessments": {"p7": []},
                    "files": {},
                }
            },
            "p7",
            "current-hash",
        )
        is False
    )


def test_build_context_reconstructs_missing_markdown_from_saved_p1(monkeypatch):
    rows = [{"title": "saved", "text": "meaningful evidence"}]
    monkeypatch.setattr(orchestrator, "get_run_config", lambda _now: "24h")
    monkeypatch.setattr(orchestrator, "_read_json_checkpoint", lambda *_args: rows)
    monkeypatch.setattr(orchestrator, "_read_text_checkpoint", lambda *_args: "")
    monkeypatch.setattr(orchestrator, "_recover_p2_p3_from_checkpoint", lambda _ckpt: ("", ""))
    monkeypatch.setattr(orchestrator, "format_evidence", lambda items: f"rebuilt:{len(items)}")

    context = orchestrator._build_context(
        datetime(2026, 7, 15),
        "20260715090000",
        {"stage": "p2_resume", "files": {"p1": "saved.json"}},
        "p2_resume",
    )

    assert context["p1_results"] == rows
    assert context["evidence_raw"] == "rebuilt:1"


def test_persist_p3_normalizes_non_mapping_stats(monkeypatch):
    updates = []
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **payload: updates.append((stage, payload)),
    )

    orchestrator._persist_p3_artifacts("", [])

    assert updates == [
        (
            "p3",
            {
                "status": "failed_empty_output",
                "metrics": {
                    "content_chars": 0,
                    "duration_seconds": 0,
                    "jinnang_1_count": 0,
                    "jinnang_2_count": 0,
                    "jinnang_3_count": 0,
                    "structured_items": 0,
                    "query_failures": 0,
                    "failure_errors": [],
                },
            },
        )
    ]


@pytest.mark.parametrize(
    ("audit_status", "audited", "expected"),
    [
        ("completed_no_results", "audited evidence", ""),
        ("failed_timeout", "", "fresh supplement evidence"),
        ("raw_ready", "", ""),
    ],
)
def test_research_p75_uses_manifest_status_for_empty_audit_fallback(
    monkeypatch,
    audit_status,
    audited,
    expected,
):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": ["query"],
        "should_run_p7": False,
        "should_run_p75": True,
    }

    @contextmanager
    def quiet_heartbeat(*_args, **_kwargs):
        yield

    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "_load_p6_identity_validation", lambda *_args: {"rejected_target_names": []})
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "raw.md")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(
        orchestrator,
        "gemini_search",
        lambda *_args: {"text": "fresh supplement evidence"},
    )
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda *_args, **_kwargs: audited)
    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {"stages": {"p75": {"status": audit_status}}},
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "log_heartbeat", quiet_heartbeat)
    context = {
        "resume_from": "none",
        "ckpt": {"files": {"p6": "p6.md"}, "p6_metrics": {}},
        "p6_mapping": "mapping",
        "p4_data": "",
        "p5_evidence": "",
        "p7_deep": "",
        "p7a_audited": "",
        "p75_supplement": "",
        "search_queries": [],
        "rejected_target_names": [],
    }

    orchestrator._stage_research(context)

    assert context["p75_supplement"] == expected


def test_research_resume_normalizes_non_mapping_stage_assessments(monkeypatch):
    plan = {
        "goal": "goal",
        "triggers": [],
        "supplement_queries": [],
        "should_run_p7": False,
        "should_run_p75": False,
    }
    monkeypatch.setattr(orchestrator, "build_research_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(orchestrator, "_load_p6_identity_validation", lambda *_args: {"rejected_target_names": []})
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "plan.json")
    monkeypatch.setattr(orchestrator, "_context_p6_input_sha256", lambda _context: "")
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = {
        "resume_from": "p6",
        "ckpt": {
            "files": {"p6": "p6.md"},
            "p6_metrics": {},
            "stage_assessments": [],
        },
        "p6_mapping": "mapping",
        "p4_data": "",
        "p5_evidence": "",
        "p7_deep": "",
        "p7a_audited": "",
        "p75_supplement": "",
        "search_queries": [],
        "rejected_target_names": [],
    }

    orchestrator._stage_research(context)

    assert context["search_queries"] == []


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        ([], {"layers_evaluated": 0, "total_units": 0}),
        (
            {
                "p1": [],
                "p2": {
                    "units": "bad",
                    "unique_units": "bad",
                    "adopted_units": "bad",
                },
            },
            {"layers_evaluated": 2, "total_units": 0},
        ),
    ],
)
def test_roi_normalizes_invalid_layer_metrics(monkeypatch, layers, expected):
    updates = []
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: {"stages": {}})
    monkeypatch.setattr(orchestrator, "build_layer_roi_report", lambda **_kwargs: {"layers": layers})
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda _payload: "roi")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args: "roi.md")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **payload: updates.append((stage, payload)),
    )
    context = {
        "resume_from": "none",
        "ckpt": {"files": {}},
        "p1_results": [],
        "p1_stats": {},
        "p3_stats": {},
        "p3_data": "",
        "p4_data": "",
        "p75_supplement": "",
        "p75_raw_text": "",
        "search_queries": [],
        "p6_mapping": "",
        "final_report": "",
    }

    orchestrator._stage_roi(context)

    metrics = updates[0][1]["metrics"]
    assert metrics["layers_evaluated"] == expected["layers_evaluated"]
    assert metrics["total_units"] == expected["total_units"]
