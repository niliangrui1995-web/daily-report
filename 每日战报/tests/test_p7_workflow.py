# -*- coding: utf-8 -*-
"""Focused offline tests for the adaptive P7 workflow."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestP7ResearchPlan:
    def test_build_research_plan_filters_recent_topics(self, monkeypatch):
        import p7_deep_research as p7

        monkeypatch.setattr(
            p7,
            "_recent_deep_topic_history",
            lambda days=5: [
                {
                    "topic": "HBM substrate pricing pass-through to PCB suppliers",
                    "normalized": "hbmsubstratepricingpassthroughtopcbsuppliers",
                    "date": "2026-04-11",
                    "path": "dummy",
                }
            ],
        )

        plan = p7.build_research_plan(
            "\n".join(
                [
                    "DEEP_RESEARCH: HBM substrate pricing pass-through to PCB suppliers",
                    "DEEP_RESEARCH: Optical module winners after CPO demand expansion",
                    "SEARCH: CPO supply chain bottleneck",
                ]
            )
        )

        assert plan["deep_topics"] == ["Optical module winners after CPO demand expansion"]
        assert "HBM substrate pricing pass-through to PCB suppliers" in plan["skipped_recent_topics"]

    def test_build_research_plan_sets_high_priority_budget(self, monkeypatch):
        import p7_deep_research as p7

        monkeypatch.setattr(p7, "_recent_deep_topic_history", lambda days=5: [])

        plan = p7.build_research_plan(
            "\n".join(
                [
                    "DEEP_RESEARCH: Why NVLink switch shortages transmit into A-share PCB winners",
                    "SEARCH: NVLink switch substrate shortage",
                    "上游材料紧缺 -> 下游模块传导，关注替代路径和竞争格局",
                ]
            ),
            p4_data="NVIDIA 区间涨跌 +6.20% 大涨催化",
            p5_evidence="## 中可信情报",
        )

        assert plan["should_run_p7"] is True
        assert plan["max_rounds"] == 3
        assert "second_order_chain" in plan["triggers"]
        assert "oligarch_large_move" in plan["triggers"]
        assert "low_confidence" in plan["triggers"]

    def test_iterative_deep_search_stops_early_with_structured_output(self, monkeypatch):
        import p7_deep_research as p7
        import search_engine
        import settings

        monkeypatch.setattr(p7.random, "uniform", lambda *args, **kwargs: 0.0)
        monkeypatch.setattr(p7.time, "sleep", lambda *args, **kwargs: None)

        refine_responses = iter([SimpleNamespace(content="HBM substrate shortage winners and losers")])
        search_results = iter(
            [
                {
                    "text": (
                        "Cause: HBM demand surge drives substrate shortage. "
                        "Winner: PCB supplier A gains incremental orders. "
                        "Loser: small packaging vendors face pressure. "
                        "Alternative: glass substrate remains the backup path."
                    ),
                    "grounding_sources": [{"url": "https://example.com/1", "title": "Round1"}],
                },
                {
                    "text": (
                        "Cause: HBM demand surge drives substrate shortage. "
                        "Winner: PCB supplier A gains incremental orders. "
                        "Loser: small packaging vendors face pressure. "
                        "Alternative: glass substrate remains the backup path."
                    ),
                    "grounding_sources": [{"url": "https://example.com/2", "title": "Round2"}],
                },
            ]
        )

        monkeypatch.setattr(settings.pro_adapter, "invoke", lambda prompt: next(refine_responses))
        monkeypatch.setattr(search_engine, "gemini_search", lambda *args, **kwargs: next(search_results))

        result = p7.iterative_deep_search(
            "HBM substrate pass-through",
            rounds=3,
            goal="explain winners and losers",
            round_backtracks=["72h", "168h", "720h"],
        )

        assert result["rounds_run"] == 2
        assert result["stop_reason"] in {"coverage_complete", "no_new_entities", "low_novelty"}
        assert "#### Evidence" in result["text"]
        assert "#### Open Questions" in result["text"]


class TestSearchPrompt:
    def test_build_search_prompt_mentions_window_policy(self):
        from search_engine import _build_search_prompt

        recent_prompt = _build_search_prompt("P7", "HBM substrate shortage", "72h")
        background_prompt = _build_search_prompt("P7", "HBM substrate shortage", "720h")

        assert "72h" in recent_prompt
        assert "last 72 hours" in recent_prompt
        assert "720h" in background_prompt
        assert "allow up to the last 720 hours" in background_prompt
