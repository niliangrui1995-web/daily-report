# -*- coding: utf-8 -*-
"""单元测试 — 核心逻辑离线验证（不联网、不花钱）。

运行方式：cd 每日战报/每日战报 && python -m pytest tests/ -v
"""

import sys
import os

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta, timezone


# =====================================================================
# 1. 交易日历测试
# =====================================================================

class TestTradingCalendar:
    """测试 A 股交易日历判断"""

    def test_normal_weekday(self):
        """普通工作日应为交易日"""
        from trading_calendar import is_trading_day
        # 2026-03-02 是周一
        assert is_trading_day(date(2026, 3, 2)) is True

    def test_weekend(self):
        """普通周末应为非交易日"""
        from trading_calendar import is_trading_day
        # 2024-03-09 是周六
        assert is_trading_day(date(2024, 3, 9)) is False

    def test_holiday(self):
        """法定假日应为非交易日"""
        from trading_calendar import is_trading_day
        # 2024-01-01 元旦
        assert is_trading_day(date(2024, 1, 1)) is False

    def test_saturday_workday(self):
        """周六调休上班日应为非交易日（A股周末雷打不动不开盘）"""
        from trading_calendar import is_trading_day
        # 2024-02-18 是周日但调休上班
        assert is_trading_day(date(2024, 2, 18)) is False

    def test_spring_festival(self):
        """春节连休应为非交易日"""
        from trading_calendar import is_trading_day
        # Why: 2024春节假期包含 2/12-2/16 （工作日，但法定休市）
        for day in [12, 13, 14, 15, 16]:
            assert is_trading_day(date(2024, 2, day)) is False, (
                f"2024-02-{day} 春节期间不应为交易日"
            )


# =====================================================================
# 2. 产业链字典测试
# =====================================================================

class TestIndustryDict:
    """测试产业链字典完整性"""

    def test_oligarch_sectors(self):
        """应有 15 个赛道"""
        from industry_dict import OLIGARCH_DICT
        assert len(OLIGARCH_DICT) == 15

    def test_oligarch_flat(self):
        """展平后应有 100+ 家公司"""
        from industry_dict import ALL_OLIGARCHS_FLAT
        assert len(ALL_OLIGARCHS_FLAT) > 100

    def test_vanguard_tickers(self):
        """美股标的池应有 50+ 只"""
        from industry_dict import VANGUARD_TICKERS
        assert len(VANGUARD_TICKERS) >= 50
        # NVIDIA 必须在
        assert "NVIDIA" in VANGUARD_TICKERS
        assert VANGUARD_TICKERS["NVIDIA"] == "NVDA"

    def test_super_giants(self):
        """超级巨头名单应包含核心标的"""
        from industry_dict import SUPER_GIANTS
        for name in ["NVIDIA", "TSMC", "Apple", "Microsoft"]:
            assert name in SUPER_GIANTS

    def test_classify_giant_surge(self):
        """超级巨头 +5% 应触发大涨催化"""
        from industry_dict import classify_vanguard_signal
        assert "大涨" in classify_vanguard_signal("NVIDIA", 5.0)

    def test_classify_giant_crash(self):
        """超级巨头 -6% 应触发暴跌警惕"""
        from industry_dict import classify_vanguard_signal
        assert "暴跌" in classify_vanguard_signal("NVIDIA", -6.0)

    def test_classify_midcap_normal(self):
        """中型标的 +3% 应为正常"""
        from industry_dict import classify_vanguard_signal
        assert "正常" in classify_vanguard_signal("Credo", 3.0)

    def test_classify_midcap_surge(self):
        """中型标的 +5% 应触发大涨催化"""
        from industry_dict import classify_vanguard_signal
        assert "大涨" in classify_vanguard_signal("Credo", 5.0)


# =====================================================================
# 3. P8 股票代码提取测试
# =====================================================================

class TestStockExtraction:
    """测试从 P6 映射中提取 A 股代码"""

    def test_basic_extract(self):
        """基本格式：公司名（代码）"""
        from p8_technical import extract_stocks_from_report
        text = "寒武纪（688256）是国产 AI 芯片龙头"
        stocks = extract_stocks_from_report(text)
        assert ("寒武纪", "688256") in stocks

    def test_multiple_stocks(self):
        """多只股票提取"""
        from p8_technical import extract_stocks_from_report
        text = """
        赛道: 算力核心
        - 寒武纪（688256） | 🔥高弹性
        - 海光信息（688041） | ⚡中弹性
        - 景嘉微（300474） | 💀纯概念
        """
        stocks = extract_stocks_from_report(text)
        codes = [code for _, code in stocks]
        assert "688256" in codes
        assert "688041" in codes
        assert "300474" in codes

    def test_dot_format(self):
        """半角中文括号格式：公司名(代码)"""
        from p8_technical import extract_stocks_from_report
        text = "中际旭创(300308) 光模块龙头"
        stocks = extract_stocks_from_report(text)
        codes = [code for _, code in stocks]
        assert "300308" in codes

    def test_no_stocks(self):
        """无股票代码时应返回空列表"""
        from p8_technical import extract_stocks_from_report
        text = "今天天气不错"
        stocks = extract_stocks_from_report(text)
        assert stocks == []

    def test_dedup(self):
        """重复代码应去重"""
        from p8_technical import extract_stocks_from_report
        text = "寒武纪（688256）再次提及寒武纪（688256）"
        stocks = extract_stocks_from_report(text)
        codes = [code for _, code in stocks]
        assert codes.count("688256") == 1


