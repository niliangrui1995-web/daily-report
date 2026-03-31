"""
rss_source.py — 通用 RSS 探针
统一处理 Bloomberg / Reuters / DigiTimes / TrendForce / SemiAnalysis / SeekingAlpha 等 RSS Feed。
核心策略：只抓标题+摘要，不死磕正文，规避付费墙和反爬。
"""
import logging
import time
import random
from datetime import datetime
from typing import Optional

import feedparser

from config import RSS_FEEDS, FAKE_HEADERS
from db import insert_raw_news, hash_source_id

logger = logging.getLogger(__name__)


def _parse_published(entry: dict) -> Optional[str]:
    """尝试从 RSS entry 中提取发布时间"""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                return datetime(*tp[:6]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
    # 回退到字符串
    return entry.get("published", entry.get("updated", ""))


def fetch_rss_feed(feed_name: str, feed_url: str, source_tag: str) -> int:
    """
    拉取单个 RSS Feed 并入库。
    返回新增条目数。
    """
    new_count = 0
    try:
        time.sleep(random.uniform(1.0, 3.0))

        # 对 SEC EDGAR 做日期替换处理
        if "{today}" in feed_url:
            feed_url = feed_url.replace("{today}", datetime.now().strftime("%Y-%m-%d"))

        # feedparser 支持传入 agent 头
        feed = feedparser.parse(feed_url, agent=FAKE_HEADERS["User-Agent"])

        if feed.bozo and not feed.entries:
            logger.warning(f"[RSS:{feed_name}] Feed 格式异常或为空")
            return 0

        for entry in feed.entries[:30]:  # 最多处理最近 30 条，防止首次启动灌爆
            title = entry.get("title", "").strip()
            if not title:
                continue

            # 摘要：取 summary 的前 300 字（标题+摘要足以触发 AI 推理）
            summary = entry.get("summary", entry.get("description", ""))
            if summary:
                # 去除 HTML 标签（简易清理）
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:300]
            else:
                summary = ""

            link = entry.get("link", "")
            published_at = _parse_published(entry)

            # 物理去重：用链接 URL 做哈希（RSS 的 link 通常是唯一的）
            source_id = hash_source_id(link or title)

            if insert_raw_news(
                source=source_tag,
                source_id=source_id,
                url=link,
                raw_title=title[:200],
                raw_content=summary,
                published_at=published_at or "",
            ):
                new_count += 1

        logger.info(f"[RSS:{feed_name}] 拉取 {len(feed.entries)} 条，新增 {new_count} 条")

    except Exception as exc:
        logger.error(f"[RSS:{feed_name}] 异常: {exc}")

    return new_count


def fetch_all_rss() -> int:
    """遍历所有配置的 RSS 源并入库"""
    total_new = 0
    for feed_name, feed_url, source_tag in RSS_FEEDS:
        total_new += fetch_rss_feed(feed_name, feed_url, source_tag)
    return total_new
