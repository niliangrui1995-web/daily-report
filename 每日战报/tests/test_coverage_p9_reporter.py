from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest


def test_p9_small_normalizers_and_layer_helpers_cover_edge_cases():
    import p9_reporter

    assert p9_reporter._section_or_fallback(" short ", "fallback") == "fallback"
    assert p9_reporter._section_or_fallback("x" * 51, "fallback") == "x" * 51
    assert p9_reporter._is_placeholder_intel_section(None, ("missing",)) is True
    assert p9_reporter._is_placeholder_intel_section(["valid"], ("missing",)) is False
    assert p9_reporter._is_placeholder_intel_section([{"title": "missing", "body": ""}], ("missing",)) is True
    assert p9_reporter._extract_field_from_markdown_block("- Claim: value", "Claim") == "value"
    assert p9_reporter._extract_field_from_markdown_block("- Other: value", "Claim") == ""
    assert p9_reporter._normalize_stock_code("SH.600000 suffix") == "600000"
    assert p9_reporter._normalize_stock_code(None) == ""

    assert p9_reporter._coerce_float(None) is None
    assert p9_reporter._coerce_float("1,234.5%") == 1234.5
    assert p9_reporter._coerce_float("bad") is None
    assert p9_reporter._is_missing_field(None) is True
    assert p9_reporter._is_missing_field("real") is False
    assert p9_reporter._format_price("bad") is None
    assert p9_reporter._format_signed_pct("bad") is None
    assert p9_reporter._format_percentile("bad") is None
    assert p9_reporter._format_price(1.2) == "1.20"
    assert p9_reporter._format_signed_pct(-1.25) == "-1.2%"
    assert p9_reporter._format_percentile(49.6) == "50%"

    assert p9_reporter._normalize_layer_tag(None) is None
    assert p9_reporter._normalize_layer_tag("P7.5:source") == "P7.5"
    assert p9_reporter._normalize_layer_tag("P75-source") == "P7.5"
    assert p9_reporter._normalize_layer_tag("P7A:source") == "P7A"
    assert p9_reporter._normalize_layer_tag("P6-source") == "P6"
    assert p9_reporter._normalize_layer_tag("unknown") is None
    assert p9_reporter._extract_evidence_layers(None) == []
    assert p9_reporter._extract_evidence_layers("P6") == ["P6"]
    assert p9_reporter._extract_evidence_layers(["P6", "P6", "bad", "P8"]) == ["P6", "P8"]

    assert p9_reporter._normalize_confidence_value(None) is None
    assert p9_reporter._normalize_confidence_value("80%") == 0.8
    assert p9_reporter._normalize_confidence_value(80) == 0.8
    assert p9_reporter._normalize_confidence_value(2.0) == 0.02
    assert p9_reporter._normalize_confidence_value(-1) == 0.0
    assert p9_reporter._normalize_confidence_value("bad") is None
    assert p9_reporter._confidence_cap_for_layers([]) == 0.55
    assert p9_reporter._confidence_cap_for_layers(["P2"]) == 0.60
    assert p9_reporter._confidence_cap_for_layers(["P2", "P3"]) == 0.68
    assert p9_reporter._confidence_cap_for_layers(["P6", "P2"]) == 0.82
    assert p9_reporter._confidence_cap_for_layers(["P6", "P8"]) == 0.88


def test_extract_soft_intel_cards_skips_empty_and_limits_results():
    import p9_reporter

    assert p9_reporter._extract_soft_intel_cards(None) == []
    section = """intro padding that makes this source section long enough for parsing
### 1. [tag] Empty card
- Other: no body
### 2. [tag] First card
- Claim: claim one
- Impact: impact one
- Source: source one
- Confidence: 70%
- Source URL: https://example.com/1
### 3. Second card
- Claim: claim two
### 4. Third card
- Claim: claim three
"""
    cards = p9_reporter._extract_soft_intel_cards(section, max_items=2)
    assert [card["title"] for card in cards] == ["First card", "Second card"]
    assert "claim one" in cards[0]["body"]
    assert "source one" in cards[0]["body"]


def test_soft_intel_backfill_preserves_valid_sections_and_handles_empty_cards():
    import p9_reporter

    data = {
        "x_intel_snipe": [{"title": "valid", "body": "body"}],
        "p3_machine_intel": [],
    }
    metrics = p9_reporter._backfill_soft_intel_sections(
        data,
        p2_x_intel_data="x" * 60,
        p3_intel_data="y" * 60,
    )
    assert metrics == {"p2_intel_section_backfilled": 0, "p3_intel_section_backfilled": 0}
    assert data["x_intel_snipe"][0]["title"] == "valid"


