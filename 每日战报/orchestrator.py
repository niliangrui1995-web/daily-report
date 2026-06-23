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
    get_preflight_summary,
    get_pipeline_log_path,
    load_run_manifest,
    record_run_state,
    set_run_timestamp,
    summarize_stage_degradations,
    update_manifest_context,
    update_run_manifest,
    write_json_artifact,
    write_text_artifact,
)
from checkpoint import detect_checkpoint
from contribution_metrics import build_layer_roi_report, render_layer_roi_markdown
from pipeline_runtime import StageSpec, StageTimeoutError, run_stage_registry
from run_config import get_run_config, get_schedule_decision

from p1_rss import collect_from_rss
from p1_web import collect_from_web
from p2_x_intel import collect_from_x
from p3_agent import collect_p3_parallel_only, render_p3_output, run_jinnang_1
from p4_vanguard import fetch_vanguard_prices
from audit_engine import gemini_pro_audit, p7a_deep_research_audit
from p6_mapper import format_evidence, generate_report
from p7_deep_research import build_research_plan, run_deep_research
from p8_technical import (
    extract_stocks_from_report,
    extract_stock_elasticity,
    extract_stock_tracks,
    p8_search_stock_prices,
)
from p9_reporter import p9_risk_analysis, send_report_email
from search_engine import gemini_search
from agent_assistant import diagnose_failure, review_final_report

CONTROL_STAGE_START = {
    "none": "collect_intel",
    "p1": "collect_intel",
    "p5": "audit_evidence",
    "p6": "market_mapping",
    "p8_resume": "market_mapping",
    "p9_resume": "market_mapping",
}

CONTROL_STAGE_TIMEOUTS = {
    "collect_intel": 900,
    "audit_evidence": 480,
    "market_mapping": 600,
    "research": 900,
    "technical": 300,
    "report": 600,
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


def _checkpoint_file_mtime(ckpt: dict, file_key: str) -> float:
    path = ckpt.get("files", {}).get(file_key, "")
    if not path:
        return 0.0
    try:
        from pathlib import Path

        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _checkpoint_older_than_any(ckpt: dict, file_key: str, upstream_keys: tuple[str, ...]) -> bool:
    target_mtime = _checkpoint_file_mtime(ckpt, file_key)
    if not target_mtime:
        return False
    return any(_checkpoint_file_mtime(ckpt, upstream_key) > target_mtime for upstream_key in upstream_keys)


def _parse_flags(argv: list[str]) -> tuple[bool, bool]:
    force_run = "--force" in argv or "--force-run" in argv
    force_restart = "--force" in argv or "--force-restart" in argv
    return force_run, force_restart


def _resolve_start_stage(resume_from: str) -> str | None:
    return CONTROL_STAGE_START.get(resume_from, "collect_intel")


def _build_context(now: datetime, run_ts: str, ckpt: dict, resume_from: str) -> dict[str, Any]:
    return {
        "now": now,
        "run_ts": run_ts,
        "ckpt": ckpt,
        "resume_from": resume_from,
        "backtrack": "",
        "p5_evidence": "",
        "p1_results": [],
        "p1_stats": {},
        "p2_data": "",
        "p3_data": "",
        "p3_stats": {},
        "p4_data": "",
        "p6_mapping": "",
        "p7_deep": "",
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
    }


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


def _persist_p1_artifacts(
    p1_results: list[dict[str, Any]],
    evidence_raw: str,
    p1_failed_urls: list[dict[str, Any]],
    p1_duration: float,
    p1_stats: dict[str, Any],
) -> None:
    if not p1_results:
        return

    p1_json_path = write_json_artifact("P1_RSS原始数据", p1_results)
    p1_md_path = write_text_artifact("P1_RSS原始数据", evidence_raw)
    update_run_manifest(
        "p1",
        status="completed",
        files={"json": p1_json_path, "md": p1_md_path},
        metrics={
            "records": len(p1_results),
            "failed_urls": len(p1_failed_urls),
            "evidence_chars": len(evidence_raw),
            "duration_seconds": round(p1_duration, 1),
            **p1_stats,
        },
    )


def _persist_p3_artifacts(p3_data: str, p3_stats: dict[str, Any]) -> None:
    if not p3_data:
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
        status="completed",
        files=p3_files,
        metrics={
            "content_chars": len(p3_data),
            "duration_seconds": p3_stats.get("duration_seconds", 0),
            "jinnang_1_count": p3_stats.get("jinnang_1_count", 0),
            "jinnang_2_count": p3_stats.get("jinnang_2_count", 0),
            "jinnang_3_count": p3_stats.get("jinnang_3_count", 0),
            "structured_items": p3_stats.get("structured_items", 0),
        },
    )


