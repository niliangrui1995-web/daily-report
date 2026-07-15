"""
P4 全球核心标的行情层 — yfinance 产业链核心标的客观行情注入。

从 p6_mapper.py 拆分，原属"上帝模块"的一部分。

【⚠️核心维护规则 / Agent 指令】
1. `fetch_vanguard_prices()` 中的时差对齐逻辑（剔除今日不完整盘中 bar、
   用 A 股上一交易日的前一天锚定 baseline_date、长假自动累计区间涨跌）
   是经过反复验证的精密机制，严禁随意修改 `completed_hist` 过滤和
   `baseline_date` 计算公式。
2. yfinance 是唯一允许的全球核心标的行情数据源。P8 是 A 股本地通达信技术面数据，
   严禁将 P4 的 yfinance 拉取逻辑混入 P8。
"""

import concurrent.futures
import logging
import multiprocessing
import os
import time
import traceback
from datetime import date, datetime

from artifact_store import update_run_manifest, write_text_artifact
from industry_dict import VANGUARD_TICKERS, SUPER_GIANTS, STRATEGIC_GIANTS, classify_vanguard_signal
from run_config import _get_prev_trading_day

P4_TOTAL_TIMEOUT_SECONDS = 180
P4_REQUEST_TIMEOUT_SECONDS = 12
P4_MAX_RETRIES = 2


def _fetch_vanguard_prices_inline() -> str:
    import shutil
    import tempfile
    import certifi
    start_time = time.time()

    temp_cert_dir = tempfile.mkdtemp(prefix='vcp_certs_')
    safe_cert_path = os.path.join(temp_cert_dir, 'cacert.pem')
    shutil.copy2(certifi.where(), safe_cert_path)

    # Why: 只在本函数作用域内设置 CURL_CA_BUNDLE，退出时恢复原值，防止全局副作用
    _original_ca_bundle = os.environ.get('CURL_CA_BUNDLE')
    os.environ['CURL_CA_BUNDLE'] = safe_cert_path

    try:
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            logging.warning('⚠️ yfinance/pandas 未安装，P4 核心行情不可用')
            update_run_manifest('p4', status='skipped_missing_dependency')
            return ''

        # Why: _get_prev_trading_day 期望 date 对象，不是字符串
        prev_day = _get_prev_trading_day(date.today())
        prev_day_str = prev_day.strftime('%Y-%m-%d')

        logging.info(f'[yfinance] 正在获取产业链 {len(VANGUARD_TICKERS)} 只核心标的自 {prev_day_str} 以来的区间客观股价行情...')

        lines = []
        lines.append('==================== 核心标的客观行情数据 (yfinance) ====================')
        lines.append(f'产业链核心标的客观行情（A股基准日: {prev_day_str}，显示自该日以来全球核心标的区间总涨跌）：')
        lines.append('分类规则：超级巨头(万亿级)区间≥3%触发催化/≤-5%警惕 | 中型标的区间≥5%触发催化/≤-8%警惕\n')

        ticker_outcomes = {'usable': 0, 'no_data': 0, 'failed': 0}

        def _fetch_single(name: str, ticker: str) -> tuple[str, str]:
            time.sleep(0.3)
            if name in SUPER_GIANTS:
                tier = '超级巨头'
            elif name in STRATEGIC_GIANTS:
                tier = '战略核心'
            else:
                tier = '中型标的'
            max_retries = P4_MAX_RETRIES

            for attempt in range(max_retries):
                try:
                    t = yf.Ticker(ticker)
                    hist = t.history(
                        period='1mo',
                        timeout=P4_REQUEST_TIMEOUT_SECONDS,
                        raise_errors=True,
                    )

                    if len(hist) == 0:
                        return 'no_data', f'- {name}({ticker}) [{tier}]: 暂无最新交易数据'

                    hist.index = hist.index.tz_localize(None).normalize()
                    today = pd.to_datetime('today').normalize()
                    completed_hist = hist[hist.index < today]

                    if len(completed_hist) < 1:
                        return 'no_data', f'- {name}({ticker}) [{tier}]: 今日尚无完整收盘数据'

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
                        return 'usable', f'- [{signal}] {name}({ticker}) [{tier}]: 收盘价 {curr_close:.2f}, 区间涨跌 {pct_change:+.2f}%'

                    return 'no_data', f'- [📈 正常] {name}({ticker}) [{tier}]: 收盘价 {curr_close:.2f} (无足够历史对比)'

                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue

                    tb_str = traceback.format_exc()
                    logging.error(f'❌ [yfinance] 获取 {ticker} 失败: {type(e).__name__} - {e}\n{tb_str}')
                    return 'failed', f'- {name}({ticker}) [{tier}]: 获取股价失败'

        # Why: 80+ 标的只开 2 线程太慢，提升到 6 线程（与亚洲版一致）
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {name: pool.submit(_fetch_single, name, ticker) for name, ticker in VANGUARD_TICKERS.items()}

            for fut in concurrent.futures.as_completed(futures.values()):
                try:
                    outcome, result = fut.result()
                    ticker_outcomes[outcome] += 1
                    if result:
                        lines.append(result)
                except Exception:
                    ticker_outcomes['failed'] += 1
                    for name, candidate in futures.items():
                        if candidate == fut:
                            lines.append(f'- {name}({VANGUARD_TICKERS[name]}): 获取失败')
                            break

        lines.append('================================================================\n')
        report = '\n'.join(lines)

        try:
            md_body = f'# P4 全球核心标的客观行情\n\n> 生成时间: {datetime.now().isoformat()}\n\n{report}'
            fname = write_text_artifact('P4_核心行情', md_body)
            logging.info(f'💾 [yfinance] 核心标的客观行情已落盘: {fname}')
        except Exception as e:
            logging.warning(f'⚠️ [yfinance] 行情落盘失败: {e}')
            fname = ''

        usable_tickers = ticker_outcomes['usable']
        has_partial_degradation = bool(
            ticker_outcomes['no_data'] or ticker_outcomes['failed']
        )
        if usable_tickers == 0:
            manifest_status = 'failed_no_usable_data'
            data_state = 'degraded_empty'
            degrade_reason = 'no_usable_ticker_data'
        elif has_partial_degradation:
            manifest_status = 'completed_structured_degraded'
            data_state = 'degraded'
            degrade_reason = 'partial_ticker_data'
        else:
            manifest_status = 'completed'
            data_state = 'valid'
            degrade_reason = ''

        update_run_manifest(
            'p4',
            status=manifest_status,
            files={'md': fname} if fname else None,
            metrics={
                'tickers': len(VANGUARD_TICKERS),
                'usable_tickers': usable_tickers,
                'no_data_tickers': ticker_outcomes['no_data'],
                'failed_tickers': ticker_outcomes['failed'],
                'data_state': data_state,
                'structured_degraded': has_partial_degradation,
                'degrade_reason': degrade_reason,
                'report_chars': len(report),
                'duration_seconds': round(time.time() - start_time, 1),
            },
        )
        return report
    finally:
        # Why: 恢复原始环境变量，避免污染后续模块的 SSL 证书路径
        if _original_ca_bundle is not None:
            os.environ['CURL_CA_BUNDLE'] = _original_ca_bundle
        elif 'CURL_CA_BUNDLE' in os.environ:
            del os.environ['CURL_CA_BUNDLE']
        shutil.rmtree(temp_cert_dir, ignore_errors=True)


