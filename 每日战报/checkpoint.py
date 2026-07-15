from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from artifact_store import extract_timestamp_from_name, load_run_manifest
from p6_target_parser import extract_p6_targets
from semantic_output import has_meaningful_semantic_text, is_no_evidence_text
from settings import get_today_output_dir
from tdx_stock_universe import IDENTITY_VALIDATION_VERSION

FALLBACK_PATTERNS: dict[str, tuple[str, ...]] = {
    "war_report": ("战报_*.md",),
    "p8": ("P8_技术面_*.md",),
    "p7a": ("P7a_深研审计_*.md",),
    "p7": ("P7_深度研究_*.md",),
    "p75": ("P7.5_补充审计_*.md",),
    "p75_raw": ("P7.5_补充搜索原始数据_*.md",),
    "p6": ("P6_映射_*.md",),
    "p5": ("P5_审计_*.md",),
    "p4": ("P4_核心行情_*.md", "P4_海外行情_*.md"),
    "p3": ("P3_机动情报_*.md",),
    "p2": ("P2_X平台情报_*.md",),
    "p1": ("P1_*原始数据_*.json",),
    "p1_md": ("P1_*原始数据_*.md",),
    "roi_md": ("Layer_ROI_*.md",),
    "roi_json": ("Layer_ROI_*.json",),
}

P9_DELIVERY_SUCCESS_STATUSES = frozenset({"completed_email_sent", "completed_no_email"})
STAGE_SUCCESS_STATUSES = frozenset(
    {
        "completed",
        "completed_recovered",
        "completed_structured_degraded",
    }
)
ROOT_SUCCESS_STATUSES = frozenset(
    {
        "completed",
        "completed_existing_report",
        "completed_with_stage_degradation",
    }
)
DELIVERY_RECOVERABLE_STATUSES = frozenset(
    {
        "completed_email_failed",
        "failed_delivery",
        "report_save_in_progress",
        "report_saved_pending_delivery",
    }
)
P6_HASHED_RESEARCH_STAGES = ("p7", "p7a", "p75")
INTEL_STAGES = ("p1", "p2", "p3")
P8_ACTIVE_STATUSES = frozenset({"pending", "running", "retrying", "started"})
P8_EMPTY_SENTINELS = frozenset(
    {
        "无",
        "暂无",
        "无数据",
        "暂无数据",
        "none",
        "n/a",
        "null",
        "{}",
        "[]",
        "no data",
        "not found",
    }
)
P8_STOCK_CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")
P8_CLOSE_VALUE_PATTERN = re.compile(
    r"(?:收盘价?|最新价)\s*[:：]?\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))",
    re.I,
)
CHECKPOINT_ACTIVE_STATUSES = frozenset(
    {"pending", "running", "retrying", "started", "json_ready", "raw_ready"}
)
VALID_EMPTY_STATUSES: dict[str, frozenset[str]] = {
    "p1": frozenset({"skipped_no_results", "completed_no_results"}),
    "p2": frozenset({"completed_no_results"}),
    "p3": frozenset({"skipped_no_results", "completed_no_results"}),
    "p5": frozenset({"completed_no_results"}),
    "p7": frozenset({"skipped_not_triggered", "skipped_no_results"}),
    "p7a": frozenset(
        {"skipped_not_triggered", "skipped_no_results", "completed_no_results"}
    ),
    "p75": frozenset(
        {
            "skipped_not_triggered",
            "skipped_no_results",
            "completed_no_results",
            "completed_no_valid_evidence",
        }
    ),
}
VALID_PASSTHROUGH_STATUSES: dict[str, frozenset[str]] = {
    "p7a": frozenset(
        {
            "skipped_short_input",
            "failed_missing_prompt",
            "failed_invoke",
            "failed_empty_output",
            "failed_semantic_output",
            "failed_exception_passthrough",
        }
    ),
    "p75": frozenset(
        {
            "failed_missing_prompt",
            "failed_invoke",
            "failed_empty_output",
            "failed_semantic_output",
        }
    ),
}
REUSABLE_WITHOUT_ARTIFACT_STATES = frozenset(
    {"valid_empty", "valid_passthrough"}
)
P4_USABLE_QUOTE_PATTERN = re.compile(r"收盘价\s+(?!nan\b)[+-]?(?:\d+(?:\.\d+)?|\.\d+)", re.I)
P5_SUMMARY_PATTERN = re.compile(r"通过\s*[:：]\s*(\d+)条\s*/\s*删除\s*[:：]\s*(\d+)条")
P2_X_LEAK_PATTERN = re.compile(r"<x_leaks>\s*(.*?)\s*</x_leaks>", re.I | re.S)
P2_CORE_CLAIM_PATTERN = re.compile(
    r"^\s*-\s*\*\*核心内容\*\*\s*:\s*(\S.*)$",
    re.I | re.M,
)
STRUCTURED_CLAIM_PATTERN = re.compile(r"^\s*-\s*Claim\s*:\s*(\S.*)$", re.I | re.M)
REPORT_REQUIRED_HEADINGS = (
    "## 一、核心信号",
    "## 二、标的狙击表",
    "## 三、资金路径推演",
    "## 四、今日操作建议",
    "## ⚠️ 风险提示",
)
STAGE_PRIMARY_FILE_KEYS = {
    "p1": "p1",
    "p2": "p2",
    "p3": "p3",
    "p4": "p4",
    "p5": "p5",
    "p6": "p6",
    "p7": "p7",
    "p7a": "p7a",
    "p75": "p75",
    "p8": "p8",
    "p9": "war_report",
    "roi": "roi_md",
}


def _pick_existing_file(path: str, *, require_nonempty: bool = False) -> str:
    if not path:
        return ""
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return ""
        if require_nonempty and candidate.stat().st_size <= 0:
            return ""
    except OSError:
        return ""
    return path


def get_file_mtime(files: dict[str, str], file_key: str) -> float:
    path = files.get(file_key, "")
    if not path:
        return 0.0
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def compute_file_sha256(path: str) -> str:
    if not path:
        return ""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def is_older_than_any(files: dict[str, str], file_key: str, upstream_keys: tuple[str, ...]) -> bool:
    target_mtime = get_file_mtime(files, file_key)
    if not target_mtime:
        return False
    return any(get_file_mtime(files, upstream_key) > target_mtime for upstream_key in upstream_keys)


def _manifest_p6_input_hashes(manifest: dict) -> dict[str, str]:
    hashes = {}
    for stage_name in P6_HASHED_RESEARCH_STAGES:
        payload = _current_stage_payload(manifest, stage_name)
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        value = str(metrics.get("p6_input_sha256") or "").strip().lower()
        if value:
            hashes[stage_name] = value
    return hashes


