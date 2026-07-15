from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest


def test_http_security_logging_and_hostname_failure(monkeypatch):
    import http_security

    warnings = []
    monkeypatch.setattr(http_security.logging, "warning", lambda *args: warnings.append(args))
    http_security.log_tls_policy("P1", "https://example.com", {"insecure_fetch": False})
    assert warnings == []
    http_security.log_tls_policy(
        "P1",
        "https://example.com",
        {"insecure_fetch": True, "host": "example.com"},
    )
    assert warnings[-1][1] == "P1"
    monkeypatch.setattr(
        http_security,
        "urlparse",
        lambda _url: (_ for _ in ()).throw(ValueError("bad url")),
    )
    assert http_security._safe_hostname("bad") == ""


@pytest.mark.parametrize(
    ("name", "change", "expected_fragment"),
    [
        ("NVIDIA", 3, ""),
        ("NVIDIA", -5, ""),
        ("NVIDIA", -2, ""),
        ("NVIDIA", 0, ""),
        ("Small", 5, ""),
        ("Small", -8, ""),
        ("Small", -3, ""),
        ("Small", 0, ""),
    ],
)
def test_industry_signal_thresholds(name, change, expected_fragment):
    import industry_dict

    result = industry_dict.classify_vanguard_signal(name, change)
    assert isinstance(result, str)
    assert result


def test_intel_confidence_conversion_and_p2_parsing_edges():
    import intel_structuring

    assert intel_structuring._confidence_to_score(None, default=0.5) == 0.5
    assert intel_structuring._confidence_to_score("80") == 0.8
    assert intel_structuring._confidence_to_score("0.7") == 0.7
    assert intel_structuring._confidence_to_score("high") == 0.82
    assert intel_structuring._confidence_to_score("medium") == 0.64
    assert intel_structuring._confidence_to_score("low") == 0.46
    assert intel_structuring._confidence_to_score("unknown", default=0.55) == 0.55
    assert intel_structuring._score_to_label(0.8) == "high"
    assert intel_structuring._score_to_label(0.6) == "medium"
    assert intel_structuring._score_to_label(0.2) == "low"
    assert intel_structuring._score_to_percent(2) == "100%"
    assert intel_structuring._score_to_percent(-1) == "0%"

    raw = """
<x_leaks>
no title
</x_leaks>
<x_leaks>
### [AI] Missing claim
- **信源**: source
</x_leaks>
<x_leaks>
### Plain title
- **核心内容**: Valid claim
- **可信度**: high
</x_leaks>
"""
    items = intel_structuring.parse_p2_structured_items(raw)
    assert len(items) == 1
    assert items[0]["title"] == "Plain title"
    assert items[0]["confidence_label"] == "high"


def test_intel_p3_normalization_and_render_defaults():
    import intel_structuring

    items = intel_structuring.normalize_p3_structured_items(
        {
            "jinnang_1": ["invalid", {}, {"summary": "summary only"}],
            "jinnang_2": [{"title": "title only", "confidence": "low"}],
        }
    )
    assert len(items) == 2
    assert items[0]["title"] == "summary only"
    assert items[0]["source"]
    assert items[1]["claim"] == "title only"
    p2_md = intel_structuring.render_p2_structured_markdown(
        [{"title": "item", "claim": "claim"}],
        model="model",
        collected_at="now",
    )
    p3_md = intel_structuring.render_p3_structured_markdown(items, collected_at="now")
    assert "item" in p2_md
    assert "summary only" in p3_md


def test_pipeline_maintenance_clean_and_prune_variants(tmp_path, monkeypatch):
    import pipeline_maintenance

    project = tmp_path / "project"
    cache = project / "nested" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "cache.pyc").write_bytes(b"x")
    output = tmp_path / "output"
    output.mkdir()
    (output / "pipeline.log").write_text("log", encoding="utf-8")
    monkeypatch.setattr(pipeline_maintenance, "get_output_root_dir", lambda: str(output))
    cleaned = pipeline_maintenance.clean_workspace(str(project))
    assert cleaned == {"removed_dirs": 1, "removed_files": 1}

    assert pipeline_maintenance.prune_outputs(str(tmp_path / "missing")) == {"removed_dirs": 0}
    (output / "not-a-day").mkdir()
    (output / "20261399").mkdir()
    recent = output / datetime.now().strftime("%Y%m%d")
    recent.mkdir(exist_ok=True)
    old = output / "20200101"
    (old / "logs").mkdir(parents=True)
    (old / "raw").mkdir()
    (old / "ordinary.txt").write_text("keep", encoding="utf-8")
    result = pipeline_maintenance.prune_outputs(str(output), keep_days=7)
    assert result["removed_dirs"] == 2
    assert (old / "ordinary.txt").exists()


