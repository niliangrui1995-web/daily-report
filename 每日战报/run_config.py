"""
运行配置计算 — 自适应回溯窗口。

从 p6_mapper.py 拆分，原属"上帝模块"的一部分。

【⚠️核心维护规则 / Agent 指令】
1. get_run_config() 的回溯窗口计算依赖统一交易日历与“周内隔一个交易日更新”规则，
   修改时必须确保与
   scheduled_launcher.py 的交易日判断完全一致。
"""

import logging
import math
import os
from datetime import date, datetime

from trading_calendar import (
    get_prev_alternate_update_trading_day,
    get_prev_trading_day,
    get_trading_day_index_in_week,
    get_week_trading_days,
    is_alternate_update_trading_day,
    is_trading_day as _is_td,
)

A_SHARE_MARKET_OPEN_HOUR = 9


def _get_prev_trading_day(today: date, steps: int = 1) -> date:
    return get_prev_trading_day(today, steps=steps)


def get_schedule_decision(today: date) -> dict:
    if not _is_td(today):
        return {
            "is_trading_day": False,
            "should_run": False,
            "reason": "today is not a trading day",
            "trading_day_index_in_week": 0,
            "week_trading_days": [],
            "window_anchor_day": None,
        }

    trading_day_index = get_trading_day_index_in_week(today)
    week_trading_days = get_week_trading_days(today)
    should_run = is_alternate_update_trading_day(today)
    anchor_day = get_prev_alternate_update_trading_day(today)
    reason = (
        f"week_trading_day_index={trading_day_index}, "
        f"week_trading_days={[day.isoformat() for day in week_trading_days]}, "
        f"schedule_rule=run_on_odd_trading_days"
    )
    return {
        "is_trading_day": True,
        "should_run": should_run,
        "reason": reason,
        "trading_day_index_in_week": trading_day_index,
        "week_trading_days": week_trading_days,
        "window_anchor_day": anchor_day,
    }


def get_run_config(now: datetime) -> str:
    if os.environ.get('FORCE_72H'):
        logging.info('【强制触发】环境变量强制回溯 72h 全扫')
        return '72h'
        
    if os.environ.get('FORCE_24H'):
        logging.info('【强制触发】环境变量强制回溯 24h 全扫')
        return '24h'
        
    today = now.date()
    schedule = get_schedule_decision(today)
    anchor_day = schedule["window_anchor_day"] or _get_prev_trading_day(today)

    # Why: now 来自 datetime.now()（naive，本地北京时间），anchor_open 也用 naive 保持一致
    # 若 now 带 tzinfo 则 anchor_open 也会带，确保 delta 计算不会抛 TypeError
    anchor_open = datetime.combine(anchor_day, datetime.min.time()).replace(
        hour=A_SHARE_MARKET_OPEN_HOUR,
        tzinfo=now.tzinfo,
    )
    delta_hours = max(math.ceil((now - anchor_open).total_seconds() / 3600), 1)

    logging.info(
        '【自适应回溯】本周交易日序号=%s, 窗口锚点更新日=%s → 回溯 %sh',
        schedule.get("trading_day_index_in_week"),
        anchor_day,
        delta_hours,
    )
    return f'{delta_hours}h'
