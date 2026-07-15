# -*- coding: utf-8 -*-
"""Behavioral branch coverage for P1 web, P2 X intel, and P4 market sources."""

from __future__ import annotations

import sys
import runpy
from datetime import datetime
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
import requests

import p1_web
import p2_x_intel
import p4_vanguard


class FakeResponse:
    def __init__(self, *, status_code=200, text="", payload=None, http_error=None):
        self.status_code = status_code
        self.text = text
        self.payload = payload
        self.http_error = http_error

    def raise_for_status(self):
        if self.http_error is not None:
            raise self.http_error
        if self.status_code >= 400:
            response = SimpleNamespace(status_code=self.status_code)
            raise requests.exceptions.HTTPError(str(self.status_code), response=response)

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeNode:
    def __init__(self, text="", **attrs):
        self.text = text
        self.attrib = attrs


class InlineFuture:
    def __init__(self, fn, args):
        self.cancelled = False
        try:
            self.value = fn(*args)
            self.error = None
        except Exception as exc:
            self.value = None
            self.error = exc

    def result(self):
        if self.error:
            raise self.error
        return self.value

    def cancel(self):
        self.cancelled = True
        return True


class InlineExecutor:
    instances = []

    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.futures = []
        self.shutdown_args = None
        self.__class__.instances.append(self)

    def submit(self, fn, *args):
        future = InlineFuture(fn, args)
        self.futures.append(future)
        return future

    def shutdown(self, *, wait, cancel_futures):
        self.shutdown_args = (wait, cancel_futures)


def _all_done(futures, **_kwargs):
    return set(futures), set()


def test_web_source_candidates_and_navigation_urls():
    source = {
        "url": "https://example.com/main",
        "fallback_urls": ["", "https://example.com/main", "https://example.com/fallback"],
    }
    assert p1_web._source_candidate_urls(source) == [
        "https://example.com/main",
        "https://example.com/fallback",
    ]
    assert p1_web._is_known_navigation_url("ASML_News", "") is False
    assert p1_web._is_known_navigation_url("ASML_News", "https://asml.com/en/news/") is True
    assert p1_web._is_known_navigation_url("Other", "https://asml.com/en/news") is False
    assert p1_web._is_known_navigation_url("ASML_News", "https://asml.com/en/news/article") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (" Published | • : ", ""),
        ("2026/07/14", "2026-07-14T23:59:59 (北京时间)"),
        ("2026.07.14 03:04", "2026-07-14T03:04:00 (北京时间)"),
        ("2026-07-14T00:00:00Z", "2026-07-14T08:00:00 (北京时间)"),
        ("Jul 14, 2026 03:04 PM", "2026-07-14T15:04:00 (北京时间)"),
        ("Updated July 14, 2026", "2026-07-14T00:00:00 (北京时间)"),
        ("not-a-date", ""),
    ],
)
def test_normalize_web_datetime_variants(raw, expected):
    assert p1_web._normalize_web_datetime(raw) == expected


def test_extract_web_published_at_from_context_and_urls(monkeypatch):
    html = "<div>Published 2026-07-14 03:04 UTC <a href='/story'>Story title</a></div>"
    assert p1_web._extract_web_published_at(html, "/story", "https://example.com/story", "Story title") == (
        "2026-07-14T11:04:00 (北京时间)"
    )
    assert p1_web._extract_web_published_at("", "https://example.com/2026/07/13/story", "", "") == (
        "2026-07-13T23:59:59 (北京时间)"
    )
    assert p1_web._extract_web_published_at("", "", "https://example.com/news-20260712", "") == (
        "2026-07-12T23:59:59 (北京时间)"
    )

    monkeypatch.setattr(p1_web, "_normalize_web_datetime", lambda _raw: "")
    assert p1_web._extract_web_published_at("2026-07-14 title", "title", "", "title") == ""
    assert p1_web._extract_web_published_at("", "", "", "no date") == ""


def test_classify_web_time_window_all_statuses(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 14, 12, 0, 0, tzinfo=p1_web.BEIJING_TZ)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(p1_web, "datetime", FrozenDateTime)
    assert p1_web._classify_time_window(None, 24)["time_status"] == "unknown_time"
    assert p1_web._classify_time_window("2026-07-14T10:00:00 (北京时间)", 24) == {
        "allowed": True,
        "time_status": "recent",
        "time_reason": "",
    }
    assert p1_web._classify_time_window("2026-07-10T10:00:00 (北京时间)", 24)["time_status"] == "stale"
    assert p1_web._classify_time_window("2026-07-14T13:00:00 (北京时间)", 24)["time_status"] == "future"
    invalid = p1_web._classify_time_window("2030-07-14T10:00:00 (北京时间)", 24)
    assert invalid["time_status"] == "invalid_time" and "2030" in invalid["time_reason"]
    assert p1_web._classify_time_window("bad", 24)["time_status"] == "parse_failed"


def test_scrape_one_source_requires_selector(caplog):
    assert p1_web._scrape_one_source({"url": "https://example.com", "name": "Missing"}) == []
    assert "未配置 CSS 选择器" in caplog.text