def test_run_config_forced_windows_and_capped_adaptive_window(monkeypatch):
    import run_config

    monkeypatch.setenv("FORCE_72H", "1")
    assert run_config.get_run_config(datetime(2026, 7, 14, 9)) == "72h"
    monkeypatch.delenv("FORCE_72H")
    monkeypatch.setenv("FORCE_24H", "1")
    assert run_config.get_run_config(datetime(2026, 7, 14, 9)) == "24h"
    monkeypatch.delenv("FORCE_24H")

    monkeypatch.setattr(
        run_config,
        "get_schedule_decision",
        lambda _today: {
            "window_anchor_day": date(2020, 1, 1),
            "trading_day_index_in_week": 1,
        },
    )
    assert run_config.get_run_config(datetime(2026, 7, 14, 9)) == "72h"

    monkeypatch.setattr(
        run_config,
        "get_schedule_decision",
        lambda _today: {
            "window_anchor_day": None,
            "trading_day_index_in_week": 1,
        },
    )
    monkeypatch.setattr(run_config, "_get_prev_trading_day", lambda _today: date(2026, 7, 14))
    assert run_config.get_run_config(datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)) == "1h"


def test_trading_calendar_library_fallback_and_search_limits(monkeypatch):
    import trading_calendar

    saturday = date(2026, 7, 18)
    assert trading_calendar.is_trading_day(saturday) is False
    monkeypatch.setattr(trading_calendar, "_HAS_CALENDAR_LIB", False)
    assert trading_calendar.is_trading_day(date(2026, 7, 17)) is True

    monkeypatch.setattr(trading_calendar, "_HAS_CALENDAR_LIB", True)
    monkeypatch.setattr(
        trading_calendar,
        "chinese_calendar",
        SimpleNamespace(
            is_holiday=lambda _day: (_ for _ in ()).throw(NotImplementedError("future"))
        ),
    )
    assert trading_calendar.is_trading_day(date(2099, 1, 2)) is True

    assert trading_calendar.get_prev_trading_day(date(2026, 7, 14), steps=0) == date(2026, 7, 14)
    monkeypatch.setattr(trading_calendar, "MAX_TRADING_DAY_LOOKBACK", 2)
    monkeypatch.setattr(trading_calendar, "is_trading_day", lambda _day: False)
    assert trading_calendar.get_prev_trading_day(date(2026, 7, 14), steps=3) == date(2026, 7, 11)
    assert trading_calendar.count_trading_days_between(date(2026, 7, 15), date(2026, 7, 14)) == 0

    monkeypatch.setattr(trading_calendar, "is_trading_day", lambda day: day.weekday() < 5)
    assert trading_calendar.count_trading_days_between(
        date(2026, 7, 13),
        date(2026, 7, 17),
        include_start=True,
        include_end=False,
    ) == 4
    assert len(trading_calendar.get_week_trading_days(date(2026, 7, 15))) == 5
    assert trading_calendar.get_trading_day_index_in_week(date(2026, 7, 18)) == 0

    monkeypatch.setattr(trading_calendar, "get_week_trading_days", lambda _day: [])
    assert trading_calendar.get_trading_day_index_in_week(date(2026, 7, 17)) == 0


def test_utils_normalization_parsing_and_json_repair_edges():
    import utils

    assert utils.normalize_common_text_mojibake(123) == 123
    assert utils.normalize_common_text_mojibake("plain") == "plain"
    repaired = utils.normalize_common_mojibake_artifacts(
        {"key": ["plain", ("\u00e2\u0080\u0099",)], 1: "value"}
    )
    assert repaired[1] == "value"
    assert repaired["key"][1][0] == "’"
    assert utils.parse_backtrack_hours("", default=12) == 12
    assert utils.parse_backtrack_hours("invalid", default=12) == 12
    assert utils.parse_backtrack_hours(" 72 H ") == 72
    assert utils._type_matches({}, "dict") is True
    assert utils._type_matches([], "list") is True
    assert utils._type_matches("anything", "other") is True
    assert utils._strip_opening_code_fence("```json\n{}\n```") == "{}"

    assert utils._append_missing_json_closers('{"a": [1') == '{"a": [1\n]}'
    assert utils._append_missing_json_closers('{"a": 1]}') is None
    assert utils._append_missing_json_closers('{"a": "unterminated') is None
    assert utils._append_missing_json_closers('{}') == '{}'
    assert utils._looks_like_json_string_close('"', 0) is True
    assert utils._looks_like_json_string_close('"x', 0) is False
    assert utils._escape_unescaped_inner_quotes('{"text":"say "hello" now"}')

    with pytest.raises(json.JSONDecodeError):
        utils.extract_json_from_llm_response("")
    assert utils.extract_json_from_llm_response("prefix {\"a\": 1} suffix") == {"a": 1}
    assert utils.extract_json_from_llm_response("[1, 2]", expect_type="list") == [1, 2]
    assert utils.extract_json_from_llm_response('{"text":"say "hello""', allow_partial=True) == {
        "text": 'say "hello"'
    }
    with pytest.raises(json.JSONDecodeError):
        utils.extract_json_from_llm_response("not json", allow_partial=True)
