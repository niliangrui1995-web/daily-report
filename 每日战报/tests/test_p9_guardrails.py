# -*- coding: utf-8 -*-
"""Tests for P9 evidence guardrails."""

from __future__ import annotations

import copy
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_apply_evidence_guardrails_caps_single_and_soft_multi_source():
    import p9_reporter

    payload = {
        "core_signals": [
            {
                "source": "P2",
                "significance": "高优先级",
                "title": "单源传闻",
                "body": "只有一条 X 平台情报。",
                "confidence": 0.91,
                "evidence_refs": ["P2-1"],
            },
            {
                "source": "P7.5",
                "significance": "🟢高",
                "title": "软双源补充",
                "body": "来自补充搜索与边缘情报。",
                "confidence": "86%",
                "evidence_refs": ["P2:传闻", "P7.5:博客转述"],
            },
            {
                "source": "P6",
                "significance": "🟢高",
                "title": "硬双源确认",
                "body": "映射与技术面共振。",
                "confidence": 0.95,
                "evidence_refs": ["P6:映射", "P8:周线MACD金叉"],
            },
        ],
        "today_advice": [
            {
                "priority": "★★★",
                "name": "生益科技",
                "code": "600183",
                "reason": "单一映射线索。",
                "strategy": "低吸",
                "confidence": "76%",
                "evidence_refs": ["P6-1"],
            },
            {
                "priority": "★★★",
                "name": "某弹性股",
                "code": "300001",
                "reason": "软双源组合。",
                "strategy": "观察",
                "confidence": 0.83,
                "evidence_refs": ["P2:传闻", "P7.5:补充验证"],
            },
        ],
    }

    guarded, metrics = p9_reporter._apply_evidence_guardrails(copy.deepcopy(payload))

    assert guarded["core_signals"][0]["confidence"] == 0.60
    assert guarded["core_signals"][0]["significance"] == "🟡中"

    assert guarded["core_signals"][1]["confidence"] == 0.68
    assert guarded["core_signals"][1]["significance"] == "🟡中"

    assert guarded["core_signals"][2]["confidence"] == 0.88
    assert guarded["core_signals"][2]["significance"] == "🟢高"

    assert guarded["today_advice"][0]["confidence"] == 0.60
    assert guarded["today_advice"][0]["priority"] == "★★"

    assert guarded["today_advice"][1]["confidence"] == 0.68
    assert guarded["today_advice"][1]["priority"] == "★★☆"

    assert metrics["confidence_caps_applied"] == 5
    assert metrics["priority_downgrades"] == 2
    assert metrics["significance_downgrades"] == 2


def test_parse_report_payload_repairs_complete_partial_json():
    import p9_reporter

    raw = json.dumps(
        {
            "x_intel_snipe": [],
            "p3_machine_intel": [],
            "core_signals": [],
            "sniper_tables": [],
            "fund_pathway": [],
            "today_advice": [],
            "risk_warning": [],
        },
        ensure_ascii=False,
    )[:-1]

    payload = p9_reporter._parse_report_payload(raw)

    assert payload["today_advice"] == []


def test_parse_report_payload_rejects_incomplete_repaired_json():
    import pytest
    import p9_reporter

    raw = '```json\n{"core_signals": [], "sniper_tables": []'

    with pytest.raises(json.JSONDecodeError):
        p9_reporter._parse_report_payload(raw)