def test_apply_tdx_codes_rejects_invalid_unmapped_and_legacy_rows(monkeypatch):
    import p9_reporter

    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"Known": "600000"})
    monkeypatch.setattr(
        p9_reporter,
        "canonicalize_stock_aliases_in_tree",
        lambda data: (copy.deepcopy(data), 0),
    )
    monkeypatch.setattr(
        p9_reporter,
        "_lookup_tdx_stock_code",
        lambda stock_code_map, name: stock_code_map.get(str(name), ""),
    )
    data = {
        "sniper_tables": [
            "invalid-table",
            {"track_name": "empty", "targets": ["invalid-row"]},
            {
                "track_name": "mixed",
                "targets": [
                    {"name": "Known", "code": ""},
                    {"name": "Unknown", "code": "123456"},
                    {"name": "Unknown2", "code": p9_reporter.UNMAPPED_CODE_PLACEHOLDER},
                ],
            },
        ],
        "sniper_table": [
            {"name": "Known", "code": "600000"},
            {"name": "Unknown", "code": "999999"},
        ],
        "today_advice": ["invalid", {"name": "Known"}, {"name": "Unknown"}],
    }
    metrics = p9_reporter._apply_tdx_stock_codes_to_report(data)
    assert len(data["sniper_tables"]) == 1
    assert data["sniper_tables"][0]["targets"] == [{"name": "Known", "code": "600000"}]
    assert data["sniper_table"] == [{"name": "Known", "code": "600000"}]
    assert data["today_advice"] == [{"name": "Known", "code": "600000"}]
    assert metrics["p9_rejected_targets"] >= 5
    assert metrics["p9_unmapped_codes"] >= 3


def test_filter_rejected_identity_payload_covers_sections_tables_and_advice(monkeypatch):
    import p9_reporter

    monkeypatch.setattr(p9_reporter, "normalize_rejected_stock_names", lambda names: set(names or []))

    def fake_filter_tree(item, rejected):
        if item == "drop" or (isinstance(item, dict) and item.get("drop")):
            return None, 1
        if isinstance(item, dict) and item.get("empty_body"):
            return {"title": "empty", "body": ""}, 1
        return item, 0

    monkeypatch.setattr(p9_reporter, "filter_rejected_stock_tree", fake_filter_tree)
    monkeypatch.setattr(
        p9_reporter,
        "find_rejected_stock_names",
        lambda value, rejected: bool(
            (isinstance(value, str) and value in rejected)
            or (isinstance(value, dict) and value.get("contains_rejected"))
        ),
    )
    monkeypatch.setattr(p9_reporter, "normalize_stock_name", lambda value: str(value))
    data = {
        "x_intel_snipe": ["keep", "drop"],
        "p3_machine_intel": ["keep"],
        "core_signals": [{"empty_body": True}, {"body": "valid"}],
        "fund_pathway": [],
        "risk_warning": [],
        "sniper_tables": [
            "invalid",
            {"track_name": "Rejected", "targets": [{"name": "keep"}]},
            {
                "track_name": "valid",
                "targets": [
                    {"name": "Rejected"},
                    {"name": "keep", "contains_rejected": True},
                    {"name": "good"},
                ],
            },
            {"track_name": "all-removed", "targets": [{"name": "Rejected"}]},
        ],
        "today_advice": [
            {"name": "Rejected"},
            {"name": "keep", "contains_rejected": True},
            {"name": "good"},
        ],
    }
    metrics = p9_reporter._filter_rejected_identity_payload(data, ["Rejected"])
    assert data["x_intel_snipe"] == ["keep"]
    assert data["core_signals"] == [{"body": "valid"}]
    assert data["sniper_tables"] == [
        {"track_name": "valid", "targets": [{"name": "good"}]}
    ]
    assert data["today_advice"] == [{"name": "good"}]
    assert metrics["rejected_identity_items_removed"] >= 8

    assert p9_reporter._filter_rejected_identity_payload({}, []) == {
        "rejected_identity_items_removed": 0
    }


