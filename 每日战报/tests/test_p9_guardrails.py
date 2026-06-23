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

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        p9_reporter,
        "require_prompt",
        lambda name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
    )
    monkeypatch.setattr(
        p9_reporter.opus_adapter,
        "invoke",
        lambda prompt: SimpleNamespace(content='```json\n{"core_signals": [', thinking_content=""),
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
