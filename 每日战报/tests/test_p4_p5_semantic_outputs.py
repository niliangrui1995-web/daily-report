"""Focused regressions for P4/P5 semantic output states."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

import audit_engine
import p4_vanguard


class _InlineFuture:
    def __init__(self, fn, args):
        try:
            self._value = fn(*args)
            self._error = None
        except BaseException as exc:  # pragma: no cover - surfaced by result()
            self._value = None
            self._error = exc

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _InlinePool:
    def __init__(self, max_workers):
        assert max_workers == 6

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args):
        return _InlineFuture(fn, args)


def _run_p4(monkeypatch, frames, *, tickers=None):
    today = pd.to_datetime("today").normalize()
    manifests = []

    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **_kwargs):
            value = frames[self.ticker]
            if callable(value):
                value = value(**_kwargs)
            if isinstance(value, BaseException):
                raise value
            return value.copy()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=Ticker))
    monkeypatch.setattr(
        p4_vanguard,
        "VANGUARD_TICKERS",
        tickers or {name: name for name in frames},
    )
    monkeypatch.setattr(p4_vanguard, "SUPER_GIANTS", set())
    monkeypatch.setattr(p4_vanguard, "STRATEGIC_GIANTS", set())
    monkeypatch.setattr(
        p4_vanguard,
        "_get_prev_trading_day",
        lambda _today: (today - pd.Timedelta(days=2)).date(),
    )
    monkeypatch.setattr(
        p4_vanguard,
        "classify_vanguard_signal",
        lambda _name, _change: "normal",
    )
    monkeypatch.setattr(p4_vanguard.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        p4_vanguard.concurrent.futures,
        "ThreadPoolExecutor",
        _InlinePool,
    )
    monkeypatch.setattr(
        p4_vanguard.concurrent.futures,
        "as_completed",
        lambda futures: list(futures),
    )
    monkeypatch.setattr(
        p4_vanguard,
        "write_text_artifact",
        lambda _prefix, _body: "p4.md",
    )
    monkeypatch.setattr(
        p4_vanguard,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )

    report = p4_vanguard._fetch_vanguard_prices_inline()
    return report, manifests[-1]


def test_p4_partial_data_is_structured_degraded_and_counted(monkeypatch):
    today = pd.to_datetime("today").normalize()
    usable = pd.DataFrame(
        {"Close": [100.0, 110.0]},
        index=pd.DatetimeIndex(
            [today - pd.Timedelta(days=3), today - pd.Timedelta(days=1)]
        ),
    )
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))

    report, (stage, payload) = _run_p4(
        monkeypatch,
        {"usable": usable, "no_data": empty, "failed": RuntimeError("offline")},
    )

    assert report
    assert stage == "p4"
    assert payload["status"] == "completed_structured_degraded"
    assert payload["files"] == {"md": "p4.md"}
    assert payload["metrics"]["usable_tickers"] == 1
    assert payload["metrics"]["no_data_tickers"] == 1
    assert payload["metrics"]["failed_tickers"] == 1


def test_p4_zero_usable_data_is_failed_but_keeps_diagnostic_artifact(monkeypatch):
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))

    report, (_stage, payload) = _run_p4(
        monkeypatch,
        {"empty": empty, "failed": RuntimeError("offline")},
    )

    assert report
    assert payload["status"] == "failed_no_usable_data"
    assert payload["files"] == {"md": "p4.md"}
    assert payload["metrics"]["usable_tickers"] == 0
    assert payload["metrics"]["no_data_tickers"] == 1
    assert payload["metrics"]["failed_tickers"] == 1


def test_p4_uses_hourly_close_when_latest_daily_history_row_is_nan(monkeypatch):
    today = pd.to_datetime("today").normalize()
    daily_with_unpriced_tail = pd.DataFrame(
        {"Close": [100.0, float("nan")]},
        index=pd.DatetimeIndex(
            [
                today - pd.Timedelta(days=3),
                today - pd.Timedelta(days=1),
            ]
        ),
    )
    hourly_with_completed_tail = pd.DataFrame(
        {"Close": [100.0, 110.0, 999.0]},
        index=pd.DatetimeIndex(
            [
                today - pd.Timedelta(days=3) + pd.Timedelta(hours=17),
                today - pd.Timedelta(days=1) + pd.Timedelta(hours=17),
                today + pd.Timedelta(hours=17),
            ]
        ),
    )
    daily_request = {
        "period": "1mo",
        "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS,
        "raise_errors": True,
    }
    hourly_request = {
        "period": "5d",
        "interval": "1h",
        "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS,
        "raise_errors": True,
    }
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        if kwargs == daily_request:
            return daily_with_unpriced_tail
        if kwargs == hourly_request:
            return hourly_with_completed_tail
        raise AssertionError(f"unexpected request: {kwargs}")

    report, (_stage, payload) = _run_p4(
        monkeypatch,
        {"Affected": history},
    )

    assert "Affected(Affected) [中型标的]: 收盘价 110.00, 区间涨跌 +10.00%" in report
    assert "收盘价 nan" not in report
    assert calls == [daily_request, hourly_request]
    assert payload["status"] == "completed"
    assert payload["metrics"]["usable_tickers"] == 1
    assert payload["metrics"]["no_data_tickers"] == 0


def test_p4_marks_all_nan_completed_history_as_missing_and_not_comparable(monkeypatch):
    today = pd.to_datetime("today").normalize()
    all_nan_history = pd.DataFrame(
        {"Close": [float("nan"), float("nan")]},
        index=pd.DatetimeIndex(
            [today - pd.Timedelta(days=3), today - pd.Timedelta(days=1)]
        ),
    )
    daily_request = {
        "period": "1mo",
        "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS,
        "raise_errors": True,
    }
    hourly_request = {
        "period": "5d",
        "interval": "1h",
        "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS,
        "raise_errors": True,
    }
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        if kwargs == daily_request:
            return all_nan_history
        if kwargs == hourly_request:
            return pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
        raise AssertionError(f"unexpected request: {kwargs}")

    report, (_stage, payload) = _run_p4(
        monkeypatch,
        {"Missing": history},
    )

    assert "Missing(Missing) [中型标的]: 数据缺失/不可比较（最近已完成交易日收盘价缺失）" in report
    assert "[📈 正常]" not in report
    assert calls == [daily_request, hourly_request]
    assert payload["status"] == "failed_no_usable_data"
    assert payload["metrics"]["usable_tickers"] == 0
    assert payload["metrics"]["no_data_tickers"] == 1


def test_p4_recovers_the_nine_verified_european_tickers_from_unpriced_tails(monkeypatch):
    from industry_dict import VANGUARD_TICKERS

    expected_tickers = {
        "Siemens EDA": "SIE.DE",
        "Soitec": "SOI.PA",
        "IQE": "IQE.L",
        "PVA TePla": "TPE.DE",
        "Besi": "BESI.AS",
        "Mycronic": "MYCR.ST",
        "Schneider": "SU.PA",
        "Technoprobe": "TPRO.MI",
        "Prysmian": "PRY.MI",
    }
    assert {name: VANGUARD_TICKERS[name] for name in expected_tickers} == expected_tickers

    today = pd.to_datetime("today").normalize()
    daily_with_unpriced_tail = pd.DataFrame(
        {"Close": [100.0, float("nan")]},
        index=pd.DatetimeIndex(
            [
                today - pd.Timedelta(days=3),
                today - pd.Timedelta(days=1),
            ]
        ),
    )
    hourly_with_completed_tail = pd.DataFrame(
        {"Close": [100.0, 110.0]},
        index=pd.DatetimeIndex(
            [
                today - pd.Timedelta(days=3) + pd.Timedelta(hours=17),
                today - pd.Timedelta(days=1) + pd.Timedelta(hours=17),
            ]
        ),
    )

    def history(**kwargs):
        if kwargs["period"] == "1mo":
            return daily_with_unpriced_tail
        if kwargs["period"] == "5d" and kwargs["interval"] == "1h":
            return hourly_with_completed_tail
        raise AssertionError(f"unexpected request: {kwargs}")

    frames = {
        ticker: history
        for ticker in expected_tickers.values()
    }

    report, (_stage, payload) = _run_p4(
        monkeypatch,
        frames,
        tickers=expected_tickers,
    )

    for name, ticker in expected_tickers.items():
        assert f"{name}({ticker}) [中型标的]: 收盘价 110.00, 区间涨跌 +10.00%" in report
    assert "收盘价 nan" not in report
    assert payload["status"] == "completed"
    assert payload["metrics"]["tickers"] == 9
    assert payload["metrics"]["usable_tickers"] == 9
    assert payload["metrics"]["no_data_tickers"] == 0


def test_p4_keeps_priced_daily_history_on_single_request(monkeypatch):
    today = pd.to_datetime("today").normalize()
    daily_history = pd.DataFrame(
        {"Close": [100.0, 110.0]},
        index=pd.DatetimeIndex(
            [today - pd.Timedelta(days=3), today - pd.Timedelta(days=1)]
        ),
    )
    daily_request = {
        "period": "1mo",
        "timeout": p4_vanguard.P4_REQUEST_TIMEOUT_SECONDS,
        "raise_errors": True,
    }
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        if kwargs == daily_request:
            return daily_history
        raise AssertionError("hourly fallback must not run for a priced daily tail")

    report, (_stage, payload) = _run_p4(
        monkeypatch,
        {"Normal": history},
    )

    assert "Normal(Normal) [中型标的]: 收盘价 110.00, 区间涨跌 +10.00%" in report
    assert calls == [daily_request]
    assert payload["status"] == "completed"
    assert payload["metrics"]["usable_tickers"] == 1


@pytest.fixture
def audit_harness(monkeypatch):
    manifests = []
    json_writes = []
    text_writes = []
    payload = {}

    monkeypatch.setattr(
        audit_engine,
        "require_prompt",
        lambda _name: (
            "{layer_name}|{header_text}|{time_section}|{task_two}|{task_three}|"
            "{vanguard_list}|{raw_data}"
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "_invoke_audit_with_fallback",
        lambda *_args, **_kwargs: (
            SimpleNamespace(content="model-json"),
            SimpleNamespace(last_error_category="", last_error_message=""),
            {"audit_fallback_used": False},
        ),
    )
    monkeypatch.setattr(
        audit_engine,
        "extract_json_from_llm_response",
        lambda *_args, **_kwargs: payload["value"],
    )
    monkeypatch.setattr(
        audit_engine,
        "write_json_artifact",
        lambda prefix, data: json_writes.append((prefix, data)) or "audit.json",
    )
    monkeypatch.setattr(
        audit_engine,
        "write_text_artifact",
        lambda prefix, text: text_writes.append((prefix, text)) or "audit.md",
    )
    monkeypatch.setattr(
        audit_engine,
        "update_run_manifest",
        lambda stage, **kwargs: manifests.append((stage, kwargs)),
    )
    monkeypatch.setattr(audit_engine.time, "sleep", lambda _seconds: None)
    return payload, manifests, json_writes, text_writes


@pytest.mark.parametrize(
    ("invalid_payload", "expected_error"),
    [
        ({}, "missing_summary"),
        (
            {
                "summary": {"passed": 0, "deleted": 0},
                "high_trust": [],
                "medium_trust": [],
                "deleted": [],
            },
            "empty_audit_payload",
        ),
        (
            {
                "summary": {"passed": 2, "deleted": 0},
                "high_trust": ["one"],
                "medium_trust": [],
                "deleted": [],
            },
            "passed_count_mismatch",
        ),
        (
            {
                "summary": {"passed": 0, "deleted": 1},
                "high_trust": [],
                "medium_trust": [],
                "deleted": [],
            },
            "deleted_count_without_items",
        ),
    ],
)
def test_p5_rejects_empty_or_inconsistent_structured_shells(
    audit_harness,
    invalid_payload,
    expected_error,
):
    payload, manifests, json_writes, text_writes = audit_harness
    payload["value"] = invalid_payload

    result = audit_engine.gemini_pro_audit("raw P1 evidence", audit_type="p5")

    assert result == ""
    assert json_writes == []
    assert manifests[-1][0] == "p5"
    assert manifests[-1][1]["status"] == "failed_semantic_output"
    assert manifests[-1][1]["metrics"]["semantic_error"] == expected_error
    assert manifests[-1][1]["files"] == {"raw": "audit.md"}
    assert text_writes[-1][0].endswith("_RAW_SEMANTIC_INVALID")


def test_p5_all_deleted_is_a_valid_no_results_state(audit_harness):
    payload, manifests, json_writes, text_writes = audit_harness
    payload["value"] = {
        "summary": {"passed": 0, "deleted": 3},
        "high_trust": [],
        "medium_trust": [],
        "deleted": [
            {"reason": "stale", "content": "old item"},
            {"reason": "duplicate", "content": "duplicate item"},
        ],
    }

    result = audit_engine.gemini_pro_audit("raw P1 evidence", audit_type="p5")

    assert result == ""
    assert json_writes
    assert text_writes
    assert manifests[-1][1]["status"] == "completed_no_results"
    metrics = manifests[-1][1]["metrics"]
    assert metrics["passed_count"] == 0
    assert metrics["high_trust_count"] == 0
    assert metrics["medium_trust_count"] == 0
    assert metrics["deleted_count"] == 3
    assert metrics["deleted_items_count"] == 2
    assert metrics["data_state"] == "valid_empty"


def test_p75_empty_sentinel_shell_is_a_valid_no_results_state(audit_harness):
    payload, manifests, json_writes, text_writes = audit_harness
    payload["value"] = {
        "summary": {"passed": 0, "deleted": 0},
        "high_trust": ["无"],
        "medium_trust": ["not found"],
        "deleted": [],
    }

    result = audit_engine.gemini_pro_audit("raw supplement evidence", audit_type="p7.5")

    assert result == ""
    assert json_writes
    assert text_writes
    assert manifests[-1][0] == "p75"
    assert manifests[-1][1]["status"] == "completed_no_results"
    metrics = manifests[-1][1]["metrics"]
    assert metrics["passed_count"] == 0
    assert metrics["data_state"] == "valid_empty"
