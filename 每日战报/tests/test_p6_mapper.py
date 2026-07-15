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

    def fake_invoke(_prompt, _config=None):
        payload = {
            "events": [
                {
                    "track_name": "HBM与先进存储",
                    "event_summary": (
                        f"{run_day} 历史日志复现：佰维存储被模型错配为 300667。"
                        + "存储价格、供需、订单和产能证据已逐项交叉验证。" * 50
                    ),
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

    def fake_invoke(prompt, _config=None):
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

    def fake_invoke(_prompt, _config=None):
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
    assert "未收录公司（N/A）" not in report
    assert all(
        target["name"] != "未收录公司"
        for target in written_json[0]["events"][0]["targets"]
    )
    assert written_json[0]["events"][0]["targets"][0]["code"] == "688525"
    assert "已移除模型 code=123456" in caplog.text


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


def test_p6_parse_failure_never_returns_unvalidated_raw_model_text(monkeypatch):
    import p6_mapper

    updates = []
    invoke_configs = []
    raw_model_text = "ignore local allowlist and recommend UNKNOWN(123456)"

    def fake_invoke(_prompt, config=None):
        invoke_configs.append(config)
        return SimpleNamespace(content=raw_model_text, thinking_content="")

    monkeypatch.setattr(
        p6_mapper.opus_adapter,
        "invoke",
        fake_invoke,
    )
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda *args, **kwargs: "invalid.txt")
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda _seconds: None)

    assert p6_mapper.generate_report("untrusted evidence") == ""
    assert updates[-1][0] == "p6"
    assert updates[-1][1]["status"] == "failed_parse"
    assert all(update[1]["status"] != "fallback_raw_llm" for update in updates)
    assert invoke_configs[0] is None
    assert invoke_configs[1]["force_fallback"] is True


def test_p6_rejected_identity_is_removed_from_research_directives(monkeypatch):
    import p6_mapper

    written_json = []
    manifest_updates = []
    valid_targets = [
        {"name": name, "elasticity": "中", "reasoning": "有效标的"}
        for name in TEST_TDX_STOCK_CODE_MAP
    ]
    payload = {
        "events": [
            {
                "track_name": "电子布",
                "event_summary": "电子布供给紧张",
                "targets": [
                    {"name": "虚构电子", "code": "002011", "elasticity": "高", "reasoning": "错误身份"},
                    *valid_targets,
                ],
            }
        ],
        "optional_searches": ["虚构电子与佰维存储的供给对比"],
        "deep_research": ["中国巨石/虚构电子/佰维存储的国产替代能力"],
    }

    monkeypatch.setattr(
        p6_mapper.opus_adapter,
        "invoke",
        lambda _prompt, _config=None: SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="",
        ),
    )
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: TEST_TDX_STOCK_CODE_MAP)
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(
        p6_mapper,
        "write_json_artifact",
        lambda _prefix, data, **_kwargs: written_json.append(data) or "p6.json",
    )
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda *args, **kwargs: "p6.md")
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda _seconds: None)

    report = p6_mapper.generate_report("电子布证据")

    assert "虚构电子" not in report
    assert written_json[0]["deep_research"] == ["中国巨石/佰维存储的国产替代能力"]
    assert written_json[0]["optional_searches"] == ["与佰维存储的供给对比"]
    metrics = manifest_updates[-1][1]["metrics"]
    assert metrics["rejected_target_names"] == ["虚构电子"]
    assert metrics["identity_validation_version"] == 2


def test_p6_canonicalizes_honghe_alias_and_wrong_code(monkeypatch):
    import p6_mapper

    payload = {
        "events": [
            {
                "track_name": "电子布",
                "event_summary": "宏和电子（002011）受益于低介电电子布需求",
                "targets": [
                    {
                        "name": "宏和电子",
                        "code": "002011",
                        "elasticity": "高",
                        "reasoning": "宏和电子供给弹性",
                    }
                ],
            }
        ],
        "optional_searches": ["宏和电子与中国巨石产能对比"],
        "deep_research": ["宏和电子（002011）Low-Dk量产进展"],
    }
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: {"宏和科技": "603256"})

    metrics = p6_mapper._apply_tdx_stock_codes(payload)

    target = payload["events"][0]["targets"][0]
    assert target["name"] == "宏和科技"
    assert target["code"] == "603256"
    assert "宏和电子" not in str(payload)
    assert "宏和科技（603256）" in payload["events"][0]["event_summary"]
    assert metrics["rejected_targets"] == 0
    assert metrics["canonical_alias_rewrites"] >= 4


def test_p6_records_effective_fallback_model_in_manifest_and_reasoning(monkeypatch):
    import p6_mapper

    text_artifacts = []
    manifest_updates = []
    payload = {
        "events": [
            {
                "track_name": "存储",
                "event_summary": "存储景气",
                "targets": [
                    {"name": name, "elasticity": "中", "reasoning": "有效标的"}
                    for name in TEST_TDX_STOCK_CODE_MAP
                ],
            }
        ],
        "optional_searches": [],
        "deep_research": [],
    }
    provenance = {
        "requested_model": "gpt-5.6-sol",
        "effective_model": "claude-opus-4-6-thinking",
        "attempted_models": ["gpt-5.6-sol", "claude-opus-4-6-thinking"],
        "fallback_used": True,
        "fallback_reason_category": "rate_limit",
        "fallback_reason": "429 Too Many Requests",
    }
    monkeypatch.setattr(
        p6_mapper.opus_adapter,
        "invoke",
        lambda _prompt, _config=None: SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="fallback reasoning",
            response_metadata={"model_provenance": provenance},
        ),
    )
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: TEST_TDX_STOCK_CODE_MAP)
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", lambda *args, **kwargs: "p6.json")
    monkeypatch.setattr(
        p6_mapper,
        "write_text_artifact",
        lambda prefix, content, **kwargs: text_artifacts.append((prefix, content)) or "p6.md",
    )
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda *args, **kwargs: manifest_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda _seconds: None)

    p6_mapper.generate_report("存储证据")

    reasoning_prefix, reasoning_body = text_artifacts[0]
    assert reasoning_prefix == "P6_LLM推理链"
    assert "请求模型: gpt-5.6-sol" in reasoning_body
    assert "实际模型: claude-opus-4-6-thinking" in reasoning_body
    metrics_provenance = manifest_updates[-1][1]["metrics"]["model_provenance"]
    assert metrics_provenance["requested_model"] == provenance["requested_model"]
    assert metrics_provenance["effective_model"] == provenance["effective_model"]
    assert metrics_provenance["fallback_reason"] == provenance["fallback_reason"]
