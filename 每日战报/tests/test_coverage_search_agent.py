"""Behavioral coverage for the search transport and P3 collection boundaries."""

from __future__ import annotations

import json
import logging
import runpy

import pytest
import requests

import p3_agent
import search_engine


class _Response:
    def __init__(self, status_code=200, *, text="", data=None):
        self.status_code = status_code
        self.text = text
        self._data = data if data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.text, response=self)

    def json(self):
        return self._data


def _gemini_payload(text="result", *, sources=None):
    chunks = [
        {"web": {"uri": source["url"], "title": source.get("title", "")}}
        for source in (sources or [])
    ]
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "groundingMetadata": {"groundingChunks": chunks},
            }
        ]
    }


def _gemini_impl():
    func = search_engine.gemini_search
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _install_search_route(monkeypatch, responses, *, keys=None, fallback=None, threshold=99):
    calls = []
    response_iter = iter(responses)

    def fake_post(**kwargs):
        calls.append(kwargs)
        value = next(response_iter)
        if isinstance(value, BaseException):
            raise value
        return value

    rotator = search_engine.SearchKeyRotator(keys or ["key_A", "key_B"], "https://example.invalid")
    monkeypatch.setattr(search_engine, "search_rotator_direct", rotator)
    monkeypatch.setattr(
        search_engine,
        "channel_breaker",
        search_engine.ChannelCircuitBreaker(threshold=threshold),
    )
    monkeypatch.setattr(search_engine, "_post_search_request", fake_post)
    monkeypatch.setattr(search_engine, "_tavily_fallback", fallback or (lambda *args, **kwargs: {}))
    monkeypatch.setattr(search_engine.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(search_engine.random, "uniform", lambda low, _high: low)
    monkeypatch.setattr(search_engine, "enrich_search_urls", lambda result: result)
    return calls, rotator


def test_key_rotator_boundaries_and_skip_loop(caplog):
    rotator = search_engine.SearchKeyRotator(["key_A", "key_B", "key_C"], "https://base")
    assert rotator.total == 3
    assert rotator.available == 3

    rotator.mark_exhausted(-1)
    rotator.mark_exhausted(9)
    assert rotator.available == 3

    rotator.mark_exhausted(1)
    rotator._index = 1
    assert rotator.get_client() == ("https://base", "key_C", 2, "ey_C")

    rotator.mark_exhausted(2)
    assert "switch to ...ey_A" in caplog.text
    rotator.mark_exhausted(0)
    assert rotator.get_client() == (None, None, -1, "N/A")
    assert "all 3 keys exhausted" in caplog.text

    assert search_engine.SearchKeyRotator([], "https://base").get_client()[2] == -1

    skip_during_exhaustion = search_engine.SearchKeyRotator(
        ["key_A", "key_B", "key_C"], "https://base"
    )
    skip_during_exhaustion._exhausted.add(1)
    skip_during_exhaustion.mark_exhausted(0)
    assert skip_during_exhaustion._index == 2


def test_circuit_breaker_late_results_trip_and_half_open(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(search_engine.time, "time", lambda: now[0])
    breaker = search_engine.ChannelCircuitBreaker(threshold=2, cooldown_sec=10, max_cooldown_sec=30)

    breaker.record_failure("direct")
    breaker.record_failure("direct")
    opened_at = breaker._opened_at["direct"]
    breaker.record_failure("direct")
    breaker.record_success("direct")
    assert breaker.state("direct") == "open"
    assert breaker._opened_at["direct"] == opened_at
    assert breaker.is_tripped("direct") is True

    now[0] = 111.0
    assert breaker.is_tripped("direct") is False
    assert breaker.state("direct") == "half_open"
    assert breaker.is_tripped("direct") is True

    breaker.record_failure("direct")
    assert breaker.state("direct") == "open"
    assert breaker._cooldowns["direct"] == 30

    breaker.trip("manual")
    assert breaker.state("manual") == "open"
    now[0] = 200.0
    assert breaker.is_tripped("manual") is False
    breaker.record_success("manual")
    assert breaker.state("manual") == "closed"
    assert breaker.is_tripped("new") is False


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        (200, "", ""),
        (408, "timeout", "server_error"),
        (400, "FAILED_PRECONDITION: location is not supported", "unsupported_location"),
        (400, "invalid request", "bad_request"),
        (429, "rate", "quota_exhausted"),
        (503, "unavailable", "server_error"),
        (403, "PERMISSION_DENIED service_disabled", "permission_denied"),
        (403, "RESOURCE_EXHAUSTED quota", "quota_exhausted"),
        (403, "access forbidden", "forbidden"),
        (404, "missing", ""),
    ],
)
def test_search_http_classification(status, text, expected):
    assert search_engine._classify_search_http_response(_Response(status, text=text)) == expected


def test_response_text_redacts_query_key_and_handles_missing_text():
    response = type("R", (), {"text": "failed https://host/path?key=SECRET&x=1"})()
    assert "SECRET" not in search_engine._search_response_text(response)
    assert "key=<redacted>" in search_engine._search_response_text(response)
    assert search_engine._search_response_text(object()) == ""


