"""
A股交易日历 — 基于 `chinesecalendar` / `chinese_calendar` 自动判断。

命名说明：
  - PyPI 包名：`chinesecalendar`
  - import 名：`chinese_calendar`

Why: 之前使用硬编码的节假日列表，每年底必须手动更新，一旦遗漏就会在法定假日空烧 API。
     现在改用社区维护的日历库自动跟进国务院放假通知，彻底告别年度人工维护。

兜底策略：
  - 如果日历库不可用（比如未安装、版本太旧不支持当年），
    自动降级为「周末休市」的保守策略。
"""

import logging
from datetime import date, timedelta
from importlib import metadata

CALENDAR_PACKAGE_NAME = "chinesecalendar"
CALENDAR_IMPORT_NAME = "chinese_calendar"
MAX_TRADING_DAY_LOOKBACK = 366

try:
    import chinese_calendar
    _HAS_CALENDAR_LIB = True
    try:
        _CALENDAR_PACKAGE_VERSION = metadata.version(CALENDAR_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        _CALENDAR_PACKAGE_VERSION = None
except ImportError:
    _HAS_CALENDAR_LIB = False
    _CALENDAR_PACKAGE_VERSION = None
    logging.warning(
        "⚠️ 日历库未安装，交易日判断将降级为仅排除周末。"
        "请执行 pip install chinesecalendar 安装。"
    )

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
        
    if _HAS_CALENDAR_LIB:
        try:
            return not chinese_calendar.is_holiday(d)
        except NotImplementedError:
            logging.warning(f"⚠️ 日历库不支持 {d.year} 年数据，降级为仅排除周末")
            return True
            
    return True


def get_prev_trading_day(d: date, steps: int = 1) -> date:
    if steps <= 0:
        return d

    current = d
    matched = 0
    for _ in range(MAX_TRADING_DAY_LOOKBACK):
        current -= timedelta(days=1)
        if is_trading_day(current):
            matched += 1
            if matched >= steps:
                return current

    return d - timedelta(days=steps)


def count_trading_days_between(
    start: date,
    end: date,
    *,
    include_start: bool = False,
    include_end: bool = True,
) -> int:
    if start > end:
        return 0

    count = 0
    current = start
    while current <= end:
        include_current = True
        if current == start and not include_start:
            include_current = False
        if current == end and not include_end:
            include_current = False
        if include_current and is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def get_week_trading_days(anchor_day: date) -> list[date]:
    week_start = anchor_day - timedelta(days=anchor_day.weekday())
    days: list[date] = []
    for offset in range(7):
        current = week_start + timedelta(days=offset)
        if is_trading_day(current):
            days.append(current)
    return days


def get_trading_day_index_in_week(d: date) -> int:
    if not is_trading_day(d):
        return 0

    trading_days = get_week_trading_days(d)
    for index, trading_day in enumerate(trading_days, start=1):
        if trading_day == d:
            return index
    return 0


def is_alternate_update_trading_day(d: date) -> bool:
    trading_day_index = get_trading_day_index_in_week(d)
    return trading_day_index > 0 and trading_day_index % 2 == 1


def get_prev_alternate_update_trading_day(d: date) -> date:
    current = d
    for _ in range(MAX_TRADING_DAY_LOOKBACK):
        current = get_prev_trading_day(current)
        if is_alternate_update_trading_day(current):
            return current
    return get_prev_trading_day(d)


def get_calendar_mode() -> str:
    return 'chinese_calendar' if _HAS_CALENDAR_LIB else 'weekend_only_fallback'


def get_calendar_status() -> dict:
    return {
        'mode': get_calendar_mode(),
        'library_available': _HAS_CALENDAR_LIB,
        'package_name': CALENDAR_PACKAGE_NAME,
        'import_name': CALENDAR_IMPORT_NAME,
        'package_version': _CALENDAR_PACKAGE_VERSION,
    }
