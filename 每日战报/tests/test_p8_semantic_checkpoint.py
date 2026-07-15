from __future__ import annotations

from pathlib import Path


RUN_TS = "20260715083000"


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _manifest(
    p6_path: str,
    *,
    p8_status: str,
    p8_files: dict[str, str],
    stocks_requested: int,
    stocks_with_data: int,
) -> dict:
    return {
        "run_timestamp": RUN_TS,
        "status": "failed_exception",
        "stages": {
            "p6": {
                "status": "completed",
                "files": {"md": p6_path},
                "metrics": {
                    "accepted_targets": 5,
                    "stock_count": 5,
                    "identity_validation_version": 2,
                },
            },
            "market_mapping": {"status": "completed"},
            "p8": {
                "status": p8_status,
                "files": p8_files,
                "metrics": {
                    "stocks_requested": stocks_requested,
                    "stocks_with_data": stocks_with_data,
                },
            },
        },
    }


def _detect(monkeypatch, tmp_path: Path, manifest: dict) -> dict:
    import checkpoint

    monkeypatch.setattr(checkpoint, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(checkpoint, "load_run_manifest", lambda: manifest)
    return checkpoint.detect_checkpoint()


def _successful_p6(tmp_path: Path) -> str:
    targets = "\n".join(
        f"- Example {index}（60000{index}） | high | " + ("reason " * 35)
        for index in range(5)
    )
    return _write(
        tmp_path / f"P6_mapping_{RUN_TS}.md",
        "### 赛道: example\n> 事件触发: usable event\n" + targets,
    )


def _usable_p8_markdown(code: str = "600000") -> str:
    return f"# P8 technical\n\n### Example ({code})\n- 收盘 10.00 元\n"


def test_no_stocks_checkpoint_is_invalid_when_current_p6_has_targets(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "no stocks were mapped")
    manifest = _manifest(
        p6_path,
        p8_status="skipped_no_stocks",
        p8_files={"md": p8_path},
        stocks_requested=0,
        stocks_with_data=0,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False
    assert checkpoint["p8_assessment"]["reason"] == "stocks_now_available"


def test_json_only_empty_object_checkpoint_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.json", "{}")
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"json": p8_path},
        stocks_requested=5,
        stocks_with_data=0,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_zero_byte_markdown_checkpoint_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = tmp_path / f"P8_technical_{RUN_TS}.md"
    p8_path.touch()
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": str(p8_path)},
        stocks_requested=5,
        stocks_with_data=1,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_no_vipdoc_checkpoint_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "vipdoc unavailable")
    manifest = _manifest(
        p6_path,
        p8_status="skipped_no_vipdoc",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=0,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_failed_fallback_checkpoint_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "fallback placeholder")
    manifest = _manifest(
        p6_path,
        p8_status="failed_exception_fallback",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=0,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_completed_checkpoint_without_any_stock_data_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "headers without price rows")
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=0,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_completed_checkpoint_with_usable_stock_data_is_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", _usable_p8_markdown())
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_resume"
    assert checkpoint["p8_assessment"]["state"] == "valid"
    assert checkpoint["p8_assessment"]["reusable"] is True


def test_completed_checkpoint_with_header_only_markdown_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(
        tmp_path / f"P8_technical_{RUN_TS}.md",
        "# P8 股价分析数据\n\n> 读取时间: 2026-07-15T08:30:00\n",
    )
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reason"] == "markdown_missing_usable_stock_rows"


def test_stock_code_and_close_label_without_numeric_price_is_not_reusable(
    monkeypatch,
    tmp_path,
):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(
        tmp_path / f"P8_technical_{RUN_TS}.md",
        "# P8 technical\n\n### Example (600000)\n- 收盘数据暂无\n",
    )
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reason"] == "markdown_missing_usable_stock_rows"