def test_load_and_extract_p8_fields_cover_missing_invalid_and_partial_data(tmp_path, monkeypatch):
    import p9_reporter

    monkeypatch.setattr(p9_reporter, "load_run_manifest", lambda: {"stages": {"p8": {"files": {}}}})
    assert p9_reporter._load_p8_price_db() == {}

    payload_path = tmp_path / "p8.json"
    payload_path.write_text(json.dumps([1, 2, 3]), encoding=p9_reporter.UTF8_FILE_ENCODING)
    monkeypatch.setattr(
        p9_reporter,
        "load_run_manifest",
        lambda: {"stages": {"p8": {"files": {"json": str(payload_path)}}}},
    )
    assert p9_reporter._load_p8_price_db() == {}
    payload_path.write_text("not-json", encoding=p9_reporter.UTF8_FILE_ENCODING)
    assert p9_reporter._load_p8_price_db() == {}

    fields = p9_reporter._extract_p8_fields(
        {
            "latest_close": 10,
            "chg_3m_pct": 5,
            "percentile_250d": 70,
            "tags": {"vol_desc": "normal"},
            "relative_strength": {"rs_excess_20d": 1.2},
            "weekly_level": {"weekly_trend": "up"},
        }
    )
    assert fields["price"] == "10.00"
    assert fields["rs_strength"]
    assert fields["weekly_level"] == "up"
    assert p9_reporter._extract_p8_fields({}) == {}


def test_backfill_sniper_tables_skips_invalid_rows_and_preserves_nonmissing_soft_fields():
    import p9_reporter

    data = {
        "sniper_tables": [
            "invalid",
            {
                "targets": [
                    "invalid-row",
                    {"code": "000001", "volume": "keep"},
                    {"code": "999999"},
                ]
            },
        ]
    }
    p8_db = {
        "000001": {
            "latest_close": 11,
            "tags": {"vol_desc": "replace-attempt"},
            "relative_strength": {"rs_excess_20d": 1, "rs_excess_60d": 2},
        },
        "bad": "not-a-dict",
    }
    metrics = p9_reporter._backfill_sniper_tables_from_p8(data, p8_db)
    row = data["sniper_tables"][1]["targets"][1]
    assert row["price"] == "11.00"
    assert row["volume"] == "keep"
    assert metrics == {"p8_backfilled_targets": 1, "p8_price_db_size": 2}
    assert p9_reporter._backfill_sniper_tables_from_p8({}, {}) == {
        "p8_backfilled_targets": 0,
        "p8_price_db_size": 0,
    }


def test_evidence_guardrails_ignore_scalars_invalid_confidence_and_already_safe_values():
    import p9_reporter

    data = {
        "core_signals": [
            "scalar",
            {"confidence": "bad", "significance": "normal", "evidence_refs": []},
            {"confidence": 0.4, "significance": None, "evidence_refs": ["P6", "P8"]},
        ],
        "today_advice": [
            "scalar",
            {"confidence": "bad", "priority": "unknown", "evidence_refs": []},
            {"confidence": 0.5, "priority": None, "evidence_refs": ["P6", "P8"]},
        ],
    }
    guarded, metrics = p9_reporter._apply_evidence_guardrails(data)
    assert guarded is data
    assert metrics == {
        "confidence_caps_applied": 0,
        "priority_downgrades": 0,
        "significance_downgrades": 0,
    }


def _patch_p9_pipeline_dependencies(monkeypatch, p9_reporter, *, content="", thinking=""):
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: "{today_str}|{p5_evidence}|{p7_section}|{p6_mapping}|{p75_section}|{price_data}|{p2_section}|{p3_section}|{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda _prompt, _config=None: SimpleNamespace(content=content, thinking_content=thinking),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: {})
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_common_mojibake_artifacts", lambda value: value)
    monkeypatch.setattr(p9_reporter, "normalize_rejected_stock_names", lambda names: set(names or []))
    monkeypatch.setattr(p9_reporter, "filter_rejected_stock_lines", lambda text, _names: (text, 0))
    monkeypatch.setattr(p9_reporter, "build_rejected_stock_guardrail", lambda _names: "")


def test_p9_risk_analysis_handles_missing_prompt_empty_output_and_reasoning_write_failure(monkeypatch):
    import p9_reporter

    updates = []
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260714090000")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: (_ for _ in ()).throw(FileNotFoundError("missing prompt")),
    )
    report, run_ts = p9_reporter.p9_risk_analysis("", "", "")
    assert (report, run_ts) == ("", "20260714090000")
    assert updates[-1][1]["status"] == "failed_missing_prompt"

    _patch_p9_pipeline_dependencies(monkeypatch, p9_reporter, content="", thinking="reasoning")
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    report, _ = p9_reporter.p9_risk_analysis("p5", "p7", "p75")
    assert report == ""
    assert updates[-1][1]["status"] == "failed_empty_output"


