"""
运行配置计算 — 自适应回溯窗口。

从 p6_mapper.py 拆分，原属"上帝模块"的一部分。

【⚠️核心维护规则 / Agent 指令】
1. get_run_config() 的回溯窗口计算依赖 _get_prev_trading_day()，
   而后者复用 trading_calendar.py 的统一日历。修改时必须确保与
   scheduled_launcher.py 的交易日判断完全一致。
"""

import logging
import os
from datetime import date, datetime, timedelta
from trading_calendar import is_trading_day as _is_td


def _get_prev_trading_day(today: date) -> date:
    d = today - timedelta(days=1)
    for _ in range(30):
        if _is_td(d):
            return d
        d -= timedelta(days=1)
    return today - timedelta(days=1)


def get_run_config(now: datetime) -> str:
    if os.environ.get('FORCE_72H'):
        logging.info('【强制触发】环境变量强制回溯 72h 全扫')
        return '72h'
        
    if os.environ.get('FORCE_24H'):
        logging.info('【强制触发】环境变量强制回溯 24h 全扫')
        return '24h'
        
    today = now.date()
    prev_day = _get_prev_trading_day(today)
    
    # Why: now 来自 datetime.now()（naive，本地北京时间），prev_open 也用 naive 保持一致
    # 若 now 带 tzinfo 则 prev_open 也会带，确保 delta 计算不会抛 TypeError
    prev_open = datetime.combine(prev_day, datetime.min.time()).replace(hour=9, tzinfo=now.tzinfo)
    delta_hours = max(int((now - prev_open).total_seconds() / 3600), 24)
    delta_hours = min(delta_hours, 240)
    
    logging.info(f'【自适应回溯】上一交易日={prev_day} → 回溯 {delta_hours}h')
    return f'{delta_hours}h'
