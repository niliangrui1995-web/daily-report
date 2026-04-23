# -*- coding: utf-8 -*-
"""亚洲寡头 250 日 K 线数据拉取器。

从 industry_dict.py 自动读取 VANGUARD_TICKERS，
筛选出亚洲市场标的（.TW / .TWO / .KS / .T / .HK），
用 yfinance 拉取 250 个交易日的 OHLCV 日线数据，
输出 JSON 文件供前端看板渲染 K 线图。

用法：
    python asian_kline_fetcher.py              # 拉取全部亚洲标的
    python asian_kline_fetcher.py --market JP   # 只拉日本
    python asian_kline_fetcher.py --ticker 8035.T  # 只拉单只
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yfinance as yf

# Why: 脚本在 亚洲寡头行情/ 目录，industry_dict 在上级 每日战报/ 目录
#      加入 sys.path 后直接 import，改字典就自动同步，零维护
_PIPELINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "每日战报")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from industry_dict import VANGUARD_TICKERS, OLIGARCH_DICT  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def _ensure_yfinance_cert_bundle() -> None:
    """为 yfinance/curl_cffi 设置稳定证书路径，避免 Windows venv certifi 路径失效。"""
    try:
        import certifi

        cert_dir = os.path.join(tempfile.gettempdir(), "vcp_certs")
        os.makedirs(cert_dir, exist_ok=True)
        safe_cert_path = os.path.join(cert_dir, "cacert.pem")
        if not os.path.exists(safe_cert_path):
            shutil.copy2(certifi.where(), safe_cert_path)
        os.environ.setdefault("CURL_CA_BUNDLE", safe_cert_path)
    except (ImportError, OSError, PermissionError, shutil.Error) as exc:
        logging.warning(f"⚠️ 无法初始化 yfinance 证书路径: {exc}")

# ===================================================================
# 亚洲市场后缀映射（与 p6_mapper.py 保持一致）
# ===================================================================
ASIAN_MARKET_MAP = {
    ".TW": "台湾",
    ".TWO": "台湾上柜",
    ".KS": "韩国",
    ".T": "日本",
    ".HK": "香港",
}

# Why: 命令行参数用的简写 → 后缀映射
MARKET_SHORTCUT = {
    "TW": (".TW", ".TWO"),
    "KR": (".KS",),
    "JP": (".T",),
    "HK": (".HK",),
}


def _get_market_suffix(ticker: str) -> str | None:
    """提取 ticker 的市场后缀，无后缀返回 None。"""
    for suffix in ASIAN_MARKET_MAP:
        if ticker.endswith(suffix):
            return suffix
    return None


def _get_market_name(ticker: str) -> str:
    """获取 ticker 对应的市场中文名。"""
    suffix = _get_market_suffix(ticker)
    return ASIAN_MARKET_MAP.get(suffix, "未知")


def filter_asian_tickers(market_filter: str | None = None) -> dict[str, str]:
    """从 VANGUARD_TICKERS 中筛选亚洲市场标的。

    Args:
        market_filter: 可选市场简写（TW/KR/JP/HK），None 表示全部亚洲
    Returns:
        {公司名: ticker} 字典
    """
    result = {}

    # Why: 确定要筛选的后缀范围
    if market_filter:
        target_suffixes = MARKET_SHORTCUT.get(market_filter.upper())
        if not target_suffixes:
            logging.error(f"未知市场简写: {market_filter}，支持: TW/KR/JP/HK")
            return {}
    else:
        target_suffixes = tuple(ASIAN_MARKET_MAP.keys())

    for name, ticker in VANGUARD_TICKERS.items():
        if any(ticker.endswith(s) for s in target_suffixes):
            result[name] = ticker

    return result


def _find_track(ticker: str) -> str:
    """反查 ticker 所属的赛道名称。"""
    # Why: 从 VANGUARD_TICKERS 找到公司名，再从 OLIGARCH_DICT 反查赛道
    company_name = None
    for name, tk in VANGUARD_TICKERS.items():
        if tk == ticker:
            company_name = name
            break

    if not company_name:
        return "未知赛道"

    name_lower = company_name.lower()
    for track, companies in OLIGARCH_DICT.items():
        for comp in companies:
            comp_lower = comp.lower()
            # Why: 三层匹配——完整包含 > 英文前缀 > 公司名是赛道成员子串
            if name_lower in comp_lower or comp_lower.startswith(name_lower):
                return track
            eng_prefix = comp.split("(")[0].strip()
            if eng_prefix.lower() == name_lower:
                return track
    return "未知赛道"


def fetch_single_kline(name: str, ticker: str, period: str = "1y") -> dict | None:
    """拉取单只标的的 K 线数据。

    Returns:
        {
            "name": "Tokyo Electron",
            "ticker": "8035.T",
            "market": "日本",
            "track": "前道晶圆设备与量测",
            "currency": "JPY",
            "klines": [
                {"date": "2025-04-01", "open": 100, "high": 105, "low": 98, "close": 103, "volume": 12345},
                ...
            ]
        }
    """
    try:
        _ensure_yfinance_cert_bundle()
        t = yf.Ticker(ticker)
        hist = t.history(period=period)

        if hist.empty:
            logging.warning(f"⚠️ {name}({ticker}): 无数据")
            return None

        # Why: 获取货币单位，方便前端显示
        info = t.fast_info
        currency = getattr(info, "currency", "N/A") if info else "N/A"

        klines = []
        for idx, row in hist.iterrows():
            klines.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })

        return {
            "name": name,
            "ticker": ticker,
            "market": _get_market_name(ticker),
            "track": _find_track(ticker),
            "currency": currency,
            "kline_count": len(klines),
            "klines": klines,
        }

    except Exception as e:
        logging.error(f"❌ {name}({ticker}): 拉取失败 — {e}")
        return None


def fetch_all_asian_klines(
    market_filter: str | None = None,
    single_ticker: str | None = None,
    max_workers: int = 6,
    period: str = "1y",
) -> list[dict]:
    """并发拉取亚洲寡头 K 线数据。

    Args:
        market_filter: 市场筛选（TW/KR/JP/HK）
        single_ticker: 只拉单只
        max_workers: 并发线程数（别太高，Yahoo 会限速）
        period: yfinance 的 period 参数，默认 1y（约 250 个交易日）
    """
    # Why: 支持单只调试模式
    if single_ticker:
        name = None
        for n, tk in VANGUARD_TICKERS.items():
            if tk == single_ticker:
                name = n
                break
        if not name:
            logging.error(f"ticker {single_ticker} 不在 VANGUARD_TICKERS 中")
            return []
        tickers = {name: single_ticker}
    else:
        tickers = filter_asian_tickers(market_filter)

    if not tickers:
        logging.error("没有找到符合条件的亚洲标的")
        return []

    logging.info(
        f"📊 开始拉取 {len(tickers)} 只亚洲标的的 K 线数据 "
        f"(period={period}, workers={max_workers})"
    )

    results = []
    failed = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for name, ticker in tickers.items():
            # Why: 错开请求避免被 Yahoo 限速
            time.sleep(0.3)
            futures[pool.submit(fetch_single_kline, name, ticker, period)] = (name, ticker)

        for future in as_completed(futures, timeout=120):
            name, ticker = futures[future]
            try:
                data = future.result()
                if data:
                    results.append(data)
                    logging.info(
                        f"  ✅ {name}({ticker}) [{data['market']}]: "
                        f"{data['kline_count']} 根K线"
                    )
                else:
                    failed.append(f"{name}({ticker})")
            except Exception as e:
                failed.append(f"{name}({ticker})")
                logging.error(f"  ❌ {name}({ticker}): {e}")

    # Why: 按市场分组排序，便于前端渲染
    results.sort(key=lambda x: (x["market"], x["name"]))

    logging.info(
        f"\n📊 拉取完成: 成功 {len(results)}/{len(tickers)}"
        + (f"，失败 {len(failed)}: {', '.join(failed)}" if failed else "")
    )

    return results


def save_kline_data(
    data: list[dict],
    output_dir: str | None = None,
    *,
    keep_history: bool = False,
) -> str:
    """保存 K 线数据到 JSON 文件。"""
    if not output_dir:
        # Why: 默认存到同级 data/ 目录，与脚本同文件夹
        output_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data"
        )
    os.makedirs(output_dir, exist_ok=True)

    now_str = datetime.now().strftime("%Y%m%d_%H%M")
    history_path = os.path.join(output_dir, f"asian_klines_{now_str}.json")

    # Why: 生成元数据摘要，前端可以读 meta 做标题/更新时间显示
    meta = {
        "generated_at": datetime.now().isoformat(),
        "total_stocks": len(data),
        "markets": {},
    }
    for item in data:
        market = item["market"]
        meta["markets"].setdefault(market, 0)
        meta["markets"][market] += 1

    output = {
        "meta": meta,
        "stocks": data,
    }

    # Why: latest.json 才是前端固定读取路径，默认只维护这一份，避免仓库持续堆积 1.7MB 历史快照
    latest_path = os.path.join(output_dir, "asian_klines_latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logging.info(f"💾 最新快照已更新: {latest_path}")

    if keep_history:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logging.info(f"💾 历史快照已保存: {history_path} ({os.path.getsize(history_path) / 1024:.0f} KB)")
        return history_path

    return latest_path


def main():
    parser = argparse.ArgumentParser(description="亚洲寡头 250 日 K 线数据拉取器")
    parser.add_argument(
        "--market",
        choices=["TW", "KR", "JP", "HK"],
        help="只拉指定市场（TW=台湾 KR=韩国 JP=日本 HK=香港）",
    )
    parser.add_argument(
        "--ticker",
        help="只拉单只标的（如 8035.T）",
    )
    parser.add_argument(
        "--period",
        default="1y",
        help="K 线周期（默认 1y ≈ 250 个交易日）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="并发线程数（默认 6，太高会被 Yahoo 限速）",
    )
    parser.add_argument(
        "--output-dir",
        help="输出目录（默认：亚洲寡头行情/data/）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出目标标的，不实际拉数据",
    )
    parser.add_argument(
        "--keep-history",
        action="store_true",
        help="额外保留带时间戳的历史快照（默认仅更新 latest.json）",
    )

    args = parser.parse_args()

    # Why: dry-run 模式方便确认标的列表是否正确
    if args.dry_run:
        tickers = filter_asian_tickers(args.market)
        print(f"\n📋 目标标的 ({len(tickers)} 只):\n")
        for name, ticker in sorted(tickers.items(), key=lambda x: x[1]):
            print(f"  {ticker:>12s}  {name:<20s}  [{_get_market_name(ticker)}] {_find_track(ticker)}")
        return

    data = fetch_all_asian_klines(
        market_filter=args.market,
        single_ticker=args.ticker,
        max_workers=args.workers,
        period=args.period,
    )

    if data:
        save_kline_data(data, args.output_dir, keep_history=args.keep_history)


if __name__ == "__main__":
    main()