def test_p9_drops_only_target_missing_catalyst_and_records_schema_repair(monkeypatch):
    import p9_reporter

    payload = {
        "x_intel_snipe": [],
        "p3_machine_intel": [],
        "core_signals": [],
        "sniper_tables": [
            {
                "track_name": "高频覆铜板",
                "targets": [
                    {
                        "name": "生益科技",
                        "price": "125.99",
                        "chg_3m": "+61.5%",
                        "percentile_250d": "59%",
                        "volume": "正常",
                        "elasticity": "中军",
                        "rs_strength": "60日强势",
                        "weekly_level": "周线上行",
                        "catalyst": "高频覆铜板需求增长",
                        "risk": "波动风险",
                    },
                    {
                        "name": "南亚新材",
                        "price": "239.80",
                        "chg_3m": "+37.0%",
                        "percentile_250d": "53%",
                        "volume": "正常",
                        "elasticity": "中军",
                        "rs_strength": "60日强势",
                        "weekly_level": "周线上行",
                        "risk": "极端波动",
                    },
                ],
            }
        ],
        "fund_pathway": [],
        "today_advice": [],
        "risk_warning": [],
    }
    original = copy.deepcopy(payload)
    captured: dict[str, object] = {"updates": [], "invocations": 0}

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: "{today_str}{p5_evidence}{p7_section}{p6_mapping}{p75_section}{price_data}{p2_section}{p3_section}{yfinance_section}",
    )

    def fake_invoke(_prompt, _config=None):
        captured["invocations"] += 1
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), thinking_content="")

    monkeypatch.setattr(p9_reporter.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"生益科技": "600183"})
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "raw.md")
    monkeypatch.setattr(
        p9_reporter,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: captured.setdefault("json_payload", copy.deepcopy(data)) or "report.json",
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: captured["updates"].append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 audited evidence",
        p7_deep="P7 audited research",
        p75_supplement="P7.5 audited supplement",
        p6_mapping="P6 mapping",
        price_data="P8 technical data " * 5,
    )

    assert payload == original
    assert captured["invocations"] == 1
    assert "生益科技" in report
    assert "南亚新材" not in report
    assert [row["name"] for row in captured["json_payload"]["sniper_tables"][0]["targets"]] == [
        "生益科技"
    ]
    json_ready = captured["updates"][-1][1]
    assert json_ready["status"] == "json_ready"
    assert json_ready["metrics"]["schema_validation"] == {
        "status": "repaired",
        "repair_kind": "drop_invalid_target",
        "paths": ["$.sniper_tables[0].targets[1].catalyst"],
        "dropped_targets": 1,
        "dropped_empty_tables": 0,
        "revalidated": True,
    }
    assert json_ready["metrics"]["model_generation"]["status"] == "completed"
    assert json_ready["metrics"]["delivery"]["mode"] == "model_generated"
    assert json_ready["metrics"]["structured_degraded"] is True


def test_backfill_soft_intel_sections_replaces_false_missing_placeholders():
    import p9_reporter

    payload = {
        "x_intel_snipe": [
            {
                "title": "本次运行无P2 X平台情报",
                "body": "本轮未收到P2 X平台实时情报数据。",
            }
        ],
        "p3_machine_intel": [
            {
                "title": "本次运行无P3机动情报",
                "body": "本轮未收到P3 Gemini机动情报数据。",
            }
        ],
    }
    p2_markdown = """### 1. [HBM与先进存储] SK hynix HBM4订单
- Claim: SK hynix 下单 TC Bonder，HBM4 量产线启动。
- Source: @TradexWhisperer
- Confidence: 64% (medium)
- Impact: 利好 HBM 设备链。
- Source URL: https://x.com/example/status/1"""
    p3_markdown = """### 1. [锦囊二 | Tavily备用搜索] d-Matrix AI推理芯片
- Claim: d-Matrix 获融资并推出推理芯片。
- Source: Tavily
- Confidence: 56% (low)
- Source URL: https://www.d-matrix.ai/news"""

    metrics = p9_reporter._backfill_soft_intel_sections(
        payload,
        p2_x_intel_data=p2_markdown,
        p3_intel_data=p3_markdown,
    )

    assert metrics == {"p2_intel_section_backfilled": 1, "p3_intel_section_backfilled": 1}
    assert payload["x_intel_snipe"][0]["title"] == "SK hynix HBM4订单"
    assert "TC Bonder" in payload["x_intel_snipe"][0]["body"]
    assert payload["p3_machine_intel"][0]["title"] == "d-Matrix AI推理芯片"
    assert "Tavily" in payload["p3_machine_intel"][0]["body"]