def _stage_collect_intel(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]
    now = context["now"]

    if resume_from not in {"none", "p1"}:
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

    p1_results: list[dict[str, Any]] = []
    rss_stats: dict[str, Any] = {}
    web_stats: dict[str, Any] = {}
    p2_data = ""
    p3_data = ""
    p3_stats: dict[str, Any] = {}
    p1_failed_urls: list[dict[str, Any]] = []
    collect_started = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rss_future = pool.submit(collect_from_rss, context["backtrack"])
        web_future = pool.submit(collect_from_web, context["backtrack"])
        x_future = pool.submit(collect_from_x, context["backtrack"])
        p3_future = pool.submit(collect_p3_parallel_only, context["backtrack"])

        try:
            # Why: 加 timeout 防止单个采集任务挂死拖住整个阶段
            rss_results, rss_stats = rss_future.result(timeout=300)
            p1_results.extend(rss_results)
            p1_failed_urls.extend(rss_stats.get("failed_urls", []))
        except Exception as exc:
            logging.warning("RSS collection failed: %s", exc)

        try:
            web_results, web_stats = web_future.result(timeout=300)
            p1_results.extend(web_results)
        except Exception as exc:
            logging.warning("web collection failed: %s", exc)

        try:
            p2_data, _ = x_future.result(timeout=300)
        except Exception as exc:
            logging.warning("X collection failed: %s", exc)

        try:
            p3_data, p3_stats = p3_future.result(timeout=300)
        except Exception as exc:
            logging.warning("P3 collection failed: %s", exc)

    evidence_raw = format_evidence(p1_results) if p1_results else ""
    p1_duration = time.time() - collect_started
    p1_stats = _build_p1_stats(rss_stats, web_stats, p1_failed_urls)

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

    _persist_p1_artifacts(p1_results, evidence_raw, p1_failed_urls, p1_duration, p1_stats)
    _persist_p3_artifacts(p3_data, p3_stats)

    if p1_failed_urls:
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


def _stage_audit_evidence(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]

    if resume_from in {"none", "p1"}:
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
        if not context["p5_evidence"].strip() and context.get("evidence_raw", "").strip():
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
                },
            )
        return

    context["p5_evidence"] = _read_text_checkpoint(ckpt["files"].get("p5", ""), "P5 audit result")
    if not context["p2_data"] or not context["p3_data"]:
        context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)


def _stage_market_mapping(context: dict[str, Any]) -> None:
    resume_from = context["resume_from"]
    ckpt = context["ckpt"]

    if resume_from in {"p6", "p8_resume", "p9_resume"} and ckpt["files"].get("p6"):
        if _checkpoint_older_than_any(ckpt, "p6", ("p5", "p4", "p3", "p2")):
            logging.warning("[P6] checkpoint is older than upstream evidence; rerunning mapping")
        else:
            context["p6_mapping"] = _read_text_checkpoint(ckpt["files"].get("p6", ""), "P6 mapping")
            context["p5_evidence"] = context["p5_evidence"] or _read_text_checkpoint(ckpt["files"].get("p5", ""), "P5 audit")
            context["p4_data"] = _read_text_checkpoint(ckpt["files"].get("p4", ""), "P4 market snapshot")
            if not context["p2_data"] or not context["p3_data"]:
                context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)
            context["p7_deep"] = _read_text_checkpoint(ckpt["files"].get("p7", ""), "P7 deep research")
            context["p7a_audited"] = _read_text_checkpoint(ckpt["files"].get("p7a", ""), "P7a audited research")
            context["p75_supplement"] = _read_text_checkpoint(ckpt["files"].get("p75", ""), "P7.5 supplement")
            context["price_data"] = _read_text_checkpoint(ckpt["files"].get("p8", ""), "P8 technical data")
            return

    if not context.get("p5_evidence") and ckpt["files"].get("p5"):
        context["p5_evidence"] = _read_text_checkpoint(ckpt["files"].get("p5", ""), "P5 audit")
    if not context["p2_data"] or not context["p3_data"]:
        context["p2_data"], context["p3_data"] = _recover_p2_p3_from_checkpoint(ckpt)

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

    with log_heartbeat("P6 mapping"):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(generate_report, combined_evidence)
                context["p6_mapping"] = future.result(timeout=600)
        except concurrent.futures.TimeoutError:
            logging.error("[P6] mapping timed out")
            context["p6_mapping"] = ""
        except Exception as exc:
            logging.error("[P6] mapping failed: %s", exc)
            context["p6_mapping"] = ""

    if context["p6_mapping"]:
        p6_path = write_text_artifact("P6_映射", context["p6_mapping"])
        update_run_manifest(
            "p6",
            status="completed",
            files={"md": p6_path},
            metrics={"mapping_chars": len(context["p6_mapping"])},
        )


