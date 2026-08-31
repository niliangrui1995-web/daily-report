"""Adaptive deep-research planning and execution for P7."""

from __future__ import annotations

import concurrent.futures
import glob
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from artifact_store import extract_timestamp_from_name
from semantic_output import is_no_evidence_text
from settings import OUTPUT_DIR
from tdx_stock_universe import (
    filter_rejected_stock_lines,
    normalize_rejected_stock_names,
    remove_rejected_stock_mentions,
)

P7_TOPIC_PREFIX = "## \u6df1\u5ea6\u7814\u7a76\uff1a"
P7_PRIMARY_SOURCE_BACKED_TIER = "primary_source_backed"
P7_SUMMARY_LEAD_TIER = "summary_lead_pending_verification"
# Why: 深度研究是研究型搜索，不需要时间窗口限制
#      空串传入 _build_search_prompt 会走 unlimited 模式
ROUND_BACKTRACKS = ("", "", "")
ROUND_HINTS = (
    "Prioritize the freshest event facts and named entities.",
    "Expand into chain transmission, winners, losers, and supplier or customer context.",
    "Fill the remaining gaps around alternatives, substitutes, and competitive response.",
)


def _nonnegative_evidence_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_p7_evidence_provenance(value: object) -> dict[str, object]:
    """Normalize the optional P7 Scrapling enrichment provenance.

    ``None`` and unrecognised values intentionally return an empty mapping so
    older callers keep their historical behavior.  Once a payload is present,
    the count/tier invariant is conservative: zero successful high-value body
    enrichments is always a summary lead pending verification.
    """
    if not isinstance(value, dict):
        return {}

    count_keys = (
        "primary_source_count",
        "summary_source_count",
        "source_url_count",
    )
    raw_rounds = value.get("rounds")
    has_direct_evidence = any(key in value for key in (*count_keys, "evidence_tier"))
    normalized_rounds: list[dict[str, object]] = []
    if isinstance(raw_rounds, list):
        for position, raw_round in enumerate(raw_rounds, start=1):
            normalized_round = normalize_p7_evidence_provenance(raw_round)
            if not normalized_round:
                continue
            normalized_round.pop("rounds", None)
            normalized_round["round_index"] = _nonnegative_evidence_count(
                raw_round.get("round_index", position) if isinstance(raw_round, dict) else position
            ) or position
            normalized_rounds.append(normalized_round)

    if not has_direct_evidence:
        if not normalized_rounds:
            return {}
        return _merge_p7_evidence_provenance(normalized_rounds, rounds=normalized_rounds)

    primary_count = _nonnegative_evidence_count(value.get("primary_source_count"))
    normalized: dict[str, object] = {
        "primary_source_count": primary_count,
        "summary_source_count": _nonnegative_evidence_count(value.get("summary_source_count")),
        "source_url_count": _nonnegative_evidence_count(value.get("source_url_count")),
        "evidence_tier": (
            P7_PRIMARY_SOURCE_BACKED_TIER
            if primary_count > 0
            else P7_SUMMARY_LEAD_TIER
        ),
    }
    if normalized_rounds:
        normalized["rounds"] = normalized_rounds
    return normalized


