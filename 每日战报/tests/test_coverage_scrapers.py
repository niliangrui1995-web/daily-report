# -*- coding: utf-8 -*-
"""Focused behavioral coverage for shared scraping and RSS collection helpers."""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import p1_rss
import scraper_utils


PUBLIC_ADDR = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        text="",
        content=None,
        headers=None,
        url="https://example.com/article",
        redirect=False,
        payload=None,
        chunks=None,
    ):
        self.status_code = status_code
        self.text = text
        self.content = text.encode() if content is None else content
        self.headers = dict(headers or {})
        self.url = url
        self.is_redirect = redirect
        self.is_permanent_redirect = False
        self.payload = payload
        self.chunks = list(chunks) if chunks is not None else [self.content]
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size, decode_unicode):
        assert chunk_size == scraper_utils.RESPONSE_CHUNK_BYTES
        assert decode_unicode is False
        yield from self.chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))

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
        except Exception as exc:  # exercised through Future.result, like the real executor
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


def _recent_timestamp(minutes_ago=5):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_hostname_dns_pinning_and_address_filters(monkeypatch):
    calls = []

    def system_lookup(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, port, family, type, proto, flags))
        return [PUBLIC_ADDR]

    monkeypatch.setattr(scraper_utils, "_SYSTEM_GETADDRINFO", system_lookup)
    if hasattr(scraper_utils._DNS_PIN_STATE, "pins"):
        del scraper_utils._DNS_PIN_STATE.pins

    assert scraper_utils._normalize_hostname(b"EXAMPLE.COM.") == "example.com"
    assert scraper_utils._normalize_hostname("例子.测试").startswith("xn--")
    assert scraper_utils._pinned_getaddrinfo("example.com", 80) == [PUBLIC_ADDR]
    assert calls[0][:2] == ("example.com", 80)

    pinned = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "v4", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_DGRAM, 17, "v6", ("2001:4860:4860::8888", 443, 0, 2)),
    ]
    with scraper_utils._pin_validated_dns("Example.com", pinned):
        resolved_v4 = scraper_utils._pinned_getaddrinfo(
            "EXAMPLE.COM.", 8443, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=6
        )
        resolved_v6 = scraper_utils._pinned_getaddrinfo(
            "example.com", 9443, family=socket.AF_INET6, type=socket.SOCK_DGRAM, proto=17
        )
        assert resolved_v4[0][4] == ("8.8.8.8", 8443)
        assert resolved_v6[0][4] == ("2001:4860:4860::8888", 9443, 0, 2)
        with pytest.raises(socket.gaierror):
            scraper_utils._pinned_getaddrinfo("example.com", 443, family=99999)
        with pytest.raises(socket.gaierror):
            scraper_utils._pinned_getaddrinfo(
                "example.com", 443, family=socket.AF_INET, type=socket.SOCK_DGRAM
            )
        with pytest.raises(socket.gaierror):
            scraper_utils._pinned_getaddrinfo(
                "example.com", 443, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=17
            )
    assert not hasattr(scraper_utils._DNS_PIN_STATE, "pins")

    scraper_utils._DNS_PIN_STATE.pins = {"old.example": tuple([PUBLIC_ADDR])}
    try:
        with scraper_utils._pin_validated_dns("new.example", pinned):
            assert "new.example" in scraper_utils._DNS_PIN_STATE.pins
        assert set(scraper_utils._DNS_PIN_STATE.pins) == {"old.example"}
    finally:
        del scraper_utils._DNS_PIN_STATE.pins

    with scraper_utils._pin_validated_dns("deleted.example", pinned):
        del scraper_utils._DNS_PIN_STATE.pins
    assert not hasattr(scraper_utils._DNS_PIN_STATE, "pins")


def test_hostname_unicode_fallback():
    class NonIdnaHostname(str):
        def strip(self, *_args):
            return self

        def rstrip(self, *_args):
            return self

        def lower(self):
            return self

        def encode(self, *_args, **_kwargs):
            raise UnicodeError("cannot encode")

    hostname = NonIdnaHostname("broken.example")
    assert scraper_utils._normalize_hostname(hostname) is hostname


@pytest.mark.parametrize(
    ("value", "blocked", "proxy_fake"),
    [
        ("127.0.0.1", True, False),
        ("10.0.0.1", True, False),
        ("8.8.8.8", False, False),
        ("198.18.1.1", True, True),
    ],
)
def test_ip_classification(value, blocked, proxy_fake):
    parsed = scraper_utils._parse_ip_address(value)
    assert parsed is not None
    assert scraper_utils._is_blocked_ip_address(parsed) is blocked
    assert scraper_utils._is_proxy_fake_ip_address(value) is proxy_fake


def test_dns_resolution_rejects_invalid_or_private_results(monkeypatch):
    assert scraper_utils._parse_ip_address("not-an-ip") is None
    assert scraper_utils._is_blocked_hostname("LOCALHOST.") is True
    assert scraper_utils._is_blocked_hostname("printer.internal") is True
    assert scraper_utils._is_blocked_hostname("example.com") is False

    def set_resolver(value):
        def resolver(*_args, **_kwargs):
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(scraper_utils, "_SYSTEM_GETADDRINFO", resolver)
        monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", scraper_utils._pinned_getaddrinfo)

    set_resolver(OSError("dns down"))
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == ()
    set_resolver([])
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == ()
    set_resolver([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ())])
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == ()
    set_resolver([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))])
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == ()
    set_resolver([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))])
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == ()
    proxy = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.20", 443))
    set_resolver([proxy, PUBLIC_ADDR])
    assert scraper_utils._resolve_public_addr_infos("example.com", 443) == (proxy, PUBLIC_ADDR)
    assert scraper_utils._host_resolves_to_public_addresses("example.com") is True

    custom = lambda *_args, **_kwargs: []
    monkeypatch.setattr(scraper_utils.socket, "getaddrinfo", custom)
    assert scraper_utils._resolver_for_validation() is custom


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ftp://example.com/file", False),
        ("https:///missing-host", False),
        ("https://user:pass@example.com/article", False),  # pragma: allowlist secret
        ("https://localhost/article", False),
        ("http://127.0.0.1/private", False),
        ("https://8.8.8.8/article", True),
        ("https://example.com/article", True),
    ],
)
def test_safe_external_url_static_validation(url, expected):
    assert scraper_utils._is_safe_external_url(url) is expected


