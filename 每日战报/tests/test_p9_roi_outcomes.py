from __future__ import annotations

import json
from types import SimpleNamespace


def _empty_report_payload() -> dict:
    return {
        "x_intel_snipe": [],
        "p3_machine_intel": [],
        "core_signals": [],
        "sniper_tables": [],
        "fund_pathway": [],
        "today_advice": [],
        "risk_warning": [],
    }


def test_report_outcome_metrics_distinguish_legal_empty_from_actionable():
    import p9_reporter

    empty_metrics = p9_reporter._report_outcome_metrics(_empty_report_payload())
    assert empty_metrics == {
        "report_outcome": "completed_no_actionable_signals",
        "signals_count": 0,
        "sniper_tables_count": 0,
        "targets_count": 0,
        "advice_count": 0,
        "fund_pathways_count": 0,
        "risk_warnings_count": 0,
        "x_intel_count": 0,
        "p3_intel_count": 0,
        "actionable_items_count": 0,
        "nonempty_sections_count": 0,
    }

    actionable = _empty_report_payload()
    actionable["core_signals"] = [{"title": "signal"}]
    actionable["sniper_tables"] = [
        {"track_name": "track", "targets": [{"name": "A"}, {"name": "B"}]}
    ]
    actionable["today_advice"] = [{"name": "A"}]
    actionable["risk_warning"] = ["risk"]

    metrics = p9_reporter._report_outcome_metrics(actionable)
    assert metrics["report_outcome"] == "actionable"
    assert metrics["signals_count"] == 1
    assert metrics["targets_count"] == 2
    assert metrics["advice_count"] == 1
    assert metrics["risk_warnings_count"] == 1
    assert metrics["actionable_items_count"] == 4
    assert metrics["nonempty_sections_count"] == 4


def test_empty_structured_report_is_legal_and_records_no_actionable_outcome(monkeypatch):
    import p9_reporter

    updates = []
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715083000")
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: (
            "{today_str}{p5_evidence}{p7_section}{p6_mapping}{p75_section}"
            "{price_data}{p2_section}{p3_section}{yfinance_section}"
        ),
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda *_args, **_kwargs: SimpleNamespace(
            content=json.dumps(_empty_report_payload()),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: {})
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_common_mojibake_artifacts", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_rejected_stock_names", lambda _names: set())
    monkeypatch.setattr(p9_reporter, "filter_rejected_stock_lines", lambda text, _names: (text, 0))
    monkeypatch.setattr(p9_reporter, "build_rejected_stock_guardrail", lambda _names: "")
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(p9_reporter, "write_json_artifact", lambda *_args, **_kwargs: "report.json")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis("", "", "")

    assert report.strip()
    json_ready = next(payload for stage, payload in updates if stage == "p9" and payload["status"] == "json_ready")
    assert json_ready["metrics"]["report_outcome"] == "completed_no_actionable_signals"
    assert json_ready["metrics"]["signals_count"] == 0
    assert json_ready["metrics"]["targets_count"] == 0
    assert json_ready["metrics"]["advice_count"] == 0


def test_structured_no_evidence_placeholders_are_cleaned_before_outcome(monkeypatch):
    import p9_reporter

    sentinel_payload = {
        "x_intel_snipe": [{"title": "暂无", "body": "未找到相关证据"}],
        "p3_machine_intel": [{"title": "无", "body": "N/A"}],
        "core_signals": [
            {
                "source": "none",
                "significance": "无",
                "title": "暂无",
                "body": "未找到相关证据",
                "confidence": "N/A",
                "evidence_refs": ["null"],
            }
        ],
        "sniper_tables": [
            {
                "track_name": "暂无",
                "targets": [
                    {
                        "name": "无",
                        "price": "N/A",
                        "chg_3m": "none",
                        "percentile_250d": "null",
                        "volume": "暂无",
                        "elasticity": "无",
                        "rs_strength": "N/A",
                        "weekly_level": "none",
                        "catalyst": "未找到相关证据",
                        "risk": "暂无",
                    }
                ],
            }
        ],
        "fund_pathway": ["N/A"],
        "today_advice": [
            {
                "priority": "无",
                "name": "暂无",
                "reason": "未找到相关证据",
                "strategy": "none",
                "confidence": "null",
                "evidence_refs": ["N/A"],
            }
        ],
        "risk_warning": ["暂无"],
    }
    updates = []
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715083000")
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: (
            "{today_str}{p5_evidence}{p7_section}{p6_mapping}{p75_section}"
            "{price_data}{p2_section}{p3_section}{yfinance_section}"
        ),
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda *_args, **_kwargs: SimpleNamespace(
            content=json.dumps(sentinel_payload),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: {})
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_common_mojibake_artifacts", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_rejected_stock_names", lambda _names: set())
    monkeypatch.setattr(p9_reporter, "filter_rejected_stock_lines", lambda text, _names: (text, 0))
    monkeypatch.setattr(p9_reporter, "build_rejected_stock_guardrail", lambda _names: "")
    monkeypatch.setattr(p9_reporter, "_apply_tdx_stock_codes_to_report", lambda _data: {})
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(p9_reporter, "write_json_artifact", lambda *_args, **_kwargs: "report.json")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis("", "", "")

    assert report.strip()
    json_ready = next(payload for stage, payload in updates if stage == "p9" and payload["status"] == "json_ready")
    assert json_ready["metrics"]["report_outcome"] == "completed_no_actionable_signals"
    assert json_ready["metrics"]["actionable_items_count"] == 0
    assert json_ready["metrics"]["semantic_placeholder_items_removed"] == 7
    assert json_ready["metrics"]["semantic_empty_sniper_tables_removed"] == 1


