from __future__ import annotations

import json
import smtplib
import socket
import struct
from contextlib import contextmanager
from datetime import datetime
import pytest


def test_email_message_falls_back_to_escaped_preformatted_html(monkeypatch):
    import p9_email

    monkeypatch.setattr(
        p9_email,
        "_render_safe_report_html",
        lambda _content: (_ for _ in ()).throw(ImportError("markdown unavailable")),
    )

    msg = p9_email.build_report_email_message(
        "<script>alert(1)</script>",
        sender_email="sender@example.com",
        receiver_email="one@example.com,two@example.com",
        now=datetime(2026, 7, 14, 9, 30),
    )

    assert msg["From"]
    assert msg["To"] == "one@example.com,two@example.com"
    assert "&lt;script&gt;" in msg.get_payload(decode=True).decode("utf-8")


@pytest.mark.parametrize(
    ("value", "expected_fake", "expected_usable"),
    [
        ("not-an-ip", False, False),
        ("198.18.0.1", True, False),
        ("172.19.1.2", True, False),
        ("127.0.0.1", False, False),
        ("169.254.1.1", False, False),
        ("224.0.0.1", False, False),
        ("0.0.0.0", False, False),
        ("::1", False, False),
        ("10.10.0.5", False, True),
        ("8.8.8.8", False, False),
    ],
)
def test_email_ip_classification(value, expected_fake, expected_usable):
    import p9_email

    assert p9_email._is_fake_ip(value) is expected_fake
    assert p9_email._is_usable_source_ip(value) is expected_usable


def test_candidate_source_ips_deduplicates_and_ignores_lookup_failures(monkeypatch):
    import p9_email

    monkeypatch.setattr(p9_email.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(
        p9_email.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, 0, 0, "", ("10.0.0.2", 0)),
            (socket.AF_INET, 0, 0, "", ("198.18.0.2", 0)),
        ],
    )
    monkeypatch.setattr(
        p9_email.socket,
        "gethostbyname_ex",
        lambda _host: ("host", [], ["10.0.0.2", "192.168.1.3", "bad"]),
    )
    assert p9_email._candidate_source_ips() == ["10.0.0.2", "192.168.1.3"]

    monkeypatch.setattr(
        p9_email.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("dns down")),
    )
    monkeypatch.setattr(
        p9_email.socket,
        "gethostbyname_ex",
        lambda _host: (_ for _ in ()).throw(OSError("dns down")),
    )
    assert p9_email._candidate_source_ips() == []


class _DnsResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_resolve_real_smtp_ips_combines_system_and_doh(monkeypatch):
    import p9_email

    monkeypatch.setattr(
        p9_email.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, 0, 0, "", ("1.1.1.1", 465)),
            (socket.AF_INET, 0, 0, "", ("198.18.0.1", 465)),
            (socket.AF_INET6, 0, 0, "", ("::1", 465)),
        ],
    )
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 2:
            raise OSError("DoH unavailable")
        if len(calls) == 3:
            return _DnsResponse({})
        return _DnsResponse(
            {
                "Answer": [
                    {"type": 1, "data": "2.2.2.2"},
                    {"type": 1, "data": "1.1.1.1"},
                    {"type": 28, "data": "::1"},
                    {"type": 1, "data": "invalid"},
                ]
            }
        )

    monkeypatch.setattr(p9_email.urllib.request, "urlopen", fake_urlopen)
    assert p9_email._resolve_real_smtp_ips() == ["1.1.1.1", "2.2.2.2"]
    assert len(calls) == len(p9_email.REAL_DNS_ENDPOINTS)

    monkeypatch.setattr(
        p9_email.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("system DNS down")),
    )
    monkeypatch.setattr(
        p9_email.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("DoH down")),
    )
    assert p9_email._resolve_real_smtp_ips() == []


