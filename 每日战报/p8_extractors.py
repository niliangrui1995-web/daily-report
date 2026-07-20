"""P8 extractors and local data readers."""

from __future__ import annotations

import logging
import math
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


def load_tdx_capital_snapshot(vipdoc: str, codes):
    """从通达信 base.dbf 读取指定股票的总股本与流通 A 股股本。"""
    requested_codes = {
        str(code).strip()
        for code in (codes or [])
        if re.fullmatch(r'\d{6}', str(code).strip())
    }
    if not requested_codes:
        return {}

    base_dbf = os.path.join(
        os.path.dirname(os.path.normpath(vipdoc)),
        'T0002',
        'hq_cache',
        'base.dbf',
    )
    try:
        with open(base_dbf, 'rb') as f:
            data = f.read()
        if len(data) < 33:
            raise ValueError('DBF header is incomplete')

        record_count = struct.unpack_from('<I', data, 4)[0]
        header_length = struct.unpack_from('<H', data, 8)[0]
        record_length = struct.unpack_from('<H', data, 10)[0]
        if header_length < 33 or record_length < 2 or header_length > len(data):
            raise ValueError('DBF header lengths are invalid')

        fields = {}
        field_offset = 1
        descriptor_offset = 32
        while descriptor_offset + 32 <= header_length:
            if data[descriptor_offset] == 0x0D:
                break
            descriptor = data[descriptor_offset:descriptor_offset + 32]
            name = descriptor[:11].split(b'\x00', 1)[0].decode('ascii', errors='ignore').strip().upper()
            length = descriptor[16]
            if name and length:
                fields[name] = (field_offset, length)
            field_offset += length
            descriptor_offset += 32

        if 'GPDM' not in fields or 'LTAG' not in fields:
            raise ValueError('DBF is missing GPDM or LTAG fields')

        def read_field(row, field_name):
            field = fields.get(field_name)
            if not field:
                return ''
            offset, length = field
            return row[offset:offset + length].decode('ascii', errors='ignore').strip()

        def parse_shares(row, field_name):
            raw_value = read_field(row, field_name)
            if not raw_value:
                return None
            value = float(raw_value) * 10_000.0
            return value if value > 0 and math.isfinite(value) else None

        snapshot = {}
        available_records = max(0, (len(data) - header_length) // record_length)
        for index in range(min(record_count, available_records)):
            start = header_length + index * record_length
            row = data[start:start + record_length]
            if not row or row[0] == 0x2A:
                continue
            code = read_field(row, 'GPDM')
            if code not in requested_codes:
                continue
            total_shares = parse_shares(row, 'ZGB')
            float_shares = parse_shares(row, 'LTAG')
            if total_shares is None and float_shares is None:
                continue
            snapshot[code] = {
                'total_shares': total_shares,
                'float_shares': float_shares,
                'updated_at': read_field(row, 'GXRQ'),
                'source': 'tdx_base_dbf',
            }
            if len(snapshot) == len(requested_codes):
                break
        return snapshot
    except (OSError, IndexError, struct.error, TypeError, ValueError) as exc:
        logging.warning('[P8] 读取本地股本数据失败 (%s): %s', base_dbf, exc)
        return {}