def _merge_p7_evidence_provenance(
    values: list[object],
    *,
    rounds: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    normalized_values = [
        normalize_p7_evidence_provenance(value)
        for value in values
    ]
    normalized_values = [value for value in normalized_values if value]
    merged = normalize_p7_evidence_provenance(
        {
            "primary_source_count": sum(
                _nonnegative_evidence_count(value.get("primary_source_count"))
                for value in normalized_values
            ),
            "summary_source_count": sum(
                _nonnegative_evidence_count(value.get("summary_source_count"))
                for value in normalized_values
            ),
            "source_url_count": sum(
                _nonnegative_evidence_count(value.get("source_url_count"))
                for value in normalized_values
            ),
        }
    )
    if rounds:
        merged["rounds"] = rounds
    return merged


def _empty_p7_evidence_provenance() -> dict[str, object]:
    return normalize_p7_evidence_provenance(
        {
            "primary_source_count": 0,
            "summary_source_count": 0,
            "source_url_count": 0,
        }
    )


def build_p7_evidence_boundary(provenance: object) -> str:
    """Return a human-visible evidence boundary without Markdown decoration."""
    normalized = normalize_p7_evidence_provenance(provenance)
    if not normalized:
        return ""

    primary_count = _nonnegative_evidence_count(normalized.get("primary_source_count"))
    summary_count = _nonnegative_evidence_count(normalized.get("summary_source_count"))
    source_url_count = _nonnegative_evidence_count(normalized.get("source_url_count"))
    if primary_count == 0:
        return (
            "【摘要线索待核验】本轮 P7 的 Scrapling 高价值正文深挖成功数为 0"
            f"（摘要提取 {summary_count} 条、候选 URL {source_url_count} 个）。"
            "涉及具体订单、报价、量产或公司归因的内容仅为摘要线索，不能作为硬证据。"
        )
    return (
        f"P7 证据层级：{P7_PRIMARY_SOURCE_BACKED_TIER}（Scrapling 高价值正文深挖成功 "
        f"{primary_count} 条、摘要提取 {summary_count} 条、候选 URL {source_url_count} 个）。"
        "该计数仅描述采集路径，不等同于官方披露认证。"
    )


def _p7_round_evidence_provenance(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        normalized = normalize_p7_evidence_provenance(
            result.get("scrapling_evidence_provenance")
        )
        if normalized:
            return normalized
        sources = result.get("grounding_sources")
        source_url_count = len(sources) if isinstance(sources, list) else 0
    else:
        source_url_count = 0
    # P7 has requested a provenance-aware search.  A missing payload therefore
    # cannot upgrade evidence; keep it as a zero-primary summary lead.
    return normalize_p7_evidence_provenance(
        {
            "primary_source_count": 0,
            "summary_source_count": 0,
            "source_url_count": source_url_count,
        }
    )

_TOPIC_VALUE_MARKERS = (
    "\u539f\u56e0",
    "\u4f20\u5bfc",
    "\u5f71\u54cd",
    "\u4ea7\u4e1a\u94fe",
    "\u4f9b\u5e94\u94fe",
    "\u4e0a\u6e38",
    "\u4e0b\u6e38",
    "\u53d7\u76ca",
    "\u53d7\u635f",
    "\u66ff\u4ee3",
    "\u7ade\u4e89\u683c\u5c40",
    "why",
    "winner",
    "loser",
    "substitute",
)
_SECOND_ORDER_MARKERS = _TOPIC_VALUE_MARKERS
_LOW_CONFIDENCE_MARKERS = (
    "\u4e2d\u53ef\u4fe1",
    "\u5f85\u786e\u8ba4",
    "\u5c1a\u5f85",
    "\u53ef\u80fd",
    "\u4f20\u95fb",
    "\u672a\u8bc1\u5b9e",
)
_OLIGARCH_MOVE_MARKERS = (
    "\u5927\u6da8\u50ac\u5316",
    "\u66b4\u8dcc\u8b66\u60d5",
    "\u4e0b\u8dcc\u6ce8\u610f",
)
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "will",
    "into",
    "over",
    "more",
    "have",
    "after",
    "before",
    "about",
    "their",
    "industry",
    "market",
    "company",
    "event",
    "today",
    "latest",
    "explain",
    "search",
    "research",
    "cause",
    "driver",
    "trigger",
    "winner",
    "winners",
    "loser",
    "losers",
    "benefit",
    "beneficiary",
    "beneficiaries",
    "supplier",
    "suppliers",
    "alternative",
    "alternatives",
    "substitute",
    "substitutes",
    "competition",
    "response",
    "downside",
    "pressure",
    "implication",
    "chain",
    "transmission",
    "\u6df1\u5ea6",
    "\u7814\u7a76",
    "\u4e8b\u4ef6",
    "\u516c\u53f8",
    "\u4ea7\u4e1a",
    "\u884c\u4e1a",
    "\u5e02\u573a",
    "\u4eca\u65e5",
    "\u6700\u65b0",
}
_CAUSE_MARKERS = (
    "cause",
    "\u539f\u56e0",
    "\u89e6\u53d1",
    "\u9a71\u52a8",
    "\u80cc\u540e",
    "because",
    "driver",
    "why",
)
_WINNER_MARKERS = (
    "\u53d7\u76ca",
    "\u8d62\u5bb6",
    "\u5229\u597d",
    "\u589e\u91cf",
    "benefit",
    "winner",
    "upside",
)
_LOSER_MARKERS = (
    "\u53d7\u635f",
    "\u8d1f\u9762",
    "\u5229\u7a7a",
    "\u538b\u529b",
    "loser",
    "downside",
    "risk",
)
_ALTERNATIVE_MARKERS = (
    "\u66ff\u4ee3",
    "\u5907\u9009",
    "\u7ade\u4e89",
    "\u683c\u5c40",
    "alternative",
    "substitute",
    "competitive",
)


def _normalize_topic_key(text: str) -> str:
    if not text:
        return ""
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", text.lower())
    return "".join(tokens)


def _topic_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", text.lower())
        if token not in _STOPWORDS
    }


def _topic_similarity(left: str, right: str) -> float:
    left_key = _normalize_topic_key(left)
    right_key = _normalize_topic_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    if left_key in right_key or right_key in left_key:
        return 0.92
    left_tokens = _topic_tokens(left)
    right_tokens = _topic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def extract_search_queries(p6_mapping: str) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    for raw_query in re.findall(r"^\s*SEARCH:\s*(.+)$", p6_mapping or "", re.MULTILINE):
        query = raw_query.strip()
        if len(query) < 8:
            continue
        key = _normalize_topic_key(query)
        if not key or key in seen:
            continue
        seen.add(key)
        queries.append(query)
    return queries[:5]


