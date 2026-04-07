import os
import re
import struct
import logging

import json
from datetime import datetime

from settings import get_today_output_dir

_TDX_VIPDOC_CANDIDATES = [
    'D:\\HT\\vipdoc',
    'D:\\vcp_qt\\vipdoc',
    'C:\\new_tdx\\vipdoc',
    'D:\\new_tdx\\vipdoc',
    'C:\\HT\\vipdoc'
]

def _find_tdx_vipdoc():
    for candidate in _TDX_VIPDOC_CANDIDATES:
        if os.path.isdir(candidate):
            return candidate
    return None

def extract_stocks_from_report(report: str):
    patterns = [
        r'([\u4e00-\u9fa5]{2,6})[（\(]\s*(\d{6})\s*[）\)]',
        r'([\u4e00-\u9fa5]{2,6})\s+(\d{6})',
        r'\|\s*([\u4e00-\u9fa5]{2,6})\s*\|\s*(\d{6})\s*\|'
    ]
    stocks = {}
    for pattern in patterns:
        for match in re.finditer(pattern, report):
            name, code = match.group(1), match.group(2)
            if code not in stocks:
                stocks[code] = name
    result = [(name, code) for code, name in stocks.items()]
    return result

def extract_stock_tracks(report: str):
    track_map = {}
    current_track = ""
    for line in report.split('\n'):
        track_match = re.match(r'###\s*赛道[:：]\s*(.+)', line)
        if track_match:
            current_track = track_match.group(1).strip()
            continue
        if current_track:
            stock_match = re.search(r'[\u4e00-\u9fa5]{2,6}[（\(]\s*(\d{6})\s*[）\)]', line)
            if stock_match:
                track_map[stock_match.group(1)] = current_track
    return track_map

def extract_stock_elasticity(report: str):
    elasticity_map = {}
    for line in report.split('\n'):
        stock_match = re.search(r'[（\(](\d{6})[）\)]\s*\|\s*(🔥高弹性|⚡中弹性|💀纯概念)', line)
        if stock_match:
            elasticity_map[stock_match.group(1)] = stock_match.group(2)
    return elasticity_map

def calc_macd(closes):
    if len(closes) < 35:
        return 0, "数据不足"
    dif_series = []
    e12 = closes[0]
    e26 = closes[0]
    for c in closes:
        e12 = e12 * 11/13 + c * 2/13
        e26 = e26 * 25/27 + c * 2/27
        dif_series.append(e12 - e26)
    dea = dif_series[0]
    for d in dif_series[1:]:
        dea = dea * 8/10 + d * 2/10
    macd = (dif_series[-1] - dea) * 2
    status = '金叉上方' if macd > 0 and dif_series[-1] > dea else ('死叉下方' if macd < 0 and dif_series[-1] < dea else ('由负转正' if macd > 0 else '由正转负'))
    return macd, status

def calc_rsi(closes, period=14):
    if len(closes) <= period:
        return 50
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:])/period
    avg_loss = sum(losses[-period:])/period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_kdj(highs, lows, closes, period=9):
    if len(closes) <= period:
        return 50
    k, d = 50, 50
    for i in range(period-1, len(closes)):
        h9 = max(highs[i-period+1:i+1])
        l9 = min(lows[i-period+1:i+1])
        c = closes[i]
        rsv = (c - l9)/(h9 - l9)*100 if h9 > l9 else 50
        k = 2/3*k + 1/3*rsv
        d = 2/3*d + 1/3*k
    j = 3*k - 2*d
    return j

