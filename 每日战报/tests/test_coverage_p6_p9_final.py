from __future__ import annotations

import json
import logging
from types import SimpleNamespace


def _patch_p6_prompt_runtime(monkeypatch, p6_mapper) -> None:
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(
        p6_mapper,
        "get_model_provenance",
        lambda *_args: {
            "requested_model": "gpt-5.6-sol",
            "effective_model": "gpt-5.6-sol",
            "fallback_used": False,
        },
    )


def _empty_p9_payload() -> dict:
    return {
        "x_intel_snipe": [],
        "p3_machine_intel": [],
        "core_signals": [],
        "sniper_tables": [],
        "fund_pathway": [],
        "today_advice": [],
        "risk_warning": [],
    }


def test_p6_tdx_filter_handles_non_list_events_and_malformed_nested_values(monkeypatch):
    import p6_mapper

    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(p6_mapper, "resolve_tdx_root", lambda: "D:/TDX")

    non_list = {"events": "not-a-list"}
    metrics = p6_mapper._apply_tdx_stock_codes(non_list)

    assert non_list["events"] == []
    assert metrics["accepted_targets"] == 0
    assert metrics["rejected_events"] == 0

    malformed = {
        "events": [
            "not-an-event",
            {"track_name": "bad-target-container", "targets": "not-a-list"},
            {"track_name": "bad-targets", "targets": ["not-a-target", {"name": "", "code": ""}]},
        ],
        "optional_searches": "not-a-list",
        "deep_research": ["   "],
    }
    metrics = p6_mapper._apply_tdx_stock_codes(malformed)

    assert malformed["events"] == []
    assert malformed["optional_searches"] == []
    assert malformed["deep_research"] == []
    assert metrics["rejected_events"] == 3
    assert metrics["rejected_targets"] == 2
    assert metrics["rejected_target_names"] == []


def test_p6_format_evidence_skips_blank_items_but_keeps_valid_metadata():
    import p6_mapper

    assert p6_mapper.format_evidence([]) == ""

    result = p6_mapper.format_evidence(
        [
            {"category": "blank", "query": "ignored", "text": "   "},
            {"category": "存储", "query": "source-a", "text": "  有效证据  "},
        ]
    )

    assert "blank" not in result
    assert "### 分类: 存储 | 来源: source-a" in result
    assert "有效证据" in result