def _p6_input_hash_is_stale(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
    stage_name: str,
) -> bool:
    return not p6_input_hash_matches(files, p6_input_hashes, stage_name)


def p6_input_hash_matches(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
    stage_name: str,
) -> bool:
    expected = str(p6_input_hashes.get(stage_name) or "").strip().lower()
    actual = compute_file_sha256(files.get("p6", ""))
    if not files.get(stage_name) and not expected:
        return True
    return bool(expected and actual and expected == actual)


def _drop_stale_downstream_files(
    files: dict[str, str],
    p6_input_hashes: dict[str, str],
) -> None:
    p7_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p7")
    p75_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p75")
    p7a_hash_stale = _p6_input_hash_is_stale(files, p6_input_hashes, "p7a")
    p7_stale = bool(files.get("p7")) and p7_hash_stale
    p75_stale = bool(files.get("p75")) and p75_hash_stale
    p7a_stale = bool(files.get("p7a")) and (
        not files.get("p7")
        or p7_stale
        or p7a_hash_stale
        or is_older_than_any(files, "p7a", ("p7",))
    )
    p6_dependency_stale = p7_hash_stale or p75_hash_stale or p7a_hash_stale

    for file_key, stale in (("p7", p7_stale), ("p75", p75_stale), ("p7a", p7a_stale)):
        if stale:
            files.pop(file_key, None)

    if p6_dependency_stale or (files.get("p8") and is_older_than_any(files, "p8", ("p6",))):
        files.pop("p8", None)

    if p6_dependency_stale or (
        files.get("war_report")
        and is_older_than_any(
            files,
            "war_report",
            ("p8", "p75", "p7a", "p7", "p6", "p5", "p4", "p3", "p2", "p1"),
        )
    ):
        files.pop("war_report", None)


def _current_stage_payload(manifest: dict, stage_name: str) -> dict:
    stages = manifest.get("stages", {})
    if not isinstance(stages, dict):
        return {}
    payload = stages.get(stage_name, {})
    if not isinstance(payload, dict):
        return {}

    manifest_run_ts = str(manifest.get("run_timestamp") or "").strip()
    stage_run_ts = str(payload.get("run_timestamp") or "").strip()
    if manifest_run_ts and stage_run_ts and manifest_run_ts != stage_run_ts:
        return {}
    return payload


def _stage_status(manifest: dict, stage_name: str) -> str:
    return str(_current_stage_payload(manifest, stage_name).get("status", ""))


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _p8_markdown_has_business_reason(markdown_text: str) -> bool:
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = line.lstrip(">-* ").strip()
        if not normalized or normalized.startswith(("读取时间:", "注：")):
            continue
        if normalized.lower() not in P8_EMPTY_SENTINELS:
            return True
    return False


def _p8_markdown_has_usable_rows(markdown_text: str) -> bool:
    code_matches = list(P8_STOCK_CODE_PATTERN.finditer(markdown_text))
    for index, code_match in enumerate(code_matches):
        segment_end = (
            code_matches[index + 1].start()
            if index + 1 < len(code_matches)
            else len(markdown_text)
        )
        segment = markdown_text[code_match.end():segment_end]
        close_match = P8_CLOSE_VALUE_PATTERN.search(segment)
        if close_match and float(close_match.group(1)) > 0:
            return True
    return False