def test_scrape_one_source_falls_through_network_parser_and_selector_errors(monkeypatch):
    urls = ["timeout", "http", "unknown-http", "other", "parse", "css", "empty", "success"]
    source = {
        "name": "ASML_News",
        "url": urls[0],
        "fallback_urls": urls[1:],
        "selector": ".item",
        "max_items": 10,
        "timeout": 7,
    }
    calls = []
    monkeypatch.setattr(
        p1_web,
        "get_tls_policy_for_url",
        lambda url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False, "url": url},
    )
    monkeypatch.setattr(p1_web, "log_tls_policy", lambda *_args: None)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url == "timeout":
            raise requests.exceptions.Timeout("slow")
        if url == "http":
            return FakeResponse(status_code=403)
        if url == "unknown-http":
            return FakeResponse(http_error=requests.exceptions.HTTPError("unknown"))
        if url == "other":
            raise RuntimeError("network failed")
        return FakeResponse(text=url)

    class Page:
        def __init__(self, kind):
            self.kind = kind

        def css(self, selector):
            if self.kind == "css" and selector == ".item":
                raise ValueError("bad primary selector")
            if self.kind == "empty":
                if selector == "h2 a":
                    raise ValueError("bad fallback")
                if selector == "h3 a":
                    return [FakeNode("one")]
                return []
            if self.kind == "success":
                return [
                    FakeNode("Valid article title", href="/article"),
                    FakeNode("bad", href="/short"),
                    FakeNode("Navigation page", href="/en/news"),
                    FakeNode("Blocked article", href="/blocked"),
                    FakeNode("No link article"),
                ]
            return []

    def fake_selector(html):
        if html == "parse":
            raise ValueError("invalid html")
        return Page(html)

    monkeypatch.setattr(p1_web, "_safe_external_requests_get", fake_get)
    monkeypatch.setattr(p1_web, "Selector", fake_selector)
    monkeypatch.setattr(p1_web, "_is_article_url", lambda url: "blocked" not in url)
    monkeypatch.setattr(p1_web, "_extract_web_published_at", lambda _html, _raw, _link, title: f"date:{title}")

    entries = p1_web._scrape_one_source(source)
    assert [entry["title"] for entry in entries] == ["Valid article title", "No link article"]
    assert entries[0]["url"] == "/article"
    assert entries[0]["published_at"] == "date:Valid article title"
    assert entries[0]["tls_mode"] == "strict"
    assert entries[1]["url"] == ""
    assert calls == [(url, 7) for url in urls]