def test_backfill_sniper_tables_from_p8_price_json():
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "track_name": "先进封装",
                "targets": [
                    {
                        "name": "华海清科",
                        "code": "688120",
                        "price": "未提供",
                        "chg_3m": "未提供",
                        "percentile_250d": "未提供",
                        "volume": "未提供",
                        "rs_strength": "未提供",
                        "weekly_level": "未提供",
                    }
                ],
            }
        ]
    }
    p8_db = {
        "688120": {
            "price_data": {
                "latest_close": 298.0,
                "chg_3m_pct": 62.8,
                "percentile_250d": 100.0,
            },
            "tags": {"vol_desc": "明显放量"},
            "relative_strength": {
                "rs_excess_20d": 55.6,
                "rs_excess_60d": 62.1,
                "rs_desc": "显著强于大盘",
            },
            "weekly_level": {
                "weekly_macd_status": "金叉上方",
                "weekly_ma20_position": "站上",
                "weekly_trend": "上行",
            },
        }
    }

    metrics = p9_reporter._backfill_sniper_tables_from_p8(payload, p8_db)
    row = payload["sniper_tables"][0]["targets"][0]

    assert metrics == {"p8_backfilled_targets": 1, "p8_price_db_size": 1}
    assert row["price"] == "298.00"
    assert row["chg_3m"] == "+62.8%"
    assert row["percentile_250d"] == "100%"
    assert row["volume"] == "明显放量"
    assert row["rs_strength"] == "20日超额+55.6%，60日超额+62.1%，显著强于大盘"
    assert row["weekly_level"] == "金叉上方，站上，上行"


def test_apply_tdx_stock_codes_to_report_overrides_model_codes(monkeypatch):
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "track_name": "存储",
                "targets": [{"name": "佰维存储", "code": "300667"}],
            }
        ],
        "today_advice": [{"name": "佰维存储", "code": "300667"}],
    }

    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"佰维存储": "688525"})

    metrics = p9_reporter._apply_tdx_stock_codes_to_report(payload)

    assert payload["sniper_tables"][0]["targets"][0]["code"] == "688525"
    assert payload["today_advice"][0]["code"] == "688525"
    assert metrics["p9_model_codes_ignored"] == 2


def test_apply_tdx_stock_codes_to_report_uses_local_alias_fallback(monkeypatch):
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "track_name": "先进封装",
                "targets": [{"name": "文一科技"}],
            },
            {
                "track_name": "光通信",
                "targets": [{"name": "博创科技"}],
            },
        ],
        "today_advice": [{"name": "博创科技"}],
    }

    monkeypatch.setattr(
        p9_reporter,
        "load_tdx_stock_code_map",
        lambda: {
            "三佳科技": "600520",
            "文一三佳": "600520",
            "文一科技": "600520",
            "长芯博创": "300548",
            "博创股份": "300548",
        },
    )

    metrics = p9_reporter._apply_tdx_stock_codes_to_report(payload)

    assert payload["sniper_tables"][0]["targets"][0]["code"] == "600520"
    assert payload["sniper_tables"][1]["targets"][0]["code"] == "300548"
    assert payload["today_advice"][0]["code"] == "300548"
    assert metrics["p9_codes_filled_from_tdx"] == 3
    assert metrics["p9_unmapped_codes"] == 0


def test_p9_prompt_no_longer_asks_model_for_stock_codes():
    from pathlib import Path

    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "p9_report.txt").read_text(encoding="utf-8")

    assert '"code":' not in prompt
    assert "不要输出 `code` 字段" in prompt


