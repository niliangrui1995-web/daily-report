"""
db.py — SQLite 数据库层
提供线程安全的连接管理及 CRUD 操作，含物理去重（UNIQUE 约束）。
"""
import sqlite3
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)

# ==============================================================================
# 建表 DDL
# ==============================================================================
_DDL = """
CREATE TABLE IF NOT EXISTS news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT,
    raw_title       TEXT,
    raw_content     TEXT,
    cn_title        TEXT,
    cn_summary      TEXT,
    track           TEXT,
    companies       TEXT,
    a_share_mapping TEXT,
    sentiment       TEXT,
    urgency         INTEGER DEFAULT 1,
    ai_processed    INTEGER DEFAULT 0,
    published_at    TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_created  ON news(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_urgency  ON news(urgency DESC);
CREATE INDEX IF NOT EXISTS idx_track    ON news(track);
CREATE INDEX IF NOT EXISTS idx_source   ON news(source);
CREATE INDEX IF NOT EXISTS idx_ai_proc  ON news(ai_processed);
"""


def _get_conn() -> sqlite3.Connection:
    """获取线程安全的 SQLite 连接（每次调用新建，由调用方关闭）"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 写前日志模式，提升并发读写性能
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """初始化数据库表结构"""
    conn = _get_conn()
    try:
        conn.executescript(_DDL)
        conn.commit()
        logger.info(f"[DB] 数据库初始化完成: {DB_PATH}")
    finally:
        conn.close()


# ==============================================================================
# 写入操作
# ==============================================================================

def hash_source_id(raw_id: str) -> str:
    """对原始 ID/URL 做 SHA256 哈希，作为去重指纹"""
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]


def insert_raw_news(
    source: str,
    source_id: str,
    url: str,
    raw_title: str,
    raw_content: str = "",
    published_at: Optional[str] = None,
) -> bool:
    """
    插入一条原始新闻。利用 UNIQUE(source, source_id) 约束实现物理去重。
    返回 True 表示插入成功（新数据），False 表示已存在（被去重跳过）。
    """
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO news (source, source_id, url, raw_title, raw_content, published_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source, source_id, url, raw_title, raw_content, published_at),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # UNIQUE 约束冲突 → 重复数据，直接跳过
        return False
    except Exception as exc:
        logger.error(f"[DB] 插入异常: {exc}")
        return False
    finally:
        conn.close()


def update_ai_result(
    news_id: int,
    cn_title: str,
    cn_summary: str,
    track: str,
    companies: list[str],
    a_share_mapping: list[str],
    sentiment: str,
    urgency: int,
) -> None:
    """AI 洗稿完成后，更新标签字段并标记 ai_processed=1"""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE news SET
                cn_title = ?, cn_summary = ?, track = ?,
                companies = ?, a_share_mapping = ?,
                sentiment = ?, urgency = ?, ai_processed = 1
               WHERE id = ?""",
            (
                cn_title, cn_summary, track,
                json.dumps(companies, ensure_ascii=False),
                json.dumps(a_share_mapping, ensure_ascii=False),
                sentiment, urgency, news_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_as_duplicate(news_id: int) -> None:
    """AI 语义去重后，标记为已处理但不展示核心字段"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE news SET ai_processed = -1 WHERE id = ?",
            (news_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# 查询操作
# ==============================================================================

def get_unprocessed(limit: int = 5) -> list[dict]:
    """获取待 AI 处理的条目（ai_processed=0），按创建时间升序"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM news WHERE ai_processed = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_titles(hours: int = 2, limit: int = 5) -> list[str]:
    """获取最近 N 小时内已处理新闻的标题，用于语义去重"""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT cn_title FROM news
               WHERE ai_processed = 1 AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [r["cn_title"] for r in rows if r["cn_title"]]
    finally:
        conn.close()


def query_news(
    hours: int = 24,
    tracks: Optional[list[str]] = None,
    min_urgency: int = 1,
    sentiment: Optional[str] = None,
    company: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """
    前台看板查询入口。支持按时间、赛道、紧急度、情感、公司过滤。
    只返回 ai_processed=1 的已标注数据。
    """
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conditions = ["ai_processed = 1", "created_at >= ?", "urgency >= ?"]
    params: list = [since, min_urgency]

    if tracks:
        placeholders = ",".join("?" for _ in tracks)
        conditions.append(f"track IN ({placeholders})")
        params.extend(tracks)

    if sentiment:
        conditions.append("sentiment = ?")
        params.append(sentiment)

    if company:
        # 模糊匹配公司名
        conditions.append("(companies LIKE ? OR a_share_mapping LIKE ?)")
        params.extend([f"%{company}%", f"%{company}%"])

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM news WHERE {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    conn = _get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats(hours: int = 24) -> dict:
    """获取统计摘要：今日总数、5星爆料、利好/利空计数"""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM news WHERE ai_processed = 1 AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        urgent = conn.execute(
            "SELECT COUNT(*) FROM news WHERE ai_processed = 1 AND urgency = 5 AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        bullish = conn.execute(
            "SELECT COUNT(*) FROM news WHERE ai_processed = 1 AND sentiment = '利好' AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        bearish = conn.execute(
            "SELECT COUNT(*) FROM news WHERE ai_processed = 1 AND sentiment = '利空' AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        return {"total": total, "urgent_5star": urgent, "bullish": bullish, "bearish": bearish}
    finally:
        conn.close()
