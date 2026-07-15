"""P8 extractors and local data readers."""

from __future__ import annotations

import logging
import os
import re
import struct

from p6_target_parser import extract_p6_targets
from tdx_stock_universe import resolve_tdx_root


def find_tdx_vipdoc():
    vipdoc = resolve_tdx_root() / 'vipdoc'
    if vipdoc.is_dir():
        return str(vipdoc)
    return None


def resolve_tdx_market_and_path(vipdoc: str, code: str):
    """按股票代码推断通达信市场目录，并做文件存在性回退探测。"""
    candidates = []

    if code.startswith(('6', '9', '5')):
        candidates.append(('sh', f'sh{code}.day'))
    elif code.startswith(('8', '4')):
        candidates.extend(
            [
                ('bj', f'bj{code}.day'),
                ('sz', f'sz{code}.day'),
                ('sh', f'sh{code}.day'),
            ]
        )
    else:
        candidates.extend(
            [
                ('sz', f'sz{code}.day'),
                ('sh', f'sh{code}.day'),
            ]
        )

    deduped = []
    seen = set()
    for market, fname in candidates:
        key = (market, fname)
        if key not in seen:
            deduped.append((market, fname))
            seen.add(key)

    for market, fname in deduped:
        path = os.path.join(vipdoc, market, 'lday', fname)
        if os.path.exists(path):
            return market, path

    fallback_market, fallback_name = deduped[0]
    fallback_path = os.path.join(vipdoc, fallback_market, 'lday', fallback_name)
    return fallback_market, fallback_path


def extract_stocks_from_report(report: str):
    stocks = {code: name for name, code in extract_p6_targets(report)}
    patterns = [
        r'([\u4e00-\u9fa5]{2,6})[（\(]\s*(\d{6})\s*[）\)]',
        r'([\u4e00-\u9fa5]{2,6})\s+(\d{6})',
        r'\|\s*([\u4e00-\u9fa5]{2,6})\s*\|\s*(\d{6})\s*\|',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, report):
            name, code = match.group(1), match.group(2)
            if code not in stocks:
                stocks[code] = name
    return [(name, code) for code, name in stocks.items()]


def extract_stock_tracks(report: str):
    track_map = {}
    current_track = ""
    for line in report.split('\n'):
        track_match = re.match(r'###\s*赛道[:：]\s*(.+)', line)
        if track_match:
            current_track = track_match.group(1).strip()
            continue
        if current_track:
            parsed_targets = extract_p6_targets(line)
            if parsed_targets:
                for _name, code in parsed_targets:
                    track_map[code] = current_track
                continue
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


def read_day_file_simple(filepath):
    records = []
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        for chunk in struct.iter_unpack('<IIIIIfII', data):
            date_int, o, h, low, c, _amount_f, vol, _ = chunk
            date_str = str(date_int)
            if len(date_str) == 8:
                records.append(
                    {
                        'date': date_str,
                        'open': o / 100.0,
                        'high': h / 100.0,
                        'low': low / 100.0,
                        'close': c / 100.0,
                        'vol': vol,
                    }
                )
        return records
    except Exception as e:
        logging.error(f"[P8] 读取本地 K 线数据失败 ({filepath}): {e}")
        return []