def test_limited_response_actions(monkeypatch):
    rotator = search_engine.SearchKeyRotator(["key_A", "key_B"], "https://base")
    breaker = search_engine.ChannelCircuitBreaker(threshold=2)
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)

    common = {
        "category": "unit",
        "channel_name": "direct",
        "rotator": rotator,
        "key_idx": 0,
        "key_last4": "ey_A",
    }
    assert search_engine._handle_search_limited_response(response=_Response(200), **common) == ""
    assert (
        search_engine._handle_search_limited_response(response=_Response(429, text="quota"), **common)
        == "quota_exhausted"
    )
    assert rotator.available == 1

    assert (
        search_engine._handle_search_limited_response(response=_Response(400, text="bad"), **common)
        == "bad_request"
    )
    assert breaker.state("direct") == "open"

    other = search_engine.ChannelCircuitBreaker(threshold=2)
    monkeypatch.setattr(search_engine, "channel_breaker", other)
    assert (
        search_engine._handle_search_limited_response(response=_Response(403, text="forbidden"), **common)
        == "forbidden"
    )
    assert other.state("direct") == "closed"
    assert other._failures["direct"] == 1


def test_server_retry_windows_and_logging(monkeypatch, caplog):
    sampled = []
    monkeypatch.setattr(search_engine.random, "uniform", lambda low, high: sampled.append((low, high)) or high)
    assert search_engine._server_error_retry_delay(0) == 6.0
    assert search_engine._server_error_retry_delay(1) == 15.0
    assert search_engine._server_error_retry_delay(99) == 30.0
    assert sampled == [(3.0, 6.0), (8.0, 15.0), (18.0, 30.0)]

    caplog.set_level(logging.WARNING)
    search_engine._log_server_error_retry(
        category="unit",
        channel_name="direct",
        key_last4="ABCD",
        attempt=0,
        max_retries=3,
        delay=3.5,
        prompt="hello",
    )
    assert "retry same key ...ABCD in 3.5s" in caplog.text
    assert "prompt_chars=5" in caplog.text


def test_normalize_and_render_structured_payload():
    payload = search_engine._normalize_p7_structured_payload(
        {
            "summary": " x " * 300,
            "claims": [" claim ", "CLAIM", "", "z" * 300],
            "entities": ["NVIDIA", "nvidia", None],
            "dates": ["2026-07-14"],
            "implications": ["benefits suppliers"],
            "open_questions": ["what next?"],
            "coverage_flags": {"cause": 1, "winners": True},
        }
    )
    assert len(payload["summary"]) == 400
    assert payload["claims"] == ["claim", "z" * 240]
    assert payload["entities"] == ["NVIDIA"]
    assert payload["coverage_flags"] == {
        "cause": True,
        "winners": True,
        "losers": False,
        "alternatives": False,
    }
    rendered = search_engine._render_p7_structured_text(payload)
    assert rendered.startswith("Summary:")
    assert "Dates: 2026-07-14" in rendered
    assert "Implications:" in rendered
    assert "Open questions:" in rendered

    empty = search_engine._normalize_p7_structured_payload(
        {"claims": "not-a-list", "coverage_flags": "not-a-dict"}
    )
    assert empty["claims"] == []
    assert search_engine._render_p7_structured_text(empty) == ""


def test_compact_split_and_parse_tavily_items():
    assert search_engine.compact_search_text("  a\n b  ") == "a b"
    assert search_engine.compact_search_text("abcdef", max_chars=3) == "abc..."
    raw = (
        "ignored preface\n"
        "- First: cause and benefit\n"
        "continued [https://one.example]\n"
        "- second without colon\n"
        "- Third: pressure"
    )
    blocks = search_engine.split_tavily_result_blocks(raw)
    assert len(blocks) == 3
    items = search_engine._parse_tavily_text_items(
        raw,
        sources=[{}, {"url": "https://two.example"}, {"url": "https://three.example"}],
    )
    assert items[0] == {
        "title": "First",
        "content": "cause and benefit continued",
        "url": "https://one.example",
    }
    assert items[1]["title"] == "Tavily result 2"
    assert items[1]["url"] == "https://two.example"
    assert items[2]["url"] == "https://three.example"

    many = "\n".join(f"- T{i}: C{i}" for i in range(8))
    assert len(search_engine._parse_tavily_text_items(many)) == 5
    assert search_engine._parse_tavily_text_items("no blocks") == []


def test_parse_tavily_text_items_ignores_defensive_empty_block(monkeypatch):
    monkeypatch.setattr(search_engine, "split_tavily_result_blocks", lambda _raw: ["- "])
    assert search_engine._parse_tavily_text_items("ignored") == []


