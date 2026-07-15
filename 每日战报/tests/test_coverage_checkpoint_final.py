from __future__ import annotations

import json

import pytest

import checkpoint


def test_file_probes_tolerate_missing_paths_and_os_errors(tmp_path, monkeypatch):
    missing = tmp_path / "missing.md"
    assert checkpoint._pick_existing_file(str(missing)) == ""

    class BrokenPath:
        def is_file(self):
            raise OSError("simulated metadata failure")

        def stat(self):
            raise OSError("simulated stat failure")

    monkeypatch.setattr(checkpoint, "Path", lambda _path: BrokenPath())

    assert checkpoint._pick_existing_file("unreadable.md") == ""
    assert checkpoint.get_file_mtime({"p1": "unreadable.md"}, "p1") == 0.0


def test_current_stage_payload_rejects_legacy_non_mapping_shapes():
    assert checkpoint._current_stage_payload({"stages": []}, "p1") == {}
    assert checkpoint._current_stage_payload({"stages": {"p1": []}}, "p1") == {}


def test_checkpoint_readers_reject_wrong_suffix_and_corrupt_files(tmp_path):
    wrong_suffix = tmp_path / "checkpoint.txt"
    wrong_suffix.write_text("content", encoding="utf-8")
    assert checkpoint._read_checkpoint_text(str(wrong_suffix)) is None

    unreadable_markdown = tmp_path / "unreadable.md"
    unreadable_markdown.write_bytes(b"\xff\xfe\xfa")
    assert checkpoint._read_checkpoint_text(str(unreadable_markdown)) is None

    corrupt_json = tmp_path / "corrupt.json"
    corrupt_json.write_text("{broken", encoding="utf-8")
    assert checkpoint._read_checkpoint_json(str(corrupt_json)) is None


def test_p8_empty_reason_requires_a_business_explanation():
    metadata_only = """
# P8 技术面

读取时间: 2026-07-15 09:30
注：数据源暂不可用
暂无数据
"""
    assert checkpoint._p8_markdown_has_business_reason(metadata_only) is False
    assert checkpoint._p8_markdown_has_business_reason("未发现可映射的 A 股标的") is True


def test_structured_and_plain_x_blocks_and_unmetered_quotes_are_usable():
    structured = "<x_leaks>\n- **核心内容**: AI 订单继续增长\n</x_leaks>"
    plain = "<x_leaks>供应链交付节奏较上月改善</x_leaks>"

    assert checkpoint._p2_markdown_has_intel(structured, {}) == (True, 1)
    assert checkpoint._p2_markdown_has_intel(plain, {}) == (True, 1)
    assert checkpoint._p4_markdown_has_quotes("行情有效，收盘价 12.30", {}) == (True, 1)


def _complete_report() -> str:
    return """# VCP Hunter P9 战报

## 一、核心信号
| 标的 | 代码 | 结论 |
| --- | --- | --- |
核心订单信号已得到交叉验证。

## 二、标的狙击表
| 优先级 | 标的 | 理由 |
| --- | --- | --- |
首选标的具备明确催化剂。

## 三、资金路径推演
资金可能沿上游向核心器件扩散。

## 四、今日操作建议
等待量价确认后分批执行。

## ⚠️ 风险提示
订单节奏和估值波动仍需关注。
"""


def test_report_validation_rejects_reordered_sections_and_skips_table_scaffolding():
    reordered = """# VCP Hunter P9 战报

## 二、标的狙击表
有效内容。
## 一、核心信号
有效内容。
## 三、资金路径推演
有效内容。
## 四、今日操作建议
有效内容。
## ⚠️ 风险提示
有效内容。
"""

    assert checkpoint._report_markdown_is_complete(reordered) is False
    assert checkpoint._report_markdown_is_complete(_complete_report()) is True