def test_scrape_one_source_uses_fallback_selector_and_attribute_title(monkeypatch):
    nodes = [
        FakeNode("ignored text", title="Attribute title one", data_url="/one"),
        FakeNode("ignored text", title="Attribute title two", data_url="/two"),
        FakeNode("ignored text", title="Attribute title three", data_url="/three"),
        SimpleNamespace(text="missing attributes"),
    ]

    class Page:
        def css(self, selector):
            if selector == ".primary":
                return []
            if selector == "h2 a":
                return nodes
            return []

    monkeypatch.setattr(
        p1_web,
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "compat", "insecure_fetch": True},
    )
    monkeypatch.setattr(p1_web, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(p1_web, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(text="html"))
    monkeypatch.setattr(p1_web, "Selector", lambda _html: Page())
    monkeypatch.setattr(p1_web, "_is_article_url", lambda _url: True)
    monkeypatch.setattr(p1_web, "_extract_web_published_at", lambda *_args: "")
    entries = p1_web._scrape_one_source(
        {
            "name": "AttrSource",
            "url": "https://example.com/list",
            "selector": ".primary",
            "title_attr": "title",
            "link_attr": "data_url",
        }
    )
    assert len(entries) == 3
    assert entries[0]["title"] == "Attribute title one"
    assert entries[0]["url"] == "https://example.com/one"
    assert entries[0]["insecure_fetch"] is True


def test_scrape_one_source_returns_empty_when_all_elements_are_invalid(monkeypatch):
    monkeypatch.setattr(
        p1_web,
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
    )
    monkeypatch.setattr(p1_web, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(p1_web, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(text="html"))
    monkeypatch.setattr(
        p1_web,
        "Selector",
        lambda _html: SimpleNamespace(css=lambda _selector: [FakeNode("bad", href="/short")]),
    )
    assert p1_web._scrape_one_source(
        {"name": "Invalid", "url": "https://example.com", "selector": ".item"}
    ) == []


def test_web_result_formatting_time_stats_and_enrich_helpers(monkeypatch):
    def classify(value, _hours):
        if value == "old":
            return {"allowed": False, "time_status": "stale", "time_reason": ""}
        if value == "bad":
            return {"allowed": False, "time_status": "parse_failed", "time_reason": "bad"}
        return {"allowed": True, "time_status": "recent" if value else "unknown_time", "time_reason": ""}

    monkeypatch.setattr(p1_web, "_classify_time_window", classify)
    entries = [
        {"title": "Same", "content": "Same", "url": "", "published_at": "", "source": "feed"},
        {
            "title": "Detailed",
            "content": "Useful body",
            "url": "https://example.com/a",
            "published_at": "new",
            "source": "feed",
            "tls_mode": "compat",
            "insecure_fetch": True,
        },
        {"title": "Old", "content": "", "url": "https://example.com/old", "published_at": "old"},
        {"title": "Bad", "content": "", "url": "https://example.com/bad", "published_at": "bad"},
    ]
    result = p1_web._entries_to_p1_result(entries, "Category", "Source", 24)
    assert "[unknown_time]" in result["text"]
    assert "Detailed — Useful body [new]" in result["text"]
    assert len(result["grounding_sources"]) == 1
    assert result["grounding_sources"][0]["insecure_fetch"] is True
    assert result["time_window_stats"] == {
        "recent": 1,
        "unknown_time": 1,
        "stale": 1,
        "parse_failed": 1,
        "future": 0,
        "invalid_time": 0,
    }
    assert p1_web._entries_to_p1_result(entries[2:], "Category", "Source", 24) is None

    stats = {key: 9 for key, _source in p1_web._WEB_TIME_STAT_KEYS}
    p1_web._reset_web_time_stats(stats)
    assert all(stats[key] == 0 for key, _source in p1_web._WEB_TIME_STAT_KEYS)
    p1_web._add_web_time_stats(stats, {"recent": 2, "future": 1})
    assert stats["time_recent"] == 2 and stats["time_future"] == 1
    p1_web._rebuild_web_time_stats(stats, [result, {"time_window_stats": {"invalid_time": 3}}])
    assert stats["time_recent"] == 1 and stats["time_invalid"] == 3

    assert p1_web._normalize_enrich_result(("body", {"published_at": "date"})) == (
        "body",
        {"published_at": "date"},
    )
    assert p1_web._normalize_enrich_result((None, "bad meta")) == ("", {})
    assert p1_web._normalize_enrich_result("plain") == ("plain", {})

    entry = {"published_at": "existing"}
    p1_web._fill_entry_published_at_from_meta(entry, {"published_at": "replacement"})
    assert entry["published_at"] == "existing"
    entry = {"published_at": ""}
    monkeypatch.setattr(p1_web, "_normalize_web_datetime", lambda value: "normalized" if value else "")
    p1_web._fill_entry_published_at_from_meta(entry, "not a dict")
    assert entry["published_at"] == ""
    p1_web._fill_entry_published_at_from_meta(entry, {"published_at": "hint"})
    assert entry["published_at"] == "normalized"


def test_collect_from_web_early_exits(monkeypatch):
    monkeypatch.setattr(p1_web, "_parser_available", False)
    results, stats = p1_web.collect_from_web("24h")
    assert results == [] and stats["total"] == 0

    monkeypatch.setattr(p1_web, "_parser_available", True)
    monkeypatch.setattr(p1_web, "V2_WEB_SOURCES", [])
    results, stats = p1_web.collect_from_web("24h")
    assert results == [] and stats["total"] == 0


def test_collect_from_web_mixed_phase_results(monkeypatch):
    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_web, "_parser_available", True)
    monkeypatch.setattr(
        p1_web,
        "V2_WEB_SOURCES",
        [
            {"name": "Good", "category": "News"},
            {"name": "Empty", "category": "News"},
            {"name": "Stale", "category": "News"},
            {"name": "Error", "category": "News"},
        ],
    )

    def scrape(source):
        name = source["name"]
        if name == "Empty":
            return []
        if name == "Error":
            raise RuntimeError("source failed")
        if name == "Stale":
            return [{"title": "Old", "content": "", "url": "https://example.com/old", "published_at": "old", "source": name}]
        return [
            {"title": "Deep", "content": "", "url": "https://example.com/deep", "published_at": "", "source": name},
            {"title": "Empty body", "content": "", "url": "https://example.com/empty", "published_at": "new", "source": name},
            {"title": "Worker error", "content": "", "url": "https://example.com/error", "published_at": "new", "source": name},
            {"title": "No URL", "content": "kept", "url": "", "published_at": "new", "source": name},
        ]

    def classify(value, _hours):
        if value == "old":
            return {"allowed": False, "time_status": "stale", "time_reason": ""}
        return {"allowed": True, "time_status": "recent" if value else "unknown_time", "time_reason": ""}

    def enrich(url, _source, return_meta=False):
        assert return_meta is True
        if url.endswith("error"):
            raise RuntimeError("enrich failed")
        if url.endswith("deep"):
            return "deep content", {"published_at": "2026-07-14"}
        return "", {}

    monkeypatch.setattr(p1_web, "_scrape_one_source", scrape)
    monkeypatch.setattr(p1_web, "_classify_time_window", classify)
    monkeypatch.setattr(p1_web, "_normalize_web_datetime", lambda value: "new" if value else "")
    monkeypatch.setattr(p1_web, "_is_article_url", lambda _url: True)
    monkeypatch.setattr(p1_web, "enrich_article_url", enrich)
    monkeypatch.setattr(p1_web, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_web, "wait", _all_done)
    monkeypatch.setattr(p1_web, "parse_backtrack_hours", lambda _value: 24)
    monkeypatch.setattr(p1_web.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p1_web.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_web.random, "shuffle", lambda _items: None)

    results, stats = p1_web.collect_from_web("24h")
    assert stats["total"] == 4
    assert stats["success"] == [("Good", 3)]
    assert {name for name, _reason in stats["empty"]} == {"Empty", "Stale"}
    assert stats["failed"] == [("Error", "source failed")]
    assert stats["enrich_ok"] == 1 and stats["enrich_fail"] == 2
    assert len(results) == 1
    assert "Deep — deep content [new]" in results[0]["text"]
    assert "No URL — kept" in results[0]["text"]
    assert not any(key.startswith("_") for key in results[0])
    assert [executor.max_workers for executor in InlineExecutor.instances] == [4, 10]
    assert all(executor.shutdown_args == (False, True) for executor in InlineExecutor.instances)


def test_collect_from_web_phase_timeouts(monkeypatch):
    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_web, "_parser_available", True)
    monkeypatch.setattr(p1_web, "V2_WEB_SOURCES", [{"name": "Good"}, {"name": "Late"}])
    monkeypatch.setattr(
        p1_web,
        "_scrape_one_source",
        lambda source: [{"title": source["name"], "content": "", "url": "https://example.com/a", "published_at": "new", "source": source["name"]}],
    )
    monkeypatch.setattr(
        p1_web,
        "_classify_time_window",
        lambda *_args: {"allowed": True, "time_status": "recent", "time_reason": ""},
    )
    monkeypatch.setattr(p1_web, "_is_article_url", lambda _url: True)
    monkeypatch.setattr(p1_web, "enrich_article_url", lambda *_args, **_kwargs: ("late content", {}))
    monkeypatch.setattr(p1_web, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_web, "parse_backtrack_hours", lambda _value: 24)
    monkeypatch.setattr(p1_web.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p1_web.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_web.random, "shuffle", lambda _items: None)
    calls = 0

    def partial_wait(futures, **_kwargs):
        nonlocal calls
        calls += 1
        futures = list(futures)
        if calls == 1:
            return {futures[0]}, set(futures[1:])
        return set(), set(futures)

    monkeypatch.setattr(p1_web, "wait", partial_wait)
    results, stats = p1_web.collect_from_web("24h")
    assert results and "Good" in results[0]["text"]
    assert stats["enrich_fail"] == 1
    assert any(name == "Late" and "超时" in reason for name, reason in stats["failed"])
    assert InlineExecutor.instances[0].futures[1].cancelled is True
    assert InlineExecutor.instances[1].futures[0].cancelled is True