def test_tavily_extractors_and_questions():
    text = (
        "[Tavily Fallback] NVIDIA demand is the cause.\n"
        "NVIDIA benefits winners while AMD faces downside pressure; glass is an alternative. "
        "Dates 2026-07-14 and 2026/7/15."
    )
    assert search_engine._extract_tavily_answer(text).startswith("NVIDIA demand")
    assert search_engine._extract_tavily_answer("plain") == ""
    entities = search_engine._extract_tavily_entities(text, limit=3)
    assert "NVIDIA" in entities
    assert len(entities) == 3
    assert search_engine._extract_tavily_dates(text) == ["2026-07-14", "2026/7/15"]
    assert search_engine._extract_tavily_entities("") == []
    assert search_engine._extract_tavily_dates("2026-07-14 2026-07-14") == ["2026-07-14"]
    six_dates = " ".join(f"2026-07-{day:02d}" for day in range(1, 8))
    assert len(search_engine._extract_tavily_dates(six_dates)) == 6

    coverage = search_engine._coverage_from_tavily_text(text)
    assert all(coverage.values())
    assert search_engine._tavily_open_questions(coverage, "query") == []
    questions = search_engine._tavily_open_questions({}, "HBM")
    assert len(questions) == 4
    assert "HBM" in questions[0]


def test_structured_payload_from_tavily_guards_and_items():
    assert search_engine._structured_payload_from_tavily_result({}, query="q", structured_mode=None) is None
    assert search_engine._structured_payload_from_tavily_result([], query="q", structured_mode="p7") is None
    assert (
        search_engine._structured_payload_from_tavily_result(
            {"text": "ordinary"}, query="q", structured_mode="p7"
        )
        is None
    )

    result = {
        "text": "[Tavily Fallback] HBM demand benefits NVIDIA on 2026-07-14.",
        "is_fallback": True,
        "fallback_results": [
            {"title": "HBM", "content": "Demand is the cause and benefits NVIDIA."},
            "ignored",
            {"title": "Glass alternative", "content": ""},
        ],
    }
    payload = search_engine._structured_payload_from_tavily_result(
        result, query="HBM winners alternatives", structured_mode="p7"
    )
    assert payload["summary"].startswith("HBM demand")
    assert payload["claims"] == ["Demand is the cause and benefits NVIDIA.", "Glass alternative"]
    assert payload["implications"] == ["Demand is the cause and benefits NVIDIA."]
    assert payload["dates"] == ["2026-07-14"]

    parsed = search_engine._structured_payload_from_tavily_result(
        {
            "text": "[Tavily Fallback]\n- Item: pressure on losers [https://example.com]",
            "grounding_sources": [],
        },
        query="q",
        structured_mode="p7",
    )
    assert parsed["summary"] == "pressure on losers"
    assert parsed["implications"] == ["pressure on losers"]

    claim_limit = search_engine._structured_payload_from_tavily_result(
        {
            "text": "[Tavily Fallback]",
            "is_fallback": True,
            "fallback_results": [{}]
            + [{"content": f"claim {index}"} for index in range(8)],
        },
        query="q",
        structured_mode="p7",
    )
    assert claim_limit["claims"] == [f"claim {index}" for index in range(6)]


def test_parse_structured_payload_paths(monkeypatch):
    assert search_engine._parse_structured_payload("{}", None) is None
    assert search_engine._parse_structured_payload("   ", "p7") is None
    assert search_engine._parse_structured_payload("not json", "p7") is None

    raw = json.dumps({"summary": "ok", "claims": ["fact"], "coverage_flags": {}})
    parsed = search_engine._parse_structured_payload(raw, "p7")
    assert parsed["summary"] == "ok"
    assert search_engine._parse_structured_payload(raw, "unknown") is None


@pytest.mark.parametrize(
    ("backtrack", "expected"),
    [("0h", "no time restriction"), ("24h", "focus on the last 24 hours"), ("168h", "allow up to the last 168 hours")],
)
def test_build_search_prompt_windows(backtrack, expected):
    prompt = search_engine._build_search_prompt("news", "HBM", backtrack)
    assert expected in prompt
    assert "Topic: HBM" in prompt


def test_build_structured_search_prompt():
    prompt = search_engine._build_search_prompt("deep", "CPO", "", structured_mode="p7")
    assert "Return a JSON object only" in prompt
    assert "coverage_flags" in prompt
    assert "Search window: unlimited" in prompt


def test_tavily_fallback_success_shapes(monkeypatch):
    response = _Response(
        data={
            "answer": "Demand remains strong",
            "results": [
                {"title": "One", "content": "A" * 350, "url": "https://one.example"},
                {"title": "Two", "content": "B", "url": ""},
            ],
        }
    )
    posts = []
    monkeypatch.setattr(search_engine, "TAVILY_API_KEY", "token")
    monkeypatch.setattr(search_engine.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)) or response)
    breaker = search_engine.ChannelCircuitBreaker()
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)

    result = search_engine._tavily_fallback("q" * 500, category="unit", return_dict=True)
    assert result["is_fallback"] is True
    assert result["fallback_provider"] == "tavily"
    assert len(result["fallback_results"][0]["content"]) == 350
    assert result["grounding_sources"] == [{"url": "https://one.example", "title": "One"}]
    assert posts[0][1]["json"]["query"] == "q" * 400
    assert breaker.state("tavily") == "closed"

    text = search_engine._tavily_fallback("query")
    assert text.startswith("[Tavily Fallback] Demand remains strong")