def test_p6_generate_report_records_missing_prompt_with_recent_history(monkeypatch, caplog):
    import p6_mapper

    updates = []
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "近期主题")
    monkeypatch.setattr(
        p6_mapper,
        "require_prompt",
        lambda _name: (_ for _ in ()).throw(FileNotFoundError("p6 prompt missing")),
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    caplog.set_level(logging.ERROR)

    assert p6_mapper.generate_report("证据") == ""
    assert updates == [
        (
            ("p6",),
            {
                "status": "failed_missing_prompt",
                "metrics": {"input_chars": 2, "error": "p6 prompt missing"},
            },
        )
    ]
    assert "关键 Prompt 缺失" in caplog.text


def test_p6_empty_output_survives_reasoning_write_failure(monkeypatch, caplog):
    import p6_mapper

    updates = []
    _patch_p6_prompt_runtime(monkeypatch, p6_mapper)
    monkeypatch.setattr(
        p6_mapper.opus_adapter,
        "invoke",
        lambda *_args, **_kwargs: SimpleNamespace(content="", thinking_content="reasoning"),
    )
    monkeypatch.setattr(
        p6_mapper,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    caplog.set_level(logging.WARNING)

    assert p6_mapper.generate_report("input") == ""
    assert updates[-1][1]["status"] == "failed_empty_output"
    assert updates[-1][1]["files"] is None
    assert updates[-1][1]["metrics"]["input_chars"] == 5
    assert "推理链保存失败: disk full" in caplog.text


def test_p6_rendering_skips_bad_rows_and_keeps_reasoning_when_json_write_fails(monkeypatch):
    import p6_mapper

    updates = []
    valid_targets = [
        {
            "name": f"公司{i}",
            "code": f"60000{i}",
            "elasticity": "中",
            "reasoning": "产业证据与订单证据均已核验。" * 20,
        }
        for i in range(1, 6)
    ]
    payload = {
        "events": [
            "not-an-event",
            {
                "track_name": "完整赛道",
                "event_summary": "",
                "targets": [
                    "not-a-target",
                    *valid_targets,
                    {
                        "name": "观察项",
                        "code": "not-code",
                        "elasticity": "低",
                        "reasoning": "仅观察，不计入六位证券代码集合。" * 20,
                    },
                ],
            },
        ],
        "optional_searches": ["   ", "有效搜索"],
        "deep_research": ["", "有效深研"],
    }
    _patch_p6_prompt_runtime(monkeypatch, p6_mapper)
    monkeypatch.setattr(
        p6_mapper.opus_adapter,
        "invoke",
        lambda *_args, **_kwargs: SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="reasoning",
        ),
    )
    monkeypatch.setattr(
        p6_mapper,
        "_apply_tdx_stock_codes",
        lambda _data: {"accepted_targets": 6, "rejected_targets": 0, "rejected_events": 0},
    )
    monkeypatch.setattr(
        p6_mapper,
        "write_text_artifact",
        lambda *_args, **_kwargs: "thinking.md",
    )
    monkeypatch.setattr(
        p6_mapper,
        "write_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("json disk full")),
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    report = p6_mapper.generate_report("input")

    assert len(report) >= 1000
    assert "公司1（600001）" in report
    assert "观察项（not-code）" in report
    assert "SEARCH: 有效搜索" in report
    assert "DEEP_RESEARCH: 有效深研" in report
    assert "SEARCH:    " not in report
    assert updates[-1][1]["status"] == "json_ready"
    assert updates[-1][1]["files"] == {"thinking": "thinking.md"}
    assert updates[-1][1]["metrics"]["stock_count"] == 5


def test_p6_incomplete_retry_keeps_reasoning_when_json_artifact_is_unavailable(monkeypatch):
    import p6_mapper

    updates = []
    invoke_configs = []
    payload = {"events": [], "optional_searches": [], "deep_research": []}
    _patch_p6_prompt_runtime(monkeypatch, p6_mapper)

    def fake_invoke(_prompt, config=None):
        invoke_configs.append(config)
        return SimpleNamespace(
            content=json.dumps(payload),
            thinking_content="reasoning",
        )

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(
        p6_mapper,
        "_apply_tdx_stock_codes",
        lambda _data: {"accepted_targets": 0, "rejected_targets": 0, "rejected_events": 0},
    )
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda *_args, **_kwargs: "thinking.md")
    monkeypatch.setattr(
        p6_mapper,
        "write_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("json disk full")),
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    assert p6_mapper.generate_report("input", _retry_count=1) == ""
    assert invoke_configs == [
        {
            "force_fallback": True,
            "fallback_reason": "P6 previous output failed JSON or completeness validation",
        }
    ]
    assert updates[-1][1]["status"] == "failed_incomplete_output"
    assert updates[-1][1]["files"] == {"thinking": "thinking.md"}
    assert set(updates[-1][1]["metrics"]["incomplete_reasons"]) == {
        "no_valid_event_targets",
        "report_too_short",
        "too_few_rendered_targets",
    }


def test_p6_terminal_parse_failure_survives_invalid_raw_write_failure(monkeypatch, caplog):
    import p6_mapper

    updates = []
    invoke_configs = []
    _patch_p6_prompt_runtime(monkeypatch, p6_mapper)

    def fake_invoke(_prompt, config=None):
        invoke_configs.append(config)
        return SimpleNamespace(content="not-json", thinking_content="")

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(
        p6_mapper,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("raw disk full")),
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    caplog.set_level(logging.WARNING)

    assert p6_mapper.generate_report("input", _retry_count=p6_mapper.P6_JSON_RETRY_LIMIT) == ""
    assert invoke_configs[0]["force_fallback"] is True
    assert updates[-1][1]["status"] == "failed_parse"
    assert updates[-1][1]["files"] is None
    assert "invalid raw output save failed: raw disk full" in caplog.text


def test_p9_semantic_sanitizer_handles_null_blank_and_scalar_objects():
    import p9_reporter

    assert p9_reporter._is_semantic_placeholder(None) is True
    assert p9_reporter._is_semantic_placeholder("   ") is True

    data = _empty_p9_payload()
    data["sniper_tables"] = ["not-a-table"]
    data["today_advice"] = ["not-advice"]

    metrics = p9_reporter._sanitize_semantic_empty_report_payload(data)

    assert data["sniper_tables"] == []
    assert data["today_advice"] == []
    assert metrics == {
        "semantic_placeholder_items_removed": 2,
        "semantic_empty_sniper_tables_removed": 0,
    }


def test_p9_persisted_metrics_returns_empty_for_read_error_and_non_mapping(monkeypatch, caplog):
    import p9_reporter

    monkeypatch.setattr(
        p9_reporter,
        "load_run_manifest",
        lambda: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )
    caplog.set_level(logging.WARNING)
    assert p9_reporter._load_persisted_report_outcome_metrics() == {}
    assert "manifest unavailable" in caplog.text

    monkeypatch.setattr(
        p9_reporter,
        "load_run_manifest",
        lambda: {"stages": {"p9": {"metrics": ["not", "a", "mapping"]}}},
    )
    assert p9_reporter._load_persisted_report_outcome_metrics() == {}


def test_p9_soft_intel_backfill_leaves_placeholder_when_source_has_no_cards():
    import p9_reporter

    placeholder = [{"title": "本次运行无P2", "body": "未收到P2"}]
    data = {"x_intel_snipe": placeholder.copy(), "p3_machine_intel": []}

    metrics = p9_reporter._backfill_soft_intel_sections(
        data,
        p2_x_intel_data="没有结构化卡片的长文本" * 10,
        p3_intel_data=None,
    )

    assert metrics == {"p2_intel_section_backfilled": 0, "p3_intel_section_backfilled": 0}
    assert data["x_intel_snipe"] == placeholder


def test_p9_identity_filter_removes_residual_mentions_and_ignores_empty_tables():
    import p9_reporter

    data = {
        **_empty_p9_payload(),
        "sniper_tables": [{"track_name": "empty", "targets": []}],
        "residual_note": "虚构电子相关结论必须删除",
    }

    metrics = p9_reporter._filter_rejected_identity_payload(data, ["虚构电子"])

    assert data["sniper_tables"] == []
    assert data["residual_note"] == ""
    assert metrics["rejected_identity_items_removed"] == 1


def test_p9_p8_backfill_does_not_count_identical_price_as_change():
    import p9_reporter

    row = {"name": "已同步", "code": "000001", "price": "10.00"}
    data = {"sniper_tables": [{"track_name": "一致数据", "targets": [row]}]}
    p8_db = {"000001": {"latest_close": 10}}

    metrics = p9_reporter._backfill_sniper_tables_from_p8(data, p8_db)

    assert row == {"name": "已同步", "code": "000001", "price": "10.00"}
    assert metrics == {"p8_backfilled_targets": 0, "p8_price_db_size": 1}


def test_p9_priority_downgrade_is_safe_when_rank_table_has_no_allowed_fallback(monkeypatch):
    import p9_reporter

    monkeypatch.setattr(p9_reporter, "_PRIORITY_RANKS", {"custom-high": 4.0})

    assert p9_reporter._downgrade_priority("custom-high", 0.82) == "custom-high"


def test_p9_risk_analysis_records_successful_soft_intel_backfill(monkeypatch, caplog):
    import p9_reporter

    payload = _empty_p9_payload()
    payload["x_intel_snipe"] = [
        {
            "title": "本次运行无P2 X平台情报",
            "body": "本轮未收到P2 X平台实时情报数据。",
        }
    ]
    p2_markdown = """### 1. [HBM] HBM4 订单跟踪
- Claim: HBM4 订单进入验证阶段。
- Impact: 关注设备与材料链验证节奏。
- Source: 官方公告摘要
- Confidence: 70%
- Source URL: https://example.com/hbm4"""
    updates = []
    written_payloads = []
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715090000")
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
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: {})
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(
        p9_reporter,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: written_payloads.append(data) or "report.json",
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    caplog.set_level(logging.INFO)

    report, run_ts = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 evidence",
        p7_deep="P7 evidence",
        p75_supplement="P7.5 evidence",
        p2_x_intel_data=p2_markdown,
        price_data="有效技术面价格数据" * 10,
    )

    assert run_ts == "20260715090000"
    assert "HBM4 订单跟踪" in report
    assert written_payloads[0]["x_intel_snipe"][0]["title"] == "HBM4 订单跟踪"
    json_ready = next(kwargs for args, kwargs in updates if args == ("p9",) and kwargs["status"] == "json_ready")
    assert json_ready["metrics"]["p2_intel_section_backfilled"] == 1
    assert "backfilled soft intel sections" in caplog.text


def test_p9_required_email_without_credentials_fails_after_local_save(monkeypatch):
    import p9_reporter

    updates = []
    smtp_calls = []
    monkeypatch.setattr(p9_reporter, "normalize_common_text_mojibake", lambda value: value)
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260715090000")
    monkeypatch.setattr(p9_reporter, "_load_persisted_report_outcome_metrics", lambda: {})
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "report.md")
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")
    monkeypatch.setattr(
        p9_reporter,
        "send_report_via_email",
        lambda *_args, **_kwargs: smtp_calls.append(True),
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    result = p9_reporter.send_report_email("report", email_required=True)

    assert result == {
        "ok": False,
        "status": "failed_delivery_config",
        "report_path": "report.md",
        "email_sent": False,
    }
    assert smtp_calls == []
    assert [kwargs["status"] for args, kwargs in updates if args == ("p9",)] == [
        "report_save_in_progress",
        "report_saved_pending_delivery",
        "failed_delivery_config",
    ]
