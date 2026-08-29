"""Focused branch tests for the remaining reporting and launcher paths."""

from __future__ import annotations

import smtplib
from types import SimpleNamespace

import pytest


def test_p8_extractors_cover_structured_tracks_and_elasticity():
    import p8_extractors

    report = "前言\n### 赛道：光模块\n- 中际旭创（300308） | 核心标的"

    assert p8_extractors.extract_stock_tracks(report) == {"300308": "光模块"}
    assert p8_extractors.extract_stock_elasticity(
        "中际旭创（300308） | 🔥高弹性"
    ) == {"300308": "🔥高弹性"}


def test_weekly_summary_covers_directional_trends(monkeypatch):
    import p8_indicators

    rising = [float(value) for value in range(1, 101)]
    falling = list(reversed(rising))

    monkeypatch.setattr(p8_indicators, "calc_macd", lambda _closes: (1.0, "positive"))
    assert p8_indicators.calc_weekly_summary(rising)["trend"] == "上行"

    monkeypatch.setattr(p8_indicators, "calc_macd", lambda _closes: (-1.0, "negative"))
    assert p8_indicators.calc_weekly_summary(falling)["trend"] == "下行"


def _records_from_closes(closes: list[float]) -> list[dict]:
    return [
        {
            "date": f"2026{index + 1:04d}",
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "vol": 1_000 + index,
        }
        for index, close in enumerate(closes)
    ]


def test_p8_technical_covers_core_edge_profiles(monkeypatch):
    import p8_technical

    captured = {}
    rising = [float(value) for value in range(20, 100)]
    old_peak = [100.0] * 80
    old_peak[30] = 200.0
    limit_history = [100.0] * 80
    limit_history[30] = 200.0
    limit_history[70] = 110.0
    limit_history[71] = 99.0
    records = {
        "SHORT": _records_from_closes([100.0] * 20),
        "RISING": _records_from_closes(rising),
        "OLDPEAK": _records_from_closes(old_peak),
        "LIMITS": _records_from_closes(limit_history),
    }

    monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: "vipdoc")
    monkeypatch.setattr(
        p8_technical,
        "_resolve_tdx_market_and_path",
        lambda _vipdoc, code: ("sh", code),
    )
    monkeypatch.setattr(p8_technical.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        p8_technical,
        "read_day_file_simple",
        lambda path: [] if "999999" in path or "399006" in path else records[path],
    )
    monkeypatch.setattr(
        p8_technical,
        "_persist_p8_outputs",
        lambda markdown, data, **kwargs: captured.update(data=data, kwargs=kwargs) or markdown,
    )

    markdown = p8_technical.p8_search_stock_prices(
        [("短数据", "SHORT"), ("新高", "RISING"), ("旧高点", "OLDPEAK"), ("涨跌停", "LIMITS")],
        elasticity_map={code: "🔥高弹性" for code in records},
    )

    assert "未找到足够的本地K线数据" in markdown
    assert captured["data"]["SHORT"]["error"] == "insufficient_local_kline"
    assert captured["data"]["RISING"]["tags"]["pos_desc"] == "历史高位区间"
    assert captured["data"]["OLDPEAK"]["tags"]["breakouts"]
    assert "涨停" in markdown and "跌停" in markdown


def test_p9_rendering_covers_numbered_scalar_pathways():
    import p9_rendering

    markdown = p9_rendering.build_report_markdown(
        {
            "fund_pathway": [
                "1. 【算力扩张】资本开支 -> 光模块",
                "2.存储复苏：去库存 -> 涨价",
            ]
        },
        "2026-07-15",
    )

    assert "### 路径1：【算力扩张】" in markdown
    assert "### 路径2：存储复苏" in markdown
    assert "> → 光模块" in markdown


def test_email_fallback_preserves_authentication_failure(monkeypatch):
    import p9_email

    calls = []

    def fail_send(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError("primary route unavailable")
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(p9_email, "_send_message", fail_send)
    monkeypatch.setattr(p9_email, "_resolve_real_smtp_ips", lambda: ["203.0.113.1"])
    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: ["192.0.2.1"])

    with pytest.raises(smtplib.SMTPAuthenticationError):
        p9_email.send_report_via_email(
            "report",
            sender_email="sender@example.com",
            sender_password="secret",  # pragma: allowlist secret
            receiver_email="receiver@example.com",
        )