def test_p9_risk_analysis_covers_json_write_failure_and_terminal_parse_failure(monkeypatch):
    import p9_reporter

    updates = []
    valid = {
        "x_intel_snipe": [],
        "p3_machine_intel": [],
        "core_signals": [],
        "sniper_tables": [],
        "fund_pathway": [],
        "today_advice": [],
        "risk_warning": [],
    }
    _patch_p9_pipeline_dependencies(monkeypatch, p9_reporter, content=json.dumps(valid), thinking="reasoning")
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260714090000")
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "thinking.md")
    monkeypatch.setattr(
        p9_reporter,
        "write_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("json write failed")),
    )
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(
        p9_reporter,
        "canonicalize_stock_aliases_in_tree",
        lambda data: (copy.deepcopy(data), 0),
    )
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    report, _ = p9_reporter.p9_risk_analysis("p5", "p7", "p75", price_data="x" * 60)
    assert report
    assert updates[-1][1]["status"] == "json_ready"
    assert updates[-1][1]["files"] == {"thinking": "thinking.md"}

    _patch_p9_pipeline_dependencies(monkeypatch, p9_reporter, content="not-json")
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("raw write failed")),
    )
    report, _ = p9_reporter.p9_risk_analysis(
        "p5",
        "p7",
        "p75",
        _retry_count=p9_reporter.P9_JSON_RETRY_LIMIT,
    )
    assert report == ""
    assert updates[-1][1]["status"] == "failed_parse"
    assert updates[-1][1]["files"] is None


def test_send_report_email_covers_empty_write_configured_and_delivery_outcomes(monkeypatch):
    import p9_reporter

    updates = []
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    assert p9_reporter.send_report_email("   ")["status"] == "failed_empty_report"

    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260714090000")
    monkeypatch.setattr(
        p9_reporter,
        "_load_persisted_report_outcome_metrics",
        lambda: {
            "schema_validation": {"status": "not_run"},
            "model_generation": {"status": "failed"},
            "delivery": {"status": "ready_degraded", "mode": "local_checkpoint_fallback"},
        },
    )
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert p9_reporter.send_report_email("report")["status"] == "failed_md_write"
    failed_write_metrics = updates[-1][1]["metrics"]
    assert failed_write_metrics["schema_validation"]["status"] == "not_run"
    assert failed_write_metrics["model_generation"]["status"] == "failed"
    assert failed_write_metrics["delivery"] == {
        "status": "failed_md_write",
        "mode": "local_checkpoint_fallback",
    }

    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "report.md")
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")
    no_email = p9_reporter.send_report_email("report")
    assert no_email == {
        "ok": True,
        "status": "completed_no_email",
        "report_path": "report.md",
        "email_sent": False,
    }

    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "receiver@example.com")
    monkeypatch.setattr(p9_reporter, "send_report_via_email", lambda *_args, **_kwargs: None)
    sent = p9_reporter.send_report_email("report")
    assert sent["status"] == "completed_email_sent"
    assert sent["email_sent"] is True

    monkeypatch.setattr(
        p9_reporter,
        "send_report_via_email",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("smtp down")),
    )
    failed = p9_reporter.send_report_email("report")
    assert failed["status"] == "completed_email_failed"
    assert failed["email_sent"] is False


def test_send_report_email_persists_saved_checkpoint_before_smtp(monkeypatch):
    import p9_reporter

    events = []
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715090000")
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda *_args, **_kwargs: events.append(("write", "report.md")) or "report.md",
    )
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "receiver@example.com")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda stage, **kwargs: events.append(("manifest", stage, kwargs)),
    )
    monkeypatch.setattr(
        p9_reporter,
        "send_report_via_email",
        lambda *_args, **_kwargs: events.append(("smtp",))
        or (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        p9_reporter.send_report_email("saved report")

    assert events == [
        (
            "manifest",
            "p9",
            {
                "status": "report_save_in_progress",
                "metrics": {"email_configured": True, "email_sent": False},
            },
        ),
        ("write", "report.md"),
        (
            "manifest",
            "p9",
            {
                "status": "report_saved_pending_delivery",
                "files": {"md": "report.md"},
                "metrics": {"email_configured": True, "email_sent": False},
            },
        ),
        ("smtp",),
    ]


def test_send_report_email_preserves_local_only_resume_policy(monkeypatch):
    import p9_reporter

    updates = []
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "receiver@example.com")
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715090000")
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "report.md")
    monkeypatch.setattr(
        p9_reporter,
        "send_report_via_email",
        lambda *_args, **_kwargs: pytest.fail("local-only resume must not send email"),
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    result = p9_reporter.send_report_email("saved report", email_required=False)

    assert result["status"] == "completed_no_email"
    assert updates[-1][1]["metrics"]["email_configured"] is False
