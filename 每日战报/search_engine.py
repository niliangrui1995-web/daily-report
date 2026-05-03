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
    GEMINI_SEARCH_CF_URL,
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
    """Per-channel circuit breaker with cooldown probing."""

    def __init__(self, threshold: int = 2, cooldown_sec: int = 300):
        self._failures: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._tripped_time: dict[str, float] = {}
        self._threshold = threshold
        self._cooldown = cooldown_sec
        self._lock = threading.Lock()

    def record_failure(self, channel_name: str):
        with self._lock:
            self._failures[channel_name] = self._failures.get(channel_name, 0) + 1
            if self._failures[channel_name] >= self._threshold:
                self._tripped.add(channel_name)
                self._tripped_time[channel_name] = time.time()
                logging.warning(
                    f"[search] circuit breaker tripped for {channel_name} after {self._threshold} failures"
                )

    def record_success(self, channel_name: str):
        with self._lock:
            self._failures[channel_name] = 0
            self._tripped.discard(channel_name)
            self._tripped_time.pop(channel_name, None)

    def is_tripped(self, channel_name: str) -> bool:
        with self._lock:
            if channel_name not in self._tripped:
                return False
            elapsed = time.time() - self._tripped_time.get(channel_name, 0)
            if elapsed > self._cooldown:
                self._tripped.discard(channel_name)
                self._tripped_time.pop(channel_name, None)
                logging.info(f"[search] cooldown finished for {channel_name}; probe once")
                return False
            return True


search_rotator_direct = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_DIRECT_URL)
search_rotator_cf = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_CF_URL)
channel_breaker = ChannelCircuitBreaker(threshold=2)
_http_session = requests.Session()
GEMINI_CONCURRENCY = max(search_rotator_direct.total, 1)
_GEMINI_KEY_QUERY_RE = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)


def _redact_sensitive_log_text(value: object) -> str:
    return _GEMINI_KEY_QUERY_RE.sub(r"\1<redacted>", str(value))

P7_STRUCTURED_MODE = "p7"
_P7_STRUCTURED_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "dates": {"type": "array", "items": {"type": "string"}},
        "implications": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "coverage_flags": {
            "type": "object",
            "properties": {
                "cause": {"type": "boolean"},
                "winners": {"type": "boolean"},
                "losers": {"type": "boolean"},
                "alternatives": {"type": "boolean"},
            },
            "required": ["cause", "winners", "losers", "alternatives"],
        },
    },
    "required": [
        "summary",
        "claims",
        "entities",
        "dates",
        "implications",
        "open_questions",
        "coverage_flags",
    ],
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


def _structured_mode_schema(structured_mode: str | None) -> dict | None:
    if structured_mode == P7_STRUCTURED_MODE:
        return _P7_STRUCTURED_RESPONSE_SCHEMA
    return None


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
        if answer:
            parts.append(f"[Tavily Fallback] {answer}")
        for item in results[:5]:
            title = item.get("title", "")
            content = item.get("content", "")
            url = item.get("url", "")
            parts.append(f"- {title}: {content[:300]} [{url}]")
            if url:
                sources.append({"url": url, "title": title})
        if not parts:
            return {} if return_dict else ""
        channel_breaker.record_success("tavily")
        text = "\n".join(parts)
        if return_dict:
            return {"text": text, "grounding_sources": sources}
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
    response_json_schema: dict | None = None,
) -> requests.Response:
    url = f"{base_url}/v1beta/models/{GEMINI_SEARCH_MODEL}:generateContent"
    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": 8192,
    }
    # Why: Google Search grounding（googleSearch 工具）和 JSON 结构化输出互斥。
    #      同时传 responseJsonSchema + googleSearch 会返回 400 Bad Request。
    #      P7 结构化模式改为在 prompt 中要求 JSON，拿到纯文本后用代码解析。
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
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
    channels = [("direct", search_rotator_direct), ("cf_worker", search_rotator_cf)]

    for channel_name, rotator in channels:
        if channel_breaker.is_tripped(channel_name):
            logging.debug(f"[raw_search] skip tripped channel {channel_name}")
            continue

        for _ in range(rotator.total):
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
                    if response.status_code in (429, 403):
                        logging.warning(f"[raw_search] key ...{key_last4} limited on {channel_name}")
                        rotator.mark_exhausted(key_idx)
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
                except requests.exceptions.ConnectTimeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f"[raw_search] connect timeout on {channel_name}")
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
    response_json_schema = _structured_mode_schema(structured_mode)
    channels = [("direct", search_rotator_direct), ("cf_worker", search_rotator_cf)]

    for channel_name, rotator in channels:
        if channel_breaker.is_tripped(channel_name):
            logging.debug(f"[{category}] skip tripped channel {channel_name}")
            continue

        for _ in range(rotator.total):
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
                        response_json_schema=response_json_schema,
                    )
                    if response.status_code in (429, 403):
                        logging.warning(f"[{category}] key ...{key_last4} limited on {channel_name}")
                        rotator.mark_exhausted(key_idx)
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
                    return enrich_search_urls(result)
                except requests.exceptions.ConnectTimeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f"[{category}] connect timeout on {channel_name}")
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

    result = _tavily_fallback(query, category=category, return_dict=True)
    if result:
        return enrich_search_urls(result)

    logging.error(f"[{category}] all search routes failed")
    return {}