def _read_checkpoint_text(path: str, *, suffix: str = ".md") -> str | None:
    candidate_path = str(path or "").strip()
    if not candidate_path:
        return None
    candidate = Path(candidate_path)
    if suffix and candidate.suffix.lower() != suffix:
        return None
    try:
        return candidate.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _read_checkpoint_json(path: str) -> Any:
    candidate_path = str(path or "").strip()
    if not candidate_path or Path(candidate_path).suffix.lower() != ".json":
        return None
    try:
        with open(candidate_path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _meaningful_markdown_text(markdown_text: str | None) -> bool:
    if not markdown_text or not markdown_text.strip():
        return False
    semantic_lines: list[str] = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or set(line) <= {"-", "=", "_", "*"}:
            continue
        normalized = line.lstrip(">-* ").strip()
        if not normalized or normalized.lower().startswith(
            (
                "生成时间:",
                "读取时间:",
                "采集时间:",
                "generated_at:",
                "goal:",
                "triggers:",
                "注：",
            )
        ):
            continue
        semantic_lines.append(normalized)
    if not semantic_lines:
        return False
    combined = " ".join(semantic_lines).strip()
    return combined.lower() not in P8_EMPTY_SENTINELS and not is_no_evidence_text(combined)


def _checkpoint_result(
    stage_name: str,
    status: str,
    reusable: bool,
    state: str,
    reason: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "stage": stage_name,
        "status": status,
        "reusable": reusable,
        "state": state,
        "reason": reason,
        **details,
    }


def _status_blocks_reuse(status: str) -> bool:
    return bool(
        status.startswith(("failed", "auth_failed"))
        or status in CHECKPOINT_ACTIVE_STATUSES
        or status in {"skipped_no_key", "skipped_missing_dependency", "skipped_no_vipdoc"}
    )


def _p1_json_has_evidence(path: str) -> bool:
    payload = _read_checkpoint_json(path)
    return bool(
        isinstance(payload, list)
        and any(
            isinstance(item, dict)
            and has_meaningful_semantic_text(str(item.get("text") or ""))
            for item in payload
        )
    )


def _p2_markdown_has_intel(markdown_text: str, metrics: dict[str, Any]) -> tuple[bool, int]:
    declared_items = _nonnegative_int(metrics.get("structured_items"))
    structured_claims = sum(
        1
        for claim in STRUCTURED_CLAIM_PATTERN.findall(markdown_text)
        if not is_no_evidence_text(claim)
    )
    raw_blocks = 0
    for raw_block in P2_X_LEAK_PATTERN.findall(markdown_text):
        raw_claims = P2_CORE_CLAIM_PATTERN.findall(raw_block)
        if raw_claims:
            if any(not is_no_evidence_text(claim) for claim in raw_claims):
                raw_blocks += 1
            continue
        block_text = re.sub(r"<[^>]+>", " ", raw_block)
        block_text = re.sub(r"\s+", " ", block_text).strip()
        if block_text and not is_no_evidence_text(block_text):
            raw_blocks += 1
    effective_items = max(structured_claims, raw_blocks)
    metrics_usable = declared_items is None or declared_items > 0 or raw_blocks > 0
    return bool(effective_items > 0 and metrics_usable), effective_items


def _p3_markdown_has_intel(markdown_text: str, manifest_version: int) -> tuple[bool, int]:
    structured_claims = sum(
        1
        for claim in STRUCTURED_CLAIM_PATTERN.findall(markdown_text)
        if not is_no_evidence_text(claim)
    )
    if manifest_version < 3:
        return True, structured_claims
    return structured_claims > 0, structured_claims


def _p4_markdown_has_quotes(markdown_text: str, metrics: dict[str, Any]) -> tuple[bool, int | None]:
    declared_usable = _nonnegative_int(
        metrics.get("tickers_success", metrics.get("usable_tickers"))
    )
    parsed_usable = len(P4_USABLE_QUOTE_PATTERN.findall(markdown_text))
    if declared_usable is not None:
        return declared_usable > 0 and parsed_usable > 0, parsed_usable
    return parsed_usable > 0, parsed_usable


def _p5_markdown_has_audit(markdown_text: str, metrics: dict[str, Any]) -> tuple[bool, int, int]:
    declared_passed = _nonnegative_int(metrics.get("passed_count"))
    declared_deleted = _nonnegative_int(metrics.get("deleted_count"))
    summary_match = P5_SUMMARY_PATTERN.search(markdown_text)
    if not summary_match:
        return False, declared_passed or 0, declared_deleted or 0
    parsed_passed = int(summary_match.group(1))
    parsed_deleted = int(summary_match.group(2))
    counts_consistent = (
        (declared_passed is None or declared_passed == parsed_passed)
        and (declared_deleted is None or declared_deleted == parsed_deleted)
    )
    return bool(
        counts_consistent and (parsed_passed > 0 or parsed_deleted > 0)
    ), parsed_passed, parsed_deleted


def _p6_markdown_has_mapping(markdown_text: str, metrics: dict[str, Any]) -> tuple[bool, int]:
    target_count = _nonnegative_int(
        metrics.get("accepted_targets", metrics.get("stock_count"))
    )
    parsed_count = len(extract_p6_targets(markdown_text))
    effective_count = target_count if target_count is not None else parsed_count
    return bool(
        effective_count >= 5
        and parsed_count >= 5
        and len(markdown_text) >= 1000
        and "### 赛道:" in markdown_text
    ), effective_count


def _report_markdown_is_complete(markdown_text: str) -> bool:
    if not markdown_text.strip() or not markdown_text.lstrip().startswith(
        "# VCP Hunter P9 战报"
    ):
        return False

    heading_matches = []
    for heading in REPORT_REQUIRED_HEADINGS:
        match = re.search(rf"^{re.escape(heading)}\s*$", markdown_text, re.M)
        if not match:
            return False
        heading_matches.append(match)
    if heading_matches != sorted(heading_matches, key=lambda match: match.start()):
        return False

    for index, match in enumerate(heading_matches):
        section_end = (
            heading_matches[index + 1].start()
            if index + 1 < len(heading_matches)
            else len(markdown_text)
        )
        section_lines = []
        for raw_line in markdown_text[match.end():section_end].splitlines():
            line = raw_line.strip()
            if not line or line == "---":
                continue
            if re.fullmatch(r"\|?(?:\s*:?-+:?\s*\|)+", line):
                continue
            if line.startswith(("| 标的 | 代码 |", "| 优先级 | 标的 |")):
                continue
            section_lines.append(line)
        if not _meaningful_markdown_text("\n".join(section_lines)):
            return False
    return True


def _roi_json_is_complete(payload: Any) -> bool:
    layers = payload.get("layers") if isinstance(payload, dict) else None
    return isinstance(layers, dict) and len(layers) >= 4


def assess_stage_checkpoint(
    stage_name: str,
    manifest: dict,
    files: dict[str, str],
) -> dict[str, Any]:
    """Validate a non-P8 stage artifact against its status and business structure."""
    payload = _current_stage_payload(manifest, stage_name)
    status = str(payload.get("status") or "").strip().lower()
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    manifest_version = _nonnegative_int(manifest.get("manifest_version")) or 0

    valid_empty = VALID_EMPTY_STATUSES.get(stage_name, frozenset())
    if status in valid_empty:
        return _checkpoint_result(stage_name, status, True, "valid_empty", status)
    valid_passthrough = VALID_PASSTHROUGH_STATUSES.get(stage_name, frozenset())
    if status in valid_passthrough:
        source_key = "p7" if stage_name == "p7a" else "p75_raw"
        source_text = _read_checkpoint_text(files.get(source_key, ""))
        source_usable = bool(source_text and _meaningful_markdown_text(source_text))
        return _checkpoint_result(
            stage_name,
            status,
            source_usable,
            "valid_passthrough" if source_usable else "invalid",
            status if source_usable else "missing_passthrough_source",
            passthrough_source=source_key,
        )
    if stage_name not in {"p9", "roi"} and _status_blocks_reuse(status):
        return _checkpoint_result(stage_name, status, False, "invalid", status or "blocked_status")

    if stage_name == "p1":
        usable = _p1_json_has_evidence(files.get("p1", ""))
        return _checkpoint_result(
            stage_name,
            status,
            usable,
            "valid" if usable else "invalid",
            "usable_evidence_records" if usable else "missing_or_empty_evidence_records",
        )

    file_key = {
        "p9": "war_report",
        "roi": "roi_md",
    }.get(stage_name, stage_name)
    markdown_text = _read_checkpoint_text(files.get(file_key, ""))

    if stage_name == "p9":
        status_usable = (
            not status
            or status in P9_DELIVERY_SUCCESS_STATUSES
            or status in DELIVERY_RECOVERABLE_STATUSES
        )
        report_complete = bool(
            markdown_text
            and (
                _report_markdown_is_complete(markdown_text)
                or (manifest_version < 3 and _meaningful_markdown_text(markdown_text))
            )
        )
        report_outcome = str(metrics.get("report_outcome") or "").strip().lower()
        actionable_items = _nonnegative_int(metrics.get("actionable_items_count"))
        outcome_consistent = bool(
            not report_outcome
            or (
                report_outcome == "actionable"
                and actionable_items is not None
                and actionable_items > 0
            )
            or (
                report_outcome == "completed_no_actionable_signals"
                and actionable_items == 0
            )
        )
        reusable = bool(status_usable and report_complete and outcome_consistent)
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            "complete_report" if reusable else "missing_incomplete_or_inconsistent_report",
            report_outcome=report_outcome,
            actionable_items_count=actionable_items,
        )

    if stage_name == "roi":
        json_payload = _read_checkpoint_json(files.get("roi_json", ""))
        reusable = bool(
            status in STAGE_SUCCESS_STATUSES
            and markdown_text
            and _meaningful_markdown_text(markdown_text)
            and _roi_json_is_complete(json_payload)
        )
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            "complete_roi_artifacts" if reusable else "missing_or_incomplete_roi_artifacts",
        )

    if not markdown_text or not _meaningful_markdown_text(markdown_text):
        return _checkpoint_result(
            stage_name,
            status,
            False,
            "invalid",
            "missing_or_semantically_empty_markdown",
        )

    if (
        manifest_version < 3
        and status in STAGE_SUCCESS_STATUSES
        and stage_name in {"p4", "p5"}
    ):
        return _checkpoint_result(
            stage_name,
            status,
            True,
            "legacy_valid",
            "legacy_nonempty_markdown",
        )

    if stage_name == "p2":
        successful_chunks = _nonnegative_int(metrics.get("successful_chunks"))
        content_length = _nonnegative_int(metrics.get("content_length"))
        structure_usable, effective_items = _p2_markdown_has_intel(markdown_text, metrics)
        if manifest_version < 3:
            structure_usable = True
        metrics_usable = (
            (successful_chunks is None or successful_chunks > 0)
            and (content_length is None or content_length > 0)
        )
        reusable = bool(
            structure_usable
            and metrics_usable
            and (not status or status in STAGE_SUCCESS_STATUSES)
        )
        reason = "usable_x_intel" if reusable else "invalid_x_intel_status_or_metrics"
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            reason,
            effective_items=effective_items,
        )
    elif stage_name == "p3":
        content_chars = _nonnegative_int(metrics.get("content_chars", metrics.get("content_length")))
        structured_items = _nonnegative_int(metrics.get("structured_items"))
        structure_usable, rendered_items = _p3_markdown_has_intel(
            markdown_text,
            manifest_version,
        )
        metrics_usable = (
            (content_chars is None or content_chars > 0)
            and (structured_items is None or structured_items > 0)
        )
        reusable = bool(
            structure_usable
            and metrics_usable
            and (not status or status in STAGE_SUCCESS_STATUSES)
        )
        reason = "usable_agent_intel" if reusable else "invalid_agent_intel_status_or_metrics"
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            reason,
            rendered_items=rendered_items,
        )
    elif stage_name == "p4":
        reusable, usable_quotes = _p4_markdown_has_quotes(markdown_text, metrics)
        reusable = bool(reusable and (not status or status in STAGE_SUCCESS_STATUSES))
        reason = "usable_market_quotes" if reusable else "no_usable_market_quotes"
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            reason,
            usable_quotes=usable_quotes,
        )
    elif stage_name == "p5":
        reusable, passed, deleted = _p5_markdown_has_audit(markdown_text, metrics)
        reusable = bool(
            reusable
            and (not status or status in STAGE_SUCCESS_STATUSES or status == "completed_no_results")
        )
        reason = "usable_audit" if reusable else "empty_or_failed_audit"
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            reason,
            passed_count=passed,
            deleted_count=deleted,
        )
    elif stage_name == "p6":
        reusable, target_count = _p6_markdown_has_mapping(markdown_text, metrics)
        identity_validation_version = _nonnegative_int(
            metrics.get("identity_validation_version")
        )
        identity_validation_current = bool(
            identity_validation_version is not None
            and identity_validation_version >= IDENTITY_VALIDATION_VERSION
        )
        reusable = bool(
            reusable
            and identity_validation_current
            and (not status or status in STAGE_SUCCESS_STATUSES)
        )
        if reusable:
            reason = "usable_mapping"
        elif not identity_validation_current:
            reason = "identity_validation_missing_or_outdated"
        else:
            reason = "incomplete_mapping"
        return _checkpoint_result(
            stage_name,
            status,
            reusable,
            "valid" if reusable else "invalid",
            reason,
            target_count=target_count,
            identity_validation_version=identity_validation_version,
        )
    elif stage_name in {"p7", "p7a", "p75"}:
        reusable = bool(not status or status in STAGE_SUCCESS_STATUSES)
        reason = "usable_research" if reusable else "invalid_research_status"
    else:
        reusable = bool(not status or status in STAGE_SUCCESS_STATUSES)
        reason = "usable_markdown" if reusable else "invalid_status"

    return _checkpoint_result(
        stage_name,
        status,
        reusable,
        "valid" if reusable else "invalid",
        reason,
    )