def test_safe_url_helpers_and_custom_checker(monkeypatch):
    class BadURL:
        def decode(self, *_args):
            raise RuntimeError("bad url")

    assert scraper_utils._is_safe_external_url(BadURL()) is False
    monkeypatch.setattr(scraper_utils, "_host_resolves_to_public_addresses", lambda host: host == "example.com")
    monkeypatch.setattr(scraper_utils, "URL_BLACKLIST", ["blocked-path"])

    assert scraper_utils._safe_url_for_fetch("https://example.com/article") is True
    assert scraper_utils._safe_url_for_fetch("https://example.com/blocked-path") is False
    assert scraper_utils._safe_external_url_for_fetch("https://example.com/feed.xml") is True
    assert scraper_utils._safe_external_url_for_fetch("") is False
    assert scraper_utils._url_is_safe_without_dns("https://example.com/article", scraper_utils._safe_url_for_fetch)
    assert scraper_utils._url_is_safe_without_dns("https://example.com/feed.xml", scraper_utils._safe_external_url_for_fetch)
    assert scraper_utils._url_is_safe_without_dns("value", lambda value: value == "value")


def test_timeout_close_and_bounded_response_behaviors():
    assert scraper_utils._split_timeout((1, 9)) == (1, 9)
    assert scraper_utils._split_timeout(3) == (3.0, 3.0)
    assert scraper_utils._split_timeout(30) == (5.0, 30.0)
    with pytest.raises(ValueError, match="connect and read"):
        scraper_utils._split_timeout((1, 2, 3))

    oversized_header = FakeResponse(headers={"Content-Length": "11"})
    with pytest.raises(requests.exceptions.RequestException, match="Content-Length"):
        scraper_utils._read_bounded_response(oversized_header, 10)
    assert oversized_header.closed is True

    invalid_header = FakeResponse(headers={"Content-Length": "unknown"}, chunks=[b"", b"ab", b"cd"])
    assert scraper_utils._read_bounded_response(invalid_header, 4)._content == b"abcd"
    assert invalid_header._content_consumed is True
    assert invalid_header.closed is True

    valid_header = FakeResponse(headers={"Content-Length": "2"}, chunks=[b"ok"])
    assert scraper_utils._read_bounded_response(valid_header, 2)._content == b"ok"

    streamed = FakeResponse(chunks=[b"abc", b"def"])
    with pytest.raises(requests.exceptions.RequestException, match="body exceeds"):
        scraper_utils._read_bounded_response(streamed, 5)
    assert streamed.closed is True

    closed = []
    plain = SimpleNamespace(content=b"abc", headers={}, close=lambda: closed.append(True))
    assert scraper_utils._read_bounded_response(plain, 3) is plain
    too_large = SimpleNamespace(content=b"abcd", headers={}, close=lambda: closed.append(True))
    with pytest.raises(requests.exceptions.RequestException, match="body exceeds"):
        scraper_utils._read_bounded_response(too_large, 3)
    scraper_utils._close_response(SimpleNamespace())
    assert closed == [True]


def test_safe_requests_get_validation_and_redirect_paths(monkeypatch):
    monkeypatch.setattr(scraper_utils, "_url_is_safe_without_dns", lambda url, checker: "unsafe" not in url)
    monkeypatch.setattr(scraper_utils, "_resolve_public_addr_infos", lambda host, port: (PUBLIC_ADDR,) if host != "dnsless.example" else ())

    with pytest.raises(ValueError, match="positive"):
        scraper_utils._safe_requests_get_checked("https://example.com/article", lambda _url: True, max_response_bytes=0)
    with pytest.raises(requests.exceptions.InvalidURL):
        scraper_utils._safe_requests_get_checked("https://unsafe.example/article", lambda _url: True)
    with pytest.raises(requests.exceptions.InvalidURL):
        scraper_utils._safe_requests_get_checked("https://dnsless.example/article", lambda _url: True)

    calls = []
    responses = [
        FakeResponse(
            status_code=302,
            headers={"Location": "/next"},
            url="https://example.com/start",
            redirect=True,
        ),
        FakeResponse(text="done", url="https://example.com/next"),
    ]

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(scraper_utils.requests, "get", fake_get)
    result = scraper_utils._safe_requests_get_checked(
        "https://example.com/start",
        lambda _url: True,
        timeout=(2, 7),
        allow_redirects=True,
        stream=False,
        proxies={"https": "bad"},
        max_response_bytes=100,
        headers={"X-Test": "yes"},
    )
    assert result._content == b"done"
    assert [call[0] for call in calls] == ["https://example.com/start", "https://example.com/next"]
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["proxies"] == {"http": "", "https": ""}
    assert calls[0][1]["timeout"] == (2, 7)

    no_location = FakeResponse(status_code=301, headers={}, redirect=True, text="redirect body")
    monkeypatch.setattr(scraper_utils.requests, "get", lambda *_args, **_kwargs: no_location)
    assert scraper_utils._safe_requests_get_checked(
        "https://example.com/start", lambda _url: True, max_response_bytes=100
    )._content == b"redirect body"

    monkeypatch.setattr(scraper_utils, "MAX_SAFE_REDIRECTS", 1)
    redirects = []

    def endless_redirect(url, **_kwargs):
        response = FakeResponse(headers={"Location": "/again"}, url=url, redirect=True)
        redirects.append(response)
        return response

    monkeypatch.setattr(scraper_utils.requests, "get", endless_redirect)
    with pytest.raises(requests.exceptions.TooManyRedirects):
        scraper_utils._safe_requests_get_checked("https://example.com/start", lambda _url: True)
    assert len(redirects) == 2
    assert all(response.closed for response in redirects)


def test_safe_request_wrappers_select_the_expected_checker(monkeypatch):
    seen = []

    def fake_checked(url, checker, **kwargs):
        seen.append((url, checker, kwargs))
        return "ok"

    monkeypatch.setattr(scraper_utils, "_safe_requests_get_checked", fake_checked)
    assert scraper_utils._safe_requests_get("https://example.com/a", timeout=1) == "ok"
    assert scraper_utils._safe_external_requests_get("https://example.com/feed") == "ok"
    assert seen[0][1] is scraper_utils._safe_url_for_fetch
    assert seen[1][1] is scraper_utils._safe_external_url_for_fetch


def test_domain_and_article_url_classification(monkeypatch):
    assert scraper_utils._is_high_value_domain("https://www.reuters.com/world/story") is True
    assert scraper_utils._is_high_value_domain("https://news.reuters.com/world/story") is True
    assert scraper_utils._is_high_value_domain("https://example.com/story") is False

    original_urlparse = scraper_utils.urlparse
    monkeypatch.setattr(scraper_utils, "urlparse", lambda _url: (_ for _ in ()).throw(ValueError("bad")))
    assert scraper_utils._is_high_value_domain("bad") is False
    monkeypatch.setattr(scraper_utils, "urlparse", original_urlparse)
    monkeypatch.setattr(scraper_utils, "URL_BLACKLIST", ["/tag/", "login"])

    assert scraper_utils.is_article_url("") is True
    assert scraper_utils.is_article_url("file:///etc/passwd") is False
    assert scraper_utils.is_article_url("https://example.com/tag/news") is False
    assert scraper_utils.is_article_url("https://example.com/story") is True


