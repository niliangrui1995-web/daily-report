r"""Local Tongdaxin stock universe loader.

The P6 mapper must not trust model-generated stock codes.  It resolves the
final A-share code from the local Tongdaxin security tables under
D:\HT by default.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_TDX_ROOT = r'D:\HT'
TNF_MARKET_FILES = ('shs.tnf', 'szs.tnf', 'bjs.tnf')
TNF_HEADER_SIZE = 50
TNF_RECORD_SIZE = 360
TNF_CODE_OFFSET = 0
TNF_CODE_SIZE = 6
TNF_NAME_OFFSET = 31
TNF_NAME_SIZE = 40
INFOHARBOR_CODE_FILE = 'infoharbor_ex.code'
COMPANY_ALIAS_SUFFIXES = ('科技', '股份')
COMPANY_ALIAS_MARKERS = ('科技', '股份', '集团', '控股')
IDENTITY_VALIDATION_VERSION = 2
CANONICAL_STOCK_IDENTITIES: dict[str, tuple[str, str]] = {
    '宏和电子': ('宏和科技', '603256'),
}
GENERIC_REJECTED_TEXT_NAMES = frozenset({
    'AI',
    '中国',
    '电子',
    '科技',
    '股份',
    '集团',
    '公司',
    '芯片',
    '材料',
})


def resolve_tdx_root(tdx_root: str | os.PathLike[str] | None = None) -> Path:
    if tdx_root is None:
        configured_root = os.environ.get('TDX_ROOT', '').strip()
        tdx_root = configured_root or DEFAULT_TDX_ROOT
    return Path(tdx_root)


def normalize_stock_name(name: str) -> str:
    return re.sub(r'\s+', '', str(name or '').strip())


def canonicalize_stock_identity(name: object) -> tuple[str, str]:
    raw_name = str(name or '').strip()
    compact_name = normalize_stock_name(raw_name)
    without_code = re.sub(r'[\s（(【\[]*\d{6}[\s）)】\]]*', '', compact_name).strip()
    identity = CANONICAL_STOCK_IDENTITIES.get(without_code)
    if identity:
        return identity
    for canonical_name, expected_code in CANONICAL_STOCK_IDENTITIES.values():
        if without_code == canonical_name:
            return canonical_name, expected_code
    return raw_name, ''


def canonicalize_stock_aliases_in_text(text: object) -> tuple[str, int]:
    value = str(text or '')
    rewrite_count = 0
    for alias_name, (canonical_name, expected_code) in CANONICAL_STOCK_IDENTITIES.items():
        coded_pattern = re.compile(
            rf'(?:{re.escape(alias_name)}|{re.escape(canonical_name)})'
            rf'\s*[（(]\s*\d{{6}}\s*[）)]'
        )

        def replace_coded(match: re.Match[str]) -> str:
            nonlocal rewrite_count
            replacement = f'{canonical_name}（{expected_code}）'
            if match.group(0) != replacement:
                rewrite_count += 1
            return replacement

        value = coded_pattern.sub(replace_coded, value)
        value, standalone_count = re.subn(
            rf'{re.escape(alias_name)}(?!材料\s*科技\s*股份\s*有限公司)',
            canonical_name,
            value,
        )
        rewrite_count += standalone_count
    return value, rewrite_count


def canonicalize_stock_aliases_in_tree(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return canonicalize_stock_aliases_in_text(value)
    if isinstance(value, list):
        rewritten: list[Any] = []
        rewrite_count = 0
        for item in value:
            normalized_item, item_count = canonicalize_stock_aliases_in_tree(item)
            rewritten.append(normalized_item)
            rewrite_count += item_count
        return rewritten, rewrite_count
    if isinstance(value, dict):
        rewritten_dict: dict[Any, Any] = {}
        rewrite_count = 0
        for key, item in value.items():
            normalized_item, item_count = canonicalize_stock_aliases_in_tree(item)
            rewritten_dict[key] = normalized_item
            rewrite_count += item_count
        return rewritten_dict, rewrite_count
    return value, 0


def normalize_rejected_stock_names(names: Iterable[object] | None) -> list[str]:
    if isinstance(names, (str, bytes)):
        items: Iterable[object] = [names]
    elif isinstance(names, (list, tuple, set, frozenset)):
        items = names
    else:
        items = []

    normalized: list[str] = []
    seen: set[str] = set()
    for name in items:
        value = normalize_stock_name(str(name or ''))
        if not value or value in CANONICAL_STOCK_IDENTITIES or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def find_rejected_stock_names(text: object, names: Iterable[object] | None) -> list[str]:
    raw_text = str(text or '')
    haystack = normalize_stock_name(raw_text)
    if not haystack:
        return []
    matched: list[str] = []
    for name in normalize_rejected_stock_names(names):
        chinese_name = bool(re.fullmatch(r'[\u4e00-\u9fff]+', name))
        if len(name) == 1 or (chinese_name and len(name) <= 2) or name.upper() in GENERIC_REJECTED_TEXT_NAMES:
            exact_match = haystack.casefold() == name.casefold()
            coded_match = bool(
                re.search(
                    rf'{re.escape(name)}\s*[（(]\s*\d{{6}}\s*[）)]',
                    raw_text,
                    flags=re.IGNORECASE,
                )
            )
            if exact_match or coded_match:
                matched.append(name)
            continue

        if re.search(r'[A-Za-z0-9]', name):
            if re.search(
                rf'(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])',
                raw_text,
                flags=re.IGNORECASE,
            ):
                matched.append(name)
            continue

        if name in haystack:
            matched.append(name)
    return matched


def remove_rejected_stock_mentions(
    text: object,
    names: Iterable[object] | None,
) -> tuple[str, list[str]]:
    value, _ = canonicalize_stock_aliases_in_text(text)
    value = value.strip()
    removed = find_rejected_stock_names(value, names)
    for name in sorted(removed, key=len, reverse=True):
        value = re.sub(
            rf'{re.escape(name)}\s*[（(]\s*\d{{6}}\s*[）)]',
            '',
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(re.escape(name), '', value, flags=re.IGNORECASE)
    value = re.sub(r'/\s*/+', '/', value)
    value = re.sub(r'\s+', ' ', value).strip(' /、，,;；')
    return value, removed


def filter_rejected_stock_lines(
    text: object,
    names: Iterable[object] | None,
) -> tuple[str, int]:
    canonical_text, _ = canonicalize_stock_aliases_in_text(text)
    rejected_names = normalize_rejected_stock_names(names)
    if not rejected_names:
        return canonical_text, 0

    kept_lines: list[str] = []
    removed_count = 0
    for line in canonical_text.splitlines():
        if not find_rejected_stock_names(line, rejected_names):
            kept_lines.append(line)
            continue

        parts = re.split(r'([；;。！？!?、/，,])', line)
        kept_parts: list[str] = []
        for index in range(0, len(parts), 2):
            clause = parts[index]
            separator = parts[index + 1] if index + 1 < len(parts) else ''
            if find_rejected_stock_names(clause, rejected_names):
                continue
            kept_parts.append(f'{clause}{separator}')
        cleaned_line = ''.join(kept_parts).strip(' ；;。！？!?、/，,')
        removed_count += 1
        if cleaned_line:
            kept_lines.append(cleaned_line)
    return '\n'.join(kept_lines).strip(), removed_count


def filter_rejected_stock_tree(
    value: Any,
    names: Iterable[object] | None,
) -> tuple[Any, int]:
    rejected_names = normalize_rejected_stock_names(names)

    if isinstance(value, str):
        cleaned, removed_lines = filter_rejected_stock_lines(value, rejected_names)
        return cleaned, removed_lines

    if isinstance(value, list):
        filtered: list[Any] = []
        removed_count = 0
        for item in value:
            cleaned, nested_removed = filter_rejected_stock_tree(item, rejected_names)
            removed_count += nested_removed
            if isinstance(cleaned, str) and not cleaned:
                continue
            filtered.append(cleaned)
        return filtered, removed_count

    if isinstance(value, dict):
        filtered_dict: dict[Any, Any] = {}
        removed_count = 0
        for key, item in value.items():
            cleaned, nested_removed = filter_rejected_stock_tree(item, rejected_names)
            removed_count += nested_removed
            filtered_dict[key] = cleaned
        return filtered_dict, removed_count

    return value, 0


def build_rejected_stock_guardrail(names: Iterable[object] | None) -> str:
    rejected_names = normalize_rejected_stock_names(names)
    if not rejected_names:
        return ''
    joined = '、'.join(rejected_names)
    return (
        '【本地股票身份硬约束】\n'
        f'以下名称已被本地 TDX 股票主数据明确拒绝：{joined}。\n'
        '不得把它们作为 A 股公司、受益标的或交易建议，不得为其分配证券代码；'
        '若输入材料中出现，必须忽略相关公司级结论。'
    )


def _split_company_suffix(name: str) -> tuple[str, str]:
    normalized = normalize_stock_name(name)
    for suffix in COMPANY_ALIAS_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            return normalized[:-len(suffix)], suffix
    return normalized, ''


def _looks_like_company_alias(alias: str, primary_name: str) -> bool:
    alias = normalize_stock_name(alias)
    if len(alias) < 4:
        return False
    if alias.endswith(COMPANY_ALIAS_MARKERS):
        return True

    primary_base, _ = _split_company_suffix(primary_name)
    return len(primary_base) >= 2 and primary_base in alias and len(alias) <= 6


def _iter_infoharbor_aliases(primary_name: str, alias_field: str) -> Iterator[str]:
    primary_base, primary_suffix = _split_company_suffix(primary_name)
    for raw_alias in re.split(r'[,，、/；;]+', alias_field or ''):
        alias = normalize_stock_name(raw_alias)
        if not _looks_like_company_alias(alias, primary_name):
            continue

        yield alias
        if primary_suffix and primary_base in alias:
            alias_prefix = alias.split(primary_base, 1)[0]
            if len(alias_prefix) >= 2:
                yield f'{alias_prefix}{primary_suffix}'


def _stock_name_lookup_keys(name: object) -> list[str]:
    raw_name = str(name or '').strip()
    if not raw_name:
        return []

    compact_name = normalize_stock_name(raw_name)
    without_code = re.sub(r'[\s（(【\[]*\d{6}[\s）)】\]]*', '', compact_name).strip()

    keys: list[str] = []
    for key in (raw_name, compact_name, without_code, normalize_stock_name(without_code)):
        if key and key not in keys:
            keys.append(key)

    for key in list(keys):
        base, suffix = _split_company_suffix(key)
        if not suffix:
            continue
        for alt_suffix in COMPANY_ALIAS_SUFFIXES:
            alias_key = f'{base}{alt_suffix}'
            if alias_key and alias_key not in keys:
                keys.append(alias_key)
    return keys


def lookup_tdx_stock_code(stock_code_map: dict[str, str], name: object) -> str:
    canonical_name, expected_code = canonicalize_stock_identity(name)
    for key in _stock_name_lookup_keys(canonical_name):
        local_code = stock_code_map.get(key)
        if local_code:
            if expected_code and local_code != expected_code:
                logging.error(
                    '[TDX股票主数据] 规范映射冲突：%s 期望 %s，实际 %s',
                    canonical_name,
                    expected_code,
                    local_code,
                )
                return ''
            return local_code
    return ''


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


def _iter_infoharbor_company_aliases(path: Path) -> Iterator[tuple[str, str]]:
    text = _read_text_file(path)
    for line in text.splitlines():
        fields = [field.strip() for field in line.split('|')]
        if len(fields) < 3:
            continue
        code, name = fields[0], fields[1]
        if not re.fullmatch(r'\d{6}', code) or not name:
            continue
        for alias in _iter_infoharbor_aliases(name, fields[2]):
            yield alias, code


def _add_unique_alias(stock_code_map: dict[str, str], alias: str, code: str) -> None:
    keys = {alias, normalize_stock_name(alias)}
    for key in keys:
        if not key:
            continue
        existing_code = stock_code_map.get(key)
        if existing_code and existing_code != code:
            return
    _add_stock_code(stock_code_map, alias, code)


@lru_cache(maxsize=4)
def load_tdx_stock_code_map(
    tdx_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    hq_cache = resolve_tdx_root(tdx_root) / 'T0002' / 'hq_cache'
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

            alias_codes: dict[str, set[str]] = {}
            for alias, code in _iter_infoharbor_company_aliases(full_name_path):
                if code in stock_codes:
                    alias_codes.setdefault(alias, set()).add(code)
            for alias, codes in alias_codes.items():
                if len(codes) == 1:
                    _add_unique_alias(stock_code_map, alias, next(iter(codes)))
        except OSError as exc:
            logging.warning(f'[TDX股票主数据] 读取失败 {full_name_path}: {exc}')

    return stock_code_map