def assess_p8_checkpoint(
    markdown_path: str = "",
    *,
    markdown_text: str | None = None,
    stage_status: str = "",
    stage_metrics: dict[str, Any] | None = None,
    current_stock_count: int | None = None,
    stale: bool = False,
    upstream_valid: bool | None = None,
) -> dict[str, Any]:
    """Classify whether a P8 checkpoint is semantically safe to reuse."""
    metrics = stage_metrics if isinstance(stage_metrics, dict) else {}
    status = str(stage_status or "").strip().lower()
    requested = _nonnegative_int(metrics.get("stocks_requested"))
    with_data = _nonnegative_int(metrics.get("stocks_with_data"))
    current_count = _nonnegative_int(current_stock_count)
    path = str(markdown_path or "").strip()

    def _result(state: str, reusable: bool, reason: str) -> dict[str, Any]:
        return {
            "state": state,
            "reusable": reusable,
            "reason": reason,
            "status": status,
            "markdown_path": path,
            "stocks_requested": requested,
            "stocks_with_data": with_data,
            "current_stock_count": current_count,
        }

    if markdown_text is None:
        if not path:
            return _result("missing", False, "missing_markdown")
        candidate = Path(path)
        if candidate.suffix.lower() != ".md":
            return _result("invalid", False, "non_markdown_artifact")
        try:
            markdown_text = candidate.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return _result("invalid", False, "unreadable_markdown")

    if not str(markdown_text or "").strip():
        return _result("invalid", False, "empty_markdown")
    if stale:
        return _result("invalid", False, "stale_upstream")
    if upstream_valid is False:
        return _result("invalid", False, "upstream_p6_invalid")

    data_state = str(metrics.get("data_state") or "").strip().lower()
    if (
        status.startswith("failed")
        or "fallback" in status
        or status in P8_ACTIVE_STATUSES
        or status == "skipped_no_vipdoc"
        or data_state in {"degraded_empty", "invalid"}
    ):
        return _result("degraded_empty", False, status or data_state or "degraded_status")

    if status == "skipped_no_stocks":
        if requested != 0 or with_data != 0:
            return _result("invalid", False, "invalid_no_stocks_metrics")
        if current_count not in {None, 0}:
            return _result("invalid", False, "stocks_now_available")
        if not _p8_markdown_has_business_reason(str(markdown_text)):
            return _result("invalid", False, "missing_business_empty_reason")
        return _result("valid_empty", True, "valid_business_empty")

    if status == "completed":
        if requested is None or with_data is None:
            return _result("invalid", False, "missing_stock_metrics")
        if requested <= 0:
            return _result("invalid", False, "completed_without_requested_stocks")
        if with_data <= 0:
            return _result("degraded_empty", False, "no_usable_stock_data")
        if with_data > requested:
            return _result("invalid", False, "invalid_stock_metrics")
        if current_count is not None and current_count != requested:
            return _result("invalid", False, "current_stock_count_changed")
        if not _p8_markdown_has_usable_rows(str(markdown_text)):
            return _result("degraded_empty", False, "markdown_missing_usable_stock_rows")
        return _result("valid", True, "usable_stock_data")

    if status.startswith("completed") or status.startswith("skipped"):
        return _result("degraded_empty", False, status)
    if not status and requested is None and with_data is None:
        if _p8_markdown_has_usable_rows(str(markdown_text)):
            return _result("valid", True, "legacy_usable_markdown")
        return _result("invalid", False, "legacy_markdown_without_usable_rows")
    return _result("invalid", False, "unrecognized_status")


