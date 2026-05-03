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


def test_search_exception_logging_redacts_gemini_key(monkeypatch, caplog):
    import search_engine

    secret = "SECRETKEY123"

    class FakeResponse:
        status_code = 500
        url = f"https://generativelanguage.googleapis.com/v1beta/models/test:generateContent?key={secret}&x=1"

        def raise_for_status(self):
            raise requests.HTTPError(f"500 Server Error for url: {self.url}", response=self)

    monkeypatch.setattr(search_engine, "search_rotator_direct", search_engine.SearchKeyRotator([secret], "https://example.invalid"))
    monkeypatch.setattr(search_engine, "search_rotator_cf", search_engine.SearchKeyRotator([], "https://example.invalid"))
    monkeypatch.setattr(search_engine, "channel_breaker", search_engine.ChannelCircuitBreaker(threshold=99))
    monkeypatch.setattr(search_engine, "_post_search_request", lambda **kwargs: FakeResponse())
    monkeypatch.setattr(search_engine, "_tavily_fallback", lambda *args, **kwargs: {})
    monkeypatch.setattr(search_engine.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(search_engine.random, "uniform", lambda start, end: 0)

    caplog.set_level(logging.WARNING)

    assert search_engine.raw_gemini_search("test prompt", max_retries=1) == ""
    assert secret not in caplog.text
    assert "key=<redacted>" in caplog.text


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
