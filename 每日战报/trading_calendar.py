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
from datetime import date
from importlib import metadata

CALENDAR_PACKAGE_NAME = "chinesecalendar"
CALENDAR_IMPORT_NAME = "chinese_calendar"

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
