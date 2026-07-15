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


class TestP3GroundingRecovery:
    def test_parse_json_response_prefers_grounding_source_url(self):
        import p3_agent

        raw = """```json
[
  {
    "title": "Policy update",
    "source_url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/ABC123",
    "source_name": "Example Source",
    "track": "HBM",
    "summary": "Grounded summary",
    "signal_type": "jinnang_3"
  }
]
```"""

        items = p3_agent._parse_json_response(
            raw,
            bucket="jinnang_3",
            grounding_sources=[{"url": "https://example.com/policy", "title": "Policy update"}],
        )

        assert len(items) == 1
        assert items[0]["source_url"] == "https://example.com/policy"

    def test_parse_json_response_recovers_multiline_source_url(self):
        import p3_agent

        raw = """```json
[
  {
    "title": "Policy update",
    "source_url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/ABC
DEF",
    "source_name": "Example Source",
    "track": "HBM",
    "summary": "Grounded summary",
    "signal_type": "jinnang_3"
  }
]
```"""

        items = p3_agent._parse_json_response(
            raw,
            bucket="jinnang_3",
            grounding_sources=[{"url": "https://example.com/policy", "title": "Policy update"}],
        )

        assert len(items) == 1
        assert items[0]["source_url"] == "https://example.com/policy"

    def test_parse_json_response_persists_raw_response_on_unrecoverable_failure(self, monkeypatch):
        import p3_agent

        captured = {}

        def _fake_write_text_artifact(prefix, content, **kwargs):
            captured["prefix"] = prefix
            captured["content"] = content
            return "mock-artifact.md"

        monkeypatch.setattr(p3_agent, "write_text_artifact", _fake_write_text_artifact)

        items = p3_agent._parse_json_response(
            "not-json-at-all",
            bucket="jinnang_3",
            grounding_sources=[],
        )

        assert items == []
        assert captured["prefix"].startswith("P3_raw_response")
        assert captured["content"] == "not-json-at-all"

    def test_parse_json_response_converts_tavily_fallback_to_items(self):
        import p3_agent

        raw = """[Tavily Fallback] d-Matrix launched its first AI chip.
- Microsoft-backed startup d-Matrix launches first AI chip: d-Matrix raises $275 million for inference chips. [https://www.d-matrix.ai/announcements/d-matrix-raises-275-million]
- CNBC semiconductor equipment: Latest semiconductor and AI equipment headlines. [https://www.cnbc.com/semiconductor-equipment]"""

        items = p3_agent._parse_json_response(
            raw,
            bucket="jinnang_2",
            grounding_sources=[],
        )

        assert len(items) == 2
        assert items[0]["source_name"] == "Tavily"
        assert items[0]["track"] == "Tavily备用搜索"
        assert items[0]["source_url"] == "https://www.d-matrix.ai/announcements/d-matrix-raises-275-million"
        assert items[0]["signal_type"] == "jinnang_2"
        assert items[0]["confidence"] == 0.56


