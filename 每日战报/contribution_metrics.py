"""Layer contribution metrics for workflow pruning decisions."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "over",
    "latest",
    "today",
    "research",
    "search",
    "intel",
    "signal",
    "industry",
    "market",
    "company",
    "stock",
    "query",
    "round",
    "summary",
    "source",
    "sources",
    "report",
    "news",
    "analysis",
    "deep",
    "supplement",
    "补充",
    "搜索",
    "研究",
    "情报",
    "新闻",
    "行业",
    "产业",
    "公司",
    "市场",
    "标的",
    "赛道",
}


def _normalize_text(text: str) -> str:
    normalized = re.sub(r"https?://\S+", "", text or "")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", _normalize_text(text))
    return [token for token in tokens if token not in _STOPWORDS]


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    inter = len(left & right)
    union = len(left | right)
    return inter / union if union else 0.0


def _make_unit(label: str, text: str, *, url: str = "", ticker: str = "", bucket: str = "") -> dict[str, Any] | None:
    clean_label = re.sub(r"\s+", " ", (label or "").strip())
    clean_text = re.sub(r"\s+", " ", (text or "").strip())
    tokens = _tokenize(f"{clean_label} {clean_text}")
    if not clean_label and not clean_text:
        return None
    if not tokens and not ticker and not url:
        return None
    return {
        "label": clean_label or clean_text[:80],
        "text": clean_text or clean_label,
        "url": (url or "").strip(),
        "ticker": (ticker or "").strip().upper(),
        "bucket": bucket,
        "tokens": set(tokens),
        "key": _build_unit_key(clean_label or clean_text, url=url, ticker=ticker),
    }


def _build_unit_key(text: str, *, url: str = "", ticker: str = "") -> str:
    if ticker:
        return f"ticker::{ticker.upper()}"
    if url:
        return f"url::{url.strip().lower()}"
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _normalize_text(text))[:160]


def _dedupe_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        key = unit.get("key", "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(unit)
    return deduped


def extract_p1_units(results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for result in results or []:
        category = result.get("category", "")
        query = result.get("query", "")
        text = result.get("text", "")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not re.match(r"^\d+\.", line):
                continue
            head = line.split("《", 1)[0].split("<", 1)[0].strip()
            head = re.sub(r"^\d+\.\s*", "", head)
            head = re.sub(r"^\[[^\]]+\]\s*", "", head)
            title = re.split(r"\s+[—-]\s+", head, maxsplit=1)[0].strip()
            url_match = re.search(r"https?://[^\s|》]+", line)
            unit = _make_unit(
                title,
                f"{head} {category} {query}",
                url=url_match.group(0) if url_match else "",
                bucket="p1",
            )
            if unit:
                units.append(unit)
    return _dedupe_units(units)


def extract_p3_units(raw_results: dict[str, Any] | None = None, fallback_text: str = "") -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    raw_results = raw_results or {}
    for bucket in ("jinnang_1", "jinnang_2", "jinnang_3"):
        for item in raw_results.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            track = item.get("track", "")
            summary = item.get("summary", "")
            source_url = item.get("source_url", "")
            unit = _make_unit(title, f"{track} {summary}", url=source_url, bucket=bucket)
            if unit:
                units.append(unit)

    if units:
        return _dedupe_units(units)

    current_bucket = ""
    for raw_line in fallback_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_bucket = line
            continue
        if not re.match(r"^\d+\.", line):
            continue
        title = re.sub(r"^\d+\.\s*", "", line)
        unit = _make_unit(title, f"{title} {current_bucket}", bucket=current_bucket or "p3")
        if unit:
            units.append(unit)
    return _dedupe_units(units)


def extract_p4_units(p4_text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*-\s*(?:\[(?P<signal>[^\]]+)\]\s*)?(?P<name>[^(\n]+)\((?P<ticker>[^)]+)\).*?(?P<change>[+-]\d+(?:\.\d+)?)%",
        re.MULTILINE,
    )
    for match in pattern.finditer(p4_text or ""):
        signal = (match.group("signal") or "").strip()
        name = match.group("name").strip()
        ticker = match.group("ticker").strip().upper()
        change = match.group("change").strip()
        text = " ".join(part for part in (signal, name, ticker, f"{change}%") if part)
        unit = _make_unit(name, text, ticker=ticker, bucket="p4")
        if unit:
            units.append(unit)
    return _dedupe_units(units)


def extract_p75_units(p75_text: str, queries: list[str] | None = None) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for raw_line in (p75_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("```"):
            continue
        if re.match(r"^[-*]\s+", line) or re.match(r"^\d+[\.\)]\s+", line):
            content = re.sub(r"^[-*\d\.\)\s]+", "", line).strip()
            unit = _make_unit(content[:80], content, bucket="p75")
            if unit:
                units.append(unit)

    if not units:
        sentences = re.split(r"(?<=[\.\u3002\uff1b;])\s+", p75_text or "")
        for sentence in sentences:
            content = sentence.strip()
            if len(_normalize_text(content)) < 24:
                continue
            unit = _make_unit(content[:80], content, bucket="p75")
            if unit:
                units.append(unit)
            if len(units) >= 12:
                break

    if not units:
        for query in queries or []:
            unit = _make_unit(query[:80], query, bucket="p75_query")
            if unit:
                units.append(unit)
    return _dedupe_units(units)


def _unit_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_ticker = left.get("ticker", "")
    right_ticker = right.get("ticker", "")
    if left_ticker and right_ticker and left_ticker == right_ticker:
        return 1.0

    left_url = left.get("url", "")
    right_url = right.get("url", "")
    if left_url and right_url and left_url == right_url:
        return 1.0

    left_key = left.get("key", "")
    right_key = right.get("key", "")
    if left_key and right_key and (left_key == right_key or left_key in right_key or right_key in left_key):
        return 0.9

    return _jaccard(left.get("tokens", set()), right.get("tokens", set()))


def _unit_adopted(unit: dict[str, Any], target_text: str) -> bool:
    haystack = _normalize_text(target_text)
    if not haystack:
        return False

    ticker = unit.get("ticker", "")
    if ticker and ticker.lower() in haystack:
        return True

    url = unit.get("url", "")
    if url and url.lower() in haystack:
        return True

    tokens = sorted(unit.get("tokens", set()), key=len, reverse=True)
    if not tokens:
        return False

    anchors = [token for token in tokens if len(token) >= 4][:4]
    if not anchors:
        anchors = tokens[:2]
    hits = sum(1 for token in anchors if token in haystack)
    if len(anchors) == 1:
        return hits == 1
    if len(anchors) == 2:
        return hits >= 2 or any(len(token) >= 6 and token in haystack for token in anchors)
    return hits >= 2


def _recommendation(stage: str, total_units: int, unique_ratio: float, adoption_ratio: float, score: float) -> str:
    if total_units == 0:
        return "no_output"
    if stage == "p4" and adoption_ratio < 0.15:
        return "gate_by_event"
    if stage == "p75" and unique_ratio < 0.2 and adoption_ratio < 0.25:
        return "merge_or_gate"
    if stage == "p3" and unique_ratio < 0.2 and adoption_ratio < 0.25:
        return "merge_with_search_layers"
    if stage == "p1" and adoption_ratio >= 0.3:
        return "keep_core_collector"
    if score >= 65:
        return "keep"
    if score >= 40:
        return "keep_but_shrink"
    return "candidate_to_prune"


def _score_stage(total_units: int, unique_units: int, adopted_units: int, cost_seconds: float | None) -> float:
    if total_units <= 0:
        return 0.0
    unique_ratio = unique_units / total_units
    adoption_ratio = adopted_units / total_units
    score = unique_ratio * 45 + adoption_ratio * 45
    score += min(10.0, unique_units * 0.8)
    if adopted_units > 0:
        score += min(10.0, adopted_units * 0.6)
    if cost_seconds and adopted_units <= 0:
        score -= min(15.0, cost_seconds / 20.0)
    elif cost_seconds and adopted_units > 0:
        score -= min(12.0, (cost_seconds / adopted_units) / 30.0)
    return round(max(0.0, min(100.0, score)), 1)


def _summarize_stage(
    stage: str,
    units: list[dict[str, Any]],
    peer_units: list[dict[str, Any]],
    *,
    p6_mapping: str,
    final_report: str,
    cost_seconds: float | None,
    extra_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unique_units = 0
    p6_hits = 0
    p9_hits = 0
    adopted_units = 0
    unique_samples: list[str] = []
    adopted_samples: list[str] = []

    for unit in units:
        if not any(_unit_similarity(unit, peer) >= 0.55 for peer in peer_units):
            unique_units += 1
            if len(unique_samples) < 3:
                unique_samples.append(unit.get("label", ""))

        hit_p6 = _unit_adopted(unit, p6_mapping)
        hit_p9 = _unit_adopted(unit, final_report)
        if hit_p6:
            p6_hits += 1
        if hit_p9:
            p9_hits += 1
        if hit_p6 or hit_p9:
            adopted_units += 1
            if len(adopted_samples) < 3:
                adopted_samples.append(unit.get("label", ""))

    total_units = len(units)
    unique_ratio = round(unique_units / total_units, 3) if total_units else 0.0
    adoption_ratio = round(adopted_units / total_units, 3) if total_units else 0.0
    score = _score_stage(total_units, unique_units, adopted_units, cost_seconds)

    summary = {
        "stage": stage,
        "status": "evaluated" if total_units else "empty",
        "units": total_units,
        "unique_units": unique_units,
        "unique_ratio": unique_ratio,
        "peer_overlap_units": max(total_units - unique_units, 0),
        "p6_hits": p6_hits,
        "p9_hits": p9_hits,
        "adopted_units": adopted_units,
        "adoption_ratio": adoption_ratio,
        "cost_seconds": round(cost_seconds, 1) if cost_seconds else 0.0,
        "cost_per_unique_unit": round(cost_seconds / unique_units, 2) if cost_seconds and unique_units else None,
        "cost_per_adopted_unit": round(cost_seconds / adopted_units, 2) if cost_seconds and adopted_units else None,
        "score": score,
        "recommendation": _recommendation(stage, total_units, unique_ratio, adoption_ratio, score),
        "sample_unique_units": unique_samples,
        "sample_adopted_units": adopted_samples,
    }
    if extra_metrics:
        summary["extra_metrics"] = extra_metrics
    return summary


def build_layer_roi_report(
    *,
    p1_results: list[dict[str, Any]] | None = None,
    p3_raw_results: dict[str, Any] | None = None,
    p3_text: str = "",
    p4_text: str = "",
    p75_text: str = "",
    p75_queries: list[str] | None = None,
    p6_mapping: str = "",
    final_report: str = "",
    timings: dict[str, float] | None = None,
    extras: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timings = timings or {}
    extras = extras or {}

    stage_units = {
        "p1": extract_p1_units(p1_results),
        "p3": extract_p3_units(p3_raw_results, p3_text),
        "p4": extract_p4_units(p4_text),
        "p75": extract_p75_units(p75_text, queries=p75_queries),
    }

    layers: dict[str, dict[str, Any]] = {}
    for stage_name, units in stage_units.items():
        peer_units = []
        for other_stage, other_units in stage_units.items():
            if other_stage == stage_name:
                continue
            peer_units.extend(other_units)
        layers[stage_name] = _summarize_stage(
            stage_name,
            units,
            peer_units,
            p6_mapping=p6_mapping,
            final_report=final_report,
            cost_seconds=timings.get(stage_name),
            extra_metrics=extras.get(stage_name),
        )

    scoreboard = sorted(
        (
            {
                "stage": stage,
                "score": payload["score"],
                "recommendation": payload["recommendation"],
            }
            for stage, payload in layers.items()
        ),
        key=lambda item: item["score"],
        reverse=True,
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "method": {
            "uniqueness": "Unit-level similarity against peer layers",
            "adoption": "Matched against P6 mapping and final report text",
            "cost": "Stage elapsed seconds from current run or recovered estimates",
        },
        "layers": layers,
        "scoreboard": scoreboard,
    }


def render_layer_roi_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Layer ROI",
        "",
        f"> Generated at: {report.get('generated_at', '')}",
        "> Metrics: unique units / downstream adoption / cost efficiency",
        "",
        "| Layer | Units | Unique | Unique% | P6 hits | P9 hits | Adopted | Cost(s) | Score | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    layers = report.get("layers", {})
    for stage_name in ("p1", "p3", "p4", "p75"):
        payload = layers.get(stage_name, {})
        lines.append(
            "| {stage} | {units} | {unique_units} | {unique_ratio:.1%} | {p6_hits} | {p9_hits} | {adopted_units} | {cost_seconds:.1f} | {score:.1f} | {recommendation} |".format(
                stage=stage_name.upper(),
                units=payload.get("units", 0),
                unique_units=payload.get("unique_units", 0),
                unique_ratio=payload.get("unique_ratio", 0.0),
                p6_hits=payload.get("p6_hits", 0),
                p9_hits=payload.get("p9_hits", 0),
                adopted_units=payload.get("adopted_units", 0),
                cost_seconds=payload.get("cost_seconds", 0.0),
                score=payload.get("score", 0.0),
                recommendation=payload.get("recommendation", "unknown"),
            )
        )

    for stage_name in ("p1", "p3", "p4", "p75"):
        payload = layers.get(stage_name, {})
        lines.extend(
            [
                "",
                f"## {stage_name.upper()}",
                f"- Recommendation: {payload.get('recommendation', 'unknown')}",
                f"- Sample unique units: {', '.join(payload.get('sample_unique_units', [])) or 'none'}",
                f"- Sample adopted units: {', '.join(payload.get('sample_adopted_units', [])) or 'none'}",
            ]
        )
        if payload.get("extra_metrics"):
            rendered = ", ".join(
                f"{key}={value}" for key, value in sorted(payload["extra_metrics"].items())
            )
            lines.append(f"- Extra metrics: {rendered}")

    return "\n".join(lines).strip() + "\n"
