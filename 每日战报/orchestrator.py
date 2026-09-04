# -*- coding: utf-8 -*-
"""Main pipeline orchestrator for the daily report workflow."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from artifact_store import (
    UTF8_FILE_ENCODING,
    build_runtime_context,
    configure_utf8_environment,
    get_error_log_path,
    get_pipeline_log_path,
    get_preflight_summary,
    load_run_manifest,
    record_run_state,
    reset_run_manifest,
    set_run_timestamp,
    summarize_stage_degradations,
    update_manifest_context,
    update_run_manifest,
    write_json_artifact,
    write_text_artifact,
)
from checkpoint import (
    P6_HASHED_RESEARCH_STAGES,
    VALID_EMPTY_STATUSES,
    assess_p8_checkpoint,
    compute_file_sha256,
    detect_checkpoint,
    is_older_than_any,
    p6_input_hash_matches,
)
from contribution_metrics import build_layer_roi_report, render_layer_roi_markdown
from pipeline_runtime import StageSpec, StageTimeoutError, run_stage_registry
from run_config import get_run_config, get_schedule_decision
from settings import (
    EMAIL_ADDRESS,
    EMAIL_PASSWORD,
    OPUS_STAGE_TIMEOUT_SECONDS,
    RECEIVER_EMAIL,
)

from p1_rss import collect_from_rss
from p1_web import collect_from_web
from p2_x_intel import collect_from_x
from p3_agent import collect_p3_parallel_only, render_p3_output, run_jinnang_1
from p4_vanguard import fetch_vanguard_prices
from audit_engine import (
    P7A_STRUCTURED_DEGRADED_MARKER,
    gemini_pro_audit,
    p7a_deep_research_audit,
)
from p6_mapper import format_evidence, generate_report
from p7_deep_research import (
    build_p7_evidence_boundary,
    build_research_plan,
    normalize_p7_evidence_provenance,
    run_deep_research,
)
from p8_technical import (
    extract_stocks_from_report,
    extract_stock_elasticity,
    extract_stock_tracks,
    p8_search_stock_prices,
)
from p9_reporter import p9_risk_analysis, send_report_email
from search_engine import gemini_search
from semantic_output import has_meaningful_semantic_text, is_no_evidence_text
from tdx_stock_universe import IDENTITY_VALIDATION_VERSION, normalize_rejected_stock_names

CONTROL_STAGE_START = {
    "none": "collect_intel",
    "p1": "collect_intel",
    "intel_resume": "collect_intel",
    "p2_resume": "recover_p2",
    "p5": "audit_evidence",
    "p5_resume": "audit_evidence",
    "p6_resume": "market_mapping",
    "p6": "market_mapping",
    "p8_resume": "market_mapping",
    "p9_resume": "market_mapping",
    "delivery_resume": "delivery_resume",
    "roi_resume": "roi",
    "manual_confirmation_required": None,
}

CONTROL_STAGE_TIMEOUTS = {
    "collect_intel": 900,
    "recover_p2": 480,
    "audit_evidence": 900,
    "market_mapping": OPUS_STAGE_TIMEOUT_SECONDS,
    "research": 900,
    "technical": 300,
    "report": OPUS_STAGE_TIMEOUT_SECONDS,
    "delivery_resume": 600,
    "roi": 180,
}


def _configure_stdio() -> None:
    configure_utf8_environment()


@contextlib.contextmanager
def log_heartbeat(task_name: str, interval: int = 15):
    stop_event = threading.Event()

    def heartbeat() -> None:
        started_at = time.time()
        while not stop_event.wait(interval):
            elapsed = int(time.time() - started_at)
            logging.info("[heartbeat] %s still running (%ss)", task_name, elapsed)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1.0)


def _recover_p2_p3_from_checkpoint(ckpt: dict) -> tuple[str, str]:
    return (
        _read_text_checkpoint(ckpt.get("files", {}).get("p2", ""), "P2 X intel"),
        _read_text_checkpoint(ckpt.get("files", {}).get("p3", ""), "P3 agent intel"),
    )


def _read_text_checkpoint(path: str, label: str) -> str:
    if not path:
        return ""
    if not str(path).lower().endswith(".md"):
        logging.warning("%s checkpoint is not Markdown; ignoring it: %s", label, path)
        return ""
    try:
        with open(path, "r", encoding=UTF8_FILE_ENCODING) as f:
            data = f.read()
        logging.info("recovered %s", label)
        return data
    except FileNotFoundError:
        logging.warning("%s file missing during resume", label)
    except Exception as exc:
        logging.warning("%s resume failed: %s", label, exc)
    return ""


def _read_json_checkpoint(path: str, label: str):
    if not path:
        return None
    try:
        import json

        with open(path, "r", encoding=UTF8_FILE_ENCODING) as f:
            data = json.load(f)
        logging.info("recovered %s", label)
        return data
    except FileNotFoundError:
        logging.warning("%s file missing during resume", label)
    except Exception as exc:
        logging.warning("%s resume failed: %s", label, exc)
    return None


def _recover_p7_evidence_provenance(ckpt: dict) -> dict | None:
    """Recover the optional P7 provenance artifact without invalidating legacy runs."""
    payload = _read_json_checkpoint(
        ckpt.get("files", {}).get("p7_evidence", ""),
        "P7 evidence provenance",
    )
    if not isinstance(payload, dict):
        return None
    aggregate = payload.get("aggregate", payload)
    provenance = _normalize_p7_evidence_with_topics(aggregate)
    if provenance and isinstance(payload.get("topics"), list):
        provenance["topics"] = payload["topics"]
    return provenance or None


def _normalize_p7_evidence_with_topics(value: object) -> dict | None:
    """Normalize count/tier fields while retaining per-topic artifact records."""
    provenance = normalize_p7_evidence_provenance(value)
    if not provenance:
        return None
    if isinstance(value, dict) and isinstance(value.get("topics"), list):
        provenance["topics"] = value["topics"]
    return provenance


def _write_p7_evidence_artifact(context: dict[str, Any], provenance: dict | None) -> str:
    """Persist aggregate and per-topic P7 provenance independently of Markdown output."""
    if not provenance:
        return ""
    payload = {
        "aggregate": provenance,
        "topics": provenance.get("topics", []),
    }
    try:
        path = write_json_artifact("P7_证据元数据", payload)
    except Exception as exc:
        logging.warning("[P7] failed to write evidence provenance: %s", exc)
        return ""
    context["ckpt"].setdefault("files", {})["p7_evidence"] = path
    return path


def _mark_p7a_unavailable_passthrough(raw_research: str, reason: str) -> str:
    """Keep failed P7a passthroughs from being treated as audited P7 evidence."""
    if P7A_STRUCTURED_DEGRADED_MARKER in raw_research:
        return raw_research
    return (
        f"> {P7A_STRUCTURED_DEGRADED_MARKER}{reason}；未获得可用 P7a Markdown 审计正文，"
        "以下为未经 P7a 审计的原始 P7 透传。"
        "P9 不得将 P7/P7a 作为独立硬证据或据此提高置信度。\n\n"
        f"{raw_research}"
    )


def _read_fresh_text_checkpoint(
    ckpt: dict,
    file_key: str,
    label: str,
    *,
    upstream_keys: tuple[str, ...],
) -> str:
    files = ckpt.get("files", {})
    hashes = ckpt.get("p6_input_sha256", {})
    p6_hashes = hashes if isinstance(hashes, dict) else {}
    if file_key in P6_HASHED_RESEARCH_STAGES and not p6_input_hash_matches(files, p6_hashes, file_key):
        logging.warning("%s checkpoint P6 input hash is missing or stale; ignoring it", label)
        return ""
    remaining_upstreams = tuple(key for key in upstream_keys if key != "p6")
    if is_older_than_any(files, file_key, remaining_upstreams):
        logging.warning("%s checkpoint is older than upstream input; ignoring it", label)
        return ""
    return _read_text_checkpoint(ckpt.get("files", {}).get(file_key, ""), label)


def _recover_p75_checkpoint(ckpt: dict) -> tuple[str, str]:
    assessments = ckpt.get("stage_assessments", {})
    p75_assessment = assessments.get("p75", {}) if isinstance(assessments, dict) else {}
    if (
        isinstance(p75_assessment, dict)
        and p75_assessment.get("state") == "valid_empty"
    ):
        return "", ""
    if (
        isinstance(p75_assessment, dict)
        and p75_assessment.get("state") == "valid_passthrough"
    ):
        raw_text = _read_text_checkpoint(
            ckpt.get("files", {}).get("p75_raw", ""),
            "P7.5 raw passthrough",
        )
        return raw_text, raw_text
    audited_text = _read_fresh_text_checkpoint(
        ckpt,
        "p75",
        "P7.5 supplement",
        upstream_keys=("p6",),
    )
    return audited_text, ""


def _recover_p5_checkpoint(ckpt: dict, label: str = "P5 audit") -> str:
    assessments = ckpt.get("stage_assessments", {})
    p5_assessment = assessments.get("p5", {}) if isinstance(assessments, dict) else {}
    if isinstance(p5_assessment, dict) and p5_assessment.get("state") == "valid_empty":
        return ""
    return _read_text_checkpoint(
        ckpt.get("files", {}).get("p5", ""),
        label,
    )


def _context_p6_input_sha256(context: dict[str, Any]) -> str:
    return compute_file_sha256(context.get("ckpt", {}).get("files", {}).get("p6", ""))


def _research_stage_needs_p6_hash_refresh(
    context: dict[str, Any],
    stage_name: str,
    current_p6_hash: str,
) -> bool:
    """Return whether an old optional-research state belongs to another P6 output."""
    if not current_p6_hash:
        return False

    ckpt = context.get("ckpt", {})
    if not isinstance(ckpt, dict):
        return False
    saved_hashes = ckpt.get("p6_input_sha256", {})
    if not isinstance(saved_hashes, dict):
        saved_hashes = {}
    saved_hash = str(saved_hashes.get(stage_name) or "").strip().lower()

    assessments = ckpt.get("stage_assessments", {})
    if not isinstance(assessments, dict):
        assessments = {}
    assessment = assessments.get(stage_name, {})
    if not isinstance(assessment, dict):
        assessment = {}

    files = ckpt.get("files", {})
    if not isinstance(files, dict):
        files = {}
    recorded = bool(
        saved_hash
        or str(assessment.get("status") or "").strip()
        or files.get(stage_name)
        or (stage_name == "p75" and files.get("p75_raw"))
    )
    return recorded and saved_hash != current_p6_hash.lower()


def _record_research_stage_not_triggered(
    stage_name: str,
    p6_hash_metrics: dict[str, str],
) -> None:
    update_run_manifest(
        stage_name,
        status="skipped_not_triggered",
        metrics={
            "data_state": "valid_empty",
            "skip_reason": "research_plan_not_triggered",
            **p6_hash_metrics,
        },
    )


def _refresh_not_triggered_plan_stages(
    context: dict[str, Any],
    *,
    need_run_p7: bool,
    need_run_p75: bool,
    search_queries: list[str],
    p6_input_sha256: str,
    p6_hash_metrics: dict[str, str],
) -> None:
    if (
        not need_run_p7
        and not context.get("p7_deep", "").strip()
        and _research_stage_needs_p6_hash_refresh(context, "p7", p6_input_sha256)
    ):
        _record_research_stage_not_triggered("p7", p6_hash_metrics)

    if (
        (not need_run_p75 or not search_queries)
        and not context.get("p75_supplement", "").strip()
        and not context.get("p75_raw_text", "").strip()
        and _research_stage_needs_p6_hash_refresh(context, "p75", p6_input_sha256)
    ):
        _record_research_stage_not_triggered("p75", p6_hash_metrics)


def _load_p6_identity_validation(metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    if metrics is None:
        manifest = load_run_manifest()
        metrics = manifest.get("stages", {}).get("p6", {}).get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    rejected_names = normalize_rejected_stock_names(metrics.get("rejected_target_names"))
    try:
        rejected_targets = int(metrics.get("rejected_targets") or 0)
    except (TypeError, ValueError):
        rejected_targets = 0
    try:
        validation_version = int(metrics.get("identity_validation_version") or 0)
    except (TypeError, ValueError):
        validation_version = 0
    if validation_version >= IDENTITY_VALIDATION_VERSION:
        complete = True
        reason = ""
    elif validation_version == 0 and rejected_targets > 0 and not rejected_names:
        complete = False
        reason = "legacy_rejections_missing_names"
    elif validation_version == 0:
        complete = False
        reason = "identity_validation_unversioned"
    else:
        complete = False
        reason = "identity_validation_outdated"
    return {
        "rejected_target_names": rejected_names,
        "rejected_targets": rejected_targets,
        "identity_validation_version": validation_version,
        "complete": complete,
        "reason": reason,
    }


def _parse_flags(argv: list[str]) -> tuple[bool, bool]:
    force_restart = "--force" in argv or "--force-restart" in argv
    force_run = "--force" in argv or "--force-run" in argv or force_restart
    return force_run, force_restart


def _resolve_start_stage(resume_from: str) -> str | None:
    return CONTROL_STAGE_START.get(resume_from, "collect_intel")


def _build_context(now: datetime, run_ts: str, ckpt: dict, resume_from: str) -> dict[str, Any]:
    context = {
        "now": now,
        "run_ts": run_ts,
        "ckpt": ckpt,
        "resume_from": resume_from,
        "backtrack": "",
        "p5_evidence": "",
        "p1_results": [],
        "p1_stats": {},
        "evidence_raw": "",
        "p2_data": "",
        "p3_data": "",
        "p3_stats": {},
        "p4_data": "",
        "p6_mapping": "",
        "rejected_target_names": [],
        "p7_deep": "",
        "p7_evidence_provenance": None,
        "p7a_audited": "",
        "p75_supplement": "",
        "p75_raw_text": "",
        "price_data": "",
        "research_plan": {
            "goal": "",
            "triggers": [],
            "deep_topics": [],
            "supplement_queries": [],
            "should_run_p7": False,
            "should_run_p75": False,
            "max_rounds": 0,
            "round_backtracks": [],
            "skipped_recent_topics": [],
        },
        "p7_plan_path": "",
        "p1_duration": 0.0,
        "p4_duration": 0.0,
        "p75_duration": 0.0,
        "search_queries": [],
        "final_report": "",
        "report_ts": "",
        "intel_resume_stages": list(ckpt.get("intel_resume_stages", [])),
    }

    if resume_from in {"delivery_resume", "roi_resume"}:
        context["final_report"] = _read_text_checkpoint(
            ckpt.get("files", {}).get("war_report", ""),
            "saved P9 report",
        )
        context["report_ts"] = run_ts

    if resume_from in {"p2_resume", "intel_resume", "p5_resume"}:
        context["backtrack"] = get_run_config(now)
        p1_path = ckpt.get("files", {}).get("p1", "")
        if p1_path:
            recovered_results = _read_json_checkpoint(p1_path, "P1 raw results")
            if isinstance(recovered_results, list):
                context["p1_results"] = recovered_results
        context["evidence_raw"] = _read_text_checkpoint(
            ckpt.get("files", {}).get("p1_md", ""),
            "P1 markdown",
        )
        if not context["evidence_raw"] and context["p1_results"]:
            context["evidence_raw"] = format_evidence(context["p1_results"])
        context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)

    if resume_from in {"delivery_resume", "roi_resume"}:
        context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)
        context["p4_data"] = _read_text_checkpoint(ckpt.get("files", {}).get("p4", ""), "P4 market snapshot")
        context["p6_mapping"] = _read_text_checkpoint(ckpt.get("files", {}).get("p6", ""), "P6 mapping")
        context["p7_deep"] = _read_fresh_text_checkpoint(
            ckpt,
            "p7",
            "P7 deep research",
            upstream_keys=("p6",),
        )
        if context["p7_deep"]:
            context["p7_evidence_provenance"] = _recover_p7_evidence_provenance(ckpt)
        context["p75_supplement"], context["p75_raw_text"] = (
            _recover_p75_checkpoint(ckpt)
        )
        if context["p7_deep"]:
            context["p7a_audited"] = _read_fresh_text_checkpoint(
                ckpt,
                "p7a",
                "P7a audited research",
                upstream_keys=("p6", "p7"),
            )

    return context


def _build_p1_stats(
    rss_stats: dict[str, Any],
    web_stats: dict[str, Any],
    p1_failed_urls: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "rss_success_sources": len(rss_stats.get("success", [])),
        "web_success_sources": len(web_stats.get("success", [])),
        "rss_failed_sources": len(rss_stats.get("failed", [])),
        "web_failed_sources": len(web_stats.get("failed", [])),
        "enrich_ok": int(rss_stats.get("enrich_ok", 0)) + int(web_stats.get("enrich_ok", 0)),
        "enrich_fail": int(rss_stats.get("enrich_fail", 0)) + int(web_stats.get("enrich_fail", 0)),
        "failed_urls": len(p1_failed_urls),
        "web_time_recent": int(web_stats.get("time_recent", 0)),
        "web_time_unknown": int(web_stats.get("time_unknown", 0)),
        "web_time_stale": int(web_stats.get("time_stale", 0)),
        "web_time_parse_failed": int(web_stats.get("time_parse_failed", 0)),
    }


def _filter_usable_p1_results(
    p1_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    usable_results = [
        item
        for item in p1_results
        if isinstance(item, dict)
        and has_meaningful_semantic_text(str(item.get("text") or ""))
    ]
    return usable_results, len(p1_results) - len(usable_results)


def _persist_p1_artifacts(
    p1_results: list[dict[str, Any]],
    evidence_raw: str,
    p1_failed_urls: list[dict[str, Any]],
    p1_duration: float,
    p1_stats: dict[str, Any],
) -> None:
    usable_results, rejected_records = _filter_usable_p1_results(p1_results)
    usable_evidence = str(evidence_raw or "").strip()
    if not has_meaningful_semantic_text(usable_evidence):
        usable_evidence = format_evidence(usable_results)
    source_failures = int(p1_stats.get("source_failures", 0) or 0)
    metrics = {
        "records": len(usable_results),
        "raw_records": len(p1_results),
        "semantic_rejected_records": rejected_records,
        "failed_urls": len(p1_failed_urls),
        "evidence_chars": len(usable_evidence),
        "duration_seconds": round(p1_duration, 1),
        **p1_stats,
    }
    if source_failures:
        metrics["degrade_reason"] = "; ".join(
            str(item) for item in p1_stats.get("source_failure_details", []) if item
        )[:2000]
    if not usable_results or not usable_evidence:
        status = "failed_no_results" if source_failures else "skipped_no_results"
        update_run_manifest("p1", status=status, metrics=metrics)
        return

    p1_json_path = write_json_artifact("P1_RSS原始数据", usable_results)
    p1_md_path = write_text_artifact("P1_RSS原始数据", usable_evidence)
    update_run_manifest(
        "p1",
        status="completed_structured_degraded" if source_failures else "completed",
        files={"json": p1_json_path, "md": p1_md_path},
        metrics=metrics,
    )


def _fill_missing_p2_p3_from_checkpoint(context: dict[str, Any], ckpt: dict) -> None:
    if context.get("p2_data") and context.get("p3_data"):
        return
    recovered_p2, recovered_p3 = _recover_p2_p3_from_checkpoint(ckpt)
    if not context.get("p2_data"):
        context["p2_data"] = recovered_p2
    if not context.get("p3_data"):
        context["p3_data"] = recovered_p3


def _persist_p3_artifacts(p3_data: str, p3_stats: dict[str, Any]) -> None:
    if not isinstance(p3_stats, dict):
        p3_stats = {}
    source_status = str(p3_stats.get("status") or "").strip().lower()
    metrics = {
        "content_chars": len(p3_data),
        "duration_seconds": p3_stats.get("duration_seconds", 0),
        "jinnang_1_count": p3_stats.get("jinnang_1_count", 0),
        "jinnang_2_count": p3_stats.get("jinnang_2_count", 0),
        "jinnang_3_count": p3_stats.get("jinnang_3_count", 0),
        "structured_items": p3_stats.get("structured_items", 0),
        "query_failures": p3_stats.get("query_failures", 0),
        "failure_errors": p3_stats.get("failure_errors", []),
    }
    if p3_stats.get("error"):
        metrics["error"] = str(p3_stats["error"])
    if p3_stats.get("failure_errors"):
        metrics["degrade_reason"] = "; ".join(
            str(item) for item in p3_stats.get("failure_errors", []) if item
        )[:2000]
    structured_items = p3_stats.get("structured_items")
    semantically_empty = not p3_data.strip() or is_no_evidence_text(p3_data)
    structurally_empty = (
        structured_items is not None
        and not isinstance(structured_items, bool)
        and str(structured_items).strip().lstrip("+-").isdigit()
        and int(structured_items) <= 0
    )
    if semantically_empty or structurally_empty:
        if source_status == "empty":
            status = "skipped_no_results"
        elif source_status.startswith("skipped") or source_status.startswith("failed"):
            status = source_status
        elif structurally_empty:
            status = "failed_incomplete_output"
        else:
            status = "failed_empty_output"
        update_run_manifest("p3", status=status, metrics=metrics)
        return

    p3_path = write_text_artifact("P3_机动情报", p3_data)
    p3_files = {"md": p3_path}
    raw_results = p3_stats.get("raw_results") if isinstance(p3_stats, dict) else {}
    if isinstance(raw_results, dict) and raw_results:
        _, p3_structured_items = render_p3_output(raw_results)
        p3_json_path = write_json_artifact(
            "P3_机动情报",
            {
                "generated_at": datetime.now().isoformat(),
                "items": p3_structured_items,
                "raw_results": raw_results,
            },
        )
        p3_files["json"] = p3_json_path
    update_run_manifest(
        "p3",
        status=(
            "completed_structured_degraded"
            if source_status.startswith(("partial", "failed"))
            else "completed"
        ),
        files=p3_files,
        metrics=metrics,
    )


def _stage_collect_intel(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]
    now = context["now"]

    if resume_from not in {"none", "p1", "intel_resume"}:
        return

    context["backtrack"] = get_run_config(now)
    if resume_from == "p1":
        recovered_results = _read_json_checkpoint(ckpt["files"].get("p1", ""), "P1 raw results")
        if isinstance(recovered_results, list):
            context["p1_results"] = recovered_results
        context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)
        evidence_raw = _read_text_checkpoint(ckpt["files"].get("p1_md", ""), "P1 markdown")
        if not evidence_raw and context["p1_results"]:
            evidence_raw = format_evidence(context["p1_results"])
        context["evidence_raw"] = evidence_raw
        return

    resume_stages = (
        {"p1", "p2", "p3"}
        if resume_from == "none"
        else set(context.get("intel_resume_stages") or ckpt.get("intel_resume_stages", []))
    )
    run_p1 = "p1" in resume_stages
    run_p2 = "p2" in resume_stages
    run_p3 = "p3" in resume_stages

    p1_results: list[dict[str, Any]] = [] if run_p1 else list(context.get("p1_results") or [])
    rss_stats: dict[str, Any] = {}
    web_stats: dict[str, Any] = {}
    p2_data = "" if run_p2 else str(context.get("p2_data") or "")
    p3_data = "" if run_p3 else str(context.get("p3_data") or "")
    p3_stats: dict[str, Any] = {} if run_p3 else dict(context.get("p3_stats") or {})
    p1_failed_urls: list[dict[str, Any]] = []
    p1_source_failure_details: list[str] = []
    collect_started = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rss_future = pool.submit(collect_from_rss, context["backtrack"]) if run_p1 else None
        web_future = pool.submit(collect_from_web, context["backtrack"]) if run_p1 else None
        x_future = pool.submit(collect_from_x, context["backtrack"]) if run_p2 else None
        p3_future = pool.submit(collect_p3_parallel_only, context["backtrack"]) if run_p3 else None

        if rss_future is not None:
            try:
                rss_results, rss_stats = rss_future.result()
                p1_results.extend(rss_results)
                p1_failed_urls.extend(rss_stats.get("failed_urls", []))
            except Exception as exc:
                logging.warning("RSS collection failed: %s", exc)
                p1_source_failure_details.append(f"rss: {exc}")

        if web_future is not None:
            try:
                web_results, web_stats = web_future.result()
                p1_results.extend(web_results)
            except Exception as exc:
                logging.warning("web collection failed: %s", exc)
                p1_source_failure_details.append(f"web: {exc}")

        if x_future is not None:
            try:
                p2_data, _ = x_future.result()
            except Exception as exc:
                logging.warning("X collection failed: %s", exc)
                update_run_manifest(
                    "p2",
                    status="failed_exception",
                    metrics={"error": str(exc), "collection_mode": "parallel"},
                )

        if p3_future is not None:
            try:
                p3_data, p3_stats = p3_future.result()
            except Exception as exc:
                logging.warning("P3 collection failed: %s", exc)
                p3_stats = {
                    "status": "failed_exception",
                    "error": str(exc),
                    "failure_errors": [str(exc)],
                    "query_failures": 1,
                }

    raw_p1_records = len(p1_results)
    semantic_rejected_records = 0
    if run_p1:
        p1_results, semantic_rejected_records = _filter_usable_p1_results(p1_results)

    evidence_raw = (
        format_evidence(p1_results) if run_p1 and p1_results else context.get("evidence_raw", "")
    )
    if run_p1:
        p1_duration = time.time() - collect_started
        p1_stats = _build_p1_stats(rss_stats, web_stats, p1_failed_urls)
        for source_label, source_stats in (("rss", rss_stats), ("web", web_stats)):
            for failure in source_stats.get("failed", []) if isinstance(source_stats, dict) else []:
                if isinstance(failure, (list, tuple)) and len(failure) >= 2:
                    p1_source_failure_details.append(f"{source_label}/{failure[0]}: {failure[1]}")
                else:
                    p1_source_failure_details.append(f"{source_label}: {failure}")
        p1_stats["source_failures"] = len(p1_source_failure_details)
        p1_stats["source_failure_details"] = p1_source_failure_details
        p1_stats["raw_records"] = raw_p1_records
        p1_stats["semantic_rejected_records"] = semantic_rejected_records
    else:
        p1_duration = float(context.get("p1_duration", 0.0) or 0.0)
        p1_stats = dict(context.get("p1_stats") or {})

    context.update(
        {
            "p1_results": p1_results,
            "p1_stats": p1_stats,
            "p2_data": p2_data,
            "p3_data": p3_data,
            "p3_stats": p3_stats,
            "p1_duration": p1_duration,
            "evidence_raw": evidence_raw,
        }
    )

    if run_p1:
        _persist_p1_artifacts(p1_results, evidence_raw, p1_failed_urls, p1_duration, p1_stats)
    if run_p3:
        _persist_p3_artifacts(p3_data, p3_stats)

    if run_p1 and run_p3 and p1_failed_urls:
        try:
            jinnang_1_results = run_jinnang_1(p1_failed_urls)
            if jinnang_1_results:
                raw_results = p3_stats.get("raw_results")
                if not isinstance(raw_results, dict):
                    raw_results = {}
                raw_results["jinnang_1"] = jinnang_1_results
                p3_stats["raw_results"] = raw_results
                p3_stats["jinnang_1_count"] = len(jinnang_1_results)
                p3_data, p3_structured_items = render_p3_output(raw_results)
                p3_stats["structured_items"] = len(p3_structured_items)
                p3_stats["content_length"] = len(p3_data)
                context["p3_data"] = p3_data
                context["p3_stats"] = p3_stats
                _persist_p3_artifacts(p3_data, p3_stats)
        except Exception as exc:
            logging.warning("jinnang_1 recovery failed: %s", exc)


def _run_p5_audit(context: dict[str, Any]) -> None:
    with log_heartbeat("P5 audit"):
        try:
            context["p5_evidence"] = gemini_pro_audit(
                context.get("evidence_raw", ""),
                audit_type="p5",
                backtrack=context["backtrack"] or get_run_config(context["now"]),
            )
        except Exception as exc:
            logging.error("[P5] audit raised unexpectedly; falling back to raw P1 evidence: %s", exc)
            update_run_manifest(
                "p5",
                status="failed_exception",
                metrics={
                    "input_chars": len(context.get("evidence_raw", "") or ""),
                    "fallback_reason": str(exc),
                },
            )
            context["p5_evidence"] = ""

    p5_status = str(
        load_run_manifest().get("stages", {}).get("p5", {}).get("status", "")
    ).strip().lower()
    p5_valid_empty = p5_status in VALID_EMPTY_STATUSES.get("p5", frozenset())
    if (
        not context["p5_evidence"].strip()
        and context.get("evidence_raw", "").strip()
        and not p5_valid_empty
    ):
        fallback_text = (
            "## P5 Pro 审计失败兜底\n\n"
            "> Gemini Pro 审计层未返回有效结果。以下内容是未经 P5 Pro 清洗的 P1 原始情报，"
            "仅用于保持后续 P6/P9 链路不断流；下游必须按未审计数据降权处理。\n\n"
            + context["evidence_raw"].strip()
        )
        context["p5_evidence"] = fallback_text
        fallback_path = write_text_artifact("P5_审计_RAW_P1_FALLBACK", fallback_text)
        update_run_manifest(
            "p5",
            status="failed_empty_output_raw_p1_fallback",
            files={"md": fallback_path},
            metrics={
                "fallback": "raw_p1_unreviewed",
                "fallback_chars": len(fallback_text),
                "data_state": "invalid",
            },
        )
        context["p5_audit_degraded"] = True


def _stage_recover_p2(context: dict[str, Any]) -> None:
    if context.get("resume_from") != "p2_resume" or context.get("p2_resume_attempted"):
        return

    context["p2_resume_attempted"] = True
    context["backtrack"] = context.get("backtrack") or get_run_config(context["now"])
    logging.info("[P2] checkpoint is incomplete without an artifact; rerunning P2 only")
    try:
        with log_heartbeat("P2 targeted resume"):
            context["p2_data"], _ = collect_from_x(context["backtrack"])
    except Exception as exc:
        context["p2_data"] = ""
        logging.error("[P2] targeted resume failed; continuing with an explicit degraded state: %s", exc)
        update_run_manifest(
            "p2",
            status="failed_exception",
            metrics={"fallback_reason": str(exc), "targeted_resume": True},
        )


def _stage_audit_evidence(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]

    if resume_from == "p2_resume" and not context.get("p2_resume_attempted"):
        _stage_recover_p2(context)

    intel_resume_stages = set(context.get("intel_resume_stages") or [])
    should_run_p5 = (
        resume_from in {"none", "p1", "p5_resume"}
        or (resume_from == "p2_resume" and not ckpt.get("files", {}).get("p5"))
        or (
            resume_from == "intel_resume"
            and ("p1" in intel_resume_stages or not ckpt.get("files", {}).get("p5"))
        )
    )
    if should_run_p5:
        _run_p5_audit(context)
        _fill_missing_p2_p3_from_checkpoint(context, ckpt)
        return

    context["p5_evidence"] = _recover_p5_checkpoint(ckpt, "P5 audit result")
    _fill_missing_p2_p3_from_checkpoint(context, ckpt)


def _stage_market_mapping(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]

    identity_validation = _load_p6_identity_validation(ckpt.get("p6_metrics", {}))
    if resume_from in {"p6", "p8_resume", "p9_resume"} and ckpt["files"].get("p6"):
        if is_older_than_any(ckpt.get("files", {}), "p6", ("p5", "p4", "p3", "p2")):
            logging.warning("[P6] checkpoint is older than upstream evidence; rerunning mapping")
        elif not identity_validation["complete"]:
            logging.warning(
                "[P6] checkpoint identity validation incomplete (%s); rerunning P6",
                identity_validation["reason"],
            )
        else:
            context["rejected_target_names"] = identity_validation["rejected_target_names"]
            context["p6_mapping"] = _read_text_checkpoint(ckpt["files"].get("p6", ""), "P6 mapping")
            context["p5_evidence"] = context["p5_evidence"] or _recover_p5_checkpoint(ckpt)
            context["p4_data"] = _read_text_checkpoint(ckpt["files"].get("p4", ""), "P4 market snapshot")
            _fill_missing_p2_p3_from_checkpoint(context, ckpt)
            context["p7_deep"] = _read_fresh_text_checkpoint(
                ckpt,
                "p7",
                "P7 deep research",
                upstream_keys=("p6",),
            )
            if context["p7_deep"]:
                context["p7_evidence_provenance"] = _recover_p7_evidence_provenance(
                    ckpt
                )
            context["p75_supplement"], context["p75_raw_text"] = (
                _recover_p75_checkpoint(ckpt)
            )
            if context["p7_deep"]:
                context["p7a_audited"] = _read_fresh_text_checkpoint(
                    ckpt,
                    "p7a",
                    "P7a audited research",
                    upstream_keys=("p6", "p7"),
                )
            context["price_data"] = _read_text_checkpoint(ckpt["files"].get("p8", ""), "P8 technical data")
            return

    p1_was_recollected = (
        resume_from == "intel_resume"
        and "p1" in set(context.get("intel_resume_stages") or ckpt.get("intel_resume_stages", []))
    )
    if (
        not context.get("p5_evidence")
        and ckpt["files"].get("p5")
        and not p1_was_recollected
    ):
        context["p5_evidence"] = _recover_p5_checkpoint(ckpt)
    _fill_missing_p2_p3_from_checkpoint(context, ckpt)

    if ckpt["files"].get("p4"):
        context["p4_data"] = _read_text_checkpoint(ckpt["files"].get("p4", ""), "P4 market snapshot")
    else:
        started_at = time.time()
        try:
            context["p4_data"] = fetch_vanguard_prices()
        except Exception as exc:
            context["p4_data"] = ""
            logging.error("[P4] market snapshot failed; continuing without market snapshot: %s", exc)
            update_run_manifest(
                "p4",
                status="failed_exception_fallback_empty",
                metrics={
                    "fallback_reason": str(exc),
                    "duration_seconds": round(time.time() - started_at, 1),
                },
            )
        context["p4_duration"] = time.time() - started_at

    combined_evidence = "\n".join(
        [
            context.get("p5_evidence", ""),
            context.get("p4_data", ""),
            context.get("p2_data", ""),
            context.get("p3_data", ""),
        ]
    ).strip()

    if not has_meaningful_semantic_text(combined_evidence):
        update_run_manifest(
            "p6",
            status="failed_missing_input",
            metrics={
                "input_chars": len(combined_evidence),
                "error": "no meaningful P2/P3/P4/P5 evidence",
            },
        )
        raise RuntimeError("P6 mapping blocked: no meaningful upstream evidence")

    with log_heartbeat("P6 mapping"):
        try:
            context["p6_mapping"] = generate_report(combined_evidence)
        except Exception as exc:
            logging.error("[P6] mapping failed: %s", exc)
            context["p6_mapping"] = ""
            update_run_manifest(
                "p6",
                status="failed_exception",
                metrics={
                    "input_chars": len(combined_evidence),
                    "error": str(exc),
                },
            )
            raise RuntimeError(f"P6 mapping failed: {exc}") from exc

    if not context["p6_mapping"].strip():
        p6_status = str(
            load_run_manifest().get("stages", {}).get("p6", {}).get("status", "")
        ).strip()
        if p6_status != "failed_incomplete_output":
            update_run_manifest(
                "p6",
                status="failed_empty_output",
                metrics={
                    "input_chars": len(combined_evidence),
                    "error": "P6 mapping returned empty output",
                },
            )
        raise RuntimeError("P6 mapping returned empty output")

    p6_path = write_text_artifact("P6_映射", context["p6_mapping"])
    context["ckpt"].setdefault("files", {})["p6"] = p6_path
    update_run_manifest(
        "p6",
        status="completed",
        files={"md": p6_path},
        metrics={"mapping_chars": len(context["p6_mapping"])},
    )
    identity_validation = _load_p6_identity_validation()
    context["rejected_target_names"] = identity_validation["rejected_target_names"]
    context["ckpt"]["p6_metrics"] = {
        "rejected_target_names": identity_validation["rejected_target_names"],
        "rejected_targets": identity_validation["rejected_targets"],
        "identity_validation_version": identity_validation["identity_validation_version"],
    }


def _stage_research(context: dict[str, Any]) -> None:
    if not context.get("p6_mapping"):
        return

    resume_from = context["resume_from"]
    recovered_p7_evidence = _normalize_p7_evidence_with_topics(
        context.get("p7_evidence_provenance")
    )
    context["p7_evidence_provenance"] = recovered_p7_evidence
    rejected_target_names = normalize_rejected_stock_names(
        context.get("rejected_target_names")
    )
    if not rejected_target_names:
        identity_validation = _load_p6_identity_validation(
            context.get("ckpt", {}).get("p6_metrics", {})
        )
        rejected_target_names = identity_validation["rejected_target_names"]
        context["rejected_target_names"] = rejected_target_names
    try:
        research_plan = build_research_plan(
            context["p6_mapping"],
            p4_data=context.get("p4_data", ""),
            p5_evidence=context.get("p5_evidence", ""),
            rejected_target_names=rejected_target_names,
        )
    except Exception as exc:
        logging.error("[P7] research plan failed; skipping optional research layers: %s", exc)
        update_run_manifest(
            "p7_plan",
            status="failed_plan_fallback",
            metrics={"fallback_reason": str(exc)},
        )
        research_plan = {
            "goal": "",
            "triggers": [],
            "deep_topics": [],
            "supplement_queries": [],
            "should_run_p7": False,
            "should_run_p75": False,
            "max_rounds": 0,
            "round_backtracks": [],
            "skipped_recent_topics": [],
        }
    context["research_plan"] = research_plan

    try:
        context["p7_plan_path"] = write_json_artifact("P7_研究计划", research_plan)
    except Exception as exc:
        logging.error("[P7] failed to write plan: %s", exc)
        context["p7_plan_path"] = ""

    search_queries = list(research_plan.get("supplement_queries") or [])
    need_run_p7 = bool(research_plan.get("should_run_p7"))
    need_run_p75 = bool(research_plan.get("should_run_p75"))
    p6_input_sha256 = _context_p6_input_sha256(context)
    p6_hash_metrics = {"p6_input_sha256": p6_input_sha256} if p6_input_sha256 else {}

    if resume_from in {"p6", "p8_resume", "p9_resume"}:
        stage_assessments = context.get("ckpt", {}).get("stage_assessments", {})
        if not isinstance(stage_assessments, dict):
            stage_assessments = {}
        if context.get("p7_deep", "").strip():
            need_run_p7 = False
        elif stage_assessments.get("p7", {}).get("state") == "valid_empty":
            need_run_p7 = False
        if context.get("p75_supplement", "").strip() or not search_queries:
            need_run_p75 = False
        elif stage_assessments.get("p75", {}).get("state") == "valid_empty":
            need_run_p75 = False

    _refresh_not_triggered_plan_stages(
        context,
        need_run_p7=need_run_p7,
        need_run_p75=need_run_p75,
        search_queries=search_queries,
        p6_input_sha256=p6_input_sha256,
        p6_hash_metrics=p6_hash_metrics,
    )

    p75_started = None
    if need_run_p7 or need_run_p75:
        with log_heartbeat("P7/P7.5 research"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                p7_future = pool.submit(run_deep_research, research_plan) if need_run_p7 else None
                supplement_results: list[str] = []
                supplement_query_failures = 0
                capped_queries = search_queries[:5]
                if need_run_p75 and capped_queries:
                    p75_started = time.time()
                    for query in capped_queries:
                        try:
                            # Why: P7.5 是研究型查询（产能、技术差距等），不是实时新闻采集
                            #      空串走 unlimited 模式，不限制搜索时间窗口
                            result = gemini_search("P7.5补充", query, "")
                            text = (result.get("text") or "").strip() if result else ""
                            # Why: Gemini prompt 要求搜不到时回 "not found"，
                            #      必须过滤掉这种空结果，否则会被当作有效数据送进审计浪费 token
                            if text and not is_no_evidence_text(text):
                                supplement_results.append(text)
                        except Exception as exc:
                            supplement_query_failures += 1
                            logging.error("[P7.5] supplement search failed: %s", exc)

                if p7_future is not None:
                    try:
                        p7_result = p7_future.result()
                        context["p7_deep"] = p7_result.get("text", "")
                        result_p7_evidence = _normalize_p7_evidence_with_topics(
                            p7_result.get("evidence_provenance")
                        )
                        if result_p7_evidence:
                            context["p7_evidence_provenance"] = result_p7_evidence
                        context["p7_rejected_identity_lines_removed"] = int(
                            p7_result.get("rejected_identity_lines_removed", 0) or 0
                        )
                        p7_status = str(p7_result.get("status") or "").strip()
                        if not context["p7_deep"].strip() and p7_status:
                            empty_status = (
                                p7_status
                                if p7_status == "skipped_no_results" or p7_status.startswith("failed")
                                else "failed_empty_output"
                            )
                            p7_evidence_path = _write_p7_evidence_artifact(
                                context,
                                context.get("p7_evidence_provenance"),
                            )
                            update_run_manifest(
                                "p7",
                                status=empty_status,
                                files={"evidence": p7_evidence_path}
                                if p7_evidence_path
                                else None,
                                metrics={
                                    "output_chars": 0,
                                    "topics_planned": int(p7_result.get("topics_planned", 0) or 0),
                                    "topics_completed": int(p7_result.get("topics_completed", 0) or 0),
                                    **(
                                        {
                                            "p7_evidence_provenance": context[
                                                "p7_evidence_provenance"
                                            ]
                                        }
                                        if context.get("p7_evidence_provenance")
                                        else {}
                                    ),
                                    **p6_hash_metrics,
                                },
                            )
                    except Exception as exc:
                        logging.error("[P7] deep research failed: %s", exc)
                        context["p7_deep"] = ""
                        update_run_manifest(
                            "p7",
                            status="failed_exception",
                            metrics={
                                "fallback_reason": str(exc),
                                **p6_hash_metrics,
                            },
                        )

                if need_run_p75:
                    if supplement_results:
                        raw_text = "\n".join(supplement_results)
                        context["p75_raw_text"] = raw_text
                        raw_path = write_text_artifact("P7.5_补充搜索原始数据", raw_text)
                        update_run_manifest(
                            "p75",
                            status="raw_ready",
                            files={"raw": raw_path},
                            metrics={
                                "queries": len(capped_queries),
                                "raw_chars": len(raw_text),
                                **p6_hash_metrics,
                            },
                        )
                        with log_heartbeat("P7.5 audit"):
                            audited = gemini_pro_audit(raw_text, audit_type="p7.5", backtrack="")
                        p75_audit_status = str(
                            load_run_manifest()
                            .get("stages", {})
                            .get("p75", {})
                            .get("status", "")
                        ).strip().lower()
                        if p75_audit_status in {
                            "completed_no_results",
                            "completed_no_valid_evidence",
                            "skipped_no_results",
                        }:
                            context["p75_supplement"] = ""
                        elif str(audited or "").strip():
                            context["p75_supplement"] = audited
                        elif p75_audit_status.startswith("failed"):
                            context["p75_supplement"] = raw_text
                        else:
                            context["p75_supplement"] = ""
                    else:
                        context["p75_supplement"] = ""
                        update_run_manifest(
                            "p75",
                            status=(
                                "failed_query_errors"
                                if supplement_query_failures
                                else "skipped_no_results"
                            ),
                            metrics={
                                "queries": len(capped_queries),
                                "query_failures": supplement_query_failures,
                                "raw_chars": 0,
                                **p6_hash_metrics,
                            },
                        )

    if p75_started is not None:
        context["p75_duration"] = time.time() - p75_started

    if context.get("p7_deep"):
        p7_evidence = _normalize_p7_evidence_with_topics(
            context.get("p7_evidence_provenance")
        )
        context["p7_evidence_provenance"] = p7_evidence
        p7_evidence_boundary = build_p7_evidence_boundary(p7_evidence)
        p7_evidence_prefix = (
            f"> {p7_evidence_boundary}\n\n"
            if p7_evidence_boundary and p7_evidence_boundary not in context["p7_deep"]
            else ""
        )
        p7_body = (
            "# P7 深度研究结果\n\n"
            f"> generated_at: {datetime.now().isoformat()}\n\n"
            f"> goal: {research_plan.get('goal', '')}\n"
            f"> triggers: {', '.join(research_plan.get('triggers', [])) or 'none'}\n\n"
            f"{p7_evidence_prefix}{context['p7_deep']}"
        )
        p7_path = write_text_artifact("P7_深度研究", p7_body)
        p7_files = {"md": p7_path}
        if context["p7_plan_path"]:
            p7_files["plan"] = context["p7_plan_path"]
        p7_evidence_path = _write_p7_evidence_artifact(context, p7_evidence)
        if p7_evidence_path:
            p7_files["evidence"] = p7_evidence_path
        update_run_manifest(
            "p7",
            status="completed",
            files=p7_files,
            metrics={
                "output_chars": len(context["p7_deep"]),
                "rejected_target_names": rejected_target_names,
                "rejected_identity_lines_removed": context.get(
                    "p7_rejected_identity_lines_removed",
                    0,
                ),
                **({"p7_evidence_provenance": p7_evidence} if p7_evidence else {}),
                **p6_hash_metrics,
            },
        )

    if (
        not context.get("p7_deep", "").strip()
        and _research_stage_needs_p6_hash_refresh(context, "p7a", p6_input_sha256)
    ):
        _record_research_stage_not_triggered("p7a", p6_hash_metrics)

    stage_assessments = context.get("ckpt", {}).get("stage_assessments", {})
    if not isinstance(stage_assessments, dict):
        stage_assessments = {}
    p7a_checkpoint_state = (
        str(stage_assessments.get("p7a", {}).get("state") or "")
        if resume_from in {"p6", "p8_resume", "p9_resume"}
        else ""
    )
    p7a_reusable_without_audit = p7a_checkpoint_state in {
        "valid_empty",
        "valid_passthrough",
    }
    if (
        p7a_checkpoint_state == "valid_passthrough"
        and not context.get("p7a_audited")
    ):
        p7a_checkpoint_status = str(
            stage_assessments.get("p7a", {}).get("status") or ""
        ).strip().lower()
        raw_p7_research = context.get("p7_deep", "")
        context["p7a_audited"] = (
            _mark_p7a_unavailable_passthrough(
                raw_p7_research,
                "历史 P7a 审计未生成可用结果",
            )
            if p7a_checkpoint_status.startswith("failed")
            else raw_p7_research
        )
    if not p7a_reusable_without_audit and not context.get("p7a_audited") and (
        context.get("p7_deep", "").strip() and len(context["p7_deep"].strip()) > 200
    ):
        with log_heartbeat("P7a audit"):
            try:
                if p6_hash_metrics:
                    update_run_manifest("p7a", status="running", metrics=p6_hash_metrics)
                context["p7a_audited"] = p7a_deep_research_audit(
                    context["p7_deep"],
                    rejected_target_names=rejected_target_names,
                    p7_evidence_provenance=context.get("p7_evidence_provenance"),
                )
            except Exception as exc:
                logging.error("[P7a] audit failed unexpectedly; passing through raw P7 data: %s", exc)
                update_run_manifest(
                    "p7a",
                    status="failed_exception_passthrough",
                    metrics={
                        "input_chars": len(context.get("p7_deep", "") or ""),
                        "fallback_reason": str(exc),
                        "structured_degraded": True,
                        "data_state": "invalid",
                        "degrade_reason": "p7a_exception_passthrough",
                        **(
                            {
                                "p7_evidence_provenance": context[
                                    "p7_evidence_provenance"
                                ]
                            }
                            if context.get("p7_evidence_provenance")
                            else {}
                        ),
                    },
                )
                context["p7a_audited"] = _mark_p7a_unavailable_passthrough(
                    context.get("p7_deep", ""),
                    "P7a 审计发生异常",
                )
    elif not p7a_reusable_without_audit and not context.get("p7a_audited"):
        context["p7a_audited"] = context.get("p7_deep", "")
        if context["p7a_audited"].strip():
            update_run_manifest(
                "p7a",
                status="skipped_short_input",
                metrics={
                    "input_chars": len(context["p7a_audited"]),
                    "passthrough_chars": len(context["p7a_audited"]),
                    "data_state": "valid_passthrough",
                    **(
                        {
                            "p7_evidence_provenance": context[
                                "p7_evidence_provenance"
                            ]
                        }
                        if context.get("p7_evidence_provenance")
                        else {}
                    ),
                    **p6_hash_metrics,
                },
            )

    context["search_queries"] = search_queries[:5]


def _stage_technical(context: dict[str, Any]) -> None:
    try:
        current_stocks = extract_stocks_from_report(context.get("p6_mapping", ""))
        if context["resume_from"] == "p9_resume" and context.get("price_data", "").strip():
            ckpt = context.get("ckpt", {})
            stored_assessment = ckpt.get("p8_assessment", {})
            if isinstance(stored_assessment, dict) and stored_assessment.get("reusable") is False:
                assessment = dict(stored_assessment)
            else:
                assessment_metrics = ckpt.get("p8_metrics", {})
                if not isinstance(assessment_metrics, dict) or not assessment_metrics:
                    assessment_metrics = {
                        "stocks_requested": stored_assessment.get("stocks_requested"),
                        "stocks_with_data": stored_assessment.get("stocks_with_data"),
                    }
                assessment = assess_p8_checkpoint(
                    ckpt.get("files", {}).get("p8", ""),
                    markdown_text=context.get("price_data", ""),
                    stage_status=str(ckpt.get("p8_status") or stored_assessment.get("status") or ""),
                    stage_metrics=assessment_metrics,
                    current_stock_count=len(current_stocks),
                    stale=is_older_than_any(ckpt.get("files", {}), "p8", ("p6",)),
                    upstream_valid=bool(context.get("p6_mapping", "").strip()),
                )
            ckpt["p8_assessment"] = assessment
            if assessment.get("reusable"):
                logging.info(
                    "reusing P8 technical data from checkpoint (%s)",
                    assessment.get("state", "unknown"),
                )
                return
            logging.warning(
                "[P8] checkpoint is not reusable (%s); rerunning technical layer",
                assessment.get("reason", "unknown"),
            )

        with log_heartbeat("P8 technical"):
            tracks = extract_stock_tracks(context.get("p6_mapping", ""))
            elasticity = extract_stock_elasticity(context.get("p6_mapping", ""))
            context["price_data"] = p8_search_stock_prices(current_stocks, tracks, elasticity)
    except Exception as exc:
        logging.error("[P8] technical layer failed; continuing with fallback price section: %s", exc)
        fallback_text = (
            "# P8 技术面分析数据（降级占位）\n\n"
            f"> 读取时间: {datetime.now().isoformat()}\n\n"
            "> P8 技术面环节发生异常，本次 P9 将在无可用技术面数据的情况下继续生成，并应降低技术面权重。\n"
        )
        context["price_data"] = fallback_text
        fallback_path = ""
        try:
            fallback_path = write_text_artifact("P8_技术面_FALLBACK", fallback_text)
        except Exception as write_exc:
            logging.warning("[P8] fallback artifact write failed: %s", write_exc)
        update_run_manifest(
            "p8",
            status="failed_exception_fallback",
            files={"md": fallback_path} if fallback_path else None,
            metrics={
                "fallback_reason": str(exc),
                "price_data_chars": len(fallback_text),
                "data_state": "invalid",
                "degrade_reason": "p8_exception_fallback",
            },
        )


def _resolve_p9_checkpoint_snapshot(context: dict[str, Any]) -> dict[str, Any] | None:
    initial_checkpoint = context.get("ckpt", {})
    if (
        context.get("resume_from") == "p9_resume"
        and isinstance(initial_checkpoint, dict)
        and initial_checkpoint.get("stage_assessments")
        and initial_checkpoint.get("p8_assessment")
    ):
        try:
            current_checkpoint = detect_checkpoint()
        except Exception as exc:
            logging.error("[P9] failed to refresh the P8 checkpoint assessment: %s", exc)
            return initial_checkpoint
        current_p8_assessment = current_checkpoint.get("p8_assessment", {})
        if isinstance(current_p8_assessment, dict) and current_p8_assessment:
            checkpoint_snapshot = dict(initial_checkpoint)
            checkpoint_snapshot["p8_assessment"] = current_p8_assessment
            return checkpoint_snapshot
        return initial_checkpoint
    try:
        return detect_checkpoint()
    except Exception as exc:
        logging.error("[P9] failed to prepare checkpoint validation snapshot: %s", exc)
        return None


def _p5_audit_is_degraded(context: dict[str, Any]) -> bool:
    if context.get("p5_audit_degraded") is True:
        return True
    try:
        p5_payload = load_run_manifest().get("stages", {}).get("p5", {})
    except Exception as exc:
        logging.error("[P9] failed to read P5 audit state; P5 will be downgraded: %s", exc)
        return True
    metrics = p5_payload.get("metrics", {}) if isinstance(p5_payload, dict) else {}
    if not isinstance(metrics, dict):
        logging.error("[P9] malformed P5 audit metrics; P5 will be downgraded")
        return True
    p5_status = str(p5_payload.get("status") or "").strip().lower()
    p5_data_state = str(metrics.get("data_state") or "").strip().lower()
    if p5_status == "completed" and p5_data_state == "valid":
        return False
    if (
        p5_status in VALID_EMPTY_STATUSES.get("p5", frozenset())
        and p5_data_state == "valid_empty"
    ):
        return False
    return (
        metrics.get("fallback") == "raw_p1_unreviewed"
        or str(metrics.get("data_state") or "").strip().lower() == "invalid"
    )


def _stage_report(context: dict[str, Any]) -> None:
    if not str(context.get("p6_mapping", "")).strip():
        raise RuntimeError("P6 mapping is missing; P9 report generation is blocked")

    with log_heartbeat("P9 report"):
        try:
            p5_audit_degraded = _p5_audit_is_degraded(context)
            final_report, report_ts = p9_risk_analysis(
                p5_evidence=context.get("p5_evidence", ""),
                p7_deep=context.get("p7a_audited", ""),
                p75_supplement=context.get("p75_supplement", ""),
                p6_mapping=context.get("p6_mapping", ""),
                price_data=context.get("price_data", ""),
                p2_x_intel_data=context.get("p2_data", ""),
                p3_intel_data=context.get("p3_data", ""),
                p4_yfinance=context.get("p4_data", ""),
                rejected_target_names=context.get("rejected_target_names", []),
                checkpoint_snapshot=_resolve_p9_checkpoint_snapshot(context),
                p5_audit_degraded=p5_audit_degraded,
                p7_evidence_provenance=context.get("p7_evidence_provenance"),
            )
        except Exception as exc:
            logging.error("[P9] report generation failed: %s", exc)
            raise RuntimeError(f"report generation failed: {exc}") from exc

    context["final_report"] = final_report
    context["report_ts"] = report_ts

    if not final_report or not final_report.strip():
        raise RuntimeError("report generation returned an empty report")

    delivery = send_report_email(final_report, report_ts)
    if not delivery.get("ok"):
        raise RuntimeError(f"report delivery failed: {delivery.get('status', 'unknown')}")

def _stage_delivery_resume(context: dict[str, Any]) -> None:
    if context.get("resume_from") != "delivery_resume":
        return

    final_report = context.get("final_report", "")
    report_ts = context.get("report_ts", "") or context.get("run_ts", "")
    if not final_report or not final_report.strip():
        raise RuntimeError("saved report is missing or empty; delivery cannot resume")

    p9_metrics = context.get("ckpt", {}).get("p9_metrics", {})
    if isinstance(p9_metrics, dict) and p9_metrics.get("delivery_ambiguous") is True:
        raise RuntimeError("delivery state is ambiguous; manual confirmation is required before resend")
    saved_email_policy = p9_metrics.get("email_configured") if isinstance(p9_metrics, dict) else None
    email_required = saved_email_policy if isinstance(saved_email_policy, bool) else None
    if email_required is True and not (EMAIL_ADDRESS and EMAIL_PASSWORD and RECEIVER_EMAIL):
        raise RuntimeError("email configuration is incomplete; required delivery cannot resume")

    delivery = send_report_email(final_report, report_ts, email_required=email_required)
    if not delivery.get("ok"):
        raise RuntimeError(f"report delivery failed: {delivery.get('status', 'unknown')}")

    update_run_manifest(
        "report",
        status="completed_recovered",
        metrics={
            "recovered_delivery": True,
            "delivery_status": delivery.get("status", ""),
            "report_chars": len(final_report),
        },
    )


def _stage_roi(context: dict[str, Any]) -> None:
    ckpt = context["ckpt"]
    if context.get("resume_from") == "roi_resume":
        final_report = context.get("final_report", "")
        if not final_report or not final_report.strip():
            raise RuntimeError("saved report is missing or empty; ROI cannot resume")
        update_run_manifest(
            "report",
            status="completed_recovered",
            metrics={
                "recovered_for_roi": True,
                "report_chars": len(final_report),
            },
        )

    p1_results = context.get("p1_results") or []
    if not p1_results and ckpt.get("files", {}).get("p1"):
        recovered_results = _read_json_checkpoint(ckpt["files"].get("p1", ""), "P1 raw results")
        if isinstance(recovered_results, list):
            p1_results = recovered_results

    manifest_snapshot = load_run_manifest()

    def manifest_metric(stage: str, key: str, default: float = 0.0) -> float:
        try:
            value = manifest_snapshot.get("stages", {}).get(stage, {}).get("metrics", {}).get(key, default)
            return float(value)
        except (TypeError, ValueError):
            return default

    p75_status = str(
        manifest_snapshot.get("stages", {}).get("p75", {}).get("status", "")
    ).strip().lower()
    p75_valid_empty = p75_status in VALID_EMPTY_STATUSES.get("p75", frozenset())
    p75_text = context.get("p75_supplement", "")
    if not p75_text and not p75_valid_empty:
        p75_text = context.get("p75_raw_text", "")

    roi_payload = build_layer_roi_report(
        p1_results=p1_results,
        p3_raw_results=context.get("p3_stats", {}).get("raw_results") if isinstance(context.get("p3_stats"), dict) else None,
        p3_text=context.get("p3_data", ""),
        p4_text=context.get("p4_data", ""),
        p75_text=p75_text,
        p75_queries=context.get("search_queries", []),
        p6_mapping=context.get("p6_mapping", ""),
        final_report=context.get("final_report", ""),
        timings={
            "p1": context.get("p1_duration", 0.0) or manifest_metric("p1", "duration_seconds"),
            "p3": float(context.get("p3_stats", {}).get("duration_seconds", 0) or manifest_metric("p3", "duration_seconds")),
            "p4": context.get("p4_duration", 0.0) or manifest_metric("p4", "duration_seconds"),
            "p75": context.get("p75_duration", 0.0) or manifest_metric("p75", "duration_seconds"),
        },
        extras={
            "p1": {
                "records": len(p1_results),
                "failed_urls": context.get("p1_stats", {}).get("failed_urls", 0),
                "rss_success_sources": context.get("p1_stats", {}).get("rss_success_sources", 0),
                "web_success_sources": context.get("p1_stats", {}).get("web_success_sources", 0),
            },
            "p3": {
                "jinnang_2_count": context.get("p3_stats", {}).get("jinnang_2_count", 0),
                "jinnang_3_count": context.get("p3_stats", {}).get("jinnang_3_count", 0),
            },
            "p4": {
                "signal_lines": context.get("p4_data", "").count("%") if context.get("p4_data") else 0,
            },
            "p75": {
                "query_count": len(context.get("search_queries", [])),
                "raw_chars": len(context.get("p75_raw_text", "") or ""),
                "audited_chars": len(context.get("p75_supplement", "") or ""),
                "valid_empty": p75_valid_empty,
            },
        },
    )

    roi_json_path = write_json_artifact("Layer_ROI", roi_payload)
    roi_md_path = write_text_artifact("Layer_ROI", render_layer_roi_markdown(roi_payload))
    roi_layers = roi_payload.get("layers", {})
    if not isinstance(roi_layers, dict):
        roi_layers = {}

    def roi_metric_int(payload: Any, key: str) -> int:
        if not isinstance(payload, dict):
            return 0
        try:
            return max(int(payload.get(key, 0)), 0)
        except (TypeError, ValueError):
            return 0

    layer_units = [roi_metric_int(payload, "units") for payload in roi_layers.values()]
    update_run_manifest(
        "roi",
        status="completed",
        files={"json": roi_json_path, "md": roi_md_path},
        metrics={
            "layers_evaluated": len(roi_layers),
            "layers_with_units": sum(units > 0 for units in layer_units),
            "empty_layers": sum(units == 0 for units in layer_units),
            "total_units": sum(layer_units),
            "total_unique_units": sum(roi_metric_int(payload, "unique_units") for payload in roi_layers.values()),
            "total_adopted_units": sum(roi_metric_int(payload, "adopted_units") for payload in roi_layers.values()),
        },
    )

    manifest_after_roi = load_run_manifest()
    for stage_name, payload in roi_layers.items():
        stage_payload = manifest_after_roi.get("stages", {}).get(stage_name, {})
        if not isinstance(stage_payload, dict) or not str(stage_payload.get("status") or "").strip():
            continue
        stage_status = stage_payload["status"]
        update_run_manifest(
            stage_name,
            status=stage_status,
            metrics={
                "roi_units": payload.get("units", 0),
                "roi_unique_units": payload.get("unique_units", 0),
                "roi_unique_ratio": payload.get("unique_ratio", 0.0),
                "roi_adopted_units": payload.get("adopted_units", 0),
                "roi_adoption_ratio": payload.get("adoption_ratio", 0.0),
                "roi_p6_hits": payload.get("p6_hits", 0),
                "roi_p9_hits": payload.get("p9_hits", 0),
                "roi_score": payload.get("score", 0.0),
                "roi_recommendation": payload.get("recommendation", "unknown"),
            },
        )


def _build_stage_registry() -> list[StageSpec]:
    return [
        StageSpec(
            name="collect_intel",
            handler=_stage_collect_intel,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["collect_intel"],
            description="P1-P3 collection and resume loading",
        ),
        StageSpec(
            name="recover_p2",
            handler=_stage_recover_p2,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["recover_p2"],
            description="Targeted P2 recovery without rerunning P1 or P3",
        ),
        StageSpec(
            name="audit_evidence",
            handler=_stage_audit_evidence,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["audit_evidence"],
            description="P5 audit and checkpoint reuse",
        ),
        StageSpec(
            name="market_mapping",
            handler=_stage_market_mapping,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["market_mapping"],
            description="P4 snapshot and P6 mapping",
        ),
        StageSpec(
            name="research",
            handler=_stage_research,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["research"],
            continue_on_error=True,
            description="P7/P7a/P7.5 research pipeline",
        ),
        StageSpec(
            name="technical",
            handler=_stage_technical,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["technical"],
            continue_on_error=True,
            description="P8 technical layer",
        ),
        StageSpec(
            name="report",
            handler=_stage_report,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["report"],
            description="P9 report generation",
        ),
        StageSpec(
            name="delivery_resume",
            handler=_stage_delivery_resume,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["delivery_resume"],
            description="Retry delivery of an existing P9 report",
        ),
        StageSpec(
            name="roi",
            handler=_stage_roi,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["roi"],
            description="Layer ROI summary",
        ),
    ]


def orchestrator_main(argv: list[str] | None = None):
    total_start = time.time()
    now = datetime.now()
    args = list(sys.argv[1:] if argv is None else argv)
    force_run, force_restart = _parse_flags(args)

    logging.info("=" * 60)
    logging.info("VCP Hunter orchestrator | %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logging.info("=" * 60)

    if not force_run:
        schedule = get_schedule_decision(now.date())
        if not schedule["is_trading_day"]:
            record_run_state(
                status="skipped_non_trading_day",
                root_cause="non_trading_day",
                reason=str(schedule.get("reason", "today is not a trading day")),
                source="orchestrator",
                schedule=schedule,
                main_flow_entered=False,
                runtime_context=build_runtime_context(),
            )
            logging.info("today is not a trading day, skip run")
            return
        if not schedule["should_run"]:
            record_run_state(
                status="skipped_schedule_rule",
                root_cause="schedule_rule_skip",
                reason=str(schedule.get("reason", "scheduled rule skipped today's run")),
                source="orchestrator",
                schedule=schedule,
                main_flow_entered=False,
                runtime_context=build_runtime_context(),
            )
            logging.info("scheduled rule skipped today's run | %s", schedule["reason"])
            return

    ckpt = {"stage": "none", "files": {}, "run_timestamp": ""}
    if not force_restart:
        ckpt = detect_checkpoint()
        run_ts = set_run_timestamp(ckpt.get("run_timestamp") or None)
    else:
        run_ts = set_run_timestamp()
        archive_path = reset_run_manifest(run_ts)
        if archive_path:
            logging.info("[restart] archived previous manifest: %s", archive_path)

    resume_from = ckpt["stage"]
    start_at = _resolve_start_stage(resume_from)
    completed_after_resume = resume_from not in {"", "none", "p9_done", "manual_confirmation_required"}
    resume_runtime_context = {
        "resume_from": resume_from,
        "resume_start_stage": start_at or "",
        "completed_after_resume": completed_after_resume,
    }
    update_run_manifest(
        "orchestrator",
        status="started",
        metrics={
            "resume_from": resume_from,
            "resume_start_stage": start_at or "",
            "completed_after_resume": completed_after_resume,
            "force_restart": force_restart,
            "force_run": force_run,
        },
        timestamp=run_ts,
    )
    runtime_context = build_runtime_context()
    runtime_context.update(resume_runtime_context)
    update_manifest_context(
        runtime_context=runtime_context,
        preflight=get_preflight_summary(),
        timestamp=run_ts,
    )
    record_run_state(
        status="running",
        root_cause="orchestrator_started",
        reason="orchestrator entered main flow",
        source="orchestrator",
        main_flow_entered=True,
        runtime_context=resume_runtime_context,
        timestamp=run_ts,
    )

    if resume_from == "manual_confirmation_required":
        reason = "SMTP delivery outcome is ambiguous; manual confirmation is required before any resend"
        logging.error(reason)
        update_run_manifest(
            "orchestrator",
            status="manual_confirmation_required",
            metrics={
                "resume_from": resume_from,
                "resume_start_stage": "",
                "completed_after_resume": False,
                "force_restart": force_restart,
                "force_run": force_run,
                "automatic_progress_blocked": True,
                "reason": reason,
            },
            timestamp=run_ts,
        )
        record_run_state(
            status="manual_confirmation_required",
            root_cause="delivery_ambiguous",
            reason=reason,
            source="orchestrator",
            main_flow_entered=False,
            runtime_context=resume_runtime_context,
            timestamp=run_ts,
        )
        return

    if resume_from == "p9_done":
        logging.info("war report already exists for today, skip run")
        update_run_manifest(
            "orchestrator",
            status="completed",
            metrics={
                "resume_from": resume_from,
                "resume_start_stage": start_at or "",
                "completed_after_resume": completed_after_resume,
                "force_restart": force_restart,
                "force_run": force_run,
                "total_elapsed_seconds": round(time.time() - total_start, 1),
            },
            timestamp=run_ts,
        )
        record_run_state(
            status="completed_existing_report",
            root_cause="checkpoint_p9_done",
            reason="war report already exists for today",
            source="orchestrator",
            main_flow_entered=True,
            runtime_context=resume_runtime_context,
            timestamp=run_ts,
        )
        return

    context = _build_context(now, run_ts, ckpt, resume_from)
    try:
        run_stage_registry(_build_stage_registry(), context, start_at=start_at)
    except Exception as exc:
        status = "failed_timeout" if isinstance(exc, StageTimeoutError) else "failed_exception"
        update_run_manifest(
            "orchestrator",
            status=status,
            metrics={
                "resume_from": resume_from,
                "resume_start_stage": start_at or "",
                "completed_after_resume": completed_after_resume,
                "total_elapsed_seconds": round(time.time() - total_start, 1),
                "error": str(exc),
            },
            timestamp=run_ts,
        )
        record_run_state(
            status=status,
            root_cause=status,
            reason=str(exc),
            source="orchestrator",
            main_flow_entered=True,
            runtime_context=resume_runtime_context,
            timestamp=run_ts,
        )
        raise

    total_elapsed = time.time() - total_start
    degraded_stages = summarize_stage_degradations(load_run_manifest())
    final_status = "completed_with_stage_degradation" if degraded_stages else "completed"
    final_root_cause = "stage_degraded" if degraded_stages else "completed"
    if degraded_stages:
        final_reason = "completed with degraded stages: " + ", ".join(item["stage"] for item in degraded_stages)
    else:
        final_reason = "orchestrator completed"
    if completed_after_resume:
        final_reason = f"{final_reason} after resume_from={resume_from} start_stage={start_at or 'unknown'}"
    final_runtime_context = dict(resume_runtime_context)
    if degraded_stages:
        final_runtime_context["degraded_stages"] = degraded_stages
    update_run_manifest(
        "orchestrator",
        status=final_status,
        metrics={
            "resume_from": resume_from,
            "resume_start_stage": start_at or "",
            "completed_after_resume": completed_after_resume,
            "total_elapsed_seconds": round(total_elapsed, 1),
            "final_report_chars": len(context.get("final_report", "") or ""),
            "degraded_stage_count": len(degraded_stages),
        },
        timestamp=run_ts,
    )
    record_run_state(
        status=final_status,
        root_cause=final_root_cause,
        reason=final_reason,
        source="orchestrator",
        main_flow_entered=True,
        runtime_context=final_runtime_context,
        timestamp=run_ts,
    )
    logging.info("pipeline finished in %.1fs", total_elapsed)


def _configure_file_logging() -> None:
    _configure_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", force=True)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    pipeline_handler = RotatingFileHandler(
        get_pipeline_log_path(),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    pipeline_handler.setLevel(logging.INFO)
    pipeline_handler.setFormatter(formatter)
    logging.getLogger().addHandler(pipeline_handler)

    error_handler = RotatingFileHandler(
        get_error_log_path(),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logging.getLogger().addHandler(error_handler)

    for noisy_logger in ("httpx", "httpcore", "urllib3", "google"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


if __name__ == "__main__":
    _configure_file_logging()
    orchestrator_main()