def test_quick_meta_extract_success_and_failures(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", False)
    assert scraper_utils._quick_meta_extract("https://example.com/article") == ""

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda url: "unsafe" not in url)
    assert scraper_utils._quick_meta_extract("https://unsafe.example/article") == ""

    monkeypatch.setattr(scraper_utils, "get_tls_policy_for_url", lambda _url: {"verify": True})
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(scraper_utils, "_safe_requests_get", lambda *_args, **_kwargs: FakeResponse(status_code=503))
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: SimpleNamespace(css=lambda _selector: []))
    assert scraper_utils._quick_meta_extract("https://example.com/article") == ""

    class MetaPage:
        def css(self, selector):
            if "name='description'" in selector:
                raise ValueError("selector unsupported")
            return [FakeNode(content="A useful description " * 5)]

    monkeypatch.setattr(scraper_utils, "_safe_requests_get", lambda *_args, **_kwargs: FakeResponse(text="page"))
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: MetaPage())
    assert scraper_utils._quick_meta_extract("https://example.com/article").startswith("A useful description")

    class EmptyMetaPage:
        def css(self, selector):
            if "name='description'" in selector:
                return []
            return [FakeNode(content="short")]

    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: EmptyMetaPage())
    assert scraper_utils._quick_meta_extract("https://example.com/article") == ""

    monkeypatch.setattr(
        scraper_utils,
        "_safe_requests_get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.Timeout("slow")),
    )
    assert scraper_utils._quick_meta_extract("https://example.com/article") == ""


def test_extract_content_uses_source_domain_generic_and_meta_fallback(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", False)
    assert scraper_utils.extract_content_from_html("<html/>") == ""

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "SOURCE_CONTENT_SELECTORS", {"Wire": [".wire"], "example.com": [".domain"]})
    monkeypatch.setattr(scraper_utils, "GENERIC_CONTENT_SELECTORS", ["article p", ".broken"])
    monkeypatch.setattr(scraper_utils, "CONTENT_NOISE_KEYWORDS", ["subscribe"])

    long_a = "First meaningful paragraph about semiconductor supply conditions."
    long_b = "Second meaningful paragraph adds enough detail for the summary output."

    class ContentPage:
        def css(self, selector):
            if selector == ".wire":
                return [FakeNode(long_a), FakeNode(long_b), FakeNode("subscribe " + long_b), FakeNode("short")]
            if selector == ".domain":
                return [FakeNode(long_a), FakeNode(long_b)]
            if selector == ".broken" or selector == "meta[name='description']":
                raise ValueError("bad selector")
            if selector == "meta[property='og:description']":
                return [FakeNode(content="Fallback description " * 4)]
            return []

    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: ContentPage())
    source_content = scraper_utils.extract_content_from_html("html", source_name="Wire")
    assert long_a in source_content and "subscribe" not in source_content
    domain_content = scraper_utils.extract_content_from_html("html", url="https://news.example.com/a")
    assert domain_content.startswith("First meaningful")

    monkeypatch.setattr(scraper_utils, "SOURCE_CONTENT_SELECTORS", {})
    monkeypatch.setattr(scraper_utils, "GENERIC_CONTENT_SELECTORS", [".broken"])
    assert scraper_utils.extract_content_from_html("html").startswith("Fallback description")

    class EmptyContentPage:
        def css(self, selector):
            if selector == "article p":
                return [FakeNode("too short")]
            if selector == "meta[name='description']":
                return []
            if selector == "meta[property='og:description']":
                return [FakeNode(content="short")]
            return []

    monkeypatch.setattr(scraper_utils, "SOURCE_CONTENT_SELECTORS", {"unmatched.example": [".never"]})
    monkeypatch.setattr(scraper_utils, "GENERIC_CONTENT_SELECTORS", ["article p"])
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: EmptyContentPage())
    assert scraper_utils.extract_content_from_html("html", url="https://example.com/no-match") == ""


def test_published_hint_selector_json_and_failure_paths(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", False)
    assert scraper_utils._extract_published_hint_from_html("html") == ""

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)

    class HintPage:
        def css(self, selector):
            if "article:published_time" in selector:
                raise ValueError("not supported")
            if "time[datetime]" in selector:
                return [FakeNode(datetime="2026-07-14T01:02:03Z")]
            return []

    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: HintPage())
    assert scraper_utils._extract_published_hint_from_html("html") == "2026-07-14T01:02:03Z"

    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: SimpleNamespace(css=lambda _selector: []))
    assert scraper_utils._extract_published_hint_from_html('{"datePublished":"2026-07-13"}') == "2026-07-13"
    monkeypatch.setattr(
        scraper_utils,
        "_Selector",
        lambda _html: SimpleNamespace(css=lambda _selector: [FakeNode(content="", datetime="")]),
    )
    assert scraper_utils._extract_published_hint_from_html("no published hint") == ""
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: (_ for _ in ()).throw(ValueError("bad html")))
    assert scraper_utils._extract_published_hint_from_html("html") == ""


def test_enrich_article_url_full_content_cache_and_meta_fallback(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_Selector", object())
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: True)
    monkeypatch.setattr(scraper_utils.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scraper_utils.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(
        scraper_utils,
        "get_tls_policy_for_url",
        lambda url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False, "url": url},
    )
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda _url: True)

    responses = [FakeResponse(status_code=503), FakeResponse(text="full html")]
    requested = []

    def fake_get(url, **_kwargs):
        requested.append(url)
        return responses.pop(0)

    monkeypatch.setattr(scraper_utils, "_safe_requests_get", fake_get)
    monkeypatch.setattr(scraper_utils, "_extract_published_hint_from_html", lambda _html: "2026-07-14T00:00:00Z")
    monkeypatch.setattr(scraper_utils, "extract_content_from_html", lambda *_args, **_kwargs: "C" * 120)
    content, meta = scraper_utils.enrich_article_url(
        "https://reuters.com/story", source_name="Reuters", return_meta=True
    )
    assert content == "C" * 120
    assert requested[1].startswith("https://webcache.googleusercontent.com/search?q=cache:")
    assert meta["published_at"] == "2026-07-14T00:00:00Z"
    assert meta["resolved_url"] == requested[1]

    requested.clear()
    responses[:] = [requests.exceptions.Timeout("cache miss"), FakeResponse(text="shell")]

    def digitimes_get(url, **_kwargs):
        requested.append(url)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(scraper_utils, "_safe_requests_get", digitimes_get)
    monkeypatch.setattr(scraper_utils, "extract_content_from_html", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(scraper_utils, "_extract_meta_from_html", lambda _html: "M" * 80)
    assert scraper_utils.enrich_article_url("https://digitimes.com/paywall", source_name="DIGITIMES") == "M" * 80
    assert "webcache" in requested[0] and requested[1] == "https://digitimes.com/paywall"


def test_enrich_article_url_empty_skipped_and_outer_error(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", False)
    assert scraper_utils.enrich_article_url("https://example.com/article") == ""

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_Selector", object())
    monkeypatch.setattr(scraper_utils.time, "sleep", lambda _seconds: None)
    checks = iter([True, False])
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: next(checks))
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda _url: False)
    empty, meta = scraper_utils.enrich_article_url("https://example.com/article", return_meta=True)
    assert empty == "" and meta["resolved_url"] == ""

    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: True)
    monkeypatch.setattr(
        scraper_utils,
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
    )
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(scraper_utils, "_safe_requests_get", lambda *_args, **_kwargs: FakeResponse(text="empty shell"))
    monkeypatch.setattr(scraper_utils, "_extract_published_hint_from_html", lambda _html: "")
    monkeypatch.setattr(scraper_utils, "extract_content_from_html", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(scraper_utils, "_extract_meta_from_html", lambda _html: "")
    empty, meta = scraper_utils.enrich_article_url("https://example.com/article", return_meta=True)
    assert empty == "" and meta["resolved_url"] == "https://example.com/article"

    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: True)
    monkeypatch.setattr(scraper_utils.time, "sleep", lambda _seconds: (_ for _ in ()).throw(RuntimeError("sleep failed")))
    empty, meta = scraper_utils.enrich_article_url("https://example.com/article", return_meta=True)
    assert empty == ""
    assert meta == {
        "requested_url": "https://example.com/article",
        "resolved_url": "",
        "tls_mode": "strict",
        "insecure_fetch": False,
        "published_at": "",
    }


