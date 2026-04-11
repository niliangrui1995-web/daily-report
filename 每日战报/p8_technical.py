"""
P8 技术面分析层 — 从本地通达信 K 线数据计算技术指标。

从 daily_report.py 拆分，包含：
- 股票代码提取（从 P6 映射文本中解析 A 股代码）
- 通达信 .day 二进制文件解析（struct.unpack）
- 技术指标计算（MACD/RSI/KDJ/布林/均线/量能/RS/周线）
- 大盘对比基准（上证指数 + 创业板指）

【⚠️核心维护规则 / Agent 指令】
1. `read_day_file_simple()` 的 struct 解包格式 '<IIIIIfII' 严格对应通达信
   .day 文件的 32 字节记录结构，修改前必须确认通达信版本兼容性。
2. `_resolve_tdx_market_and_path()` 的多路径回退探测顺序（bj→sz→sh）
   是兼容北交所历史数据落在 sz 目录的特殊处理，不可简化。
3. `p8_search_stock_prices()` 返回纯 Markdown 字符串（不含 JSON），
   JSON 数据单独落盘至 .json 文件，避免将 JSON 原样发送到 P9 浪费 Token。
"""

import os
import logging

from datetime import datetime

from p8_extractors import (
    extract_stock_elasticity,
    extract_stock_tracks,
    extract_stocks_from_report,
    find_tdx_vipdoc as _find_tdx_vipdoc,
    read_day_file_simple,
    resolve_tdx_market_and_path as _resolve_tdx_market_and_path,
)
from p8_indicators import (
    calc_atr_pct as _calc_atr_pct,
    calc_boll_width as _calc_boll_width,
    calc_kdj,
    calc_macd,
    calc_period_change as _calc_period_change,
    calc_rsi,
    calc_weekly_summary as _calc_weekly_summary,
)
from p8_storage import persist_p8_outputs as _persist_p8_outputs

__all__ = [
    'extract_stocks_from_report',
    'extract_stock_tracks',
    'extract_stock_elasticity',
    'p8_search_stock_prices',
]

