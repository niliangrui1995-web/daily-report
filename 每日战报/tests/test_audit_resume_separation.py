from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("audit_type", "stage_name", "file_prefix"),
    [
        ("p5", "p5", "P5_审计"),
        ("p7.5", "p75", "P7.5_补充审计"),
    ],
)
def test_audit_resume_artifact_excludes_deleted_evidence(
    monkeypatch,
    audit_type,
    stage_name,
    file_prefix,
):
    import audit_engine

    text_writes: list[tuple[str, str]] = []
    manifest_updates: list[tuple[str, dict]] = []
    payload = {
        "summary": {"passed": 1, "deleted": 1},
        "high_trust": ["1. [芯片] 可恢复的可信事实"],
        "medium_trust": [],
        "deleted": [{"reason": "过期", "content": "不得复活的旧证据"}],
    }

    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: (
            "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|"
            "{vanguard_list}|{raw_data}"
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content="model-json"),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda prefix, _data: f"{prefix}.json",
    )

    def write_text(prefix, text):
        text_writes.append((prefix, text))
        return f"{prefix}.md"

    monkeypatch.setattr(audit_engine, "write_text_artifact", write_text)
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda stage, **kwargs: manifest_updates.append((stage, kwargs)),
    )

    result = audit_engine.gemini_pro_audit("raw evidence", audit_type=audit_type)

    assert "可恢复的可信事实" in result
    assert "不得复活的旧证据" not in result
    main_body = next(text for prefix, text in text_writes if prefix == file_prefix)
    deleted_body = next(
        text for prefix, text in text_writes if prefix == f"{file_prefix}删除清单"
    )
    assert "可恢复的可信事实" in main_body
    assert "不得复活的旧证据" not in main_body
    assert "不得复活的旧证据" in deleted_body

    stage, update = manifest_updates[-1]
    assert stage == stage_name
    assert update["files"]["md"] == f"{file_prefix}.md"
    assert update["files"]["deleted"] == f"{file_prefix}删除清单.md"


def test_p5_all_deleted_is_valid_empty_and_recovers_as_empty(tmp_path):
    import checkpoint
    import orchestrator

    contaminated_path = tmp_path / "P5_审计_20260715150000.md"
    contaminated_path.write_text("不得复活的旧证据", encoding="utf-8")
    manifest = {
        "manifest_version": 3,
        "run_timestamp": "20260715150000",
        "stages": {
            "p5": {
                "status": "completed_no_results",
                "files": {"md": str(contaminated_path)},
                "metrics": {"passed_count": 0, "deleted_count": 1},
            }
        },
    }
    files = {"p5": str(contaminated_path)}

    assessment = checkpoint.assess_stage_checkpoint("p5", manifest, files)

    assert assessment["reusable"] is True
    assert assessment["state"] == "valid_empty"
    recovered = orchestrator._recover_p5_checkpoint(
        {
            "files": files,
            "stage_assessments": {"p5": assessment},
        }
    )
    assert recovered == ""


def test_p75_valid_empty_never_recovers_saved_audit_or_raw_text(tmp_path):
    import orchestrator

    audited = tmp_path / "P7.5_补充审计_20260715150000.md"
    raw = tmp_path / "P7.5_补充搜索原始数据_20260715150000.md"
    audited.write_text("不得复活的审计删除项", encoding="utf-8")
    raw.write_text("被审计全部删除的原始搜索", encoding="utf-8")

    recovered = orchestrator._recover_p75_checkpoint(
        {
            "files": {"p75": str(audited), "p75_raw": str(raw)},
            "stage_assessments": {"p75": {"state": "valid_empty", "reusable": True}},
        }
    )

    assert recovered == ("", "")


def test_p5_all_deleted_current_run_does_not_restore_raw_input(monkeypatch):
    import orchestrator

    manifest_updates: list[tuple[str, dict]] = []
    manifest = {"stages": {"p5": {"status": "completed_no_results"}}}
    monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: manifest)
    monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", lambda: "")
    monkeypatch.setattr(
        orchestrator,
        "generate_report",
        lambda _evidence: pytest.fail("P6 model must not run without meaningful evidence"),
    )
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "p6.md")
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: manifest_updates.append((stage, kwargs)),
    )
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
        "now": datetime.now(),
        "backtrack": "24h",
        "resume_from": "none",
        "ckpt": {"files": {}, "p6_metrics": {}},
        "evidence_raw": "这条原始证据已被 P5 全部删除",
        "p5_evidence": "",
        "p2_data": "",
        "p3_data": "",
        "p4_data": "",
        "p6_mapping": "",
    }

    orchestrator._run_p5_audit(context)
    assert context["p5_evidence"] == ""

    with pytest.raises(RuntimeError, match="no meaningful upstream evidence"):
        orchestrator._stage_market_mapping(context)

    assert manifest_updates[-1][0] == "p6"
    assert manifest_updates[-1][1]["status"] == "failed_missing_input"