def _p8_upstream_is_usable(manifest: dict, files: dict[str, str]) -> bool:
    p6_markdown = _pick_existing_file(files.get("p6", ""), require_nonempty=True)
    if not p6_markdown:
        return False
    p6_status = _stage_status(manifest, "p6").strip().lower()
    return not p6_status or p6_status in STAGE_SUCCESS_STATUSES


def _current_p6_stock_count(files: dict[str, str]) -> int | None:
    p6_text = _read_checkpoint_text(files.get("p6", ""))
    if p6_text is None:
        return None
    return len(extract_p6_targets(p6_text))


def _assess_p8_from_manifest(
    manifest: dict,
    files: dict[str, str],
    *,
    markdown_path: str = "",
    stale: bool = False,
) -> dict[str, Any]:
    payload = _current_stage_payload(manifest, "p8")
    payload_files = payload.get("files", {}) if isinstance(payload, dict) else {}
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    stage_status = str(payload.get("status", ""))
    candidate_path = markdown_path or files.get("p8", "") or str(payload_files.get("md", ""))
    current_stock_count = _current_p6_stock_count(files)
    if current_stock_count == 0 and stage_status.strip().lower() != "skipped_no_stocks":
        current_stock_count = None
    return assess_p8_checkpoint(
        candidate_path,
        stage_status=stage_status,
        stage_metrics=metrics if isinstance(metrics, dict) else {},
        current_stock_count=current_stock_count,
        stale=stale,
        upstream_valid=_p8_upstream_is_usable(manifest, files),
    )


def _prepare_p8_assessment(manifest: dict, files: dict[str, str]) -> dict[str, Any]:
    original_p8 = files.get("p8", "")
    p8_stale_upstream = _stage_output_is_stale(
        manifest,
        files,
        "p8",
        ("p6",),
    )
    _drop_stale_downstream_files(files, _manifest_p6_input_hashes(manifest))
    assessment = _assess_p8_from_manifest(
        manifest,
        files,
        markdown_path=original_p8,
        stale=bool(p8_stale_upstream or (original_p8 and not files.get("p8"))),
    )
    if not assessment["reusable"]:
        files.pop("p8", None)
        if _stage_was_attempted(manifest, "p8", files) or original_p8:
            for downstream_key in ("war_report", "roi_md", "roi_json"):
                files.pop(downstream_key, None)
    else:
        for downstream_stage, downstream_key in (
            ("p9", "war_report"),
            ("roi", "roi_md"),
            ("roi", "roi_json"),
        ):
            if _stage_output_is_stale(
                manifest,
                files,
                downstream_stage,
                ("p8",),
                downstream_file_key=downstream_key,
            ):
                files.pop(downstream_key, None)
    return assessment


def _stage_was_attempted(manifest: dict, stage_name: str, files: dict[str, str]) -> bool:
    payload = _current_stage_payload(manifest, stage_name)
    payload_files = payload.get("files", {}) if isinstance(payload, dict) else {}
    file_key = {"p9": "war_report", "roi": "roi_md"}.get(stage_name, stage_name)
    return bool(str(payload.get("status") or "").strip() or payload_files or files.get(file_key))


def _iso_timestamp(value: Any) -> float:
    raw_value = str(value or "").strip()
    if not raw_value:
        return 0.0
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def _stage_output_timestamp_sources(
    manifest: dict,
    files: dict[str, str],
    stage_name: str,
    *,
    file_key: str | None = None,
) -> tuple[float, float, float]:
    payload = _current_stage_payload(manifest, stage_name)
    output_updated_at = _iso_timestamp(payload.get("output_updated_at"))

    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    roi_bookkeeping_present = isinstance(metrics, dict) and any(
        str(key).startswith("roi_") for key in metrics
    )
    legacy_updated_at = (
        0.0
        if roi_bookkeeping_present
        else _iso_timestamp(payload.get("updated_at"))
    )

    artifact_mtime = get_file_mtime(
        files,
        file_key or STAGE_PRIMARY_FILE_KEYS.get(stage_name, stage_name),
    )
    return output_updated_at, legacy_updated_at, artifact_mtime


def _stage_output_is_stale(
    manifest: dict,
    files: dict[str, str],
    stage_name: str,
    upstream_stages: tuple[str, ...],
    *,
    downstream_file_key: str | None = None,
) -> str:
    downstream_sources = _stage_output_timestamp_sources(
        manifest,
        files,
        stage_name,
        file_key=downstream_file_key,
    )
    for upstream_stage in upstream_stages:
        if not _stage_was_attempted(manifest, upstream_stage, files):
            continue
        upstream_sources = _stage_output_timestamp_sources(
            manifest,
            files,
            upstream_stage,
        )
        downstream_output, downstream_legacy, downstream_mtime = downstream_sources
        upstream_output, upstream_legacy, upstream_mtime = upstream_sources
        if downstream_output and upstream_output:
            is_stale = upstream_output > downstream_output
        elif downstream_mtime and upstream_mtime:
            is_stale = upstream_mtime > downstream_mtime
        elif downstream_legacy and upstream_legacy:
            is_stale = upstream_legacy > downstream_legacy
        else:
            downstream_timestamp = downstream_output or downstream_legacy or downstream_mtime
            upstream_timestamp = upstream_output or upstream_legacy or upstream_mtime
            is_stale = bool(
                downstream_timestamp
                and upstream_timestamp > downstream_timestamp
            )
        if is_stale:
            return upstream_stage
    return ""


