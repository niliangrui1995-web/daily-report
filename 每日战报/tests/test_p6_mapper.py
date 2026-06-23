"""Regression tests for P6 A-share mapping guardrails."""

from __future__ import annotations

import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_TDX_STOCK_CODE_MAP = {
    "佰维存储": "688525",
    "江波龙": "301308",
    "德明利": "001309",
    "兆易创新": "603986",
    "聚辰股份": "688123",
}


P6_BAIWEI_WRONG_CODE_CASES = [
    (
        "2026-06-11",
        "内存价格飙涨，DDR5/NAND 涨价周期带动国产存储模组弹性，佰维存储被纳入 HBM 与先进存储映射。",
        "DDR5/NAND 涨价周期带动国产存储模组弹性。",
    ),
    (
        "2026-06-12",
        "SK海力士晶圆产能扩张，美光 HBM 售罄，企业 SSD 创新高，佰维存储签 18.6 亿美元 NAND 长协。",
        "刚签署 18.6 亿美元两年期 3D NAND 长协。",
    ),
    (
        "2026-06-16",
        "SK Hynix HBM 长约、Micron 股价新高、企业 SSD 收入环比 +86.1%，佰维存储受益国产存储模组需求。",
        "企业级 SSD 收入创历史新高，AI Agent 驱动存储需求。",
    ),
]


