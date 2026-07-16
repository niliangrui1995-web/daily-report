from __future__ import annotations

import hashlib
import inspect
import os
from datetime import datetime
from pathlib import Path

import pytest


RUN_TS = "20260710083000"
P1_RECORD_JSON = '[{"text":"usable evidence"}]'


def _write(path: Path, text: str = "data", *, mtime: float | None = None) -> str:
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


def _valid_p6_markdown() -> str:
    targets = "\n".join(
        f"- Example {index}（60000{index}） | high | " + ("reason " * 35)
        for index in range(5)
    )
    return "### 赛道: example\n> 事件触发: usable event\n" + targets


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
        roi_payload = {"status": roi_status}
        if roi_status == "completed":
            report_dir = Path(report_path).parent
            roi_md = _write(
                report_dir / f"Layer_ROI_{RUN_TS}.md",
                "# Layer ROI\n\n- evaluated layers: 4",
            )
            roi_json = _write(
                report_dir / f"Layer_ROI_{RUN_TS}.json",
                '{"layers":{"p1":{},"p3":{},"p4":{},"p75":{}}}',
            )
            roi_payload["files"] = {"md": roi_md, "json": roi_json}
        stages["roi"] = roi_payload
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


def _p2_checkpoint_manifest(tmp_path: Path, p2_status: str, *, p2_path: str = "") -> dict:
    p1_path = _write(tmp_path / f"P1_RSS原始数据_{RUN_TS}.json", P1_RECORD_JSON)
    p3_path = _write(tmp_path / f"P3_机动情报_{RUN_TS}.md", "saved p3")
    p5_path = _write(tmp_path / f"P5_审计_{RUN_TS}.md", "saved p5")
    p2_payload = {"status": p2_status, "metrics": {}}
    if p2_path:
        p2_payload["files"] = {"md": p2_path}
    return {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p1": {"status": "completed", "files": {"json": p1_path}},
            "p2": p2_payload,
            "p3": {"status": "completed", "files": {"md": p3_path}},
            "p5": {"status": "completed", "files": {"md": p5_path}},
        },
    }


def _p2_before_p5_manifest(tmp_path: Path, p2_status: str | None) -> dict:
    p1_path = _write(tmp_path / f"P1_RSS原始数据_{RUN_TS}.json", P1_RECORD_JSON)
    p1_md_path = _write(tmp_path / f"P1_RSS原始数据_{RUN_TS}.md", "saved p1")
    p3_path = _write(tmp_path / f"P3_机动情报_{RUN_TS}.md", "saved p3")
    stages = {
        "p1": {
            "status": "completed",
            "files": {"json": p1_path, "md": p1_md_path},
        },
        "p3": {"status": "completed", "files": {"md": p3_path}},
    }
    if p2_status is not None:
        stages["p2"] = {"status": p2_status, "metrics": {}}
    return {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": stages,
    }


def _p6_checkpoint_manifest(tmp_path: Path, p6_status: str, *, p6_path: str = "") -> dict:
    p1_path = _write(tmp_path / f"P1_RSS原始数据_{RUN_TS}.json", P1_RECORD_JSON)
    p2_path = _write(
        tmp_path / f"P2_X平台情报_{RUN_TS}.md",
        "### 1. X intelligence\n- Claim: usable x intelligence",
    )
    p3_path = _write(
        tmp_path / f"P3_机动情报_{RUN_TS}.md",
        "### 1. P3 intelligence\n- Claim: usable machine intelligence",
    )
    p4_path = _write(
        tmp_path / f"P4_核心行情_{RUN_TS}.md",
        "- ARM: 收盘价 100.0",
    )
    p5_path = _write(
        tmp_path / f"P5_审计_{RUN_TS}.md",
        "## 审计摘要\n通过: 1条 / 删除: 0条\n\n## 高可信情报\n1. usable evidence",
    )
    p6_payload = {
        "status": p6_status,
        "metrics": {
            "accepted_targets": 5,
            "stock_count": 5,
            "identity_validation_version": 2,
        },
    }
    if p6_path:
        p6_payload["files"] = {"md": p6_path}
        newest_upstream_mtime = max(
            Path(path).stat().st_mtime
            for path in (p1_path, p2_path, p3_path, p4_path, p5_path)
        )
        os.utime(p6_path, (newest_upstream_mtime + 1, newest_upstream_mtime + 1))
    return {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p1": {"status": "completed", "files": {"json": p1_path}},
            "p2": {
                "status": "completed",
                "files": {"md": p2_path},
                "metrics": {"successful_chunks": 1, "content_length": 40, "structured_items": 1},
            },
            "p3": {
                "status": "completed",
                "files": {"md": p3_path},
                "metrics": {"content_chars": 50, "structured_items": 1},
            },
            "p4": {
                "status": "completed",
                "files": {"md": p4_path},
                "metrics": {"usable_tickers": 1},
            },
            "p5": {
                "status": "completed",
                "files": {"md": p5_path},
                "metrics": {"passed_count": 1, "deleted_count": 0},
            },
            "p6": p6_payload,
            "market_mapping": {"status": "failed_exception"},
        },
    }


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