def test_enrich_article_url_require_body_does_not_promote_source_meta(monkeypatch):
    class MetaOnlyPage:
        def css(self, selector):
            if selector.startswith("meta["):
                return [FakeNode(content="M" * 120)]
            return []

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: MetaOnlyPage())
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: True)
    monkeypatch.setattr(scraper_utils.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scraper_utils.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda _url: False)
    monkeypatch.setattr(
        scraper_utils,
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
    )
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(scraper_utils, "_safe_requests_get", lambda *_args, **_kwargs: FakeResponse(text="shell"))

    assert scraper_utils.enrich_article_url("https://example.com/article") == "M" * 120
    assert scraper_utils.enrich_article_url("https://example.com/article", require_body=True) == ""


def test_extract_meta_from_html_and_enrich_search_urls(monkeypatch):
    monkeypatch.setattr(scraper_utils, "scrapling_available", False)
    assert scraper_utils._extract_meta_from_html("html") == ""
    assert scraper_utils.enrich_search_urls(None) == {}

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)

    class MetaPage:
        def css(self, selector):
            if "og:description" in selector:
                raise ValueError("bad selector")
            return [FakeNode(content="Long fallback metadata " * 4)]

    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: MetaPage())
    assert scraper_utils._extract_meta_from_html("html").startswith("Long fallback metadata")
    monkeypatch.setattr(
        scraper_utils,
        "_Selector",
        lambda _html: SimpleNamespace(
            css=lambda selector: [] if "og:description" in selector else [FakeNode(content="short")]
        ),
    )
    assert scraper_utils._extract_meta_from_html("html") == ""
    monkeypatch.setattr(scraper_utils, "_Selector", lambda _html: (_ for _ in ()).throw(ValueError("bad html")))
    assert scraper_utils._extract_meta_from_html("html") == ""

    assert scraper_utils.enrich_search_urls({"text": "base"}) == {"text": "base"}
    result = {
        "text": "base",
        "grounding_sources": [
            {"title": "Deep", "url": "https://reuters.com/deep"},
            {"title": "High fallback", "url": "https://reuters.com/fallback"},
            {"title": "Ordinary", "url": "https://example.com/a"},
            {"title": "Missing", "url": ""},
        ],
    }
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda url: "reuters" in url)
    monkeypatch.setattr(
        scraper_utils,
        "enrich_article_url",
        lambda url, source_name="", *, require_body=False: "deep body" if url.endswith("deep") else "",
    )
    monkeypatch.setattr(scraper_utils, "_quick_meta_extract", lambda url: "fallback meta" if "fallback" in url else ("ordinary meta" if "example" in url else ""))
    enriched = scraper_utils.enrich_search_urls(result)
    assert "【正文深挖·一手源】Deep" in enriched["text"]
    assert "【摘要提取】High fallback" in enriched["text"]
    assert "【摘要提取】Ordinary" in enriched["text"]

    no_enrichment = {"text": "base", "grounding_sources": [{"title": "Empty", "url": "https://example.com/empty"}]}
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda _url: False)
    monkeypatch.setattr(scraper_utils, "_quick_meta_extract", lambda _url: "")
    assert scraper_utils.enrich_search_urls(no_enrichment)["text"] == "base"


@pytest.mark.parametrize(
    ("primary_body", "expected_primary_count", "expected_summary_count", "expected_tier"),
    [
        (False, 0, 1, "summary_lead_pending_verification"),
        (True, 1, 0, "primary_source_backed"),
    ],
)
def test_enrich_search_urls_can_attach_p7_evidence_provenance(
    monkeypatch,
    primary_body,
    expected_primary_count,
    expected_summary_count,
    expected_tier,
):
    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda _url: True)
    monkeypatch.setattr(
        scraper_utils,
        "enrich_article_url",
        lambda *_args, **_kwargs: "primary body" if primary_body else "",
    )
    monkeypatch.setattr(scraper_utils, "_quick_meta_extract", lambda _url: "summary body")

    result = scraper_utils.enrich_search_urls(
        {
            "text": "base",
            "grounding_sources": [{"url": "https://example.com/article", "title": "Example"}],
        },
        include_evidence_metadata=True,
    )

    provenance = result["scrapling_evidence_provenance"]
    assert provenance["primary_source_count"] == expected_primary_count
    assert provenance["summary_source_count"] == expected_summary_count
    assert provenance["source_url_count"] == 1
    assert provenance["evidence_tier"] == expected_tier


def test_vertex_grounding_redirect_enters_verified_target_body_path(monkeypatch):
    wrapper_url = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/opaque-token"
    )
    publisher_url = "https://www.reuters.com/world/verified-story"
    redirect_response = FakeResponse(
        status_code=302,
        headers={"Location": publisher_url},
        url=wrapper_url,
        redirect=True,
    )
    resolver_calls = []
    body_calls = []

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(
        scraper_utils,
        "get_tls_policy_for_url",
        lambda url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False, "url": url},
    )
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda url: url == publisher_url)

    def fake_external_get(url, **kwargs):
        resolver_calls.append((url, kwargs))
        return redirect_response

    def fake_body_enrichment(url, source_name="", *, require_body=False):
        body_calls.append((url, source_name, require_body))
        return "B" * 120

    monkeypatch.setattr(scraper_utils, "_safe_external_requests_get", fake_external_get)
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda url: url == publisher_url)
    monkeypatch.setattr(scraper_utils, "enrich_article_url", fake_body_enrichment)
    monkeypatch.setattr(
        scraper_utils,
        "_quick_meta_extract",
        lambda _url: pytest.fail("已解析目标页时不得回退为搜索摘要"),
    )

    result = scraper_utils.enrich_search_urls(
        {
            "text": "base",
            "grounding_sources": [{"url": wrapper_url, "title": "Reuters"}],
        },
        include_evidence_metadata=True,
    )

    assert resolver_calls == [(wrapper_url, {"headers": scraper_utils.HEADERS, "timeout": 8, "verify": True, "follow_redirects": False})]
    assert redirect_response.closed is True
    assert body_calls == [(publisher_url, "", True)]
    assert "【正文深挖·一手源】Reuters" in result["text"]
    assert result["grounding_sources"] == [
        {
            "url": publisher_url,
            "title": "Reuters",
            "grounding_redirect_url": wrapper_url,
        }
    ]
    assert result["scrapling_evidence_provenance"] == {
        "primary_source_count": 1,
        "summary_source_count": 0,
        "source_url_count": 1,
        "evidence_tier": "primary_source_backed",
    }