def pick_deep_topics(p6_mapping: str) -> list[str]:
    seen: set[str] = set()
    topics: list[str] = []
    for raw_topic in re.findall(r"DEEP_RESEARCH:\s*(.+)", p6_mapping or ""):
        topic = raw_topic.strip()
        if len(topic) < 10:
            continue
        key = _normalize_topic_key(topic)
        if not key or key in seen:
            continue
        seen.add(key)
        topics.append(topic)

    if topics:
        logging.info(f"[P7] planner selected {len(topics)} deep-research topic(s)")
    else:
        logging.info("[P7] planner did not request deep research")
    return topics[:3]


def _extract_heading_topic(line: str) -> str:
    if not line.startswith("## "):
        return ""
    for separator in ("\uff1a", ":"):
        if separator in line:
            return line.split(separator, 1)[1].strip()
    return ""


def _recent_deep_topic_history(days: int = 5) -> list[dict[str, str]]:
    cutoff = datetime.now() - timedelta(days=days)
    pattern = os.path.join(OUTPUT_DIR, "**", "P7_*.md")
    history: list[dict[str, str]] = []
    seen: set[str] = set()

    for file_path in sorted(glob.glob(pattern, recursive=True), reverse=True):
        timestamp = extract_timestamp_from_name(os.path.basename(file_path))
        if not timestamp:
            continue
        try:
            file_time = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S")
        except ValueError:
            try:
                file_time = datetime.strptime(timestamp[:12], "%Y%m%d%H%M")
            except ValueError:
                continue
        if file_time < cutoff:
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    topic = _extract_heading_topic(raw_line.strip())
                    if not topic:
                        continue
                    normalized = _normalize_topic_key(topic)
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    history.append(
                        {
                            "topic": topic,
                            "normalized": normalized,
                            "date": file_time.strftime("%Y-%m-%d"),
                            "path": file_path,
                        }
                    )
        except OSError as exc:
            logging.debug(f"[P7] skip recent-history scan failure: {exc}")

    return history


def build_recent_topic_guardrail(days: int = 5, *, limit: int = 12) -> str:
    history = _recent_deep_topic_history(days=days)
    if not history:
        return ""

    lines = [f"- {item['topic']} ({item['date'][5:]})" for item in history[:limit]]
    logging.info(f"[P7] recent-topic guardrail loaded {len(lines)} item(s) for {days}d window")
    return "\n".join(lines)


def _is_recent_duplicate(topic: str, recent_history: list[dict[str, str]]) -> bool:
    for item in recent_history:
        if _topic_similarity(topic, item.get("topic", "")) >= 0.8:
            logging.info(
                f"[P7] skip duplicated topic within 5d window: {topic} ~ {item.get('topic', '')}"
            )
            return True
    return False


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    haystack = (text or "").lower()
    return any(marker.lower() in haystack for marker in markers)


def _has_second_order_signal(p6_mapping: str) -> bool:
    return _has_marker(p6_mapping, _SECOND_ORDER_MARKERS)


def _has_large_oligarch_move(p4_data: str) -> bool:
    if _has_marker(p4_data, _OLIGARCH_MOVE_MARKERS):
        return True
    for match in re.finditer(r"([+-]\d+(?:\.\d+)?)%", p4_data or ""):
        try:
            if abs(float(match.group(1))) >= 5:
                return True
        except ValueError:
            continue
    return False


def _has_low_confidence_signal(p5_evidence: str, p6_mapping: str) -> bool:
    return _has_marker(p5_evidence, _LOW_CONFIDENCE_MARKERS) or _has_marker(
        p6_mapping, _LOW_CONFIDENCE_MARKERS
    )


def _topic_priority(topic: str) -> int:
    score = 0
    lowered = topic.lower()
    if any(marker.lower() in lowered for marker in _TOPIC_VALUE_MARKERS):
        score += 2
    if len(topic) >= 22:
        score += 1
    if any(token in lowered for token in ("->", "\u2192", "\u5bf9", "\u53d7\u76ca", "\u53d7\u635f")):
        score += 1
    return score


def _derive_goal(triggers: list[str], topics: list[str], queries: list[str]) -> str:
    goal_parts: list[str] = []
    if "second_order_chain" in triggers:
        goal_parts.append("explain the chain transmission and identify concrete winners or losers")
    if "oligarch_large_move" in triggers:
        goal_parts.append("translate the overseas oligarch move into A-share transmission paths")
    if "low_confidence" in triggers:
        goal_parts.append("close evidence gaps before the final report")
    if not goal_parts and topics:
        goal_parts.append("pin down the event cause and the direct industry implication")
    if queries:
        goal_parts.append("reuse supplement searches only for missing holes, not for broad repetition")
    return "; ".join(goal_parts)