def test_tavily_fallback_absent_empty_and_exception(monkeypatch):
    monkeypatch.setattr(search_engine, "TAVILY_API_KEY", "")
    assert search_engine._tavily_fallback("q") == ""
    assert search_engine._tavily_fallback("q", return_dict=True) == {}

    monkeypatch.setattr(search_engine, "TAVILY_API_KEY", "token")
    monkeypatch.setattr(search_engine.requests, "post", lambda *args, **kwargs: _Response(data={}))
    assert search_engine._tavily_fallback("q") == ""
    assert search_engine._tavily_fallback("q", return_dict=True) == {}

    monkeypatch.setattr(search_engine.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("down")))
    assert search_engine._tavily_fallback("q") == ""
    assert search_engine._tavily_fallback("q", return_dict=True) == {}


def test_extract_gemini_text_and_sources():
    assert search_engine._extract_gemini_text_and_sources({}) == ("", [])
    data = _gemini_payload(
        "grounded",
        sources=[{"url": "https://one.example", "title": "One"}],
    )
    data["candidates"][0]["groundingMetadata"]["groundingChunks"].append({"web": {}})
    assert search_engine._extract_gemini_text_and_sources(data) == (
        "grounded",
        [{"url": "https://one.example", "title": "One"}],
    )


def test_raw_search_empty_then_success(monkeypatch):
    calls, _ = _install_search_route(
        monkeypatch,
        [
            _Response(data=_gemini_payload("   ")),
            _Response(data=_gemini_payload("recovered")),
        ],
    )
    assert search_engine.raw_gemini_search("prompt", max_retries=2) == "recovered"
    assert [call["key"] for call in calls] == ["key_A", "key_A"]


def test_raw_search_quota_rotates_to_next_key(monkeypatch):
    calls, rotator = _install_search_route(
        monkeypatch,
        [
            _Response(429, text="RESOURCE_EXHAUSTED"),
            _Response(data=_gemini_payload("next key works")),
        ],
    )
    result = search_engine.raw_gemini_search("prompt", max_retries=1, return_dict=True)
    assert result["text"] == "next key works"
    assert [call["key"] for call in calls] == ["key_A", "key_B"]
    assert rotator.available == 1


def test_raw_search_connect_timeout_uses_fallback(monkeypatch):
    fallback_result = {"text": "fallback", "grounding_sources": []}
    calls, _ = _install_search_route(
        monkeypatch,
        [requests.exceptions.ConnectTimeout("slow")],
        fallback=lambda *args, **kwargs: fallback_result,
    )
    assert search_engine.raw_gemini_search("prompt", return_dict=True) == fallback_result
    assert len(calls) == 1


def test_raw_search_read_timeout_records_failure_and_skips_key_rotation(monkeypatch):
    fallback_calls = []
    fallback_result = {"text": "fallback", "grounding_sources": []}
    calls, rotator = _install_search_route(
        monkeypatch,
        [requests.exceptions.ReadTimeout("slow response")],
        keys=["key_A", "key_B", "key_C"],
        fallback=lambda *args, **kwargs: fallback_calls.append((args, kwargs)) or fallback_result,
    )

    result = search_engine.raw_gemini_search("prompt", max_retries=3, return_dict=True)

    assert result == fallback_result
    assert [call["key"] for call in calls] == ["key_A"]
    assert rotator.available == 3
    assert search_engine.channel_breaker._failures["direct"] == 1
    assert len(fallback_calls) == 1


def test_raw_search_exception_quota_and_retry_paths(monkeypatch):
    calls, rotator = _install_search_route(
        monkeypatch,
        [RuntimeError("403 quota"), _Response(data=_gemini_payload("rotated"))],
    )
    assert search_engine.raw_gemini_search("prompt", max_retries=1) == "rotated"
    assert [call["key"] for call in calls] == ["key_A", "key_B"]
    assert rotator.available == 1

    calls, _ = _install_search_route(
        monkeypatch,
        [RuntimeError("temporary"), _Response(data=_gemini_payload("retried"))],
    )
    assert search_engine.raw_gemini_search("prompt", max_retries=2) == "retried"
    assert [call["key"] for call in calls] == ["key_A", "key_A"]