def test_p9_risk_analysis_overrides_model_codes_from_local_tdx(monkeypatch):
    import p9_reporter

    captured: dict[str, object] = {}

    def fake_write_json_artifact(prefix, data, **kwargs):
        captured["json_payload"] = copy.deepcopy(data)
        return "dummy.json"

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda prompt: SimpleNamespace(
            content=json.dumps(
                {
                    "x_intel_snipe": [],
                    "p3_machine_intel": [],
                    "core_signals": [],
                    "sniper_tables": [
                        {
                            "track_name": "存储链",
                            "targets": [
                                {
                                    "name": "佰维存储",
                                    "code": "300667",
                                    "price": "未提供",
                                    "chg_3m": "未提供",
                                    "percentile_250d": "未提供",
                                    "volume": "未提供",
                                    "rs_strength": "未提供",
                                    "weekly_level": "未提供",
                                    "elasticity": "高弹",
                                    "catalyst": "存储景气",
                                    "risk": "🟡",
                                }
                            ],
                        }
                    ],
                    "fund_pathway": [],
                    "today_advice": [
                        {
                            "priority": "★★",
                            "name": "佰维存储",
                            "code": "300667",
                            "reason": "存储需求上行",
                            "strategy": "观察低吸",
                            "confidence": 0.82,
                            "evidence_refs": ["P6:存储映射", "P8:技术面"],
                        }
                    ],
                    "risk_warning": [],
                },
                ensure_ascii=False,
            ),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"佰维存储": "688525"})
    monkeypatch.setattr(
        p9_reporter,
        "_load_p8_price_db",
        lambda: {
            "688525": {
                "price_data": {
                    "latest_close": 88,
                    "chg_3m_pct": 12.3,
                    "percentile_250d": 91,
                }
            }
        },
    )
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *args, **kwargs: "dummy.md")
    monkeypatch.setattr(p9_reporter, "write_json_artifact", fake_write_json_artifact)
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: captured.setdefault("manifest_updates", []).append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 evidence",
        p7_deep="P7 evidence",
        p75_supplement="P7.5 evidence",
        p6_mapping="P6 mapping",
        price_data="P8 price data " * 10,
        p2_x_intel_data="P2 data",
        p3_intel_data="P3 data",
        p4_yfinance="P4 data",
    )

    payload = captured["json_payload"]
    row = payload["sniper_tables"][0]["targets"][0]

    assert row["code"] == "688525"
    assert row["price"] == "88.00"
    assert payload["today_advice"][0]["code"] == "688525"
    assert "688525" in report
    assert "300667" not in report


def test_p9_risk_analysis_applies_guardrails_before_rendering(monkeypatch):
    import p9_reporter

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda prompt: SimpleNamespace(
            content=json.dumps(
                {
                    "x_intel_snipe": [],
                    "p3_machine_intel": [],
                    "core_signals": [
                        {
                            "source": "P2",
                            "significance": "高优先级",
                            "title": "X 平台孤证",
                            "body": "尚未被硬层交叉验证。",
                            "confidence": 0.92,
                            "evidence_refs": ["P2-1"],
                        }
                    ],
                    "sniper_tables": [],
                    "fund_pathway": [],
                    "today_advice": [
                        {
                            "priority": "★★★",
                            "name": "生益科技",
                            "code": "600183",
                            "reason": "补充搜索与传闻共振。",
                            "strategy": "只做观察",
                            "confidence": "84%",
                            "evidence_refs": ["P2:传闻", "P7.5:补充验证"],
                        }
                    ],
                    "risk_warning": ["需等待硬层确认。"],
                },
                ensure_ascii=False,
            ),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"生益科技": "600183"})
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *args, **kwargs: "dummy.md")
    monkeypatch.setattr(
        p9_reporter,
        "write_json_artifact",
        lambda prefix, data, timestamp=None: captured.setdefault("json_payload", copy.deepcopy(data)) or "dummy.json",
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: captured.setdefault("manifest_updates", []).append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 evidence",
        p7_deep="P7 evidence",
        p75_supplement="P7.5 evidence",
        p6_mapping="P6 mapping",
        price_data="P8 price data",
        p2_x_intel_data="P2 data",
        p3_intel_data="P3 data",
        p4_yfinance="P4 data",
    )

    payload = captured["json_payload"]
    assert payload["core_signals"][0]["confidence"] == 0.60
    assert payload["today_advice"][0]["confidence"] == 0.68
    assert "置信度 60%" in report
    assert "| ★★☆ | **生益科技** | 600183 | 补充搜索与传闻共振。 | 只做观察 | 68% |" in report


