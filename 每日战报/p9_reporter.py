import logging
import re
from datetime import datetime
from typing import Any

from artifact_store import get_run_timestamp, update_run_manifest, write_json_artifact, write_text_artifact
from p9_email import send_report_via_email
from p9_rendering import build_report_markdown
from settings import EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL, opus_adapter, require_prompt
from utils import extract_json_from_llm_response


def _section_or_fallback(value: str | None, fallback: str) -> str:
    if value and len(value.strip()) > 50:
        return value.strip()
    return fallback


def p9_risk_analysis(
    p5_evidence,
    p7_deep,
    p75_supplement,
    p6_mapping=None,
    price_data=None,
    p2_x_intel_data=None,
    p3_intel_data="",
    p4_yfinance="",
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
    p2_section = _section_or_fallback(p2_x_intel_data, "（本次运行无 P2 X 平台情报数据）")
    p3_section = _section_or_fallback(p3_intel_data, "（本次运行无 P3 机动情报数据）")
    yfinance_section = _section_or_fallback(p4_yfinance, "（本次运行无 P4 海外行情数据）")

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
        p6_mapping=p6_mapping,
        p75_section=p75_section,
        price_data=price_data,
        p2_section=p2_section,
        p3_section=p3_section,
        yfinance_section=yfinance_section,
    )

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
        data = extract_json_from_llm_response(raw_content, expect_type="dict")
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
                metrics={"markdown_chars": len(result), "raw_output_chars": len(raw_content)},
            )
        except Exception as exc:
            logging.error("[P9] failed to save report JSON: %s", exc)
            update_run_manifest(
                "p9",
                status="json_ready",
                files={"thinking": out_fn} if out_fn else None,
                metrics={"markdown_chars": len(result), "raw_output_chars": len(raw_content)},
            )

        return result, run_ts
    except Exception as exc:
        logging.error("[P9] failed to parse model output as JSON, falling back to raw text: %s", exc)
        update_run_manifest(
            "p9",
            status="fallback_raw_llm",
            files={"thinking": out_fn} if out_fn else None,
            metrics={"raw_output_chars": len(raw_content), "error": str(exc)},
        )
        return raw_content, run_ts


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