def p8_search_stock_prices(stocks=None, track_map=None, elasticity_map=None):
    if not stocks:
        markdown = (
            '# P8 股价分析数据（本地通达信 250 日 K 线）\n\n'
            f'> 读取时间: {datetime.now().isoformat()}\n\n'
            '> 本次未从 P6 映射中提取到 A 股标的，技术面分析跳过。\n'
        )
        return _persist_p8_outputs(
            markdown,
            {},
            status='skipped_no_stocks',
            metrics={'stocks_requested': 0, 'stocks_with_data': 0},
        )
    vipdoc = _find_tdx_vipdoc()
    if not vipdoc:
        markdown = (
            '# P8 股价分析数据（本地通达信 250 日 K 线）\n\n'
            f'> 读取时间: {datetime.now().isoformat()}\n\n'
            '> 未找到本地通达信 `vipdoc` 目录，技术面分析不可用。\n'
        )
        return _persist_p8_outputs(
            markdown,
            {},
            status='skipped_no_vipdoc',
            metrics={'stocks_requested': len(stocks), 'stocks_with_data': 0},
        )

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
        elasticity = elasticity_map.get(code, '⚡中弹性') if elasticity_map else '⚡中弹性'
        track_label = track_map.get(code, '未分类') if track_map else '未分类'

        market, filepath = _resolve_tdx_market_and_path(vipdoc, code)
        if os.path.exists(filepath):
            records = read_day_file_simple(filepath)
        else:
            records = []
            logging.warning(f"[P8] 本地 K 线文件缺失 ({code}): {filepath}")

        is_core = '高弹性' in elasticity or '核心' in elasticity

        if not is_core:
            if records:
                r = records[-1]
                closes = [rc['close'] for rc in records[-250:]]
                highs = [rc['high'] for rc in records[-250:]]
                lows = [rc['low'] for rc in records[-250:]]
                chg_1m = _calc_period_change(closes, 20)
                rs_20d = chg_1m - _index_returns.get('上证指数', {}).get('chg_20d', 0)
                boll_width = _calc_boll_width(closes)
                atr_pct = _calc_atr_pct(highs, lows, closes)
                weekly = _calc_weekly_summary(closes)
                low_elasticity_names.append(
                    f"- 【{track_label}】{name}({code}) | 收盘 {r['close']:.2f}元 | "
                    f"20日{chg_1m:+.1f}% | RS{rs_20d:+.1f}% | ATR{atr_pct:.1f}% | "
                    f"周线{weekly['trend']} | 布林宽{boll_width:.1f}%"
                )
                p8_json_db[code] = {
                    'name': name,
                    'code': code,
                    'elasticity': elasticity,
                    'fast_path': True,
                    'track': track_label,
                    'latest_close': round(r['close'], 2),
                    'chg_20d_pct': round(chg_1m, 1),
                    'rs_20d_pct': round(rs_20d, 1),
                    'atr_pct': round(atr_pct, 1),
                    'boll_width_pct': round(boll_width, 1),
                    'weekly_trend': weekly['trend'],
                }
            else:
                low_elasticity_names.append(f"- 【{track_label}】{name}({code}) | 本地 K 线缺失，跳过技术画像")
                p8_json_db[code] = {
                    'name': name,
                    'code': code,
                    'elasticity': elasticity,
                    'fast_path': True,
                    'track': track_label,
                    'error': 'missing_local_kline',
                }

            continue

        if not records or len(records) < 60:
            price_summaries.append(f"### {name}（{code}）\n未找到足够的本地K线数据\n\n")
            p8_json_db[code] = {
                'name': name,
                'code': code,
                'elasticity': elasticity,
                'track': track_label,
                'error': 'insufficient_local_kline',
            }
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
        chg_1m = (latest_c - c_1m)/c_1m * 100 if c_1m > 0 else 0
        c_3m = closes[-60] if n_rec >= 60 else closes[0]
        chg_3m = (latest_c - c_3m)/c_3m * 100 if c_3m > 0 else 0

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
        min_ma = min(ma5, ma10, ma20, ma60)
        ma_spread = (max(ma5, ma10, ma20, ma60) - min_ma)/min_ma*100 if min_ma > 0 else 0

        # 量能
        avg_vol_5d = sum(vols[-5:])/5 if vols[-5:] else 1
        avg_vol_20d = sum(vols[-20:])/20 if vols[-20:] else 1
        avg_vol_60d = sum(vols[-60:])/60 if vols[-60:] else 1
        vr = avg_vol_5d / avg_vol_20d
        vs = avg_vol_5d / avg_vol_60d
        vol_desc = "明显放量" if vr > 1.5 else ("温和缩量" if vr < 0.7 else "正常")

        # 波动
        atr = _calc_atr_pct(highs, lows, closes)
        boll_w = _calc_boll_width(closes)
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
        weekly = _calc_weekly_summary(closes)
        w_stat = weekly['w_stat']
        wmacd = weekly['wmacd']
        wmacd_stat = weekly['wmacd_stat']
        w_trend = weekly['trend']

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
- 收盘 {latest_c:.2f} 元（{latest_r['date'][4:6]}-{latest_r['date'][6:8]}）
- 涨跌：近20日 {chg_1m:+.1f}% | 近60日 {chg_3m:+.1f}% | 250日分位 {int(pct_250d)}%（{pos_desc}）
- 距60日前高 {dist_to_60d_high:+.1f}%（回撤 {abs(dist_to_60d_high):.1f}%）| 突破：{breakouts}
- 均线：5日{(latest_c-ma5)/ma5*100:+.1f}%，10日{(latest_c-ma10)/ma10*100:+.1f}%，20日{(latest_c-ma20)/ma20*100:+.1f}%，60日{(latest_c-ma60)/ma60*100:+.1f}% | 密集度 {ma_spread:.1f}%
- 量能：{vol_desc}（量比 {vr:.2f}，5日/60日 {vs:.2f}）| 换手：未提供
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
                "turnover_10d_avg": None,
                "turnover_anomaly": None,
                "boll_width_pct": round(boll_w, 1),
                "atr_compress_ratio": round(ac, 2),
                "ma_spread_pct": round(ma_spread, 1),
                "macd_bar": round(macd_val, 3),
                "rsi_14": rsi_val,
                "kdj_j": kdj_j,
                "yang_ratio_20d": yang_ratio
            },
            "finance_data": {
                "total_cap_yi": None, "liutong_cap_yi": None, "jinglirun_yi": None
            },
            "tags": {
                "vol_desc": vol_desc,
                "pos_desc": pos_desc,
                "breakouts": [] if breakouts == "无" else [breakouts],
                "rsi_desc": rsi_desc,
                "macd_status": macd_stat,
                "turnover_anom_desc": "未提供"
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

    # 抽取纯MD但不含JSON标签，以符合老格式
    pure_md_str = "".join([
        "# P8 股价分析数据（本地通达信 250 日 K 线）\n\n",
        f"> 读取时间: {datetime.now().isoformat()}\n\n",
        "> 注：以下所有「X日」均指交易日（非自然日）\n\n",
        market_chunk,
        summary_chunk
    ])

    # P8 JSON 数据已落盘至独立文件。
    # 鉴于 P9 只需要读基本盘信息，这里只返回 Markdown，避免将 JSON 原样再发一遍浪费数千 Token
    stocks_with_data = sum(1 for item in p8_json_db.values() if not item.get('error'))
    return _persist_p8_outputs(
        pure_md_str,
        p8_json_db,
        status='completed',
        metrics={'stocks_requested': len(stocks), 'stocks_with_data': stocks_with_data},
    )
