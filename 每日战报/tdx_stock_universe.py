r"""Local Tongdaxin stock universe loader.

The P6 mapper must not trust model-generated stock codes.  It resolves the
final A-share code from the local Tongdaxin security tables under D:\HT.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterator

DEFAULT_TDX_ROOT = os.environ.get('TDX_ROOT', r'D:\HT')
TNF_MARKET_FILES = ('shs.tnf', 'szs.tnf', 'bjs.tnf')
TNF_HEADER_SIZE = 50
TNF_RECORD_SIZE = 360
TNF_CODE_OFFSET = 0
TNF_CODE_SIZE = 6
TNF_NAME_OFFSET = 31
TNF_NAME_SIZE = 40
INFOHARBOR_CODE_FILE = 'infoharbor_ex.code'


def normalize_stock_name(name: str) -> str:
    return re.sub(r'\s+', '', str(name or '').strip())


def _decode_tnf_field(raw: bytes) -> str:
    return raw.split(b'\0', 1)[0].strip().decode('gbk', errors='ignore').strip()


def _is_a_share_stock_code(code: str, market_file: str) -> bool:
    if market_file == 'shs.tnf':
        return code.startswith(('600', '601', '603', '605', '688', '689'))
    if market_file == 'szs.tnf':
        return code.startswith(('000', '001', '002', '003', '300', '301'))
    if market_file == 'bjs.tnf':
        return code.startswith(('43', '83', '87', '88', '92'))
    return False


def _add_stock_code(stock_code_map: dict[str, str], name: str, code: str) -> None:
    for key in {name, normalize_stock_name(name)}:
        if not key:
            continue
        existing_code = stock_code_map.get(key)
        if existing_code and existing_code != code:
            logging.warning(f'[TDX股票主数据] 公司名重复：{key} {existing_code}/{code}，保留首次记录')
            continue
        stock_code_map[key] = code


def _iter_tnf_records(path: Path, market_file: str) -> Iterator[tuple[str, str]]:
    data = path.read_bytes()
    if len(data) <= TNF_HEADER_SIZE:
        return

    payload_size = len(data) - TNF_HEADER_SIZE
    if payload_size % TNF_RECORD_SIZE != 0:
        logging.warning(f'[TDX股票主数据] {path} 长度异常，按完整记录部分读取')

    record_count = payload_size // TNF_RECORD_SIZE
    for index in range(record_count):
        start = TNF_HEADER_SIZE + index * TNF_RECORD_SIZE
        record = data[start:start + TNF_RECORD_SIZE]
        code = _decode_tnf_field(record[TNF_CODE_OFFSET:TNF_CODE_OFFSET + TNF_CODE_SIZE])
        name = _decode_tnf_field(record[TNF_NAME_OFFSET:TNF_NAME_OFFSET + TNF_NAME_SIZE])
        if re.fullmatch(r'\d{6}', code) and name and _is_a_share_stock_code(code, market_file):
            yield name, code


def _read_text_file(path: Path) -> str:
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding='gbk', errors='ignore')


def _iter_infoharbor_full_names(path: Path) -> Iterator[tuple[str, str]]:
    text = _read_text_file(path)
    for line in text.splitlines():
        fields = [field.strip() for field in line.split('|')]
        if len(fields) < 2:
            continue
        code, name = fields[0], fields[1]
        if re.fullmatch(r'\d{6}', code) and name:
            yield name, code


@lru_cache(maxsize=4)
def load_tdx_stock_code_map(tdx_root: str | os.PathLike[str] = DEFAULT_TDX_ROOT) -> dict[str, str]:
    hq_cache = Path(tdx_root) / 'T0002' / 'hq_cache'
    stock_code_map: dict[str, str] = {}
    stock_codes: set[str] = set()

    for market_file in TNF_MARKET_FILES:
        path = hq_cache / market_file
        if not path.exists():
            logging.warning(f'[TDX股票主数据] 缺少 {path}')
            continue

        try:
            records = _iter_tnf_records(path, market_file)
            for name, code in records:
                stock_codes.add(code)
                _add_stock_code(stock_code_map, name, code)
        except OSError as exc:
            logging.warning(f'[TDX股票主数据] 读取失败 {path}: {exc}')

    full_name_path = hq_cache / INFOHARBOR_CODE_FILE
    if full_name_path.exists():
        try:
            for name, code in _iter_infoharbor_full_names(full_name_path):
                if code in stock_codes:
                    _add_stock_code(stock_code_map, name, code)
        except OSError as exc:
            logging.warning(f'[TDX股票主数据] 读取失败 {full_name_path}: {exc}')

    return stock_code_map
