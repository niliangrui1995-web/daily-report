from __future__ import annotations

import hashlib
from pathlib import Path


RUN_TS = "20260715133000"
REPORT_REQUIRED_HEADINGS_FOR_TEST = (
    "## 一、核心信号",
    "## 二、标的狙击表",
    "## 三、资金路径推演",
    "## 四、今日操作建议",
    "## ⚠️ 风险提示",
)


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _p6_hash_metrics(manifest: dict) -> dict[str, str]:
    return {
        "p6_input_sha256": _sha256(manifest["stages"]["p6"]["files"]["md"])
    }


def _valid_report() -> str:
    return "\n".join(
        [
            "# VCP Hunter P9 战报 | 2026-07-15",
            "## 一、核心信号",
            "今日无明显核心信号。",
            "## 二、标的狙击表",
            "| 无符合条件的标的 |",
            "## 三、资金路径推演",
            "推演路径不明确。",
            "## 四、今日操作建议",
            "| 暂无建议 |",
            "## ⚠️ 风险提示",
            "暂无明显风险。",
        ]
    )


def _base_manifest(tmp_path: Path) -> dict:
    p1 = _write(tmp_path / f"P1_raw_{RUN_TS}.json", '[{"text":"evidence"}]')
    p2 = _write(
        tmp_path / f"P2_x_{RUN_TS}.md",
        "### 1. [HBM] supply update\n- Claim: HBM supply remains tight",
    )
    p3 = _write(
        tmp_path / f"P3_intel_{RUN_TS}.md",
        "### 1. [P3 | HBM] supply update\n- Claim: substrate lead times expanded",
    )
    p4 = _write(tmp_path / f"P4_market_{RUN_TS}.md", "- ARM: 收盘价 100.0")
    p5 = _write(
        tmp_path / f"P5_audit_{RUN_TS}.md",
        "## 审计摘要\n通过: 1条 / 删除: 0条\n\n## 高可信情报\n1. usable evidence",
    )
    p6_targets = "\n".join(
        f"- 测试公司{i}（60000{i}） | 高弹性 | " + ("reason " * 35)
        for i in range(5)
    )
    p6 = _write(
        tmp_path / f"P6_mapping_{RUN_TS}.md",
        "### 赛道: 测试\n> 事件触发: usable event\n" + p6_targets,
    )
    return {
        "manifest_version": 3,
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p1": {"status": "completed", "files": {"json": p1}},
            "p2": {
                "status": "completed",
                "files": {"md": p2},
                "metrics": {
                    "successful_chunks": 1,
                    "content_length": 60,
                    "structured_items": 1,
                },
            },
            "p3": {
                "status": "completed",
                "files": {"md": p3},
                "metrics": {"structured_items": 1, "content_chars": 70},
            },
            "p4": {
                "status": "completed",
                "files": {"md": p4},
                "metrics": {"usable_tickers": 1, "no_data_tickers": 0, "failed_tickers": 0},
            },
            "p5": {
                "status": "completed",
                "files": {"md": p5},
                "metrics": {"passed_count": 1, "deleted_count": 0},
            },
            "p6": {
                "status": "completed",
                "files": {"md": p6},
                "metrics": {
                    "accepted_targets": 5,
                    "stock_count": 5,
                    "valid_events": 1,
                    "valid_targets": 5,
                    "identity_validation_version": 2,
                },
            },
            "market_mapping": {"status": "completed"},
        },
    }


def _add_valid_p8(tmp_path: Path, manifest: dict) -> str:
    stock_rows = "\n".join(
        f"### 测试公司{i}（60000{i}）\n- 收盘 {10 + i}.00 元"
        for i in range(5)
    )
    p8 = _write(
        tmp_path / f"P8_technical_{RUN_TS}.md",
        f"# P8 technical\n\n{stock_rows}",
    )
    manifest["stages"]["p8"] = {
        "status": "completed",
        "files": {"md": p8},
        "metrics": {
            "stocks_requested": 5,
            "stocks_with_data": 5,
            "data_state": "valid",
        },
    }
    return p8


def _add_terminal_artifacts(tmp_path: Path, manifest: dict) -> None:
    report = _write(tmp_path / f"war_report_{RUN_TS}.md", _valid_report())
    roi_md = _write(tmp_path / f"Layer_ROI_{RUN_TS}.md", "# Layer ROI\n\n- evaluated: 4")
    roi_json = _write(
        tmp_path / f"Layer_ROI_{RUN_TS}.json",
        '{"layers":{"p1":{},"p3":{},"p4":{},"p75":{}}}',
    )
    manifest["status"] = "completed"
    manifest["stages"].update(
        {
            "p9": {
                "status": "completed_email_sent",
                "updated_at": "2026-07-15T10:05:00",
                "files": {"md": report},
            },
            "report": {"status": "completed", "updated_at": "2026-07-15T10:05:01"},
            "roi": {
                "status": "completed",
                "updated_at": "2026-07-15T10:06:00",
                "files": {"md": roi_md, "json": roi_json},
            },
        }
    )


