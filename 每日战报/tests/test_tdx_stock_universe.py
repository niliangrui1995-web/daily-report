"""Tests for local Tongdaxin stock universe parsing."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PureWindowsPath

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p8_extractors import find_tdx_vipdoc
from tdx_stock_universe import (
    DEFAULT_TDX_ROOT,
    load_tdx_stock_code_map,
    lookup_tdx_stock_code,
    resolve_tdx_root,
)

TNF_HEADER_SIZE = 50
TNF_RECORD_SIZE = 360
TNF_CODE_OFFSET = 0
TNF_NAME_OFFSET = 31


def test_default_tdx_root_points_to_huatai_data_source():
    assert PureWindowsPath(DEFAULT_TDX_ROOT) == PureWindowsPath(r'D:\HT')


def test_resolve_tdx_root_honors_environment_override(monkeypatch, tmp_path):
    override_root = tmp_path / 'tdx-override'
    monkeypatch.setenv('TDX_ROOT', str(override_root))

    assert resolve_tdx_root() == override_root


def test_find_tdx_vipdoc_uses_configured_root(monkeypatch, tmp_path):
    override_root = tmp_path / 'tdx-override'
    vipdoc = override_root / 'vipdoc'
    vipdoc.mkdir(parents=True)
    monkeypatch.setenv('TDX_ROOT', str(override_root))

    assert Path(find_tdx_vipdoc()) == vipdoc


def _write_fake_tnf(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(b'\0' * TNF_HEADER_SIZE)
    for code, name in records:
        record = bytearray(b'\0' * TNF_RECORD_SIZE)
        record[TNF_CODE_OFFSET:TNF_CODE_OFFSET + 6] = code.encode('ascii')
        name_bytes = name.encode('gbk')
        record[TNF_NAME_OFFSET:TNF_NAME_OFFSET + len(name_bytes)] = name_bytes
        data.extend(record)
    path.write_bytes(data)


def test_load_tdx_stock_code_map_reads_local_sh_sz_bj_tables(tmp_path):
    hq_cache = tmp_path / 'T0002' / 'hq_cache'
    _write_fake_tnf(
        hq_cache / 'shs.tnf',
        [
            ('000001', '上证指数'),
            ('688525', 'XD佰维存'),
            ('688700', '东威科技'),
        ],
    )
    _write_fake_tnf(
        hq_cache / 'szs.tnf',
        [
            ('399001', '深证成指'),
            ('301200', '大族数控'),
            ('301308', '江波龙'),
        ],
    )
    _write_fake_tnf(hq_cache / 'bjs.tnf', [('920001', '北证测试')])
    (hq_cache / 'infoharbor_ex.code').write_text(
        '688525|佰维存储|孙成思,何瀚\n'
        '000001|上证指数|\n',
        encoding='gbk',
    )

    stock_code_map = load_tdx_stock_code_map(str(tmp_path))

    assert stock_code_map['佰维存储'] == '688525'
    assert stock_code_map['XD佰维存'] == '688525'
    assert stock_code_map['东威科技'] == '688700'
    assert stock_code_map['大族数控'] == '301200'
    assert stock_code_map['江波龙'] == '301308'
    assert stock_code_map['北证测试'] == '920001'
    assert '上证指数' not in stock_code_map
    assert '深证成指' not in stock_code_map


def test_lookup_tdx_stock_code_falls_back_to_company_aliases(tmp_path):
    hq_cache = tmp_path / 'T0002' / 'hq_cache'
    _write_fake_tnf(
        hq_cache / 'shs.tnf',
        [
            ('600520', '三佳科技'),
        ],
    )
    _write_fake_tnf(
        hq_cache / 'szs.tnf',
        [
            ('300548', '长芯博创'),
        ],
    )
    _write_fake_tnf(hq_cache / 'bjs.tnf', [])
    (hq_cache / 'infoharbor_ex.code').write_text(
        '600520|三佳科技|文一三佳,裴晓辉\n'
        '300548|长芯博创|博创股份,庄丹\n',
        encoding='gbk',
    )

    stock_code_map = load_tdx_stock_code_map(str(tmp_path))

    assert stock_code_map['文一三佳'] == '600520'
    assert stock_code_map['博创股份'] == '300548'
    assert '裴晓辉' not in stock_code_map
    assert '庄丹' not in stock_code_map
    assert lookup_tdx_stock_code(stock_code_map, '文一科技') == '600520'
    assert lookup_tdx_stock_code(stock_code_map, '博创科技') == '300548'