def _stage_research(context: dict[str, Any]) -> None:
    if not context.get("p6_mapping"):
        return

    resume_from = context["resume_from"]
    ckpt = context["ckpt"]
    try:
        research_plan = build_research_plan(
            context["p6_mapping"],
            p4_data=context.get("p4_data", ""),
            p5_evidence=context.get("p5_evidence", ""),
        )
    except Exception as exc:
        logging.error("[P7] research plan failed; skipping optional research layers: %s", exc)
        update_run_manifest(
            "research",
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

    if resume_from in {"p8_resume", "p9_resume"}:
        if context.get("p7_deep", "").strip():
            need_run_p7 = False
        if ckpt["files"].get("p75") or not search_queries:
            need_run_p75 = False

    p75_started = None
    if need_run_p7 or need_run_p75:
        with log_heartbeat("P7/P7.5 research"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                p7_future = pool.submit(run_deep_research, research_plan) if need_run_p7 else None
                supplement_results: list[str] = []
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
                            if text and text.lower().replace(".", "") not in ("not found", "no results", "no relevant results"):
                                supplement_results.append(text)
                        except Exception as exc:
                            logging.error("[P7.5] supplement search failed: %s", exc)

                if p7_future is not None:
                    try:
                        p7_result = p7_future.result(timeout=600)
                        context["p7_deep"] = p7_result.get("text", "")
                    except concurrent.futures.TimeoutError:
                        logging.warning("[P7] deep research timed out")
                        context["p7_deep"] = ""
                    except Exception as exc:
                        logging.error("[P7] deep research failed: %s", exc)
                        context["p7_deep"] = ""

                if need_run_p75:
                    if supplement_results:
                        raw_text = "\n".join(supplement_results)
                        context["p75_raw_text"] = raw_text
                        raw_path = write_text_artifact("P7.5_补充搜索原始数据", raw_text)
                        update_run_manifest(
                            "p75",
                            status="raw_ready",
                            files={"raw": raw_path},
                            metrics={"queries": len(capped_queries), "raw_chars": len(raw_text)},
                        )
                        with log_heartbeat("P7.5 audit"):
                            audited = gemini_pro_audit(raw_text, audit_type="p7.5", backtrack="")
                        context["p75_supplement"] = audited or raw_text
                    else:
                        context["p75_supplement"] = ""
                        update_run_manifest(
                            "p75",
                            status="skipped_no_results",
                            metrics={"queries": len(capped_queries), "raw_chars": 0},
                        )

    if p75_started is not None:
        context["p75_duration"] = time.time() - p75_started

    if context.get("p7_deep"):
        p7_body = (
            "# P7 深度研究结果\n\n"
            f"> generated_at: {datetime.now().isoformat()}\n\n"
            f"> goal: {research_plan.get('goal', '')}\n"
            f"> triggers: {', '.join(research_plan.get('triggers', [])) or 'none'}\n\n"
            f"{context['p7_deep']}"
        )
        p7_path = write_text_artifact("P7_深度研究", p7_body)
        p7_files = {"md": p7_path}
        if context["p7_plan_path"]:
            p7_files["plan"] = context["p7_plan_path"]
        update_run_manifest(
            "p7",
            status="completed",
            files=p7_files,
            metrics={"output_chars": len(context["p7_deep"])},
        )

    if context.get("p7a_audited"):
        return
    if context.get("p7_deep", "").strip() and len(context["p7_deep"].strip()) > 200:
        with log_heartbeat("P7a audit"):
            try:
                context["p7a_audited"] = p7a_deep_research_audit(context["p7_deep"])
            except Exception as exc:
                logging.error("[P7a] audit failed unexpectedly; passing through raw P7 data: %s", exc)
                update_run_manifest(
                    "p7a",
                    status="failed_exception_passthrough",
                    metrics={
                        "input_chars": len(context.get("p7_deep", "") or ""),
                        "fallback_reason": str(exc),
                    },
                )
                context["p7a_audited"] = context.get("p7_deep", "")
    else:
        context["p7a_audited"] = context.get("p7_deep", "")

    context["search_queries"] = search_queries[:5]


def _stage_technical(context: dict[str, Any]) -> None:
    try:
        if context["resume_from"] == "p9_resume" and context.get("price_data", "").strip():
            current_stocks = extract_stocks_from_report(context.get("p6_mapping", ""))
            stale_p8 = _checkpoint_older_than_any(context.get("ckpt", {}), "p8", ("p6",))
            stale_no_stock_p8 = "未从 P6 映射中提取到 A 股标的" in context.get("price_data", "")
            if not (stale_p8 or (current_stocks and stale_no_stock_p8)):
                logging.info("reusing P8 technical data from checkpoint")
                return
            logging.warning(
                "[P8] checkpoint is stale or no-stock while current P6 exposes %s stocks; rerunning technical layer",
                len(current_stocks),
            )

        with log_heartbeat("P8 technical"):
            stocks = current_stocks if "current_stocks" in locals() else extract_stocks_from_report(context.get("p6_mapping", ""))
            tracks = extract_stock_tracks(context.get("p6_mapping", ""))
            elasticity = extract_stock_elasticity(context.get("p6_mapping", ""))
            context["price_data"] = p8_search_stock_prices(stocks, tracks, elasticity)
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
            },
        )