def build_research_plan(
    p6_mapping: str,
    *,
    p4_data: str = "",
    p5_evidence: str = "",
    rejected_target_names: list[str] | None = None,
) -> dict[str, object]:
    rejected_names = normalize_rejected_stock_names(rejected_target_names)
    identity_rewrites = 0

    raw_topics: list[str] = []
    for raw_topic in pick_deep_topics(p6_mapping):
        topic, removed_names = remove_rejected_stock_mentions(raw_topic, rejected_names)
        if removed_names:
            identity_rewrites += 1
            logging.warning(
                "[P7] removed rejected stock identities from topic: %s",
                ", ".join(removed_names),
            )
        if len(topic) >= 10:
            raw_topics.append(topic)

    search_queries: list[str] = []
    for raw_query in extract_search_queries(p6_mapping):
        query, removed_names = remove_rejected_stock_mentions(raw_query, rejected_names)
        if removed_names:
            identity_rewrites += 1
            logging.warning(
                "[P7] removed rejected stock identities from supplement query: %s",
                ", ".join(removed_names),
            )
        if len(query) >= 8:
            search_queries.append(query)
    recent_history = _recent_deep_topic_history(days=5)

    skipped_recent_topics: list[str] = []
    candidate_topics: list[str] = []
    for topic in raw_topics:
        if _is_recent_duplicate(topic, recent_history):
            skipped_recent_topics.append(topic)
            continue
        candidate_topics.append(topic)

    candidate_topics.sort(key=_topic_priority, reverse=True)

    second_order = _has_second_order_signal(p6_mapping)
    oligarch_move = _has_large_oligarch_move(p4_data)
    low_confidence = _has_low_confidence_signal(p5_evidence, p6_mapping)

    triggers: list[str] = []
    if candidate_topics:
        triggers.append("explicit_topics")
    if second_order:
        triggers.append("second_order_chain")
    if oligarch_move:
        triggers.append("oligarch_large_move")
    if low_confidence:
        triggers.append("low_confidence")

    should_run_p7 = bool(candidate_topics)
    max_topics = 0
    max_rounds = 0
    if should_run_p7:
        if second_order or oligarch_move or low_confidence:
            max_topics = min(len(candidate_topics), 3)
            max_rounds = 3
        elif len(candidate_topics) >= 2 or search_queries:
            max_topics = min(len(candidate_topics), 2)
            max_rounds = 2
        else:
            max_topics = 1
            max_rounds = 2

    deep_topics = candidate_topics[:max_topics]
    round_backtracks = list(ROUND_BACKTRACKS[:max_rounds])
    goal = _derive_goal(triggers, deep_topics, search_queries)

    return {
        "goal": goal,
        "triggers": triggers,
        "priority": "high" if len(triggers) >= 2 else "normal",
        "should_run_p7": should_run_p7 and bool(deep_topics),
        "should_run_p75": bool(search_queries),
        "deep_topics": deep_topics,
        "supplement_queries": search_queries[:5],
        "max_topics": max_topics,
        "max_rounds": max_rounds,
        "round_backtracks": round_backtracks,
        "skipped_recent_topics": skipped_recent_topics,
        "recent_topics": [item["topic"] for item in recent_history[:10]],
        "rejected_target_names": rejected_names,
        "identity_rewrites": identity_rewrites,
    }


def _normalize_fact_line(line: str) -> str:
    normalized = re.sub(r"https?://\S+", "", line or "")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip(" -*\t")
    return normalized.lower()


def _extract_fact_lines(text: str) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-*]\s*", "", line)
        normalized = _normalize_fact_line(line)
        if len(normalized) < 18 or normalized in seen:
            continue
        seen.add(normalized)
        facts.append(line)

    if facts:
        return facts

    sentences = re.split(r"(?<=[\.\u3002\uff1b;])\s+", text or "")
    for sentence in sentences:
        normalized = _normalize_fact_line(sentence)
        if len(normalized) < 18 or normalized in seen:
            continue
        seen.add(normalized)
        facts.append(sentence.strip())
        if len(facts) >= 6:
            break
    return facts


def _extract_entities(text: str) -> set[str]:
    entities: set[str] = set()
    for token in re.findall(r"[A-Z][A-Za-z0-9&\.-]{1,}|[A-Za-z]{3,}|[\u4e00-\u9fff]{2,8}", text or ""):
        lowered = token.lower()
        if lowered in _STOPWORDS:
            continue
        if len(token) < 3:
            continue
        entities.add(lowered)
    return entities


def _extract_query_terms(text: str, *, limit: int = 4) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Z][A-Za-z0-9&\.-]{1,}|[A-Za-z]{3,}|[\u4e00-\u9fff]{2,8}", text or ""):
        lowered = token.lower()
        if lowered in _STOPWORDS or len(token) < 3:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def _collect_priority_sentences(text: str) -> list[str]:
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[\.\u3002\uff1b;])\s+", text or "")
        if sentence and sentence.strip()
    ]
    if not sentences:
        return []

    prioritized: list[str] = []
    seen: set[str] = set()
    for markers in (_WINNER_MARKERS, _ALTERNATIVE_MARKERS, _LOSER_MARKERS, _CAUSE_MARKERS):
        for sentence in sentences:
            lowered = sentence.lower()
            if sentence in seen:
                continue
            if any(marker.lower() in lowered for marker in markers):
                seen.add(sentence)
                prioritized.append(sentence)

    for sentence in sentences:
        if sentence in seen:
            continue
        prioritized.append(sentence)
    return prioritized


