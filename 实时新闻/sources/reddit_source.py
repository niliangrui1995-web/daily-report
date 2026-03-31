"""
reddit_source.py — Reddit JSON 后门探针
使用合规的 User-Agent 和 old.reddit.com 域名绕过 403。
"""
import logging
import time
import random
import requests

from config import REDDIT_SUBS, FAKE_UA
from db import insert_raw_news, hash_source_id

logger = logging.getLogger(__name__)

# 使用真实的浏览器 UA 伪装，避免无 OAuth 时被快速 403 封锁
REDDIT_HEADERS = {
    "User-Agent": FAKE_UA,
    "Accept": "application/json",
}


def fetch_reddit_sub(subreddit: str) -> int:
    """
    拉取单个 subreddit 的最新帖子并入库。
    返回新增条目数。
    """
    new_count = 0
    # 用 old.reddit.com 更可靠
    url = f"https://old.reddit.com/r/{subreddit}/new/.json?limit=25"

    try:
        time.sleep(random.uniform(3.0, 8.0))

        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)

        if resp.status_code == 403:
            logger.warning(f"[Reddit:r/{subreddit}] 被封锁(403)，静默跳过")
            return 0
        if resp.status_code == 429:
            logger.warning(f"[Reddit:r/{subreddit}] 限流(429)，下次再试")
            return 0

        resp.raise_for_status()
        data = resp.json()

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            post_data = post.get("data", {})
            post_id = post_data.get("id", "")
            title = post_data.get("title", "").strip()
            if not title:
                continue

            selftext = post_data.get("selftext", "")[:300]
            permalink = post_data.get("permalink", "")
            post_url = f"https://www.reddit.com{permalink}" if permalink else ""

            # 发布时间
            created_utc = post_data.get("created_utc", 0)
            published_at = ""
            if created_utc:
                try:
                    published_at = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.gmtime(int(created_utc))
                    )
                except (ValueError, OSError):
                    pass

            source_id = hash_source_id(post_id or post_url or title)

            if insert_raw_news(
                source=f"reddit_{subreddit}",
                source_id=source_id,
                url=post_url,
                raw_title=title[:200],
                raw_content=selftext,
                published_at=published_at,
            ):
                new_count += 1

        if new_count > 0:
            logger.info(f"[Reddit:r/{subreddit}] 新增 {new_count} 条")

    except requests.RequestException as exc:
        logger.warning(f"[Reddit:r/{subreddit}] 网络异常: {type(exc).__name__}")
    except Exception as exc:
        logger.error(f"[Reddit:r/{subreddit}] 解析异常: {exc}")

    return new_count


def fetch_all_reddit() -> int:
    """遍历所有配置的 subreddit 并入库"""
    total_new = 0
    for sub in REDDIT_SUBS:
        total_new += fetch_reddit_sub(sub)
    return total_new
