"""
P4 海外行情层 — yfinance 产业链核心标的客观行情注入。

从 p6_mapper.py 拆分，原属"上帝模块"的一部分。

【⚠️核心维护规则 / Agent 指令】
1. `fetch_vanguard_prices()` 中的时差对齐逻辑（剔除今日不完整盘中 bar、
   用 A 股上一交易日的前一天锚定 baseline_date、长假自动累计区间涨跌）
   是经过反复验证的精密机制，严禁随意修改 `completed_hist` 过滤和
   `baseline_date` 计算公式。
2. yfinance 是唯一允许的海外行情数据源。P8 是 A 股本地通达信数据，
   严禁将 yfinance 逻辑混入 P8。
"""

import concurrent.futures
import logging
import os
import time
import traceback
from datetime import date, datetime

from artifact_store import update_run_manifest, write_text_artifact
from industry_dict import VANGUARD_TICKERS, SUPER_GIANTS, classify_vanguard_signal
from run_config import _get_prev_trading_day

def fetch_vanguard_prices() -> str:
    import shutil
    import tempfile
    import certifi

    temp_cert_dir = os.path.join(tempfile.gettempdir(), 'vcp_certs')
    os.makedirs(temp_cert_dir, exist_ok=True)
    safe_cert_path = os.path.join(temp_cert_dir, 'cacert.pem')

    if not os.path.exists(safe_cert_path):
        shutil.copy2(certifi.where(), safe_cert_path)

    # Why: 只在本函数作用域内设置 CURL_CA_BUNDLE，退出时恢复原值，防止全局副作用
    _original_ca_bundle = os.environ.get('CURL_CA_BUNDLE')
    os.environ['CURL_CA_BUNDLE'] = safe_cert_path

    try:
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            logging.warning('⚠️ yfinance/pandas 未安装，P4 海外行情不可用')
            update_run_manifest('p4', status='skipped_missing_dependency')
            return ''

        # Why: _get_prev_trading_day 期望 date 对象，不是字符串
        prev_day = _get_prev_trading_day(date.today())
        prev_day_str = prev_day.strftime('%Y-%m-%d')

        logging.info(f'[yfinance] 正在获取产业链 {len(VANGUARD_TICKERS)} 只核心标的自 {prev_day_str} 以来的区间客观股价行情...')

        lines = []
        lines.append('==================== 客观行情数据 (yfinance) ====================')
        lines.append(f'产业链核心标的客观行情（A股基准日: {prev_day_str}，显示自该日以来海外市场区间总涨跌）：')
        lines.append('分类规则：超级巨头(万亿级)区间≥3%触发催化/≤-5%警惕 | 中型标的区间≥5%触发催化/≤-8%警惕\n')

        def _fetch_single(name: str, ticker: str) -> str:
            time.sleep(0.3)
            tier = '超级巨头' if name in SUPER_GIANTS else '中型标的'
            max_retries = 3

            for attempt in range(max_retries):
                try:
                    t = yf.Ticker(ticker)
                    hist = t.history(period='1mo')

                    if len(hist) == 0:
                        return f'- {name}({ticker}) [{tier}]: 暂无最新交易数据'

                    hist.index = hist.index.tz_localize(None).normalize()
                    today = pd.to_datetime('today').normalize()
                    completed_hist = hist[hist.index < today]

                    if len(completed_hist) < 1:
                        return f'- {name}({ticker}) [{tier}]: 今日尚无完整收盘数据'

                    baseline_date = pd.to_datetime(prev_day_str) - pd.Timedelta(days=1)
                    past_hist = completed_hist[completed_hist.index <= baseline_date]

                    if len(past_hist) > 0:
                        prev_close = past_hist['Close'].iloc[-1]
                    else:
                        prev_close = completed_hist['Close'].iloc[0]

                    curr_close = completed_hist['Close'].iloc[-1]

                    if curr_close > 0 and prev_close > 0:
                        pct_change = (curr_close - prev_close) / prev_close * 100
                        signal = classify_vanguard_signal(name, pct_change)
                        return f'- [{signal}] {name}({ticker}) [{tier}]: ${curr_close:.2f}, 区间涨跌 {pct_change:+.2f}%'

                    return f'- [📈 正常] {name}({ticker}) [{tier}]: ${curr_close:.2f} (无足够历史对比)'

                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue

                    tb_str = traceback.format_exc()
                    logging.error(f'❌ [yfinance] 获取 {ticker} 失败: {type(e).__name__} - {e}\n{tb_str}')
                    return f'- {name}({ticker}) [{tier}]: 获取股价失败'

        # Why: 80+ 标的只开 2 线程太慢，提升到 6 线程（与亚洲版一致）
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {name: pool.submit(_fetch_single, name, ticker) for name, ticker in VANGUARD_TICKERS.items()}

            try:
                for fut in concurrent.futures.as_completed(futures.values(), timeout=180):
                    try:
                        result = fut.result()
                        if result:
                            lines.append(result)
                    except Exception:
                        for name, f in futures.items():
                            if f == fut:
                                lines.append(f'- {name}({VANGUARD_TICKERS[name]}): 获取超时')
            except (concurrent.futures.TimeoutError, TimeoutError):
                logging.warning('[yfinance] 部分标的获取超时（180s），使用已获取的数据')
                for name, fut in futures.items():
                    if not fut.done():
                        lines.append(f'- {name}({VANGUARD_TICKERS[name]}): 获取超时')

        lines.append('================================================================\n')
        report = '\n'.join(lines)

        try:
            md_body = f'# P4 海外客观行情\n\n> 生成时间: {datetime.now().isoformat()}\n\n{report}'
            fname = write_text_artifact('P4_海外行情', md_body)
            logging.info(f'💾 [yfinance] 海外客观行情已落盘: {fname}')
        except Exception as e:
            logging.warning(f'⚠️ [yfinance] 行情落盘失败: {e}')
            fname = ''

        update_run_manifest(
            'p4',
            status='completed',
            files={'md': fname} if fname else None,
            metrics={'tickers': len(VANGUARD_TICKERS), 'report_chars': len(report)},
        )
        return report
    finally:
        # Why: 恢复原始环境变量，避免污染后续模块的 SSL 证书路径
        if _original_ca_bundle is not None:
            os.environ['CURL_CA_BUNDLE'] = _original_ca_bundle
        elif 'CURL_CA_BUNDLE' in os.environ:
            del os.environ['CURL_CA_BUNDLE']