def test_collect_from_web_future_error_and_no_enrichment_work(monkeypatch):
    class ErrorFuture:
        def result(self):
            raise RuntimeError("future failed")

        def cancel(self):
            return True

    class ErrorExecutor:
        def __init__(self, max_workers):
            assert max_workers == 4

        def submit(self, _fn, *_args):
            return ErrorFuture()

        def shutdown(self, *, wait, cancel_futures):
            assert (wait, cancel_futures) == (False, True)

    monkeypatch.setattr(p1_web, "_parser_available", True)
    monkeypatch.setattr(p1_web, "V2_WEB_SOURCES", [{"name": "Broken"}])
    monkeypatch.setattr(p1_web, "ThreadPoolExecutor", ErrorExecutor)
    monkeypatch.setattr(p1_web, "wait", _all_done)
    monkeypatch.setattr(p1_web.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p1_web.random, "uniform", lambda *_args: 0)
    results, stats = p1_web.collect_from_web("24h")
    assert results == [] and stats["failed"] == [("Broken", "future failed")]

    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_web, "V2_WEB_SOURCES", [{"name": "NoURL", "category": "News"}])
    monkeypatch.setattr(
        p1_web,
        "_scrape_one_source",
        lambda _source: [{"title": "No URL", "content": "body", "url": "", "published_at": "", "source": "NoURL"}],
    )
    monkeypatch.setattr(
        p1_web,
        "_classify_time_window",
        lambda *_args: {"allowed": True, "time_status": "recent", "time_reason": ""},
    )
    monkeypatch.setattr(p1_web, "ThreadPoolExecutor", InlineExecutor)
    results, stats = p1_web.collect_from_web("24h")
    assert len(results) == 1 and "No URL — body" in results[0]["text"]
    assert "enrich_ok" not in stats


def test_p1_web_script_entry_with_fake_http(monkeypatch, capsys):
    import config
    import scraper_utils

    fake_scrapling = ModuleType("scrapling")

    class Page:
        def __init__(self, _html):
            pass

        def css(self, _selector):
            return [
                FakeNode(f"Article title {index}", href=f"/article-{index}")
                for index in range(1, 5)
            ]

    fake_scrapling.Selector = Page
    sources = [{"name": "Entrypoint", "category": "News", "url": "https://example.com/list", "selector": ".item"}]
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setattr(config, "WEB_SCRAPE_SOURCES", sources)
    monkeypatch.setattr(scraper_utils, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(text="listing"))
    monkeypatch.setattr(scraper_utils, "is_article_url", lambda _url: True)
    monkeypatch.setattr(
        scraper_utils,
        "enrich_article_url",
        lambda *_args, **_kwargs: ("entrypoint deep content", {}),
    )
    monkeypatch.setattr(p1_web.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p1_web.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_web.random, "shuffle", lambda _items: None)
    monkeypatch.setattr(
        sys.modules["http_security"],
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
    )
    monkeypatch.setattr(sys.modules["http_security"], "log_tls_policy", lambda *_args: None)

    runpy.run_module("p1_web", run_name="__main__")
    output = capsys.readouterr().out
    assert "网页采集结果: 1 个数据源" in output
    assert "... 还有 1 条" in output
    assert "总计: 4 条新闻" in output
    assert "成功 1 | 空 0 | 失败 0" in output


def test_p2_environment_prompt_budget_and_sleep_helpers(monkeypatch):
    monkeypatch.delenv("TEST_POSITIVE_INT", raising=False)
    assert p2_x_intel._env_positive_int("TEST_POSITIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_POSITIVE_INT", "bad")
    assert p2_x_intel._env_positive_int("TEST_POSITIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_POSITIVE_INT", "0")
    assert p2_x_intel._env_positive_int("TEST_POSITIVE_INT", 7) == 7
    monkeypatch.setenv("TEST_POSITIVE_INT", "12")
    assert p2_x_intel._env_positive_int("TEST_POSITIVE_INT", 7) == 12

    prompts = []

    def require_prompt(name):
        prompts.append(name)
        return "hours={hours}\nkeywords:\n{keyword_section}\ntime={current_time}"

    monkeypatch.setattr(p2_x_intel, "require_prompt", require_prompt)
    monkeypatch.setattr(p2_x_intel, "parse_backtrack_hours", lambda value: 72 if value == "3d" else 24)
    monkeypatch.setattr(p2_x_intel, "INDUSTRY_SCAN_KEYWORDS", ["one", "two"])
    prompt = p2_x_intel._build_user_prompt("3d", ["custom"])
    assert "hours=72" in prompt and "- custom" in prompt
    assert "- one" in p2_x_intel._build_user_prompt()
    assert prompts == ["p2_user.txt", "p2_user.txt"]

    monkeypatch.setattr(p2_x_intel.time, "time", lambda: 110.0)
    assert p2_x_intel._remaining_budget_seconds(100.0, 0) == float("inf")
    assert p2_x_intel._remaining_budget_seconds(100.0, 20) == 10.0

    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: float("inf"))
    assert p2_x_intel._bounded_request_timeout(0, 0, 33) == 33.0
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 10)
    assert p2_x_intel._bounded_request_timeout(0, 100, 33) == 0
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 50)
    assert p2_x_intel._bounded_request_timeout(0, 100, 100) == 40.0
    assert p2_x_intel._bounded_request_timeout(0, 100, 3) == 5.0

    sleeps = []
    monkeypatch.setattr(p2_x_intel.time, "sleep", sleeps.append)
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: float("inf"))
    assert p2_x_intel._sleep_with_budget(3, 0, 0) is True
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 12)
    assert p2_x_intel._sleep_with_budget(8, 0, 100) is True
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 10)
    assert p2_x_intel._sleep_with_budget(8, 0, 100) is False
    assert sleeps == [3, 2]


def test_filter_x_leak_blocks_rejects_duplicate_and_fake_ids():
    seen = {"9081726354"}
    content = """
outside
<x_leaks><item>valid https://x.com/analyst/status/9182736450</item></x_leaks>
<x_leaks><item>duplicate https://twitter.com/a/status/9081726354</item></x_leaks>
<x_leaks><item>fake https://x.com/a/status/7777111122</item></x_leaks>
<x_leaks><item>no URL remains valid</item></x_leaks>
"""
    filtered = p2_x_intel._filter_x_leak_blocks(content, seen)
    assert "9182736450" in filtered
    assert "no URL remains valid" in filtered
    assert "duplicate" not in filtered
    assert "fake" not in filtered
    assert seen == {"9081726354", "9182736450", "7777111122"}
    assert p2_x_intel._filter_x_leak_blocks("plain text", set()) == ""


