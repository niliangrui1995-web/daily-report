"""
twitter_source.py — X (Twitter) 探针
策略：优先使用 RSSHub 公共实例（更稳定），降级到 Nitter（可能不可用）。
"""
import logging
import time
import random
from datetime import datetime
from typing import Optional

import feedparser

from config import TWITTER_WATCHLIST, FAKE_HEADERS
from db import insert_raw_news, hash_source_id

logger = logging.getLogger(__name__)

# RSSHub 公共实例（比 Nitter 稳定得多）
RSSHUB_INSTANCES: list[str] = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub-instance.zeabur.app",
]

# Nitter 实例（当前全球大面积宕机，作为最后备用）
NITTER_INSTANCES: list[str] = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

_current_rsshub_idx = 0


def _parse_published(entry: dict) -> Optional[str]:
    """从 RSS entry 中提取发布时间"""
    tp = entry.get("published_parsed")
    if tp:
        try:
            return datetime(*tp[:6]).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return entry.get("published", "")


def _try_rsshub(username: str) -> list:
    """通过 RSSHub 拉取推文"""
    global _current_rsshub_idx
    for attempt in range(len(RSSHUB_INSTANCES)):
        idx = (_current_rsshub_idx + attempt) % len(RSSHUB_INSTANCES)
        base = RSSHUB_INSTANCES[idx]
        rss_url = f"{base}/twitter/user/{username}"
        try:
            feed = feedparser.parse(rss_url, agent=FAKE_HEADERS["User-Agent"])
            if feed.entries:
                _current_rsshub_idx = idx  # 记住可用的实例
                return feed.entries
        except Exception:
            continue
    return []


def _try_nitter(username: str) -> list:
    """通过 Nitter 拉取推文（备用）"""
    for base in NITTER_INSTANCES:
        rss_url = f"{base}/{username}/rss"
        try:
            feed = feedparser.parse(rss_url, agent=FAKE_HEADERS["User-Agent"])
            if feed.entries:
                return feed.entries
        except Exception:
            continue
    return []


def fetch_twitter_user(display_name: str, username: str) -> int:
    """
    拉取单个 Twitter 大V 的最新推文并入库。
    优先 RSSHub，降级 Nitter。
    返回新增条目数。
    """
    new_count = 0

    try:
        time.sleep(random.uniform(1.0, 4.0))

        # 优先 RSSHub
        entries = _try_rsshub(username)

        # 降级 Nitter
        if not entries:
            entries = _try_nitter(username)

        if not entries:
            # 静默跳过，避免日志刷屏（这些服务经常不可用）
            return 0

        import re
        for entry in entries[:20]:
            title = entry.get("title", "").strip()
            if not title:
                continue

            content = entry.get("summary", entry.get("description", ""))
            if content:
                content = re.sub(r"<[^>]+>", "", content)[:500]
            else:
                content = title

            link = entry.get("link", "")

            published_at = _parse_published(entry)
            source_id = hash_source_id(link or f"{username}_{title[:50]}")

            if insert_raw_news(
                source=f"twitter_{username}",
                source_id=source_id,
                url=link,
                raw_title=f"@{username}: {title[:180]}",
                raw_content=content,
                published_at=published_at or "",
            ):
                new_count += 1

        if new_count > 0:
            logger.info(f"[Twitter:@{username}] 新增 {new_count} 条推文")

    except Exception as exc:
        logger.error(f"[Twitter:@{username}] 异常: {exc}")

    return new_count


def fetch_all_twitter() -> int:
    """遍历所有监控的 Twitter 大V 并入库"""
    total_new = 0
    for display_name, username in TWITTER_WATCHLIST:
        total_new += fetch_twitter_user(display_name, username)
    return total_new