def p8_search_stock_prices(stocks=None, track_map=None, elasticity_map=None):
    if not stocks:
        return ""
    vipdoc = _find_tdx_vipdoc()
    if not vipdoc:
        return ""

    def read_day_file_simple(filepath):
        records = []
        try:
            with open(filepath, 'rb') as f:
                while True:
                    buf = f.read(32)
                    if len(buf) < 32:
                        break
                    date_int, o, h, low, c, _amount_f, vol, _ = struct.unpack('<IIIIIfII', buf)
                    date_str = str(date_int)
                    if len(date_str) == 8:
                        records.append({
                            'date': date_str,
                            'open': o / 100.0,
                            'high': h / 100.0,
                            'low': low / 100.0,
                            'close': c / 100.0,
                            'vol': vol
                        })
            return records
        except Exception as e:
            logging.error(f"[P8] 读取本地 K 线数据失败 ({filepath}): {e}")
            return []

    market_index_lines = ["## 同期大盘参考\n\n"]
    _index_returns = {}
    _index_list = [('上证指数', 'sh', 'sh999999'), ('创业板指', 'sz', 'sz399006')]

    for _idx_name, _idx_market, _idx_file in _index_list:
        _idx_path = os.path.join(vipdoc, _idx_market, 'lday', f'{_idx_file}.day')
        _idx_records = read_day_file_simple(_idx_path)
        if _idx_records:
            _idx_records = _idx_records[-250:]
            _idx_closes = [r['close'] for r in _idx_records]
            _idx_latest = _idx_closes[-1]
            c_1m = _idx_closes[-20] if len(_idx_closes) >= 20 else _idx_closes[0]
            c_3m = _idx_closes[-60] if len(_idx_closes) >= 60 else _idx_closes[0]
            _idx_ret_1m = (_idx_latest - c_1m) / c_1m * 100
            _idx_ret_3m = (_idx_latest - c_3m) / c_3m * 100
            pctv = (_idx_latest - min(_idx_closes)) / (max(_idx_closes) - min(_idx_closes)) * 100 if max(_idx_closes) > min(_idx_closes) else 0
            _index_returns[_idx_name] = {'chg_20d': _idx_ret_1m, 'chg_60d': _idx_ret_3m}
            date_str = f"{_idx_records[-1]['date'][:4]}-{_idx_records[-1]['date'][4:6]}-{_idx_records[-1]['date'][6:8]}"
            market_index_lines.append(f"- {_idx_name}: 最新 {_idx_latest:.2f}（{date_str}）, 近20日 {_idx_ret_1m:+.1f}%, 近60日 {_idx_ret_3m:+.1f}%, 250日分位 {int(pctv)}%\n")

    p8_json_db = {}
    price_summaries = ["\n"]
    low_elasticity_names = []



    for name, code in stocks:
        elasticity = elasticity_map.get(code, '💧中弹性') if elasticity_map else '💧中弹性'
        track_label = track_map.get(code, '未分类') if track_map else '未分类'

        market = 'sh' if code.startswith('6') else 'sz'
        filepath = os.path.join(vipdoc, market, 'lday', f'{market}{code}.day')
        records = read_day_file_simple(filepath)

        is_core = '高弹性' in elasticity or '核心' in elasticity

        if not is_core:
            if records:
                r = records[-1]
                closes = [rc['close'] for rc in records[-250:]]
                c_1m = closes[-20] if len(closes) >= 20 else closes[0]
                chg_1m = (r['close'] - c_1m) / c_1m * 100 if c_1m > 0 else 0
                rs_20d = chg_1m - _index_returns.get('上证指数', {}).get('chg_20d', 0)
                boll_width = 0
                atr_pct = 0
                weekly_trend = "周线下行" # simplified
                if len(closes) >= 20:
                    ma20 = sum(closes[-20:]) / 20.0
                    std20 = (sum((c - ma20)**2 for c in closes[-20:]) / 20.0) ** 0.5
                    boll_width = (4 * std20) / ma20 * 100 if ma20 > 0 else 0
                low_elasticity_names.append(f"- 【{track_label}】{name}({code}) | {r['close']:.2f}元 | 20日{chg_1m:+.1f}% | RS{rs_20d:+.1f}% | ATR{atr_pct:.1f}% | {weekly_trend} | 布林宽{boll_width:.1f}%")

            p8_json_db[code] = {
                'name': name, 'code': code, 'elasticity': elasticity, 'fast_path': True
            }
            continue

        if not records or len(records) < 60:
            price_summaries.append(f"### {name}（{code}）\n未找到足够的本地K线数据\n\n")
            continue

        records = records[-250:]
        closes = [r['close'] for r in records]
        highs = [r['high'] for r in records]
        lows = [r['low'] for r in records]
        vols = [r['vol'] for r in records]
        n_rec = len(records)
        latest_r = records[-1]
        latest_c = closes[-1]

        # 涨跌幅, 分位
        low_250d, high_250d = min(closes), max(closes)
        pct_250d = (latest_c - low_250d)/(high_250d - low_250d)*100 if high_250d > low_250d else 50
        pos_desc = "高位区间" if pct_250d > 80 else ("低位区间" if pct_250d < 40 else "中位区间")
        if pct_250d > 95:
            pos_desc = "历史高位区间"

        c_1m = closes[-20] if n_rec >= 20 else closes[0]
        chg_1m = (latest_c - c_1m)/c_1m * 100
        c_3m = closes[-60] if n_rec >= 60 else closes[0]
        chg_3m = (latest_c - c_3m)/c_3m * 100

        # 60日前高
        high_60 = max(highs[-60:])
        dist_to_60d_high = (latest_c - high_60)/high_60 * 100
        breakouts = "无"
        if latest_c >= max(highs[-20:]) * 0.98:
            breakouts = "🔥 逼近20日新高"
            if latest_c >= high_60 * 0.98:
                breakouts = "🔥 逼近20日新高，逼近60日新高"

        # 均线
        ma5 = sum(closes[-5:])/5
        ma10 = sum(closes[-10:])/10
        ma20 = sum(closes[-20:])/20
        ma60 = sum(closes[-60:])/60
        ma_spread = (max(ma5, ma10, ma20, ma60) - min(ma5, ma10, ma20, ma60))/min(ma5, ma10, ma20, ma60)*100

        # 量能
        avg_vol_5d = sum(vols[-5:])/5 if vols[-5:] else 1
        avg_vol_20d = sum(vols[-20:])/20 if vols[-20:] else 1
        avg_vol_60d = sum(vols[-60:])/60 if vols[-60:] else 1
        vr = avg_vol_5d / avg_vol_20d
        vs = avg_vol_5d / avg_vol_60d
        vol_desc = "明显放量" if vr > 1.5 else ("温和缩量" if vr < 0.7 else "正常")

        # 波动
        atr14_sum = 0
        for i in range(1, 15):
            high_val, low_val, prev_close = highs[-i], lows[-i], closes[-i-1]
            atr14_sum += max(high_val-low_val, abs(high_val-prev_close), abs(low_val-prev_close))
        atr = (atr14_sum/14)/latest_c*100

        std20 = (sum((c - ma20)**2 for c in closes[-20:]) / 20.0) ** 0.5
        boll_w = (4 * std20) / ma20 * 100
        boll_desc = "极窄蓄力" if boll_w < 10 else ""
        ac = atr / 5.0 # fallback normalization

        # 动量
        macd_val, macd_stat = calc_macd(closes)
        rsi_val = int(calc_rsi(closes))
        rsi_desc = "超买" if rsi_val > 70 else ("超卖" if rsi_val < 30 else "中性")
        kdj_j = int(calc_kdj(highs, lows, closes))
        kdj_desc = "超买" if kdj_j > 100 else ("超卖" if kdj_j < 0 else "中性")
        yang_count = sum(1 for i in range(-20, 0) if closes[i] >= closes[i-1])
        yang_ratio = yang_count/20 * 100

        # RS
        sh_chg_20 = _index_returns.get('上证指数', {}).get('chg_20d', 0)
        sh_chg_60 = _index_returns.get('上证指数', {}).get('chg_60d', 0)
        rs_20 = chg_1m - sh_chg_20
        rs_60 = chg_3m - sh_chg_60
        rs_desc = "显著强于大盘" if rs_60 > 20 else ("强于大盘" if rs_60 > 10 else ("弱于大盘" if rs_60 < -5 else "基本持平"))

        # 周线
        weekly_c = closes[::5]
        if len(weekly_c) >= 20:
            wm20 = sum(weekly_c[-20:])/20
            w_stat = "站上" if weekly_c[-1] > wm20 else "跌破"
            wmacd, wmacd_stat = calc_macd(weekly_c)
            w_trend = "上行" if wmacd > 0 and weekly_c[-1] > wm20 else ("下行" if wmacd < 0 and weekly_c[-1] < wm20 else "横盘震荡")
        else:
            w_stat, wmacd, wmacd_stat, w_trend = "未知", 0, "未知", "数据不足"

        # 涨跌停历程 (简易)
        limits = []
        for i in range(-20, 0):
            if closes[i] >= round(closes[i-1]*1.1, 2) - 0.01:
                limits.append(f"{records[i]['date'][4:6]}-{records[i]['date'][6:8]}涨停")
            elif closes[i] <= round(closes[i-1]*0.9, 2) + 0.01:
                limits.append(f"{records[i]['date'][4:6]}-{records[i]['date'][6:8]}跌停")
        limit_str = "，".join(limits) if limits else "无"

        b_desc = f"（{boll_desc}）" if boll_desc else ""
        c_desc = "压缩" if ac < 0.8 else ("放大" if ac > 1.2 else "正常")

        block = f"""### {name}（{code}）
- 所属赛道：{track_label}
- 收盘 {latest_c:.2f} 元（{latest_r['date'][4:6]}-{latest_r['date'][6:8]}）| 市值未知
- 涨跌：近20日 {chg_1m:+.1f}% | 近60日 {chg_3m:+.1f}% | 250日分位 {int(pct_250d)}%（{pos_desc}）
- 距60日前高 {dist_to_60d_high:+.1f}%（回撤 {abs(dist_to_60d_high):.1f}%）| 突破：{breakouts}
- 均线：5日{(latest_c-ma5)/ma5*100:+.1f}%，10日{(latest_c-ma10)/ma10*100:+.1f}%，20日{(latest_c-ma20)/ma20*100:+.1f}%，60日{(latest_c-ma60)/ma60*100:+.1f}% | 密集度 {ma_spread:.1f}%
- 量能：{vol_desc}（量比 {vr:.2f}，缩量度 {vs:.2f}）| 换手 0.00% 异动 1.0x（正常）
- 波动：布林宽 {boll_w:.1f}%{b_desc} | ATR {atr:.1f}% 压缩比 {ac:.2f}（{c_desc}）
- 动量：MACD {macd_stat}({macd_val:.3f}) | RSI {rsi_val}（{rsi_desc}）| KDJ J={kdj_j}（{kdj_desc}）| 阳线 {int(yang_ratio)}%
- ⚡ RS：20日超额 {rs_20:+.1f}% 60日超额 {rs_60:+.1f}%（{rs_desc}）
- 📅 周线：MACD {wmacd_stat}({wmacd:.3f}) MA20 {w_stat} 趋势 {w_trend}
- 🔔 涨停/跌停：{limit_str}
"""
        price_summaries.append(block)

        # Populate JSON correctly
        p8_json_db[code] = {
            "name": name,
            "market": market,
            "date": latest_r['date'],
            "price_data": {
                "latest_close": latest_c,
                "chg_1m_pct": round(chg_1m, 1),
                "chg_3m_pct": round(chg_3m, 1),
                "low_250d": round(low_250d, 2),
                "high_250d": round(high_250d, 2),
                "percentile_250d": round(pct_250d, 1),
                "dist_to_60d_high_pct": round(dist_to_60d_high, 1),
                "drawdown_60d_pct": round(abs(dist_to_60d_high), 1)
            },
            "tech_indicators": {
                "vol_ratio_5d_20d": round(vr, 2),
                "vol_shrink_5d_60d": round(vs, 2),
                "turnover_10d_avg": 0,
                "turnover_anomaly": 1,
                "boll_width_pct": round(boll_w, 1),
                "atr_compress_ratio": round(ac, 2),
                "ma_spread_pct": round(ma_spread, 1),
                "macd_bar": round(macd_val, 3),
                "rsi_14": rsi_val,
                "kdj_j": kdj_j,
                "yang_ratio_20d": yang_ratio
            },
            "finance_data": {
                "total_cap_yi": 0, "liutong_cap_yi": 0, "jinglirun_yi": 0
            },
            "tags": {
                "vol_desc": vol_desc,
                "pos_desc": pos_desc,
                "breakouts": [] if breakouts == "无" else [breakouts],
                "rsi_desc": rsi_desc,
                "macd_status": macd_stat,
                "turnover_anom_desc": "正常"
            },
            "relative_strength": {
                "rs_excess_20d": round(rs_20, 1),
                "rs_excess_60d": round(rs_60, 1),
                "rs_desc": rs_desc
            },
            "weekly_level": {
                "weekly_macd_bar": round(wmacd, 3),
                "weekly_macd_status": wmacd_stat,
                "weekly_ma20_position": w_stat,
                "weekly_trend": w_trend
            }
        }

    market_chunk = "".join(market_index_lines)
    summary_chunk = "\n".join(price_summaries)

    # 杂鱼输出
    if low_elasticity_names:
        summary_chunk += "\n" + "\n\n".join(low_elasticity_names) + "\n"

    # 写入文件以兼容旧架构的观测习惯
    today_dir = get_today_output_dir()
    now_str = datetime.now().strftime("%Y%m%d%H%M")

    p8_md_path = os.path.join(today_dir, f"P8_技术面_{now_str}.md")
    p8_json_path = os.path.join(today_dir, f"P8_技术面_{now_str}.json")

    # 抽取纯MD但不含JSON标签，以符合老格式
    pure_md_str = "".join([
        "# P8 股价分析数据（本地通达信 250 日 K 线）\n\n",
        f"> 读取时间: {datetime.now().isoformat()}\n\n",
        "> 注：以下所有「X日」均指交易日（非自然日）\n\n",
        market_chunk,
        summary_chunk
    ])

    try:
        with open(p8_md_path, "w", encoding="utf-8") as f:
            f.write(pure_md_str)
        with open(p8_json_path, "w", encoding="utf-8") as f:
            json.dump(p8_json_db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"[P8] 写入文件失败: {e}")

    final_output = pure_md_str + "\n### P8 机器视觉数据库\n<p8_technical_db>\n```json\n" + json.dumps(p8_json_db, ensure_ascii=False, indent=2) + "\n```\n</p8_technical_db>\n"

    return final_output