# =====================================================================
# 4. Scrapling 工具测试
# =====================================================================

class TestScraperUtils:
    """测试 URL 过滤逻辑"""

    def test_normal_url(self):
        """正常文章 URL 应通过"""
        from scraper_utils import is_article_url
        assert is_article_url("https://example.com/news/article-1") is True

    def test_empty_url(self):
        """空 URL 应通过（由调用方处理）"""
        from scraper_utils import is_article_url
        assert is_article_url("") is True


class TestP1WebTimeWindow:
    """测试 P1 Web 的时间窗筛选逻辑"""

    def test_normalize_web_datetime_date_only(self):
        """纯日期字符串应归一化到北京时间当天 23:59:59"""
        from p1_web import _normalize_web_datetime
        assert _normalize_web_datetime("2026/04/10") == "2026-04-10T23:59:59 (北京时间)"

    def test_extract_web_published_at_from_url(self):
        """列表页抽不到日期时应可从 URL 路径回退提取日期"""
        from p1_web import _extract_web_published_at

        result = _extract_web_published_at(
            page_html="<html><body>no date here</body></html>",
            raw_link="/news/2026/04/10/sample-article",
            link="https://example.com/news/2026/04/10/sample-article",
            title="Sample Article",
        )
        assert result == "2026-04-10T23:59:59 (北京时间)"

    def test_entries_to_p1_result_filters_outdated_items(self):
        """超出时间窗的网页条目不应进入 P1 输出"""
        from p1_web import _entries_to_p1_result

        bj_tz = timezone(timedelta(hours=8))
        recent_time = (datetime.now(bj_tz) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S (北京时间)")
        old_time = (datetime.now(bj_tz) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S (北京时间)")

        result = _entries_to_p1_result(
            [
                {"title": "Recent item", "content": "", "url": "https://example.com/recent", "published_at": recent_time, "source": "Example"},
                {"title": "Old item", "content": "", "url": "https://example.com/old", "published_at": old_time, "source": "Example"},
            ],
            "网页采集",
            "Example",
            24,
        )

        assert result is not None
        assert "Recent item" in result["text"]
        assert "Old item" not in result["text"]
        assert len(result["grounding_sources"]) == 1


# =====================================================================
# 5. 搜索引擎组件测试
# =====================================================================

class TestSearchKeyRotator:
    """测试 Key 轮换器"""

    def test_rotation(self):
        """Key 应按顺序轮换"""
        from search_engine import SearchKeyRotator
        rotator = SearchKeyRotator(["key_A", "key_B", "key_C"], "http://localhost")
        _, _, idx1, _ = rotator.get_client()
        _, _, idx2, _ = rotator.get_client()
        _, _, idx3, _ = rotator.get_client()
        assert idx1 == 0
        assert idx2 == 1
        assert idx3 == 2

    def test_exhaustion(self):
        """标记耗尽后应跳过该 Key"""
        from search_engine import SearchKeyRotator
        rotator = SearchKeyRotator(["key_A", "key_B"], "http://localhost")
        rotator.mark_exhausted(0)
        _, _, idx, _ = rotator.get_client()
        assert idx == 1  # 跳过 0，返回 1

    def test_all_exhausted(self):
        """全部耗尽后应返回 None"""
        from search_engine import SearchKeyRotator
        rotator = SearchKeyRotator(["key_A", "key_B"], "http://localhost")
        rotator.mark_exhausted(0)
        rotator.mark_exhausted(1)
        _, _, idx, _ = rotator.get_client()
        assert idx == -1


class TestCircuitBreaker:
    """测试熔断器"""

    def test_not_tripped(self):
        """初始状态不熔断"""
        from search_engine import ChannelCircuitBreaker
        cb = ChannelCircuitBreaker(threshold=2)
        assert cb.is_tripped("direct") is False

    def test_trip_on_threshold(self):
        """连续失败达到阈值后应熔断"""
        from search_engine import ChannelCircuitBreaker
        cb = ChannelCircuitBreaker(threshold=2)
        cb.record_failure("direct")
        assert cb.is_tripped("direct") is False  # 1 次不触发
        cb.record_failure("direct")
        assert cb.is_tripped("direct") is True   # 2 次触发

    def test_reset_on_success(self):
        """成功后应重置计数"""
        from search_engine import ChannelCircuitBreaker
        cb = ChannelCircuitBreaker(threshold=2)
        cb.record_failure("direct")
        cb.record_success("direct")
        cb.record_failure("direct")
        assert cb.is_tripped("direct") is False  # 中间成功过，计数重置





# =====================================================================
# 7. 配置加载测试
# =====================================================================

class TestSettings:
    """测试配置加载（依赖 .env）"""

    def test_env_loaded(self):
        """关键配置应从 .env 加载"""
        from settings import SEARCH_API_KEY_POOL, ANTIGRAVITY_BASE_URL
        assert len(SEARCH_API_KEY_POOL) > 0, "SEARCH_API_KEY_POOL should not be empty"
        assert ANTIGRAVITY_BASE_URL.startswith("http")

    def test_adapters_initialized(self):
        """LLM 适配器应正确初始化"""
        from settings import pro_adapter, opus_adapter
        assert pro_adapter.model == "gemini-3.1-pro-high"
        assert opus_adapter.model == "claude-opus-4-6-thinking"

    def test_output_dir_exists(self):
        """输出目录应存在"""
        from settings import OUTPUT_DIR
        assert os.path.isdir(OUTPUT_DIR)

    def test_require_prompt_missing_raises(self):
        """关键 Prompt 缺失时应显式抛错，禁止静默返回空串"""
        import pytest
        from settings import _prompt_cache, require_prompt

        missing_name = 'definitely_missing_prompt_for_test.txt'
        _prompt_cache.pop(missing_name, None)

        with pytest.raises(FileNotFoundError):
            require_prompt(missing_name)


# =====================================================================
# 8. P7 查询门控测试
# =====================================================================

class TestP7QueryGuard:
    """测试 P7 细化查询质量门控"""

    def test_invalid_initiating_query_fallback(self):
        """无效占位语应回退原主题"""
        from p7_deep_research import _pick_refined_query
        query, used_fallback = _pick_refined_query("**Initiating the Analysis**", "fallback-topic")
        assert used_fallback is True
        assert query == "fallback-topic"

    def test_valid_query_pass(self):
        """有效查询应被放行"""
        from p7_deep_research import _pick_refined_query
        query, used_fallback = _pick_refined_query(
            "HBM pricing pass-through to substrate suppliers in 2026Q2",
            "fallback-topic"
        )
        assert used_fallback is False
        assert "HBM pricing" in query


# =====================================================================
# 9. P8 市场路由测试
# =====================================================================

class TestP8MarketRouting:
    """测试 P8 通达信市场路径路由"""

    def test_bj_code_prefers_bj_file(self, tmp_path):
        """北交所代码在 bj 文件存在时应优先命中 bj"""
        from p8_technical import _resolve_tdx_market_and_path

        vipdoc = tmp_path
        bj_file = vipdoc / "bj" / "lday" / "bj831137.day"
        bj_file.parent.mkdir(parents=True)
        bj_file.write_bytes(b"")

        market, path = _resolve_tdx_market_and_path(str(vipdoc), "831137")
        assert market == "bj"
        assert path.endswith(os.path.join("bj", "lday", "bj831137.day"))

    def test_bj_code_fallback_to_sz(self, tmp_path):
        """bj 缺失时应可回退命中 sz（兼容历史目录）"""
        from p8_technical import _resolve_tdx_market_and_path

        vipdoc = tmp_path
        sz_file = vipdoc / "sz" / "lday" / "sz831137.day"
        sz_file.parent.mkdir(parents=True)
        sz_file.write_bytes(b"")

        market, path = _resolve_tdx_market_and_path(str(vipdoc), "831137")
        assert market == "sz"
        assert path.endswith(os.path.join("sz", "lday", "sz831137.day"))


# =====================================================================
# 10. JSON 提取工具测试
# =====================================================================

class TestExtractJson:
    """测试从大模型返回中鲁棒提取 JSON"""

    def test_clean_json(self):
        """纯净 JSON 字符串应直接解析"""
        from utils import extract_json_from_llm_response
        result = extract_json_from_llm_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_code_block_wrapped(self):
        """```json ``` 代码块包裹的 JSON"""
        from utils import extract_json_from_llm_response
        raw = '这是一段废话\n```json\n{"name": "test"}\n```\n再来一段废话'
        result = extract_json_from_llm_response(raw)
        assert result["name"] == "test"

    def test_garbage_prefix_suffix(self):
        """JSON 前后带废话文本"""
        from utils import extract_json_from_llm_response
        raw = '好的，以下是分析结果：\n{"score": 95}\n希望对你有帮助！'
        result = extract_json_from_llm_response(raw)
        assert result["score"] == 95

    def test_expect_list(self):
        """期望 list 类型"""
        from utils import extract_json_from_llm_response
        raw = '[{"a": 1}, {"b": 2}]'
        result = extract_json_from_llm_response(raw, expect_type='list')
        assert len(result) == 2

    def test_empty_raises(self):
        """空输入应抛异常"""
        import json
        from utils import extract_json_from_llm_response
        import pytest
        with pytest.raises(json.JSONDecodeError):
            extract_json_from_llm_response('')

    def test_no_json_raises(self):
        """纯文本无 JSON 应抛异常"""
        import json
        from utils import extract_json_from_llm_response
        import pytest
        with pytest.raises(json.JSONDecodeError):
            extract_json_from_llm_response('今天天气不错，没有 JSON')


# =====================================================================
# 11. Backtrack 解析工具测试
# =====================================================================

class TestParseBacktrackHours:
    """测试回溯窗口字符串解析"""

    def test_standard_format(self):
        """标准 '24h' 格式"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('24h') == 24

    def test_large_window(self):
        """大窗口 '240h'"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('240h') == 240

    def test_no_suffix(self):
        """纯数字 '72' 无后缀"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('72') == 72

    def test_empty_string(self):
        """空字符串应返回默认值"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('') == 24

    def test_none_string(self):
        """None 输入应返回默认值"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours(None) == 24

    def test_garbage_input(self):
        """乱码输入应返回默认值"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('abc') == 24

    def test_custom_default(self):
        """自定义默认值"""
        from utils import parse_backtrack_hours
        assert parse_backtrack_hours('', default=48) == 48


# =====================================================================
# 12. Checkpoint 断点检测测试
# =====================================================================

class TestCheckpoint:
    """测试断点检测逻辑"""

    def test_empty_dir_returns_none(self, tmp_path, monkeypatch):
        """空目录应返回 stage='none'"""
        monkeypatch.setattr('checkpoint.get_today_output_dir', lambda: str(tmp_path))
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        from checkpoint import detect_checkpoint
        ckpt = detect_checkpoint()
        assert ckpt['stage'] == 'none'

    def test_p1_detected(self, tmp_path, monkeypatch):
        """有 P1 文件应返回 stage='p1'"""
        monkeypatch.setattr('checkpoint.get_today_output_dir', lambda: str(tmp_path))
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        (tmp_path / 'P1_RSS原始数据_202604091000.json').write_text('{}', encoding='utf-8')
        from checkpoint import detect_checkpoint
        ckpt = detect_checkpoint()
        assert ckpt['stage'] == 'p1'

    def test_war_report_done(self, tmp_path, monkeypatch):
        """有战报文件应返回 stage='p9_done'"""
        monkeypatch.setattr('checkpoint.get_today_output_dir', lambda: str(tmp_path))
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        (tmp_path / '战报_202604091200.md').write_text('done', encoding='utf-8')
        from checkpoint import detect_checkpoint
        ckpt = detect_checkpoint()
        assert ckpt['stage'] == 'p9_done'

    def test_p8_detected_as_p9_resume(self, tmp_path, monkeypatch):
        """有 P8 文件时应进入 p9_resume，避免重复重跑技术面"""
        monkeypatch.setattr('checkpoint.get_today_output_dir', lambda: str(tmp_path))
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        (tmp_path / 'P8_技术面_202604091130.md').write_text('p8 data', encoding='utf-8')
        from checkpoint import detect_checkpoint
        ckpt = detect_checkpoint()
        assert ckpt['stage'] == 'p9_resume'


# =====================================================================
# 13. 运行产物管理测试
# =====================================================================

class TestArtifactStore:
    """测试统一产物与 manifest 管理"""

    def test_extract_timestamp_from_name(self):
        """应能从文件名中提取 12/14 位时间戳"""
        from artifact_store import extract_timestamp_from_name
        assert extract_timestamp_from_name('战报_20260410123045.md') == '20260410123045'
        assert extract_timestamp_from_name('P8_技术面_202604101230.md') == '202604101230'

    def test_manifest_roundtrip(self, tmp_path, monkeypatch):
        """manifest 应可写入并读取阶段状态"""
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        from artifact_store import load_run_manifest, update_run_manifest

        update_run_manifest(
            'p8',
            status='completed',
            files={'md': str(tmp_path / 'P8_技术面_20260410123045.md')},
            metrics={'stocks_requested': 5},
            timestamp='20260410123045',
        )
        manifest = load_run_manifest()
        assert manifest['run_timestamp'] == '20260410123045'
        assert manifest['stages']['p8']['status'] == 'completed'
        assert manifest['stages']['p8']['files']['md'].endswith('P8_技术面_20260410123045.md')