def test_p9_risk_analysis_blocks_raw_delivery_on_invalid_json(monkeypatch):
    import p9_reporter

    captured: dict[str, object] = {"invoke_configs": []}

    def fake_invoke(prompt, config=None):
        captured["invoke_configs"].append(config)
        return SimpleNamespace(content='```json\n{"core_signals": [', thinking_content="")

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        fake_invoke,
    )
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda prefix, content, **kwargs: captured.setdefault("raw_artifact", (prefix, content)) and "raw.md",
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: captured.setdefault("manifest_updates", []).append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 evidence",
        p7_deep="P7 evidence",
        p75_supplement="P7.5 evidence",
        p6_mapping="P6 mapping",
        price_data="P8 price data",
        p2_x_intel_data="P2 data",
        p3_intel_data="P3 data",
        p4_yfinance="P4 data",
    )

    assert report == ""
    assert captured["raw_artifact"][0] == "P9_raw_invalid_output"
    assert captured["manifest_updates"][-1][1]["status"] == "failed_parse"
    assert captured["invoke_configs"][0] is None
    assert captured["invoke_configs"][1]["force_fallback"] is True


def test_p9_two_model_network_failure_delivers_validated_checkpoint_fallback(monkeypatch):
    import artifact_store
    import checkpoint
    import p9_reporter

    run_ts = "20260728083002"
    provenance = {
        "requested_model": "gpt-5.6-sol",
        "attempted_models": ["gpt-5.6-sol", "claude-opus-4-6-thinking"],
        "fallback_used": True,
        "fallback_model": "claude-opus-4-6-thinking",
        "fallback_reason_category": "network_error",
        "fallback_reason": "primary network unavailable",
        "effective_model": "",
        "final_error_category": "empty_response",
        "final_error": "fallback provider returned no output text",
    }
    stage_assessments = {
        stage: {"state": "valid", "reusable": True, "reason": "fixture_valid"}
        for stage in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7a", "p75")
    }
    checkpoint_snapshot = {
        "run_timestamp": run_ts,
        "stage_assessments": stage_assessments,
        "p8_assessment": {"state": "valid", "reusable": True, "reason": "fixture_valid"},
    }
    updates = []

    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: run_ts)
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: "{today_str}{p5_evidence}{p7_section}{p6_mapping}{p75_section}{price_data}{p2_section}{p3_section}{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda _prompt: SimpleNamespace(content="   ", thinking_content=""),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: provenance)
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    report, report_ts = p9_reporter.p9_risk_analysis(
        p5_evidence="# P5 审计\n通过：8条 / 删除：1条",
        p7_deep="# P7a 深研审计\nInP 供应约束已核验",
        p75_supplement="# P7.5 补充审计\n暂无新增硬证据",
        p6_mapping="# P6 映射\n### 赛道: 光芯片\n- 生益科技",
        price_data="# P8 技术面\n生益科技收盘价 100.00",
        p2_x_intel_data="# P2 X 情报\n软线索待核验",
        p3_intel_data="# P3 机动情报\n边缘线索待核验",
        p4_yfinance="# P4 核心行情\n生益科技收盘价 100.00",
        checkpoint_snapshot=checkpoint_snapshot,
    )

    assert report_ts == run_ts
    assert report.startswith("# VCP Hunter P9 战报（降级生成）")
    assert "【降级生成】" in report
    assert "不新增模型归纳、标的筛选、资金路径或操作建议" in report
    assert "## 一、核心信号" in report
    assert "## ⚠️ 风险提示" in report
    assert checkpoint._report_markdown_is_complete(report) is True
    fallback_ready = updates[-1][1]
    assert fallback_ready["status"] == "local_fallback_ready"
    assert fallback_ready["metrics"]["schema_validation"]["status"] == "not_run"
    assert fallback_ready["metrics"]["model_generation"]["status"] == "failed"
    assert fallback_ready["metrics"]["delivery"]["mode"] == "local_checkpoint_fallback"
    assert fallback_ready["metrics"]["structured_degraded"] is True
    assert fallback_ready["metrics"]["error"] == ""

    persisted_metrics = copy.deepcopy(fallback_ready["metrics"])
    delivery_updates = []
    monkeypatch.setattr(
        p9_reporter,
        "_load_persisted_report_outcome_metrics",
        lambda: persisted_metrics,
    )
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "fallback.md")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: delivery_updates.append((args, kwargs)),
    )

    delivery = p9_reporter.send_report_email(report, report_ts)

    assert delivery["ok"] is True
    assert delivery["status"] == "completed_no_email"
    delivered_metrics = delivery_updates[-1][1]["metrics"]
    assert delivered_metrics["schema_validation"]["status"] == "not_run"
    assert delivered_metrics["model_generation"]["status"] == "failed"
    assert delivered_metrics["delivery"]["status"] == "completed_no_email"
    assert delivered_metrics["delivery"]["mode"] == "local_checkpoint_fallback"
    assert delivered_metrics["structured_degraded"] is True
    assert artifact_store.classify_stage_status("completed_no_email", delivered_metrics) == "degraded"