@pytest.mark.parametrize(
    ("status", "expected_reusable", "expected_reason"),
    [
        ("completed", True, "usable_markdown"),
        ("cancelled", False, "invalid_status"),
    ],
)
def test_generic_stage_normalizes_non_mapping_metrics(
    tmp_path,
    status,
    expected_reusable,
    expected_reason,
):
    markdown = tmp_path / "custom.md"
    markdown.write_text("兼容阶段保留了可核验的业务内容。", encoding="utf-8")
    manifest = {
        "manifest_version": 3,
        "stages": {"custom": {"status": status, "metrics": []}},
    }

    result = checkpoint.assess_stage_checkpoint(
        "custom",
        manifest,
        {"custom": str(markdown)},
    )

    assert result["reusable"] is expected_reusable
    assert result["reason"] == expected_reason


def test_p8_reader_rejects_non_markdown_and_unreadable_artifacts(tmp_path):
    json_path = tmp_path / "p8.json"
    json_path.write_text("{}", encoding="utf-8")
    unreadable = tmp_path / "p8.md"
    unreadable.write_bytes(b"\xff\xfe\xfa")

    assert checkpoint.assess_p8_checkpoint(str(json_path))["reason"] == "non_markdown_artifact"
    assert checkpoint.assess_p8_checkpoint(str(unreadable))["reason"] == "unreadable_markdown"


@pytest.mark.parametrize(
    ("status", "metrics", "current_count", "text", "expected_reason"),
    [
        (
            "skipped_no_stocks",
            {"stocks_requested": 1, "stocks_with_data": 0},
            0,
            "未发现可映射的 A 股标的",
            "invalid_no_stocks_metrics",
        ),
        (
            "skipped_no_stocks",
            {"stocks_requested": 0, "stocks_with_data": 0},
            0,
            "# P8\n读取时间: 2026-07-15\n注：无\n暂无数据",
            "missing_business_empty_reason",
        ),
        (
            "completed",
            {"stocks_requested": 0, "stocks_with_data": 0},
            0,
            "已完成技术面核验。",
            "completed_without_requested_stocks",
        ),
        (
            "completed",
            {"stocks_requested": 1, "stocks_with_data": 2},
            1,
            "已完成技术面核验。",
            "invalid_stock_metrics",
        ),
        (
            "completed",
            {"stocks_requested": 1, "stocks_with_data": 1},
            2,
            "000001 收盘价 10.50",
            "current_stock_count_changed",
        ),
        (
            "completed_recovered",
            {"stocks_requested": 1, "stocks_with_data": 1},
            1,
            "000001 收盘价 10.50",
            "completed_recovered",
        ),
        (
            "",
            {},
            None,
            "历史文件仅保留文字，未包含有效行情行。",
            "legacy_markdown_without_usable_rows",
        ),
        (
            "migrated",
            {"stocks_requested": 1, "stocks_with_data": 1},
            1,
            "000001 收盘价 10.50",
            "unrecognized_status",
        ),
    ],
)
def test_p8_status_and_metrics_validation_boundaries(
    status,
    metrics,
    current_count,
    text,
    expected_reason,
):
    result = checkpoint.assess_p8_checkpoint(
        markdown_text=text,
        stage_status=status,
        stage_metrics=metrics,
        current_stock_count=current_count,
    )

    assert result["reusable"] is False
    assert result["reason"] == expected_reason


def test_p8_manifest_treats_zero_parsed_targets_as_unknown_for_completed_run(tmp_path):
    p6_path = tmp_path / "P6_映射.md"
    p6_path.write_text("映射文件存在，但旧格式未包含可解析代码。", encoding="utf-8")
    p8_path = tmp_path / "P8_技术面.md"
    p8_path.write_text("000001 收盘价 10.50", encoding="utf-8")
    manifest = {
        "stages": {
            "p6": {"status": "completed"},
            "p8": {
                "status": "completed",
                "metrics": {"stocks_requested": 1, "stocks_with_data": 1},
                "files": {"md": str(p8_path)},
            },
        }
    }

    result = checkpoint._assess_p8_from_manifest(
        manifest,
        {"p6": str(p6_path), "p8": str(p8_path)},
    )

    assert result["reusable"] is True
    assert result["current_stock_count"] is None


