# -*- coding: utf-8 -*-
"""Tests for structured intel and refactored helper modules."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIntelStructuring:
    def test_parse_p2_structured_items_extracts_claim_source_confidence(self):
        from intel_structuring import parse_p2_structured_items

        raw_text = """
<x_leaks>
### [HBM] SK 海力士扩产传导
- **信源**: @supplychainwatch
- **核心内容**: HBM 基板交期再次拉长，PCB 供应商进入议价窗口
- **产业链影响**: 上游覆铜板和载板报价有望跟涨
- **原文摘要**: 渠道称 6 月排产继续吃紧
- **真实链接**: https://x.com/example/status/1234567890123456789
- **可信度**: 高
</x_leaks>
"""

        items = parse_p2_structured_items(raw_text)

        assert len(items) == 1
        assert items[0]["track"] == "HBM"
        assert items[0]["source"] == "@supplychainwatch"
        assert "PCB 供应商进入议价窗口" in items[0]["claim"]
        assert items[0]["confidence_label"] == "high"

    def test_render_p3_output_returns_structured_markdown(self):
        from p3_agent import render_p3_output

        markdown, items = render_p3_output(
            {
                "jinnang_1": [{"title": "阵亡链接补回", "summary": "补回一条供应链线索", "track": "HBM", "source_name": "Gemini", "source_url": "https://example.com/a"}],
                "jinnang_2": [{"title": "黑马线索", "summary": "发现一家北美冷板厂扩产", "track": "液冷", "source_name": "HN", "source_url": "https://example.com/b"}],
                "jinnang_3": [],
            },
            collected_at="2026-04-12 09:00",
        )

        assert len(items) == 2
        assert "Claim:" in markdown
        assert "Confidence:" in markdown
        assert "锦囊一" in markdown


class TestSharedResearchGuardrail:
    def test_build_recent_topic_guardrail_reuses_p7_history(self, monkeypatch):
        import p7_deep_research as p7

        monkeypatch.setattr(
            p7,
            "_recent_deep_topic_history",
            lambda days=5: [
                {"topic": "HBM substrate pricing pass-through", "normalized": "a", "date": "2026-04-10", "path": "dummy"},
                {"topic": "CPO optical module bottleneck", "normalized": "b", "date": "2026-04-11", "path": "dummy"},
            ],
        )

        guardrail = p7.build_recent_topic_guardrail(days=5)

        assert "HBM substrate pricing pass-through" in guardrail
        assert "04-11" in guardrail


class TestP9Rendering:
    def test_build_report_markdown_renders_confidence_and_evidence(self):
        from p9_rendering import build_report_markdown

        data = {
            "core_signals": [
                {
                    "source": "P2",
                    "significance": "高优先级",
                    "title": "HBM 交期拉长",
                    "body": "基板和封装同步吃紧。",
                    "confidence": 0.83,
                    "evidence_refs": ["P2-1", "P5-2"],
                }
            ],
            "today_advice": [
                {
                    "priority": "★★★",
                    "name": "生益科技",
                    "code": "600183",
                    "reason": "覆铜板报价传导",
                    "strategy": "等分歧回踩",
                    "confidence": "78%",
                    "evidence_refs": ["P6-1"],
                }
            ],
            "risk_warning": ["海外寡头反身波动可能导致映射失真"],
        }

        markdown = build_report_markdown(data, "2026年04月12日 北京时间09:00")

        assert "置信度 83%" in markdown
        assert "证据 P2-1 / P5-2" in markdown
        assert "| 优先级 | 标的 | 代码 |" in markdown