def test_raw_search_tripped_no_available_and_failure_shapes(monkeypatch):
    fallback_calls = []
    monkeypatch.setattr(search_engine, "_tavily_fallback", lambda *args, **kwargs: fallback_calls.append(kwargs) or {})
    breaker = search_engine.ChannelCircuitBreaker(threshold=1)
    breaker.trip("direct")
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)
    monkeypatch.setattr(
        search_engine,
        "search_rotator_direct",
        search_engine.SearchKeyRotator(["key_A"], "https://base"),
    )
    assert search_engine.raw_gemini_search("prompt", return_dict=True) == {}
    assert fallback_calls[-1]["return_dict"] is True

    rotator = search_engine.SearchKeyRotator(["key_A"], "https://base")
    rotator.mark_exhausted(0)
    monkeypatch.setattr(search_engine, "search_rotator_direct", rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", search_engine.ChannelCircuitBreaker())
    assert search_engine.raw_gemini_search("prompt") == ""


def test_gemini_search_success_and_structured_result(monkeypatch):
    raw = json.dumps(
        {
            "summary": "HBM demand",
            "claims": ["Demand is rising"],
            "entities": ["HBM"],
            "dates": ["2026-07-14"],
            "implications": ["Suppliers benefit"],
            "open_questions": [],
            "coverage_flags": {"cause": True},
        }
    )
    calls, _ = _install_search_route(
        monkeypatch,
        [_Response(data=_gemini_payload(raw, sources=[{"url": "https://one.example", "title": "One"}]))],
    )
    result = _gemini_impl()("p7", "HBM", structured_mode="p7")
    assert result["structured"]["summary"] == "HBM demand"
    assert result["raw_text"] == raw
    assert result["text"].startswith("Summary: HBM demand")
    assert result["grounding_sources"][0]["url"] == "https://one.example"
    assert calls[0]["temperature"] == 1.0


def test_gemini_search_plain_empty_and_quota_rotation(monkeypatch):
    _install_search_route(monkeypatch, [_Response(data=_gemini_payload("plain"))])
    assert _gemini_impl()("news", "query")["text"] == "plain"

    _install_search_route(monkeypatch, [_Response(data=_gemini_payload("   "))])
    assert _gemini_impl()("news", "query") == {}

    calls, rotator = _install_search_route(
        monkeypatch,
        [_Response(429, text="quota"), _Response(data=_gemini_payload("key B"))],
    )
    assert _gemini_impl()("news", "query", max_retries=1)["text"] == "key B"
    assert [call["key"] for call in calls] == ["key_A", "key_B"]
    assert rotator.available == 1


def test_gemini_search_server_error_builds_structured_fallback(monkeypatch):
    fallback = {
        "text": "[Tavily Fallback] Demand benefits winners.\n- HBM: pressure and alternative glass [https://x]",
        "grounding_sources": [{"url": "https://x", "title": "HBM"}],
        "is_fallback": True,
    }
    calls, _ = _install_search_route(
        monkeypatch,
        [_Response(503, text="unavailable")],
        fallback=lambda *args, **kwargs: dict(fallback),
        threshold=1,
    )
    result = _gemini_impl()("p7", "HBM", max_retries=1, structured_mode="p7")
    assert len(calls) == 1
    assert result["structured"]["summary"] == "Demand benefits winners."
    assert result["raw_text"].startswith("[Tavily Fallback]")
    assert result["text"].startswith("Summary:")


def test_gemini_search_server_retry_then_success_and_bad_request(monkeypatch):
    calls, _ = _install_search_route(
        monkeypatch,
        [_Response(503, text="unavailable"), _Response(data=_gemini_payload("recovered"))],
        threshold=2,
    )
    assert _gemini_impl()("news", "q", max_retries=3)["text"] == "recovered"
    assert [call["key"] for call in calls] == ["key_A", "key_A"]

    fallback = {"text": "fallback", "grounding_sources": []}
    calls, _ = _install_search_route(
        monkeypatch,
        [_Response(400, text="bad request")],
        fallback=lambda *args, **kwargs: fallback,
    )
    assert _gemini_impl()("news", "q") == fallback
    assert len(calls) == 1


def test_gemini_search_connect_timeout_and_generic_exceptions(monkeypatch):
    fallback = {"text": "fallback", "grounding_sources": []}
    _install_search_route(
        monkeypatch,
        [requests.exceptions.ConnectTimeout("slow")],
        fallback=lambda *args, **kwargs: fallback,
    )
    assert _gemini_impl()("news", "q") == fallback

    calls, rotator = _install_search_route(
        monkeypatch,
        [RuntimeError("429 quota"), _Response(data=_gemini_payload("rotated"))],
    )
    assert _gemini_impl()("news", "q", max_retries=1)["text"] == "rotated"
    assert [call["key"] for call in calls] == ["key_A", "key_B"]
    assert rotator.available == 1

    calls, _ = _install_search_route(
        monkeypatch,
        [RuntimeError("temporary"), _Response(data=_gemini_payload("retried"))],
    )
    assert _gemini_impl()("news", "q", max_retries=2)["text"] == "retried"
    assert [call["key"] for call in calls] == ["key_A", "key_A"]


def test_gemini_search_read_timeout_records_failure_and_skips_key_rotation(monkeypatch):
    fallback_calls = []
    fallback_result = {"text": "fallback", "grounding_sources": []}
    calls, rotator = _install_search_route(
        monkeypatch,
        [requests.exceptions.ReadTimeout("slow response")],
        keys=["key_A", "key_B", "key_C"],
        fallback=lambda *args, **kwargs: fallback_calls.append((args, kwargs)) or fallback_result,
    )

    result = _gemini_impl()("news", "q", max_retries=3)

    assert result == fallback_result
    assert [call["key"] for call in calls] == ["key_A"]
    assert rotator.available == 3
    assert search_engine.channel_breaker._failures["direct"] == 1
    assert len(fallback_calls) == 1


def test_gemini_search_tripped_no_key_and_no_fallback(monkeypatch):
    breaker = search_engine.ChannelCircuitBreaker(threshold=1)
    breaker.trip("direct")
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)
    monkeypatch.setattr(
        search_engine,
        "search_rotator_direct",
        search_engine.SearchKeyRotator(["key_A"], "https://base"),
    )
    monkeypatch.setattr(search_engine, "_tavily_fallback", lambda *args, **kwargs: {})
    monkeypatch.setattr(search_engine, "enrich_search_urls", lambda result: result)
    assert _gemini_impl()("news", "q") == {}

    rotator = search_engine.SearchKeyRotator(["key_A"], "https://base")
    rotator.mark_exhausted(0)
    monkeypatch.setattr(search_engine, "search_rotator_direct", rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", search_engine.ChannelCircuitBreaker())
    assert _gemini_impl()("news", "q") == {}


def test_gemini_search_zero_retries_and_exhausted_attempts_advance_key(monkeypatch):
    fallback = {"text": "fallback", "grounding_sources": []}
    calls, _ = _install_search_route(
        monkeypatch,
        [],
        fallback=lambda *args, **kwargs: fallback,
    )
    assert _gemini_impl()("news", "q", max_retries=0) == fallback
    assert calls == []

    calls, _ = _install_search_route(
        monkeypatch,
        [
            RuntimeError("temporary one"),
            RuntimeError("temporary two"),
            _Response(data=_gemini_payload("second key")),
        ],
    )
    assert _gemini_impl()("news", "q", max_retries=2)["text"] == "second key"
    assert [call["key"] for call in calls] == ["key_A", "key_A", "key_B"]


def test_p3_call_search_normalizes_dict_and_text(monkeypatch):
    monkeypatch.setattr(
        p3_agent,
        "raw_gemini_search",
        lambda prompt, max_retries, return_dict: {"text": None, "grounding_sources": None},
    )
    with pytest.raises(RuntimeError, match="empty response"):
        p3_agent._call_gemini_search("q", max_retries=1)
    monkeypatch.setattr(
        p3_agent,
        "raw_gemini_search",
        lambda prompt, max_retries, return_dict: "plain",
    )
    assert p3_agent._call_gemini_search("q") == {"text": "plain", "grounding_sources": []}


def test_p3_url_repair_matching_and_application():
    raw = '{"source_url":" https://example.com/a \n b "}'
    assert p3_agent._repair_source_url_fields(raw) == '{"source_url":"https://example.com/ab"}'
    assert p3_agent._should_replace_source_url("") is True
    assert p3_agent._should_replace_source_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/x") is True
    assert p3_agent._should_replace_source_url("https://publisher.example") is False

    assert p3_agent._match_grounding_source_url({}, [], index=0, total_items=1) == ""
    sources = [{"title": " NVIDIA News ", "url": " https://one.example/path "}]
    assert (
        p3_agent._match_grounding_source_url(
            {"title": "nvidia news"}, sources, index=0, total_items=1
        )
        == "https://one.example/path"
    )
    assert (
        p3_agent._match_grounding_source_url(
            {"title": "unmatched"}, sources, index=0, total_items=2
        )
        == "https://one.example/path"
    )
    positional = [
        {"title": "A", "url": "https://a"},
        {"title": "B", "url": "https://b"},
    ]
    assert (
        p3_agent._match_grounding_source_url({}, positional, index=1, total_items=2)
        == "https://b"
    )
    assert p3_agent._match_grounding_source_url({}, positional, index=0, total_items=3) == ""

    items = p3_agent._apply_grounding_sources(
        [
            "ignored",
            {"title": "A", "source_url": ""},
            {"title": "B", "source_url": " https://publisher.example/x "},
        ],
        positional,
    )
    assert len(items) == 2
    assert items[0]["source_url"] == "https://a"
    assert items[1]["source_url"] == "https://publisher.example/x"


def test_p3_persist_raw_response(monkeypatch, caplog):
    writes = []
    monkeypatch.setattr(p3_agent, "write_text_artifact", lambda prefix, text: writes.append((prefix, text)) or "artifact.txt")
    p3_agent._persist_raw_response("bucket", "bad")
    p3_agent._persist_raw_response("", "worse")
    assert writes == [("P3_raw_response_bucket", "bad"), ("P3_raw_response", "worse")]
    assert "artifact.txt" in caplog.text


def test_p3_parse_tavily_fallback_variants():
    assert p3_agent._parse_tavily_fallback_response("plain", bucket="b") == []
    raw = (
        "[Tavily Fallback] answer\n"
        "- Named: summary [https://one.example]\n"
        "- Without colon\n"
        + "\n".join(f"- T{i}: S{i}" for i in range(3, 9))
    )
    items = p3_agent._parse_tavily_fallback_response(
        raw,
        bucket="b",
        grounding_sources=[{}, {"url": " https://two.example "}],
    )
    assert len(items) == 5
    assert items[0]["title"] == "Named"
    assert items[0]["source_url"] == "https://one.example"
    assert items[1]["source_url"] == "https://two.example"
    assert items[1]["signal_type"] == "b"

    summary = p3_agent._parse_tavily_fallback_response(
        "[Tavily Fallback] only an answer",
        bucket="summary",
        grounding_sources=[{"url": "https://source.example"}],
    )
    assert len(summary) == 1
    assert summary[0]["summary"] == "only an answer"
    assert summary[0]["source_url"] == "https://source.example"
    assert p3_agent._parse_tavily_fallback_response("[Tavily Fallback]", bucket="empty") == []


def test_p3_tavily_parser_defensive_empty_blocks(monkeypatch):
    monkeypatch.setattr(p3_agent, "split_tavily_result_blocks", lambda _raw: ["- ", "- x"])
    monkeypatch.setattr(p3_agent, "compact_search_text", lambda *args, **kwargs: "")
    assert p3_agent._parse_tavily_fallback_response("[Tavily Fallback]", bucket="empty") == []


def test_p3_parse_json_response_success_fallback_repair_and_failures(monkeypatch):
    assert p3_agent._parse_json_response("") == []
    raw = '[{"title":"A","source_url":""}]'
    assert p3_agent._parse_json_response(
        raw, grounding_sources=[{"title": "A", "url": "https://a"}]
    )[0]["source_url"] == "https://a"

    fallback = p3_agent._parse_json_response(
        "[Tavily Fallback] answer\n- T: S [https://t]", bucket="b"
    )
    assert fallback[0]["source_url"] == "https://t"

    decoder_error = json.JSONDecodeError("bad", "x", 0)
    calls = []

    def repaired_extract(text, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            raise decoder_error
        return [{"title": "fixed", "source_url": "https://a"}]

    monkeypatch.setattr(p3_agent, "extract_json_from_llm_response", repaired_extract)
    repaired_raw = '[{"source_url":"https://exa\n mple.com"}]'
    assert p3_agent._parse_json_response(repaired_raw)[0]["title"] == "fixed"
    assert "\n" not in calls[1]

    persisted = []
    monkeypatch.setattr(p3_agent, "_persist_raw_response", lambda bucket, text: persisted.append((bucket, text)))
    monkeypatch.setattr(
        p3_agent,
        "extract_json_from_llm_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(decoder_error),
    )
    assert p3_agent._parse_json_response("invalid", bucket="bad") == []
    assert persisted[-1] == ("bad", "invalid")
    assert p3_agent._parse_json_response(repaired_raw, bucket="twice") == []
    assert persisted[-1] == ("twice", repaired_raw)

    monkeypatch.setattr(p3_agent, "extract_json_from_llm_response", lambda *args, **kwargs: {"not": "list"})
    assert p3_agent._parse_json_response("{}") == []


def test_p3_parse_search_result_and_format_lines(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        p3_agent,
        "_parse_json_response",
        lambda text, **kwargs: captured.update({"text": text, **kwargs}) or [{"ok": True}],
    )
    assert p3_agent._parse_search_result(
        {"text": "raw", "grounding_sources": [{"url": "x"}]}, "bucket"
    ) == [{"ok": True}]
    assert captured == {
        "text": "raw",
        "bucket": "bucket",
        "grounding_sources": [{"url": "x"}],
    }

    lines = p3_agent._format_failed_article_lines(
        [{"title": "A", "url": "u", "source": "s"}, {}]
    )
    assert "1." in lines and "A" in lines and "u" in lines and "s" in lines
    assert "2." in lines
    assert p3_agent._format_failed_article_lines([]) == ""


def test_p3_filter_priority_targets(monkeypatch):
    monkeypatch.setattr(p3_agent, "JINNANG_1_RECOVERY_SOURCE_KEYS", {"eligible"})
    monkeypatch.setattr(p3_agent, "PRIORITY_KEYWORDS", ["HBM", "NVIDIA"])
    assert p3_agent._filter_priority_targets([]) == []

    items = [
        {"source": "other", "title": "HBM NVIDIA"},
        {"source": "eligible", "title": "ordinary"},
        {"source": "Eligible", "title": "HBM"},
        {"source": "eligible", "title": "NVIDIA HBM"},
    ]
    selected = p3_agent._filter_priority_targets(items, max_targets=1)
    assert selected == [items[3]]
    assert p3_agent._filter_priority_targets(items[:2], max_targets=3) == []


def test_p3_run_jinnang_1_empty_single_and_batched(monkeypatch):
    monkeypatch.setattr(p3_agent, "_filter_priority_targets", lambda failed: [])
    assert p3_agent.run_jinnang_1([{"x": 1}]) == []

    targets = [{"title": "A", "url": "u", "source": "s"}]
    prompts = []
    monkeypatch.setattr(p3_agent, "_filter_priority_targets", lambda failed: targets)
    monkeypatch.setattr(p3_agent, "load_prompt", lambda name: "{failed_articles}|{current_time}")
    monkeypatch.setattr(p3_agent, "_call_gemini_search", lambda prompt: prompts.append(prompt) or {"text": "[]"})
    monkeypatch.setattr(p3_agent, "_parse_search_result", lambda result, bucket: [{"bucket": bucket}])
    assert p3_agent.run_jinnang_1([{"x": 1}]) == [{"bucket": "jinnang_1"}]
    assert len(prompts) == 1 and "A" in prompts[0]

    targets = [{"title": str(index), "url": "u", "source": "s"} for index in range(6)]
    sleeps = []
    monkeypatch.setattr(p3_agent, "_filter_priority_targets", lambda failed: targets)
    monkeypatch.setattr(p3_agent.time, "sleep", sleeps.append)
    prompts.clear()
    results = p3_agent.run_jinnang_1([{"x": 1}])
    assert results == [{"bucket": "jinnang_1"}, {"bucket": "jinnang_1"}]
    assert len(prompts) == 2
    assert sleeps == [2]


def test_p3_run_jinnang_2_and_3(monkeypatch):
    prompts = []
    monkeypatch.setattr(p3_agent, "load_prompt", lambda name: f"{name} {{hours}} {{current_time}}")
    monkeypatch.setattr(p3_agent, "_call_gemini_search", lambda prompt: prompts.append(prompt) or {"text": "[]"})
    monkeypatch.setattr(p3_agent, "_parse_search_result", lambda result, bucket: [{"bucket": bucket}])
    assert p3_agent.run_jinnang_2(36) == [{"bucket": "jinnang_2"}]
    assert p3_agent.run_jinnang_3(48) == [{"bucket": "jinnang_3"}]
    assert "36" in prompts[0] and "48" in prompts[1]
    assert all("JSON output hard limits" in prompt for prompt in prompts)


def test_p3_render_output_empty_and_success(monkeypatch):
    monkeypatch.setattr(p3_agent, "normalize_p3_structured_items", lambda raw: [])
    assert p3_agent.render_p3_output({}) == ("", [])

    items = [{"title": "A"}]
    monkeypatch.setattr(p3_agent, "normalize_p3_structured_items", lambda raw: items)
    monkeypatch.setattr(
        p3_agent,
        "render_p3_structured_markdown",
        lambda structured, collected_at=None: f"rendered:{collected_at}:{len(structured)}",
    )
    assert p3_agent.render_p3_output({}, collected_at="now") == ("rendered:now:1", items)


def test_p3_search_distinguishes_route_failure_from_valid_empty_json(monkeypatch):
    monkeypatch.setattr(p3_agent, "raw_gemini_search", lambda *_args, **_kwargs: {})
    with pytest.raises(RuntimeError, match="empty response"):
        p3_agent._call_gemini_search("prompt")

    monkeypatch.setattr(
        p3_agent,
        "raw_gemini_search",
        lambda *_args, **_kwargs: {"text": "[]", "grounding_sources": []},
    )
    assert p3_agent._call_gemini_search("prompt")["text"] == "[]"


def test_p3_collect_parallel_no_key_empty_partial_failure_and_success(monkeypatch):
    monkeypatch.setattr(p3_agent, "SEARCH_API_KEY_POOL", [])
    text, stats = p3_agent.collect_p3_parallel_only()
    assert text == ""
    assert stats["status"] == "skipped_no_key"

    monkeypatch.setattr(p3_agent, "SEARCH_API_KEY_POOL", ["key"])
    monkeypatch.setattr(p3_agent, "parse_backtrack_hours", lambda value: 24)
    monkeypatch.setattr(p3_agent, "run_jinnang_2", lambda hours: [])
    monkeypatch.setattr(p3_agent, "run_jinnang_3", lambda hours: [])
    text, stats = p3_agent.collect_p3_parallel_only("24h")
    assert text == ""
    assert stats["status"] == "empty"
    assert stats["raw_results"]["jinnang_2"] == []

    def fail(_hours):
        raise RuntimeError("isolated failure")

    monkeypatch.setattr(p3_agent, "run_jinnang_2", fail)
    monkeypatch.setattr(p3_agent, "run_jinnang_3", lambda hours: [{"title": "B"}])
    monkeypatch.setattr(p3_agent, "render_p3_output", lambda raw: ("report", [{"title": "B"}]))
    text, stats = p3_agent.collect_p3_parallel_only("24h")
    assert text == "report"
    assert stats["status"] == "partial_query_errors"
    assert stats["query_failures"] == 1
    assert stats["jinnang_2_count"] == 0
    assert stats["jinnang_3_count"] == 1
    assert stats["content_length"] == 6
    assert stats["structured_items"] == 1

    monkeypatch.setattr(p3_agent, "run_jinnang_2", lambda hours: [{"title": "A"}])
    monkeypatch.setattr(p3_agent, "run_jinnang_3", fail)
    text, stats = p3_agent.collect_p3_parallel_only("24h")
    assert text == "report"
    assert stats["status"] == "partial_query_errors"
    assert stats["jinnang_2_count"] == 1
    assert stats["jinnang_3_count"] == 0

    monkeypatch.setattr(p3_agent, "run_jinnang_2", fail)
    monkeypatch.setattr(p3_agent, "run_jinnang_3", fail)
    text, stats = p3_agent.collect_p3_parallel_only("24h")
    assert text == ""
    assert stats["status"] == "failed_all_queries"
    assert stats["query_failures"] == 2


def test_p3_module_entrypoint_smoke(monkeypatch, capsys):
    import settings

    monkeypatch.setattr(settings, "SEARCH_API_KEY_POOL", [])
    runpy.run_path(p3_agent.__file__, run_name="__main__")
    output = capsys.readouterr().out
    assert "Testing P3 parallel search" in output
    assert "Stats:" in output
    assert "skipped_no_key" in output