def _valid_empty_invalidates_file(
    manifest: dict,
    files: dict[str, str],
    stage_name: str,
    downstream_file_key: str,
) -> bool:
    downstream_stage = {
        "war_report": "p9",
        "roi_md": "roi",
        "roi_json": "roi",
    }.get(downstream_file_key, downstream_file_key)
    return bool(
        _stage_output_is_stale(
            manifest,
            files,
            downstream_stage,
            (stage_name,),
            downstream_file_key=downstream_file_key,
        )
    )


def _prepare_stage_assessments(
    manifest: dict,
    files: dict[str, str],
) -> dict[str, dict[str, Any]]:
    p6_input_hashes = _manifest_p6_input_hashes(manifest)
    for stage_name in P6_HASHED_RESEARCH_STAGES:
        status = _stage_status(manifest, stage_name).strip().lower()
        if status in (
            VALID_EMPTY_STATUSES.get(stage_name, frozenset())
            | VALID_PASSTHROUGH_STATUSES.get(stage_name, frozenset())
        ):
            files.pop(stage_name, None)
    _drop_stale_downstream_files(files, p6_input_hashes)
    stage_names = ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7a", "p75", "p9", "roi")
    assessments = {
        stage_name: assess_stage_checkpoint(stage_name, manifest, files)
        for stage_name in stage_names
    }
    manifest_version = _nonnegative_int(manifest.get("manifest_version")) or 0
    for stage_name in P6_HASHED_RESEARCH_STAGES:
        attempted = _stage_was_attempted(manifest, stage_name, files)
        missing_required_hash = bool(
            manifest_version >= 3
            and attempted
            and not p6_input_hashes.get(stage_name)
        )
        if attempted and (
            missing_required_hash
            or _p6_input_hash_is_stale(files, p6_input_hashes, stage_name)
        ):
            assessments[stage_name] = _checkpoint_result(
                stage_name,
                _stage_status(manifest, stage_name).strip().lower(),
                False,
                "invalid",
                (
                    "missing_p6_input_hash"
                    if missing_required_hash
                    else "stale_p6_input_hash"
                ),
            )
    if assessments["p1"].get("state") == "valid_empty":
        assessments["p5"] = _checkpoint_result(
            "p5",
            _stage_status(manifest, "p5").strip().lower(),
            True,
            "valid_empty",
            "upstream_p1_valid_empty",
        )

    dependency_rules = (
        ("p5", ("p1",)),
        ("p6", ("p1", "p2", "p3", "p4", "p5")),
        ("p7a", ("p7",)),
        ("p9", ("p2", "p3", "p4", "p5", "p6", "p7", "p7a", "p75")),
        ("roi", ("p9",)),
    )
    for stage_name, upstream_stages in dependency_rules:
        assessment = assessments[stage_name]
        if not assessment.get("reusable"):
            continue
        if stage_name == "p5" and assessment.get("reason") == "upstream_p1_valid_empty":
            continue
        reusable_upstreams = tuple(
            upstream_stage
            for upstream_stage in upstream_stages
            if assessments.get(upstream_stage, {}).get("reusable")
        )
        stale_upstream = _stage_output_is_stale(
            manifest,
            files,
            stage_name,
            reusable_upstreams,
        )
        if stale_upstream:
            assessments[stage_name] = _checkpoint_result(
                stage_name,
                _stage_status(manifest, stage_name).strip().lower(),
                False,
                "invalid",
                f"stale_{stale_upstream}_input",
            )

    file_keys = {
        "p1": ("p1", "p1_md"),
        "p2": ("p2",),
        "p3": ("p3",),
        "p4": ("p4",),
        "p5": ("p5",),
        "p6": ("p6",),
        "p7": ("p7",),
        "p7a": ("p7a",),
        "p75": ("p75", "p75_raw"),
        "p9": ("war_report",),
        "roi": ("roi_md", "roi_json"),
    }
    for stage_name, assessment in assessments.items():
        reusable = bool(assessment.get("reusable"))
        state = str(assessment.get("state") or "")
        if reusable and state not in REUSABLE_WITHOUT_ARTIFACT_STATES:
            continue
        keys_to_remove = file_keys[stage_name]
        if reusable and state == "valid_passthrough" and stage_name == "p75":
            keys_to_remove = ("p75",)
        for file_key in keys_to_remove:
            files.pop(file_key, None)

    downstream_by_stage = {
        "p1": ("p5", "p6", "p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p2": ("p6", "p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p3": ("p6", "p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p4": ("p6", "p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p5": ("p6", "p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p6": ("p7", "p7a", "p75", "p75_raw", "p8", "war_report", "roi_md", "roi_json"),
        "p7": ("p7a", "war_report", "roi_md", "roi_json"),
        "p7a": ("war_report", "roi_md", "roi_json"),
        "p75": ("war_report", "roi_md", "roi_json"),
        "p9": ("roi_md", "roi_json"),
    }
    for stage_name, downstream_keys in downstream_by_stage.items():
        assessment = assessments[stage_name]
        if not _stage_was_attempted(manifest, stage_name, files):
            continue
        for file_key in downstream_keys:
            if not assessment.get("reusable") or (
                assessment.get("state") in REUSABLE_WITHOUT_ARTIFACT_STATES
                and _valid_empty_invalidates_file(
                    manifest,
                    files,
                    stage_name,
                    file_key,
                )
            ):
                files.pop(file_key, None)
    return assessments


def _has_terminal_success(
    manifest: dict,
    files: dict[str, str],
    stage_assessments: dict[str, dict[str, Any]] | None = None,
) -> bool:
    assessments = stage_assessments or {}
    p9_reusable = assessments.get("p9", {}).get("reusable", bool(files.get("war_report")))
    roi_reusable = assessments.get("roi", {}).get(
        "reusable",
        _stage_status(manifest, "roi") in STAGE_SUCCESS_STATUSES,
    )
    return bool(
        files.get("war_report")
        and p9_reusable
        and roi_reusable
        and _stage_status(manifest, "p9") in P9_DELIVERY_SUCCESS_STATUSES
        and _stage_status(manifest, "report") in STAGE_SUCCESS_STATUSES
        and _stage_status(manifest, "roi") in STAGE_SUCCESS_STATUSES
        and str(manifest.get("status", "")) in ROOT_SUCCESS_STATUSES
    )


def _needs_delivery_resume(manifest: dict, files: dict[str, str]) -> bool:
    if not files.get("war_report"):
        return False
    p9 = _current_stage_payload(manifest, "p9")
    status = str(p9.get("status", ""))
    metrics = p9.get("metrics", {})
    return status in DELIVERY_RECOVERABLE_STATUSES or (
        metrics.get("email_configured") is True and metrics.get("email_sent") is False
    )


def _needs_roi_resume(
    manifest: dict,
    files: dict[str, str],
    stage_assessments: dict[str, dict[str, Any]] | None = None,
) -> bool:
    return bool(
        files.get("war_report")
        and _stage_status(manifest, "p9") in P9_DELIVERY_SUCCESS_STATUSES
        and not _has_terminal_success(manifest, files, stage_assessments)
    )


def _missing_intel_resume_stages(
    manifest: dict,
    files: dict[str, str],
    stage_assessments: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    current_statuses = {stage: _stage_status(manifest, stage).strip().lower() for stage in INTEL_STAGES}
    has_resume_anchor = bool(
        any(files.get(key) for key in (*INTEL_STAGES, "p5"))
        or any(current_statuses.values())
        or _stage_status(manifest, "collect_intel")
    )
    if not has_resume_anchor:
        return []

    missing: list[str] = []
    for stage_name in INTEL_STAGES:
        if stage_assessments is not None:
            if stage_assessments.get(stage_name, {}).get("reusable"):
                continue
            missing.append(stage_name)
            continue
        if _pick_existing_file(files.get(stage_name, ""), require_nonempty=True):
            continue

        status = current_statuses[stage_name]
        if stage_name == "p2":
            retryable = (
                not status
                or status in {"pending", "running", "retrying"}
                or status.startswith("failed")
                or status.startswith("auth_failed_")
            )
        else:
            retryable = status not in {"skipped_no_results", "completed_no_results"}

        if retryable:
            missing.append(stage_name)
    return missing


def _needs_p6_resume(manifest: dict, files: dict[str, str]) -> bool:
    has_recoverable_upstream = any(
        _pick_existing_file(files.get(stage_name, ""), require_nonempty=True)
        for stage_name in ("p2", "p3", "p4", "p5")
    )
    if not has_recoverable_upstream:
        return False

    p6_payload = _current_stage_payload(manifest, "p6")
    p6_status = str(p6_payload.get("status", "")).strip().lower()
    market_mapping_status = _stage_status(manifest, "market_mapping").strip().lower()
    p6_was_attempted = bool(p6_status) or market_mapping_status in {
        "running",
        "retrying",
        "completed",
    } or market_mapping_status.startswith("failed")
    if not p6_was_attempted:
        return False

    current_markdown = _pick_existing_file(files.get("p6", ""), require_nonempty=True)
    return not (p6_status in STAGE_SUCCESS_STATUSES and current_markdown)


def _needs_p8_resume(
    manifest: dict,
    files: dict[str, str],
    assessment: dict[str, Any],
) -> bool:
    if assessment.get("reusable") or not _p8_upstream_is_usable(manifest, files):
        return False
    p8_payload = _current_stage_payload(manifest, "p8")
    p8_files = p8_payload.get("files", {}) if isinstance(p8_payload, dict) else {}
    p8_was_attempted = bool(str(p8_payload.get("status", "")).strip() or p8_files)
    downstream_started = bool(
        _stage_status(manifest, "report")
        or _stage_status(manifest, "p9")
        or files.get("war_report")
    )
    return p8_was_attempted or downstream_started


def _load_files_from_manifest(ckpt: dict, manifest: dict) -> None:
    manifest_run_ts = str(manifest.get("run_timestamp") or "").strip()

    def _store(
        stage_name: str,
        file_key: str,
        *preferred_keys: str,
        require_nonempty: bool = False,
    ) -> None:
        payload = _current_stage_payload(manifest, stage_name)
        files = payload.get("files", {})
        for preferred in preferred_keys:
            existing = _pick_existing_file(files.get(preferred, ""), require_nonempty=require_nonempty)
            artifact_run_ts = extract_timestamp_from_name(existing)
            if existing and not (
                manifest_run_ts
                and artifact_run_ts
                and artifact_run_ts != manifest_run_ts
            ):
                ckpt["files"][file_key] = existing
                return

    _store("p1", "p1", "json", require_nonempty=True)
    _store("p1", "p1_md", "md")
    _store("p2", "p2", "md", require_nonempty=True)
    _store("p3", "p3", "md", require_nonempty=True)
    _store("p4", "p4", "md", require_nonempty=True)
    _store("p5", "p5", "md", require_nonempty=True)
    _store("p6", "p6", "md", require_nonempty=True)
    _store("p7", "p7", "md", require_nonempty=True)
    _store("p7a", "p7a", "md", require_nonempty=True)
    _store("p75", "p75", "md", require_nonempty=True)
    _store("p75", "p75_raw", "raw", require_nonempty=True)
    _store("p8", "p8", "md", require_nonempty=True)
    _store("p9", "war_report", "md", require_nonempty=True)
    _store("roi", "roi_md", "md", require_nonempty=True)
    _store("roi", "roi_json", "json", require_nonempty=True)


def _merge_files_from_fallback(
    ckpt: dict,
    day_dir: Path,
    *,
    run_timestamp: str = "",
) -> None:
    candidates_by_key: dict[str, list[Path]] = {}
    timestamped_candidates: list[str] = []

    for file_key, patterns in FALLBACK_PATTERNS.items():
        candidates: list[Path] = []
        seen_paths: set[str] = set()
        for pattern in patterns:
            for match in sorted(day_dir.rglob(pattern), reverse=True):
                normalized = str(match)
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)
                candidates.append(match)
                timestamp = extract_timestamp_from_name(normalized)
                if timestamp:
                    timestamped_candidates.append(timestamp)
        candidates_by_key[file_key] = candidates

    target_run_ts = str(run_timestamp or "").strip()
    if not target_run_ts and timestamped_candidates:
        target_run_ts = max(timestamped_candidates)
        ckpt["run_timestamp"] = target_run_ts

    for file_key, candidates in candidates_by_key.items():
        if file_key in ckpt["files"]:
            continue
        matched_path = ""
        for match in candidates:
            candidate_run_ts = extract_timestamp_from_name(str(match))
            if target_run_ts and candidate_run_ts != target_run_ts:
                continue
            if not target_run_ts and candidate_run_ts:
                continue
            candidate = _pick_existing_file(
                str(match),
                require_nonempty=file_key in {"p8", "war_report"},
            )
            if candidate:
                matched_path = candidate
                break
        if matched_path:
            ckpt["files"][file_key] = matched_path


def _manifest_has_recovery_state(manifest: dict) -> bool:
    run_timestamp = str(manifest.get("run_timestamp") or "").strip()
    if (
        manifest.get("corrupt_manifest")
        or not run_timestamp
        or extract_timestamp_from_name(run_timestamp) != run_timestamp
    ):
        return False

    stages = manifest.get("stages", {})
    return bool(
        (isinstance(stages, dict) and stages)
        or str(manifest.get("status") or "").strip()
        or str(manifest.get("updated_at") or "").strip()
    )


def _downstream_prerequisites_reusable(
    manifest: dict,
    files: dict[str, str],
    stage_assessments: dict[str, dict[str, Any]],
    p8_assessment: dict[str, Any],
) -> bool:
    manifest_version = _nonnegative_int(manifest.get("manifest_version")) or 0
    if manifest_version < 3:
        return True
    return bool(
        files.get("p6")
        and stage_assessments.get("p6", {}).get("reusable")
        and files.get("p8")
        and p8_assessment.get("reusable")
    )


def _derive_stage(
    files: dict[str, str],
    manifest: dict,
    *,
    p8_assessment: dict[str, Any] | None = None,
    stage_assessments: dict[str, dict[str, Any]] | None = None,
) -> str:
    assessments = stage_assessments or _prepare_stage_assessments(manifest, files)
    assessment = p8_assessment or _prepare_p8_assessment(manifest, files)
    intel_resume_stages = _missing_intel_resume_stages(manifest, files, assessments)
    if intel_resume_stages == ["p2"]:
        return "p2_resume"
    if intel_resume_stages:
        return "intel_resume"
    if (
        not assessments.get("p5", {}).get("reusable")
        and _stage_was_attempted(manifest, "p5", files)
    ):
        return "p5_resume"
    if (
        not assessments.get("p4", {}).get("reusable")
        and _stage_was_attempted(manifest, "p4", files)
    ):
        return "p6_resume"
    if _needs_p6_resume(manifest, files):
        return "p6_resume"
    if any(
        not assessments.get(stage_name, {}).get("reusable")
        and _stage_was_attempted(manifest, stage_name, files)
        for stage_name in ("p7", "p7a", "p75")
    ):
        return "p6"
    research_valid_empty = any(
        assessments.get(stage_name, {}).get("state") == "valid_empty"
        and _stage_was_attempted(manifest, stage_name, files)
        for stage_name in ("p7", "p7a", "p75")
    )
    if files.get("p6") and research_valid_empty and not assessment.get("reusable"):
        return "p8_resume"
    if _needs_p8_resume(manifest, files, assessment):
        return "p8_resume"
    if _downstream_prerequisites_reusable(manifest, files, assessments, assessment):
        if _has_terminal_success(manifest, files, assessments):
            return "p9_done"
        if _needs_delivery_resume(manifest, files):
            return "delivery_resume"
        if _needs_roi_resume(manifest, files, assessments):
            return "roi_resume"
    if files.get("p8"):
        return "p9_resume"
    if files.get("p7") or files.get("p7a"):
        return "p8_resume"
    if files.get("p6"):
        return "p6"
    if files.get("p5"):
        return "p5"
    if files.get("p1"):
        return "p1"
    intel_settled = all(
        assessments.get(stage_name, {}).get("reusable")
        for stage_name in INTEL_STAGES
    )
    if intel_settled and any(
        _stage_was_attempted(manifest, stage_name, files)
        for stage_name in INTEL_STAGES
    ):
        return "p5"
    return "none"


def detect_checkpoint() -> dict:
    day_dir = Path(get_today_output_dir())
    ckpt = {
        "stage": "none",
        "files": {},
        "p6_input_sha256": {},
        "p6_metrics": {},
        "p8_status": "",
        "p8_metrics": {},
        "p8_assessment": {
            "state": "missing",
            "reusable": False,
            "reason": "missing_markdown",
        },
        "p9_metrics": {},
        "stage_assessments": {},
        "intel_resume_stages": [],
        "run_timestamp": "",
    }

    manifest = load_run_manifest()
    manifest_is_authoritative = _manifest_has_recovery_state(manifest)
    recovery_manifest = manifest if manifest_is_authoritative else {}
    if manifest_is_authoritative:
        ckpt["run_timestamp"] = manifest.get("run_timestamp", "")
        ckpt["p6_input_sha256"] = _manifest_p6_input_hashes(manifest)
        p6_metrics = _current_stage_payload(manifest, "p6").get("metrics", {})
        ckpt["p6_metrics"] = dict(p6_metrics) if isinstance(p6_metrics, dict) else {}
        p8_payload = _current_stage_payload(manifest, "p8")
        ckpt["p8_status"] = str(p8_payload.get("status", ""))
        p8_metrics = p8_payload.get("metrics", {})
        ckpt["p8_metrics"] = dict(p8_metrics) if isinstance(p8_metrics, dict) else {}
        p9_metrics = _current_stage_payload(manifest, "p9").get("metrics", {})
        ckpt["p9_metrics"] = dict(p9_metrics) if isinstance(p9_metrics, dict) else {}
        _load_files_from_manifest(ckpt, manifest)
        ckpt["stage_assessments"] = _prepare_stage_assessments(manifest, ckpt["files"])
        ckpt["p8_assessment"] = _prepare_p8_assessment(manifest, ckpt["files"])
        ckpt["intel_resume_stages"] = _missing_intel_resume_stages(
            manifest,
            ckpt["files"],
            ckpt["stage_assessments"],
        )
        ckpt["stage"] = _derive_stage(
            ckpt["files"],
            manifest,
            p8_assessment=ckpt["p8_assessment"],
            stage_assessments=ckpt["stage_assessments"],
        )
        if ckpt["stage"] == "p9_done":
            return ckpt

    _merge_files_from_fallback(
        ckpt,
        day_dir,
        run_timestamp=ckpt["run_timestamp"] if manifest_is_authoritative else "",
    )
    ckpt["stage_assessments"] = _prepare_stage_assessments(
        recovery_manifest,
        ckpt["files"],
    )
    ckpt["p8_assessment"] = _prepare_p8_assessment(recovery_manifest, ckpt["files"])
    ckpt["intel_resume_stages"] = _missing_intel_resume_stages(
        recovery_manifest,
        ckpt["files"],
        ckpt["stage_assessments"],
    )
    ckpt["stage"] = _derive_stage(
        ckpt["files"],
        recovery_manifest,
        p8_assessment=ckpt["p8_assessment"],
        stage_assessments=ckpt["stage_assessments"],
    )

    if not ckpt["run_timestamp"]:
        for candidate in ckpt["files"].values():
            ckpt["run_timestamp"] = extract_timestamp_from_name(candidate)
            if ckpt["run_timestamp"]:
                break

    return ckpt
