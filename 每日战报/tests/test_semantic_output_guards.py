"""Focused regressions for semantic-empty model outputs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "text",
    [
        "not found.",
        "No relevant information found.",
        "No relevant information was found.",
        "No evidence was found.",
        "I could not find any relevant evidence.",
        "未找到相关证据。",
        "没有找到任何相关资料。",
        "无可用证据",
        "暂无相关结果。",
    ],
)
def test_no_evidence_sentinel_recognizes_provider_variants(text):
    from semantic_output import is_no_evidence_text

    assert is_no_evidence_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "No evidence supports supplier A, but the filing confirms supplier B.",
        "未找到公司 A 的订单证据，但公司 B 的公告披露了量产进展。",
    ],
)
def test_no_evidence_sentinel_does_not_hide_meaningful_analysis(text):
    from semantic_output import is_no_evidence_text

    assert is_no_evidence_text(text) is False


def test_p1_markdown_heading_shell_is_rejected_by_producer_and_checkpoint(tmp_path):
    import checkpoint
    import orchestrator

    records = [{"text": "# 搜索结果\n暂无相关证据。"}]
    usable, rejected = orchestrator._filter_usable_p1_results(records)
    json_path = tmp_path / "p1.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    assert usable == []
    assert rejected == 1
    assert checkpoint._p1_json_has_evidence(str(json_path)) is False


def test_p1_markdown_heading_with_real_body_remains_usable(tmp_path):
    import checkpoint
    import orchestrator

    records = [{"text": "# 搜索结果\n公司公告披露新产线已经投产。"}]
    usable, rejected = orchestrator._filter_usable_p1_results(records)
    json_path = tmp_path / "p1.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    assert usable == records
    assert rejected == 0
    assert checkpoint._p1_json_has_evidence(str(json_path)) is True


def test_p6_rejects_second_semantically_incomplete_mapping(monkeypatch):
    import p6_mapper

    payload = {
        "events": [
            {
                "track_name": "存储",
                "event_summary": "只有赛道描述，没有任何可验证的本地 A 股标的。",
                "targets": [],
            }
        ],
        "optional_searches": ["继续搜索存储景气"],
        "deep_research": ["研究存储供需"],
    }
    updates = []
    invokes = []

    def fake_invoke(_prompt, _config=None):
        invokes.append(1)
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="",
        )

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", lambda *args, **kwargs: "p6.json")
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda *args, **kwargs: "p6.md")
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda _seconds: None)

    assert p6_mapper.generate_report("存储景气证据") == ""
    assert len(invokes) == 2
    assert updates[-1][0] == "p6"
    assert updates[-1][1]["status"] == "failed_incomplete_output"
    assert updates[-1][1]["metrics"]["valid_events"] == 0
    assert updates[-1][1]["metrics"]["valid_targets"] == 0


def test_p6_duplicate_stock_codes_do_not_satisfy_minimum_targets(monkeypatch):
    import p6_mapper

    targets = [
        {
            "name": f"Company {index}",
            "code": "600000",
            "elasticity": "high",
            "reasoning": "verified reasoning " * 40,
        }
        for index in range(5)
    ]
    payload = {
        "events": [
            {
                "track_name": "memory",
                "event_summary": "supply event",
                "targets": targets,
            }
        ],
        "optional_searches": [],
        "deep_research": [],
    }
    updates = []
    invokes = []

    def fake_invoke(_prompt, _config=None):
        invokes.append(1)
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            thinking_content="",
        )

    monkeypatch.setattr(p6_mapper.opus_adapter, "invoke", fake_invoke)
    monkeypatch.setattr(p6_mapper, "load_tdx_stock_code_map", lambda: {})
    monkeypatch.setattr(p6_mapper, "lookup_tdx_stock_code", lambda _mapping, _name: "600000")
    monkeypatch.setattr(p6_mapper, "require_prompt", lambda _name: "{evidence_text}")
    monkeypatch.setattr(p6_mapper, "build_recent_topic_guardrail", lambda days=5: "")
    monkeypatch.setattr(p6_mapper, "write_json_artifact", lambda *args, **kwargs: "p6.json")
    monkeypatch.setattr(p6_mapper, "write_text_artifact", lambda *args, **kwargs: "p6.md")
    monkeypatch.setattr(
        p6_mapper,
        "update_run_manifest",
        lambda stage, **kwargs: updates.append((stage, kwargs)),
    )
    monkeypatch.setattr(p6_mapper.time, "sleep", lambda _seconds: None)

    assert p6_mapper.generate_report("memory supply evidence") == ""
    assert len(invokes) == 2
    assert updates[-1][1]["status"] == "failed_incomplete_output"
    assert updates[-1][1]["metrics"]["stock_count"] == 1
    assert "too_few_rendered_targets" in updates[-1][1]["metrics"]["incomplete_reasons"]


@pytest.mark.parametrize("sentinel", ["No relevant evidence found.", "未找到相关证据。"])
def test_p7_treats_explicit_no_evidence_as_business_empty(monkeypatch, sentinel):
    import p7_deep_research as p7
    import search_engine

    monkeypatch.setattr(
        search_engine,
        "gemini_search",
        lambda *args, **kwargs: {
            "text": sentinel,
            "structured": None,
            "grounding_sources": [],
        },
    )
    monkeypatch.setattr(p7.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p7.random, "uniform", lambda *_args: 0.0)

    result = p7.iterative_deep_search("HBM supply", rounds=1)

    assert result["status"] == "skipped_no_results"
    assert result["text"] == ""
    assert result["rounds_run"] == 0
    assert result["stop_reason"] == "no_evidence_first_round"


def test_p7_aggregate_preserves_all_topics_no_result_outcome(monkeypatch):
    import p7_deep_research as p7

    monkeypatch.setattr(
        p7,
        "iterative_deep_search",
        lambda *args, **kwargs: {
            "topic": args[0],
            "text": "",
            "status": "skipped_no_results",
            "stop_reason": "no_evidence_first_round",
            "rounds_run": 0,
            "successful_rounds": 0,
            "queries_used": [args[0]],
            "coverage": [],
        },
    )

    result = p7.run_deep_research(
        {
            "should_run_p7": True,
            "deep_topics": ["HBM supply"],
            "max_rounds": 1,
            "round_backtracks": ["unlimited"],
        }
    )

    assert result["status"] == "skipped_no_results"
    assert result["text"] == ""