def _read_log_excerpt(limit_chars: int = 4000) -> str:
    try:
        with open(get_pipeline_log_path(), "r", encoding=UTF8_FILE_ENCODING) as f:
            return f.read()[-limit_chars:]
    except Exception:
        return ""


def diagnose_failure_and_record(stage_name: str, error_text: str, manifest: dict[str, Any], log_excerpt: str = "") -> dict:
    result = diagnose_failure(stage_name, error_text, manifest, log_excerpt)
    update_run_manifest(
        "agent_diagnose",
        status="completed" if result.get("ok") else "skipped",
        files={
            key: value
            for key, value in {
                "json": result.get("json_path", ""),
                "md": result.get("md_path", ""),
            }.items()
            if value
        } or None,
        metrics={
            "ok": bool(result.get("ok")),
            "reason": result.get("reason", ""),
            "stage": stage_name,
        },
    )
    return result


def _run_agent_failure_diagnosis(stage_name: str, exc: Exception) -> None:
    manifest = load_run_manifest()
    log_excerpt = _read_log_excerpt()
    result = diagnose_failure_and_record(stage_name, str(exc), manifest, log_excerpt=log_excerpt)
    update_run_manifest(
        "agent_diagnose",
        status="completed" if result.get("ok") else "skipped",
        files={
            key: value
            for key, value in {
                "json": result.get("json_path", ""),
                "md": result.get("md_path", ""),
            }.items()
            if value
        } or None,
        metrics={
            "ok": bool(result.get("ok")),
            "reason": result.get("reason", ""),
            "stage": stage_name,
        },
    )
    if result.get("json_path"):
        logging.info("agent diagnosis artifact saved: %s", result["json_path"])