class TestP2BudgetGuardrail:
    def test_filter_x_leak_blocks_keeps_single_line_block(self):
        import p2_x_intel

        raw = (
            "prefix <x_leaks>### [HBM] 单行爆料 "
            "https://x.com/example/status/1983746519203847561</x_leaks> suffix"
        )

        filtered = p2_x_intel._filter_x_leak_blocks(raw, set())

        assert filtered == (
            "<x_leaks>### [HBM] 单行爆料 "
            "https://x.com/example/status/1983746519203847561</x_leaks>"
        )

    def test_filter_x_leak_blocks_rejects_cross_batch_duplicate(self):
        import p2_x_intel

        tweet_id = "1983746519203847561"
        raw = f"<x_leaks>https://x.com/example/status/{tweet_id}</x_leaks>"

        assert p2_x_intel._filter_x_leak_blocks(raw, {tweet_id}) == ""

    def test_collect_from_x_returns_partial_content_when_budget_is_exhausted(self, monkeypatch):
        import p2_x_intel

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "usage": {
                        "total_tokens": 123,
                        "input_tokens": 80,
                        "output_tokens": 43,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "input_tokens_details": {"cached_tokens": 12},
                    },
                    "output": [
                        {
                            "role": "assistant",
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": (
                                        "<x_leaks>\n"
                                        "### [HBM] SK 海力士扩产传导\n"
                                        "- **信源**: @supplychainwatch\n"
                                        "- **核心内容**: HBM 基板交期再次拉长\n"
                                        "- **产业链影响**: 上游覆铜板和载板报价有望跟涨\n"
                                        "- **原文摘要**: 渠道称排产继续吃紧\n"
                                        "- **真实链接**: https://x.com/example/status/1983746519203847561\n"
                                        "- **可信度**: 高\n"
                                        "</x_leaks>"
                                    ),
                                }
                            ],
                        }
                    ],
                }

        clock = {"now": 0.0}
        calls = []
        manifest_updates = []

        def fake_post(*args, **kwargs):
            calls.append(kwargs)
            clock["now"] = 95.0
            return FakeResponse()

        def fake_require_prompt(name):
            if name == "p2_user.txt":
                return "hours={hours}\n{keyword_section}\n{current_time}"
            return "system {{track_count}} {{industry_tracks}} {{super_giants}} {{strategic_giants}}"

        monkeypatch.setattr(p2_x_intel, "GROK_API_KEY", "test-key")
        monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", "")
        monkeypatch.setattr(p2_x_intel, "GROK_MODEL", "grok-4.3")
        monkeypatch.setattr(p2_x_intel, "GROK_REASONING_EFFORT", "none")
        monkeypatch.setattr(p2_x_intel, "GROK_MAX_OUTPUT_TOKENS", 2048)
        monkeypatch.setattr(p2_x_intel, "INDUSTRY_SCAN_KEYWORDS", ["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
        monkeypatch.setattr(p2_x_intel, "require_prompt", fake_require_prompt)
        monkeypatch.setattr(p2_x_intel.requests, "post", fake_post)
        monkeypatch.setattr(p2_x_intel.time, "time", lambda: clock["now"])
        monkeypatch.setattr(p2_x_intel.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(p2_x_intel.random, "uniform", lambda _start, _end: 0)
        monkeypatch.setattr(p2_x_intel, "write_text_artifact", lambda prefix, content: f"{prefix}.md")
        monkeypatch.setattr(p2_x_intel, "write_json_artifact", lambda prefix, data: f"{prefix}.json")
        monkeypatch.setattr(
            p2_x_intel,
            "update_run_manifest",
            lambda stage, status, files=None, metrics=None: manifest_updates.append(
                {"stage": stage, "status": status, "files": files, "metrics": metrics or {}}
            ),
        )

        content, stats = p2_x_intel.collect_from_x(
            "24h",
            max_retries=3,
            max_total_seconds=100,
            request_timeout_seconds=75,
        )

        assert len(calls) == 1
        assert calls[0]["json"]["model"] == "grok-4.3"
        assert calls[0]["json"]["reasoning"] == {"effort": "none"}
        assert calls[0]["json"]["tools"] == [{"type": "x_search"}]
        assert calls[0]["json"]["max_output_tokens"] == 2048
        assert "HBM 基板交期再次拉长" in content
        assert stats["status"] == "partial_timeout"
        assert stats["input_tokens"] == 80
        assert stats["output_tokens"] == 43
        assert stats["reasoning_tokens"] == 0
        assert stats["attempted_chunks"] == 1
        assert stats["successful_chunks"] == 1
        assert stats["total_chunks"] == 2
        assert manifest_updates[-1]["status"] == "completed_structured_degraded"
        assert manifest_updates[-1]["metrics"]["stop_reason"] == "budget_exhausted"


class TestP1Deduplication:
    def test_deduplicate_entries_merges_sources_without_dropping_first_item(self):
        import p1_rss

        entries = [
            {"title": "HBM supply remains tight", "content": "Lead times extend", "source": "source-a"},
            {"title": "HBM supply remains tight", "content": "Lead times extend", "source": "source-b"},
        ]

        result = p1_rss._deduplicate_entries(entries)

        assert len(result) == 1
        assert result[0]["source"] == "source-a, source-b"


class TestCollectIntelPersistence:
    def test_stage_collect_intel_persists_p1_and_p3_before_jinnang_1(self, monkeypatch):
        from datetime import datetime

        import orchestrator

        events = []

        monkeypatch.setattr(orchestrator, "get_run_config", lambda now: "24h")
        monkeypatch.setattr(orchestrator, "format_evidence", lambda results: "evidence")
        monkeypatch.setattr(
            orchestrator,
            "collect_from_rss",
            lambda backtrack: (
                [{"title": "rss item", "text": "HBM"}],
                {
                    "success": ["rss"],
                    "failed": [],
                    "enrich_ok": 0,
                    "enrich_fail": 1,
                    "failed_urls": [{"title": "HBM failed url", "url": "https://example.com/a"}],
                },
            ),
        )
        monkeypatch.setattr(orchestrator, "collect_from_web", lambda backtrack: ([], {"success": [], "failed": []}))
        monkeypatch.setattr(orchestrator, "collect_from_x", lambda backtrack: ("", {"status": "failed_empty"}))
        monkeypatch.setattr(
            orchestrator,
            "collect_p3_parallel_only",
            lambda backtrack: (
                "p3 parallel data",
                {
                    "duration_seconds": 1,
                    "jinnang_1_count": 0,
                    "jinnang_2_count": 1,
                    "jinnang_3_count": 0,
                    "structured_items": 1,
                    "raw_results": {},
                },
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "write_json_artifact",
            lambda prefix, data: events.append(f"json:{prefix}") or f"{prefix}.json",
        )
        monkeypatch.setattr(
            orchestrator,
            "write_text_artifact",
            lambda prefix, content: events.append(f"text:{prefix}") or f"{prefix}.md",
        )
        monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *args, **kwargs: None)

        def fake_run_jinnang_1(failed_urls):
            events.append("jinnang_1")
            return []

        monkeypatch.setattr(orchestrator, "run_jinnang_1", fake_run_jinnang_1)

        context = {
            "resume_from": "none",
            "ckpt": {"files": {}},
            "now": datetime(2026, 5, 12, 8, 30),
        }

        orchestrator._stage_collect_intel(context)

        assert events.index("json:P1_RSS原始数据") < events.index("jinnang_1")
        assert events.index("text:P3_机动情报") < events.index("jinnang_1")
        assert context["evidence_raw"] == "evidence"


class TestTechnicalResume:
    def test_stage_market_mapping_reruns_stale_p6_checkpoint(self, tmp_path, monkeypatch):
        import orchestrator

        files = {}
        for name, content in {
            "p2": "fresh P2",
            "p3": "fresh P3",
            "p4": "fresh P4",
            "p5": "fresh P5",
            "p6": "old P6",
        }.items():
            path = tmp_path / f"{name}.md"
            path.write_text(content, encoding="utf-8")
            files[name] = str(path)

        os.utime(files["p6"], (1000, 1000))
        for key in ("p2", "p3", "p4", "p5"):
            os.utime(files[key], (2000, 2000))

        captured = {}

        def fake_generate_report(evidence):
            captured["evidence"] = evidence
            return "fresh P6"

        monkeypatch.setattr(orchestrator, "generate_report", fake_generate_report)
        monkeypatch.setattr(orchestrator, "write_text_artifact", lambda prefix, content: "fresh-p6.md")
        monkeypatch.setattr(orchestrator, "update_run_manifest", lambda *args, **kwargs: captured.setdefault("manifest", (args, kwargs)))

        context = {
            "resume_from": "p9_resume",
            "ckpt": {"files": files},
            "p2_data": "",
            "p3_data": "",
            "p4_data": "",
            "p5_evidence": "",
            "p6_mapping": "",
            "p7_deep": "",
            "p7a_audited": "",
            "p75_supplement": "",
            "price_data": "",
        }

        orchestrator._stage_market_mapping(context)

        assert context["p6_mapping"] == "fresh P6"
        assert "fresh P5" in captured["evidence"]
        assert "fresh P4" in captured["evidence"]
        assert "fresh P2" in captured["evidence"]
        assert "fresh P3" in captured["evidence"]

    def test_stage_technical_reruns_stale_no_stock_checkpoint(self, monkeypatch):
        import orchestrator

        calls = {}
        monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda report: [("源杰科技", "688498")])
        monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda report: {"688498": "光芯片"})
        monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda report: {"688498": "高弹性"})

        def fake_p8(stocks, tracks, elasticity):
            calls["stocks"] = stocks
            calls["tracks"] = tracks
            calls["elasticity"] = elasticity
            return "fresh P8 data"

        monkeypatch.setattr(orchestrator, "p8_search_stock_prices", fake_p8)

        context = {
            "resume_from": "p9_resume",
            "ckpt": {
                "files": {},
                "p8_status": "skipped_no_stocks",
                "p8_metrics": {"stocks_requested": 0, "stocks_with_data": 0},
                "p8_assessment": {
                    "state": "valid_empty",
                    "reusable": True,
                    "reason": "valid_business_empty",
                    "stocks_requested": 0,
                    "stocks_with_data": 0,
                },
            },
            "p6_mapping": "recovered P6 mapping",
            "price_data": "本次未从 P6 映射中提取到 A 股标的，技术面分析跳过。",
        }

        orchestrator._stage_technical(context)

        assert calls["stocks"] == [("源杰科技", "688498")]
        assert context["price_data"] == "fresh P8 data"

    def test_stage_technical_reruns_p8_older_than_p6(self, tmp_path, monkeypatch):
        import orchestrator

        p6_path = tmp_path / "p6.md"
        p8_path = tmp_path / "p8.md"
        p6_path.write_text("fresh P6", encoding="utf-8")
        p8_path.write_text("old P8", encoding="utf-8")
        os.utime(p8_path, (1000, 1000))
        os.utime(p6_path, (2000, 2000))

        calls = {}
        monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda report: [("Stock", "600000")])
        monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda report: {"600000": "track"})
        monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda report: {"600000": "high"})

        def fake_p8(stocks, tracks, elasticity):
            calls["stocks"] = stocks
            return "fresh P8 data"

        monkeypatch.setattr(orchestrator, "p8_search_stock_prices", fake_p8)

        context = {
            "resume_from": "p9_resume",
            "ckpt": {"files": {"p6": str(p6_path), "p8": str(p8_path)}},
            "p6_mapping": "fresh P6 mapping",
            "price_data": "old P8 data",
        }

        orchestrator._stage_technical(context)

        assert calls["stocks"] == [("Stock", "600000")]
        assert context["price_data"] == "fresh P8 data"

    def test_stage_technical_reuses_non_stale_checkpoint(self, monkeypatch):
        import orchestrator

        monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda report: [("源杰科技", "688498")])
        monkeypatch.setattr(
            orchestrator,
            "p8_search_stock_prices",
            lambda stocks, tracks, elasticity: (_ for _ in ()).throw(AssertionError("should reuse checkpoint")),
        )

        context = {
            "resume_from": "p9_resume",
            "p6_mapping": "recovered P6 mapping",
            "price_data": "# P8 技术面数据\n\n### 源杰科技（688498）\n- 收盘 100.00 元",
        }

        orchestrator._stage_technical(context)

        assert "688498" in context["price_data"]


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