def _configure_p2(monkeypatch, *, keywords=None, reasoning="high"):
    manifests = []
    monkeypatch.setattr(p2_x_intel, "GROK_API_KEY", "test-key")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", "")
    monkeypatch.setattr(p2_x_intel, "GROK_MODEL", "grok-test")
    monkeypatch.setattr(p2_x_intel, "GROK_REASONING_EFFORT", reasoning)
    monkeypatch.setattr(p2_x_intel, "GROK_MAX_OUTPUT_TOKENS", 1234)
    monkeypatch.setattr(p2_x_intel, "INDUSTRY_SCAN_KEYWORDS", keywords or ["kw1"])
    monkeypatch.setattr(p2_x_intel, "OLIGARCH_DICT", {"Track": ["Alpha", "Beta"]})
    monkeypatch.setattr(p2_x_intel, "SUPER_GIANTS", {"Alpha"})
    monkeypatch.setattr(p2_x_intel, "STRATEGIC_GIANTS", {"Beta"})
    monkeypatch.setattr(p2_x_intel, "parse_backtrack_hours", lambda _value: 24)

    def require_prompt(name):
        if name == "p2_user.txt":
            return "hours={hours}\n{keyword_section}\n{current_time}"
        return "tracks={{track_count}}\n{{industry_tracks}}\nsuper={{super_giants}}\nstrategic={{strategic_giants}}"

    monkeypatch.setattr(p2_x_intel, "require_prompt", require_prompt)
    monkeypatch.setattr(p2_x_intel.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p2_x_intel.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        p2_x_intel,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    return manifests


def _x_success_payload(tweet_id, *, text_prefix="intel"):
    return {
        "usage": {
            "total_tokens": 10,
            "input_tokens": 6,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
        "output": [
            {
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"<x_leaks><item>{text_prefix} https://x.com/source/status/{tweet_id}</item></x_leaks>",
                    },
                    {"type": "metadata", "text": "ignored"},
                ],
            },
            {"role": "tool", "type": "message", "content": []},
        ],
    }


def test_collect_from_x_skips_missing_key_and_prompt(monkeypatch):
    manifests = []
    monkeypatch.setattr(p2_x_intel, "GROK_API_KEY", "")
    monkeypatch.setattr(
        p2_x_intel,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    content, stats = p2_x_intel.collect_from_x()
    assert content == "" and stats["status"] == "skipped_no_key"
    assert manifests[-1][1]["status"] == "skipped_no_key"

    manifests = _configure_p2(monkeypatch)
    monkeypatch.setattr(
        p2_x_intel,
        "require_prompt",
        lambda name: (_ for _ in ()).throw(FileNotFoundError(name)),
    )
    content, stats = p2_x_intel.collect_from_x()
    assert content == "" and stats["status"] == "failed_missing_prompt"
    assert manifests[-1][1]["status"] == "failed_missing_prompt"


def test_collect_from_x_successful_chunks_structured_output(monkeypatch):
    manifests = _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    posts = []
    payloads = [
        _x_success_payload("9182736450", text_prefix="first"),
        _x_success_payload("9081726354", text_prefix="second"),
    ]

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return FakeResponse(payload=payloads.pop(0))

    saved = []
    monkeypatch.setattr(p2_x_intel.requests, "post", fake_post)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda content: [{"content": content}])
    monkeypatch.setattr(
        p2_x_intel,
        "render_p2_structured_markdown",
        lambda items, **kwargs: f"rendered:{len(items)}:{kwargs['model']}",
    )
    monkeypatch.setattr(
        p2_x_intel,
        "_save_p2_artifacts",
        lambda content, items, stats: saved.append((content, items, dict(stats))) or ("p2.md", "p2.json"),
    )

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content == "rendered:1:grok-test"
    assert stats["status"] == "success"
    assert stats["attempted_chunks"] == stats["successful_chunks"] == stats["total_chunks"] == 2
    assert stats["total_tokens"] == 20
    assert stats["reasoning_tokens"] == 2 and stats["cached_tokens"] == 4
    assert len(posts) == 2
    assert posts[0][0].endswith("/v1/responses")
    assert posts[0][1]["headers"]["Authorization"] == "Bearer test-key"
    assert posts[0][1]["json"]["tools"] == [{"type": "x_search"}]
    assert posts[0][1]["json"]["reasoning"] == {"effort": "high"}
    assert "tracks=1" in posts[0][1]["json"]["input"][0]["content"]
    assert saved[0][0] == content
    assert manifests[0][1]["status"] == "running"
    assert manifests[-1][1]["status"] == "completed"
    assert manifests[-1][1]["files"] == {"md": "p2.md", "json": "p2.json"}


def test_collect_from_x_primary_budget_exhaustion_still_uses_independent_fallback(monkeypatch):
    _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_MODEL", "grok-4.5")
    clock = {"now": 0.0}
    calls = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        calls.append((url, model, kwargs["timeout"]))
        if model == "grok-test":
            clock["now"] = 300.0
            return FakeResponse(payload=_x_success_payload("9182736450", text_prefix="primary"))
        return FakeResponse(payload=_x_success_payload("9081726354", text_prefix="fallback"))

    monkeypatch.setattr(p2_x_intel.time, "time", lambda: clock["now"])
    monkeypatch.setattr(p2_x_intel.requests, "post", post)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("p2.md", "p2.json"))

    content, stats = p2_x_intel.collect_from_x(
        max_retries=1,
        max_total_seconds=300,
        fallback_total_seconds=120,
    )

    assert content
    assert [model for _url, model, _timeout in calls] == ["grok-test", "grok-4.5"]
    assert stats["successful_chunks"] == stats["total_chunks"] == 2
    assert stats["fallback_chunks"] == 1


def test_collect_from_x_latches_fallback_only_after_primary_failure(monkeypatch):
    _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_MODEL", "grok-4.5")
    models = []
    fallback_ids = iter(["9182736450", "9081726354"])

    def post(_url, **kwargs):
        model = kwargs["json"]["model"]
        models.append(model)
        if model == "grok-test":
            return FakeResponse(status_code=503)
        return FakeResponse(payload=_x_success_payload(next(fallback_ids), text_prefix="fallback"))

    monkeypatch.setattr(p2_x_intel.requests, "post", post)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("p2.md", "p2.json"))

    content, stats = p2_x_intel.collect_from_x(
        max_retries=1,
        max_total_seconds=300,
        fallback_total_seconds=120,
    )

    assert content
    assert models == ["grok-test", "grok-4.5", "grok-4.5"]
    assert stats["successful_chunks"] == stats["total_chunks"] == 2
    assert stats["fallback_chunks"] == 2


