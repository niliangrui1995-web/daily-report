"""Normalize P2/P3 intel into claim-source-confidence structures."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_P2_FIELD_ALIASES = {
    "信源": "source",
    "核心内容": "claim",
    "产业链影响": "impact",
    "原文摘要": "raw_summary",
    "真实链接": "source_url",
    "可信度": "confidence",
}

_P3_BUCKET_META = {
    "jinnang_1": {"label": "锦囊一", "confidence": 0.78},
    "jinnang_2": {"label": "锦囊二", "confidence": 0.64},
    "jinnang_3": {"label": "锦囊三", "confidence": 0.68},
}


def _confidence_to_score(raw_value: Any, default: float = 0.6) -> float:
    text = str(raw_value or "").strip()
    if not text:
        return default
    try:
        value = float(text)
        return value / 100.0 if value > 1 else value
    except ValueError:
        pass

    lowered = text.lower()
    if "高" in text or "high" in lowered:
        return 0.82
    if "中" in text or "medium" in lowered:
        return 0.64
    if "低" in text or "low" in lowered:
        return 0.46
    return default


def _score_to_label(score: float) -> str:
    if score >= 0.78:
        return "high"
    if score >= 0.58:
        return "medium"
    return "low"


def _score_to_percent(score: float) -> str:
    return f"{round(max(0.0, min(score, 1.0)) * 100):.0f}%"


def parse_p2_structured_items(raw_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    blocks = re.findall(r"<x_leaks>\s*(.*?)\s*</x_leaks>", raw_text or "", flags=re.DOTALL | re.IGNORECASE)
    for block in blocks:
        title_match = re.search(r"^###\s*(.+)$", block, flags=re.MULTILINE)
        if not title_match:
            continue

        full_title = title_match.group(1).strip()
        track = ""
        title = full_title
        track_match = re.match(r"\[(.+?)\]\s*(.+)", full_title)
        if track_match:
            track = track_match.group(1).strip()
            title = track_match.group(2).strip()

        fields: dict[str, str] = {}
        for key, value in re.findall(r"-\s*\*\*(.+?)\*\*:\s*(.+)", block):
            normalized_key = _P2_FIELD_ALIASES.get(key.strip())
            if normalized_key:
                fields[normalized_key] = value.strip()

        claim = fields.get("claim", "").strip()
        if not claim:
            continue

        confidence_score = _confidence_to_score(fields.get("confidence", ""), default=0.62)
        items.append(
            {
                "track": track or "X情报",
                "title": title,
                "claim": claim,
                "source": fields.get("source", "unknown"),
                "source_url": fields.get("source_url", ""),
                "confidence_score": confidence_score,
                "confidence": _score_to_percent(confidence_score),
                "confidence_label": _score_to_label(confidence_score),
                "impact": fields.get("impact", ""),
                "raw_summary": fields.get("raw_summary", ""),
            }
        )
    return items


def normalize_p3_structured_items(raw_results: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_results = raw_results or {}
    for bucket, meta in _P3_BUCKET_META.items():
        for item in raw_results.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            summary = str(item.get("summary", "")).strip()
            if not title and not summary:
                continue
            score = _confidence_to_score(item.get("confidence"), default=meta["confidence"])
            claim = summary or title
            items.append(
                {
                    "bucket": bucket,
                    "bucket_label": meta["label"],
                    "track": str(item.get("track", "")).strip() or "P3情报",
                    "title": title or claim[:48],
                    "claim": claim,
                    "source": str(item.get("source_name", "")).strip() or meta["label"],
                    "source_url": str(item.get("source_url", "")).strip(),
                    "confidence_score": score,
                    "confidence": _score_to_percent(score),
                    "confidence_label": _score_to_label(score),
                }
            )
    return items


def render_p2_structured_markdown(items: list[dict[str, Any]], *, model: str, collected_at: str | None = None) -> str:
    collected_at = collected_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "============================================================",
        f"P2 X平台结构化情报 (Grok {model})",
        f"采集时间: {collected_at}",
        "字段: Claim / Source / Confidence / Impact / Source URL",
        "============================================================",
        "",
    ]
    for idx, item in enumerate(items, 1):
        lines.extend(
            [
                f"### {idx}. [{item.get('track', 'X情报')}] {item.get('title', '')}",
                f"- Claim: {item.get('claim', '')}",
                f"- Source: {item.get('source', 'unknown')}",
                f"- Confidence: {item.get('confidence', '')} ({item.get('confidence_label', '')})",
                f"- Impact: {item.get('impact', '') or '未提供'}",
                f"- Source URL: {item.get('source_url', '') or '未提供'}",
                f"- Raw Summary: {item.get('raw_summary', '') or '未提供'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def render_p3_structured_markdown(items: list[dict[str, Any]], *, collected_at: str | None = None) -> str:
    collected_at = collected_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "============================================================",
        "P3 结构化机动情报",
        f"采集时间: {collected_at}",
        "字段: Claim / Source / Confidence / Source URL",
        "============================================================",
        "",
    ]
    for idx, item in enumerate(items, 1):
        lines.extend(
            [
                f"### {idx}. [{item.get('bucket_label', 'P3')} | {item.get('track', 'P3情报')}] {item.get('title', '')}",
                f"- Claim: {item.get('claim', '')}",
                f"- Source: {item.get('source', 'unknown')}",
                f"- Confidence: {item.get('confidence', '')} ({item.get('confidence_label', '')})",
                f"- Source URL: {item.get('source_url', '') or '未提供'}",
                "",
            ]
        )
    return "\n".join(lines).strip()