def test_saved_report_pending_delivery_resumes_without_regenerating_p9(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "report")
    manifest = _manifest(
        report_path,
        root_status="running",
        p9_status="report_saved_pending_delivery",
        report_status="running",
        roi_status=None,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "delivery_resume"
    assert checkpoint["files"]["war_report"] == report_path
    assert checkpoint["p9_metrics"] == {}


def test_report_save_in_progress_with_same_run_report_resumes_delivery(monkeypatch, tmp_path):
    report_path = _write(tmp_path / f"战报_{RUN_TS}.md", "saved report")
    manifest = {
        "run_timestamp": RUN_TS,
        "status": "running",
        "stages": {
            "p9": {"status": "report_save_in_progress", "metrics": {}},
            "report": {"status": "running"},
        },
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "delivery_resume"
    assert checkpoint["files"]["war_report"] == report_path


def test_report_save_in_progress_without_report_does_not_resume_delivery(monkeypatch, tmp_path):
    p6_path = _write(tmp_path / f"P6_映射_{RUN_TS}.md", _valid_p6_markdown())
    p8_path = _write(tmp_path / f"P8_技术面_{RUN_TS}.md", "saved p8")
    manifest = _p6_checkpoint_manifest(tmp_path, "completed", p6_path=p6_path)
    manifest["status"] = "running"
    manifest["stages"]["p8"] = {"status": "completed", "files": {"md": p8_path}}
    manifest["stages"]["p9"] = {"status": "report_save_in_progress", "metrics": {}}
    manifest["stages"]["report"] = {"status": "running"}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert "war_report" not in checkpoint["files"]


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

    assert checkpoint["stage"] == "none"
    assert not checkpoint["files"].get("war_report")


@pytest.mark.parametrize("p2_status", ["pending", "failed_empty", "failed_timeout", "auth_failed_401"])
def test_incomplete_p2_without_artifact_uses_targeted_resume(monkeypatch, tmp_path, p2_status):
    manifest = _p2_checkpoint_manifest(tmp_path, p2_status)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"


def test_zero_byte_failed_p2_is_not_a_usable_artifact(monkeypatch, tmp_path):
    p2_path = tmp_path / f"P2_X平台情报_{RUN_TS}.md"
    p2_path.touch()
    manifest = _p2_checkpoint_manifest(tmp_path, "failed_empty", p2_path=str(p2_path))

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"


def test_p2_completed_no_results_does_not_loop_back_to_p2(monkeypatch, tmp_path):
    manifest = _p2_checkpoint_manifest(tmp_path, "completed_no_results")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p5"
    assert checkpoint["stage_assessments"]["p2"]["state"] == "valid_empty"


@pytest.mark.parametrize("p2_status", ["skipped_no_key", "completed"])
def test_p2_without_usable_markdown_triggers_targeted_resume(monkeypatch, tmp_path, p2_status):
    manifest = _p2_checkpoint_manifest(tmp_path, p2_status)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"


def test_failed_p2_with_existing_artifact_still_triggers_targeted_resume(monkeypatch, tmp_path):
    p2_path = _write(tmp_path / f"P2_X平台情报_{RUN_TS}.md", "saved p2")
    manifest = _p2_checkpoint_manifest(tmp_path, "failed_timeout", p2_path=p2_path)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"
    assert "p2" not in checkpoint["files"]


def test_p2_targeted_resume_precedes_downstream_checkpoint(monkeypatch, tmp_path):
    manifest = _p2_checkpoint_manifest(tmp_path, "pending")
    p8_path = _write(tmp_path / f"P8_技术面_{RUN_TS}.md", "saved p8")
    manifest["stages"]["p8"] = {"status": "completed", "files": {"md": p8_path}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"


def test_p2_targeted_resume_precedes_terminal_delivery_and_roi(monkeypatch, tmp_path):
    p2_stages = _p2_checkpoint_manifest(tmp_path, "pending")["stages"]
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "saved report")
    cases = [
        (_manifest(report_path), "p2_resume"),
        (
            _manifest(
                report_path,
                root_status="failed_exception",
                p9_status="completed_email_failed",
                report_status="failed_exception",
                roi_status=None,
            ),
            "p2_resume",
        ),
        (
            _manifest(report_path, root_status="failed_exception", roi_status=None),
            "p2_resume",
        ),
    ]

    for manifest, expected_stage in cases:
        manifest["stages"].update(p2_stages)
        checkpoint = _detect(monkeypatch, tmp_path, manifest)
        assert checkpoint["stage"] == expected_stage


def test_corrupt_manifest_falls_back_to_latest_single_artifact_cohort(monkeypatch, tmp_path):
    old_ts = "20260710070000"
    latest_ts = "20260710090000"
    _write(tmp_path / f"P2_X平台情报_{old_ts}.md", "old p2")
    latest_p1 = _write(tmp_path / f"P1_RSS原始数据_{latest_ts}.json", P1_RECORD_JSON)
    latest_p3 = _write(tmp_path / f"P3_机动情报_{latest_ts}.md", "latest p3")
    _write(tmp_path / f"P5_审计_{latest_ts}.md", "latest p5")
    corrupt_manifest = {
        "run_timestamp": "20260710110000",
        "updated_at": "2026-07-10T11:00:00",
        "corrupt_manifest": True,
        "stages": {},
    }

    checkpoint = _detect(monkeypatch, tmp_path, corrupt_manifest)

    assert checkpoint["run_timestamp"] == latest_ts
    assert checkpoint["stage"] == "p2_resume"
    assert checkpoint["files"]["p1"] == latest_p1
    assert checkpoint["files"]["p3"] == latest_p3
    assert "p5" not in checkpoint["files"]
    assert "p2" not in checkpoint["files"]


def test_manifest_without_valid_root_timestamp_cannot_mix_stage_cohorts(monkeypatch, tmp_path):
    old_ts = "20260710070000"
    latest_ts = "20260710090000"
    old_p2 = _write(tmp_path / f"P2_X平台情报_{old_ts}.md", "old p2")
    latest_p1 = _write(tmp_path / f"P1_RSS原始数据_{latest_ts}.json", P1_RECORD_JSON)
    latest_p3 = _write(tmp_path / f"P3_机动情报_{latest_ts}.md", "latest p3")
    latest_p5 = _write(tmp_path / f"P5_审计_{latest_ts}.md", "latest p5")
    manifest = {
        "run_timestamp": "",
        "status": "failed_exception",
        "stages": {
            "p1": {"status": "completed", "files": {"json": latest_p1}},
            "p2": {"status": "completed", "files": {"md": old_p2}},
            "p3": {"status": "completed", "files": {"md": latest_p3}},
            "p5": {"status": "completed", "files": {"md": latest_p5}},
        },
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["run_timestamp"] == latest_ts
    assert checkpoint["stage"] == "p2_resume"
    assert checkpoint["files"]["p1"] == latest_p1
    assert checkpoint["files"]["p3"] == latest_p3
    assert "p5" not in checkpoint["files"]
    assert "p2" not in checkpoint["files"]


@pytest.mark.parametrize(
    "p6_status",
    ["pending", "running", "failed_empty_output", "failed_missing_input", "failed_parse"],
)
def test_incomplete_p6_without_artifact_uses_targeted_resume(monkeypatch, tmp_path, p6_status):
    manifest = _p6_checkpoint_manifest(tmp_path, p6_status)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_zero_byte_failed_p6_is_not_a_usable_artifact(monkeypatch, tmp_path):
    p6_path = tmp_path / f"P6_映射_{RUN_TS}.md"
    p6_path.touch()
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_empty_output", p6_path=str(p6_path))

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_failed_p6_with_existing_artifact_still_uses_targeted_resume(monkeypatch, tmp_path):
    p6_path = _write(tmp_path / f"P6_映射_{RUN_TS}.md", _valid_p6_markdown())
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_parse", p6_path=p6_path)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_completed_p6_with_current_nonempty_markdown_keeps_normal_resume(monkeypatch, tmp_path):
    p6_path = _write(tmp_path / f"P6_映射_{RUN_TS}.md", _valid_p6_markdown())
    manifest = _p6_checkpoint_manifest(tmp_path, "completed", p6_path=p6_path)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"


def test_completed_p6_with_json_only_still_uses_targeted_resume(monkeypatch, tmp_path):
    p6_json = _write(tmp_path / f"P6_映射_{RUN_TS}.json", "{}")
    manifest = _p6_checkpoint_manifest(tmp_path, "completed")
    manifest["stages"]["p6"]["files"] = {"json": p6_json}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_old_same_day_p6_fallback_does_not_revive_current_failure(monkeypatch, tmp_path):
    _write(tmp_path / "P6_映射_20260710070000.md", "old p6")
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_empty_output")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_p6_targeted_resume_precedes_stale_p8_checkpoint(monkeypatch, tmp_path):
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_empty_output")
    p8_path = _write(tmp_path / f"P8_技术面_{RUN_TS}.md", "stale p8")
    manifest["stages"]["p8"] = {"status": "completed", "files": {"md": p8_path}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_failed_p6_without_p5_reuses_available_p2_p3_and_resumes_p6(monkeypatch, tmp_path):
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_empty_output")
    Path(manifest["stages"]["p1"]["files"]["json"]).unlink()
    manifest["stages"]["p1"] = {"status": "skipped_no_results", "metrics": {}}
    Path(manifest["stages"]["p5"]["files"]["md"]).unlink()
    manifest["stages"].pop("p5")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert checkpoint["files"]["p2"]
    assert checkpoint["files"]["p3"]
    assert "p5" not in checkpoint["files"]


def test_p2_targeted_resume_still_precedes_p6_targeted_resume(monkeypatch, tmp_path):
    manifest = _p6_checkpoint_manifest(tmp_path, "failed_empty_output")
    Path(manifest["stages"]["p2"]["files"]["md"]).unlink()
    manifest["stages"]["p2"] = {"status": "pending", "metrics": {}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"


@pytest.mark.parametrize(("failed_stage", "file_key"), [("p1", "json"), ("p3", "md")])
def test_failed_p1_or_p3_uses_selective_intel_resume(
    monkeypatch,
    tmp_path,
    failed_stage,
    file_key,
):
    manifest = _p6_checkpoint_manifest(tmp_path, "completed")
    failed_file = manifest["stages"][failed_stage].get("files", {}).get(file_key, "")
    if failed_file:
        Path(failed_file).unlink()
    manifest["stages"][failed_stage] = {"status": "failed_exception", "metrics": {}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == [failed_stage]


def test_multiple_missing_intel_branches_resume_together(monkeypatch, tmp_path):
    manifest = _p2_before_p5_manifest(tmp_path, "failed_timeout")
    p3_file = manifest["stages"]["p3"]["files"]["md"]
    Path(p3_file).unlink()
    manifest["stages"]["p3"] = {"status": "failed_all_queries", "metrics": {}}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == ["p2", "p3"]


@pytest.mark.parametrize("p2_status", [None, "pending", "running", "retrying", "failed_timeout"])
def test_p2_interrupted_before_p5_still_uses_targeted_resume(monkeypatch, tmp_path, p2_status):
    checkpoint = _detect(monkeypatch, tmp_path, _p2_before_p5_manifest(tmp_path, p2_status))

    assert checkpoint["stage"] == "p2_resume"
    assert "p5" not in checkpoint["files"]


def test_old_same_day_p2_fallback_does_not_hide_current_p2_failure(monkeypatch, tmp_path):
    _write(tmp_path / "P2_X平台情报_20260710070000.md", "old p2")
    manifest = _p2_checkpoint_manifest(tmp_path, "pending")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"
    assert "p2" not in checkpoint["files"]


def test_manifest_rejects_stage_files_from_an_older_run(monkeypatch, tmp_path):
    old_ts = "20260710070000"
    old_report = _write(tmp_path / f"战报_{old_ts}.md", "old report")
    manifest = _p2_checkpoint_manifest(tmp_path, "completed")
    manifest["stages"]["p9"] = {
        "status": "completed_email_sent",
        "run_timestamp": old_ts,
        "files": {"md": old_report},
    }
    manifest["stages"]["report"] = {
        "status": "completed",
        "run_timestamp": old_ts,
    }
    manifest["stages"]["roi"] = {
        "status": "completed",
        "run_timestamp": old_ts,
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"
    assert "war_report" not in checkpoint["files"]


def test_manifestless_fallback_chooses_one_latest_run_cohort(monkeypatch, tmp_path):
    old_ts = "20260710070000"
    new_ts = "20260710090000"
    _write(tmp_path / f"P5_审计_{old_ts}.md", "old p5")
    _write(tmp_path / f"P6_映射_{old_ts}.md", "old p6")
    new_p1 = _write(tmp_path / f"P1_RSS原始数据_{new_ts}.json", P1_RECORD_JSON)

    checkpoint = _detect(monkeypatch, tmp_path, {})

    assert checkpoint["run_timestamp"] == new_ts
    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == ["p2", "p3"]
    assert checkpoint["files"] == {"p1": new_p1}


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
    p6 = _write(tmp_path / f"P6_mapping_{RUN_TS}.md", _valid_p6_markdown(), mtime=3000)
    expected_hash = hashlib.sha256(Path(p6).read_bytes()).hexdigest()
    p7 = _write(tmp_path / f"P7_research_{RUN_TS}.md", "p7", mtime=4000)
    p7a = _write(tmp_path / f"P7a_audit_{RUN_TS}.md", "p7a", mtime=4100)
    p75 = _write(tmp_path / f"P7.5_audit_{RUN_TS}.md", "p75", mtime=4200)
    manifest = {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p6": {
                "status": "completed",
                "files": {"md": p6},
                "metrics": {
                    "accepted_targets": 5,
                    "stock_count": 5,
                    "identity_validation_version": 2,
                },
            },
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
        lambda content, report_ts, **_kwargs: deliveries.append((content, report_ts))
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
    assert registry["market_mapping"].timeout_seconds == 3900
    assert registry["research"].timeout_seconds == 900
    assert registry["report"].timeout_seconds == 3900
    assert registry["research"].continue_on_error is True
    assert registry["technical"].continue_on_error is True
    assert registry["market_mapping"].continue_on_error is False
    assert registry["report"].continue_on_error is False
    assert "timeout=180" in inspect.getsource(search_engine._post_search_request)


def test_opus_models_have_independent_900_second_budgets_with_stage_cleanup_margin():
    import orchestrator
    import settings

    market_mapping_timeout = orchestrator.CONTROL_STAGE_TIMEOUTS["market_mapping"]
    report_timeout = orchestrator.CONTROL_STAGE_TIMEOUTS["report"]

    assert settings.OPUS_MODEL_TIMEOUT_SECONDS == 900
    assert settings.OPUS_TOTAL_TIMEOUT_SECONDS == 900
    assert settings.OPUS_FALLBACK_TOTAL_TIMEOUT_SECONDS == 900
    assert settings.opus_adapter.timeout == 900
    assert settings.opus_adapter.extra_kwargs["total_timeout_seconds"] == 900
    assert settings.opus_adapter.extra_kwargs["fallback_total_timeout_seconds"] == 900
    assert settings.opus_adapter.base_url.rstrip("/") == settings.PRO_OPUS_BASE_URL.rstrip("/")
    assert "fallback_base_url" not in settings.opus_adapter.extra_kwargs
    assert "fallback_api_key" not in settings.opus_adapter.extra_kwargs
    combined_model_budget = (
        settings.OPUS_TOTAL_TIMEOUT_SECONDS
        + settings.OPUS_FALLBACK_TOTAL_TIMEOUT_SECONDS
    )
    maximum_json_attempt_budget = combined_model_budget * 2
    assert maximum_json_attempt_budget <= market_mapping_timeout - 300
    assert maximum_json_attempt_budget <= report_timeout - 300


def test_audit_primary_and_fallback_have_bounded_budgets_inside_p5_stage():
    import orchestrator
    import settings

    assert settings.AUDIT_PRIMARY_REQUEST_TIMEOUT_SECONDS == 240
    assert settings.AUDIT_PRIMARY_TOTAL_TIMEOUT_SECONDS == 240
    assert settings.AUDIT_FALLBACK_REQUEST_TIMEOUT_SECONDS == 180
    assert settings.AUDIT_FALLBACK_TOTAL_TIMEOUT_SECONDS == 180
    assert settings.pro_adapter.timeout == 240
    assert settings.pro_adapter.extra_kwargs["total_timeout_seconds"] == 240
    assert "timeout" in settings.pro_adapter.extra_kwargs["failover_categories"]

    maximum_json_retry_budget = 2 * (
        settings.AUDIT_PRIMARY_TOTAL_TIMEOUT_SECONDS
        + settings.AUDIT_FALLBACK_TOTAL_TIMEOUT_SECONDS
    ) + 5
    assert maximum_json_retry_budget <= orchestrator.CONTROL_STAGE_TIMEOUTS["audit_evidence"] - 30


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
    orchestrator._stage_report(context)

    assert captured["research_names"] == []
    assert captured["report_names"] == []