def test_collect_from_x_keeps_earlier_chunks_when_a_later_chunk_fails(monkeypatch):
    _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", "")
    responses = [
        FakeResponse(payload=_x_success_payload("9182736450", text_prefix="kept")),
        RuntimeError("later chunk failed"),
    ]

    def post(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(p2_x_intel.requests, "post", post)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("p2.md", "p2.json"))

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)

    assert "kept" in content
    assert stats["successful_chunks"] == 1
    assert stats["status"].startswith("partial_")


def test_collect_from_x_success_without_structured_items(monkeypatch):
    _configure_p2(monkeypatch, reasoning="off")
    posts = []

    def fake_post(_url, **kwargs):
        posts.append(kwargs)
        return FakeResponse(payload=_x_success_payload("9182736450"))

    monkeypatch.setattr(p2_x_intel.requests, "post", fake_post)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("", ""))
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert "以下数据来自 X(Twitter)" in content
    assert "<x_leaks>" in content
    assert stats["structured_items"] == 0
    assert "reasoning" not in posts[0]["json"]


@pytest.mark.parametrize("status_code", [401, 403])
def test_collect_from_x_stops_on_auth_failure_without_content(monkeypatch, status_code):
    manifests = _configure_p2(monkeypatch)
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(status_code=status_code),
    )
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content == ""
    assert stats["status"] == f"auth_failed_{status_code}"
    assert manifests[-1][1]["status"] == f"auth_failed_{status_code}"


def test_collect_from_x_retries_rate_limit_then_succeeds(monkeypatch):
    _configure_p2(monkeypatch)
    responses = [FakeResponse(status_code=429), FakeResponse(payload=_x_success_payload("9182736450"))]
    sleeps = []
    monkeypatch.setattr(p2_x_intel.requests, "post", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(
        p2_x_intel,
        "_sleep_with_budget",
        lambda wait, *_args: sleeps.append(wait) or True,
    )
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("", ""))
    content, stats = p2_x_intel.collect_from_x(max_retries=2, max_total_seconds=1000)
    assert content and stats["status"] == "success"
    assert sleeps == [15]


def test_collect_from_x_timeout_and_empty_response_fail_cleanly(monkeypatch):
    manifests = _configure_p2(monkeypatch)
    responses = [requests.exceptions.Timeout("slow"), FakeResponse(payload={"output": []})]

    def fake_post(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    sleeps = []
    monkeypatch.setattr(p2_x_intel.requests, "post", fake_post)
    monkeypatch.setattr(
        p2_x_intel,
        "_sleep_with_budget",
        lambda wait, *_args: sleeps.append(wait) or True,
    )
    content, stats = p2_x_intel.collect_from_x(max_retries=2, max_total_seconds=1000)
    assert content == "" and stats["status"] == "failed_empty"
    assert sleeps == [3, 3]
    assert manifests[-1][1]["status"] == "failed_empty"


def test_collect_from_x_missing_output_and_fully_filtered_block(monkeypatch):
    _configure_p2(monkeypatch)
    monkeypatch.setattr(p2_x_intel.requests, "post", lambda *_args, **_kwargs: FakeResponse(payload={}))
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: True)
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content == "" and stats["status"] == "failed_empty"

    _configure_p2(monkeypatch)
    fake_block = {
        "output": [
            {
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "<x_leaks><item>fake https://x.com/a/status/7777111122</item></x_leaks>",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(p2_x_intel.requests, "post", lambda *_args, **_kwargs: FakeResponse(payload=fake_block))
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content == "" and stats["status"] == "failed_empty"


@pytest.mark.parametrize("failure", ["rate_limit", "timeout", "generic", "request_budget"])
def test_collect_from_x_budget_exit_paths(monkeypatch, failure):
    manifests = _configure_p2(monkeypatch)
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: False)
    if failure == "rate_limit":
        monkeypatch.setattr(p2_x_intel.requests, "post", lambda *_args, **_kwargs: FakeResponse(status_code=429))
    elif failure == "timeout":
        monkeypatch.setattr(
            p2_x_intel.requests,
            "post",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.Timeout("slow")),
        )
    elif failure == "generic":
        monkeypatch.setattr(
            p2_x_intel.requests,
            "post",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    else:
        monkeypatch.setattr(p2_x_intel, "_bounded_request_timeout", lambda *_args: 0)
        monkeypatch.setattr(
            p2_x_intel.requests,
            "post",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("post should not run")),
        )

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content == "" and stats["status"] == "failed_timeout"
    assert stats["stop_reason"] == "budget_exhausted"
    assert manifests[-1][1]["status"] == "failed_timeout"


def test_collect_from_x_budget_exhausted_before_first_chunk(monkeypatch):
    _configure_p2(monkeypatch)
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("post should not run")),
    )
    content, stats = p2_x_intel.collect_from_x(max_total_seconds=1)
    assert content == "" and stats["status"] == "failed_timeout"
    assert stats["attempted_chunks"] == 0


def test_collect_from_x_partial_timeout_and_partial_auth(monkeypatch):
    _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    remaining = iter([100, 0])
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: next(remaining))
    monkeypatch.setattr(p2_x_intel, "_bounded_request_timeout", lambda *_args: 20)
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload=_x_success_payload("9182736450")),
    )
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("", ""))
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content and stats["status"] == "partial_timeout"
    assert stats["stop_reason"] == "budget_exhausted"

    _configure_p2(monkeypatch, keywords=["kw1", "kw2", "kw3", "kw4", "kw5", "kw6"])
    responses = [
        FakeResponse(payload=_x_success_payload("9182736450")),
        FakeResponse(status_code=403),
    ]
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 100)
    monkeypatch.setattr(p2_x_intel.requests, "post", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("", ""))
    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=1000)
    assert content and stats["status"] == "partial_auth_failed_403"
    assert stats["stop_reason"] == "auth_failed_403"


