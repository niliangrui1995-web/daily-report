"""P3 机动情报：基于 Gemini Search Grounding 的并行情报采集。"""

import concurrent.futures
import json
import logging
import re
import time
from datetime import datetime

from artifact_store import write_text_artifact
from intel_structuring import normalize_p3_structured_items, render_p3_structured_markdown
from search_engine import raw_gemini_search
from settings import SEARCH_API_KEY_POOL, load_prompt
from utils import extract_json_from_llm_response, parse_backtrack_hours

PRIORITY_KEYWORDS = [
    "NVDA",
    "NVIDIA",
    "nvidia",
    "HBM",
    "hbm",
    "CoWoS",
    "cowos",
    "COWOS",
    "涨价",
    "shortage",
    "缺货",
    "价格上涨",
    "price hike",
    "price increase",
    "短缺",
    "supply shortage",
    "undersupply",
    "封装",
    "packaging",
    "advanced packaging",
    "先进封装",
    "光模块",
    "optical module",
    "transceiver",
    "800G",
    "1.6T",
    "液冷",
    "liquid cooling",
    "cold plate",
    "CDU",
    "产能",
    "capacity",
    "fab",
    "foundry",
    "产线",
    "扩产",
    "expansion",
    "ramp up",
    "量产",
    "良率",
    "yield",
    "defect rate",
    "TSMC",
    "台积电",
    "tsmc",
    "ASML",
    "asml",
    "EUV",
    "SK Hynix",
    "SK海力士",
    "海力士",
    "Samsung",
    "三星",
    "Micron",
    "美光",
    "Broadcom",
    "博通",
    "AVGO",
    "AMD",
    "amd",
    "Intel",
    "intel",
    "英特尔",
    "Qualcomm",
    "高通",
    "Apple",
    "苹果",
    "Microsoft",
    "微软",
    "Google",
    "谷歌",
    "Meta",
    "meta",
    "PCB",
    "LDI",
    "laser direct imaging",
    "激光直接成像",
    "激光钻孔",
    "laser drilling",
    "VCP",
    "垂直连续电镀",
    "AOI",
    "AVI",
    "electrical test",
    "电测",
    "钻针",
    "drill bit",
    "micro drill",
    "Atotech",
    "ESI",
    "Orbotech",
    "CCL",
    "覆铜板",
    "铜箔",
    "光刻胶",
    "photoresist",
    "靶材",
    "target material",
    "电子气体",
    "electronic gas",
    "NF3",
    "WF6",
    "探针卡",
    "probe card",
    "测试",
    "test",
    "CPO",
    "silicon photonics",
    "硅光",
    "chiplet",
    "异构集成",
    "Hybrid Bonding",
    "hybrid bonding",
    "混合键合",
    "ABF",
    "载板",
    "substrate",
    "financial results",
    "earnings",
    "revenue",
    "guidance",
    "财报",
    "营收",
    "指引",
]


def _call_gemini_search(prompt: str, max_retries: int = 3) -> dict:
    result = raw_gemini_search(prompt, max_retries=max_retries, return_dict=True)
    if isinstance(result, dict):
        return {
            "text": result.get("text", "") or "",
            "grounding_sources": result.get("grounding_sources", []) or [],
        }
    return {"text": result or "", "grounding_sources": []}


def _normalize_text_key(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).lower()


def _sanitize_source_url(url: str) -> str:
    return re.sub(r"\s+", "", (url or "").strip())


