"""
ai_processor.py — AI 洗稿中枢
通过本地 Antigravity 反代（127.0.0.1:8045）调用 OpenAI 兼容接口。
"""
import json
import logging
import time
import random
from typing import Optional

import requests

from config import GEMINI_API_KEY_POOL, GEMINI_MODEL, TRACK_ENTITY_MAP
from db import get_unprocessed, get_recent_titles, update_ai_result, mark_as_duplicate

logger = logging.getLogger(__name__)

# Cloudflare Worker 反代（api.niliangrui.cloud → Google Gemini API）
CF_WORKER_BASE = "https://api.niliangrui.cloud/v1beta/models"
API_MODEL = GEMINI_MODEL  # gemini-2.5-flash

# 同时支持 Kimi 月之暗面 API 作为备用
KIMI_API_URL = "https://api.moonshot.cn/v1/chat/completions"
KIMI_API_KEY = "REMOVED_CREDENTIAL"
KIMI_MODEL = "moonshot-v1-8k"


# ==============================================================================
# Prompt 构建
# ==============================================================================

def _build_track_map_text() -> str:
    lines = []
    for i, (track, entities) in enumerate(TRACK_ENTITY_MAP.items(), 1):
        lines.append(f"{i}. {track} → {', '.join(entities)}")
    return "\n".join(lines)


TRACK_MAP_TEXT = _build_track_map_text()


def _build_prompt(raw_title: str, raw_content: str, recent_titles: list[str]) -> str:
    recent_section = "\n".join(f"  - {t}" for t in recent_titles) if recent_titles else "  (无近期历史)"

    return f"""你是 AI 产业链情报审核员。请处理以下新抓取的新闻原文。

【语义去重】先判断这条新闻是否与以下近期历史记录说的是同一件事：
{recent_section}
- 如果是重复的，返回 {{"status": "duplicate"}}
- 如果是新内容，继续处理：

【赛道-公司映射表】（必须从以下 16 个赛道中匹配，优先按涉及公司归类）：
{TRACK_MAP_TEXT}

【结构化输出】请返回 JSON：
{{
  "status": "new",
  "cn_title": "20字以内中文标题",
  "cn_summary": "50字以内中文摘要",
  "track": "赛道名(从上述16大赛道中精确选择)",
  "companies": ["涉及公司1", "涉及公司2"],
  "a_share_mapping": ["直接受影响的A股简称1", "简称2"],
  "sentiment": "利好/利空/中性",
  "urgency": 1到5的整数
}}

紧急度标准：
⭐ 1: 常规行业动态
⭐⭐ 2: 值得关注的技术进展
⭐⭐⭐ 3: 重要订单/产能/财报变化
⭐⭐⭐⭐ 4: 产业链重大拐点信号
⭐⭐⭐⭐⭐ 5: 巨头换帅/参数泄露/重大并购/突发制裁

【待处理原文】
标题：{raw_title}
内容：{raw_content}

请直接返回 JSON，不要包裹在 markdown 代码块中。"""


# ==============================================================================
# 双通道 AI 调用：CF Worker (Google) → Kimi (月之暗面)
# ==============================================================================

_key_index = 0  # Google API Key 轮换索引


def _call_google(prompt: str) -> Optional[str]:
    """通过 Cloudflare Worker 调用 Google Gemini REST API"""
    global _key_index
    if not GEMINI_API_KEY_POOL:
        return None

    key = GEMINI_API_KEY_POOL[_key_index % len(GEMINI_API_KEY_POOL)]
    _key_index += 1

    url = f"{CF_WORKER_BASE}/{API_MODEL}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
    }

    try:
        resp = requests.post(url, json=payload, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
        elif resp.status_code == 429:
            logger.warning(f"[AI:Google] Key ...{key[-4:]} 限流(429)")
        else:
            logger.warning(f"[AI:Google] HTTP {resp.status_code}: {resp.text[:150]}")
    except Exception as exc:
        logger.warning(f"[AI:Google] {type(exc).__name__}: {exc}")
    return None


def _call_llm(prompt: str, max_retries: int = 3) -> Optional[str]:
    """通过 Cloudflare Worker 调用 Google Gemini"""
    for attempt in range(max_retries):
        result = _call_google(prompt)
        if result:
            return result
            
        if attempt < max_retries - 1:
            wait = 2 ** attempt + random.uniform(0, 1)
            logger.warning(f"[AI] 调用失败，{wait:.1f}s 后重试")
            time.sleep(wait)

    return None




def _parse_ai_response(text: Optional[str]) -> Optional[dict]:
    """解析 AI 返回的 JSON（容错处理）"""
    if not text:
        return None

    # 清理可能存在的 markdown 代码块包裹
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        # strict=False 允许字符串内存在未转义的换行符和控制字符
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        import re
        # 尝试修复最常见的尾随逗号错误
        text_fixed = re.sub(r",\s*([\]}])", r"\1", text)
        try:
            return json.loads(text_fixed, strict=False)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text_fixed, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(), strict=False)
                except json.JSONDecodeError:
                    pass
        logger.error(f"[AI] JSON 解析失败 ({e}): {text_fixed[:200]}")
        return None


# ==============================================================================
# 批处理入口
# ==============================================================================

def process_batch() -> int:
    """批量处理未标注的新闻条目。返回本次成功处理的条目数。"""
    items = get_unprocessed(limit=5)
    if not items:
        return 0

    recent_titles = get_recent_titles(hours=2, limit=5)
    processed = 0

    for item in items:
        news_id = item["id"]
        raw_title = item.get("raw_title", "")
        raw_content = item.get("raw_content", "")

        if not raw_title:
            mark_as_duplicate(news_id)
            continue

        import time
        prompt = self._build_prompt(raw_title, raw_content, recent_titles)
        
        resp_text = ""
        result = None
        for attempt in range(3):  # 最多重试2次 (1 original + 2 retries)
            resp_text = self._call_llm(prompt)
            if not resp_text:
                logger.warning(f"[AI] ID={news_id} 第 {attempt+1} 次调用 LLM 无返回")
                if attempt < 2: # If not the last attempt, wait and retry
                    time.sleep(2)
                continue

            result = self._parse_ai_response(resp_text)
            if result:
                break # Successfully parsed, exit retry loop
            else:
                logger.warning(f"[AI] ID={news_id} 尝试第 {attempt+1} 次解析失败，准备重试...")
                if attempt < 2: # If not the last attempt, wait and retry
                    time.sleep(2)
        else: # This 'else' block executes if the loop completes without a 'break'
            logger.error(f"[AI] ID={news_id} 连续解析失败，放弃处理。原始返回: {resp_text[:200]}")
            mark_as_duplicate(news_id) # 标记为 -1，移出处理队列
            return

        status = result.get("status", "new")

        if status == "duplicate":
            mark_as_duplicate(news_id)
            logger.info(f"[AI] ID={news_id} 语义去重 → 标记为重复")
        else:
            update_ai_result(
                news_id=news_id,
                cn_title=result.get("cn_title", raw_title[:20]),
                cn_summary=result.get("cn_summary", ""),
                track=result.get("track", "未分类"),
                companies=result.get("companies", []),
                a_share_mapping=result.get("a_share_mapping", []),
                sentiment=result.get("sentiment", "中性"),
                urgency=int(result.get("urgency", 1)),
            )
            processed += 1
            title_preview = result.get("cn_title", "")[:15]
            urgency_val = result.get("urgency", 1)
            logger.info(f"[AI] ID={news_id} ✅ {'⭐' * int(urgency_val)} {title_preview}")

        time.sleep(random.uniform(1.0, 3.0))

    return processed