def test_save_p2_artifacts_success_and_independent_failures(monkeypatch):
    monkeypatch.setattr(p2_x_intel, "write_text_artifact", lambda prefix, body: f"{prefix}.md" if body else "")
    monkeypatch.setattr(p2_x_intel, "write_json_artifact", lambda prefix, data: f"{prefix}.json" if data["items"] else "empty.json")
    assert p2_x_intel._save_p2_artifacts("content", [{"id": 1}], {"model": "m"}) == (
        "P2_X平台情报.md",
        "P2_X平台情报.json",
    )

    monkeypatch.setattr(
        p2_x_intel,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("md failed")),
    )
    assert p2_x_intel._save_p2_artifacts("content", [], {}) == ("", "empty.json")
    monkeypatch.setattr(p2_x_intel, "write_text_artifact", lambda *_args: "ok.md")
    monkeypatch.setattr(
        p2_x_intel,
        "write_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("json failed")),
    )
    assert p2_x_intel._save_p2_artifacts("content", [], {}) == ("ok.md", "")


class P4Pool:
    def __init__(self, max_workers):
        assert max_workers == 6
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, name, ticker):
        if name == "OuterBoom":
            future = InlineFuture(lambda: (_ for _ in ()).throw(RuntimeError("outer future")), ())
        else:
            future = InlineFuture(fn, (name, ticker))
        self.futures.append(future)
        return future


def test_fetch_vanguard_inline_missing_dependency_restores_environment(monkeypatch):
    manifests = []
    monkeypatch.setenv("CURL_CA_BUNDLE", "original.pem")
    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    assert p4_vanguard._fetch_vanguard_prices_inline() == ""
    assert manifests == [("p4", {"status": "skipped_missing_dependency"})]
    assert p4_vanguard.os.environ["CURL_CA_BUNDLE"] == "original.pem"


def test_fetch_vanguard_inline_market_branches_retries_and_artifact(monkeypatch):
    today = pd.to_datetime("today").normalize()
    old_index = pd.DatetimeIndex([today - pd.Timedelta(days=3), today - pd.Timedelta(days=1)], tz="UTC")
    frames = {
        "SUPER": pd.DataFrame({"Close": [100.0, 110.0]}, index=old_index),
        "EMPTY": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
        "TODAY": pd.DataFrame({"Close": [25.0]}, index=pd.DatetimeIndex([today], tz="UTC")),
        "ZERO": pd.DataFrame({"Close": [0.0, 0.0]}, index=old_index),
        "RETRY": pd.DataFrame({"Close": [50.0, 55.0]}, index=old_index),
    }
    history_calls = {}

    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            assert kwargs == {"period": "1mo", "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS, "raise_errors": True}
            history_calls[self.ticker] = history_calls.get(self.ticker, 0) + 1
            if self.ticker == "RETRY" and history_calls[self.ticker] == 1:
                raise requests.exceptions.Timeout("retry once")
            if self.ticker == "FAIL":
                raise RuntimeError("permanent failure")
            return frames[self.ticker].copy()

    fake_yfinance = SimpleNamespace(Ticker=Ticker)
    manifests = []
    artifacts = []
    signals = []
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(
        p4_vanguard,
        "VANGUARD_TICKERS",
        {
            "Super": "SUPER",
            "Strategic": "EMPTY",
            "Medium": "TODAY",
            "Zero": "ZERO",
            "Retry": "RETRY",
            "Failure": "FAIL",
            "OuterBoom": "OUTER",
        },
    )
    monkeypatch.setattr(p4_vanguard, "SUPER_GIANTS", {"Super"})
    monkeypatch.setattr(p4_vanguard, "STRATEGIC_GIANTS", {"Strategic"})
    monkeypatch.setattr(
        p4_vanguard,
        "_get_prev_trading_day",
        lambda _today: (today - pd.Timedelta(days=2)).date(),
    )
    monkeypatch.setattr(
        p4_vanguard,
        "classify_vanguard_signal",
        lambda name, change: signals.append((name, change)) or "📈 催化",
    )
    monkeypatch.setattr(p4_vanguard.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p4_vanguard.concurrent.futures, "ThreadPoolExecutor", P4Pool)
    monkeypatch.setattr(p4_vanguard.concurrent.futures, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        p4_vanguard,
        "write_text_artifact",
        lambda prefix, body: artifacts.append((prefix, body)) or "p4.md",
    )
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )

    report = p4_vanguard._fetch_vanguard_prices_inline()
    assert "Super(SUPER) [超级巨头]" in report and "区间涨跌 +10.00%" in report
    assert "Strategic(EMPTY) [战略核心]: 暂无最新交易数据" in report
    assert "Medium(TODAY) [中型标的]: 今日尚无完整收盘数据" in report
    assert "Zero(ZERO) [中型标的]: 收盘价 0.00 (无足够历史对比)" in report
    assert "Retry(RETRY) [中型标的]" in report
    assert "Failure(FAIL) [中型标的]: 获取股价失败" in report
    assert "OuterBoom(OUTER): 获取失败" in report
    assert history_calls["RETRY"] == history_calls["FAIL"] == 2
    assert {name for name, _change in signals} == {"Super", "Retry"}
    assert artifacts[0][0] == "P4_核心行情" and report in artifacts[0][1]
    assert manifests[-1][1]["status"] == "completed"
    assert manifests[-1][1]["files"] == {"md": "p4.md"}
    assert manifests[-1][1]["metrics"]["tickers"] == 7
    assert "CURL_CA_BUNDLE" not in p4_vanguard.os.environ