def test_invalid_iso_timestamp_is_safely_ignored():
    assert checkpoint._iso_timestamp("not-a-timestamp") == 0.0


def test_legacy_missing_intel_detection_respects_files_and_terminal_empty_statuses(tmp_path):
    p1_path = tmp_path / "p1.json"
    p1_path.write_text("[]", encoding="utf-8")
    manifest = {
        "stages": {
            "p1": {"status": "completed"},
            "p2": {"status": "failed_timeout"},
            "p3": {"status": "skipped_no_results"},
        }
    }

    assert checkpoint._missing_intel_resume_stages(
        manifest,
        {"p1": str(p1_path)},
    ) == ["p2"]


def test_fallback_scan_deduplicates_matches_and_rejects_empty_required_artifacts(
    tmp_path,
    monkeypatch,
):
    legacy = tmp_path / "legacy.md"
    legacy.write_text("收盘价 9.80", encoding="utf-8")
    monkeypatch.setattr(checkpoint, "FALLBACK_PATTERNS", {"p4": ("*.md", "*.md")})
    recovered = {"files": {}, "run_timestamp": ""}

    checkpoint._merge_files_from_fallback(recovered, tmp_path)

    assert recovered["files"] == {"p4": str(legacy)}

    empty_p8 = tmp_path / "P8_技术面_20260715093000.md"
    empty_p8.write_bytes(b"")
    monkeypatch.setattr(
        checkpoint,
        "FALLBACK_PATTERNS",
        {"p8": ("P8_技术面_*.md",)},
    )
    recovered = {"files": {}, "run_timestamp": ""}

    checkpoint._merge_files_from_fallback(
        recovered,
        tmp_path,
        run_timestamp="20260715093000",
    )

    assert recovered["files"] == {}


def test_fallback_without_selected_run_rejects_a_timestamped_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "legacy.md"
    candidate.write_text("有效内容", encoding="utf-8")
    monkeypatch.setattr(checkpoint, "FALLBACK_PATTERNS", {"p4": ("legacy.md",)})
    extracted = iter(("", "20260715093000"))
    monkeypatch.setattr(
        checkpoint,
        "extract_timestamp_from_name",
        lambda _path: next(extracted),
    )
    recovered = {"files": {}, "run_timestamp": ""}

    checkpoint._merge_files_from_fallback(recovered, tmp_path)

    assert recovered["files"] == {}
    assert recovered["run_timestamp"] == ""


def test_derive_stage_can_resume_from_a_lone_compatible_p1_artifact():
    assessments = {
        stage: {"reusable": True, "state": "valid"}
        for stage in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7a", "p75", "p9", "roi")
    }

    assert checkpoint._derive_stage(
        {"p1": "legacy-p1.json"},
        {},
        p8_assessment={"reusable": False},
        stage_assessments=assessments,
    ) == "p1"


def test_detect_checkpoint_recovers_timestamp_after_legacy_fallback_merge(tmp_path, monkeypatch):
    legacy_p4 = tmp_path / "P4_海外行情.md"
    legacy_p4.write_text("可用行情，收盘价 8.60", encoding="utf-8")
    timestamped_p1 = tmp_path / "P1_原始数据_20260715093000.json"
    timestamped_p1.write_text(
        json.dumps([{"text": "供应链订单取得实质进展"}], ensure_ascii=False),
        encoding="utf-8",
    )

    def merge_legacy_files(ckpt, _day_dir, *, run_timestamp=""):
        assert run_timestamp == ""
        ckpt["files"]["p4"] = str(legacy_p4)
        ckpt["files"]["p1"] = str(timestamped_p1)

    monkeypatch.setattr(checkpoint, "load_run_manifest", lambda: {})
    monkeypatch.setattr(checkpoint, "get_today_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(checkpoint, "_merge_files_from_fallback", merge_legacy_files)

    recovered = checkpoint.detect_checkpoint()

    assert recovered["run_timestamp"] == "20260715093000"
    assert recovered["files"]["p1"] == str(timestamped_p1)