def test_p9_raw_p1_unreviewed_requires_manual_review_and_local_only_delivery(monkeypatch):
    import p9_reporter

    updates = []
    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: "20260830083002")
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual-review report must not invoke an LLM")
        ),
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    report, report_ts = p9_reporter.p9_risk_analysis(
        p5_evidence="# P5 Pro 审计失败兜底\n未经审计的 P1 原始情报",
        p7_deep="# P7 深研\n待审阅",
        p75_supplement="# P7.5\n待审阅",
        p6_mapping="# P6 映射\n- 标的",
        price_data="# P8 技术面\n标的收盘价 100",
        manual_review_required=True,
    )

    assert report_ts == "20260830083002"
    assert "【待人工审阅】" in report
    assert "禁止自动投递" in report
    ready = updates[-1][1]
    assert ready["status"] == "local_manual_review_ready"
    assert ready["metrics"]["report_outcome"] == "manual_review_required"
    assert ready["metrics"]["actionable_items_count"] == 0
    assert ready["metrics"]["delivery"]["mode"] == "manual_review_required"

    persisted_metrics = copy.deepcopy(ready["metrics"])
    delivery_updates = []
    monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "receiver@example.com")
    monkeypatch.setattr(p9_reporter, "_load_persisted_report_outcome_metrics", lambda: persisted_metrics)
    monkeypatch.setattr(p9_reporter, "write_text_artifact", lambda *_args, **_kwargs: "manual.md")
    monkeypatch.setattr(
        p9_reporter,
        "send_report_via_email",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual-review report must not send email")
        ),
    )
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: delivery_updates.append((args, kwargs)),
    )

    delivery = p9_reporter.send_report_email(report, report_ts, email_required=False)

    assert delivery == {"ok": True, "status": "completed_no_email", "report_path": "manual.md", "email_sent": False}
    delivered = delivery_updates[-1][1]["metrics"]
    assert delivered["email_sent"] is False
    assert delivered["delivery"]["mode"] == "manual_review_required"
    assert delivered["delivery"]["status"] == "completed_no_email"