def _repair_source_url_fields(raw_text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        cleaned_url = _sanitize_source_url(match.group(2))
        return f'{match.group(1)}{cleaned_url}{match.group(3)}'

    return re.sub(r'("source_url"\s*:\s*")([^"]*)(")', _replace, raw_text, flags=re.DOTALL)


def _should_replace_source_url(url: str) -> bool:
    if not url:
        return True
    return "vertexaisearch.cloud.google.com/grounding-api-redirect/" in url.lower()


def _match_grounding_source_url(
    item: dict,
    grounding_sources: list[dict],
    *,
    index: int,
    total_items: int,
) -> str:
    if not grounding_sources:
        return ""

    item_title = _normalize_text_key(item.get("title", ""))
    if item_title:
        for source in grounding_sources:
            source_title = _normalize_text_key(source.get("title", ""))
            if source_title and (
                item_title == source_title
                or item_title in source_title
                or source_title in item_title
            ):
                return _sanitize_source_url(source.get("url", ""))

    if len(grounding_sources) == 1:
        return _sanitize_source_url(grounding_sources[0].get("url", ""))

    if total_items == len(grounding_sources) and index < len(grounding_sources):
        return _sanitize_source_url(grounding_sources[index].get("url", ""))

    return ""


def _apply_grounding_sources(items: list[dict], grounding_sources: list[dict]) -> list[dict]:
    normalized_items: list[dict] = []
    total_items = len(items)

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        normalized_item = dict(item)
        cleaned_url = _sanitize_source_url(normalized_item.get("source_url", ""))
        if _should_replace_source_url(cleaned_url):
            matched_url = _match_grounding_source_url(
                normalized_item,
                grounding_sources,
                index=index,
                total_items=total_items,
            )
            normalized_item["source_url"] = matched_url or cleaned_url
        else:
            normalized_item["source_url"] = cleaned_url

        normalized_items.append(normalized_item)

    return normalized_items


def _persist_raw_response(bucket: str, raw_text: str) -> None:
    artifact_prefix = f"P3_raw_response_{bucket}" if bucket else "P3_raw_response"
    artifact_path = write_text_artifact(artifact_prefix, raw_text)
    logging.warning(
        "[P3] failed to parse Gemini JSON for %s; raw response saved to %s",
        bucket or "unknown",
        artifact_path,
    )


def _parse_json_response(
    raw_text: str,
    *,
    bucket: str = "",
    grounding_sources: list[dict] | None = None,
) -> list[dict]:
    if not raw_text:
        return []

    grounding_sources = grounding_sources or []

    try:
        result = extract_json_from_llm_response(raw_text, expect_type="list")
    except json.JSONDecodeError:
        repaired_text = _repair_source_url_fields(raw_text)
        if repaired_text == raw_text:
            _persist_raw_response(bucket, raw_text)
            return []
        try:
            result = extract_json_from_llm_response(repaired_text, expect_type="list")
        except json.JSONDecodeError:
            _persist_raw_response(bucket, raw_text)
            return []

    if isinstance(result, list):
        return _apply_grounding_sources(result, grounding_sources)
    return []


def _parse_search_result(search_result: dict, bucket: str) -> list[dict]:
    return _parse_json_response(
        search_result.get("text", ""),
        bucket=bucket,
        grounding_sources=search_result.get("grounding_sources", []),
    )


def _format_failed_article_lines(targets: list[dict]) -> str:
    lines = []
    for index, target in enumerate(targets, 1):
        lines.append(
            f"{index}. 标题: {target.get('title', '未知')}\n"
            f"   URL: {target.get('url', '未知')}\n"
            f"   来源: {target.get('source', '未知')}"
        )
    return "\n".join(lines)


def _filter_priority_targets(failed_urls: list[dict], max_targets: int = 10) -> list[dict]:
    if not failed_urls:
        return []

    scored_targets = []
    for item in failed_urls:
        title = item.get("title", "")
        score = sum(1 for keyword in PRIORITY_KEYWORDS if keyword in title)
        if score > 0:
            scored_targets.append((item, score))

    scored_targets.sort(key=lambda row: row[1], reverse=True)
    selected = [item for item, _ in scored_targets[:max_targets]]

    if selected:
        logging.info(
            "[P3 jinnang_1] selected %s high-value targets from %s failed urls",
            len(selected),
            len(failed_urls),
        )
        return selected

    logging.info("[P3 jinnang_1] no high-value failed url matched the priority keyword set")
    return []


def run_jinnang_1(failed_urls: list[dict]) -> list[dict]:
    targets = _filter_priority_targets(failed_urls)
    if not targets:
        return []

    logging.info("[P3 jinnang_1] start recovery search for %s targets", len(targets))
    template = load_prompt("p3_jinnang1.txt")
    now_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")

    all_results = []
    if len(targets) <= 5:
        prompt = template.format(
            failed_articles=_format_failed_article_lines(targets),
            current_time=now_str,
        )
        search_result = _call_gemini_search(prompt)
        all_results.extend(_parse_search_result(search_result, "jinnang_1"))
    else:
        mid = len(targets) // 2
        for batch_index, batch in enumerate([targets[:mid], targets[mid:]], 1):
            prompt = template.format(
                failed_articles=_format_failed_article_lines(batch),
                current_time=now_str,
            )
            logging.info("[P3 jinnang_1] running batch %s/2", batch_index)
            search_result = _call_gemini_search(prompt)
            all_results.extend(_parse_search_result(search_result, "jinnang_1"))
            if batch_index == 1:
                time.sleep(2)

    logging.info("[P3 jinnang_1] recovered %s items", len(all_results))
    return all_results


def run_jinnang_2(hours: int = 24) -> list[dict]:
    logging.info("[P3 jinnang_2] start search")
    template = load_prompt("p3_jinnang2.txt")
    now_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")
    prompt = template.format(hours=hours, current_time=now_str)
    search_result = _call_gemini_search(prompt)
    results = _parse_search_result(search_result, "jinnang_2")
    logging.info("[P3 jinnang_2] captured %s items", len(results))
    return results


def run_jinnang_3(hours: int = 24) -> list[dict]:
    logging.info("[P3 jinnang_3] start search")
    template = load_prompt("p3_jinnang3.txt")
    now_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")
    prompt = template.format(hours=hours, current_time=now_str)
    search_result = _call_gemini_search(prompt)
    results = _parse_search_result(search_result, "jinnang_3")
    logging.info("[P3 jinnang_3] captured %s items", len(results))
    return results


def render_p3_output(
    raw_results: dict[str, list[dict]] | None,
    *,
    collected_at: str | None = None,
) -> tuple[str, list[dict]]:
    structured_items = normalize_p3_structured_items(raw_results)
    if not structured_items:
        return "", []
    return (
        render_p3_structured_markdown(structured_items, collected_at=collected_at),
        structured_items,
    )


def collect_p3_parallel_only(backtrack: str = "24h") -> tuple[str, dict]:
    start_time = time.time()
    stats = {
        "source": "P3 Gemini Search (parallel: jinnang 2+3)",
        "status": "pending",
        "jinnang_1_count": 0,
        "jinnang_2_count": 0,
        "jinnang_3_count": 0,
        "duration_seconds": 0,
        "content_length": 0,
        "structured_items": 0,
    }

    if not SEARCH_API_KEY_POOL:
        logging.warning("[P3] GEMINI_SEARCH_API_KEYS not configured, skip parallel search")
        stats["status"] = "skipped_no_key"
        return "", stats

    logging.info("[P3] start parallel search: jinnang_2 + jinnang_3")
    hours = parse_backtrack_hours(backtrack)
    jinnang_2_results: list[dict] = []
    jinnang_3_results: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_2 = pool.submit(run_jinnang_2, hours)
        future_3 = pool.submit(run_jinnang_3, hours)

        try:
            jinnang_2_results = future_2.result(timeout=300)
        except concurrent.futures.TimeoutError:
            logging.warning("[P3] jinnang_2 timed out after 300s")
            jinnang_2_results = []
        except Exception as exc:
            logging.warning("[P3] jinnang_2 failed: %s", exc)
            jinnang_2_results = []

        try:
            jinnang_3_results = future_3.result(timeout=300)
        except concurrent.futures.TimeoutError:
            logging.warning("[P3] jinnang_3 timed out after 300s")
            jinnang_3_results = []
        except Exception as exc:
            logging.warning("[P3] jinnang_3 failed: %s", exc)
            jinnang_3_results = []

    stats["jinnang_2_count"] = len(jinnang_2_results)
    stats["jinnang_3_count"] = len(jinnang_3_results)
    elapsed = time.time() - start_time
    stats["duration_seconds"] = round(elapsed, 1)

    raw_results = {
        "jinnang_1": [],
        "jinnang_2": jinnang_2_results,
        "jinnang_3": jinnang_3_results,
    }
    total = stats["jinnang_2_count"] + stats["jinnang_3_count"]
    if total == 0:
        stats["status"] = "empty"
        stats["raw_results"] = raw_results
        return "", stats

    formatted, structured_items = render_p3_output(raw_results)
    stats["status"] = "success"
    stats["content_length"] = len(formatted)
    stats["structured_items"] = len(structured_items)
    stats["raw_results"] = raw_results

    logging.info(
        "[P3 parallel] jinnang_2=%s, jinnang_3=%s, total=%s, duration=%.1fs",
        stats["jinnang_2_count"],
        stats["jinnang_3_count"],
        total,
        elapsed,
    )
    return formatted, stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    print("Testing P3 parallel search...")
    text, stats = collect_p3_parallel_only(backtrack="24h")
    print(f"\n===== P3 output ({len(text)} chars) =====\n")
    print(text[:2000] if text else "(empty)")
    print(f"\nStats: {json.dumps(stats, ensure_ascii=False, indent=2)}")
