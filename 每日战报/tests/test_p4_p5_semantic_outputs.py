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


def _run_p4(monkeypatch, frames):
    today = pd.to_datetime("today").normalize()
    manifests = []

    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **_kwargs):
            value = frames[self.ticker]
            if isinstance(value, BaseException):
                raise value
            return value.copy()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=Ticker))
    monkeypatch.setattr(
        p4_vanguard,
        "VANGUARD_TICKERS",
        {name: name for name in frames},
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