def test_smtp_data_failure_is_ambiguous_and_never_tries_tun_fallback(monkeypatch):
    import p9_email

    attempts = []

    class DataDisconnectSMTP:
        def __init__(self, _host, _port, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def set_debuglevel(self, _level):
            pass

        def login(self, _sender, _password):
            pass

        def sendmail(self, _sender, _receivers, _payload):
            attempts.append("DATA")
            raise OSError("connection dropped after DATA")

    monkeypatch.setattr(p9_email.smtplib, "SMTP_SSL", DataDisconnectSMTP)
    monkeypatch.setattr(p9_email, "_resolve_real_smtp_ips", lambda: ["203.0.113.1"])
    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: ["192.0.2.1"])

    with pytest.raises(p9_email.SMTPDeliveryAmbiguousError) as exc_info:
        p9_email.send_report_via_email(
            "# report",
            sender_email="sender@example.com",
            sender_password="secret",  # pragma: allowlist secret
            receiver_email="receiver@example.com",
        )

    assert attempts == ["DATA"]
    provenance = exc_info.value.provenance
    assert provenance["content_sha256"]
    assert provenance["message_id"].startswith("<vcp-")


def test_smtp_teardown_after_sendmail_is_ambiguous_and_never_tries_tun_fallback(monkeypatch):
    import p9_email

    sendmail_attempts = []
    teardown_attempts = []

    class TeardownDisconnectSMTP:
        def __init__(self, _host, _port, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            teardown_attempts.append("QUIT")
            raise OSError("connection dropped while waiting for QUIT")

        def set_debuglevel(self, _level):
            pass

        def login(self, _sender, _password):
            pass

        def sendmail(self, _sender, _receivers, _payload):
            sendmail_attempts.append("DATA")

    monkeypatch.setattr(p9_email.smtplib, "SMTP_SSL", TeardownDisconnectSMTP)
    monkeypatch.setattr(p9_email, "_resolve_real_smtp_ips", lambda: ["203.0.113.1"])
    monkeypatch.setattr(p9_email, "_candidate_source_ips", lambda: ["192.0.2.1"])

    with pytest.raises(p9_email.SMTPDeliveryAmbiguousError) as exc_info:
        p9_email.send_report_via_email(
            "# report",
            sender_email="sender@example.com",
            sender_password="secret",  # pragma: allowlist secret
            receiver_email="receiver@example.com",
        )

    assert sendmail_attempts == ["DATA"]
    assert teardown_attempts == ["QUIT"]
    assert exc_info.value.provenance["error"] == "connection dropped while waiting for QUIT"


def test_scheduled_launcher_covers_defensive_failures(monkeypatch):
    import scheduled_launcher

    class BrokenStream:
        def reconfigure(self, **_kwargs):
            raise OSError("unsupported")

    monkeypatch.setattr(
        scheduled_launcher,
        "sys",
        SimpleNamespace(stdout=BrokenStream(), stderr=BrokenStream()),
    )
    scheduled_launcher._configure_stdio()

    monkeypatch.setattr(
        scheduled_launcher,
        "sys",
        SimpleNamespace(stdout=object(), stderr=object()),
    )
    scheduled_launcher._configure_stdio()

    monkeypatch.setattr(
        scheduled_launcher,
        "record_run_state",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("state unavailable")),
    )
    scheduled_launcher._record_launcher_state(
        status="failed",
        root_cause="test",
        reason="test",
        schedule={},
    )


def test_scheduled_launcher_records_process_exception(monkeypatch):
    import scheduled_launcher

    states = []
    monkeypatch.setattr(
        scheduled_launcher,
        "get_schedule_decision",
        lambda _today: {"is_trading_day": True, "should_run": True, "reason": "ok"},
    )
    monkeypatch.setattr(
        scheduled_launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    monkeypatch.setattr(
        scheduled_launcher,
        "_record_launcher_state",
        lambda **kwargs: states.append(kwargs),
    )

    with pytest.raises(SystemExit) as exc_info:
        scheduled_launcher.main([])

    assert exc_info.value.code == 1
    assert states[-1]["root_cause"] == "launcher_exception"


def test_semantic_output_covers_empty_and_metadata_lines():
    import semantic_output

    assert semantic_output.is_no_evidence_text(None) is False
    assert semantic_output.has_meaningful_semantic_text("生成时间: 2026-07-15\n有效证据") is True
