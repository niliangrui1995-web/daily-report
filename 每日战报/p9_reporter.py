import json
import logging
import re
from datetime import datetime
from typing import Any

from artifact_store import get_run_timestamp, load_run_manifest, update_run_manifest, write_json_artifact, write_text_artifact
from p9_email import send_report_via_email
from p9_rendering import build_report_markdown
from settings import EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL, opus_adapter, require_prompt
from utils import extract_json_from_llm_response

_HARD_EVIDENCE_LAYERS = {"P4", "P5", "P6", "P7", "P7A", "P8"}
_SOFT_EVIDENCE_LAYERS = {"P2", "P3", "P7.5"}
_PRIORITY_RANKS = {"★": 1.0, "★☆☆": 1.5, "★★": 2.0, "★★☆": 2.5, "★★★": 3.0}
P9_JSON_RETRY_LIMIT = 1
P9_OUTPUT_LIMITS = """
JSON output hard limits:
- Return raw JSON only. Do not wrap it in Markdown fences.
- x_intel_snipe: max 3 items.
- p3_machine_intel: max 3 items.
- core_signals: max 8 items.
- sniper_tables: max 8 tracks, max 3 targets per track.
- fund_pathway: max 3 strings.
- today_advice: max 8 items.
- risk_warning: max 5 strings.
- Prefer fewer complete items over long output. Never stop mid-object or mid-string.
"""
_REQUIRED_REPORT_KEYS = (
    "x_intel_snipe",
    "p3_machine_intel",
    "core_signals",
    "sniper_tables",
    "fund_pathway",
    "today_advice",
    "risk_warning",
)
_MISSING_FIELD_VALUES = {"", "无", "未提供", "-", "N/A", "n/a", "None", "null"}


def _parse_report_payload(raw_content: str) -> dict[str, Any]:
    try:
        return extract_json_from_llm_response(raw_content, expect_type="dict")
    except json.JSONDecodeError:
        data = extract_json_from_llm_response(raw_content, expect_type="dict", allow_partial=True)

    missing = [key for key in _REQUIRED_REPORT_KEYS if key not in data]
    wrong_type = [key for key in _REQUIRED_REPORT_KEYS if key in data and not isinstance(data.get(key), list)]
    if missing or wrong_type:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if wrong_type:
            detail.append("wrong_type=" + ",".join(wrong_type))
        raise json.JSONDecodeError("invalid P9 report payload: " + "; ".join(detail), raw_content[:200], 0)
    return data


def _section_or_fallback(value: str | None, fallback: str) -> str:
    if value and len(value.strip()) > 50:
        return value.strip()
    return fallback


def _normalize_stock_code(value: Any) -> str:
    match = re.search(r"\d{6}", str(value or ""))
    return match.group(0) if match else ""


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _is_missing_field(value: Any) -> bool:
    return value is None or str(value).strip() in _MISSING_FIELD_VALUES


def _format_price(value: Any) -> str | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return f"{number:.2f}"


def _format_signed_pct(value: Any) -> str | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return f"{number:+.1f}%"


def _format_percentile(value: Any) -> str | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return f"{round(number):.0f}%"