def test_patched_smtp_dns_only_rewrites_smtp_host(monkeypatch):
    import p9_email

    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append((host, port, args, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

    monkeypatch.setattr(p9_email.socket, "getaddrinfo", fake_getaddrinfo)
    original = p9_email.socket.getaddrinfo
    with p9_email._patched_smtp_dns("1.2.3.4"):
        p9_email.socket.getaddrinfo(p9_email.SMTP_HOST, 465)
        p9_email.socket.getaddrinfo("other.example", 443)
    assert calls[0][0] == "1.2.3.4"
    assert calls[1][0] == "other.example"
    assert p9_email.socket.getaddrinfo is original


class _FakeSmtp:
    instances = []

    def __init__(self, host, port, *, timeout, source_address, context):
        self.args = (host, port, timeout, source_address, context)
        self.events = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def set_debuglevel(self, level):
        self.events.append(("debug", level))

    def login(self, sender, password):
        self.events.append(("login", sender, password))

    def sendmail(self, sender, receivers, message):
        self.events.append(("send", sender, receivers, message))


def test_send_message_uses_source_address_and_optional_remote_ip(monkeypatch):
    import p9_email

    _FakeSmtp.instances.clear()
    monkeypatch.setattr(p9_email.smtplib, "SMTP_SSL", _FakeSmtp)
    monkeypatch.setattr(p9_email.ssl, "create_default_context", lambda: "tls")
    patched = []

    @contextmanager
    def fake_patch(remote_ip):
        patched.append(remote_ip)
        yield

    monkeypatch.setattr(p9_email, "_patched_smtp_dns", fake_patch)
    msg = p9_email.build_report_email_message(
        "body",
        sender_email="sender@example.com",
        receiver_email="one@example.com, two@example.com",
    )

    p9_email._send_message(
        msg,
        sender_email="sender@example.com",
        sender_password="secret",  # pragma: allowlist secret
        receiver_email="one@example.com, two@example.com",
        source_ip="10.0.0.2",
        remote_ip="1.2.3.4",
    )
    p9_email._send_message(
        msg,
        sender_email="sender@example.com",
        sender_password="secret",  # pragma: allowlist secret
        receiver_email="one@example.com",
    )

    assert patched == ["1.2.3.4"]
    assert _FakeSmtp.instances[0].args[3] == ("10.0.0.2", 0)
    assert _FakeSmtp.instances[1].args[3] is None
    assert _FakeSmtp.instances[0].events[-1][2] == ["one@example.com", "two@example.com"]


def test_send_report_email_primary_and_fallback_outcomes(monkeypatch):
    import p9_email

    monkeypatch.setattr(p9_email, "build_report_email_message", lambda *_args, **_kwargs: "msg")
    calls = []
    monkeypatch.setattr(p9_email, "_send_message", lambda *_args, **kwargs: calls.append(kwargs))
    p9_email.send_report_via_email(
        "content",
        sender_email="sender@example.com",
        sender_password="secret",
        receiver_email="receiver@example.com",
    )
    assert len(calls) == 1

    def auth_failure(*_args, **_kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(p9_email, "_send_message", auth_failure)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        p9_email.send_report_via_email(
            "content",
            sender_email="sender@example.com",
            sender_password="secret",
            receiver_email="receiver@example.com",
        )

    attempts = []

    def succeed_on_fallback(*_args, **kwargs):
        attempts.append(kwargs)
        if kwargs.get("remote_ip") is None:
            raise OSError("primary route down")

    monkeypatch.setattr(p9_email, "_send_message", succeed_on_fallback)
    monkeypatch.setattr(p9_email, "_resolve_real_smtp_ips", lambda: ["1.1.1.1"])
    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: ["10.0.0.2"])
    p9_email.send_report_via_email(
        "content",
        sender_email="sender@example.com",
        sender_password="secret",
        receiver_email="receiver@example.com",
    )
    assert attempts[-1]["remote_ip"] == "1.1.1.1"

    def always_fail(*_args, **_kwargs):
        raise OSError("all routes down")

    monkeypatch.setattr(p9_email, "_send_message", always_fail)
    with pytest.raises(OSError, match="all routes down"):
        p9_email.send_report_via_email(
            "content",
            sender_email="sender@example.com",
            sender_password="secret",
            receiver_email="receiver@example.com",
        )

    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: [])
    with pytest.raises(RuntimeError, match="no TUN-safe route"):
        p9_email.send_report_via_email(
            "content",
            sender_email="sender@example.com",
            sender_password="secret",
            receiver_email="receiver@example.com",
        )


def test_rendering_formatters_cover_numeric_and_text_inputs():
    import p9_rendering

    assert p9_rendering.format_confidence(None) == ""
    assert p9_rendering.format_confidence(0.75) == "75%"
    assert p9_rendering.format_confidence(88) == "88%"
    assert p9_rendering.format_confidence("unknown") == "unknown"
    assert p9_rendering.format_evidence_refs(None) == ""
    assert p9_rendering.format_evidence_refs([" A ", "", "B"]) == "A / B"
    assert p9_rendering.format_evidence_refs(" ref ") == "ref"


def test_report_rendering_covers_populated_and_empty_sections():
    import p9_rendering

    populated = {
        "x_intel_snipe": [{"title": "X title", "body": "X body"}, "X scalar"],
        "p3_machine_intel": [{"title": "P3 title", "body": "P3 body"}, "P3 scalar"],
        "core_signals": [
            {
                "source": "source",
                "significance": "high",
                "title": "signal",
                "body": "detail",
                "confidence": 0.8,
                "evidence_refs": ["ref-1"],
            },
            {"title": "minimal", "body": "body"},
            "signal scalar",
        ],
        "sniper_tables": [
            {
                "track_name": "track",
                "targets": [
                    {
                        "name": "target",
                        "code": "600000",
                        "price": "10",
                        "chg_3m": "5%",
                        "percentile_250d": "60%",
                        "elasticity": "high",
                        "rs_strength": "strong",
                        "weekly_level": "up",
                        "catalyst": "event",
                        "risk": "risk",
                    }
                ],
            },
            {"targets": []},
        ],
        "fund_pathway": [
            {"title": "path-title", "cascade": "A -> B"},
            {"name": "path-name", "content": "C -> D"},
            "1. prefixed -> path",
            "plain path",
        ],
        "today_advice": [
            {
                "priority": "high",
                "name": "target",
                "code": "600000",
                "reason": "reason",
                "strategy": "strategy",
                "confidence": 0.9,
                "evidence_refs": ["ref"],
            },
            "advice scalar",
        ],
        "risk_warning": ["risk one"],
    }
    markdown = p9_rendering.build_report_markdown(populated, "2026-07-14")
    assert "X title" in markdown
    assert "P3 title" in markdown
    assert "600000" in markdown
    assert "path-title" in markdown
    assert "risk one" in markdown

    fallback = p9_rendering.build_report_markdown(
        {"sniper_table": [{"name": "legacy", "code": "000001"}]},
        "2026-07-14",
    )
    assert "000001" in fallback
    empty = p9_rendering.build_report_markdown({}, "2026-07-14")
    assert "2026-07-14" in empty


def test_p8_extractors_resolve_paths_parse_records_and_handle_errors(tmp_path, monkeypatch):
    import p8_extractors

    root = tmp_path / "tdx"
    vipdoc = root / "vipdoc"
    monkeypatch.setattr(p8_extractors, "resolve_tdx_root", lambda: root)
    assert p8_extractors.find_tdx_vipdoc() is None
    vipdoc.mkdir(parents=True)
    assert p8_extractors.find_tdx_vipdoc() == str(vipdoc)

    sh_file = vipdoc / "sh" / "lday" / "sh600000.day"
    sh_file.parent.mkdir(parents=True)
    sh_file.write_bytes(b"")
    assert p8_extractors.resolve_tdx_market_and_path(str(vipdoc), "600000") == ("sh", str(sh_file))

    bj_fallback = p8_extractors.resolve_tdx_market_and_path(str(vipdoc), "800001")
    assert bj_fallback[0] == "bj"
    sz_fallback = p8_extractors.resolve_tdx_market_and_path(str(vipdoc), "000001")
    assert sz_fallback[0] == "sz"

    record_file = tmp_path / "record.day"
    record_file.write_bytes(struct.pack("<IIIIIfII", 20260714, 1000, 1100, 900, 1050, 1.5, 1234, 0))
    records = p8_extractors.read_day_file_simple(record_file)
    assert records == [
        {
            "date": "20260714",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "vol": 1234,
        }
    ]
    invalid_date = tmp_path / "invalid.day"
    invalid_date.write_bytes(struct.pack("<IIIIIfII", 123, 100, 100, 100, 100, 0.0, 1, 0))
    assert p8_extractors.read_day_file_simple(invalid_date) == []
    assert p8_extractors.read_day_file_simple(tmp_path / "missing.day") == []


def test_p8_extractors_report_helpers_cover_deduplication_and_maps():
    import p8_extractors

    report = "示例公司（600000）\n示例公司 600000\n| 第二公司 | 000001 |"
    stocks = p8_extractors.extract_stocks_from_report(report)
    assert len({code for _name, code in stocks}) == len(stocks)
    assert ("示例公司", "600000") in stocks

    track_report = "### 赛道：AI\n示例公司（600000）\n### unrelated\n第二公司（000001）"
    tracks = p8_extractors.extract_stock_tracks(track_report)
    assert tracks.get("600000")
    assert isinstance(p8_extractors.extract_stock_elasticity("示例公司（600000） | 普通"), dict)


def test_p8_indicators_cover_short_flat_rising_and_falling_series():
    import p8_indicators

    assert p8_indicators.calc_macd([1.0] * 10)[0] == 0
    rising = [float(i) for i in range(1, 121)]
    falling = list(reversed(rising))
    flat = [10.0] * 121
    assert p8_indicators.calc_macd(rising)[0] > 0
    assert p8_indicators.calc_macd(falling)[0] < 0
    assert p8_indicators.calc_rsi([1.0] * 10) == 50
    assert p8_indicators.calc_rsi(rising) == 100
    assert p8_indicators.calc_rsi(falling) == 0
    assert p8_indicators.calc_kdj([1] * 5, [1] * 5, [1] * 5) == 50
    assert isinstance(p8_indicators.calc_kdj(rising, rising, rising), float)
    assert p8_indicators.calc_kdj(flat, flat, flat) == pytest.approx(50)
    assert p8_indicators.calc_period_change([], 20) == 0
    assert p8_indicators.calc_period_change([0, 1], 20) == 0
    assert p8_indicators.calc_period_change([1, 2], 20) == 100
    assert p8_indicators.calc_boll_width([1] * 5) == 0
    assert p8_indicators.calc_boll_width([0] * 20) == 0
    assert p8_indicators.calc_boll_width(rising) > 0
    assert p8_indicators.calc_atr_pct([1] * 5, [1] * 5, [1] * 5) == 0
    assert p8_indicators.calc_atr_pct([1] * 15, [0] * 15, [0] * 15) == 0
    assert p8_indicators.calc_atr_pct([2] * 15, [1] * 15, [1] * 15) > 0

    short_weekly = p8_indicators.calc_weekly_summary([1] * 20)
    assert short_weekly["wmacd"] == 0
    assert p8_indicators.calc_weekly_summary(rising)["trend"]
    assert p8_indicators.calc_weekly_summary(falling)["trend"]
    assert p8_indicators.calc_weekly_summary(flat)["trend"]


def test_p8_storage_preserves_partial_artifact_success(monkeypatch):
    import p8_storage

    manifests = []
    monkeypatch.setattr(p8_storage, "write_text_artifact", lambda *_args: "report.md")
    monkeypatch.setattr(p8_storage, "write_json_artifact", lambda *_args: "report.json")
    monkeypatch.setattr(p8_storage, "update_run_manifest", lambda *args, **kwargs: manifests.append((args, kwargs)))
    assert p8_storage.persist_p8_outputs("markdown", {}, status="completed", metrics={}) == "markdown"
    assert manifests[-1][1]["files"] == {"md": "report.md", "json": "report.json"}

    monkeypatch.setattr(
        p8_storage,
        "write_text_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("md failed")),
    )
    monkeypatch.setattr(
        p8_storage,
        "write_json_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("json failed")),
    )
    assert p8_storage.persist_p8_outputs("fallback", {}, status="degraded", metrics={}) == "fallback"
    assert manifests[-1][1]["files"] is None


def test_p8_technical_no_input_no_vipdoc_and_mixed_records(monkeypatch):
    import p8_technical

    persisted = []
    monkeypatch.setattr(
        p8_technical,
        "_persist_p8_outputs",
        lambda markdown, data, **kwargs: persisted.append((markdown, data, kwargs)) or markdown,
    )
    assert p8_technical.p8_search_stock_prices([])
    assert persisted[-1][2]["status"] == "skipped_no_stocks"

    monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: None)
    assert p8_technical.p8_search_stock_prices([("NoVipdoc", "600000")])
    assert persisted[-1][2]["status"] == "skipped_no_vipdoc"

    monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: "vipdoc")
    monkeypatch.setattr(
        p8_technical,
        "_resolve_tdx_market_and_path",
        lambda _vipdoc, code: ("sh", f"{code}.day"),
    )
    monkeypatch.setattr(p8_technical.os.path, "exists", lambda path: path.startswith("600"))
    monkeypatch.setattr(p8_technical, "read_day_file_simple", lambda _path: [])
    result = p8_technical.p8_search_stock_prices(
        [("NonCore", "600000"), ("Missing", "000001")],
        track_map={"600000": "track"},
        elasticity_map={"600000": "normal", "000001": "normal"},
    )
    assert result
    assert persisted[-1][2]["status"] == "completed_structured_degraded"
    assert persisted[-1][2]["metrics"]["stocks_with_data"] == 0
    assert persisted[-1][2]["metrics"]["data_state"] == "degraded_empty"
    assert persisted[-1][2]["metrics"]["structured_degraded"] is True
    assert (
        persisted[-1][2]["metrics"]["degrade_reason"]
        == "requested_stocks_but_no_usable_local_kline"
    )
