"""Tests for local Tongdaxin stock universe parsing."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PureWindowsPath

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p8_extractors import find_tdx_vipdoc
from tdx_stock_universe import (
    DEFAULT_TDX_ROOT,
    canonicalize_stock_aliases_in_text,
    canonicalize_stock_identity,
    filter_rejected_stock_tree,
    filter_rejected_stock_lines,
    load_tdx_stock_code_map,
    lookup_tdx_stock_code,
    normalize_rejected_stock_names,
    remove_rejected_stock_mentions,
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


def test_honghe_alias_maps_to_official_short_name_and_code_without_touching_legal_name():
    assert canonicalize_stock_identity('宏和电子') == ('宏和科技', '603256')
    assert canonicalize_stock_identity('宏和电子材料科技股份有限公司') == (
        '宏和电子材料科技股份有限公司',
        '',
    )

    normalized, rewrite_count = canonicalize_stock_aliases_in_text(
        '宏和电子（002011）已量产；宏和电子供给弹性；公司全称为宏和电子材料科技股份有限公司。'
    )

    assert normalized == (
        '宏和科技（603256）已量产；宏和科技供给弹性；公司全称为宏和电子材料科技股份有限公司。'
    )
    assert rewrite_count == 2
    assert lookup_tdx_stock_code({'宏和科技': '603256'}, '宏和电子') == '603256'


def test_rejected_identity_names_keep_audit_labels_and_scalar_is_atomic():
    assert normalize_rejected_stock_names(['AI', '中国', '电子', '虚构电子']) == [
        'AI',
        '中国',
        '电子',
        '虚构电子',
    ]
    assert normalize_rejected_stock_names('虚构电子') == ['虚构电子']
    assert normalize_rejected_stock_names('宏和电子') == []


def test_generic_rejected_labels_do_not_erase_normal_industry_prose():
    text = 'AI服务器需求上升\n中国巨石供给弹性明确\n虚构电子（002011）身份错误'

    filtered, removed = filter_rejected_stock_lines(text, ['中', 'AI', '中国', '虚构电子'])

    assert removed == 1
    assert 'AI服务器需求上升' in filtered
    assert '中国巨石供给弹性明确' in filtered
    assert '虚构电子' not in filtered


def test_ascii_rejected_identity_removal_is_case_insensitive():
    filtered, removed = remove_rejected_stock_mentions(
        'catl 与中国巨石供给对比',
        ['CATL'],
    )

    assert removed == ['CATL']
    assert filtered == '与中国巨石供给对比'


def test_rejected_identity_tree_keeps_valid_nested_evidence():
    payload = {
        'topics': [
            {
                'topic': '虚构电子/中国巨石供给弹性',
                'findings': ['虚构电子身份错误', '中国巨石产能有效'],
            }
        ]
    }

    filtered, removed = filter_rejected_stock_tree(payload, ['虚构电子'])

    assert removed == 2
    assert filtered['topics'][0]['topic'] == '中国巨石供给弹性'
    assert filtered['topics'][0]['findings'] == ['中国巨石产能有效']


def test_rejected_identity_line_filter_keeps_valid_company_clauses():
    text = (
        '- A股进展：虚构电子（002011）已量产，国际复材已获认证；中国巨石产能有效。\n'
        'DEEP_RESEARCH: 中国巨石/虚构电子/国际复材供给弹性'
    )

    filtered, removed = filter_rejected_stock_lines(text, ['虚构电子'])

    assert removed == 2
    assert '虚构电子' not in filtered
    assert '国际复材已获认证' in filtered
    assert '中国巨石产能有效' in filtered
    assert '中国巨石' in filtered
    assert '国际复材供给弹性' in filtered


def test_short_rejected_alias_does_not_erase_longer_valid_company_name():
    filtered, removed = filter_rejected_stock_lines(
        '宏和科技（603256）是合法公司；宏和（002011）身份错误。',
        ['宏和'],
    )

    assert removed == 1
    assert '宏和科技（603256）是合法公司' in filtered
    assert '宏和（002011）' not in filtered