def test_p9_local_fallback_is_blocked_when_checkpoint_snapshot_is_invalid(monkeypatch):
    import p9_reporter

    run_ts = "20260728083002"
    provenance = {
        "requested_model": "gpt-5.6-sol",
        "attempted_models": ["gpt-5.6-sol", "claude-opus-4-6-thinking"],
        "fallback_used": True,
        "fallback_reason_category": "network_error",
        "final_error_category": "network_error",
        "final_error": "network unavailable",
    }
    stage_assessments = {
        stage: {"state": "valid", "reusable": True, "reason": "fixture_valid"}
        for stage in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7a", "p75")
    }
    stage_assessments["p7a"] = {
        "state": "invalid",
        "reusable": False,
        "reason": "stale_p6_input_hash",
    }
    updates = []

    monkeypatch.setattr(p9_reporter, "get_run_timestamp", lambda: run_ts)
    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: "{today_str}{p5_evidence}{p7_section}{p6_mapping}{p75_section}{price_data}{p2_section}{p3_section}{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda _prompt: SimpleNamespace(content="", thinking_content=""),
    )
    monkeypatch.setattr(p9_reporter, "get_model_provenance", lambda *_args: provenance)
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        "P5 evidence",
        "P7 evidence",
        "P7.5 evidence",
        p6_mapping="P6 mapping",
        price_data="P8 technical data " * 5,
        checkpoint_snapshot={
            "run_timestamp": run_ts,
            "stage_assessments": stage_assessments,
            "p8_assessment": {"state": "valid", "reusable": True, "reason": "fixture_valid"},
        },
    )

    assert report == ""
    assert updates[-1][1]["status"] == "failed_empty_output"
    assert updates[-1][1]["metrics"]["delivery"]["checkpoint_validation"] == "failed"


