"""Search engine utilities with key rotation, channel breaker, and Tavily fallback."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime

import requests
from ratelimit import limits, sleep_and_retry

from scraper_utils import enrich_search_urls
from settings import (
    GEMINI_SEARCH_DIRECT_URL,
    GEMINI_SEARCH_MODEL,
    SEARCH_API_KEY_POOL,
    TAVILY_API_KEY,
)
from utils import extract_json_from_llm_response, parse_backtrack_hours


class SearchKeyRotator:
    """Thread-safe Gemini search key rotator."""

    def __init__(self, keys: list[str], base_url: str):
        self._keys = keys
        self._index = 0
        self._exhausted: set[int] = set()
        self._lock = threading.Lock()
        self._base_url = base_url

    @property
    def total(self) -> int:
        return len(self._keys)

    @property
    def available(self) -> int:
        return self.total - len(self._exhausted)

    def get_client(self) -> tuple[str | None, str | None, int, str]:
        with self._lock:
            if not self._keys or len(self._exhausted) >= len(self._keys):
                return None, None, -1, "N/A"

            while self._index in self._exhausted:
                self._index = (self._index + 1) % len(self._keys)

            idx = self._index
            self._index = (self._index + 1) % len(self._keys)
            key = self._keys[idx]
            return self._base_url, key, idx, key[-4:]

    def mark_exhausted(self, key_index: int):
        with self._lock:
            if key_index < 0 or key_index >= len(self._keys):
                return
            self._exhausted.add(key_index)
            remaining = self.total - len(self._exhausted)
            key_last4 = self._keys[key_index][-4:]
            if remaining > 0:
                self._index = (key_index + 1) % len(self._keys)
                while self._index in self._exhausted:
                    self._index = (self._index + 1) % len(self._keys)
                next_last4 = self._keys[self._index][-4:]
                logging.warning(
                    f"[search] key ...{key_last4} exhausted; switch to ...{next_last4} ({remaining}/{self.total})"
                )
            else:
                logging.error(f"[search] all {self.total} keys exhausted")


class ChannelCircuitBreaker:
    """Thread-safe per-channel circuit breaker with one HALF_OPEN probe."""

    def __init__(self, threshold: int = 2, cooldown_sec: int = 120, max_cooldown_sec: int = 300):
        self._failures: dict[str, int] = {}
        self._states: dict[str, str] = {}
        self._opened_at: dict[str, float] = {}
        self._cooldowns: dict[str, int] = {}
        self._threshold = threshold
        self._initial_cooldown = cooldown_sec
        self._max_cooldown = max(max_cooldown_sec, cooldown_sec)
        self._lock = threading.Lock()

    def record_failure(self, channel_name: str):
        with self._lock:
            state = self._states.get(channel_name, "closed")
            if state == "open":
                # Requests admitted before OPEN may finish later; they must not
                # extend the cooldown or emit duplicate trip events.
                return
            if state == "half_open":
                self._failures[channel_name] = self._threshold
                self._states[channel_name] = "open"
                self._opened_at[channel_name] = time.time()
                self._cooldowns[channel_name] = self._max_cooldown
                logging.warning(
                    f"[search] half-open probe failed for {channel_name}; reopen for {self._max_cooldown}s"
                )
                return

            self._failures[channel_name] = self._failures.get(channel_name, 0) + 1
            if self._failures[channel_name] >= self._threshold:
                self._states[channel_name] = "open"
                self._opened_at[channel_name] = time.time()
                self._cooldowns[channel_name] = self._initial_cooldown
                logging.warning(
                    f"[search] circuit breaker opened for {channel_name} after {self._threshold} failures; "
                    f"cooldown={self._initial_cooldown}s"
                )

    def trip(self, channel_name: str):
        with self._lock:
            self._failures[channel_name] = self._threshold
            self._states[channel_name] = "open"
            self._opened_at[channel_name] = time.time()
            self._cooldowns[channel_name] = self._initial_cooldown

    def record_success(self, channel_name: str):
        with self._lock:
            if self._states.get(channel_name, "closed") == "open":
                # Ignore late successes from requests that were already in flight
                # when another request opened the circuit.
                return
            self._failures[channel_name] = 0
            self._states[channel_name] = "closed"
            self._opened_at.pop(channel_name, None)
            self._cooldowns.pop(channel_name, None)

    def state(self, channel_name: str) -> str:
        with self._lock:
            return self._states.get(channel_name, "closed")

    def is_tripped(self, channel_name: str) -> bool:
        with self._lock:
            state = self._states.get(channel_name, "closed")
            if state == "closed":
                return False
            if state == "half_open":
                return True

            elapsed = time.time() - self._opened_at.get(channel_name, 0)
            cooldown = self._cooldowns.get(channel_name, self._initial_cooldown)
            if elapsed >= cooldown:
                self._states[channel_name] = "half_open"
                logging.info(f"[search] cooldown finished for {channel_name}; allow one half-open probe")
                return False
            return True


search_rotator_direct = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_DIRECT_URL)
channel_breaker = ChannelCircuitBreaker(threshold=2)
_http_session = requests.Session()
GEMINI_CONCURRENCY = max(search_rotator_direct.total, 1)
_GEMINI_KEY_QUERY_RE = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)
_SEARCH_PERMISSION_MARKERS = (
    "permission_denied",
    "service_disabled",
    "cloud code private api",
    "cloudcode-pa.googleapis.com",
    "api has not been used",
)
_SEARCH_QUOTA_MARKERS = (
    "resource_exhausted",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
)


def _redact_sensitive_log_text(value: object) -> str:
    return _GEMINI_KEY_QUERY_RE.sub(r"\1<redacted>", str(value))


def _search_response_text(response: requests.Response) -> str:
    return _redact_sensitive_log_text(getattr(response, "text", ""))


def _classify_search_http_response(response: requests.Response) -> str:
    status_code = getattr(response, "status_code", 0)
    if status_code == 408:
        return "server_error"
    if status_code == 400:
        text = _search_response_text(response).lower()
        if "location is not supported" in text or "failed_precondition" in text:
            return "unsupported_location"
        return "bad_request"
    if status_code == 429:
        return "quota_exhausted"
    if 500 <= status_code < 600:
        return "server_error"
    if status_code != 403:
        return ""

    text = _search_response_text(response).lower()
    if any(marker in text for marker in _SEARCH_PERMISSION_MARKERS):
        return "permission_denied"
    if any(marker in text for marker in _SEARCH_QUOTA_MARKERS):
        return "quota_exhausted"
    return "forbidden"


def _handle_search_limited_response(
    *,
    category: str,
    channel_name: str,
    rotator: SearchKeyRotator,
    key_idx: int,
    key_last4: str,
    response: requests.Response,
) -> str:
    classification = _classify_search_http_response(response)
    if not classification:
        return ""

    if classification == "quota_exhausted":
        logging.warning(f"[{category}] key ...{key_last4} limited on {channel_name}")
        rotator.mark_exhausted(key_idx)
        return classification

    if classification in {"bad_request", "unsupported_location"}:
        channel_breaker.trip(channel_name)
    else:
        channel_breaker.record_failure(channel_name)
    logging.warning(
        f"[{category}] {classification} on {channel_name}; keep key ...{key_last4} available; "
        f"breaker={channel_breaker.state(channel_name)} | "
        f"{_search_response_text(response)[:240]}"
    )
    return classification


_SERVER_ERROR_RETRY_WINDOWS = ((3.0, 6.0), (8.0, 15.0), (18.0, 30.0))


def _server_error_retry_delay(attempt: int) -> float:
    low, high = _SERVER_ERROR_RETRY_WINDOWS[min(attempt, len(_SERVER_ERROR_RETRY_WINDOWS) - 1)]
    return random.uniform(low, high)


def _log_server_error_retry(
    *,
    category: str,
    channel_name: str,
    key_last4: str,
    attempt: int,
    max_retries: int,
    delay: float,
    prompt: str,
):
    logging.warning(
        f"[{category}] transient server error; retry same key ...{key_last4} in {delay:.1f}s "
        f"({attempt + 2}/{max_retries}); channel={channel_name}; model={GEMINI_SEARCH_MODEL}; "
        f"tool=google_search; prompt_chars={len(prompt)}"
    )

P7_STRUCTURED_MODE = "p7"
_TAVILY_FALLBACK_MARKER = "[Tavily Fallback]"
_TAVILY_ENTITY_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "latest",
    "market",
    "industry",
    "company",
    "search",
    "source",
    "news",
    "about",
    "their",
    "will",
    "into",
    "over",
    "more",
}


def _enrich_search_result(result: dict, *, structured_mode: str | None) -> dict:
    """Keep the legacy enrichment call shape outside provenance-aware P7."""
    if structured_mode != P7_STRUCTURED_MODE:
        return enrich_search_urls(result)
    try:
        return enrich_search_urls(result, include_evidence_metadata=True)
    except TypeError as exc:
        # Injected legacy enrichers (including integration adapters) may still
        # expose the old one-argument callable.  Their missing provenance is
        # treated conservatively by P7 rather than breaking the search result.
        if "include_evidence_metadata" not in str(exc):
            raise
        return enrich_search_urls(result)
_TAVILY_COVERAGE_MARKERS = {
    "cause": ("cause", "driver", "driven", "because", "due to", "原因", "驱动", "触发", "由于"),
    "winners": ("benefit", "winner", "upside", "gain", "利好", "受益", "赢家", "增长"),
    "losers": ("loser", "downside", "risk", "pressure", "decline", "利空", "受损", "风险", "压力"),
    "alternatives": ("alternative", "substitute", "backup", "competition", "替代", "备选", "竞争", "格局"),
}
def _normalize_string_list(values, *, limit: int, max_length: int) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > max_length:
            text = text[:max_length].rstrip()
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_p7_structured_payload(payload: dict) -> dict:
    coverage = payload.get("coverage_flags")
    if not isinstance(coverage, dict):
        coverage = {}
    return {
        "summary": str(payload.get("summary") or "").strip()[:400],
        "claims": _normalize_string_list(payload.get("claims"), limit=6, max_length=240),
        "entities": _normalize_string_list(payload.get("entities"), limit=8, max_length=80),
        "dates": _normalize_string_list(payload.get("dates"), limit=6, max_length=40),
        "implications": _normalize_string_list(payload.get("implications"), limit=4, max_length=240),
        "open_questions": _normalize_string_list(payload.get("open_questions"), limit=4, max_length=240),
        "coverage_flags": {
            "cause": bool(coverage.get("cause")),
            "winners": bool(coverage.get("winners")),
            "losers": bool(coverage.get("losers")),
            "alternatives": bool(coverage.get("alternatives")),
        },
    }


def _render_p7_structured_text(payload: dict) -> str:
    parts: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    claims = payload.get("claims") or []
    implications = payload.get("implications") or []
    open_questions = payload.get("open_questions") or []
    dates = payload.get("dates") or []

    if summary:
        parts.append(f"Summary: {summary}")
    for claim in claims:
        parts.append(f"- {claim}")
    if dates:
        parts.append(f"Dates: {', '.join(dates)}")
    if implications:
        parts.append("Implications:")
        parts.extend(f"- {item}" for item in implications)
    if open_questions:
        parts.append("Open questions:")
        parts.extend(f"- {item}" for item in open_questions)
    return "\n".join(parts).strip()


def compact_search_text(value: str, *, max_chars: int = 240) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def split_tavily_result_blocks(raw_text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in (raw_text or "").splitlines():
        if line.startswith("- "):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return blocks


def _parse_tavily_text_items(raw_text: str, sources: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    sources = sources or []
    items: list[dict[str, str]] = []
    for index, block in enumerate(split_tavily_result_blocks(raw_text), 1):
        text = block[2:].strip() if block.startswith("- ") else block.strip()
        if not text:
            continue

        url = ""
        url_match = re.search(r"\[(https?://[^\]\s]+)\]\s*$", text, flags=re.DOTALL)
        if url_match:
            url = url_match.group(1).strip()
            text = text[: url_match.start()].strip()
        elif index - 1 < len(sources):
            url = str(sources[index - 1].get("url", "")).strip()

        if ": " in text:
            title, content = text.split(": ", 1)
        else:
            title, content = f"Tavily result {index}", text

        items.append(
            {
                "title": compact_search_text(title, max_chars=100),
                "content": compact_search_text(content),
                "url": url,
            }
        )
        if len(items) >= 5:
            break
    return items


def _extract_tavily_answer(raw_text: str) -> str:
    for line in (raw_text or "").splitlines():
        if line.startswith(_TAVILY_FALLBACK_MARKER):
            return compact_search_text(line.replace(_TAVILY_FALLBACK_MARKER, "", 1), max_chars=400)
    return ""


def _extract_tavily_entities(text: str, *, limit: int = 8) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Z][A-Za-z0-9&\.-]{1,}|[A-Za-z]{3,}|[\u4e00-\u9fff]{2,8}", text or ""):
        lowered = token.lower()
        if lowered in _TAVILY_ENTITY_STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        entities.append(token)
        if len(entities) >= limit:
            break
    return entities


def _extract_tavily_dates(text: str) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for pattern in (r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", r"20\d{2}年\d{1,2}月\d{1,2}日"):
        for match in re.findall(pattern, text or ""):
            if match in seen:
                continue
            seen.add(match)
            dates.append(match)
            if len(dates) >= 6:
                return dates
    return dates


def _coverage_from_tavily_text(text: str) -> dict[str, bool]:
    lowered = (text or "").lower()
    return {
        key: any(marker.lower() in lowered for marker in markers)
        for key, markers in _TAVILY_COVERAGE_MARKERS.items()
    }


def _tavily_open_questions(coverage: dict[str, bool], query: str) -> list[str]:
    questions: list[str] = []
    if not coverage.get("cause"):
        questions.append(f"Need clearer cause and timing evidence for {query}.")
    if not coverage.get("winners"):
        questions.append("Need concrete beneficiary mapping to suppliers or A-share names.")
    if not coverage.get("losers"):
        questions.append("Need explicit downside or losing-side evidence.")
    if not coverage.get("alternatives"):
        questions.append("Need alternatives, substitutes, or competitive-response context.")
    return questions[:4]


def _structured_payload_from_tavily_result(
    result: dict,
    *,
    query: str,
    structured_mode: str | None,
) -> dict | None:
    if structured_mode != P7_STRUCTURED_MODE or not isinstance(result, dict):
        return None

    text = str(result.get("text") or "")
    if not result.get("is_fallback") and _TAVILY_FALLBACK_MARKER not in text:
        return None

    raw_items = result.get("fallback_results")
    items = raw_items if isinstance(raw_items, list) else []
    if not items:
        items = _parse_tavily_text_items(text, result.get("grounding_sources", []))

    claims = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = compact_search_text(
            str(item.get("content") or item.get("title") or ""),
            max_chars=240,
        )
        if claim:
            claims.append(claim)
        if len(claims) >= 6:
            break

    summary = _extract_tavily_answer(text) or (claims[0] if claims else "")
    corpus = " ".join(
        [query, text]
        + [
            f"{item.get('title', '')} {item.get('content', '')}"
            for item in items
            if isinstance(item, dict)
        ]
    )
    coverage = _coverage_from_tavily_text(corpus)
    payload = {
        "summary": summary,
        "claims": claims,
        "entities": _extract_tavily_entities(corpus),
        "dates": _extract_tavily_dates(corpus),
        "implications": [
            claim
            for claim in claims
            if any(marker.lower() in claim.lower() for marker in ("benefit", "winner", "impact", "pressure", "利好", "受益", "影响", "压力"))
        ][:4]
        or claims[:2],
        "open_questions": _tavily_open_questions(coverage, query),
        "coverage_flags": coverage,
    }
    return _normalize_p7_structured_payload(payload)


def _parse_structured_payload(raw_text: str, structured_mode: str | None) -> dict | None:
    if not structured_mode or not raw_text.strip():
        return None
    try:
        payload = extract_json_from_llm_response(raw_text, expect_type="dict")
    except json.JSONDecodeError:
        return None

    if structured_mode == P7_STRUCTURED_MODE:
        return _normalize_p7_structured_payload(payload)
    return None


def _build_search_prompt(
    category: str,
    query: str,
    backtrack: str,
    *,
    structured_mode: str | None = None,
) -> str:
    hours = parse_backtrack_hours(backtrack, default=0)
    # Why: 深度研究和补充搜索不需要时间窗口限制，只有新闻采集(P1/P3)才需要
    #      backtrack 为空或 0 时表示不限制时间，让模型自由搜索所有相关信息
    if hours <= 0:
        window_policy = (
            "Search window: no time restriction. "
            "Search all available information regardless of publication date."
        )
        backtrack_label = "unlimited"
    elif hours <= 72:
        window_policy = (
            f"Search window: focus on the last {hours} hours. "
            "Discard older items unless they are required to explain the direct cause."
        )
        backtrack_label = backtrack
    else:
        window_policy = (
            f"Search window: prioritize the last 72 hours, but allow up to the last {hours} hours "
            "for background, precedent, and named-entity context."
        )
        backtrack_label = backtrack
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    if structured_mode == P7_STRUCTURED_MODE:
        return (
            f"Current Beijing time: {now_str}\n\n"
            "You are an AI industry-chain researcher. Search the topic below and return only grounded facts.\n"
            f"Topic: {query}\n"
            f"Category: {category}\n"
            f"Search window: {backtrack_label}\n"
            f"{window_policy}\n"
            "Return a JSON object only. Do not wrap it in markdown.\n"
            "The JSON must contain: summary, claims, entities, dates, implications, open_questions, coverage_flags.\n"
            "claims: 3-6 grounded factual statements.\n"
            "entities: 0-8 specific companies, technologies, materials, products, or industry-chain nodes.\n"
            "dates: explicit dates or time anchors only.\n"
            "implications: concrete downstream or A-share transmission implications only.\n"
            "open_questions: unresolved evidence gaps only.\n"
            "coverage_flags: booleans for cause, winners, losers, alternatives.\n"
            "If nothing reliable is found, return empty arrays, an empty summary, and all coverage flags false."
        )
    return (
        f"Current Beijing time: {now_str}\n\n"
        "You are an AI industry-chain researcher. Search the topic below and return only grounded facts.\n"
        f"Topic: {query}\n"
        f"Category: {category}\n"
        f"Search window: {backtrack_label}\n"
        f"{window_policy}\n"
        "Output format: 1. [Entity] factual statement [Source: URL | date]\n"
        "If nothing is found, say \"not found\". Do not fabricate."
    )


def _tavily_fallback(query: str, category: str = "raw_search", return_dict: bool = False):
    if not TAVILY_API_KEY:
        return {} if return_dict else ""
    try:
        logging.info(f"[{category}] fallback to Tavily")
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query[:400],
                "search_depth": "advanced",
                "max_results": 5,
                "include_answer": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        answer = data.get("answer", "")
        results = data.get("results", [])
        parts: list[str] = []
        sources: list[dict[str, str]] = []
        fallback_results: list[dict[str, str]] = []
        if answer:
            parts.append(f"{_TAVILY_FALLBACK_MARKER} {answer}")
        for item in results[:5]:
            title = item.get("title", "")
            content = item.get("content", "")
            url = item.get("url", "")
            parts.append(f"- {title}: {content[:300]} [{url}]")
            fallback_results.append(
                {
                    "title": str(title).strip(),
                    "content": str(content).strip(),
                    "url": str(url).strip(),
                }
            )
            if url:
                sources.append({"url": url, "title": title})
        if not parts:
            return {} if return_dict else ""
        channel_breaker.record_success("tavily")
        text = "\n".join(parts)
        if return_dict:
            return {
                "text": text,
                "grounding_sources": sources,
                "is_fallback": True,
                "fallback_provider": "tavily",
                "fallback_results": fallback_results,
            }
        return text
    except Exception as exc:
        logging.warning(f"[{category}] Tavily fallback failed: {exc}")
        return {} if return_dict else ""


def _post_search_request(
    *,
    base_url: str,
    key: str,
    prompt: str,
    temperature: float,
) -> requests.Response:
    url = f"{base_url}/v1beta/models/{GEMINI_SEARCH_MODEL}:generateContent"
    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": 8192,
    }
    # Why: Google Search grounding 与 JSON 结构化输出互斥；P7 结构化模式
    #      在 prompt 中要求 JSON，拿到纯文本后再由代码解析。
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": generation_config,
    }
    return _http_session.post(
        url,
        params={"key": key},
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=180,
    )


def _extract_gemini_text_and_sources(data: dict) -> tuple[str, list[dict[str, str]]]:
    candidates = data.get("candidates") or []
    if not candidates:
        return "", []

    candidate = candidates[0]
    text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
    grounding = candidate.get("groundingMetadata", {}).get("groundingChunks", [])

    sources: list[dict[str, str]] = []
    for chunk in grounding:
        web = chunk.get("web", {})
        if web and web.get("uri"):
            sources.append({"url": web.get("uri"), "title": web.get("title", "")})

    return text, sources


def raw_gemini_search(prompt: str, max_retries: int = 3, *, return_dict: bool = False):
    channels = [("direct", search_rotator_direct)]

    for channel_name, rotator in channels:
        stop_channel = False
        for _ in range(rotator.total):
            if channel_breaker.is_tripped(channel_name):
                logging.debug(f"[raw_search] skip tripped channel {channel_name}")
                break

            base_url, key, key_idx, key_last4 = rotator.get_client()
            if base_url is None or key is None:
                logging.error(f"[raw_search] no available key on {channel_name}")
                break

            for attempt in range(max_retries):
                try:
                    response = _post_search_request(
                        base_url=base_url,
                        key=key,
                        prompt=prompt,
                        temperature=0.7,
                    )
                    classification = _handle_search_limited_response(
                        category="raw_search",
                        channel_name=channel_name,
                        rotator=rotator,
                        key_idx=key_idx,
                        key_last4=key_last4,
                        response=response,
                    )
                    if classification:
                        if classification == "server_error":
                            if attempt < max_retries - 1 and channel_breaker.state(channel_name) == "closed":
                                delay = _server_error_retry_delay(attempt)
                                _log_server_error_retry(
                                    category="raw_search",
                                    channel_name=channel_name,
                                    key_last4=key_last4,
                                    attempt=attempt,
                                    max_retries=max_retries,
                                    delay=delay,
                                    prompt=prompt,
                                )
                                time.sleep(delay)
                                continue
                            stop_channel = True
                        elif classification != "quota_exhausted":
                            stop_channel = True
                        break

                    response.raise_for_status()
                    data = response.json()
                    text, sources = _extract_gemini_text_and_sources(data)
                    if not text.strip():
                        logging.warning(f"[raw_search] empty Gemini response on {channel_name}")
                        time.sleep(3)
                        continue
                    channel_breaker.record_success(channel_name)
                    if return_dict:
                        return {"text": text, "grounding_sources": sources}
                    return text
                except requests.exceptions.Timeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f"[raw_search] request timeout on {channel_name}; switching to fallback")
                    stop_channel = True
                    break
                except Exception as exc:
                    err_str = _redact_sensitive_log_text(exc)
                    err_lower = err_str.lower()
                    if "429" in err_lower or "403" in err_lower or "quota" in err_lower:
                        logging.warning(f"[raw_search] quota exhausted for key ...{key_last4}")
                        rotator.mark_exhausted(key_idx)
                        break
                    wait = 3 * (2**attempt) + random.uniform(0, 2)
                    logging.warning(
                        f"[raw_search] exception on {channel_name}: {err_str}; retry in {wait:.0f}s ({attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)

            if stop_channel:
                break

    result = _tavily_fallback(prompt, category="raw_search", return_dict=return_dict)
    if result:
        return result

    logging.error("[raw_search] all search routes failed")
    return {} if return_dict else ""


@sleep_and_retry
@limits(calls=1, period=1)
def gemini_search(
    category: str,
    query: str,
    backtrack: str = "24h",
    max_retries: int = 3,
    *,
    structured_mode: str | None = None,
) -> dict:
    prompt = _build_search_prompt(category, query, backtrack, structured_mode=structured_mode)
    channels = [("direct", search_rotator_direct)]

    for channel_name, rotator in channels:
        stop_channel = False
        for _ in range(rotator.total):
            if channel_breaker.is_tripped(channel_name):
                logging.debug(f"[{category}] skip tripped channel {channel_name}")
                break

            base_url, key, key_idx, key_last4 = rotator.get_client()
            if base_url is None or key is None:
                logging.error(f"[{category}] no available key on {channel_name}")
                break

            for attempt in range(max_retries):
                try:
                    response = _post_search_request(
                        base_url=base_url,
                        key=key,
                        prompt=prompt,
                        temperature=1.0,
                    )
                    classification = _handle_search_limited_response(
                        category=category,
                        channel_name=channel_name,
                        rotator=rotator,
                        key_idx=key_idx,
                        key_last4=key_last4,
                        response=response,
                    )
                    if classification:
                        if classification == "server_error":
                            if attempt < max_retries - 1 and channel_breaker.state(channel_name) == "closed":
                                delay = _server_error_retry_delay(attempt)
                                _log_server_error_retry(
                                    category=category,
                                    channel_name=channel_name,
                                    key_last4=key_last4,
                                    attempt=attempt,
                                    max_retries=max_retries,
                                    delay=delay,
                                    prompt=prompt,
                                )
                                time.sleep(delay)
                                continue
                            stop_channel = True
                        elif classification != "quota_exhausted":
                            stop_channel = True
                        break

                    response.raise_for_status()
                    data = response.json()
                    text, sources = _extract_gemini_text_and_sources(data)
                    if not text.strip():
                        return {}

                    channel_breaker.record_success(channel_name)
                    structured_payload = _parse_structured_payload(text, structured_mode)
                    result = {
                        "text": _render_p7_structured_text(structured_payload)
                        if structured_payload and structured_mode == P7_STRUCTURED_MODE
                        else text,
                        "grounding_sources": sources,
                    }
                    if structured_payload:
                        result["structured"] = structured_payload
                        result["raw_text"] = text
                    return _enrich_search_result(result, structured_mode=structured_mode)
                except requests.exceptions.Timeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f"[{category}] request timeout on {channel_name}; switching to fallback")
                    stop_channel = True
                    break
                except Exception as exc:
                    err_str = _redact_sensitive_log_text(exc)
                    err_lower = err_str.lower()
                    if "429" in err_lower or "403" in err_lower or "quota" in err_lower:
                        logging.warning(f"[{category}] quota exhausted for key ...{key_last4}")
                        rotator.mark_exhausted(key_idx)
                        break
                    wait = 3 * (2**attempt) + random.uniform(0, 2)
                    logging.warning(
                        f"[{category}] exception on {channel_name}: {err_str}; retry in {wait:.0f}s ({attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)

            if stop_channel:
                break

    result = _tavily_fallback(query, category=category, return_dict=True)
    if result:
        structured_payload = _structured_payload_from_tavily_result(
            result,
            query=query,
            structured_mode=structured_mode,
        )
        if structured_payload:
            result["raw_text"] = result.get("text", "")
            result["structured"] = structured_payload
            result["text"] = _render_p7_structured_text(structured_payload)
        return _enrich_search_result(result, structured_mode=structured_mode)

    logging.error(f"[{category}] all search routes failed")
    return {}