def test_vertex_redirect_without_real_body_stays_summary_lead(monkeypatch):
    wrapper_url = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/opaque-token"
    )
    publisher_url = "https://www.reuters.com/world/no-body"

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(
        scraper_utils,
        "_resolve_vertex_grounding_redirect_url",
        lambda _url: publisher_url,
        raising=False,
    )
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda url: url == publisher_url)
    monkeypatch.setattr(
        scraper_utils,
        "enrich_article_url",
        lambda _url, source_name="", *, require_body=False: "",
    )
    seen_meta_urls = []

    def fake_meta(url):
        seen_meta_urls.append(url)
        return "发布页 description 仅作摘要线索"

    monkeypatch.setattr(scraper_utils, "_quick_meta_extract", fake_meta)

    result = scraper_utils.enrich_search_urls(
        {
            "text": "base",
            "grounding_sources": [{"url": wrapper_url, "title": "Reuters"}],
        },
        include_evidence_metadata=True,
    )

    assert seen_meta_urls == [publisher_url]
    assert "【正文深挖·一手源】" not in result["text"]
    assert "【摘要提取】Reuters" in result["text"]
    assert result["scrapling_evidence_provenance"] == {
        "primary_source_count": 0,
        "summary_source_count": 1,
        "source_url_count": 1,
        "evidence_tier": "summary_lead_pending_verification",
    }


def test_vertex_grounding_redirect_rejects_unsafe_location(monkeypatch):
    wrapper_url = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/opaque-token"
    )
    redirect_response = FakeResponse(
        status_code=302,
        headers={"Location": "http://127.0.0.1/internal"},
        url=wrapper_url,
        redirect=True,
    )

    monkeypatch.setattr(
        scraper_utils,
        "get_tls_policy_for_url",
        lambda _url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
    )
    monkeypatch.setattr(scraper_utils, "log_tls_policy", lambda *_args: None)
    monkeypatch.setattr(
        scraper_utils,
        "_safe_external_requests_get",
        lambda *_args, **_kwargs: redirect_response,
    )
    monkeypatch.setattr(scraper_utils, "_safe_url_for_fetch", lambda _url: False)

    assert scraper_utils._resolve_vertex_grounding_redirect_url(wrapper_url) == wrapper_url
    assert redirect_response.closed is True


def test_existing_publisher_body_bypasses_vertex_resolver(monkeypatch):
    publisher_url = "https://www.reuters.com/world/existing-body"

    monkeypatch.setattr(scraper_utils, "scrapling_available", True)
    monkeypatch.setattr(
        scraper_utils,
        "_resolve_vertex_grounding_redirect_url",
        lambda _url: pytest.fail("既有发布页不应进入 Vertex 跳转解析"),
        raising=False,
    )
    monkeypatch.setattr(scraper_utils, "_is_high_value_domain", lambda url: url == publisher_url)
    monkeypatch.setattr(
        scraper_utils,
        "enrich_article_url",
        lambda _url, source_name="", *, require_body=False: "现有有效正文" if require_body else pytest.fail("正文路径必须要求真实正文"),
    )
    monkeypatch.setattr(
        scraper_utils,
        "_quick_meta_extract",
        lambda _url: pytest.fail("既有有效正文不应回退为摘要"),
    )

    result = scraper_utils.enrich_search_urls(
        {
            "text": "base",
            "grounding_sources": [{"url": publisher_url, "title": "Reuters"}],
        },
        include_evidence_metadata=True,
    )

    assert result["grounding_sources"] == [{"url": publisher_url, "title": "Reuters"}]
    assert result["text"].endswith("【正文深挖·一手源】Reuters\n现有有效正文")
    assert result["scrapling_evidence_provenance"]["primary_source_count"] == 1
    assert result["scrapling_evidence_provenance"]["evidence_tier"] == "primary_source_backed"


def test_parse_rss_date_and_fetch_rss_entries(monkeypatch):
    assert p1_rss._parse_rss_date({"published_parsed": (2026, 7, 14, 1, 2, 3)}) == "2026-07-14T09:02:03 (北京时间)"
    fallback_entry = {
        "published_parsed": ("bad",),
        "updated_parsed": (2026, 7, 13, 16, 0, 0),
    }
    assert p1_rss._parse_rss_date(fallback_entry) == "2026-07-14T00:00:00 (北京时间)"
    raw_published = "Tue, 14 Jul 2026 01:02:03 GMT"
    assert p1_rss._parse_rss_date({"published": raw_published}) == raw_published[:19]

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(content=b"feed")

    feed = SimpleNamespace(
        bozo=False,
        entries=[
            {
                "title": "  First story  ",
                "summary": "<p>Summary <b>body</b></p>",
                "link": "https://example.com/first",
                "published_parsed": (2026, 7, 14, 1, 2, 3),
            },
            {"title": "", "summary": "ignored"},
            {
                "title": "Second story",
                "description": "Description fallback",
                "link": "https://example.com/second",
                "updated": "2026-07-14T03:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", fake_get)
    monkeypatch.setattr(p1_rss.feedparser, "parse", lambda content: feed if content == b"feed" else None)
    entries = p1_rss._fetch_rss("SEC", "https://sec.example/feed", "sec_filings")
    assert [entry["title"] for entry in entries] == ["First story", "Second story"]
    assert entries[0]["content"] == "Summary body"
    assert calls[0][1]["headers"]["User-Agent"] == p1_rss.SEC_USER_AGENT

    p1_rss._fetch_rss("Normal", "https://example.com/feed", "normal")
    assert calls[1][1]["headers"]["User-Agent"] == p1_rss.FAKE_UA

    monkeypatch.setattr(p1_rss.feedparser, "parse", lambda _content: SimpleNamespace(bozo=True, entries=[]))
    with pytest.raises(p1_rss.SourceFetchError, match="RSS 解析失败"):
        p1_rss._fetch_rss("Broken", "https://example.com/feed", "normal")
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("slow")))
    with pytest.raises(p1_rss.SourceFetchError, match="slow"):
        p1_rss._fetch_rss("Timeout", "https://example.com/feed", "normal")