def test_p9_blocks_rejected_identity_in_inputs_output_and_records_fallback_model(monkeypatch):
    import p9_reporter

    captured = {"prompts": [], "manifest_updates": [], "text_artifacts": []}
    provenance = {
        "requested_model": "claude-opus-4-6-thinking",
        "effective_model": "claude-sonnet-4-6",
        "attempted_models": ["claude-opus-4-6-thinking", "claude-sonnet-4-6"],
        "fallback_used": True,
        "fallback_reason_category": "timeout",
        "fallback_reason": "primary request timed out",
    }
    payload = {
        "x_intel_snipe": [],
        "p3_machine_intel": [],
        "core_signals": [
            {
                "source": "P7a",
                "significance": "高",
                "title": "错误身份",
                "body": "虚构电子（002011）已量产；国际复材认证有效。",
                "confidence": 0.8,
                "evidence_refs": ["P7a"],
            },
            {
                "source": "P5",
                "significance": "中",
                "title": "有效信号",
                "body": "中国巨石供给弹性明确。",
                "confidence": 0.8,
                "evidence_refs": ["P5"],
            },
        ],
        "sniper_tables": [
            {
                "track_name": "电子布",
                "targets": [
                    {
                        "name": "虚构电子",
                        "code": "002011",
                        "price": "10",
                        "chg_3m": "1%",
                        "percentile_250d": "50%",
                        "volume": "正常",
                        "elasticity": "中",
                        "rs_strength": "中",
                        "weekly_level": "中",
                        "catalyst": "错误身份",
                        "risk": "高",
                    }
                ],
            }
        ],
        "fund_pathway": [],
        "today_advice": [],
        "risk_warning": ["警惕虚构电子身份错误。"],
    }

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda _name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
    )

    def fake_invoke(prompt):
        captured["prompts"].append(prompt)
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="fallback reasoning",
            response_metadata={"model_provenance": provenance},
        )

    monkeypatch.setattr(p9_reporter.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(
        p9_reporter,
        "load_tdx_stock_code_map",
        lambda: {"中国巨石": "600176", "盾安环境": "002011"},
    )
    monkeypatch.setattr(p9_reporter, "_load_p8_price_db", lambda: {})
    monkeypatch.setattr(
        p9_reporter,
        "write_text_artifact",
        lambda prefix, content, **kwargs: captured["text_artifacts"].append((prefix, content)) or "p9.md",
    )
    monkeypatch.setattr(p9_reporter, "write_json_artifact", lambda *args, **kwargs: "report.json")
    monkeypatch.setattr(
        p9_reporter,
        "update_run_manifest",
        lambda *args, **kwargs: captured["manifest_updates"].append((args, kwargs)),
    )

    report, _ = p9_reporter.p9_risk_analysis(
        p5_evidence="P5 中国巨石有效证据",
        p7_deep="P7 虚构电子错误证据\nP7 中国巨石有效证据",
        p75_supplement="P7.5 evidence",
        p6_mapping="P6 mapping",
        price_data="P8 price data " * 10,
        p2_x_intel_data="P2 data",
        p3_intel_data="P3 data",
        p4_yfinance="P4 data",
        rejected_target_names=["虚构电子"],
    )

    assert "P7 虚构电子错误证据" not in captured["prompts"][0]
    assert "以下名称已被本地 TDX 股票主数据明确拒绝：虚构电子" in captured["prompts"][0]
    assert "虚构电子" not in report
    assert "中国巨石" in report
    assert "国际复材认证有效" in report
    reasoning_prefix, reasoning_body = captured["text_artifacts"][0]
    assert reasoning_prefix == "P9_LLM推理链"
    assert "实际模型: claude-sonnet-4-6" in reasoning_body
    metrics = next(
        kwargs["metrics"]
        for args, kwargs in reversed(captured["manifest_updates"])
        if args == ("p9",) and "model_provenance" in kwargs.get("metrics", {})
    )
    metrics_provenance = metrics["model_provenance"]
    assert metrics_provenance["requested_model"] == provenance["requested_model"]
    assert metrics_provenance["effective_model"] == provenance["effective_model"]
    assert metrics_provenance["fallback_reason_category"] == provenance["fallback_reason_category"]
    assert metrics_provenance["fallback_reason"] == provenance["fallback_reason"]
    assert metrics["rejected_identity_items_removed"] >= 3


def test_p9_short_rejected_alias_does_not_drop_longer_valid_tdx_name(monkeypatch):
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "track_name": "电子布",
                "targets": [{"name": "宏和科技", "code": ""}],
            }
        ],
        "today_advice": [{"name": "宏和科技", "code": ""}],
    }
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"宏和科技": "603256"})

    identity_metrics = p9_reporter._filter_rejected_identity_payload(payload, ["宏和"])
    code_metrics = p9_reporter._apply_tdx_stock_codes_to_report(payload)

    assert identity_metrics["rejected_identity_items_removed"] == 0
    assert payload["sniper_tables"][0]["targets"][0]["name"] == "宏和科技"
    assert payload["sniper_tables"][0]["targets"][0]["code"] == "603256"
    assert payload["today_advice"][0]["code"] == "603256"
    assert code_metrics["p9_rejected_targets"] == 0


def test_p9_canonicalizes_honghe_alias_before_local_code_validation(monkeypatch):
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "track_name": "电子布",
                "targets": [{"name": "宏和电子", "code": "002011"}],
            }
        ],
        "today_advice": [{"name": "宏和电子", "code": "002011"}],
    }
    monkeypatch.setattr(p9_reporter, "load_tdx_stock_code_map", lambda: {"宏和科技": "603256"})

    metrics = p9_reporter._apply_tdx_stock_codes_to_report(payload)

    assert payload["sniper_tables"][0]["targets"][0] == {
        "name": "宏和科技",
        "code": "603256",
    }
    assert payload["today_advice"][0] == {"name": "宏和科技", "code": "603256"}
    assert metrics["p9_model_codes_ignored"] == 2
    assert metrics["p9_canonical_alias_rewrites"] == 2
