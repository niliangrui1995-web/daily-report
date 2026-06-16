# -*- coding: utf-8 -*-
"""Regression tests for security remediations."""

from __future__ import annotations

import logging
import os
import socket
import sys
from datetime import datetime

import requests
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_is_article_url_rejects_unsafe_targets():
    from scraper_utils import is_article_url

    blocked_urls = [
        "file:///etc/passwd",
        "ftp://example.com/article",
        "http://localhost:8123/admin",
        "http://127.0.0.1:8123/admin",
        "http://[::1]/admin",
        "http://10.0.0.2/latest",
        "http://172.16.0.2/latest",
        "http://192.168.1.2/latest",
        "http://198.18.0.182/news",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com@127.0.0.1/admin",
        "http://printer.local/status",
    ]

    for url in blocked_urls:
        assert is_article_url(url) is False


def test_safe_fetch_url_allows_public_dns_resolution(monkeypatch):
    import scraper_utils

    scraper_utils._host_resolves_to_public_addresses.cache_clear()

    def fake_getaddrinfo(host, port, type=0):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", fake_getaddrinfo)

    assert scraper_utils._safe_url_for_fetch("https://example.com/news/article") is True


def test_safe_fetch_url_allows_proxy_fake_ip_dns_resolution(monkeypatch):
    import scraper_utils

    scraper_utils._host_resolves_to_public_addresses.cache_clear()

    def fake_getaddrinfo(host, port, type=0):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.182", 443))]

    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", fake_getaddrinfo)

    assert scraper_utils._safe_url_for_fetch("https://example.com/news/article") is True


def test_safe_fetch_url_rejects_dns_private_resolution(monkeypatch):
    import scraper_utils

    scraper_utils._host_resolves_to_public_addresses.cache_clear()

    def fake_getaddrinfo(host, port, type=0):
        assert host == "attacker.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", fake_getaddrinfo)

    assert scraper_utils._safe_url_for_fetch("https://attacker.example/news") is False


def test_enrich_article_url_rejects_private_url_before_request(monkeypatch):
    import scraper_utils

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_Selector", object())
    monkeypatch.setattr(scraper_utils.time, "sleep", lambda seconds: None)

    def fail_get(*args, **kwargs):
        raise AssertionError("unsafe URL reached requests.get")

    monkeypatch.setattr(scraper_utils.requests, "get", fail_get)

    assert scraper_utils.enrich_article_url("http://127.0.0.1:8123/admin") == ""


def test_safe_requests_get_rejects_redirect_to_private_host(monkeypatch):
    import scraper_utils

    scraper_utils._host_resolves_to_public_addresses.cache_clear()
    calls = []

    def fake_getaddrinfo(host, port, type=0):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class RedirectResponse:
        status_code = 302
        headers = {"Location": "http://127.0.0.1:8123/admin"}
        url = "https://example.com/start"
        is_redirect = True
        is_permanent_redirect = False

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("allow_redirects")))
        return RedirectResponse()

    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(scraper_utils.requests, "get", fake_get)

    with pytest.raises(requests.exceptions.InvalidURL):
        scraper_utils._safe_requests_get("https://example.com/start", headers={}, timeout=1, verify=True)

    assert calls == [("https://example.com/start", False)]


def test_safe_requests_get_applies_default_timeout(monkeypatch):
    import scraper_utils

    scraper_utils._host_resolves_to_public_addresses.cache_clear()
    calls = []

    def fake_getaddrinfo(host, port, type=0):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class OkResponse:
        status_code = 200
        headers = {}
        url = "https://example.com/news"
        is_redirect = False
        is_permanent_redirect = False

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return OkResponse()

    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(scraper_utils.requests, "get", fake_get)

    scraper_utils._safe_requests_get("https://example.com/news", headers={}, verify=True)

    assert calls[0]["timeout"] == 12


def test_rss_fetch_failure_does_not_let_feedparser_fetch_url(monkeypatch):
    import p1_rss

    def fail_get(*args, **kwargs):
        raise requests.exceptions.Timeout("network timeout")

    def fail_parse(*args, **kwargs):
        raise AssertionError("feedparser.parse should not fetch URL after guarded request failure")

    monkeypatch.setattr(p1_rss.requests, "get", fail_get)
    monkeypatch.setattr(p1_rss.feedparser, "parse", fail_parse)

    assert p1_rss._fetch_rss("Example", "https://example.com/feed.xml", "example") == []