def _detect(monkeypatch, tmp_path: Path, manifest: dict) -> dict:
    import checkpoint

    monkeypatch.setattr(checkpoint, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(checkpoint, "load_run_manifest", lambda: manifest)
    return checkpoint.detect_checkpoint()


def test_p1_empty_json_is_not_a_reusable_success(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    Path(manifest["stages"]["p1"]["files"]["json"]).write_text("[]", encoding="utf-8")

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == ["p1"]


def test_p1_sentinel_json_is_not_a_reusable_success(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    Path(manifest["stages"]["p1"]["files"]["json"]).write_text(
        '[{"text":"暂无相关证据。"}]',
        encoding="utf-8",
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == ["p1"]


def test_p2_empty_x_shell_is_not_reusable(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    Path(manifest["stages"]["p2"]["files"]["md"]).write_text(
        "# P2 X intelligence\n\n<x_leaks></x_leaks>",
        encoding="utf-8",
    )
    manifest["stages"]["p2"]["metrics"]["structured_items"] = 0

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p2_resume"
    assert checkpoint["intel_resume_stages"] == ["p2"]


def test_p3_nonempty_shell_with_declared_item_is_not_reusable(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    Path(manifest["stages"]["p3"]["files"]["md"]).write_text(
        "# P3 结构化机动情报\n\n采集时间: 2026-07-15",
        encoding="utf-8",
    )
    manifest["stages"]["p3"]["metrics"] = {
        "structured_items": 1,
        "content_chars": 30,
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "intel_resume"
    assert checkpoint["intel_resume_stages"] == ["p3"]


def test_valid_empty_p1_with_completed_p2_p3_does_not_restart_intel(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p1"] = {"status": "skipped_no_results", "metrics": {}}
    for stage_name in ("p4", "p5", "p6", "market_mapping"):
        manifest["stages"].pop(stage_name, None)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p5"
    assert checkpoint["intel_resume_stages"] == []


def test_p4_all_placeholder_quotes_invalidates_p6_and_downstream(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p4_path = manifest["stages"]["p4"]["files"]["md"]
    Path(p4_path).write_text("# P4 market\n- ARM: 获取股价失败", encoding="utf-8")
    manifest["stages"]["p4"]["metrics"] = {
        "usable_tickers": 1,
        "no_data_tickers": 0,
        "failed_tickers": 0,
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert "p4" not in checkpoint["files"]
    assert "p6" not in checkpoint["files"]


def test_p5_zero_pass_zero_delete_shell_uses_p5_resume(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p5_path = manifest["stages"]["p5"]["files"]["md"]
    Path(p5_path).write_text(
        "## 审计摘要\n通过: 0条 / 删除: 0条\n\n## 高可信情报\n无",
        encoding="utf-8",
    )
    manifest["stages"]["p5"]["metrics"] = {"passed_count": 1, "deleted_count": 0}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p5_resume"
    assert "p5" not in checkpoint["files"]
    assert "p6" not in checkpoint["files"]


def test_p6_incomplete_nonempty_mapping_uses_p6_resume(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p6_path = manifest["stages"]["p6"]["files"]["md"]
    Path(p6_path).write_text("SEARCH: only a follow-up query", encoding="utf-8")
    manifest["stages"]["p6"]["metrics"] = {"accepted_targets": 0, "stock_count": 0}

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert "p6" not in checkpoint["files"]


def test_p6_duplicate_codes_do_not_satisfy_target_threshold(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    repeated_targets = "\n".join(
        "- 重复公司（600000） | 高弹性 | " + ("reason " * 35)
        for _ in range(5)
    )
    Path(manifest["stages"]["p6"]["files"]["md"]).write_text(
        "### 赛道: 测试\n> 事件触发: usable event\n" + repeated_targets,
        encoding="utf-8",
    )
    manifest["stages"]["p6"]["metrics"].update(
        {"accepted_targets": 5, "stock_count": 5}
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert checkpoint["stage_assessments"]["p6"]["reusable"] is False


def test_p6_outdated_identity_validation_is_not_reusable(monkeypatch, tmp_path):
    import checkpoint as checkpoint_module

    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p6"]["metrics"]["identity_validation_version"] = 1
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)

    assessment = checkpoint_module.assess_stage_checkpoint(
        "p6",
        manifest,
        {"p6": manifest["stages"]["p6"]["files"]["md"]},
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert assessment["reason"] == "identity_validation_missing_or_outdated"


def test_p7a_short_input_is_a_reusable_passthrough(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p6_hash = _sha256(manifest["stages"]["p6"]["files"]["md"])
    p7 = _write(
        tmp_path / f"P7_research_{RUN_TS}.md",
        "# P7 research\n\nshort but meaningful research",
    )
    manifest["stages"]["p7"] = {
        "status": "completed",
        "files": {"md": p7},
        "metrics": {"p6_input_sha256": p6_hash},
    }
    manifest["stages"]["p7a"] = {
        "status": "skipped_short_input",
        "metrics": {
            "input_chars": 80,
            "data_state": "valid_passthrough",
            "p6_input_sha256": p6_hash,
        },
    }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert checkpoint["stage_assessments"]["p7a"]["state"] == "valid_passthrough"


def test_p75_failed_audit_reuses_saved_raw_passthrough(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p6_hash = _sha256(manifest["stages"]["p6"]["files"]["md"])
    raw = _write(
        tmp_path / f"P7.5_raw_{RUN_TS}.md",
        "raw supplement evidence retained after audit failure",
    )
    manifest["stages"]["p75"] = {
        "status": "failed_semantic_output",
        "files": {"raw": raw},
        "metrics": {"p6_input_sha256": p6_hash},
    }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert checkpoint["stage_assessments"]["p75"]["state"] == "valid_passthrough"
    assert checkpoint["files"]["p75_raw"] == raw


def test_current_manifest_valid_empty_research_requires_p6_hash(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "metrics": {},
    }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert checkpoint["stage_assessments"]["p7"]["reason"] == "missing_p6_input_hash"


def test_legacy_p6_without_identity_validation_is_not_reusable(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["manifest_version"] = 2
    manifest["stages"]["p6"]["metrics"].pop("identity_validation_version")
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"


def test_valid_empty_research_with_stale_p6_hash_is_rerun(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "metrics": {"p6_input_sha256": "0" * 64},
    }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert checkpoint["stage_assessments"]["p7"]["reusable"] is False
    assert checkpoint["stage_assessments"]["p7"]["reason"] == "stale_p6_input_hash"
    assert "p8" not in checkpoint["files"]


def test_p7_no_evidence_placeholder_invalidates_p8(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p7 = _write(
        tmp_path / f"P7_research_{RUN_TS}.md",
        "# P7 深度研究结果\n\n> generated_at: now\n\nnot found",
    )
    manifest["stages"]["p7"] = {"status": "completed", "files": {"md": p7}}
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert "p7" not in checkpoint["files"]
    assert "p8" not in checkpoint["files"]


def test_p7a_json_only_is_not_loaded_as_markdown(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p7a = _write(tmp_path / f"P7a_audit_{RUN_TS}.json", '{"markdown":""}')
    manifest["stages"]["p7a"] = {"status": "completed", "files": {"json": p7a}}
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6"
    assert "p7a" not in checkpoint["files"]
    assert checkpoint["files"]["p8"] == manifest["stages"]["p8"]["files"]["md"]


def test_p75_explicit_no_results_does_not_force_retry(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p75"] = {
        "status": "skipped_no_results",
        "metrics": _p6_hash_metrics(manifest),
    }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert checkpoint["stage_assessments"]["p75"]["state"] == "valid_empty"


def test_untriggered_research_layers_with_current_p6_hash_do_not_loop(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    current_hash = _sha256(manifest["stages"]["p6"]["files"]["md"])
    for stage_name in ("p7", "p7a", "p75"):
        manifest["stages"][stage_name] = {
            "status": "skipped_not_triggered",
            "metrics": {
                "data_state": "valid_empty",
                "p6_input_sha256": current_hash,
            },
        }
    _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert {
        stage_name: checkpoint["stage_assessments"][stage_name]["state"]
        for stage_name in ("p7", "p7a", "p75")
    } == {"p7": "valid_empty", "p7a": "valid_empty", "p75": "valid_empty"}


def test_valid_empty_research_drops_stale_file_but_keeps_p8(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    stale_p7 = _write(
        tmp_path / f"P7_research_{RUN_TS}.md",
        "# stale P7\n\nold research must not be reused",
    )
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "files": {"md": stale_p7},
        "metrics": _p6_hash_metrics(manifest),
    }
    p8 = _add_valid_p8(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert "p7" not in checkpoint["files"]
    assert checkpoint["files"]["p8"] == p8


def test_valid_empty_research_before_p8_resumes_at_p8(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "metrics": _p6_hash_metrics(manifest),
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["stage_assessments"]["p7"]["state"] == "valid_empty"


def test_valid_empty_research_keeps_newer_terminal_artifacts(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "updated_at": "2026-07-15T10:00:00",
        "metrics": _p6_hash_metrics(manifest),
    }
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_done"
    assert "war_report" in checkpoint["files"]
    assert "roi_json" in checkpoint["files"]


def test_valid_empty_intel_keeps_newer_terminal_artifacts(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p1"].update(
        {"status": "skipped_no_results", "updated_at": "2000-01-01T00:00:00"}
    )
    manifest["stages"]["p3"].update(
        {"status": "completed_no_results", "updated_at": "2000-01-01T00:00:01"}
    )
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_done"
    assert "p1" not in checkpoint["files"]
    assert "p3" not in checkpoint["files"]
    assert "war_report" in checkpoint["files"]


def test_valid_empty_research_keeps_newer_report_for_delivery_resume(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "updated_at": "2026-07-15T10:00:00",
        "metrics": _p6_hash_metrics(manifest),
    }
    manifest["stages"]["p75"] = {
        "status": "completed_no_results",
        "updated_at": "2026-07-15T10:00:01",
        "metrics": _p6_hash_metrics(manifest),
    }
    _add_valid_p8(tmp_path, manifest)
    report = _write(tmp_path / f"war_report_{RUN_TS}.md", _valid_report())
    manifest["stages"]["p9"] = {
        "status": "completed_email_failed",
        "updated_at": "2026-07-15T10:05:00",
        "files": {"md": report},
        "metrics": {"email_configured": True, "email_sent": False},
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "delivery_resume"
    assert checkpoint["files"]["war_report"] == report


def test_valid_empty_research_keeps_newer_report_for_roi_resume(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "updated_at": "2026-07-15T10:00:00",
        "metrics": _p6_hash_metrics(manifest),
    }
    _add_valid_p8(tmp_path, manifest)
    report = _write(tmp_path / f"war_report_{RUN_TS}.md", _valid_report())
    manifest["stages"].update(
        {
            "p9": {
                "status": "completed_email_sent",
                "updated_at": "2026-07-15T10:05:00",
                "files": {"md": report},
            },
            "report": {"status": "completed", "updated_at": "2026-07-15T10:05:01"},
        }
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"
    assert checkpoint["files"]["war_report"] == report


def test_valid_empty_research_invalidates_older_terminal_artifacts(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    manifest["stages"]["p7"] = {
        "status": "skipped_no_results",
        "updated_at": "2026-07-15T10:10:00",
        "metrics": _p6_hash_metrics(manifest),
    }
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert "war_report" not in checkpoint["files"]
    assert "roi_json" not in checkpoint["files"]


def test_terminal_no_stocks_p8_is_invalid_when_p6_has_targets(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    p8 = _write(
        tmp_path / f"P8_technical_{RUN_TS}.md",
        "# P8 technical\n\n未从 P6 映射中提取到 A 股标的。",
    )
    manifest["stages"]["p8"] = {
        "status": "skipped_no_stocks",
        "files": {"md": p8},
        "metrics": {"stocks_requested": 0, "stocks_with_data": 0},
    }
    _add_terminal_artifacts(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reason"] == "stocks_now_available"
    assert "war_report" not in checkpoint["files"]


def test_incomplete_p9_markdown_is_not_terminal(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    report = _write(tmp_path / f"war_report_{RUN_TS}.md", "# VCP Hunter P9 战报")
    manifest["status"] = "completed"
    manifest["stages"].update(
        {
            "p9": {"status": "completed_email_sent", "files": {"md": report}},
            "report": {"status": "completed"},
            "roi": {"status": "completed"},
        }
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert "war_report" not in checkpoint["files"]


def test_completed_roi_without_artifacts_uses_roi_resume(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    report = _write(tmp_path / f"war_report_{RUN_TS}.md", _valid_report())
    manifest["status"] = "completed"
    manifest["stages"].update(
        {
            "p9": {"status": "completed_email_sent", "files": {"md": report}},
            "report": {"status": "completed"},
            "roi": {"status": "completed"},
        }
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"
    assert checkpoint["stage_assessments"]["roi"]["reusable"] is False


def test_p9_required_headings_without_section_bodies_are_not_reusable(
    monkeypatch,
    tmp_path,
):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)
    report_path = manifest["stages"]["p9"]["files"]["md"]
    Path(report_path).write_text(
        "\n".join(["# VCP Hunter P9 战报", *REPORT_REQUIRED_HEADINGS_FOR_TEST]),
        encoding="utf-8",
    )
    manifest["stages"]["p9"]["metrics"] = {
        "report_outcome": "completed_no_actionable_signals",
        "actionable_items_count": 0,
    }

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert checkpoint["stage_assessments"]["p9"]["reusable"] is False


def test_new_p1_output_invalidates_older_p5_and_resumes_at_p5(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)
    manifest["stages"]["p1"]["output_updated_at"] = "2026-07-15T10:10:00"
    manifest["stages"]["p5"]["output_updated_at"] = "2026-07-15T10:05:00"
    manifest["stages"]["p6"]["output_updated_at"] = "2026-07-15T10:06:00"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p5_resume"
    assert "p6" not in checkpoint["files"]


def test_new_p2_output_invalidates_older_p6_and_resumes_at_p6(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    manifest["stages"]["p2"]["output_updated_at"] = "2026-07-15T10:10:00"
    manifest["stages"]["p6"]["output_updated_at"] = "2026-07-15T10:05:00"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert "p6" not in checkpoint["files"]


def test_new_p8_output_invalidates_older_p9_even_when_file_mtime_is_preserved(
    monkeypatch,
    tmp_path,
):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)
    manifest["stages"]["p8"]["output_updated_at"] = "2026-07-15T10:10:00"
    manifest["stages"]["p9"]["output_updated_at"] = "2026-07-15T10:05:00"
    manifest["stages"]["roi"]["output_updated_at"] = "2026-07-15T10:06:00"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert "war_report" not in checkpoint["files"]
    assert "roi_json" not in checkpoint["files"]


def test_roi_older_than_current_report_is_rebuilt(monkeypatch, tmp_path):
    manifest = _base_manifest(tmp_path)
    _add_valid_p8(tmp_path, manifest)
    _add_terminal_artifacts(tmp_path, manifest)
    manifest["stages"]["p9"]["output_updated_at"] = "2026-07-15T10:10:00"
    manifest["stages"]["roi"]["output_updated_at"] = "2026-07-15T10:05:00"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "roi_resume"
    assert checkpoint["stage_assessments"]["roi"]["reason"] == "stale_p9_input"


def test_v3_terminal_state_without_p6_and_p8_cannot_enter_downstream_resume(
    monkeypatch,
    tmp_path,
):
    manifest = _base_manifest(tmp_path)
    manifest["stages"].pop("p6")
    manifest["stages"].pop("market_mapping")
    _add_terminal_artifacts(tmp_path, manifest)

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p5"
    assert checkpoint["stage"] not in {"p9_done", "delivery_resume", "roi_resume"}


def test_roi_bookkeeping_timestamp_does_not_invalidate_valid_empty_outputs(
    monkeypatch,
    tmp_path,
):
    manifest = _base_manifest(tmp_path)
    p6_hash = _sha256(manifest["stages"]["p6"]["files"]["md"])
    for stage_name in ("p1", "p3"):
        manifest["stages"][stage_name] = {
            "status": "completed_no_results",
            "output_updated_at": "2026-07-15T10:00:00",
            "updated_at": "2026-07-15T10:20:00",
            "metrics": {"roi_score": 0.0},
        }
    manifest["stages"]["p2"]["output_updated_at"] = "2026-07-15T10:00:00"
    manifest["stages"]["p4"]["output_updated_at"] = "2026-07-15T10:00:00"
    manifest["stages"]["p5"]["output_updated_at"] = "2026-07-15T10:00:00"
    manifest["stages"]["p6"]["output_updated_at"] = "2026-07-15T10:01:00"
    manifest["stages"]["p75"] = {
        "status": "completed_no_results",
        "output_updated_at": "2026-07-15T10:02:00",
        "updated_at": "2026-07-15T10:20:00",
        "metrics": {"p6_input_sha256": p6_hash, "roi_score": 0.0},
    }
    _add_valid_p8(tmp_path, manifest)
    manifest["stages"]["p8"]["output_updated_at"] = "2026-07-15T10:03:00"
    _add_terminal_artifacts(tmp_path, manifest)
    manifest["stages"]["p9"]["output_updated_at"] = "2026-07-15T10:04:00"
    manifest["stages"]["roi"]["output_updated_at"] = "2026-07-15T10:05:00"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_done"
    assert "war_report" in checkpoint["files"]
    assert "roi_json" in checkpoint["files"]
