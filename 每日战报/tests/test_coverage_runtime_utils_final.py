from __future__ import annotations

import builtins
import json
import runpy
from datetime import date
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_pipeline_runtime_applies_child_context_and_uses_default_error(monkeypatch):
    import pipeline_runtime

    results = iter(
        [
            {"status": "success", "context": {"child": "done"}},
            {"status": "error"},
        ]
    )
    monkeypatch.setattr(pipeline_runtime, "_stage_execution_mode", lambda: "process")
    monkeypatch.setattr(pipeline_runtime, "_run_stage_in_subprocess", lambda *_args: next(results))

    context = {"parent": "kept"}
    pipeline_runtime._execute_stage_handler(
        pipeline_runtime.StageSpec("success", lambda _context: None, timeout_seconds=1),
        context,
    )
    assert context == {"parent": "kept", "child": "done"}

    with pytest.raises(RuntimeError, match="stage failure failed in subprocess"):
        pipeline_runtime._execute_stage_handler(
            pipeline_runtime.StageSpec("failure", lambda _context: None, timeout_seconds=1),
            context,
        )


@pytest.mark.parametrize(
    ("exitcode", "saved_context", "expected_status", "expected_fragment"),
    [
        (7, None, "error", "code=7"),
        (0, None, "error", "no valid context"),
        (0, {"recovered": True}, "success", ""),
    ],
)
def test_pipeline_runtime_recovers_when_child_exits_without_signal(
    tmp_path,
    monkeypatch,
    exitcode,
    saved_context,
    expected_status,
    expected_fragment,
):
    import pipeline_runtime

    class FakeQueue:
        def get(self, *, timeout):
            raise TimeoutError(timeout)

        def get_nowait(self):
            raise LookupError("empty")

        def close(self):
            return None

        def join_thread(self):
            return None

    class FakeProcess:
        pid = 1234

        def __init__(self, *, target, args, name):
            self.exitcode = exitcode
            self.target = target
            self.args = args
            self.name = name

        def start(self):
            return None

        def is_alive(self):
            return False

        def join(self, *, timeout):
            return None

    fake_queue = FakeQueue()
    fake_context = SimpleNamespace(
        Queue=lambda **_kwargs: fake_queue,
        Process=lambda **kwargs: FakeProcess(**kwargs),
    )
    monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda _method: fake_context)
    monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        pipeline_runtime,
        "_read_context_from_file",
        lambda _path: saved_context,
    )

    result = pipeline_runtime._run_stage_in_subprocess(
        pipeline_runtime.StageSpec("offline", lambda _context: None, timeout_seconds=1),
        {},
    )

    assert result["status"] == expected_status
    if expected_fragment:
        assert expected_fragment in result["error"]
    else:
        assert result["context"] == saved_context


def test_pipeline_runtime_cleans_up_after_process_start_failure(tmp_path, monkeypatch):
    import pipeline_runtime

    class BrokenQueue:
        def close(self):
            raise OSError("queue already closed")

        def join_thread(self):
            raise AssertionError("close failure must skip join_thread")

    class BrokenProcess:
        def start(self):
            raise RuntimeError("cannot start child")

    fake_context = SimpleNamespace(
        Queue=lambda **_kwargs: BrokenQueue(),
        Process=lambda **_kwargs: BrokenProcess(),
    )
    monkeypatch.setattr(pipeline_runtime.multiprocessing, "get_context", lambda _method: fake_context)
    monkeypatch.setattr(pipeline_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        pipeline_runtime.os,
        "rmdir",
        lambda _path: (_ for _ in ()).throw(OSError("directory busy")),
    )

    with pytest.raises(RuntimeError, match="cannot start child"):
        pipeline_runtime._run_stage_in_subprocess(
            pipeline_runtime.StageSpec("start-failure", lambda _context: None, timeout_seconds=1),
            {},
        )


def test_pipeline_runtime_entrypoint_writes_success_signal(tmp_path, monkeypatch):
    import pipeline_runtime

    signals = []
    result_path = tmp_path / "context.json"
    monkeypatch.setattr(pipeline_runtime, "_configure_child_logging", lambda: None)

    pipeline_runtime._stage_process_entrypoint(
        pipeline_runtime.StageSpec("success", lambda context: context.update(done=True)),
        {"source": "offline"},
        SimpleNamespace(put=signals.append),
        str(result_path),
    )

    assert signals == [
        {
            "status": "success",
            "context_sha256": signals[0]["context_sha256"],
        }
    ]
    assert pipeline_runtime._read_context_from_file(
        str(result_path),
        expected_sha256=signals[0]["context_sha256"],
    ) == {"source": "offline", "done": True}


def test_run_config_returns_explicit_non_trading_decision(monkeypatch):
    import run_config

    monkeypatch.setattr(run_config, "_is_td", lambda _day: False)

    assert run_config.get_schedule_decision(date(2026, 7, 18)) == {
        "is_trading_day": False,
        "should_run": False,
        "reason": "today is not a trading day",
        "trading_day_index_in_week": 0,
        "week_trading_days": [],
        "window_anchor_day": None,
    }


