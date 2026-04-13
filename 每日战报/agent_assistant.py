# -*- coding: utf-8 -*-
"""Optional agent assistant hooks for preflight explanation, failure diagnosis, and report review."""

from __future__ import annotations

import json
import logging
from typing import Any

from artifact_store import write_json_artifact, write_text_artifact
from settings import AGENT_ASSIST_ENABLED, get_agent_adapter, require_prompt
from utils import extract_json_from_llm_response

logger = logging.getLogger(__name__)

PROMPT_PREFLIGHT = "agent_preflight_explain.txt"
PROMPT_FAILURE = "agent_failure_diagnose.txt"
PROMPT_REVIEW = "agent_report_review.txt"


def is_agent_enabled() -> bool:
    return bool(AGENT_ASSIST_ENABLED and get_agent_adapter() is not None)


def _default_payload(kind: str, reason: str, *, error: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "ok": False,
        "reason": reason,
    }
    if error:
        payload["error"] = error
    if extra:
        payload.update(extra)
    return payload


def _render_markdown(title: str, payload: dict[str, Any]) -> str:
    lines = [f"# {title}", ""]
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"## {key}")
            if value:
                lines.extend([f"- {item}" for item in value])
            else:
                lines.append("- （空）")
        elif isinstance(value, dict):
            lines.append(f"## {key}")
            lines.append("```json")
            lines.append(json.dumps(value, ensure_ascii=False, indent=2))
            lines.append("```")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def safe_write_agent_artifact(prefix: str, data: dict[str, Any] | str) -> dict[str, str]:
    if isinstance(data, str):
        payload = {"content": data}
    else:
        payload = data
    json_path = write_json_artifact(prefix, payload, category="agent")
    md_path = write_text_artifact(prefix, _render_markdown(prefix, payload), category="agent")
    return {"json_path": json_path, "md_path": md_path}


def _invoke_json(prompt_name: str, payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if not AGENT_ASSIST_ENABLED:
        return _default_payload(kind, "disabled")

    adapter = get_agent_adapter()
    if adapter is None:
        return _default_payload(kind, "adapter_unavailable")

    try:
        prompt = require_prompt(prompt_name)
    except Exception as exc:
        return _default_payload(kind, "missing_prompt", error=str(exc))

    try:
        raw = adapter.invoke(f"{prompt}\n\n输入 JSON:\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```")
        content = getattr(raw, "content", "") or ""
        parsed = extract_json_from_llm_response(content, expect_type="dict")
        if not isinstance(parsed, dict):
            return _default_payload(kind, "invalid_response", extra={"raw_content": content[:500]})
        parsed["ok"] = True
        parsed["kind"] = kind
        return parsed
    except Exception as exc:
        logger.warning("agent assistant invoke failed for %s: %s", kind, exc)
        return _default_payload(kind, "invoke_failed", error=str(exc))


def explain_preflight(preflight_result: dict[str, Any]) -> dict[str, Any]:
    result = _invoke_json(PROMPT_PREFLIGHT, {"preflight": preflight_result}, kind="preflight")
    artifacts = safe_write_agent_artifact("Agent_预检解释", result)
    result.update(artifacts)
    return result


def diagnose_failure(stage: str, error_text: str, manifest: dict[str, Any], log_excerpt: str = "") -> dict[str, Any]:
    payload = {
        "stage": stage,
        "error_text": error_text,
        "manifest": manifest,
        "log_excerpt": log_excerpt[-4000:],
    }
    result = _invoke_json(PROMPT_FAILURE, payload, kind="failure_diagnosis")
    result.setdefault("stage", stage)
    artifacts = safe_write_agent_artifact("Agent_失败诊断", result)
    result.update(artifacts)
    return result


def review_final_report(report_md: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "report_markdown": report_md,
        "context": context or {},
    }
    result = _invoke_json(PROMPT_REVIEW, payload, kind="report_review")
    artifacts = safe_write_agent_artifact("Agent_终稿审校", result)
    result.update(artifacts)
    return result