@pytest.mark.parametrize("run_day,evidence_text,reasoning", P6_BAIWEI_WRONG_CODE_CASES)
def test_baiwei_tdx_code_postprocess_replays_historical_p6_corrections(
    monkeypatch,
    caplog,
    run_day,
    evidence_text,
    reasoning,
):
    import p6_mapper

    written_json = []

    def fake_invoke(_prompt):
        payload = {
            "events": [
                {
                    "track_name": "HBM与先进存储",
                    "event_summary": f"{run_day} 历史日志复现：佰维存储被模型错配为 300667。",
                    "targets": [
                        {
                            "name": "佰维存储",
                            "code": "300667",
                            "elasticity": "⚡中弹性",
                            "reasoning": reasoning,
                        },
                        {
                            "name": "江波龙",
                            "code": "301308",
                            "elasticity": "⚡中弹性",
                            "reasoning": "存储模组与企业级 SSD 需求共振。",
                        },
                        {
                            "name": "德明利",
                            "code": "001309",
                            "elasticity": "⚡中弹性",
                            "reasoning": "存储模组与分销业务受益。",
                        },
                        {
                            "name": "兆易创新",
                            "code": "603986",
                            "elasticity": "🔥高弹性",
                            "reasoning": "NOR/DRAM 等存储芯片景气受益。",
                        },
                        {
                            "name": "聚辰股份",
                            "code": "688123",
                            "elasticity": "⚡中弹性",
                            "reasoning": "存储接口芯片需求改善。",
                        },
                    ],
                }
            ],
            "optional_searches": [],
            "deep_research": [],
        }
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), thinking_content="")

    def fake_write_json_artifact(prefix, data, **kwargs):
        written_json.append(data)
        return f"{prefix}.json"

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: TEST_TDX_STOCK_CODE_MAP)
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", fake_write_json_artifact)
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda prefix, content, **kwargs: f"{prefix}.md")
    monkeypatch.setattr(p6_mapper, "update_run_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda seconds: None)
    caplog.set_level(logging.WARNING)

    report = p6_mapper.generate_report(evidence_text)

    assert "佰维存储（688525）" in report
    assert "佰维存储（300667）" not in report
    assert written_json[0]["events"][0]["targets"][0]["code"] == "688525"
    assert "P6本地代码覆盖" in caplog.text
    assert "300667 -> 688525" in caplog.text
    assert "688525" in caplog.text


def test_p6_prompt_no_longer_injects_stock_code_rules(monkeypatch):
    import p6_mapper

    captured_prompts = []

    def fake_invoke(prompt):
        captured_prompts.append(prompt)
        payload = {
            "events": [
                {
                    "track_name": "HBM与先进存储",
                    "event_summary": "佰维存储受存储景气催化。",
                    "targets": [
                        {
                            "name": "佰维存储",
                            "code": "300667",
                            "elasticity": "⚡中弹性",
                            "reasoning": "存储模组需求改善。",
                        },
                        {"name": "江波龙", "elasticity": "⚡中弹性", "reasoning": "存储模组受益。"},
                        {"name": "德明利", "elasticity": "⚡中弹性", "reasoning": "存储分销受益。"},
                        {"name": "兆易创新", "elasticity": "🔥高弹性", "reasoning": "存储芯片受益。"},
                        {"name": "聚辰股份", "elasticity": "⚡中弹性", "reasoning": "接口芯片受益。"},
                    ],
                }
            ],
            "optional_searches": [],
            "deep_research": [],
        }
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), thinking_content="")

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: TEST_TDX_STOCK_CODE_MAP)
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", lambda prefix, data, **kwargs: f"{prefix}.json")
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda prefix, content, **kwargs: f"{prefix}.md")
    monkeypatch.setattr(p6_mapper, "update_run_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda seconds: None)

    p6_mapper.generate_report("佰维存储受存储景气催化")

    assert "佰维存储 -> 688525" not in captured_prompts[0]
    assert "300667" not in captured_prompts[0]
    assert "canonical map" not in captured_prompts[0]


def test_p6_does_not_keep_model_code_for_unmapped_company(monkeypatch, caplog):
    import p6_mapper

    written_json = []

    def fake_invoke(_prompt):
        payload = {
            "events": [
                {
                    "track_name": "未知新赛道",
                    "event_summary": "模型选择一个本地表未收录公司。",
                    "targets": [
                        {
                            "name": "未收录公司",
                            "code": "123456",
                            "elasticity": "⚡中弹性",
                            "reasoning": "模型给出的公司理由。",
                        },
                        {"name": "佰维存储", "elasticity": "⚡中弹性", "reasoning": "存储模组受益。"},
                        {"name": "江波龙", "elasticity": "⚡中弹性", "reasoning": "存储模组受益。"},
                        {"name": "德明利", "elasticity": "⚡中弹性", "reasoning": "存储分销受益。"},
                        {"name": "兆易创新", "elasticity": "🔥高弹性", "reasoning": "存储芯片受益。"},
                        {"name": "聚辰股份", "elasticity": "⚡中弹性", "reasoning": "接口芯片受益。"},
                    ],
                }
            ],
            "optional_searches": [],
            "deep_research": [],
        }
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), thinking_content="")

    def fake_write_json_artifact(prefix, data, **kwargs):
        written_json.append(data)
        return f"{prefix}.json"

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: TEST_TDX_STOCK_CODE_MAP)
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", fake_write_json_artifact)
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda prefix, content, **kwargs: f"{prefix}.md")
    monkeypatch.setattr(p6_mapper, "update_run_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda seconds: None)
    caplog.set_level(logging.WARNING)

    report = p6_mapper.generate_report("模型选择一个本地表未收录公司")

    assert "未收录公司（123456）" not in report
    assert "未收录公司（N/A）" in report
    assert written_json[0]["events"][0]["targets"][0]["code"] == "N/A"
    assert "已忽略模型 code=123456" in caplog.text


def test_p6_tdx_code_postprocess_uses_local_alias_fallback(monkeypatch):
    import p6_mapper

    payload = {
        "events": [
            {
                "track_name": "本地别名回归",
                "targets": [
                    {"name": "文一科技"},
                    {"name": "博创科技"},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        p6_mapper,
        "load_tdx_stock_code_map",
        lambda: {
            "三佳科技": "600520",
            "文一三佳": "600520",
            "文一科技": "600520",
            "长芯博创": "300548",
            "博创股份": "300548",
        },
    )

    p6_mapper._apply_tdx_stock_codes(payload)

    targets = payload["events"][0]["targets"]
    assert targets[0]["code"] == "600520"
    assert targets[1]["code"] == "300548"