def _p4_process_entry(send_conn) -> None:
    try:
        send_conn.send({'status': 'success', 'report': _fetch_vanguard_prices_inline()})
    except BaseException as exc:
        send_conn.send({'status': 'error', 'error': f'{type(exc).__name__}: {exc}'})
    finally:
        send_conn.close()


def _stop_process(process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def fetch_vanguard_prices() -> str:
    """Run P4 behind a killable wall-clock deadline."""
    mp_context = multiprocessing.get_context('spawn')
    recv_conn, send_conn = mp_context.Pipe(duplex=False)
    process = mp_context.Process(target=_p4_process_entry, args=(send_conn,), name='p4-vanguard-fetch')

    try:
        process.start()
        send_conn.close()
        if not recv_conn.poll(P4_TOTAL_TIMEOUT_SECONDS):
            logging.error('[yfinance] P4 exceeded hard timeout of %ss', P4_TOTAL_TIMEOUT_SECONDS)
            _stop_process(process)
            update_run_manifest(
                'p4',
                status='failed_timeout',
                metrics={'timeout_seconds': P4_TOTAL_TIMEOUT_SECONDS},
            )
            return ''

        payload = recv_conn.recv()
        process.join(timeout=5)
        _stop_process(process)
    except Exception as exc:
        _stop_process(process)
        logging.error('[yfinance] P4 subprocess failed: %s', exc)
        update_run_manifest('p4', status='failed_exception', metrics={'error': str(exc)})
        return ''
    finally:
        recv_conn.close()
        send_conn.close()

    if payload.get('status') != 'success':
        error = str(payload.get('error') or 'unknown P4 subprocess error')
        logging.error('[yfinance] P4 subprocess returned error: %s', error)
        update_run_manifest('p4', status='failed_exception', metrics={'error': error})
        return ''
    return str(payload.get('report') or '')