def test_delivery_terminal_status_explicitly_carries_report_outcome_metrics(monkeypatch):
    import p9_reporter

    report_metrics = p9_reporter._report_outcome_metrics(_empty_report_payload())
    updates = []
    monkeypatch.setattr(
        p9_reporter,
        "load_run_manifest",
        lambda: {"stages": {"p9": {"metrics": report_metrics}}},
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "report.md")
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")

    result = p9_reporter.send_report_email("rendered report")

    assert result["status"] == "completed_no_email"
    terminal = updates[-1][1]
    assert terminal["status"] == "completed_no_email"
    assert terminal["metrics"]["report_outcome"] == "completed_no_actionable_signals"
    assert terminal["metrics"]["actionable_items_count"] == 0


def test_roi_completed_manifest_records_both_artifacts_and_structure_metrics(monkeypatch):
    import orchestrator

    roi_payload = {
        "layers": {
            "p1": {
                "units": 3,
                "unique_units": 2,
                "adopted_units": 1,
                "recommendation": "keep",
            },
            "p3": {
                "units": 0,
                "unique_units": 0,
                "adopted_units": 0,
                "recommendation": "drop",
            },
        }
    }
    updates = []
    monkeypatch.setattr(orchestrator, "build_layer_roi_report", lambda **_kwargs: roi_payload)
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda _payload: "# ROI")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "roi.md")
    monkeypatch.setattr(orchestrator, "load_run_manifest", lambda: {"stages": {}})
    monkeypatch.setattr(
        orchestrator,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    context = {
        "resume_from": "none",
        "ckpt": {"files": {}},
        "p1_results": [],
        "p3_stats": {},
        "p3_data": "",
        "p4_data": "",
        "p75_supplement": "",
        "p75_raw_text": "",
        "search_queries": [],
        "p6_mapping": "",
        "final_report": "# report",
        "p1_stats": {},
    }

    orchestrator._stage_roi(context)

    completed = next(payload for stage, payload in updates if stage == "roi")
    assert completed["status"] == "completed"
    assert completed["files"] == {"json": "roi.json", "md": "roi.md"}
    assert completed["metrics"] == {
        "layers_evaluated": 2,
        "layers_with_units": 1,
        "empty_layers": 1,
        "total_units": 3,
        "total_unique_units": 2,
        "total_adopted_units": 1,
    }


def test_roi_does_not_reuse_p75_raw_text_after_valid_empty_audit(monkeypatch):
    import orchestrator

    captured = {}

    def fake_build_layer_roi_report(**kwargs):
        captured.update(kwargs)
        return {"layers": {}}

    monkeypatch.setattr(orchestrator, "build_layer_roi_report", fake_build_layer_roi_report)
    monkeypatch.setattr(orchestrator, "render_layer_roi_markdown", lambda _payload: "# ROI")
    monkeypatch.setattr(orchestrator, "write_json_artifact", lambda *_args, **_kwargs: "roi.json")
    monkeypatch.setattr(orchestrator, "write_text_artifact", lambda *_args, **_kwargs: "roi.md")
    monkeypatch.setattr(
        orchestrator,
        "load_run_manifest",
        lambda: {"stages": {"p75": {"status": "completed_no_results"}}},
    )
    monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *_args, **_kwargs: None)
    context = {
        "resume_from": "none",
        "ckpt": {"files": {}},
        "p1_results": [],
        "p3_stats": {},
        "p3_data": "",
        "p4_data": "",
        "p75_supplement": "",
        "p75_raw_text": "raw evidence rejected by audit",
        "search_queries": ["query"],
        "p6_mapping": "mapping",
        "final_report": "# report",
        "p1_stats": {},
    }

    orchestrator._stage_roi(context)

    assert captured["p75_text"] == ""
    assert captured["extras"]["p75"]["valid_empty"] is True