def test_trading_calendar_import_fallbacks(monkeypatch):
    import trading_calendar

    module_path = Path(trading_calendar.__file__)
    monkeypatch.setattr(
        metadata,
        "version",
        lambda _name: (_ for _ in ()).throw(metadata.PackageNotFoundError("missing metadata")),
    )
    without_metadata = runpy.run_path(str(module_path))
    assert without_metadata["_HAS_CALENDAR_LIB"] is True
    assert without_metadata["_CALENDAR_PACKAGE_VERSION"] is None

    real_import = builtins.__import__

    def import_without_calendar(name, *args, **kwargs):
        if name == "chinese_calendar":
            raise ImportError("calendar unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_calendar)
    without_library = runpy.run_path(str(module_path))
    assert without_library["_HAS_CALENDAR_LIB"] is False
    assert without_library["_CALENDAR_PACKAGE_VERSION"] is None


def test_tdx_helpers_handle_conflicts_malformed_records_and_decode_fallback(tmp_path):
    import tdx_stock_universe as tdx

    assert tdx.find_rejected_stock_names("concatenate", ["CAT"]) == []
    assert tdx._stock_name_lookup_keys(None) == []
    assert tdx.lookup_tdx_stock_code({"宏和科技": "000001"}, "宏和电子") == ""
    assert tdx._is_a_share_stock_code("600000", "unknown.tnf") is False

    stock_codes = {}
    tdx._add_stock_code(stock_codes, "", "600000")
    assert stock_codes == {}
    stock_codes["示例科技"] = "600000"
    tdx._add_stock_code(stock_codes, "示例科技", "600001")
    assert stock_codes["示例科技"] == "600000"

    malformed = tmp_path / "malformed.tnf"
    malformed.write_bytes(b"\0" * (tdx.TNF_HEADER_SIZE + 1))
    assert list(tdx._iter_tnf_records(malformed, "shs.tnf")) == []

    invalid_text = tmp_path / "invalid.code"
    invalid_text.write_bytes(b"\xff")
    assert tdx._read_text_file(invalid_text) == ""


def test_tdx_infoharbor_iterators_skip_invalid_rows(tmp_path):
    import tdx_stock_universe as tdx

    full_names = tmp_path / "full_names.code"
    full_names.write_text(
        "broken\n123|TooShort\n600000|\n600000|有效科技\n",
        encoding="utf-8",
    )
    assert list(tdx._iter_infoharbor_full_names(full_names)) == [("有效科技", "600000")]

    aliases = tmp_path / "aliases.code"
    aliases.write_text(
        "broken\n600000|字段不足\nbad|主名科技|别名科技\n600000||别名科技\n"
        "600000|主名科技|有效别名科技\n",
        encoding="utf-8",
    )
    assert list(tdx._iter_infoharbor_company_aliases(aliases)) == [
        ("有效别名科技", "600000"),
    ]

    stock_codes = {}
    tdx._add_unique_alias(stock_codes, "", "600000")
    assert stock_codes == {}


def _make_tdx_cache(root: Path, market_files: tuple[str, ...]) -> Path:
    cache = root / "T0002" / "hq_cache"
    cache.mkdir(parents=True)
    for market_file in market_files:
        (cache / market_file).write_bytes(b"")
    return cache


def test_tdx_loader_handles_missing_and_unreadable_sources(tmp_path, monkeypatch):
    import tdx_stock_universe as tdx

    missing_root = tmp_path / "missing"
    assert tdx.load_tdx_stock_code_map(str(missing_root)) == {}

    broken_market_root = tmp_path / "broken-market"
    _make_tdx_cache(broken_market_root, tdx.TNF_MARKET_FILES)
    monkeypatch.setattr(
        tdx,
        "_iter_tnf_records",
        lambda *_args: (_ for _ in ()).throw(OSError("unreadable market table")),
    )
    assert tdx.load_tdx_stock_code_map(str(broken_market_root)) == {}

    broken_info_root = tmp_path / "broken-info"
    broken_info_cache = _make_tdx_cache(broken_info_root, tdx.TNF_MARKET_FILES)
    (broken_info_cache / tdx.INFOHARBOR_CODE_FILE).write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(tdx, "_iter_tnf_records", lambda *_args: iter([("主名科技", "600000")]))
    monkeypatch.setattr(
        tdx,
        "_iter_infoharbor_full_names",
        lambda _path: (_ for _ in ()).throw(OSError("unreadable infoharbor")),
    )
    loaded = tdx.load_tdx_stock_code_map(str(broken_info_root))
    assert loaded["主名科技"] == "600000"


def test_tdx_loader_keeps_only_local_unique_infoharbor_aliases(tmp_path, monkeypatch):
    import tdx_stock_universe as tdx

    root = tmp_path / "aliases"
    cache = _make_tdx_cache(root, tdx.TNF_MARKET_FILES)
    (cache / tdx.INFOHARBOR_CODE_FILE).write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        tdx,
        "_iter_tnf_records",
        lambda _path, market_file: iter(
            [("主名一", "600000"), ("主名二", "600001")]
            if market_file == "shs.tnf"
            else []
        ),
    )
    monkeypatch.setattr(
        tdx,
        "_iter_infoharbor_full_names",
        lambda _path: iter([("全名一", "600000"), ("外部全名", "999999")]),
    )
    monkeypatch.setattr(
        tdx,
        "_iter_infoharbor_company_aliases",
        lambda _path: iter(
            [
                ("唯一别名科技", "600000"),
                ("外部别名科技", "999999"),
                ("歧义别名科技", "600000"),
                ("歧义别名科技", "600001"),
            ]
        ),
    )

    stock_codes = tdx.load_tdx_stock_code_map(str(root))

    assert stock_codes["全名一"] == "600000"
    assert stock_codes["唯一别名科技"] == "600000"
    assert "外部全名" not in stock_codes
    assert "外部别名科技" not in stock_codes
    assert "歧义别名科技" not in stock_codes


def test_utils_handles_existing_escapes_and_wrong_top_level_type():
    import utils

    escaped = r'{"text":"say \"hello\""}'
    assert utils._escape_unescaped_inner_quotes(escaped) == escaped

    with pytest.raises(json.JSONDecodeError):
        utils.extract_json_from_llm_response("[]", expect_type="dict")
    with pytest.raises(json.JSONDecodeError):
        utils.extract_json_from_llm_response('{"a": 1]', allow_partial=True)