def _dedupe_query_parts(parts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = re.sub(r"\s+", " ", str(part or "")).strip()
        if not text:
            continue
        key = _normalize_topic_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def _estimate_incremental_novelty(previous_texts: list[str], current_text: str) -> float:
    current_facts = {_normalize_fact_line(line) for line in _extract_fact_lines(current_text)}
    current_facts.discard("")
    if not current_facts:
        return 0.0

    previous_facts: set[str] = set()
    for previous_text in previous_texts:
        previous_facts.update(_normalize_fact_line(line) for line in _extract_fact_lines(previous_text))
    previous_facts.discard("")

    new_facts = current_facts - previous_facts
    return len(new_facts) / len(current_facts)


def _coverage_flags(text: str) -> dict[str, bool]:
    lowered = (text or "").lower()
    return {
        "cause": any(marker.lower() in lowered for marker in _CAUSE_MARKERS),
        "winners": any(marker.lower() in lowered for marker in _WINNER_MARKERS),
        "losers": any(marker.lower() in lowered for marker in _LOSER_MARKERS),
        "alternatives": any(marker.lower() in lowered for marker in _ALTERNATIVE_MARKERS),
    }


def _structured_summary(structured: dict[str, object] | None) -> str:
    if not isinstance(structured, dict):
        return ""
    return str(structured.get("summary") or "").strip()


def _structured_claim_lines(structured: dict[str, object] | None, fallback_text: str) -> list[str]:
    if isinstance(structured, dict):
        claims = [str(item).strip() for item in structured.get("claims") or [] if str(item).strip()]
        if claims:
            return claims[:6]
    return _extract_fact_lines(fallback_text)


def _structured_entity_terms(structured: dict[str, object] | None, fallback_text: str) -> list[str]:
    if isinstance(structured, dict):
        entities = [str(item).strip() for item in structured.get("entities") or [] if str(item).strip()]
        if entities:
            return entities[:8]
    return []


def _structured_coverage(structured: dict[str, object] | None, fallback_text: str) -> dict[str, bool]:
    if isinstance(structured, dict):
        coverage = structured.get("coverage_flags")
        if isinstance(coverage, dict):
            return {
                "cause": bool(coverage.get("cause")),
                "winners": bool(coverage.get("winners")),
                "losers": bool(coverage.get("losers")),
                "alternatives": bool(coverage.get("alternatives")),
            }
    return _coverage_flags(fallback_text)


def _extract_dates(text: str) -> list[str]:
    patterns = (
        r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}",
        r"20\d{2}\u5e74\d{1,2}\u6708\d{1,2}\u65e5",
    )
    dates: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.findall(pattern, text or ""):
            if match in seen:
                continue
            seen.add(match)
            dates.append(match)
    return dates[:5]


def _structured_dates(structured: dict[str, object] | None, fallback_text: str) -> list[str]:
    if isinstance(structured, dict):
        dates = [str(item).strip() for item in structured.get("dates") or [] if str(item).strip()]
        if dates:
            return dates[:5]
    return _extract_dates(fallback_text)


def _extract_implications(fact_lines: list[str]) -> list[str]:
    markers = (
        "\u53d7\u76ca",
        "\u53d7\u635f",
        "\u5f71\u54cd",
        "\u62c9\u52a8",
        "\u538b\u5236",
        "benefit",
        "winner",
        "loser",
        "impact",
        "pressure",
    )
    lines = [line for line in fact_lines if any(marker.lower() in line.lower() for marker in markers)]
    if lines:
        return lines[:3]
    return fact_lines[:2]


def _structured_implications(
    structured: dict[str, object] | None,
    fact_lines: list[str],
) -> list[str]:
    if isinstance(structured, dict):
        implications = [str(item).strip() for item in structured.get("implications") or [] if str(item).strip()]
        if implications:
            return implications[:3]
    return _extract_implications(fact_lines)


def _open_questions(coverage: dict[str, bool], topic: str) -> list[str]:
    questions: list[str] = []
    if not coverage["cause"]:
        questions.append(f"Need a cleaner explanation of why '{topic}' happened now.")
    if not coverage["winners"]:
        questions.append("Need a more concrete beneficiary list across A-share names or suppliers.")
    if not coverage["losers"]:
        questions.append("Need explicit downside or crowding risk for the losing side.")
    if not coverage["alternatives"]:
        questions.append("Need alternatives, substitutes, or competitive-response context.")
    return questions[:3]


def _structured_open_questions(
    structured: dict[str, object] | None,
    coverage: dict[str, bool],
    topic: str,
) -> list[str]:
    if isinstance(structured, dict):
        questions = [str(item).strip() for item in structured.get("open_questions") or [] if str(item).strip()]
        if questions:
            return questions[:3]
    return _open_questions(coverage, topic)


def _format_source_lines(sources: list[dict[str, str]]) -> list[str]:
    if not sources:
        return ["- No grounded sources returned by the search engine."]
    rendered: list[str] = []
    seen_urls: set[str] = set()
    for source in sources:
        url = source.get("url", "").strip()
        title = source.get("title", "").strip() or url
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        rendered.append(f"- [{title}]({url})")
        if len(rendered) >= 5:
            break
    return rendered or ["- No grounded sources returned by the search engine."]


