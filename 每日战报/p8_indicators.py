"""P8 technical indicators."""

from __future__ import annotations


def calc_macd(closes):
    if len(closes) < 35:
        return 0, "数据不足"
    dif_series = []
    e12 = closes[0]
    e26 = closes[0]
    for c in closes:
        e12 = e12 * 11 / 13 + c * 2 / 13
        e26 = e26 * 25 / 27 + c * 2 / 27
        dif_series.append(e12 - e26)
    dea = dif_series[0]
    for d in dif_series[1:]:
        dea = dea * 8 / 10 + d * 2 / 10
    macd = (dif_series[-1] - dea) * 2
    status = '金叉上方' if macd > 0 and dif_series[-1] > dea else ('死叉下方' if macd < 0 and dif_series[-1] < dea else ('由负转正' if macd > 0 else '由正转负'))
    return macd, status


def calc_rsi(closes, period=14):
    if len(closes) <= period:
        return 50
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_kdj(highs, lows, closes, period=9):
    if len(closes) <= period:
        return 50
    k, d = 50, 50
    for i in range(period - 1, len(closes)):
        h9 = max(highs[i - period + 1:i + 1])
        l9 = min(lows[i - period + 1:i + 1])
        c = closes[i]
        rsv = (c - l9) / (h9 - l9) * 100 if h9 > l9 else 50
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    return 3 * k - 2 * d


def calc_period_change(closes, lookback: int) -> float:
    if not closes:
        return 0.0
    base = closes[-lookback] if len(closes) >= lookback else closes[0]
    return (closes[-1] - base) / base * 100 if base > 0 else 0.0


def calc_boll_width(closes, period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    ma = sum(closes[-period:]) / period
    std = (sum((c - ma) ** 2 for c in closes[-period:]) / period) ** 0.5
    return (4 * std) / ma * 100 if ma > 0 else 0.0


def calc_atr_pct(highs, lows, closes, period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    atr_sum = 0.0
    for i in range(1, period + 1):
        high_val = highs[-i]
        low_val = lows[-i]
        prev_close = closes[-i - 1]
        atr_sum += max(high_val - low_val, abs(high_val - prev_close), abs(low_val - prev_close))
    latest_close = closes[-1]
    return (atr_sum / period) / latest_close * 100 if latest_close > 0 else 0.0


def calc_weekly_summary(closes):
    weekly_c = closes[::5]
    if len(weekly_c) < 20:
        return {
            'w_stat': '未知',
            'wmacd': 0.0,
            'wmacd_stat': '未知',
            'trend': '数据不足',
        }

    wm20 = sum(weekly_c[-20:]) / 20
    w_stat = '站上' if weekly_c[-1] > wm20 else '跌破'
    wmacd, wmacd_stat = calc_macd(weekly_c)
    if wmacd > 0 and weekly_c[-1] > wm20:
        trend = '上行'
    elif wmacd < 0 and weekly_c[-1] < wm20:
        trend = '下行'
    else:
        trend = '横盘震荡'
    return {
        'w_stat': w_stat,
        'wmacd': wmacd,
        'wmacd_stat': wmacd_stat,
        'trend': trend,
    }
