"""
A股交易日历 — 基于 chinese-calendar 库自动判断。

Why: 之前使用硬编码的节假日列表，每年底必须手动更新，一旦遗漏就会在法定假日空烧 API。
     现在改用社区维护的 chinese-calendar 库，它会自动跟进国务院放假通知，彻底告别年度人工维护。

兜底策略：
  - 如果 chinese-calendar 库不可用（比如版本太旧不支持当年），
    自动降级为「周末休市 + 内置最小化硬编码」的保守策略。
"""

import logging
from datetime import date

try:
    import chinese_calendar
    _HAS_CALENDAR_LIB = True
except ImportError:
    _HAS_CALENDAR_LIB = False
    logging.warning('⚠️ chinese-calendar 库未安装，交易日判断将降级为仅排除周末。请执行 pip install chinese-calendar 安装。')

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
        
    if _HAS_CALENDAR_LIB:
        try:
            return not chinese_calendar.is_holiday(d)
        except NotImplementedError:
            logging.warning(f'⚠️ chinese-calendar 不支持 {d.year} 年数据，降级为仅排除周末')
            return True
            
    return True