def test_technews_configured_cdn_fallback_recovers_ssl_eof_without_changing_other_feed(monkeypatch):
    """A TechNews TLS EOF must use its configured fresh CDN feed only."""
    InlineExecutor.instances.clear()
    technews = next(feed for feed in p1_rss.V2_RSS_FEEDS if feed["name"] == "TechNews_TW")
    normal = next(feed for feed in p1_rss.V2_RSS_FEEDS if feed["name"] == "TechCrunch")
    primary_url = "https://technews.tw/feed/"
    fallback_url = "https://cdn.technews.tw/tn-rss/"
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == primary_url:
            raise requests.exceptions.SSLError(
                ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
            )
        if url == fallback_url:
            return FakeResponse(content=b"fresh-technews")
        if url == normal["url"]:
            return FakeResponse(content=b"fresh-normal")
        pytest.fail(f"unexpected RSS request: {url}")

    def parse_feed(content):
        if content == b"fresh-technews":
            return SimpleNamespace(
                bozo=False,
                entries=[
                    {
                        "title": "Fresh TechNews",
                        "summary": "fresh CDN RSS payload",
                        "link": "https://technews.tw/2026/08/31/fresh-technews/",
                        "published": "Mon, 31 Aug 2026 13:39:46 +0000",
                    }
                ],
            )
        if content == b"fresh-normal":
            return SimpleNamespace(
                bozo=False,
                entries=[
                    {
                        "title": "Fresh normal source",
                        "summary": "normal primary RSS payload",
                        "link": "https://techcrunch.com/fresh-normal/",
                        "published": "Mon, 31 Aug 2026 13:39:46 +0000",
                    }
                ],
            )
        pytest.fail(f"unexpected RSS payload: {content!r}")

    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [dict(technews), dict(normal)])
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", [])
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", fake_get)
    monkeypatch.setattr(p1_rss.feedparser, "parse", parse_feed)
    monkeypatch.setattr(p1_rss, "_fetch_hackernews", lambda: [])
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda *_args: True)
    monkeypatch.setattr(p1_rss, "_scrapling_available", False)
    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)

    results, stats = p1_rss.collect_from_rss("24h")

    assert stats["failed"] == []
    assert {name for name, _count in stats["success"]} == {"TechNews_TW", "TechCrunch"}
    assert [url for url, _kwargs in calls] == [primary_url, fallback_url, normal["url"]]
    assert calls[0][1]["timeout"] == p1_rss.REQUEST_TIMEOUT
    assert calls[0][1]["headers"]["User-Agent"] == p1_rss.FAKE_UA
    text = "\n".join(result["text"] for result in results)
    assert "Fresh TechNews" in text
    assert "Fresh normal source" in text


def test_technews_cdn_fallback_exhaustion_keeps_each_ssl_eof_candidate(monkeypatch):
    primary_url = "https://technews.tw/feed/"
    fallback_url = "https://cdn.technews.tw/tn-rss/"
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        raise requests.exceptions.SSLError(
            ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        )

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", fake_get)

    with pytest.raises(p1_rss.SourceFetchError) as exc_info:
        p1_rss._fetch_rss("TechNews_TW", primary_url, "technews_tw", (fallback_url,))

    assert calls == [primary_url, fallback_url]
    assert primary_url in str(exc_info.value)
    assert fallback_url in str(exc_info.value)
    assert "UNEXPECTED_EOF_WHILE_READING" in str(exc_info.value)


def test_technews_fallback_does_not_change_other_configured_source_task_arguments(monkeypatch):
    """Every non-TechNews source keeps the original three-argument RSS call."""
    InlineExecutor.instances.clear()
    feeds = [dict(feed) for feed in p1_rss.V2_RSS_FEEDS]
    subreddits = list(p1_rss.V2_REDDIT_SUBS)
    rss_calls = []

    def entry(title, source):
        return {
            "title": title,
            "content": "fresh source payload",
            "url": f"https://example.com/{source}",
            "published_at": "new",
            "source": source,
        }

    def fake_fetch(*args):
        rss_calls.append(args)
        name, _url, source = args[:3]
        return [entry(name, source)]

    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", feeds)
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", subreddits)
    monkeypatch.setattr(p1_rss, "_fetch_rss", fake_fetch)
    monkeypatch.setattr(p1_rss, "_fetch_reddit", lambda sub: [entry(f"Reddit/r/{sub}", f"reddit_{sub}")])
    monkeypatch.setattr(p1_rss, "_fetch_hackernews", lambda: [entry("HackerNews", "hackernews")])
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda *_args: True)
    monkeypatch.setattr(p1_rss, "_scrapling_available", False)
    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)

    results, stats = p1_rss.collect_from_rss("24h")

    technews_call = next(args for args in rss_calls if args[0] == "TechNews_TW")
    other_rss_calls = [args for args in rss_calls if args[0] != "TechNews_TW"]
    assert technews_call[3] == ("https://cdn.technews.tw/tn-rss/",)
    assert len(other_rss_calls) == len(feeds) - 1
    assert all(len(args) == 3 for args in other_rss_calls)
    assert {(args[0], args[1], args[2]) for args in other_rss_calls} == {
        (feed["name"], feed["url"], feed["source"])
        for feed in feeds
        if feed["name"] != "TechNews_TW"
    }
    assert stats["failed"] == []
    assert len(stats["success"]) == len(feeds) + len(subreddits) + 1
    assert results


def test_fetch_reddit_json_and_rss_fallback(monkeypatch):
    payload = {
        "data": {
            "children": [
                {"data": {"title": "Timed", "selftext": "body", "permalink": "/r/hardware/1", "created_utc": 1}},
                {"data": {"title": "Untimed", "selftext": "", "permalink": "/r/hardware/2", "created_utc": 0}},
            ]
        }
    }
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(payload=payload))
    entries = p1_rss._fetch_reddit("hardware")
    assert len(entries) == 2
    assert entries[0]["published_at"].endswith("(北京时间)")
    assert entries[1]["published_at"] == ""
    assert entries[0]["url"] == "https://www.reddit.com/r/hardware/1"

    fallbacks = []
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(status_code=503))
    monkeypatch.setattr(p1_rss, "_fetch_rss", lambda *args: fallbacks.append(args) or [{"fallback": True}])
    assert p1_rss._fetch_reddit("chips") == [{"fallback": True}]
    assert fallbacks[-1] == ("Reddit/chips", "https://www.reddit.com/r/chips/new/.rss?limit=50", "reddit_chips")

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad json")))
    assert p1_rss._fetch_reddit("chips") == [{"fallback": True}]