def review_final_report_and_record(report_md: str, context: dict[str, Any]) -> dict:
    review_context = {
        "p5_evidence": context.get("p5_evidence", "")[:4000],
        "p6_mapping": context.get("p6_mapping", "")[:4000],
        "p7a_audited": context.get("p7a_audited", "")[:4000],
        "p75_supplement": context.get("p75_supplement", "")[:4000],
        "price_data": context.get("price_data", "")[:4000],
    }
    result = review_final_report(report_md, review_context)
    update_run_manifest(
        "agent_review",
        status="completed" if result.get("ok") else "skipped",
        files={
            key: value
            for key, value in {
                "json": result.get("json_path", ""),
                "md": result.get("md_path", ""),
            }.items()
            if value
        } or None,
        metrics={
            "ok": bool(result.get("ok")),
            "reason": result.get("reason", ""),
            "report_chars": len(report_md or ""),
        },
    )
    return result


def _run_agent_report_review(context: dict[str, Any]) -> None:
    report_md = context.get("final_report", "")
    if not report_md or not report_md.strip():
        update_run_manifest(
            "agent_review",
            status="skipped",
            metrics={"ok": False, "reason": "empty_report"},
        )
        return
    result = review_final_report_and_record(report_md, context)
    update_run_manifest(
        "agent_review",
        status="completed" if result.get("ok") else "skipped",
        files={
            key: value
            for key, value in {
                "json": result.get("json_path", ""),
                "md": result.get("md_path", ""),
            }.items()
            if value
        } or None,
        metrics={
            "ok": bool(result.get("ok")),
            "reason": result.get("reason", ""),
            "report_chars": len(report_md),
        },
    )
    if result.get("json_path"):
        logging.info("agent review artifact saved: %s", result["json_path"])


def _stage_report(context: dict[str, Any]) -> None:
    with log_heartbeat("P9 report"):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    p9_risk_analysis,
                    p5_evidence=context.get("p5_evidence", ""),
                    p7_deep=context.get("p7a_audited", ""),
                    p75_supplement=context.get("p75_supplement", ""),
                    p6_mapping=context.get("p6_mapping", ""),
                    price_data=context.get("price_data", ""),
                    p2_x_intel_data=context.get("p2_data", ""),
                    p3_intel_data=context.get("p3_data", ""),
                    p4_yfinance=context.get("p4_data", ""),
                )
                final_report, report_ts = future.result(timeout=600)
        except concurrent.futures.TimeoutError:
            logging.error("[P9] report generation timed out")
            raise RuntimeError("report generation timed out") from None
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

    _run_agent_report_review(context)


def _stage_roi(context: dict[str, Any]) -> None:
    ckpt = context["ckpt"]
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

    roi_payload = build_layer_roi_report(
        p1_results=p1_results,
        p3_raw_results=context.get("p3_stats", {}).get("raw_results") if isinstance(context.get("p3_stats"), dict) else None,
        p3_text=context.get("p3_data", ""),
        p4_text=context.get("p4_data", ""),
        p75_text=context.get("p75_supplement", "") or context.get("p75_raw_text", ""),
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
            },
        },
    )

    roi_json_path = write_json_artifact("Layer_ROI", roi_payload)
    roi_md_path = write_text_artifact("Layer_ROI", render_layer_roi_markdown(roi_payload))
    update_run_manifest(
        "roi",
        status="completed",
        files={"json": roi_json_path, "md": roi_md_path},
        metrics={"layers_evaluated": len(roi_payload.get("layers", {}))},
    )

    manifest_after_roi = load_run_manifest()
    for stage_name, payload in roi_payload.get("layers", {}).items():
        stage_status = manifest_after_roi.get("stages", {}).get(stage_name, {}).get("status", "completed")
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
            description="P7/P7a/P7.5 research pipeline",
        ),
        StageSpec(
            name="technical",
            handler=_stage_technical,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["technical"],
            description="P8 technical layer",
        ),
        StageSpec(
            name="report",
            handler=_stage_report,
            timeout_seconds=CONTROL_STAGE_TIMEOUTS["report"],
            description="P9 report generation",
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

    resume_from = ckpt["stage"]
    start_at = _resolve_start_stage(resume_from)
    completed_after_resume = resume_from not in {"", "none", "p9_done"}
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
        try:
            stage_name = exc.stage_name if isinstance(exc, StageTimeoutError) else context.get("current_stage", start_at or "orchestrator")
            _run_agent_failure_diagnosis(stage_name, exc)
        except Exception as diagnose_exc:
            logging.warning("agent failure diagnosis skipped: %s", diagnose_exc)
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
