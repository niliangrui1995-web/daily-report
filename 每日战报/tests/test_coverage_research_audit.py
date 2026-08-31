"""Behavioral coverage for P7 planning/execution and the audit boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import audit_engine
import p7_deep_research as p7


class _Adapter:
    def __init__(self, content="", *, error=None, category="", message="", model="model"):
        self.content = content
        self.error = error
        self.last_error_category = category
        self.last_error_message = message
        self.model = model
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


def _audit_success_data(*, high=None, medium=None, deleted=None):
    return {
        "summary": {"passed": len(high or []) + len(medium or []), "deleted": len(deleted or [])},
        "high_trust": high or [],
        "medium_trust": medium or [],
        "deleted": deleted or [],
    }


@pytest.fixture
def audit_artifacts(monkeypatch):
    manifests = []
    json_writes = []
    text_writes = []
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda prefix, data: json_writes.append((prefix, data)) or f"{prefix}.json",
    )
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda prefix, text: text_writes.append((prefix, text)) or f"{prefix}.md",
    )
    return manifests, json_writes, text_writes


def test_topic_normalization_similarity_and_tokens():
    assert p7._normalize_topic_key("") == ""
    assert p7._normalize_topic_key(" HBM / ABF winners ") == "hbmabfwinners"
    assert p7._topic_similarity("", "HBM") == 0.0
    assert p7._topic_similarity("HBM ABF", "HBM ABF") == 1.0
    assert p7._topic_similarity("HBM substrate", "HBM substrate winners") == 0.92
    assert p7._topic_similarity("the and", "for with") == 0.0
    assert p7._topic_similarity("HBM glass", "HBM copper") == 0.5
    assert p7._topic_tokens("the HBM and ABF") == {"hbm", "abf"}


def test_extract_queries_and_topics_deduplicates_limits_and_empty(caplog):
    caplog.set_level("INFO")
    queries = "\n".join(
        ["SEARCH: short", "SEARCH: HBM substrate supply"]
        + [f"SEARCH: unique supplement query {index}" for index in range(8)]
        + ["SEARCH: HBM substrate supply"]
    )
    result = p7.extract_search_queries(queries)
    assert len(result) == 5
    assert result[0] == "HBM substrate supply"

    topics = "\n".join(
        ["DEEP_RESEARCH: tiny", "DEEP_RESEARCH: HBM substrate transmission path"]
        + [f"DEEP_RESEARCH: distinct long research topic number {index}" for index in range(5)]
        + ["DEEP_RESEARCH: HBM substrate transmission path"]
    )
    assert p7.pick_deep_topics(topics)[:1] == ["HBM substrate transmission path"]
    assert len(p7.pick_deep_topics(topics)) == 3
    assert p7.pick_deep_topics("nothing requested") == []
    assert "did not request" in caplog.text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("plain", ""),
        ("## heading without separator", ""),
        ("## 深度研究：HBM substrate", "HBM substrate"),
        ("## Deep: CPO demand", "CPO demand"),
    ],
)
def test_extract_heading_topic(line, expected):
    assert p7._extract_heading_topic(line) == expected


def test_recent_topic_history_reads_formats_filters_and_deduplicates(monkeypatch, tmp_path):
    now = datetime.now()
    fresh_14 = now.strftime("%Y%m%d%H%M%S")
    fresh_12_with_bad_seconds = now.strftime("%Y%m%d%H%M") + "99"
    old = (now - timedelta(days=10)).strftime("%Y%m%d%H%M%S")

    (tmp_path / f"P7_fresh_{fresh_14}.md").write_text(
        "## 深度研究：HBM substrate path\n## Deep: CPO winners\n## Deep: CPO winners\n## Deep:\n",
        encoding="utf-8",
    )
    (tmp_path / f"P7_minutes_{fresh_12_with_bad_seconds}.md").write_text(
        "## Deep: Liquid cooling capacity\n",
        encoding="utf-8",
    )
    (tmp_path / f"P7_old_{old}.md").write_text("## Deep: old topic\n", encoding="utf-8")
    (tmp_path / "P7_invalid_20261399129999.md").write_text(
        "## Deep: invalid timestamp\n", encoding="utf-8"
    )
    (tmp_path / "P7_no_timestamp.md").write_text("## Deep: no timestamp\n", encoding="utf-8")
    monkeypatch.setattr(p7, "OUTPUT_DIR", str(tmp_path))

    history = p7._recent_deep_topic_history(days=5)
    topics = [item["topic"] for item in history]
    assert "HBM substrate path" in topics
    assert "CPO winners" in topics
    assert "Liquid cooling capacity" in topics
    assert topics.count("CPO winners") == 1
    assert "old topic" not in topics
    assert all(item["date"] == now.strftime("%Y-%m-%d") for item in history)


def test_recent_topic_history_skips_open_error(monkeypatch, tmp_path):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    directory_as_file = tmp_path / f"P7_unreadable_{timestamp}.md"
    directory_as_file.mkdir()
    monkeypatch.setattr(p7.glob, "glob", lambda *args, **kwargs: [str(directory_as_file)])
    assert p7._recent_deep_topic_history() == []


def test_recent_guardrail_empty_limit_and_duplicate(monkeypatch):
    monkeypatch.setattr(p7, "_recent_deep_topic_history", lambda days=5: [])
    assert p7.build_recent_topic_guardrail() == ""

    history = [
        {"topic": f"Topic {index}", "date": f"2026-07-{index + 1:02d}", "path": "x"}
        for index in range(4)
    ]
    monkeypatch.setattr(p7, "_recent_deep_topic_history", lambda days=5: history)
    guardrail = p7.build_recent_topic_guardrail(days=7, limit=2)
    assert guardrail.count("\n") == 1
    assert "Topic 0" in guardrail and "Topic 2" not in guardrail
    assert p7._is_recent_duplicate("Topic 0", history) is True
    assert p7._is_recent_duplicate("unrelated HBM path", history) is False


def test_markers_moves_priority_and_goal(monkeypatch):
    assert p7._has_marker("Winner here", ("winner",)) is True
    assert p7._has_second_order_signal("产业链影响与替代") is True
    assert p7._has_large_oligarch_move("暴跌警惕") is True
    assert p7._has_large_oligarch_move("NVIDIA -5.01%") is True
    assert p7._has_large_oligarch_move("NVIDIA +4.99%") is False
    assert p7._has_low_confidence_signal("尚待确认", "") is True
    assert p7._has_low_confidence_signal("", "传闻") is True

    assert p7._topic_priority("short neutral") == 0
    assert p7._topic_priority("why this very long chain -> 对受益供应商形成影响") >= 3
    goal = p7._derive_goal(
        ["second_order_chain", "oligarch_large_move", "low_confidence"],
        ["topic"],
        ["query"],
    )
    assert "chain transmission" in goal
    assert "A-share" in goal
    assert "evidence gaps" in goal
    assert "supplement searches" in goal
    assert "event cause" in p7._derive_goal([], ["topic"], [])

    class BadMatch:
        def group(self, _index):
            return "not-a-number"

    monkeypatch.setattr(p7.re, "finditer", lambda *args, **kwargs: [BadMatch()])
    assert p7._has_large_oligarch_move("ordinary") is False


def test_build_research_plan_budget_paths_and_identity_rewrites(monkeypatch):
    monkeypatch.setattr(p7, "_recent_deep_topic_history", lambda days=5: [])
    monkeypatch.setattr(p7, "normalize_rejected_stock_names", lambda names: ["BadCo"])

    def remove_rejected(text, names):
        removed = ["BadCo"] if "BadCo" in text else []
        return text.replace("BadCo", "VerifiedCo"), removed

    monkeypatch.setattr(p7, "remove_rejected_stock_mentions", remove_rejected)
    mapping = "\n".join(
        [
            "DEEP_RESEARCH: BadCo ordinary detailed research topic alpha",
            "SEARCH: BadCo supplemental evidence query",
        ]
    )
    plan = p7.build_research_plan(mapping, rejected_target_names=["BadCo"])
    assert plan["deep_topics"] == ["VerifiedCo ordinary detailed research topic alpha"]
    assert plan["supplement_queries"] == ["VerifiedCo supplemental evidence query"]
    assert plan["identity_rewrites"] == 2
    assert plan["max_topics"] == 1
    assert plan["max_rounds"] == 2

    clean_query = p7.build_research_plan(
        "DEEP_RESEARCH: completely ordinary detailed gamma topic\n"
        "SEARCH: ordinary clean supplement query"
    )
    assert clean_query["supplement_queries"] == ["ordinary clean supplement query"]

    single = p7.build_research_plan(
        "DEEP_RESEARCH: completely ordinary detailed alpha topic"
    )
    assert single["max_topics"] == 1
    assert single["max_rounds"] == 2

    two_topics = p7.build_research_plan(
        "DEEP_RESEARCH: completely ordinary detailed alpha topic\n"
        "DEEP_RESEARCH: completely neutral detailed beta topic"
    )
    assert two_topics["max_topics"] == 2
    assert two_topics["max_rounds"] == 2

    high = p7.build_research_plan(
        "DEEP_RESEARCH: why HBM shortage impacts winners and alternatives",
        p4_data="NVIDIA +6.0%",
        p5_evidence="尚待确认",
    )
    assert high["priority"] == "high"
    assert high["max_rounds"] == 3


def test_build_research_plan_drops_identity_rewrites_that_become_too_short(monkeypatch):
    monkeypatch.setattr(p7, "_recent_deep_topic_history", lambda days=5: [])
    monkeypatch.setattr(p7, "normalize_rejected_stock_names", lambda names: ["BadCo"])
    monkeypatch.setattr(
        p7,
        "remove_rejected_stock_mentions",
        lambda text, names: ("tiny", ["BadCo"]),
    )
    plan = p7.build_research_plan(
        "DEEP_RESEARCH: BadCo sufficiently long topic text\n"
        "SEARCH: BadCo sufficiently long supplement query",
        rejected_target_names=["BadCo"],
    )
    assert plan["deep_topics"] == []
    assert plan["supplement_queries"] == []
    assert plan["identity_rewrites"] == 2


def test_build_research_plan_no_candidates_after_recent_filter(monkeypatch):
    monkeypatch.setattr(
        p7,
        "_recent_deep_topic_history",
        lambda days=5: [{"topic": "ordinary detailed topic alpha", "date": "2026-07-14"}],
    )
    plan = p7.build_research_plan("DEEP_RESEARCH: ordinary detailed topic alpha")
    assert plan["should_run_p7"] is False
    assert plan["max_topics"] == 0
    assert plan["round_backtracks"] == []
    assert plan["skipped_recent_topics"] == ["ordinary detailed topic alpha"]


def test_fact_entity_query_and_priority_parsers():
    text = (
        "# heading\n```json\n```\n"
        "1. NVIDIA demand causes a meaningful substrate shortage in 2026.\n"
        "- NVIDIA demand causes a meaningful substrate shortage in 2026. https://example.com\n"
        "* Glass alternatives benefit PCB suppliers across the chain."
    )
    facts = p7._extract_fact_lines(text)
    assert len(facts) == 2
    assert facts[0].startswith("NVIDIA")
    assert p7._normalize_fact_line("- Fact https://example.com   HERE") == "fact here"

    fallback_text = "# " + " ".join(
        f"This is sufficiently long fallback sentence number {index}." for index in range(8)
    )
    assert len(p7._extract_fact_lines(fallback_text)) == 6

    entities = p7._extract_entities("the NVIDIA HBM 中文")
    assert "nvidia" in entities and "hbm" in entities
    assert "the" not in entities and "中文" not in entities
    terms = p7._extract_query_terms("the NVIDIA NVIDIA HBM ABF CPO extra", limit=3)
    assert terms == ["NVIDIA", "HBM", "ABF"]
    assert p7._collect_priority_sentences("") == []
    prioritized = p7._collect_priority_sentences(
        "Neutral background sentence. Winner NVIDIA benefits. Glass is an alternative. Loser faces pressure."
    )
    assert prioritized[0] == "Winner NVIDIA benefits."
    assert prioritized[-1] == "Neutral background sentence."


def test_dedupe_novelty_and_coverage_helpers():
    assert p7._dedupe_query_parts(["", None, " HBM  path ", "hbm path", "ABF"]) == [
        "HBM path",
        "ABF",
    ]
    assert p7._estimate_incremental_novelty([], "short") == 0.0
    previous = "NVIDIA demand creates a sustained substrate shortage."
    current = previous + "\nGlass alternatives create a new competitive response."
    assert p7._estimate_incremental_novelty([previous], current) == 0.5
    coverage = p7._coverage_flags("Cause driver benefits winner; loser risk; alternative substitute")
    assert all(coverage.values())


def test_structured_helpers_valid_and_fallback_paths():
    structured = {
        "summary": " summary ",
        "claims": [" claim one ", "", "claim two"],
        "entities": [" NVIDIA ", "", "HBM"],
        "coverage_flags": {"cause": 1, "winners": True},
        "dates": [" 2026-07-14 ", ""],
        "implications": [" suppliers benefit ", ""],
        "open_questions": [" losing side? ", ""],
    }
    assert p7._structured_summary(None) == ""
    assert p7._structured_summary(structured) == "summary"
    assert p7._structured_claim_lines(structured, "fallback") == ["claim one", "claim two"]
    assert p7._structured_entity_terms(structured, "fallback") == ["NVIDIA", "HBM"]
    assert p7._structured_entity_terms(None, "NVIDIA") == []
    assert p7._structured_entity_terms({"entities": []}, "NVIDIA") == []
    assert p7._structured_coverage(structured, "fallback") == {
        "cause": True,
        "winners": True,
        "losers": False,
        "alternatives": False,
    }
    assert p7._structured_dates(structured, "fallback") == ["2026-07-14"]
    assert p7._structured_implications(structured, ["fallback"]) == ["suppliers benefit"]
    assert p7._structured_open_questions(structured, {}, "topic") == ["losing side?"]
    assert p7._structured_open_questions(
        {"open_questions": []},
        {"cause": True, "winners": True, "losers": True, "alternatives": True},
        "topic",
    ) == []

    fallback = "Cause found on 2026-07-14 and 2026/7/15; winner benefits supplier."
    assert p7._structured_claim_lines({}, fallback)
    assert p7._structured_coverage({}, fallback)["cause"] is True
    assert p7._structured_dates({}, fallback) == ["2026-07-14", "2026/7/15"]
    assert p7._structured_implications({}, ["winner benefits supplier"]) == [
        "winner benefits supplier"
    ]


def test_dates_implications_questions_and_sources():
    dates = p7._extract_dates(
        "2026-07-14 2026-07-14 2026/7/15 2026年7月16日 2026-07-17 2026-07-18 2026-07-19"
    )
    assert len(dates) == 5
    assert dates[0] == "2026-07-14"
    assert p7._extract_implications(["plain fact one", "plain fact two", "plain fact three"]) == [
        "plain fact one",
        "plain fact two",
    ]
    assert p7._extract_implications(["supplier benefits", "loser pressure", "plain"]) == [
        "supplier benefits",
        "loser pressure",
    ]

    questions = p7._open_questions(
        {"cause": False, "winners": False, "losers": False, "alternatives": False}, "HBM"
    )
    assert len(questions) == 3
    assert "HBM" in questions[0]
    assert p7._open_questions(
        {"cause": True, "winners": True, "losers": True, "alternatives": True}, "HBM"
    ) == []

    assert p7._format_source_lines([]) == ["- No grounded sources returned by the search engine."]
    sources = [
        {"url": "", "title": "missing"},
        {"url": "https://one", "title": ""},
        {"url": "https://one", "title": "duplicate"},
    ] + [{"url": f"https://{index}", "title": f"S{index}"} for index in range(2, 8)]
    rendered = p7._format_source_lines(sources)
    assert len(rendered) == 5
    assert rendered[0] == "- [https://one](https://one)"


def test_build_rule_refined_query_empty_focus_complete_fallback_and_limit(monkeypatch):
    assert p7._build_rule_refined_query("topic", "", "goal", 1) == ("topic", True)

    query, fallback = p7._build_rule_refined_query(
        "HBM substrate",
        "Cause is known but downstream gaps remain for NVIDIA and ABF suppliers.",
        "identify winners",
        1,
    )
    assert fallback is False
    assert "winners" in query and "alternatives" in query

    complete = {
        "claims": ["all core facts are now grounded"],
        "entities": ["NVIDIA"],
        "coverage_flags": {"cause": True, "winners": True, "losers": True, "alternatives": True},
    }
    first, _ = p7._build_rule_refined_query("HBM", "text", "", 1, latest_structured=complete)
    second, _ = p7._build_rule_refined_query("HBM", "text", "", 2, latest_structured=complete)
    assert "supply chain" in first
    assert "competitive response" in second

    monkeypatch.setattr(p7, "_dedupe_query_parts", lambda parts: ["topic"])
    assert p7._build_rule_refined_query("topic", "long enough evidence", "", 1) == (
        "topic",
        True,
    )
    monkeypatch.setattr(p7, "_dedupe_query_parts", lambda parts: ["x" * 250])
    truncated, used_fallback = p7._build_rule_refined_query("topic", "evidence", "", 1)
    assert used_fallback is False and len(truncated) == 220


@pytest.mark.parametrize(
    ("raw", "fallback_used"),
    [
        ("", True),
        ("```\n# heading", True),
        ("short", True),
        ("analysis", True),
        ("starting the process now", True),
        ("********", True),
        ("!!!!!!!!!!", True),
        ("1. HBM substrate winners and alternatives", False),
    ],
)
def test_pick_refined_query_guards(raw, fallback_used):
    query, used = p7._pick_refined_query(raw, "fallback topic")
    assert used is fallback_used
    if fallback_used:
        assert query == "fallback topic"
    else:
        assert query == "HBM substrate winners and alternatives"


def test_pick_refined_query_truncates_long_query():
    query, used = p7._pick_refined_query("HBM " + "x" * 300, "fallback")
    assert used is False
    assert len(query) == 220


def test_format_round_section_empty_and_structured():
    empty = p7._format_round_section(
        round_index=1,
        query="q",
        backtrack="",
        goal="",
        result_text="",
        structured=None,
        sources=[],
        novelty=0,
        coverage={"cause": True, "winners": True, "losers": True, "alternatives": True},
        new_entities_count=0,
    )
    assert "No stable claim extracted" in empty
    assert "No evidence lines" in empty
    assert "No explicit dates" in empty
    assert "did not surface" in empty
    assert "Core gaps closed" in empty

    structured = {
        "summary": "claim",
        "claims": ["evidence"],
        "dates": ["2026-07-14"],
        "implications": ["impact"],
        "open_questions": ["question"],
    }
    full = p7._format_round_section(
        round_index=2,
        query="q",
        backtrack="",
        goal="goal",
        result_text="text",
        structured=structured,
        sources=[{"url": "https://x", "title": "X"}],
        novelty=0.5,
        coverage={"cause": False, "winners": False, "losers": False, "alternatives": False},
        new_entities_count=2,
    )
    assert "evidence" in full and "2026-07-14" in full and "question" in full


def _install_p7_search(monkeypatch, results):
    import search_engine

    result_iter = iter(results)
    monkeypatch.setattr(p7.random, "uniform", lambda *args: 0.0)
    monkeypatch.setattr(p7.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(search_engine, "gemini_search", lambda *args, **kwargs: next(result_iter))


def _plain_search_result(text, *, source="https://source"):
    return {"text": text, "grounding_sources": [{"url": source, "title": "source"}]}


def test_iterative_search_empty_first_and_later(monkeypatch):
    _install_p7_search(monkeypatch, [{}])
    result = p7.iterative_deep_search("topic", rounds=2, round_backtracks=[""])
    assert result["status"] == "failed_empty_output"
    assert result["stop_reason"] == "empty_first_round"
    assert result["rounds_run"] == 0

    _install_p7_search(
        monkeypatch,
        [_plain_search_result("NVIDIA provides sufficiently detailed first-round evidence."), {}],
    )
    result = p7.iterative_deep_search("topic", rounds=3, round_backtracks=[""])
    assert result["status"] == "completed"
    assert result["stop_reason"] == "search_empty"
    assert result["rounds_run"] == 1


def test_iterative_search_fallback_streak(monkeypatch):
    results = [
        _plain_search_result(f"Entity{index} supplies a distinct sufficiently detailed evidence fact {index}.")
        for index in range(3)
    ]
    _install_p7_search(monkeypatch, results)
    monkeypatch.setattr(p7, "_build_rule_refined_query", lambda **kwargs: ("topic", True))
    result = p7.iterative_deep_search("topic", rounds=3, round_backtracks=[""])
    assert result["stop_reason"] == "fallback_streak"
    assert result["rounds_run"] == 3


def test_iterative_search_no_entities_low_novelty_and_max_rounds(monkeypatch):
    repeated_entity = [
        _plain_search_result("NVIDIA supplies the first sufficiently detailed substrate evidence."),
        _plain_search_result("NVIDIA provides another sufficiently detailed capacity evidence."),
    ]
    _install_p7_search(monkeypatch, repeated_entity)
    monkeypatch.setattr(p7, "_extract_entities", lambda _text: {"nvidia"})
    result = p7.iterative_deep_search("topic", rounds=3, round_backtracks=[""])
    assert result["stop_reason"] == "no_new_entities"

    distinct = [
        _plain_search_result("NVIDIA supplies the first sufficiently detailed substrate evidence."),
        _plain_search_result("AMD provides a second sufficiently detailed capacity evidence."),
    ]
    _install_p7_search(monkeypatch, distinct)
    entity_sets = iter([{"nvidia"}, {"amd"}])
    monkeypatch.setattr(p7, "_extract_entities", lambda _text: next(entity_sets))
    novelty = iter([1.0, 0.1])
    monkeypatch.setattr(p7, "_estimate_incremental_novelty", lambda *args: next(novelty))
    result = p7.iterative_deep_search("topic", rounds=3, round_backtracks=[""])
    assert result["stop_reason"] == "low_novelty"

    _install_p7_search(monkeypatch, [_plain_search_result("NVIDIA provides detailed evidence.")])
    monkeypatch.setattr(p7, "_extract_entities", lambda _text: {"nvidia"})
    monkeypatch.setattr(p7, "_estimate_incremental_novelty", lambda *args: 1.0)
    result = p7.iterative_deep_search("topic", rounds=1, round_backtracks=[""])
    assert result["stop_reason"] == "max_rounds_reached"
    assert result["rounds_run"] == 1


def test_iterative_search_structured_entities_and_coverage_complete(monkeypatch):
    results = [
        {
            "text": "round one",
            "structured": {
                "summary": "round one",
                "claims": ["NVIDIA cause is grounded with detailed evidence."],
                "entities": ["NVIDIA"],
                "coverage_flags": {
                    "cause": True,
                    "winners": True,
                    "losers": False,
                    "alternatives": False,
                },
            },
            "grounding_sources": [],
        },
        {
            "text": "round two",
            "structured": {
                "summary": "round two",
                "claims": ["AMD downside and glass alternatives add distinct detailed evidence."],
                "entities": ["AMD", "Glass"],
                "coverage_flags": {
                    "cause": True,
                    "winners": True,
                    "losers": True,
                    "alternatives": True,
                },
            },
            "grounding_sources": [],
        },
    ]
    _install_p7_search(monkeypatch, results)
    monkeypatch.setattr(p7, "_estimate_incremental_novelty", lambda *args: 1.0)
    result = p7.iterative_deep_search("topic", rounds=3, round_backtracks=[""])
    assert result["stop_reason"] == "coverage_complete"
    assert result["rounds_run"] == 2
    assert result["coverage"] == ["alternatives", "cause", "losers", "winners"]


def test_run_deep_research_skip_success_filter_and_worker_exception(monkeypatch):
    skipped = p7.run_deep_research({"should_run_p7": False, "deep_topics": ["topic"]})
    assert skipped["status"] == "skipped_not_triggered"

    def worker(topic, **kwargs):
        if topic == "bad":
            raise RuntimeError("boom")
        return {
            "topic": topic,
            "text": "remove this line\nkeep this evidence",
            "status": "completed",
            "rounds_run": 2,
            "successful_rounds": 2,
        }

    monkeypatch.setattr(p7, "iterative_deep_search", worker)
    monkeypatch.setattr(p7, "normalize_rejected_stock_names", lambda names: ["Rejected"])
    monkeypatch.setattr(
        p7,
        "filter_rejected_stock_lines",
        lambda text, names: ("keep this evidence", 1),
    )
    result = p7.run_deep_research(
        {
            "should_run_p7": True,
            "deep_topics": ["good", "bad"],
            "max_rounds": 2,
            "goal": "goal",
            "triggers": ["explicit_topics"],
            "rejected_target_names": ["Rejected"],
            "skipped_recent_topics": ["old"],
        }
    )
    assert result["status"] == "completed"
    assert result["topics_completed"] == 1
    assert result["topic_results"][1]["status"] == "failed_exception"
    assert result["rejected_identity_lines_removed"] == 1
    assert result["skipped_recent_topics"] == ["old"]


@pytest.mark.parametrize(
    ("primary_source_count", "expected_tier", "expects_summary_notice"),
    [
        (0, "summary_lead_pending_verification", True),
        (1, "primary_source_backed", False),
    ],
)
def test_run_deep_research_persists_topic_and_aggregate_evidence_provenance(
    monkeypatch,
    primary_source_count,
    expected_tier,
    expects_summary_notice,
):
    def worker(topic, **_kwargs):
        return {
            "topic": topic,
            "text": "示例公司订单与量产线索",
            "status": "completed",
            "rounds_run": 1,
            "successful_rounds": 1,
            "evidence_provenance": {
                "primary_source_count": primary_source_count,
                "summary_source_count": 2,
                "source_url_count": 3,
                "evidence_tier": expected_tier,
                "rounds": [
                    {
                        "round_index": 1,
                        "primary_source_count": primary_source_count,
                        "summary_source_count": 2,
                        "source_url_count": 3,
                        "evidence_tier": expected_tier,
                    }
                ],
            },
        }

    monkeypatch.setattr(p7, "iterative_deep_search", worker)
    result = p7.run_deep_research({"should_run_p7": True, "deep_topics": ["示例主题"]})

    aggregate = result["evidence_provenance"]
    topic = result["topic_results"][0]["evidence_provenance"]
    assert aggregate["primary_source_count"] == primary_source_count
    assert aggregate["summary_source_count"] == 2
    assert aggregate["evidence_tier"] == expected_tier
    assert topic["evidence_tier"] == expected_tier
    assert ("摘要线索待核验" in result["text"]) is expects_summary_notice


def test_run_deep_research_all_empty(monkeypatch):
    monkeypatch.setattr(
        p7,
        "iterative_deep_search",
        lambda topic, **kwargs: {
            "topic": topic,
            "text": "",
            "status": "failed_empty_output",
            "rounds_run": 0,
            "successful_rounds": 0,
        },
    )
    result = p7.run_deep_research({"should_run_p7": True, "deep_topics": ["a"]})
    assert result["status"] == "failed_empty_output"
    assert result["text"] == ""


def test_run_deep_research_filtered_text_can_become_empty(monkeypatch):
    monkeypatch.setattr(
        p7,
        "iterative_deep_search",
        lambda topic, **kwargs: {
            "topic": topic,
            "text": "Rejected-only output",
            "status": "completed",
            "rounds_run": 1,
            "successful_rounds": 1,
        },
    )
    monkeypatch.setattr(p7, "filter_rejected_stock_lines", lambda text, names: ("", 1))
    result = p7.run_deep_research(
        {"should_run_p7": True, "deep_topics": ["a", "b"], "rejected_target_names": ["Rejected"]}
    )
    assert result["status"] == "failed_empty_output"
    assert result["rejected_identity_lines_removed"] == 2


@pytest.mark.parametrize(
    ("category", "message", "expected"),
    [
        ("timeout", "provider slow", "timeout: provider slow"),
        ("", "provider slow", "provider slow"),
        ("timeout", "", "timeout"),
        ("", "", ""),
    ],
)
def test_adapter_error_shapes(category, message, expected):
    adapter = SimpleNamespace(last_error_category=category, last_error_message=message)
    assert audit_engine._adapter_error(adapter) == expected


def test_invoke_audit_primary_success_and_short_without_fallback(monkeypatch):
    primary = _Adapter("valid content")
    monkeypatch.setattr(audit_engine, "pro_adapter", primary)
    monkeypatch.setattr(audit_engine, "get_audit_fallback_adapter", lambda: None)
    response, active, metrics = audit_engine._invoke_audit_with_fallback("prompt", "P5", min_content_chars=5)
    assert response.content == "valid content"
    assert active is primary
    assert metrics == {"audit_fallback_used": False, "audit_primary_error": ""}

    short = _Adapter("x", category="short", message="too short")
    monkeypatch.setattr(audit_engine, "pro_adapter", short)
    response, active, metrics = audit_engine._invoke_audit_with_fallback("prompt", "P7a", min_content_chars=10)
    assert response.content == "x"
    assert active is short
    assert metrics["audit_primary_error"] == "short: too short"


def test_invoke_audit_primary_exception_no_fallback_and_fallback_success(monkeypatch):
    primary = _Adapter(error=TimeoutError("primary down"))
    monkeypatch.setattr(audit_engine, "pro_adapter", primary)
    monkeypatch.setattr(audit_engine, "get_audit_fallback_adapter", lambda: None)
    with pytest.raises(RuntimeError, match="primary down"):
        audit_engine._invoke_audit_with_fallback("prompt", "P5")

    fallback = _Adapter("fallback result", model="backup")
    monkeypatch.setattr(audit_engine, "get_audit_fallback_adapter", lambda: fallback)
    response, active, metrics = audit_engine._invoke_audit_with_fallback("prompt", "P5")
    assert response.content == "fallback result"
    assert active is fallback
    assert metrics["audit_fallback_used"] is True
    assert metrics["audit_primary_error"] == "primary down"


def test_gemini_audit_empty_prompt_missing_and_invoke_failure(monkeypatch, audit_artifacts):
    manifests, _, _ = audit_artifacts
    assert audit_engine.gemini_pro_audit("", audit_type="p5") == ""
    assert manifests[-1][1]["status"] == "skipped_empty_input"

    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert audit_engine.gemini_pro_audit("raw", audit_type="p75") == ""
    assert manifests[-1][0] == "p75"
    assert manifests[-1][1]["status"] == "failed_missing_prompt"

    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invoke failed")),
    )
    assert audit_engine.gemini_pro_audit("raw", audit_type="p5") == ""
    assert manifests[-1][1]["status"] == "failed_invoke"


def test_gemini_audit_empty_output_records_adapter_error(monkeypatch, audit_artifacts):
    manifests, _, _ = audit_artifacts
    adapter = _Adapter(category="timeout", message="provider returned nothing")
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=""),
            adapter,
            {"audit_fallback_used": True},
        ),
    )
    assert audit_engine.gemini_pro_audit("raw") == ""
    metrics = manifests[-1][1]["metrics"]
    assert manifests[-1][1]["status"] == "failed_empty_output"
    assert metrics["failure_category"] == "timeout"
    assert metrics["error"] == "provider returned nothing"

    adapter = _Adapter()
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=""),
            adapter,
            {"audit_fallback_used": False},
        ),
    )
    assert audit_engine.gemini_pro_audit("raw") == ""
    metrics = manifests[-1][1]["metrics"]
    assert "failure_category" not in metrics
    assert "error" not in metrics


@pytest.mark.parametrize(
    ("audit_type", "raw", "expected_prompt_text"),
    [
        ("p5", "fresh RSS evidence", "fresh RSS evidence"),
        ("p75", "[Tavily Fallback] fallback evidence", "Tavily"),
        ("p75", "Gemini supplement evidence", "Gemini"),
    ],
)
def test_gemini_audit_success_rendering_branches(
    monkeypatch, audit_artifacts, audit_type, raw, expected_prompt_text
):
    manifests, json_writes, text_writes = audit_artifacts
    captured = {}
    adapter = _Adapter()
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )

    def invoke(prompt, layer_name):
        captured["prompt"] = prompt
        return SimpleNamespace(content="json"), adapter, {"audit_fallback_used": False}

    monkeypatch.setattr(audit_engine, "_invoke_audit_with_fallback", invoke)
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: _audit_success_data(
            high=["verified high"],
            medium=["verified medium"],
            deleted=[{"reason": "old", "content": "stale"}, "duplicate"],
        ),
    )
    result = audit_engine.gemini_pro_audit(raw, audit_type=audit_type)
    assert "verified high" in result and "verified medium" in result
    assert expected_prompt_text in captured["prompt"]
    assert json_writes and "duplicate" in text_writes[-1][1]
    assert manifests[-1][1]["status"] == "completed"


def test_gemini_audit_rejects_empty_sections(monkeypatch, audit_artifacts):
    manifests, _, text_writes = audit_artifacts
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content="json"),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: _audit_success_data(),
    )
    result = audit_engine.gemini_pro_audit("raw")
    assert result == ""
    assert text_writes
    assert text_writes[-1][0].endswith("_RAW_SEMANTIC_INVALID")
    assert manifests[-1][1]["status"] == "failed_semantic_output"
    assert manifests[-1][1]["metrics"]["semantic_error"] == "empty_audit_payload"


def test_gemini_audit_json_retry_then_success(monkeypatch, audit_artifacts):
    manifests, _, text_writes = audit_artifacts
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    invokes = []
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: invokes.append(args) or (
            SimpleNamespace(content="raw invalid"),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    decoder_error = json.JSONDecodeError("bad", "x", 0)
    parser = Mock(side_effect=[decoder_error, _audit_success_data(high=["recovered"])])
    monkeypatch.setattr(audit_engine, "extract_json_from_llm_response", parser)
    sleeps = []
    monkeypatch.setattr(audit_engine.time, "sleep", sleeps.append)
    result = audit_engine.gemini_pro_audit("raw")
    assert "recovered" in result
    assert len(invokes) == 2
    assert sleeps == [5]
    assert any("RAW_INVALID_1" in prefix for prefix, _ in text_writes)
    assert manifests[-1][1]["metrics"]["retry_count"] == 1


def test_gemini_audit_semantic_count_retry_then_success(monkeypatch, audit_artifacts):
    manifests, _, text_writes = audit_artifacts
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    invokes = []
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: invokes.append(args) or (
            SimpleNamespace(content="model-json"),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    parser = Mock(
        side_effect=[
            _audit_success_data(high=["verified high"], medium=["verified medium"])
            | {"summary": {"passed": 1, "deleted": 0}},
            _audit_success_data(high=["verified high"], medium=["verified medium"]),
        ]
    )
    monkeypatch.setattr(audit_engine, "extract_json_from_llm_response", parser)
    sleeps = []
    monkeypatch.setattr(audit_engine.time, "sleep", sleeps.append)

    result = audit_engine.gemini_pro_audit("raw")

    assert "verified high" in result and "verified medium" in result
    assert len(invokes) == 2
    assert "summary.passed" in invokes[0][0]
    assert "passed_count_mismatch" in invokes[1][0]
    assert sleeps == [5]
    assert any("RAW_SEMANTIC_INVALID_1" in prefix for prefix, _ in text_writes)
    assert manifests[-1][1]["metrics"]["retry_count"] == 1


def test_gemini_audit_json_retry_exhausted_and_raw_write_failure(monkeypatch, audit_artifacts):
    manifests, _, _ = audit_artifacts
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|{vanguard_list}|{raw_data}",
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content="raw invalid"),
            _Adapter(),
            {"audit_fallback_used": True},
        ),
    )
    error = json.JSONDecodeError("bad", "x", 0)
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = audit_engine.gemini_pro_audit("raw", _retry_count=1)
    assert result == "raw invalid"
    assert manifests[-1][1]["status"] == "fallback_raw_llm"
    assert manifests[-1][1]["files"] is None


def test_p7a_short_prompt_missing_invoke_and_short_output(monkeypatch, audit_artifacts):
    manifests, _, _ = audit_artifacts
    assert audit_engine.p7a_deep_research_audit("short") == "short"
    assert manifests[-1][1]["status"] == "skipped_short_input"

    raw = "x" * 250
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    missing_prompt_result = audit_engine.p7a_deep_research_audit(raw)
    assert missing_prompt_result.startswith("> 【P7a结构化降级】")
    assert missing_prompt_result.endswith(raw)
    assert manifests[-1][1]["status"] == "failed_missing_prompt"

    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{today_str}|{vanguard_list}|{raw_deep_research}",
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )
    invoke_failure_result = audit_engine.p7a_deep_research_audit(raw)
    assert invoke_failure_result.startswith("> 【P7a结构化降级】")
    assert invoke_failure_result.endswith(raw)
    assert manifests[-1][1]["status"] == "failed_invoke"

    adapter = _Adapter(category="empty", message="too short")
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content="tiny"),
            adapter,
            {"audit_fallback_used": True},
        ),
    )
    empty_output_result = audit_engine.p7a_deep_research_audit(raw)
    assert empty_output_result.startswith("> 【P7a结构化降级】")
    assert empty_output_result.endswith(raw)
    assert manifests[-1][1]["status"] == "failed_empty_output"
    assert manifests[-1][1]["metrics"]["failure_category"] == "empty"

    adapter = _Adapter()
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content="tiny"),
            adapter,
            {"audit_fallback_used": False},
        ),
    )
    second_empty_output_result = audit_engine.p7a_deep_research_audit(raw)
    assert second_empty_output_result.startswith("> 【P7a结构化降级】")
    assert second_empty_output_result.endswith(raw)
    metrics = manifests[-1][1]["metrics"]
    assert "failure_category" not in metrics
    assert "error" not in metrics


def test_p7a_structured_success_identity_filter_and_artifacts(monkeypatch, audit_artifacts):
    manifests, json_writes, text_writes = audit_artifacts
    raw = "original research " * 20
    monkeypatch.setattr(audit_engine, "normalize_rejected_stock_names", lambda names: ["BadCo"])
    filter_calls = []

    def filter_lines(text, names):
        filter_calls.append(text)
        return text.replace("BadCo", ""), 1 if "BadCo" in text else 0

    monkeypatch.setattr(audit_engine, "filter_rejected_stock_lines", filter_lines)
    monkeypatch.setattr(
        audit_engine,
        "filter_rejected_stock_tree",
        lambda data, names: (data, 2),
    )
    monkeypatch.setattr(audit_engine, "build_rejected_stock_guardrail", lambda names: "IDENTITY GUARD")
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{today_str}|{vanguard_list}|{raw_deep_research}",
    )
    captured = {}

    def invoke(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        content = (
            '```json\n{"topics":[{"name":"合规主题"}]}\n```===MARKDOWN===\n'
            '## 主题1：BadCo 合规主题\n\n'
            '- verified markdown ' + "x" * 120
        )
        return SimpleNamespace(content=content), _Adapter(), {"audit_fallback_used": False}

    monkeypatch.setattr(audit_engine, "_invoke_audit_with_fallback", invoke)
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: {"topics": [{"name": "合规主题"}]},
    )
    result = audit_engine.p7a_deep_research_audit(raw, rejected_target_names=["BadCo"])
    assert "BadCo" not in result
    assert "IDENTITY GUARD" in captured["prompt"]
    assert json_writes and text_writes
    assert manifests[-1][1]["status"] == "completed"
    assert manifests[-1][1]["metrics"]["rejected_identity_items_removed"] == 3
    assert manifests[-1][1]["metrics"]["topics_count"] == 1


def test_p7a_json_degraded_artifact_failures_and_missing_separator(monkeypatch, audit_artifacts):
    manifests, _, _ = audit_artifacts
    raw = "original research " * 20
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{today_str}|{vanguard_list}|{raw_deep_research}",
    )
    valid_markdown = "## 主题1：可用 Markdown\n\n" + "markdown body " * 20
    invalid_content = "invalid json===MARKDOWN===" + valid_markdown
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=invalid_content),
            _Adapter(),
            {"audit_fallback_used": True},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad json")),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("json disk")),
    )
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("md disk")),
    )
    result = audit_engine.p7a_deep_research_audit(raw)
    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(valid_markdown)
    assert manifests[-1][1]["status"] == "completed_structured_degraded"
    metrics = manifests[-1][1]["metrics"]
    assert metrics["structured_degraded"] is True
    assert metrics["parse_error"] == "bad json"
    assert metrics["raw_json_attempt_chars"] > 0
    assert manifests[-1][1]["files"] is None

    no_separator = "## 主题1：无分隔符 Markdown\n\n" + "plain markdown without separator " * 10
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=no_separator),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    result = audit_engine.p7a_deep_research_audit(raw)
    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(no_separator)
    assert manifests[-1][1]["status"] == "completed_structured_degraded"
    assert manifests[-1][1]["metrics"]["degrade_reason"] == "missing_markdown_separator"


def test_p7a_degraded_empty_json_and_markdown_only_with_successful_artifacts(
    monkeypatch, audit_artifacts
):
    manifests, json_writes, text_writes = audit_artifacts
    raw = "original research " * 20
    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: "{today_str}|{vanguard_list}|{raw_deep_research}",
    )
    error = ValueError("empty json")
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        audit_engine,
        "filter_rejected_stock_tree",
        lambda data, names: (data, 0),
    )
    grounded_markdown = "## 主题1：有正文的降级主题\n\n" + "grounded markdown body " * 10
    empty_json_content = "===MARKDOWN===" + grounded_markdown
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=empty_json_content),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    result = audit_engine.p7a_deep_research_audit(raw)
    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(grounded_markdown)
    assert manifests[-1][1]["status"] == "completed_structured_degraded"
    assert "raw_json_attempt_chars" not in manifests[-1][1]["metrics"]
    assert json_writes and text_writes
    assert set(manifests[-1][1]["files"]) == {"md", "json"}

    markdown_only = "## 主题1：仅 Markdown 的主题\n\n" + "markdown-only response " * 10
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=markdown_only),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    result = audit_engine.p7a_deep_research_audit(raw)
    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(markdown_only)
    assert set(manifests[-1][1]["files"]) == {"md", "json"}

    valid_json_only = '{"topics":[],"padding":"' + ("x" * 150) + '"}===MARKDOWN==='
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: {"topics": []},
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *args, **kwargs: (
            SimpleNamespace(content=valid_json_only),
            _Adapter(),
            {"audit_fallback_used": False},
        ),
    )
    result = audit_engine.p7a_deep_research_audit(raw)
    assert result.startswith("> 【P7a结构化降级】")
    assert result.endswith(raw)
    assert manifests[-1][1]["status"] == "failed_semantic_output"
    assert manifests[-1][1]["metrics"]["semantic_error"] == "empty_markdown_output"
    assert set(manifests[-1][1]["files"]) == {"json"}