def test_fetch_hackernews_story_variants_and_top_level_failure(monkeypatch):
    def fake_get(url, **_kwargs):
        if url.endswith("newstories.json"):
            return FakeResponse(payload=[1, 2, 3, 4])
        item_id = int(url.rsplit("/", 1)[1].split(".", 1)[0])
        if item_id == 1:
            return FakeResponse(payload={"type": "story", "title": "One", "time": 1, "url": "https://example.com/one"})
        if item_id == 2:
            return FakeResponse(payload={"type": "comment", "title": "Not a story"})
        if item_id == 3:
            return FakeResponse(payload={"type": "story", "title": "Three", "time": 0})
        raise requests.Timeout("item failed")

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", fake_get)
    entries = p1_rss._fetch_hackernews()
    assert {entry["title"] for entry in entries} == {"One", "Three"}
    three = next(entry for entry in entries if entry["title"] == "Three")
    assert three["published_at"] == ""
    assert three["url"] == "https://news.ycombinator.com/item?id=3"

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad ids")))
    with pytest.raises(p1_rss.SourceFetchError, match="bad ids"):
        p1_rss._fetch_hackernews()


def test_fetch_hackernews_future_error_and_timeout(monkeypatch):
    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", lambda *_args, **_kwargs: FakeResponse(payload=[1, 2]))

    class ResultFuture:
        def __init__(self, error=False):
            self.error = error
            self.cancelled = False

        def result(self):
            if self.error:
                raise RuntimeError("worker failed")
            return {"title": "unused"}

        def cancel(self):
            self.cancelled = True

    class Executor:
        def __init__(self, max_workers):
            assert max_workers == 10
            self.created = []

        def submit(self, _fn, item_id):
            future = ResultFuture(error=item_id == 1)
            self.created.append(future)
            return future

        def shutdown(self, *, wait, cancel_futures):
            assert (wait, cancel_futures) == (False, True)

    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", Executor)

    def partial_wait(futures, **_kwargs):
        futures = list(futures)
        return {futures[0]}, {futures[1]}

    monkeypatch.setattr(p1_rss, "wait", partial_wait)
    with pytest.raises(p1_rss.SourceFetchError, match="HackerNews 详情请求全部失败"):
        p1_rss._fetch_hackernews()


def test_fetch_hackernews_detail_failures_require_one_successful_detail_request(monkeypatch):
    def all_details_fail(url, **_kwargs):
        if url.endswith("newstories.json"):
            return FakeResponse(payload=[1, 2])
        return FakeResponse(status_code=503, payload={"error": "detail unavailable"})

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", all_details_fail)
    with pytest.raises(p1_rss.SourceFetchError, match="HackerNews 详情请求全部失败"):
        p1_rss._fetch_hackernews()

    def one_detail_succeeds_without_story(url, **_kwargs):
        if url.endswith("newstories.json"):
            return FakeResponse(payload=[1, 2])
        if url.endswith("/1.json"):
            return FakeResponse(payload={"type": "comment", "title": "not a story"})
        raise requests.Timeout("detail timeout")

    monkeypatch.setattr(p1_rss, "_safe_external_requests_get", one_detail_succeeds_without_story)
    assert p1_rss._fetch_hackernews() == []


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("2026-07-14T08:00:00+08:00", datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ("2026-07-14 08:00:00 (北京时间)", datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ("Jul 14, 2026 3:00p", datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)),
        ("July 14, 2026", datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ("not a date", None),
    ],
)
def test_parse_published_datetime_formats(published, expected):
    assert p1_rss._parse_published_datetime(published) == expected


def test_parse_published_datetime_handles_unexpected_datetime_error(monkeypatch):
    class BrokenDatetime:
        @staticmethod
        def fromisoformat(_value):
            raise RuntimeError("datetime parser unavailable")

    monkeypatch.setattr(p1_rss, "datetime", BrokenDatetime)
    assert p1_rss._parse_published_datetime("2026-07-14 08:00") is None


def test_window_and_article_enrichment_paths(monkeypatch):
    assert p1_rss._is_within_window(None, 24) is True
    assert p1_rss._is_within_window("not a date", 24) is False
    assert p1_rss._is_within_window("2020-01-01T00:00:00Z", 24) is False
    assert p1_rss._is_within_window((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 24) is False
    assert p1_rss._is_within_window(_recent_timestamp(), 24) is True

    monkeypatch.setattr(p1_rss, "_scrapling_available", False)
    assert p1_rss._enrich_article({"url": "https://example.com/a"}) == ""
    monkeypatch.setattr(p1_rss, "_scrapling_available", True)
    assert p1_rss._enrich_article({}) == ""
    monkeypatch.setattr(p1_rss, "_is_article_url", lambda _url: False)
    assert p1_rss._enrich_article({"url": "https://example.com/a"}) == ""

    monkeypatch.setattr(p1_rss, "_is_article_url", lambda _url: True)
    monkeypatch.setattr(scraper_utils, "enrich_article_url", lambda url, source_name="": f"{source_name}:{url}")
    assert p1_rss._enrich_article({"url": "https://example.com/a", "_feed_name": "Feed"}) == "Feed:https://example.com/a"
    monkeypatch.setattr(scraper_utils, "enrich_article_url", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("parse")))
    assert p1_rss._enrich_article({"url": "https://example.com/a", "source": "fallback"}) == ""


def test_entries_to_result_filters_formats_and_limits_sources(monkeypatch):
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda published, _hours: published != "old")
    assert p1_rss._entries_to_p1_result([{"published_at": "old"}], "Cat", "Feed", 24) is None

    entries = [
        {"title": "Title", "content": "Title", "url": "", "published_at": "now"},
        {"title": "Story", "content": "Detailed content", "url": "https://example.com/1", "published_at": "now", "source": "wire"},
    ]
    entries.extend(
        {"title": f"Story {index}", "content": "Body", "url": f"https://example.com/{index}", "published_at": "now"}
        for index in range(2, 13)
    )
    result = p1_rss._entries_to_p1_result(entries, "Category", "Feed", 24)
    assert result["category"] == "Category"
    assert "1. [Feed] Title" in result["text"]
    assert "[wire] Story — Detailed content" in result["text"]
    assert len(result["grounding_sources"]) == 10


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Reuters World", "主流外媒"),
        ("Semiconductor News", "半导体产业链"),
        ("Seeking Alpha", "财务锚点"),
        ("SEC Edgar", "官方公告"),
        ("NVIDIA Blog", "算力核心"),
        ("Lightwave Optical", "光通信产业链"),
        ("Datacenter Knowledge", "数据中心基建"),
        ("SK Hynix HBM", "HBM与存储"),
        ("FierceElectronics", "电子供应链"),
        ("DigiTimes Tech", "产业链动态"),
        ("Miscellaneous", "综合RSS"),
    ],
)
def test_rss_category_mapping(name, category):
    assert p1_rss._rss_name_to_category(name) == category


