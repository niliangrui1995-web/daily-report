"""
cls_source.py — 财联社快讯探针
通过公开 JSON API 拉取最新电报快讯。
"""
import logging
import time
import random
import requests

from config import FAKE_HEADERS
from db import insert_raw_news, hash_source_id

logger = logging.getLogger(__name__)

# 财联社电报 API
CLS_API_URL = "https://m.cls.cn/nodeapi/roll/telegraph"

# 备用 API 列表（如果主接口挂了依次尝试，包含免签名的镜像站）
CLS_BACKUP_URLS = [
    "https://m.cls.cn/nodeapi/telegraphList",
    "https://www.cls.cn/nodeapi/roll/telegraph",
    "https://rsshub.rssforever.com/cls/telegraph?format=json", # 镜像源，对海外代理 IP 更宽容
    "https://rsshub.app/cls/telegraph?format=json",
]


def _try_fetch(url: str, params: dict) -> dict | None:
    """尝试单个 API 端点"""
    try:
        # 使用随机 User-Agent，避免 403 (财联社会封特定爬虫UA)
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://m.cls.cn",
            "Referer": "https://m.cls.cn/telegraph"
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"[CLS] {url} 返回非200: HTTP {resp.status_code} - {resp.text[:50]}")
    except Exception as exc:
        logger.warning(f"[CLS] {url} 请求异常: {exc}")
    return None


def fetch_cls_news() -> int:
    """
    拉取财联社最新快讯并入库。
    返回本次新增条目数。
    """
    new_count = 0
    try:
        # 随机延迟，模拟真人行为
        time.sleep(random.uniform(2.0, 5.0))

        params = {
            "app": "CailianpressWeb",
            "os": "web",
            "sv": "8.4.6",
            "page": "1",
            "rn": "30",
        }

        # 依次尝试所有端点
        data = None
        for url in [CLS_API_URL] + CLS_BACKUP_URLS:
            data = _try_fetch(url, params)
            if data:
                break

        if not data:
            logger.warning("[CLS] 所有 API 端点均不可用")
            return 0

        # 兼容多种返回结构
        roll_data = (
            data.get("data", {}).get("roll_data", [])
            or data.get("data", {}).get("roll_list", [])
            or data.get("data", [])
        )

        if not roll_data:
            logger.warning("[CLS] 未获取到快讯数据")
            return 0

        for item in roll_data:
            # 提取核心字段
            telegraph_id = str(item.get("id", ""))
            title = item.get("title", "") or item.get("brief", "") or item.get("content", "")
            content = item.get("content", "") or item.get("brief", "") or title
            # 发布时间（Unix 时间戳）
            ctime = item.get("ctime", "") or item.get("time", "")
            published_at = ""
            if ctime:
                try:
                    published_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ctime)))
                except (ValueError, OSError):
                    published_at = str(ctime)

            # 构造原文链接
            url = f"https://www.cls.cn/detail/{telegraph_id}" if telegraph_id else ""

            # 物理去重
            source_id = hash_source_id(telegraph_id or title)

            if not title:
                continue

            if insert_raw_news(
                source="cls",
                source_id=source_id,
                url=url,
                raw_title=title[:200],
                raw_content=content[:500],
                published_at=published_at,
            ):
                new_count += 1

        logger.info(f"[CLS] 拉取 {len(roll_data)} 条，新增 {new_count} 条")

    except requests.RequestException as exc:
        logger.error(f"[CLS] 网络异常: {exc}")
    except Exception as exc:
        logger.error(f"[CLS] 解析异常: {exc}")

    return new_count