def _build_rule_refined_query(
    topic: str,
    latest_text: str,
    goal: str,
    round_index: int,
    *,
    latest_structured: dict[str, object] | None = None,
) -> tuple[str, bool]:
    if not latest_text or not latest_text.strip():
        return topic, True

    coverage = _structured_coverage(latest_structured, latest_text)
    evidence_lines = _structured_claim_lines(latest_structured, latest_text)
    priority_sentences = _collect_priority_sentences("\n".join(evidence_lines[:6]) or latest_text)
    entity_corpus = " ".join(priority_sentences[:6]) or "\n".join(evidence_lines[:6]) or latest_text
    structured_entities = _structured_entity_terms(latest_structured, latest_text)
    entities = structured_entities[:4] if structured_entities else _extract_query_terms(entity_corpus, limit=4)
    goal_terms = _extract_query_terms(goal, limit=2)

    focus_terms: list[str] = []
    if not coverage["cause"]:
        focus_terms.extend(["cause", "driver", "trigger"])
    if not coverage["winners"]:
        focus_terms.extend(["winners", "beneficiaries", "suppliers"])
    if not coverage["losers"]:
        focus_terms.extend(["losers", "downside", "pressure"])
    if not coverage["alternatives"]:
        focus_terms.extend(["alternatives", "substitutes", "competition"])

    if not focus_terms:
        if round_index <= 1:
            focus_terms.extend(["supply chain", "transmission", "impact"])
        else:
            focus_terms.extend(["competitive response", "alternatives", "substitutes"])

    query_parts = [topic]
    query_parts.extend(goal_terms)
    query_parts.extend(focus_terms)
    query_parts.extend(entities)

    deduped_parts = _dedupe_query_parts(query_parts)
    query = " ".join(deduped_parts).strip()
    if not query or _normalize_topic_key(query) == _normalize_topic_key(topic):
        return topic, True
    if len(query) > 220:
        query = query[:220].rstrip()
    return query, False


def _pick_refined_query(raw_text: str, fallback: str) -> tuple[str, bool]:
    invalid_queries = {
        "initiating the analysis",
        "initiating inquiry process",
        "initiating query generation",
        "analysis",
        "query generation",
        "search query",
    }
    if not raw_text or not raw_text.strip():
        return fallback, True

    lines = []
    for line in raw_text.splitlines():
        text = line.strip()
        if not text or text.startswith("```") or text.startswith("#"):
            continue
        lines.append(text)

    if not lines:
        return fallback, True

    query = lines[0].strip().strip("\"'` ")
    query = re.sub(r"^\*+|\*+$", "", query).strip()
    query = re.sub(r"^[-\d\.\)\s]+", "", query).strip()
    query = re.sub(r"\s+", " ", query)

    if len(query) < 8:
        return fallback, True

    lowered = query.lower()
    if lowered in invalid_queries:
        return fallback, True
    if re.match(r"^(initiating|starting|analysis|query generation)", lowered):
        return fallback, True
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fa5]", query):
        return fallback, True

    if len(query) > 220:
        query = query[:220].rstrip()

    return query, False


def _format_round_section(
    *,
    round_index: int,
    query: str,
    backtrack: str,
    goal: str,
    result_text: str,
    structured: dict[str, object] | None,
    sources: list[dict[str, str]],
    novelty: float,
    coverage: dict[str, bool],
    new_entities_count: int,
    evidence_provenance: dict[str, object] | None = None,
) -> str:
    fact_lines = _structured_claim_lines(structured, result_text)
    claim = _structured_summary(structured) or (fact_lines[0] if fact_lines else "No stable claim extracted.")
    implication_lines = _structured_implications(structured, fact_lines)
    date_lines = _structured_dates(structured, result_text)
    open_questions = _structured_open_questions(structured, coverage, query)
    evidence_boundary = build_p7_evidence_boundary(evidence_provenance)

    lines = [
        f"### Round {round_index}",
        f"- Query: {query}",
        f"- Search window: {backtrack}",
        f"- Goal: {goal or 'Explain the cause and the concrete implication.'}",
        f"- Novelty score: {novelty:.2f}",
        f"- New entities: {new_entities_count}",
    ]
    if evidence_boundary:
        lines.extend([f"> {evidence_boundary}", ""])
    else:
        lines.append("")

    lines.extend(
        [
        "#### Claim",
        claim,
        "",
        "#### Evidence",
        ]
    )
    if fact_lines:
        lines.extend(f"- {line}" for line in fact_lines[:6])
    else:
        lines.append("- No evidence lines extracted from this round.")

    lines.extend(["", "#### Sources"])
    lines.extend(_format_source_lines(sources))

    lines.extend(["", "#### Dates"])
    if date_lines:
        lines.extend(f"- {item}" for item in date_lines)
    else:
        lines.append("- No explicit dates found in the returned evidence.")

    lines.extend(["", "#### Implication"])
    if implication_lines:
        lines.extend(f"- {line}" for line in implication_lines)
    else:
        lines.append("- The current round did not surface a clear downstream implication.")

    lines.extend(["", "#### Open Questions"])
    if open_questions:
        lines.extend(f"- {question}" for question in open_questions)
    else:
        lines.append("- Core gaps closed for this topic.")

    return "\n".join(lines)