def test_fetch_vanguard_inline_no_baseline_and_artifact_failure(monkeypatch):
    today = pd.to_datetime("today").normalize()
    frame = pd.DataFrame(
        {"Close": [50.0]},
        index=pd.DatetimeIndex([today - pd.Timedelta(days=1)]),
    )

    class Ticker:
        def __init__(self, _ticker):
            pass

        def history(self, **_kwargs):
            return frame.copy()

    manifests = []
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=Ticker))
    monkeypatch.setattr(p4_vanguard, "VANGUARD_TICKERS", {"Only": "ONLY"})
    monkeypatch.setattr(p4_vanguard, "SUPER_GIANTS", set())
    monkeypatch.setattr(p4_vanguard, "STRATEGIC_GIANTS", set())
    monkeypatch.setattr(
        p4_vanguard,
        "_get_prev_trading_day",
        lambda _today: (today - pd.Timedelta(days=10)).date(),
    )
    monkeypatch.setattr(p4_vanguard, "classify_vanguard_signal", lambda _name, _change: "📈 正常")
    monkeypatch.setattr(p4_vanguard.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p4_vanguard.concurrent.futures, "ThreadPoolExecutor", P4Pool)
    monkeypatch.setattr(p4_vanguard.concurrent.futures, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        p4_vanguard,
        "write_text_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    report = p4_vanguard._fetch_vanguard_prices_inline()
    assert "区间涨跌 +0.00%" in report
    assert manifests[-1][1]["files"] is None


def test_p4_process_entry_reports_success_and_base_exception(monkeypatch):
    class Connection:
        def __init__(self):
            self.messages = []
            self.closed = False

        def send(self, payload):
            self.messages.append(payload)

        def close(self):
            self.closed = True

    success = Connection()
    monkeypatch.setattr(p4_vanguard, "_fetch_vanguard_prices_inline", lambda: "report")
    p4_vanguard._p4_process_entry(success)
    assert success.messages == [{"status": "success", "report": "report"}]
    assert success.closed is True

    failed = Connection()
    monkeypatch.setattr(
        p4_vanguard,
        "_fetch_vanguard_prices_inline",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt("cancelled")),
    )
    p4_vanguard._p4_process_entry(failed)
    assert failed.messages == [{"status": "error", "error": "KeyboardInterrupt: cancelled"}]
    assert failed.closed is True


def test_stop_process_handles_dead_terminated_and_stubborn_processes():
    class Process:
        def __init__(self, alive, stubborn=False):
            self.alive = alive
            self.stubborn = stubborn
            self.terminated = 0
            self.killed = 0
            self.joins = []

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated += 1
            if not self.stubborn:
                self.alive = False

        def kill(self):
            self.killed += 1
            self.alive = False

        def join(self, timeout):
            self.joins.append(timeout)

    dead = Process(False)
    p4_vanguard._stop_process(dead)
    assert dead.terminated == 0
    normal = Process(True)
    p4_vanguard._stop_process(normal)
    assert normal.terminated == 1 and normal.killed == 0 and normal.joins == [5]
    stubborn = Process(True, stubborn=True)
    p4_vanguard._stop_process(stubborn)
    assert stubborn.terminated == stubborn.killed == 1 and stubborn.joins == [5, 5]


class FakePipeConnection:
    def __init__(self, *, poll_result=True, payload=None, recv_error=None):
        self.poll_result = poll_result
        self.payload = payload
        self.recv_error = recv_error
        self.closed = 0
        self.poll_timeout = None

    def poll(self, timeout):
        self.poll_timeout = timeout
        return self.poll_result

    def recv(self):
        if self.recv_error:
            raise self.recv_error
        return self.payload

    def close(self):
        self.closed += 1


class FakeProcess:
    def __init__(self, *, start_error=None, alive=False, stubborn=False):
        self.start_error = start_error
        self.alive = alive
        self.stubborn = stubborn
        self.started = False
        self.terminated = 0
        self.killed = 0
        self.joins = []

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated += 1
        if not self.stubborn:
            self.alive = False

    def kill(self):
        self.killed += 1
        self.alive = False

    def join(self, timeout):
        self.joins.append(timeout)


def _patch_p4_context(monkeypatch, recv_conn, send_conn, process):
    class Context:
        @staticmethod
        def Pipe(duplex):
            assert duplex is False
            return recv_conn, send_conn

        @staticmethod
        def Process(**kwargs):
            assert kwargs["target"] is p4_vanguard._p4_process_entry
            assert kwargs["args"] == (send_conn,)
            assert kwargs["name"] == "p4-vanguard-fetch"
            return process

    monkeypatch.setattr(p4_vanguard.multiprocessing, "get_context", lambda mode: Context() if mode == "spawn" else None)


def test_fetch_vanguard_prices_subprocess_success_and_error_payload(monkeypatch):
    manifests = []
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    recv = FakePipeConnection(payload={"status": "success", "report": "child report"})
    send = FakePipeConnection()
    process = FakeProcess()
    _patch_p4_context(monkeypatch, recv, send, process)
    assert p4_vanguard.fetch_vanguard_prices() == "child report"
    assert process.started is True and process.joins == [5]
    assert recv.closed == 1 and send.closed == 2

    recv = FakePipeConnection(payload={"status": "error", "error": "child failed"})
    send = FakePipeConnection()
    process = FakeProcess()
    _patch_p4_context(monkeypatch, recv, send, process)
    assert p4_vanguard.fetch_vanguard_prices() == ""
    assert manifests[-1][1] == {"status": "failed_exception", "metrics": {"error": "child failed"}}

    recv = FakePipeConnection(payload={"status": "success", "report": None})
    send = FakePipeConnection()
    process = FakeProcess()
    _patch_p4_context(monkeypatch, recv, send, process)
    assert p4_vanguard.fetch_vanguard_prices() == ""


def test_fetch_vanguard_prices_subprocess_timeout_and_exception(monkeypatch):
    manifests = []
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    recv = FakePipeConnection(poll_result=False)
    send = FakePipeConnection()
    process = FakeProcess(alive=True, stubborn=True)
    _patch_p4_context(monkeypatch, recv, send, process)
    assert p4_vanguard.fetch_vanguard_prices() == ""
    assert recv.poll_timeout == p4_vanguard.P4_TOTAL_TIMEOUT_SECONDS
    assert process.terminated == process.killed == 1
    assert manifests[-1][1]["status"] == "failed_timeout"

    recv = FakePipeConnection(payload={"status": "success"})
    send = FakePipeConnection()
    process = FakeProcess(start_error=RuntimeError("spawn failed"), alive=True)
    _patch_p4_context(monkeypatch, recv, send, process)
    assert p4_vanguard.fetch_vanguard_prices() == ""
    assert process.terminated == 1
    assert manifests[-1][1]["status"] == "failed_exception"
    assert manifests[-1][1]["metrics"] == {"error": "spawn failed"}
