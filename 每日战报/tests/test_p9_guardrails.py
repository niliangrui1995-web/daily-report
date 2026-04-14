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