def iterative_deep_search(
    topic: str,
    *,
    rounds: int = 3,
    goal: str = "",
    round_backtracks: list[str] | None = None,
) -> dict[str, object]:
    from search_engine import gemini_search

    round_backtracks = list(round_backtracks or ROUND_BACKTRACKS[:rounds])
    init_delay = random.uniform(1.5, 4.0)
    logging.info(f"[P7] deep research stagger start for '{topic}' -> {init_delay:.1f}s")
    time.sleep(init_delay)

    round_sections: list[str] = []
    round_evidence_records: list[dict[str, object]] = []
    successful_texts: list[str] = []
    successful_structured: list[dict[str, object] | None] = []
    queries_used: list[str] = []
    coverage_hits: set[str] = set()
    seen_entities: set[str] = set()
    fallback_streak = 0
    stop_reason = "max_rounds_reached"

    for round_index in range(rounds):
        backtrack = round_backtracks[min(round_index, len(round_backtracks) - 1)]
        if round_index == 0:
            query = topic
        else:
            query, used_fallback = _build_rule_refined_query(
                topic=topic,
                latest_text=successful_texts[-1],
                goal=goal,
                round_index=round_index,
                latest_structured=successful_structured[-1],
            )
            fallback_streak = fallback_streak + 1 if used_fallback else 0

        queries_used.append(query)
        time.sleep(random.uniform(1.5, 3.5))
        result = gemini_search(
            f"deep_research_r{round_index + 1}",
            query,
            backtrack=backtrack,
            structured_mode="p7",
        )
        if not result or not result.get("text"):
            logging.warning(f"[P7] search returned empty for '{topic}' round {round_index + 1}")
            if round_index == 0:
                stop_reason = "empty_first_round"
            else:
                stop_reason = "search_empty"
            break

        result_text = result["text"].strip()
        if not result_text:
            logging.warning(f"[P7] search returned whitespace-only output for '{topic}' round {round_index + 1}")
            stop_reason = "empty_first_round" if round_index == 0 else "search_empty"
            break
        if is_no_evidence_text(result_text):
            logging.info(f"[P7] search found no evidence for '{topic}' round {round_index + 1}")
            stop_reason = "no_evidence_first_round" if round_index == 0 else "no_further_evidence"
            break
        round_evidence = _p7_round_evidence_provenance(result)
        round_evidence_record = {
            key: value
            for key, value in round_evidence.items()
            if key != "rounds"
        }
        round_evidence_record["round_index"] = round_index + 1
        round_evidence_records.append(round_evidence_record)
        structured = result.get("structured") if isinstance(result, dict) else None
        analysis_lines = _structured_claim_lines(structured, result_text)
        analysis_text = "\n".join(analysis_lines) if analysis_lines else result_text
        novelty = _estimate_incremental_novelty(successful_texts, analysis_text)
        coverage = _structured_coverage(structured, result_text)
        new_coverage = [name for name, enabled in coverage.items() if enabled]
        coverage_hits.update(new_coverage)

        if isinstance(structured, dict):
            entities = {item.lower() for item in _structured_entity_terms(structured, result_text)}
        else:
            entities = _extract_entities(result_text)
        new_entities = entities - seen_entities
        seen_entities.update(entities)

        round_sections.append(
            _format_round_section(
                round_index=round_index + 1,
                query=query,
                backtrack=backtrack,
                goal=goal,
                result_text=result_text,
                structured=structured if isinstance(structured, dict) else None,
                sources=result.get("grounding_sources", []),
                novelty=novelty,
                coverage=coverage,
                new_entities_count=len(new_entities),
                evidence_provenance=round_evidence,
            )
        )
        successful_texts.append(analysis_text)
        successful_structured.append(structured if isinstance(structured, dict) else None)

        if fallback_streak >= 2:
            stop_reason = "fallback_streak"
            break
        if round_index >= 1 and not new_entities:
            stop_reason = "no_new_entities"
            break
        if round_index >= 1 and novelty < 0.18:
            stop_reason = "low_novelty"
            break
        if round_index >= 1 and len(coverage_hits) == 4:
            stop_reason = "coverage_complete"
            break

    if not round_sections:
        status = "skipped_no_results" if stop_reason == "no_evidence_first_round" else "failed_empty_output"
        evidence_provenance = _merge_p7_evidence_provenance(round_evidence_records)
        evidence_provenance["rounds"] = round_evidence_records
        return {
            "topic": topic,
            "text": "",
            "status": status,
            "stop_reason": stop_reason,
            "rounds_run": 0,
            "successful_rounds": 0,
            "queries_used": queries_used,
            "coverage": [],
            "evidence_provenance": evidence_provenance,
        }

    coverage_list = sorted(coverage_hits)
    evidence_provenance = _merge_p7_evidence_provenance(round_evidence_records)
    evidence_provenance["rounds"] = round_evidence_records
    evidence_boundary = build_p7_evidence_boundary(evidence_provenance)
    header_lines = [
        f"{P7_TOPIC_PREFIX}{topic}",
        "",
        f"> Goal: {goal or 'Explain the cause and the concrete implication.'}",
        f"> Rounds executed: {len(round_sections)}/{rounds}",
        f"> Stop reason: {stop_reason}",
        f"> Coverage: {', '.join(coverage_list) if coverage_list else 'none'}",
    ]
    if evidence_boundary:
        header_lines.append(f"> {evidence_boundary}")
    header_lines.extend(["", "\n\n".join(round_sections)])
    return {
        "topic": topic,
        "text": "\n".join(header_lines).strip(),
        "status": "completed",
        "stop_reason": stop_reason,
        "rounds_run": len(round_sections),
        "successful_rounds": len(successful_texts),
        "queries_used": queries_used,
        "coverage": coverage_list,
        "evidence_provenance": evidence_provenance,
    }


