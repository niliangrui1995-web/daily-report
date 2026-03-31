"""
spider_worker.py — 后台常驻采集器（主进程入口）
使用 APScheduler 定时调度各数据源探针 + AI 洗稿批处理。
"""
import logging
import signal
import sys
import os

# 将当前目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR

from config import (
    INTERVAL_CLS, INTERVAL_TWITTER, INTERVAL_BLOOMBERG,
    INTERVAL_DIGITIMES, INTERVAL_DEEP_RSS, INTERVAL_REDDIT,
    INTERVAL_AI_BATCH,
)
from db import init_db
from sources.cls_source import fetch_cls_news
from sources.rss_source import fetch_rss_feed, RSS_FEEDS
from sources.reddit_source import fetch_all_reddit
from sources.twitter_source import fetch_all_twitter
from ai_processor import process_batch

# ==============================================================================
# 日志配置
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "spider.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


# ==============================================================================
# 数据源调度任务（按时效层级分组）
# ==============================================================================

def job_cls():
    """⚡ 秒级突发：财联社快讯"""
    try:
        count = fetch_cls_news()
        if count > 0:
            logger.info(f"📰 财联社新增 {count} 条快讯")
    except Exception as exc:
        logger.error(f"[调度] 财联社任务异常: {exc}")


def job_twitter():
    """⚡ 秒级突发：X (Twitter) 大V"""
    try:
        count = fetch_all_twitter()
        if count > 0:
            logger.info(f"🐦 Twitter 大V 新增 {count} 条推文")
    except Exception as exc:
        logger.error(f"[调度] Twitter 任务异常: {exc}")


def job_bloomberg_reuters():
    """🔔 分钟级：Bloomberg / Reuters RSS"""
    try:
        total = 0
        for name, url, tag in RSS_FEEDS:
            if tag in ("bloomberg", "reuters"):
                total += fetch_rss_feed(name, url, tag)
        if total > 0:
            logger.info(f"📡 Bloomberg/Reuters 新增 {total} 条")
    except Exception as exc:
        logger.error(f"[调度] Bloomberg/Reuters 任务异常: {exc}")


def job_digitimes_trendforce():
    """🔔 分钟级：DigiTimes / TrendForce"""
    try:
        total = 0
        for name, url, tag in RSS_FEEDS:
            if tag in ("digitimes", "trendforce"):
                total += fetch_rss_feed(name, url, tag)
        if total > 0:
            logger.info(f"🔧 DigiTimes/TrendForce 新增 {total} 条")
    except Exception as exc:
        logger.error(f"[调度] DigiTimes/TrendForce 任务异常: {exc}")


def job_deep_rss():
    """📊 小时级：SemiAnalysis / Seeking Alpha / SEC"""
    try:
        total = 0
        for name, url, tag in RSS_FEEDS:
            if tag in ("semianalysis", "seekingalpha", "sec_edgar"):
                total += fetch_rss_feed(name, url, tag)
        if total > 0:
            logger.info(f"📊 深度 RSS 新增 {total} 条")
    except Exception as exc:
        logger.error(f"[调度] 深度 RSS 任务异常: {exc}")


def job_reddit():
    """📰 社区补充：Reddit"""
    try:
        count = fetch_all_reddit()
        if count > 0:
            logger.info(f"🤖 Reddit 新增 {count} 条帖子")
    except Exception as exc:
        logger.error(f"[调度] Reddit 任务异常: {exc}")


def job_ai_process():
    """🧠 AI 洗稿批处理"""
    try:
        count = process_batch()
        if count > 0:
            logger.info(f"🧠 AI 洗稿完成 {count} 条")
    except Exception as exc:
        logger.error(f"[调度] AI 洗稿任务异常: {exc}")


# ==============================================================================
# 异常处理
# ==============================================================================

def on_job_error(event):
    """全局任务错误回调"""
    if event.exception:
        logger.error(f"[调度器] 任务 {event.job_id} 执行异常: {event.exception}")


# ==============================================================================
# 主入口
# ==============================================================================

def main():
    logger.info("=" * 60)
    logger.info("🚀 AI 产业链实时情报采集器 启动")
    logger.info("=" * 60)

    # 初始化数据库
    init_db()

    # 创建调度器
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_listener(on_job_error, EVENT_JOB_ERROR)

    # ⚡ 秒级突发
    scheduler.add_job(job_cls, "interval", seconds=INTERVAL_CLS, id="cls", max_instances=1)
    scheduler.add_job(job_twitter, "interval", seconds=INTERVAL_TWITTER, id="twitter", max_instances=1)

    # 🔔 分钟级
    scheduler.add_job(job_bloomberg_reuters, "interval", seconds=INTERVAL_BLOOMBERG, id="bloomberg", max_instances=1)
    scheduler.add_job(job_digitimes_trendforce, "interval", seconds=INTERVAL_DIGITIMES, id="digitimes", max_instances=1)

    # 📊 小时级
    scheduler.add_job(job_deep_rss, "interval", seconds=INTERVAL_DEEP_RSS, id="deep_rss", max_instances=1)
    scheduler.add_job(job_reddit, "interval", seconds=INTERVAL_REDDIT, id="reddit", max_instances=1)

    # 🧠 AI 洗稿
    scheduler.add_job(job_ai_process, "interval", seconds=INTERVAL_AI_BATCH, id="ai_process", max_instances=1)

    # 启动时立即执行一轮全量采集
    logger.info("📡 执行首轮全量采集...")
    job_cls()
    job_twitter()
    job_bloomberg_reuters()
    job_digitimes_trendforce()
    job_deep_rss()
    job_reddit()
    job_ai_process()

    # 优雅退出
    def signal_handler(signum, frame):
        logger.info("🛑 收到退出信号，关闭调度器...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("✅ 调度器启动完毕，所有探针按配置频率轮询中...")
    logger.info(f"   财联社: {INTERVAL_CLS}s | Twitter: {INTERVAL_TWITTER}s | Bloomberg: {INTERVAL_BLOOMBERG}s")
    logger.info(f"   DigiTimes: {INTERVAL_DIGITIMES}s | Deep RSS: {INTERVAL_DEEP_RSS}s | Reddit: {INTERVAL_REDDIT}s")
    logger.info(f"   AI 洗稿: {INTERVAL_AI_BATCH}s")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 采集器已停止")


if __name__ == "__main__":
    main()