def test_search_exception_logging_redacts_gemini_key(monkeypatch, caplog):
    import search_engine

    secret = "SECRETKEY123"

    class FakeResponse:
        status_code = 418
        url = f"https://generativelanguage.googleapis.com/v1beta/models/test:generateContent?key={secret}&x=1"

        def raise_for_status(self):
            raise requests.HTTPError(f"418 Server Error for url: {self.url}", response=self)

    monkeypatch.setattr(search_engine, "search_rotator_direct", search_engine.SearchKeyRotator([secret], "https://example.invalid"))
    monkeypatch.setattr(search_engine, "channel_breaker", search_engine.ChannelCircuitBreaker(threshold=99))
    monkeypatch.setattr(search_engine, "_post_search_request", lambda **kwargs: FakeResponse())
    monkeypatch.setattr(search_engine, "_tavily_fallback", lambda *args, **kwargs: {})
    monkeypatch.setattr(search_engine.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(search_engine.random, "uniform", lambda start, end: 0)

    caplog.set_level(logging.WARNING)

    assert search_engine.raw_gemini_search("test prompt", max_retries=1) == ""
    assert secret not in caplog.text
    assert "key=<redacted>" in caplog.text


def test_search_5xx_trips_channel_and_uses_tavily(monkeypatch, caplog):
    import search_engine

    class ServerErrorResponse:
        status_code = 503
        url = "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent"
        text = "503 UNAVAILABLE: model overloaded"

        def raise_for_status(self):
            raise requests.HTTPError(self.text, response=self)

    direct_rotator = search_engine.SearchKeyRotator(["key_A", "key_B", "key_C"], "https://example.invalid")
    breaker = search_engine.ChannelCircuitBreaker(threshold=2)
    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return ServerErrorResponse()

    monkeypatch.setattr(search_engine, "search_rotator_direct", direct_rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)
    monkeypatch.setattr(search_engine, "_post_search_request", fake_post)
    monkeypatch.setattr(
        search_engine,
        "_tavily_fallback",
        lambda *args, **kwargs: {"text": "fallback ok", "grounding_sources": []},
    )

    caplog.set_level(logging.WARNING)

    result = search_engine.raw_gemini_search("test prompt", max_retries=3, return_dict=True)

    assert result["text"] == "fallback ok"
    assert len(calls) == 2
    assert breaker.is_tripped("direct") is True
    assert direct_rotator.available == 3
    assert "server_error on direct" in caplog.text


def test_search_permission_denied_403_does_not_exhaust_gemini_key(monkeypatch, caplog):
    import search_engine

    secret = "SECRETKEY123"

    class PermissionDeniedResponse:
        status_code = 403
        url = "https://api.niliangrui.cloud/v1beta/models/test:generateContent"
        text = (
            "403 PERMISSION_DENIED: Cloud Code Private API has not been used. "
            "SERVICE_DISABLED cloudcode-pa.googleapis.com"
        )

        def raise_for_status(self):
            raise requests.HTTPError(self.text, response=self)

    direct_rotator = search_engine.SearchKeyRotator([secret], "https://example.invalid")
    monkeypatch.setattr(search_engine, "search_rotator_direct", direct_rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", search_engine.ChannelCircuitBreaker(threshold=99))
    monkeypatch.setattr(search_engine, "_post_search_request", lambda **kwargs: PermissionDeniedResponse())
    monkeypatch.setattr(search_engine, "_tavily_fallback", lambda *args, **kwargs: {})
    monkeypatch.setattr(search_engine.time, "sleep", lambda seconds: None)

    caplog.set_level(logging.WARNING)

    assert search_engine.raw_gemini_search("test prompt", max_retries=1) == ""
    assert direct_rotator.available == 1
    assert "permission_denied" in caplog.text


def test_gemini_search_rest_payload_uses_current_google_search_field(monkeypatch):
    import search_engine

    captured = {}
    sentinel = object()

    class FakeSession:
        def post(self, *args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)
            return sentinel

    monkeypatch.setattr(search_engine, "_http_session", FakeSession())

    result = search_engine._post_search_request(
        base_url="https://generativelanguage.googleapis.com",
        key="SECRETKEY123",
        prompt="latest AI infrastructure news",
        temperature=0.7,
    )

    assert result is sentinel
    assert captured["args"][0].endswith(":generateContent")
    assert captured["json"]["tools"] == [{"google_search": {}}]
    assert "googleSearch" not in str(captured["json"])


def test_search_400_bad_request_trips_channel_and_uses_tavily(monkeypatch, caplog):
    import search_engine

    class BadRequestResponse:
        status_code = 400
        url = "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent"
        text = "Invalid JSON payload received. Unknown name 'googleSearch' at 'tools[0]'"

        def raise_for_status(self):
            raise requests.HTTPError(self.text, response=self)

    direct_rotator = search_engine.SearchKeyRotator(["key_A", "key_B", "key_C"], "https://example.invalid")
    breaker = search_engine.ChannelCircuitBreaker(threshold=2)
    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return BadRequestResponse()

    monkeypatch.setattr(search_engine, "search_rotator_direct", direct_rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)
    monkeypatch.setattr(search_engine, "_post_search_request", fake_post)
    monkeypatch.setattr(
        search_engine,
        "_tavily_fallback",
        lambda *args, **kwargs: {"text": "fallback ok", "grounding_sources": []},
    )

    caplog.set_level(logging.WARNING)

    result = search_engine.raw_gemini_search("test prompt", max_retries=3, return_dict=True)

    assert result["text"] == "fallback ok"
    assert len(calls) == 1
    assert breaker.is_tripped("direct") is True
    assert direct_rotator.available == 3
    assert "bad_request on direct" in caplog.text
    assert "Unknown name 'googleSearch'" in caplog.text


def test_search_400_unsupported_location_trips_channel(monkeypatch, caplog):
    import search_engine

    class UnsupportedLocationResponse:
        status_code = 400
        url = "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent"
        text = (
            '{"error":{"code":400,"message":"User location is not supported for the API use.",'
            '"status":"FAILED_PRECONDITION"}}'
        )

        def raise_for_status(self):
            raise requests.HTTPError(self.text, response=self)

    direct_rotator = search_engine.SearchKeyRotator(["key_A", "key_B"], "https://example.invalid")
    breaker = search_engine.ChannelCircuitBreaker(threshold=2)
    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return UnsupportedLocationResponse()

    monkeypatch.setattr(search_engine, "search_rotator_direct", direct_rotator)
    monkeypatch.setattr(search_engine, "channel_breaker", breaker)
    monkeypatch.setattr(search_engine, "_post_search_request", fake_post)
    monkeypatch.setattr(
        search_engine,
        "_tavily_fallback",
        lambda *args, **kwargs: {"text": "fallback ok", "grounding_sources": []},
    )

    caplog.set_level(logging.WARNING)

    result = search_engine.raw_gemini_search("test prompt", max_retries=3, return_dict=True)

    assert result["text"] == "fallback ok"
    assert len(calls) == 1
    assert breaker.is_tripped("direct") is True
    assert direct_rotator.available == 2
    assert "unsupported_location on direct" in caplog.text


def test_tavily_fallback_can_feed_p7_structured_mode():
    import search_engine

    result = {
        "text": (
            "[Tavily Fallback] HBM demand is driving ABF substrate pressure and benefits PCB suppliers.\n"
            "- HBM substrate shortage: Demand growth creates supply pressure and benefits upstream PCB suppliers. [https://example.com/hbm]\n"
            "- Glass substrate alternatives: Glass remains a backup alternative for advanced packaging. [https://example.com/glass]"
        ),
        "grounding_sources": [
            {"url": "https://example.com/hbm", "title": "HBM substrate shortage"},
            {"url": "https://example.com/glass", "title": "Glass substrate alternatives"},
        ],
        "is_fallback": True,
        "fallback_provider": "tavily",
    }

    payload = search_engine._structured_payload_from_tavily_result(
        result,
        query="HBM substrate shortage winners alternatives",
        structured_mode="p7",
    )

    assert payload is not None
    assert payload["summary"].startswith("HBM demand")
    assert payload["claims"]
    assert "HBM" in payload["entities"]
    assert payload["coverage_flags"]["winners"] is True
    assert payload["coverage_flags"]["alternatives"] is True
    assert "benefits upstream PCB suppliers" in search_engine._render_p7_structured_text(payload)


def test_report_email_sanitizes_html_and_unsafe_links():
    from p9_email import build_report_email_message

    content = (
        '<img src="http://track.invalid/pixel" onerror="alert(1)">\n'
        '[bad](javascript:alert(1))\n'
        '[safe](https://example.com/report)'
    )
    msg = build_report_email_message(
        content,
        sender_email="sender@example.com",
        receiver_email="receiver@example.com",
        now=datetime(2026, 5, 3, 9, 0),
    )
    body = msg.get_payload(decode=True).decode("utf-8")
    lowered = body.lower()

    assert "<img" not in lowered
    assert "onerror" not in lowered
    assert "javascript:" not in lowered
    assert 'href="https://example.com/report"' in body


def test_report_email_falls_back_to_tun_safe_route(monkeypatch):
    import p9_email

    calls = []

    def fake_send_message(msg, *, sender_email, sender_password, receiver_email, remote_ip=None, source_ip=None):
        calls.append({"remote_ip": remote_ip, "source_ip": source_ip})
        if remote_ip is None:
            raise TimeoutError("primary path timed out")

    monkeypatch.setattr(p9_email, "_send_message", fake_send_message)
    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: ["192.168.0.102"])
    monkeypatch.setattr(p9_email, "_resolve_real_smtp_ips", lambda: ["43.163.178.76"])

    p9_email.send_report_via_email(
        "report",
        sender_email="sender@example.com",
        sender_password="secret",
        receiver_email="receiver@example.com",
    )

    assert calls == [
        {"remote_ip": None, "source_ip": None},
        {"remote_ip": "43.163.178.76", "source_ip": "192.168.0.102"},
    ]
