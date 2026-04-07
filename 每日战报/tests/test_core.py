# -*- coding: utf-8 -*-
"""单元测试 — 核心逻辑离线验证（不联网、不花钱）。

运行方式：cd 每日战报/每日战报 && python -m pytest tests/ -v
"""

import sys
import os

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date


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
        # 2026-03-07 是周六
        assert is_trading_day(date(2026, 3, 7)) is False

    def test_holiday(self):
        """法定假日应为非交易日"""
        from trading_calendar import is_trading_day
        # 2026-01-01 元旦
        assert is_trading_day(date(2026, 1, 1)) is False

    def test_saturday_workday(self):
        """周六调休上班日应为非交易日（A股周末雷打不动不开盘）"""
        from trading_calendar import is_trading_day
        # 2026-01-24 是周六但调休上班
        assert is_trading_day(date(2026, 1, 24)) is False

    def test_spring_festival(self):
        """春节连休应为非交易日"""
        from trading_calendar import is_trading_day
        # Why: 2026春节假期 1/26-1/30，这些天确定不开盘，
        #       之前 assert res in [True, False] 等于没测。
        for day in [26, 27, 28, 29, 30]:
            assert is_trading_day(date(2026, 1, day)) is False, (
                f"2026-01-{day} 春节期间不应为交易日"
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