def _load_p8_price_db() -> dict[str, Any]:
    try:
        manifest = load_run_manifest()
        p8_files = manifest.get("stages", {}).get("p8", {}).get("files", {})
        json_path = p8_files.get("json")
        if not json_path:
            return {}
        with open(json_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logging.warning("[P9] failed to load P8 price JSON for backfill: %s", exc)
        return {}


def _extract_p8_fields(item: dict[str, Any]) -> dict[str, str]:
    price_data = item.get("price_data") if isinstance(item.get("price_data"), dict) else {}
    tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
    rs = item.get("relative_strength") if isinstance(item.get("relative_strength"), dict) else {}
    weekly = item.get("weekly_level") if isinstance(item.get("weekly_level"), dict) else {}

    fields: dict[str, str] = {}
    price = _format_price(price_data.get("latest_close", item.get("latest_close")))
    chg_3m = _format_signed_pct(price_data.get("chg_3m_pct", item.get("chg_3m_pct")))
    percentile = _format_percentile(price_data.get("percentile_250d", item.get("percentile_250d")))

    if price:
        fields["price"] = price
    if chg_3m:
        fields["chg_3m"] = chg_3m
    if percentile:
        fields["percentile_250d"] = percentile

    vol_desc = tags.get("vol_desc")
    if vol_desc:
        fields["volume"] = str(vol_desc)

    rs_20 = rs.get("rs_excess_20d", item.get("rs_20d_pct"))
    rs_60 = rs.get("rs_excess_60d", item.get("rs_60d_pct"))
    rs_desc = rs.get("rs_desc")
    if rs_20 is not None and rs_60 is not None:
        suffix = f"，{rs_desc}" if rs_desc else ""
        fields["rs_strength"] = f"20日超额{_format_signed_pct(rs_20)}，60日超额{_format_signed_pct(rs_60)}{suffix}"
    elif rs_20 is not None:
        fields["rs_strength"] = f"20日超额{_format_signed_pct(rs_20)}"

    weekly_status = weekly.get("weekly_macd_status")
    weekly_position = weekly.get("weekly_ma20_position")
    weekly_trend = weekly.get("weekly_trend", item.get("weekly_trend"))
    weekly_parts = [str(value) for value in (weekly_status, weekly_position, weekly_trend) if value]
    if weekly_parts:
        fields["weekly_level"] = "，".join(weekly_parts)

    return fields


def _backfill_sniper_tables_from_p8(data: dict[str, Any], p8_db: dict[str, Any] | None = None) -> dict[str, int]:
    p8_db = p8_db if p8_db is not None else _load_p8_price_db()
    if not p8_db:
        return {"p8_backfilled_targets": 0, "p8_price_db_size": 0}

    backfilled = 0
    for table in data.get("sniper_tables", []) or []:
        targets = table.get("targets", []) if isinstance(table, dict) else []
        for row in targets:
            if not isinstance(row, dict):
                continue
            code = _normalize_stock_code(row.get("code"))
            item = p8_db.get(code)
            if not isinstance(item, dict):
                continue
            fields = _extract_p8_fields(item)
            changed = False
            for key, value in fields.items():
                if key in {"price", "chg_3m", "percentile_250d"} or _is_missing_field(row.get(key)):
                    if row.get(key) != value:
                        row[key] = value
                        changed = True
            if changed:
                backfilled += 1

    return {"p8_backfilled_targets": backfilled, "p8_price_db_size": len(p8_db)}


def _normalize_layer_tag(ref: Any) -> str | None:
    text = re.sub(r"\s+", "", str(ref or "")).upper()
    if not text:
        return None
    if re.match(r"^P(?:7\.5|75)(?:[:：\-].*)?$", text):
        return "P7.5"
    if re.match(r"^P7A(?:[:：\-].*)?$", text):
        return "P7A"
    match = re.match(r"^(P[2-9])(?:[:：\-].*)?$", text)
    if match:
        return match.group(1)
    return None


def _extract_evidence_layers(evidence_refs: Any) -> list[str]:
    if not isinstance(evidence_refs, list):
        if evidence_refs in (None, ""):
            return []
        evidence_refs = [evidence_refs]

    layers: list[str] = []
    seen: set[str] = set()
    for ref in evidence_refs:
        layer = _normalize_layer_tag(ref)
        if not layer or layer in seen:
            continue
        seen.add(layer)
        layers.append(layer)
    return layers


def _normalize_confidence_value(raw_value: Any) -> float | None:
    if raw_value in (None, ""):
        return None
    try:
        text = str(raw_value).strip()
        is_percent = text.endswith("%")
        value = float(text.rstrip("%"))
        if is_percent or value > 1:
            value /= 100.0
        return max(0.0, min(value, 1.0))
    except (TypeError, ValueError):
        return None


def _confidence_cap_for_layers(layers: list[str]) -> float:
    unique_count = len(layers)
    hard_count = sum(1 for layer in layers if layer in _HARD_EVIDENCE_LAYERS)

    if unique_count == 0:
        return 0.55
    if unique_count == 1:
        return 0.60
    if hard_count == 0:
        return 0.68
    if hard_count >= 2:
        return 0.88
    return 0.82


def _downgrade_significance(significance: Any, confidence_cap: float) -> str | Any:
    if significance in (None, "") or confidence_cap > 0.68:
        return significance
    text = str(significance).strip()
    high_markers = ("高", "🟢", "🔥", "优先")
    if any(marker in text for marker in high_markers):
        return "🟡中"
    return significance


def _downgrade_priority(priority: Any, confidence_cap: float) -> str | Any:
    if priority in (None, ""):
        return priority

    try:
        current_rank = _PRIORITY_RANKS[str(priority)]
    except KeyError:
        return priority

    max_rank = 3.0
    if confidence_cap <= 0.60:
        max_rank = _PRIORITY_RANKS["★★"]
    elif confidence_cap <= 0.68:
        max_rank = _PRIORITY_RANKS["★★☆"]

    if current_rank <= max_rank:
        return priority

    for label, rank in sorted(_PRIORITY_RANKS.items(), key=lambda item: item[1], reverse=True):
        if rank <= max_rank:
            return label
    return priority


def _apply_evidence_guardrails(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    metrics = {
        "confidence_caps_applied": 0,
        "priority_downgrades": 0,
        "significance_downgrades": 0,
    }

    for item in data.get("core_signals", []) or []:
        if not isinstance(item, dict):
            continue
        layers = _extract_evidence_layers(item.get("evidence_refs"))
        confidence_cap = _confidence_cap_for_layers(layers)
        confidence_value = _normalize_confidence_value(item.get("confidence"))
        if confidence_value is not None:
            capped = round(min(confidence_value, confidence_cap), 2)
            if abs(capped - confidence_value) > 1e-9:
                metrics["confidence_caps_applied"] += 1
            item["confidence"] = capped

        downgraded_significance = _downgrade_significance(item.get("significance"), confidence_cap)
        if downgraded_significance != item.get("significance"):
            metrics["significance_downgrades"] += 1
            item["significance"] = downgraded_significance

    for item in data.get("today_advice", []) or []:
        if not isinstance(item, dict):
            continue
        layers = _extract_evidence_layers(item.get("evidence_refs"))
        confidence_cap = _confidence_cap_for_layers(layers)
        confidence_value = _normalize_confidence_value(item.get("confidence"))
        if confidence_value is not None:
            capped = round(min(confidence_value, confidence_cap), 2)
            if abs(capped - confidence_value) > 1e-9:
                metrics["confidence_caps_applied"] += 1
            item["confidence"] = capped

        downgraded_priority = _downgrade_priority(item.get("priority"), confidence_cap)
        if downgraded_priority != item.get("priority"):
            metrics["priority_downgrades"] += 1
            item["priority"] = downgraded_priority

    return data, metrics


def p9_risk_analysis(
    p5_evidence,
    p7_deep,
    p75_supplement,
    p6_mapping=None,
    price_data=None,
    p2_x_intel_data=None,
    p3_intel_data="",
    p4_yfinance="",
    _retry_count: int = 0,
):
    """
    Build the final report and return a stable run timestamp for downstream writes.
    """
    run_ts = get_run_timestamp()

    if not price_data or len(price_data) < 50:
        logging.warning("[P9] price data is insufficient; the technical section may be incomplete")
        price_data = "（本次运行未获取到可用的技术面价格数据）"

    logging.info("[P9] building final report")

    if p7_deep:
        p7_deep = re.sub(
            r"\n---\n(?:摘要提取|正文深挖).*?(?=\n###|\Z)",
            "",
            p7_deep,
            flags=re.DOTALL,
        )
    if p6_mapping:
        p6_mapping = re.sub(r"^(?:SEARCH|DEEP_RESEARCH):\s*.*$", "", p6_mapping, flags=re.MULTILINE).strip()

    today_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")
    p7_section = _section_or_fallback(p7_deep, "（本次运行无 P7 深度研究数据）")
    p75_section = _section_or_fallback(p75_supplement, "（本次运行无 P7.5 补充搜索数据）")
    p6_section = _section_or_fallback(p6_mapping, "（本次运行无 P6 市场映射数据）")
    p2_section = _section_or_fallback(p2_x_intel_data, "（本次运行无 P2 X 平台情报数据）")
    p3_section = _section_or_fallback(p3_intel_data, "（本次运行无 P3 机动情报数据）")
    yfinance_section = _section_or_fallback(p4_yfinance, "（本次运行无 P4 核心标的行情数据）")

    try:
        prompt_template = require_prompt("p9_report.txt")
    except FileNotFoundError as exc:
        logging.error("[P9] required prompt is missing: %s", exc)
        update_run_manifest("p9", status="failed_missing_prompt", metrics={"error": str(exc)})
        return "", run_ts

    prompt = prompt_template.format(
        today_str=today_str,
        p5_evidence=p5_evidence,
        p7_section=p7_section,
        p6_mapping=p6_section,
        p75_section=p75_section,
        price_data=price_data,
        p2_section=p2_section,
        p3_section=p3_section,
        yfinance_section=yfinance_section,
    )
    if _retry_count:
        prompt = (
            f"{prompt}\n\n{P9_OUTPUT_LIMITS}\n"
            "The previous attempt returned invalid or incomplete JSON. "
            "Shorten the content further and return one complete JSON object only."
        )
    else:
        prompt = f"{prompt}\n\n{P9_OUTPUT_LIMITS}"

    resp = opus_adapter.invoke(prompt)
    raw_content = getattr(resp, "content", "") or ""
    thinking_content = getattr(resp, "thinking_content", "") or ""

    out_fn = ""
    if thinking_content:
        try:
            out_fn = write_text_artifact("P9_Opus推理链", thinking_content)
            logging.info("[P9] saved reasoning trace to %s", out_fn)
        except Exception as exc:
            logging.error("[P9] failed to save reasoning trace: %s", exc)

    if not raw_content.strip():
        logging.error("[P9] model returned empty output")
        update_run_manifest(
            "p9",
            status="failed_empty_output",
            files={"thinking": out_fn} if out_fn else None,
            metrics={"price_data_chars": len(price_data or "")},
        )
        return "", run_ts

    try:
        data = _parse_report_payload(raw_content)
        data, guardrail_metrics = _apply_evidence_guardrails(data)
        if any(guardrail_metrics.values()):
            logging.info("[P9] applied evidence guardrails: %s", guardrail_metrics)
        p8_backfill_metrics = _backfill_sniper_tables_from_p8(data)
        if p8_backfill_metrics.get("p8_backfilled_targets"):
            logging.info("[P9] backfilled sniper table fields from P8: %s", p8_backfill_metrics)
        result = build_report_markdown(data, today_str)
        logging.info("[P9] rendered markdown report (%s chars)", len(result))

        try:
            json_fname = write_json_artifact("战报", data)
            manifest_files = {"json": json_fname}
            if out_fn:
                manifest_files["thinking"] = out_fn
            update_run_manifest(
                "p9",
                status="json_ready",
                files=manifest_files,
                metrics={
                    "markdown_chars": len(result),
                    "raw_output_chars": len(raw_content),
                    **guardrail_metrics,
                    **p8_backfill_metrics,
                },
            )
        except Exception as exc:
            logging.error("[P9] failed to save report JSON: %s", exc)
            update_run_manifest(
                "p9",
                status="json_ready",
                files={"thinking": out_fn} if out_fn else None,
                metrics={
                    "markdown_chars": len(result),
                    "raw_output_chars": len(raw_content),
                    **guardrail_metrics,
                    **p8_backfill_metrics,
                },
            )

        return result, run_ts
    except Exception as exc:
        logging.error("[P9] failed to parse model output as JSON; raw output will not be delivered as report: %s", exc)
        raw_path = ""
        try:
            raw_path = write_text_artifact("P9_raw_invalid_output", raw_content)
        except Exception as write_exc:
            logging.error("[P9] failed to save invalid raw output: %s", write_exc)
        manifest_files = {
            key: value
            for key, value in {
                "raw": raw_path,
                "thinking": out_fn,
            }.items()
            if value
        } or None
        if _retry_count < P9_JSON_RETRY_LIMIT:
            logging.warning("[P9] retrying report JSON generation after parse failure")
            update_run_manifest(
                "p9",
                status="retrying_parse",
                files=manifest_files,
                metrics={"raw_output_chars": len(raw_content), "error": str(exc), "retry_count": _retry_count},
            )
            return p9_risk_analysis(
                p5_evidence,
                p7_deep,
                p75_supplement,
                p6_mapping=p6_mapping,
                price_data=price_data,
                p2_x_intel_data=p2_x_intel_data,
                p3_intel_data=p3_intel_data,
                p4_yfinance=p4_yfinance,
                _retry_count=_retry_count + 1,
            )
        update_run_manifest(
            "p9",
            status="failed_parse",
            files=manifest_files,
            metrics={"raw_output_chars": len(raw_content), "error": str(exc), "retry_count": _retry_count},
        )
        return "", run_ts


def send_report_email(content, now_str: str = "") -> dict[str, Any]:
    """
    Save the final report and optionally deliver it by email.
    """
    if not content or not content.strip():
        logging.error("[P9] final report is empty; skipping save and delivery")
        status = "failed_empty_report"
        update_run_manifest("p9", status=status)
        return {"ok": False, "status": status, "report_path": "", "email_sent": False}

    now = datetime.now()
    file_ts = now_str or get_run_timestamp()
    try:
        fname = write_text_artifact("战报", content, timestamp=file_ts)
        logging.info("[P9] saved report to %s", fname)
    except Exception as exc:
        logging.error("[P9] failed to save report markdown: %s", exc)
        status = "failed_md_write"
        update_run_manifest("p9", status=status, metrics={"error": str(exc)})
        return {"ok": False, "status": status, "report_path": "", "email_sent": False}

    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL]):
        logging.warning("[P9] email is not configured; keeping local report only")
        status = "completed_no_email"
        update_run_manifest(
            "p9",
            status=status,
            files={"md": fname},
            metrics={"email_configured": False},
        )
        return {"ok": True, "status": status, "report_path": fname, "email_sent": False}

    try:
        send_report_via_email(
            content,
            sender_email=EMAIL_ADDRESS,
            sender_password=EMAIL_PASSWORD,
            receiver_email=RECEIVER_EMAIL,
            now=now,
        )
        logging.info("[P9] email delivered")
        status = "completed_email_sent"
        update_run_manifest(
            "p9",
            status=status,
            files={"md": fname},
            metrics={"email_configured": True, "email_sent": True},
        )
        return {"ok": True, "status": status, "report_path": fname, "email_sent": True}
    except Exception as exc:
        logging.error("[P9] email delivery failed: %s", exc)
        status = "completed_email_failed"
        update_run_manifest(
            "p9",
            status=status,
            files={"md": fname},
            metrics={"email_configured": True, "email_sent": False, "error": str(exc)},
        )
        return {"ok": False, "status": status, "report_path": fname, "email_sent": False}
