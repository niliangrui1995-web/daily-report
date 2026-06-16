"""Regression tests for P6 A-share mapping guardrails."""

from __future__ import annotations

import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
def test_baiwei_canonical_code_guardrail_replays_historical_p6_corrections(
    monkeypatch,
    caplog,
    run_day,
    evidence_text,
    reasoning,
):
    import p6_mapper

    captured_prompts = []
    written_json = []

    def fake_invoke(prompt):
        captured_prompts.append(prompt)
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
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda name: "{canonical_code_guardrail}\n{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", fake_write_json_artifact)
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda prefix, content, **kwargs: f"{prefix}.md")
    monkeypatch.setattr(p6_mapper, "update_run_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda seconds: None)
    caplog.set_level(logging.WARNING)

    report = p6_mapper.generate_report(evidence_text)

    assert "佰维存储 -> 688525" in captured_prompts[0]
    assert "佰维存储（688525）" in report
    assert "佰维存储（300667）" not in report
    assert written_json[0]["events"][0]["targets"][0]["code"] == "688525"
    assert "P6纠错拦截" in caplog.text
    assert "300667" in caplog.text
    assert "688525" in caplog.text