def test_tokenization_and_deduplication_merge_sources():
    assert p1_rss._tokenize_entry_text("AI 芯片 224G") == {"ai", "芯", "片", "224g"}
    blank = {"title": "", "content": "", "source": "blank"}
    first = {"title": "AI chip demand rises", "content": "supply remains tight", "source": "wire-a"}
    duplicate = {"title": "AI chip demand rises", "content": "supply remains tight", "source": "wire-b"}
    same_source = {"title": "AI chip demand rises", "content": "supply remains tight", "source": "wire-b"}
    distinct = {"title": "Optical module shipments", "content": "new capacity", "source": "wire-c"}
    unique = p1_rss._deduplicate_entries([blank, first, duplicate, same_source, distinct])
    assert unique == [blank, first, distinct]
    assert first["source"] == "wire-a, wire-b"


def test_collect_from_rss_mixed_phase1_and_phase2_results(monkeypatch):
    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [
        {"name": "Good", "url": "good", "source": "good"},
        {"name": "Old", "url": "old", "source": "old"},
        {"name": "Empty", "url": "empty", "source": "empty"},
        {"name": "Boom", "url": "boom", "source": "boom"},
        {"name": "Disabled", "url": "", "source": "disabled"},
    ])
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", [])

    def fake_fetch(_name, url, source):
        if url == "boom":
            raise RuntimeError("feed exploded")
        if url == "empty":
            return []
        if url == "old":
            return [{"title": "Old", "content": "old", "url": "https://example.com/old", "published_at": "old", "source": source}]
        return [
            {"title": "Good", "content": "short", "url": "https://example.com/good", "published_at": "new", "source": source},
            {"title": "No body", "content": "short", "url": "https://example.com/empty", "published_at": "new", "source": source},
            {"title": "Worker error", "content": "short", "url": "https://example.com/error", "published_at": "new", "source": source},
        ]

    monkeypatch.setattr(p1_rss, "_fetch_rss", fake_fetch)
    monkeypatch.setattr(p1_rss, "_fetch_hackernews", lambda: [])
    monkeypatch.setattr(p1_rss, "parse_backtrack_hours", lambda _value: 24)
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda published, _hours: published != "old")
    monkeypatch.setattr(p1_rss, "_scrapling_available", True)

    def fake_enrich(entry):
        if entry["title"] == "Worker error":
            raise RuntimeError("enrichment failed")
        return "enriched body" if entry["title"] == "Good" else ""

    monkeypatch.setattr(p1_rss, "_enrich_article", fake_enrich)
    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_rss.random, "shuffle", lambda _entries: None)

    results, stats = p1_rss.collect_from_rss("24h")
    assert stats["total"] == 5
    assert stats["success"] == [("Good", 3)]
    assert ("Boom", "feed exploded") in stats["failed"]
    assert {name for name, _reason in stats["empty"]} == {"Old", "Empty", "HackerNews"}
    assert stats["enrich_ok"] == 1
    assert stats["enrich_fail"] == 2
    assert len(stats["failed_urls"]) == 2
    assert len(results) == 1
    assert "enriched body" in results[0]["text"]
    assert [executor.max_workers for executor in InlineExecutor.instances] == [8, 10]
    assert all(executor.shutdown_args == (False, True) for executor in InlineExecutor.instances)


def test_collect_from_rss_phase_timeouts_and_no_scrapling(monkeypatch):
    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [{"name": "Good", "url": "good", "source": "good"}])
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", [])
    monkeypatch.setattr(
        p1_rss,
        "_fetch_rss",
        lambda *_args: [{"title": "Good", "content": "body", "url": "https://example.com/good", "published_at": "new", "source": "good"}],
    )
    monkeypatch.setattr(p1_rss, "_fetch_hackernews", lambda: [])
    monkeypatch.setattr(p1_rss, "parse_backtrack_hours", lambda _value: 24)
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda *_args: True)
    monkeypatch.setattr(p1_rss, "_scrapling_available", True)
    monkeypatch.setattr(p1_rss, "_enrich_article", lambda _entry: "late body")
    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_rss.random, "shuffle", lambda _entries: None)
    calls = 0

    def timeout_wait(futures, **_kwargs):
        nonlocal calls
        calls += 1
        futures = list(futures)
        if calls == 1:
            return {futures[0]}, set(futures[1:])
        return set(), set(futures)

    monkeypatch.setattr(p1_rss, "wait", timeout_wait)
    results, stats = p1_rss.collect_from_rss("24h")
    assert results and "body" in results[0]["text"]
    assert stats["enrich_ok"] == 0
    assert stats["enrich_fail"] == 1
    assert any(name == "HackerNews" and "超时" in reason for name, reason in stats["failed"])
    assert InlineExecutor.instances[0].futures[1].cancelled is True
    assert InlineExecutor.instances[1].futures[0].cancelled is True

    InlineExecutor.instances.clear()
    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [])
    monkeypatch.setattr(p1_rss, "_scrapling_available", False)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    results, stats = p1_rss.collect_from_rss("24h")
    assert results == []
    assert stats["enrich_ok"] == stats["enrich_fail"] == 0


def test_collect_from_rss_task_future_errors_and_reddit_task(monkeypatch):
    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [{"name": "Feed", "url": "feed", "source": "feed"}])
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", ["hardware"])
    monkeypatch.setattr(p1_rss, "_scrapling_available", True)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)

    class ErrorFuture:
        def result(self):
            raise RuntimeError("future failed")

        def cancel(self):
            return True

    class ErrorExecutor:
        def __init__(self, max_workers):
            assert max_workers == 8

        def submit(self, _fn, *_args):
            return ErrorFuture()

        def shutdown(self, *, wait, cancel_futures):
            assert (wait, cancel_futures) == (False, True)

    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", ErrorExecutor)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    results, stats = p1_rss.collect_from_rss("24h")
    assert results == []
    assert stats["total"] == 3
    assert len(stats["failed"]) == 3
    assert {name for name, _reason in stats["failed"]} == {"Feed", "Reddit/r/hardware", "HackerNews"}


def test_collect_from_rss_logs_dedup_and_skips_empty_result(monkeypatch):
    InlineExecutor.instances.clear()
    duplicate = {
        "title": "Same semiconductor story",
        "content": "identical detailed body",
        "url": "https://example.com/story",
        "published_at": "new",
        "source": "feed",
    }
    monkeypatch.setattr(p1_rss, "V2_RSS_FEEDS", [{"name": "Feed", "url": "feed", "source": "feed"}])
    monkeypatch.setattr(p1_rss, "V2_REDDIT_SUBS", [])
    monkeypatch.setattr(p1_rss, "_fetch_rss", lambda *_args: [dict(duplicate), dict(duplicate)])
    monkeypatch.setattr(p1_rss, "_fetch_hackernews", lambda: [])
    monkeypatch.setattr(p1_rss, "_is_within_window", lambda *_args: True)
    monkeypatch.setattr(p1_rss, "_scrapling_available", False)
    monkeypatch.setattr(p1_rss, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(p1_rss, "wait", _all_done)
    monkeypatch.setattr(p1_rss.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(p1_rss, "_entries_to_p1_result", lambda *_args: None)
    results, stats = p1_rss.collect_from_rss("24h")
    assert results == []
    assert stats["success"] == [("Feed", 2)]