def run_deep_research(plan: dict[str, object]) -> dict[str, object]:
    topics = list(plan.get("deep_topics") or [])
    rejected_names = normalize_rejected_stock_names(plan.get("rejected_target_names"))
    if not plan.get("should_run_p7") or not topics:
        return {
            "status": "skipped_not_triggered",
            "text": "",
            "topics_planned": len(topics),
            "topics_completed": 0,
            "rounds_run": 0,
            "successful_rounds": 0,
            "goal": plan.get("goal", ""),
            "triggers": list(plan.get("triggers") or []),
            "topic_results": [],
            "evidence_provenance": {},
        }

    rounds = int(plan.get("max_rounds") or 0) or 2
    round_backtracks = list(plan.get("round_backtracks") or ROUND_BACKTRACKS[:rounds])
    goal = str(plan.get("goal") or "")

    topic_results: list[dict[str, object]] = []
    topic_evidence_records: list[dict[str, object]] = []
    all_sections: list[str] = []
    rejected_identity_lines_removed = 0
    max_workers = min(3, len(topics))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            topic: pool.submit(
                iterative_deep_search,
                topic,
                rounds=rounds,
                goal=goal,
                round_backtracks=round_backtracks,
            )
            for topic in topics
        }
        for topic in topics:
            try:
                result = futures[topic].result()
            except Exception as exc:
                logging.warning(f"[P7] topic worker failed for '{topic}': {exc}")
                result = {
                    "topic": topic,
                    "text": "",
                    "status": "failed_exception",
                    "stop_reason": "worker_exception",
                    "rounds_run": 0,
                    "successful_rounds": 0,
                    "queries_used": [],
                    "coverage": [],
                }
            topic_provenance = normalize_p7_evidence_provenance(
                result.get("evidence_provenance")
            )
            if not topic_provenance:
                topic_provenance = _empty_p7_evidence_provenance()
            result["evidence_provenance"] = topic_provenance
            topic_evidence_record = {
                "topic": str(result.get("topic") or topic),
                "status": str(result.get("status") or ""),
                **{
                    key: value
                    for key, value in topic_provenance.items()
                    if key != "rounds"
                },
            }
            topic_rounds = topic_provenance.get("rounds")
            if isinstance(topic_rounds, list):
                topic_evidence_record["rounds"] = topic_rounds
            topic_evidence_records.append(topic_evidence_record)
            topic_results.append(result)
            if result.get("text"):
                filtered_text, removed_count = filter_rejected_stock_lines(
                    result["text"],
                    rejected_names,
                )
                result["text"] = filtered_text
                result["rejected_identity_lines_removed"] = removed_count
                rejected_identity_lines_removed += removed_count
                if filtered_text:
                    all_sections.append(filtered_text)

    combined = "\n\n".join(all_sections).strip()
    aggregate_evidence_provenance = _merge_p7_evidence_provenance(
        [item.get("evidence_provenance") for item in topic_results]
    )
    aggregate_evidence_provenance["topics"] = topic_evidence_records
    aggregate_evidence_boundary = build_p7_evidence_boundary(aggregate_evidence_provenance)
    if combined and aggregate_evidence_boundary:
        combined = f"> {aggregate_evidence_boundary}\n\n{combined}"
    topic_statuses = [str(item.get("status") or "") for item in topic_results]
    if combined:
        aggregate_status = "completed"
    elif topic_statuses and all(status == "skipped_no_results" for status in topic_statuses):
        aggregate_status = "skipped_no_results"
    else:
        aggregate_status = "failed_empty_output"
    return {
        "status": aggregate_status,
        "text": combined,
        "topics_planned": len(topics),
        "topics_completed": sum(1 for item in topic_results if item.get("text")),
        "rounds_run": sum(int(item.get("rounds_run", 0)) for item in topic_results),
        "successful_rounds": sum(int(item.get("successful_rounds", 0)) for item in topic_results),
        "goal": goal,
        "triggers": list(plan.get("triggers") or []),
        "topic_results": topic_results,
        "skipped_recent_topics": list(plan.get("skipped_recent_topics") or []),
        "rejected_target_names": rejected_names,
        "rejected_identity_lines_removed": rejected_identity_lines_removed,
        "evidence_provenance": aggregate_evidence_provenance,
    }