def test_whitespace_only_markdown_is_not_reusable(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", " \n\t\n")
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"
    assert checkpoint["p8_assessment"]["reason"] == "empty_markdown"


def test_invalid_p8_overrides_old_terminal_success(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "# P8 technical\n")
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "old report")
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )
    manifest["status"] = "completed"
    manifest["stages"].update(
        {
            "p9": {"status": "completed_email_sent", "files": {"md": report_path}},
            "report": {"status": "completed"},
            "roi": {"status": "completed"},
        }
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p8_resume"


def test_valid_p8_keeps_old_terminal_success(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", _usable_p8_markdown())
    report_path = _write(tmp_path / f"war_report_{RUN_TS}.md", "old report")
    roi_md_path = _write(
        tmp_path / f"Layer_ROI_{RUN_TS}.md",
        "# Layer ROI\n\n- evaluated layers: 4",
    )
    roi_json_path = _write(
        tmp_path / f"Layer_ROI_{RUN_TS}.json",
        '{"layers":{"p1":{},"p3":{},"p4":{},"p75":{}}}',
    )
    manifest = _manifest(
        p6_path,
        p8_status="completed",
        p8_files={"md": p8_path},
        stocks_requested=5,
        stocks_with_data=1,
    )
    manifest["status"] = "completed"
    manifest["stages"].update(
        {
            "p9": {"status": "completed_email_sent", "files": {"md": report_path}},
            "report": {"status": "completed"},
            "roi": {
                "status": "completed",
                "files": {"md": roi_md_path, "json": roi_json_path},
            },
        }
    )

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p9_done"


def test_no_stocks_checkpoint_is_invalid_when_p6_failed(monkeypatch, tmp_path):
    p6_path = _successful_p6(tmp_path)
    p5_path = _write(tmp_path / f"P5_audit_{RUN_TS}.md", "recoverable upstream evidence")
    p2_path = _write(tmp_path / f"P2_x_intel_{RUN_TS}.md", "usable X intelligence")
    p8_path = _write(tmp_path / f"P8_technical_{RUN_TS}.md", "no stocks were mapped")
    manifest = _manifest(
        p6_path,
        p8_status="skipped_no_stocks",
        p8_files={"md": p8_path},
        stocks_requested=0,
        stocks_with_data=0,
    )
    manifest["stages"].update(
        {
            "p1": {"status": "completed_no_results"},
            "p2": {"status": "completed", "files": {"md": p2_path}},
            "p3": {"status": "completed_no_results"},
        }
    )
    manifest["stages"]["p5"] = {"status": "completed", "files": {"md": p5_path}}
    manifest["stages"]["p6"]["status"] = "failed_empty_output"

    checkpoint = _detect(monkeypatch, tmp_path, manifest)

    assert checkpoint["stage"] == "p6_resume"
    assert checkpoint["p8_assessment"]["reusable"] is False


def test_stage_technical_reruns_nonempty_checkpoint_from_structured_assessment(monkeypatch):
    import orchestrator

    calls = []
    monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda _report: [("Stock", "600000")])
    monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda _report: {})
    monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda _report: {})
    monkeypatch.setattr(
        orchestrator,
        "p8_search_stock_prices",
        lambda stocks, _tracks, _elasticity: calls.append(stocks) or "fresh technical data",
    )
    context = {
        "resume_from": "p9_resume",
        "ckpt": {"files": {}, "p8_assessment": {"reusable": False, "reason": "no_usable_rows"}},
        "p6_mapping": "current mapping",
        "price_data": _usable_p8_markdown(),
    }

    orchestrator._stage_technical(context)

    assert calls == [[("Stock", "600000")]]
    assert context["price_data"] == "fresh technical data"


def test_stage_technical_reuses_checkpoint_from_structured_assessment(monkeypatch):
    import orchestrator

    monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda _report: [("Stock", "600000")])
    monkeypatch.setattr(
        orchestrator,
        "p8_search_stock_prices",
        lambda *_args: (_ for _ in ()).throw(AssertionError("reusable P8 checkpoint must not rerun")),
    )
    context = {
        "resume_from": "p9_resume",
        "ckpt": {
            "files": {},
            "p8_status": "completed",
            "p8_metrics": {"stocks_requested": 1, "stocks_with_data": 1},
            "p8_assessment": {
                "state": "valid",
                "reusable": True,
                "reason": "usable_stock_data",
                "stocks_requested": 1,
                "stocks_with_data": 1,
            },
        },
        "p6_mapping": "current mapping",
        "price_data": _usable_p8_markdown(),
    }

    orchestrator._stage_technical(context)

    assert context["price_data"] == _usable_p8_markdown()


def test_stage_technical_reruns_valid_empty_checkpoint_when_p6_now_has_stocks(monkeypatch):
    import orchestrator

    calls = []
    monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda _report: [("Stock", "600000")])
    monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda _report: {})
    monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda _report: {})
    monkeypatch.setattr(
        orchestrator,
        "p8_search_stock_prices",
        lambda stocks, _tracks, _elasticity: calls.append(stocks) or "fresh technical data",
    )
    context = {
        "resume_from": "p9_resume",
        "ckpt": {
            "files": {},
            "p8_status": "skipped_no_stocks",
            "p8_metrics": {"stocks_requested": 0, "stocks_with_data": 0},
            "p8_assessment": {
                "state": "valid_empty",
                "reusable": True,
                "reason": "valid_business_empty",
                "stocks_requested": 0,
                "stocks_with_data": 0,
            },
        },
        "p6_mapping": "current mapping",
        "price_data": "本次未从 P6 映射中提取到 A 股标的，技术面分析跳过。",
    }

    orchestrator._stage_technical(context)

    assert calls == [[("Stock", "600000")]]
    assert context["price_data"] == "fresh technical data"


def test_stage_technical_reuses_valid_empty_checkpoint_when_p6_still_has_no_stocks(monkeypatch):
    import orchestrator

    monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda _report: [])
    monkeypatch.setattr(
        orchestrator,
        "p8_search_stock_prices",
        lambda *_args: (_ for _ in ()).throw(AssertionError("valid empty P8 checkpoint must not rerun")),
    )
    context = {
        "resume_from": "p9_resume",
        "ckpt": {
            "files": {},
            "p8_status": "skipped_no_stocks",
            "p8_metrics": {"stocks_requested": 0, "stocks_with_data": 0},
            "p8_assessment": {
                "state": "valid_empty",
                "reusable": True,
                "reason": "valid_business_empty",
                "stocks_requested": 0,
                "stocks_with_data": 0,
            },
        },
        "p6_mapping": "current mapping",
        "price_data": "本次未从 P6 映射中提取到 A 股标的，技术面分析跳过。",
    }

    orchestrator._stage_technical(context)

    assert context["price_data"] == "本次未从 P6 映射中提取到 A 股标的，技术面分析跳过。"
