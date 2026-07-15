# -*- coding: utf-8 -*-
"""Offline coverage for the remaining reachable source-collector branches."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import Any

import pytest

import p1_rss
import p1_web
import p2_x_intel


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        *,
        text: str = "",
        json_error: Exception | None = None,
    ):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def _load_variant(monkeypatch, source_module, name: str):
    spec = importlib.util.spec_from_file_location(name, source_module.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_rss_import_degrades_when_scrapling_capability_is_unavailable(
    monkeypatch,
    caplog,
):
    scraper_utils = ModuleType("scraper_utils")
    scraper_utils.is_article_url = lambda _url: True
    scraper_utils._safe_external_requests_get = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "scraper_utils", scraper_utils)

    with caplog.at_level("WARNING"):
        variant = _load_variant(monkeypatch, p1_rss, "p1_rss_without_scrapling_final")

    assert variant._scrapling_available is False
    assert "scraper_utils" in caplog.text


def _scrapling_package() -> ModuleType:
    package = ModuleType("scrapling")
    package.__path__ = []
    return package


def test_web_import_uses_legacy_parser_fallback(monkeypatch):
    package = _scrapling_package()
    parser = ModuleType("scrapling.parser")

    class LegacySelector:
        pass

    parser.Selector = LegacySelector
    monkeypatch.setitem(sys.modules, "scrapling", package)
    monkeypatch.setitem(sys.modules, "scrapling.parser", parser)

    variant = _load_variant(monkeypatch, p1_web, "p1_web_legacy_parser_final")

    assert variant._parser_available is True
    assert variant.Selector is LegacySelector


def test_web_import_degrades_when_both_selector_paths_are_missing(
    monkeypatch,
    caplog,
):
    monkeypatch.setitem(sys.modules, "scrapling", _scrapling_package())
    monkeypatch.setitem(sys.modules, "scrapling.parser", ModuleType("scrapling.parser"))

    with caplog.at_level("WARNING"):
        variant = _load_variant(monkeypatch, p1_web, "p1_web_without_parser_final")

    assert variant._parser_available is False
    assert "Scrapling" in caplog.text


def test_web_url_date_falls_through_invalid_separated_date_to_compact_date():
    published_at = p1_web._extract_web_published_at(
        "",
        "/2026/99/99/story-20260715",
        "https://news.example.test/2026/99/99/story-20260715",
        "story",
    )

    assert published_at.startswith("2026-07-15T23:59:59")


def _configure_p2(monkeypatch, *, fallback_key: str = "fallback-key"):
    manifests: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(p2_x_intel, "GROK_API_KEY", "primary-key")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_API_KEY", fallback_key)
    monkeypatch.setattr(p2_x_intel, "GROK_MODEL", "grok-primary")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_MODEL", "grok-backup")
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_BASE_URL", "https://fallback.test")
    monkeypatch.setattr(p2_x_intel, "GROK_REASONING_EFFORT", "none")
    monkeypatch.setattr(p2_x_intel, "GROK_MAX_OUTPUT_TOKENS", 512)
    monkeypatch.setattr(p2_x_intel, "INDUSTRY_SCAN_KEYWORDS", ["keyword"])
    monkeypatch.setattr(p2_x_intel, "OLIGARCH_DICT", {"Track": ["Alpha"]})
    monkeypatch.setattr(p2_x_intel, "SUPER_GIANTS", {"Alpha"})
    monkeypatch.setattr(p2_x_intel, "STRATEGIC_GIANTS", set())
    monkeypatch.setattr(p2_x_intel, "parse_backtrack_hours", lambda _value: 24)

    def require_prompt(name: str) -> str:
        if name == "p2_user.txt":
            return "hours={hours}\n{keyword_section}\n{current_time}"
        return "tracks={{track_count}}\n{{industry_tracks}}\n{{super_giants}}\n{{strategic_giants}}"

    monkeypatch.setattr(p2_x_intel, "require_prompt", require_prompt)
    monkeypatch.setattr(p2_x_intel.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(p2_x_intel.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(p2_x_intel, "_remaining_budget_seconds", lambda *_args: 1000.0)
    monkeypatch.setattr(p2_x_intel, "_bounded_request_timeout", lambda *_args: 20.0)
    monkeypatch.setattr(p2_x_intel, "parse_p2_structured_items", lambda _content: [])
    monkeypatch.setattr(p2_x_intel, "_save_p2_artifacts", lambda *_args: ("", ""))
    monkeypatch.setattr(
        p2_x_intel,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    return manifests


def _success_payload(tweet_id: str = "9182736450") -> dict[str, Any]:
    return {
        "usage": {},
        "output": [
            {
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "<x_leaks><item>offline evidence "
                            f"https://x.com/source/status/{tweet_id}</item></x_leaks>"
                        ),
                    }
                ],
            }
        ],
    }


def test_p2_primary_network_error_stops_when_retry_budget_is_exhausted(monkeypatch):
    manifests = _configure_p2(monkeypatch, fallback_key="")
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline failure")),
    )
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: False)

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert stats["status"] == "failed_timeout"
    assert stats["stop_reason"] == "budget_exhausted"
    assert manifests[-1][1]["status"] == "failed_timeout"


def test_p2_fallback_v1_endpoint_stops_before_request_when_budget_is_empty(monkeypatch):
    _configure_p2(monkeypatch)
    monkeypatch.setattr(p2_x_intel, "GROK_FALLBACK_BASE_URL", "https://fallback.test/v1")
    timeouts = iter([20.0, 0.0])
    monkeypatch.setattr(p2_x_intel, "_bounded_request_timeout", lambda *_args: next(timeouts))
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: True)
    posts: list[str] = []
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda url, **_kwargs: posts.append(url) or FakeResponse(status_code=503),
    )

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert posts == [f"{p2_x_intel.GROK_BASE_URL}/v1/responses"]
    assert stats["status"] == "failed_timeout"
    assert stats["stop_reason"] == "fallback_budget_exhausted"
    assert stats["fallback_model"] == "grok-backup"


def test_p2_fallback_auth_failure_is_terminal(monkeypatch):
    _configure_p2(monkeypatch)
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: True)
    responses = iter([FakeResponse(status_code=503), FakeResponse(status_code=401)])
    urls: list[str] = []
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda url, **_kwargs: urls.append(url) or next(responses),
    )

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert urls[-1] == "https://fallback.test/v1/responses"
    assert stats["status"] == "auth_failed_401"


@pytest.mark.parametrize("failure_kind", ["http", "exception"])
def test_p2_fallback_failure_stops_when_retry_wait_exceeds_budget(
    monkeypatch,
    failure_kind,
):
    _configure_p2(monkeypatch)
    sleeps = iter([True, False])
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: next(sleeps))
    calls = 0

    def post(_url, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(status_code=503)
        if failure_kind == "exception":
            raise RuntimeError("fallback transport failed")
        return FakeResponse(status_code=500, text="fallback unavailable")

    monkeypatch.setattr(p2_x_intel.requests, "post", post)

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert calls == 2
    assert stats["status"] == "failed_timeout"
    assert stats["stop_reason"] == "fallback_budget_exhausted"


@pytest.mark.parametrize("failure_kind", ["http", "exception"])
def test_p2_fallback_retries_then_recovers(monkeypatch, failure_kind):
    _configure_p2(monkeypatch)
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: True)
    calls = 0

    def post(_url, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(status_code=503)
        if calls == 2:
            if failure_kind == "exception":
                raise RuntimeError("transient fallback failure")
            return FakeResponse(status_code=500)
        return FakeResponse(payload=_success_payload())

    monkeypatch.setattr(p2_x_intel.requests, "post", post)

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert "offline evidence" in content
    assert calls == 3
    assert stats["status"] == "success"
    assert stats["fallback_chunks"] == 1


def test_p2_parse_failure_stops_when_retry_budget_is_exhausted(monkeypatch):
    _configure_p2(monkeypatch, fallback_key="")
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(json_error=ValueError("invalid JSON")),
    )
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: False)

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert stats["status"] == "failed_timeout"
    assert stats["stop_reason"] == "budget_exhausted"
    assert stats["attempted_chunks"] == 1


def test_p2_plain_http_failure_without_stop_reason_is_failed_empty(monkeypatch):
    _configure_p2(monkeypatch, fallback_key="")
    monkeypatch.setattr(
        p2_x_intel.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(status_code=500),
    )
    monkeypatch.setattr(p2_x_intel, "_sleep_with_budget", lambda *_args: True)

    content, stats = p2_x_intel.collect_from_x(max_retries=1, max_total_seconds=60)

    assert content == ""
    assert stats["status"] == "failed_empty"
    assert "stop_reason" not in stats
