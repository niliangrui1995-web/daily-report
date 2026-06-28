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

    def test_get_prev_trading_day_supports_multiple_steps(self):
        """支持向前回溯第 N 个交易日"""
        from trading_calendar import get_prev_trading_day
        assert get_prev_trading_day(date(2026, 4, 15), steps=2) == date(2026, 4, 13)

    def test_count_trading_days_between_skips_weekend(self):
        """交易日计数应自动跳过周末与休市日"""
        from trading_calendar import count_trading_days_between
        assert count_trading_days_between(
            date(2026, 4, 17),
            date(2026, 4, 21),
            include_start=False,
            include_end=True,
        ) == 2

    def test_week_trading_day_index_is_available_for_reporting(self):
        """周内交易日序号保留给日志与 manifest 使用"""
        from trading_calendar import get_trading_day_index_in_week

        assert get_trading_day_index_in_week(date(2026, 4, 20)) == 1
        assert get_trading_day_index_in_week(date(2026, 4, 21)) == 2
        assert get_trading_day_index_in_week(date(2026, 4, 22)) == 3
        assert get_trading_day_index_in_week(date(2026, 4, 24)) == 5

    def test_holiday_short_week_starts_from_first_trading_day(self):
        """如果周一休市，则从本周第一个交易日重新开始计数"""
        from trading_calendar import get_trading_day_index_in_week

        assert get_trading_day_index_in_week(date(2024, 10, 8)) == 1
        assert get_trading_day_index_in_week(date(2024, 10, 9)) == 2
        assert get_trading_day_index_in_week(date(2024, 10, 10)) == 3


class TestRunSchedule:
    """测试每个交易日更新与动态窗口"""

    def test_daily_schedule_runs_on_each_trading_day(self):
        import run_config

        decision = run_config.get_schedule_decision(date(2026, 4, 20))

        assert decision["should_run"] is True
        assert decision["trading_day_index_in_week"] == 1
        assert decision["window_anchor_day"] == date(2026, 4, 17)

    def test_daily_schedule_uses_previous_trading_day_as_window_anchor(self):
        import run_config

        decision = run_config.get_schedule_decision(date(2026, 4, 21))

        assert decision["should_run"] is True
        assert decision["trading_day_index_in_week"] == 2
        assert decision["window_anchor_day"] == date(2026, 4, 20)

        decision = run_config.get_schedule_decision(date(2026, 4, 22))
        assert decision["should_run"] is True
        assert decision["trading_day_index_in_week"] == 3
        assert decision["window_anchor_day"] == date(2026, 4, 21)

    def test_run_config_uses_previous_trading_day_as_dynamic_window_anchor(self):
        import run_config

        backtrack = run_config.get_run_config(datetime(2026, 4, 20, 8, 50))
        assert backtrack == "72h"

        backtrack = run_config.get_run_config(datetime(2026, 4, 21, 8, 50))
        assert backtrack == "24h"

        backtrack = run_config.get_run_config(datetime(2026, 4, 22, 8, 50))
        assert backtrack == "24h"

    def test_dragon_boat_resume_caps_auto_backtrack_at_weekend_window(self):
        import run_config

        decision = run_config.get_schedule_decision(date(2026, 6, 22))

        assert decision["should_run"] is True
        assert decision["window_anchor_day"] == date(2026, 6, 18)
        assert run_config.get_run_config(datetime(2026, 6, 22, 8, 30)) == "72h"


# =====================================================================
# 2. 产业链字典测试
# =====================================================================

class TestIndustryDict:
    """测试产业链字典完整性"""

    def test_oligarch_sectors(self):
        """应有 23 个核心细分赛道"""
        from industry_dict import OLIGARCH_DICT
        assert len(OLIGARCH_DICT) == 23

    def test_refined_sector_taxonomy(self):
        """新版寡头字典应拆清核心细分赛道边界"""
        from industry_dict import OLIGARCH_DICT, VANGUARD_TICKERS

        assert "EDA与底层IP" in OLIGARCH_DICT
        assert "AI加速芯片与定制ASIC" in OLIGARCH_DICT
        assert "先进制程代工" in OLIGARCH_DICT
        assert "前道晶圆设备与量测" in OLIGARCH_DICT
        assert "关键晶圆材料与特种工艺" in OLIGARCH_DICT
        assert "数据中心电力与配电" in OLIGARCH_DICT
        assert "液冷与散热材料" in OLIGARCH_DICT
        assert "具身智能与机器人平台" in OLIGARCH_DICT
        assert "光纤光缆与宽带接入" in OLIGARCH_DICT

        assert "Disco (迪思科)" in OLIGARCH_DICT["先进封装与混合键合"]
        assert "Disco (迪思科)" not in OLIGARCH_DICT["半导体测试设备与探针卡"]
        assert "SKC (Absolics)" in OLIGARCH_DICT["IC载板与封装材料"]
        assert "Vistance Networks (原CommScope ANS/RUCKUS)" in OLIGARCH_DICT["光纤光缆与宽带接入"]
        assert "Tower Semiconductor" in OLIGARCH_DICT["先进制程代工"]
        assert VANGUARD_TICKERS["Arteris"] == "AIP"
        assert VANGUARD_TICKERS["Tower Semiconductor"] == "TSEM"
        assert VANGUARD_TICKERS["SKC"] == "011790.KS"
        assert VANGUARD_TICKERS["TE Connectivity"] == "TEL"
        assert VANGUARD_TICKERS["Vistance Networks"] == "VISN"
        assert VANGUARD_TICKERS["Arista Networks"] == "ANET"
        assert VANGUARD_TICKERS["Shin-Etsu"] == "4063.T"
        assert VANGUARD_TICKERS["SUMCO"] == "3436.T"
        assert VANGUARD_TICKERS["Accretech"] == "7729.T"
        assert "Murata (村田/MLCC)" in OLIGARCH_DICT["数据中心电力与配电"]
        assert VANGUARD_TICKERS["Murata"] == "6981.T"

    def test_optical_chain_is_split_by_value_source(self):
        """光通信链条应拆清模块、光芯片、电芯片和无源器件"""
        from industry_dict import OLIGARCH_DICT, VANGUARD_TICKERS

        assert "光模块/硅光/CPO" not in OLIGARCH_DICT
        assert "光模块与光引擎" in OLIGARCH_DICT
        assert "光芯片与硅光" in OLIGARCH_DICT
        assert "光模块电芯片" in OLIGARCH_DICT
        assert "光通信无源器件与精密零部件" in OLIGARCH_DICT

        assert "Coherent (高意)" in OLIGARCH_DICT["光模块与光引擎"]
        assert "Lumentum (朗明特)" in OLIGARCH_DICT["光芯片与硅光"]
        assert "Semtech" in OLIGARCH_DICT["光模块电芯片"]
        assert "US Conec" in OLIGARCH_DICT["光通信无源器件与精密零部件"]
        assert VANGUARD_TICKERS["Semtech"] == "SMTC"
        assert VANGUARD_TICKERS["MACOM"] == "MTSI"
        assert VANGUARD_TICKERS["MaxLinear"] == "MXL"
        assert VANGUARD_TICKERS["Fujikura"] == "5803.T"

    def test_mainland_china_companies_are_excluded_from_oligarch_dict(self):
        """寡头字典排除中国大陆公司，但保留台湾公司"""
        from industry_dict import OLIGARCH_DICT, VANGUARD_TICKERS

        flattened = "\n".join(OLIGARCH_DICT) + "\n" + "\n".join(
            company for companies in OLIGARCH_DICT.values() for company in companies
        )
        for forbidden in ["中国半导体国产替代", "SMIC", "Huawei", "NAURA", "AMEC", "Innolight", "Eoptolink"]:
            assert forbidden not in flattened
            assert forbidden not in VANGUARD_TICKERS
        assert "TSMC" in VANGUARD_TICKERS
        assert VANGUARD_TICKERS["Alchip"] == "3661.TW"
        assert not any(ticker.endswith((".SZ", ".SS")) for ticker in VANGUARD_TICKERS.values())

    def test_public_listed_oligarchs_have_p4_tickers(self):
        """已上市寡头必须进入 P4 核心行情池，供 Opus 推理使用"""
        from industry_dict import VANGUARD_TICKERS

        expected = {
            "Arteris": "AIP",
            "MJC": "6871.T",
            "SKC": "011790.KS",
            "Murata": "6981.T",
        }
        for name, ticker in expected.items():
            assert VANGUARD_TICKERS[name] == ticker

    def test_ai_pcb_equipment_sector(self):
        """AI PCB 设备赛道应纳入真实海外设备/耗材寡头"""
        from industry_dict import OLIGARCH_DICT, VANGUARD_TICKERS

        sector = OLIGARCH_DICT["AI PCB设备与关键耗材"]
        required_tickers = {
            "KLA": "KLAC",
            "MKS Instruments": "MKSI",
            "SCREEN Holdings": "7735.T",
            "Nidec": "6594.T",
            "Mycronic": "MYCR.ST",
            "AMADA": "6113.T",
            "SCHMID Group": "SHMD",
            "Union Tool": "6278.T",
            "Ushio": "6925.T",
        }

        for name, ticker in required_tickers.items():
            assert any(name in company for company in sector)
            assert VANGUARD_TICKERS[name] == ticker

    def test_ai_pcb_equipment_integrates_prompt_and_search_terms(self):
        """AI PCB 设备赛道应进入主动检索词和各层 Prompt 口径"""
        import p2_x_intel
        import p3_agent

        p2_keywords = " ".join(p2_x_intel.INDUSTRY_SCAN_KEYWORDS).lower()
        for keyword in [
            "ldi",
            "laser drilling",
            "vcp",
            "aoi",
            "electrical test",
            "drill bits",
            "atotech",
            "esi",
            "orbotech",
        ]:
            assert keyword in p2_keywords

        p3_keywords = {keyword.lower() for keyword in p3_agent.PRIORITY_KEYWORDS}
        for keyword in [
            "ldi",
            "laser drilling",
            "vcp",
            "aoi",
            "electrical test",
            "drill bit",
            "atotech",
            "esi",
            "orbotech",
        ]:
            assert keyword in p3_keywords

        prompt_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
        for prompt_name in [
            "audit.txt",
            "p3_jinnang1.txt",
            "p3_jinnang2.txt",
            "p3_jinnang3.txt",
            "p6_mapping.txt",
            "p9_report.txt",
        ]:
            prompt_text = open(os.path.join(prompt_dir, prompt_name), encoding="utf-8").read()
            assert "AI PCB设备与关键耗材" in prompt_text

    def test_ai_pcb_equipment_a_share_code_guardrail(self):
        r"""P6 不应再维护模型纠错表，股票代码统一来自 D:\HT。"""
        import p6_mapper

        assert not hasattr(p6_mapper, "HALLUCINATION_MAP")
        assert not hasattr(p6_mapper, "CANONICAL_A_SHARE_CODE_MAP")

    def test_ai_pcb_equipment_asian_kline_track_lookup(self):
        """亚洲寡头行情脚本应能把新增亚洲标的反查到 AI PCB 设备赛道"""
        import importlib.util

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script_path = os.path.join(project_root, "亚洲寡头行情", "asian_kline_fetcher.py")
        spec = importlib.util.spec_from_file_location("asian_kline_fetcher_test", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        for ticker in ["7735.T", "6113.T", "6278.T", "6925.T", "6594.T"]:
            assert module._find_track(ticker) == "AI PCB设备与关键耗材"
        assert module._find_track("6981.T") == "数据中心电力与配电"

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
        for name in ["NVIDIA", "TSMC", "Apple", "Microsoft", "Samsung", "ASML"]:
            assert name in SUPER_GIANTS

    def test_strategic_giants_are_second_tier_watchlist(self):
        """战略核心观察池不应享受超级巨头低阈值"""
        from industry_dict import STRATEGIC_GIANTS, SUPER_GIANTS, VANGUARD_TICKERS
        expected = {
            "SK Hynix",
            "Micron",
            "AMD",
            "Oracle",
            "Intel",
            "Applied Materials",
            "Lam Research",
            "KLA",
            "ARM",
            "Arista Networks",
            "Qualcomm",
            "TE Connectivity",
            "Marvell",
        }
        assert expected <= STRATEGIC_GIANTS
        assert STRATEGIC_GIANTS.isdisjoint(SUPER_GIANTS)
        assert all(name in VANGUARD_TICKERS for name in STRATEGIC_GIANTS)

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

    def test_classify_strategic_giant_keeps_normal_threshold(self):
        """战略核心 +3% 仍按常规阈值处理"""
        from industry_dict import classify_vanguard_signal
        assert "正常" in classify_vanguard_signal("AMD", 3.0)

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


class TestP8FastPath:
    """测试 P8 简版技术画像仍保留 P9 表格所需字段"""

    def test_fast_path_keeps_3m_and_250d_fields(self, monkeypatch, tmp_path):
        import p8_technical

        captured = {}

        def make_records(count, start):
            return [
                {
                    "date": f"2026{i + 1:04d}",
                    "open": start + i,
                    "high": start + i + 1,
                    "low": start + i - 1,
                    "close": start + i,
                    "amount": 0,
                    "volume": 1000 + i,
                }
                for i in range(count)
            ]

        stock_records = make_records(80, 100)
        index_records = make_records(80, 3000)

        def fake_read_day_file(path):
            if "999999" in path or "399006" in path:
                return index_records
            return stock_records

        def fake_persist(markdown_text, json_data, *, status, metrics):
            captured["markdown"] = markdown_text
            captured["json"] = json_data
            captured["status"] = status
            captured["metrics"] = metrics
            return markdown_text

        monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: str(tmp_path))
        monkeypatch.setattr(p8_technical, "_resolve_tdx_market_and_path", lambda vipdoc, code: ("sh", str(tmp_path / f"{code}.day")))
        monkeypatch.setattr(p8_technical.os.path, "exists", lambda path: True)
        monkeypatch.setattr(p8_technical, "read_day_file_simple", fake_read_day_file)
        monkeypatch.setattr(p8_technical, "_persist_p8_outputs", fake_persist)

        p8_technical.p8_search_stock_prices(
            stocks=[("中微公司", "688012")],
            track_map={"688012": "前道设备"},
            elasticity_map={"688012": "⚡中弹性"},
        )

        item = captured["json"]["688012"]
        assert captured["status"] == "completed"
        assert item["fast_path"] is True
        assert "60日" in captured["markdown"]
        assert item["chg_3m_pct"] == item["price_data"]["chg_3m_pct"]
        assert item["percentile_250d"] == item["price_data"]["percentile_250d"]


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

    def test_web_source_config_replaces_persistent_fierce_403(self):
        from config import WEB_SCRAPE_SOURCES

        sources_by_name = {source["name"]: source for source in WEB_SCRAPE_SOURCES}
        assert "FierceTelecom" not in sources_by_name
        assert "TotalTelecom" in sources_by_name
        assert "entry-title" in sources_by_name["TotalTelecom"]["selector"]

    def test_low_yield_web_sources_are_disabled_but_kept_for_recheck(self):
        from config import DISABLED_WEB_SCRAPE_SOURCES, WEB_SCRAPE_SOURCES
        from p1_web import SOURCE_TIMEOUT

        active_sources = {source["name"] for source in WEB_SCRAPE_SOURCES}
        disabled_by_name = {source["name"]: source for source in DISABLED_WEB_SCRAPE_SOURCES}

        assert "DIGITIMES_TW" not in active_sources
        assert "Lightwave" not in active_sources
        assert disabled_by_name["DIGITIMES_TW"]["timeout"] < SOURCE_TIMEOUT
        assert disabled_by_name["DIGITIMES_TW"]["fallback_urls"] == ["https://www.digitimes.com/tech/"]
        assert disabled_by_name["Lightwave"]["url"] == "https://www.lightwaveonline.com/home"

    def test_reddit_amd_is_disabled_but_kept_for_recheck(self):
        from config import DISABLED_REDDIT_SUBS, REDDIT_SUBS

        disabled_by_name = {source["name"]: source for source in DISABLED_REDDIT_SUBS}
        assert REDDIT_SUBS == ["hardware", "nvidia"]
        assert disabled_by_name["Reddit/r/amd"]["sub"] == "amd"

    def test_scrape_one_source_uses_fallback_url_after_timeout(self, monkeypatch):
        import p1_web

        calls = []

        class FakeResponse:
            text = '<html><body><a href="/news/a">Fallback article</a></body></html>'

            def raise_for_status(self):
                return None

        class FakeElement:
            text = "Fallback article"
            attrib = {"href": "/news/a"}

        class FakeSelector:
            def __init__(self, html):
                self.html = html

            def css(self, selector):
                return [FakeElement()] if selector == "a.story" else []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs.get("timeout")))
            if url == "https://slow.example/primary":
                raise p1_web.requests.exceptions.Timeout()
            return FakeResponse()

        monkeypatch.setattr(p1_web, "Selector", FakeSelector, raising=False)
        monkeypatch.setattr(p1_web.requests, "get", fake_get)
        monkeypatch.setattr(
            p1_web,
            "get_tls_policy_for_url",
            lambda url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
        )
        monkeypatch.setattr(p1_web, "log_tls_policy", lambda *args, **kwargs: None)

        entries = p1_web._scrape_one_source(
            {
                "name": "DIGITIMES_TW",
                "url": "https://slow.example/primary",
                "fallback_urls": ["https://fast.example/fallback"],
                "selector": "a.story",
                "title_attr": "text",
                "link_attr": "href",
                "timeout": 8,
            }
        )

        assert calls == [
            ("https://slow.example/primary", 8),
            ("https://fast.example/fallback", 8),
        ]
        assert len(entries) == 1
        assert entries[0]["url"] == "https://fast.example/news/a"

    def test_scrape_one_source_skips_known_asml_navigation_pages(self, monkeypatch):
        import p1_web

        class FakeResponse:
            text = "<html><body>ASML news links</body></html>"

            def raise_for_status(self):
                return None

        class FakeElement:
            def __init__(self, text, href):
                self.text = text
                self.attrib = {"href": href}

        class FakeSelector:
            def __init__(self, html):
                self.html = html

            def css(self, selector):
                return [
                    FakeElement("Stories", "/en/news/stories"),
                    FakeElement("Share buyback", "/en/news/share-buyback"),
                    FakeElement("Media library", "/en/news/media-library"),
                    FakeElement("Real ASML article", "/en/news/stories/2026/real-asml-article"),
                ]

        monkeypatch.setattr(p1_web, "Selector", FakeSelector, raising=False)
        monkeypatch.setattr(p1_web.requests, "get", lambda *args, **kwargs: FakeResponse())
        monkeypatch.setattr(
            p1_web,
            "get_tls_policy_for_url",
            lambda url: {"verify": True, "tls_mode": "strict", "insecure_fetch": False},
        )
        monkeypatch.setattr(p1_web, "log_tls_policy", lambda *args, **kwargs: None)

        entries = p1_web._scrape_one_source(
            {
                "name": "ASML_News",
                "url": "https://www.asml.com/en/news",
                "selector": "a[href*='/news/']",
                "title_attr": "text",
                "link_attr": "href",
            }
        )

        assert len(entries) == 1
        assert entries[0]["title"] == "Real ASML article"
        assert entries[0]["url"] == "https://www.asml.com/en/news/stories/2026/real-asml-article"

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

    def test_extract_web_published_at_from_compact_url_date(self):
        """TrendForce 紧凑日期 URL 应进入近时效判定，而不是落成 unknown_time"""
        from p1_web import _extract_web_published_at

        result = _extract_web_published_at(
            page_html="<html><body>no date here</body></html>",
            raw_link="/presscenter/news/20260625-13121.html",
            link="https://www.trendforce.com/presscenter/news/20260625-13121.html",
            title="NVIDIA 800V HVDC Power Rack",
        )
        assert result == "2026-06-25T23:59:59 (北京时间)"

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

    def test_entries_to_p1_result_filters_future_and_invalid_times(self):
        from p1_web import _entries_to_p1_result

        bj_tz = timezone(timedelta(hours=8))
        recent_time = (datetime.now(bj_tz) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S (北京时间)")
        future_time = (datetime.now(bj_tz) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S (北京时间)")

        result = _entries_to_p1_result(
            [
                {"title": "Recent item", "content": "", "url": "https://example.com/recent", "published_at": recent_time, "source": "Example"},
                {"title": "Future item", "content": "", "url": "https://example.com/future", "published_at": future_time, "source": "Example"},
                {"title": "Invalid year item", "content": "", "url": "https://example.com/invalid", "published_at": "6856-01-12T23:59:59 (北京时间)", "source": "Example"},
            ],
            "网页采集",
            "Example",
            24,
        )

        assert result is not None
        assert "Recent item" in result["text"]
        assert "Future item" not in result["text"]
        assert "Invalid year item" not in result["text"]
        assert result["time_window_stats"]["future"] == 1
        assert result["time_window_stats"]["invalid_time"] == 1

    def test_scheduled_holiday_resume_does_not_reopen_old_web_items(self, monkeypatch):
        import p1_web
        import run_config
        from p1_web import _entries_to_p1_result

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed_now = datetime(2026, 6, 22, 8, 30, tzinfo=timezone(timedelta(hours=8)))
                if tz is None:
                    return fixed_now.replace(tzinfo=None)
                return fixed_now.astimezone(tz)

        monkeypatch.setattr(p1_web, "datetime", FrozenDateTime)
        hours = int(run_config.get_run_config(datetime(2026, 6, 22, 8, 30)).rstrip("h"))

        result = _entries_to_p1_result(
            [
                {"title": "Old June 18 item", "content": "", "url": "https://example.com/old", "published_at": "2026-06-18T10:00:00 (北京时间)", "source": "Example"},
                {"title": "Recent June 22 item", "content": "", "url": "https://example.com/recent", "published_at": "2026-06-22T07:00:00 (北京时间)", "source": "Example"},
                {"title": "Future item", "content": "", "url": "https://example.com/future", "published_at": "2026-06-24T08:30:00 (北京时间)", "source": "Example"},
                {"title": "Invalid year item", "content": "", "url": "https://example.com/invalid", "published_at": "6856-01-12T23:59:59 (北京时间)", "source": "Example"},
            ],
            "网页采集",
            "Example",
            hours,
        )

        assert result is not None
        assert "Recent June 22 item" in result["text"]
        assert "Old June 18 item" not in result["text"]
        assert "Future item" not in result["text"]
        assert "Invalid year item" not in result["text"]
        assert result["time_window_stats"]["stale"] == 1
        assert result["time_window_stats"]["future"] == 1
        assert result["time_window_stats"]["invalid_time"] == 1


class TestP3JinnangRecovery:
    def test_jinnang_1_filters_low_value_sources_before_keyword_scoring(self):
        from p3_agent import _filter_priority_targets

        targets = _filter_priority_targets(
            [
                {"title": "HBM shortage rumor from AMD forum", "url": "https://example.com/amd", "source": "Reddit/r/amd"},
                {"title": "Apple HBM memory price story", "url": "https://example.com/tc", "source": "TechCrunch"},
                {"title": "NVIDIA HBM shortage deep supply note", "url": "https://example.com/th", "source": "TomsHardware"},
                {"title": "CPO optical module deployment update", "url": "https://example.com/dck", "source": "DataCenterKnowledge"},
            ],
            max_targets=10,
        )

        assert {target["source"] for target in targets} == {"TomsHardware", "DataCenterKnowledge"}

    def test_jinnang_1_default_target_limit_is_tight(self):
        from p3_agent import _filter_priority_targets

        failed_urls = [
            {"title": f"NVIDIA HBM shortage supply note {idx}", "url": f"https://example.com/{idx}", "source": "TomsHardware"}
            for idx in range(8)
        ]

        assert len(_filter_priority_targets(failed_urls)) == 5


class TestP1RssTimeWindow:
    def test_rss_time_window_parses_fierceelectronics_dates(self):
        import p1_rss

        recent = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%b %d, %Y %I:%M%p").replace("AM", "a").replace("PM", "p")
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%b %d, %Y %I:%M%p").replace("AM", "a").replace("PM", "p")

        assert p1_rss._is_within_window(recent, 24) is True
        assert p1_rss._is_within_window(old, 24) is False

    def test_rss_time_window_drops_future_invalid_and_unparseable_times(self):
        import p1_rss

        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S (北京时间)")

        assert p1_rss._is_within_window(future, 24) is False
        assert p1_rss._is_within_window("6856-01-12T23:59:59 (北京时间)", 24) is False
        assert p1_rss._is_within_window("totally-not-a-date", 24) is False

    def test_scheduled_holiday_resume_does_not_reopen_old_rss_items(self, monkeypatch):
        import p1_rss
        import run_config

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed_now = datetime(2026, 6, 22, 8, 30, tzinfo=timezone(timedelta(hours=8)))
                if tz is None:
                    return fixed_now.replace(tzinfo=None)
                return fixed_now.astimezone(tz)

        monkeypatch.setattr(p1_rss, "datetime", FrozenDateTime)
        hours = int(run_config.get_run_config(datetime(2026, 6, 22, 8, 30)).rstrip("h"))

        assert p1_rss._is_within_window("2026-06-18T10:00:00 (北京时间)", hours) is False
        assert p1_rss._is_within_window("2026-06-22T07:00:00 (北京时间)", hours) is True
        assert p1_rss._is_within_window("2026-06-24T08:30:00 (北京时间)", hours) is False
        assert p1_rss._is_within_window("6856-01-12T23:59:59 (北京时间)", hours) is False

    def test_rss_result_normalizes_common_utf8_mojibake(self):
        import p1_rss

        result = p1_rss._entries_to_p1_result(
            [
                {
                    "title": "OpenAI\u00e2\u0080\u0099s release",
                    "content": "supply risks\u00e2\u0080\u0094read now",
                    "url": "https://example.com/openai",
                    "published_at": "",
                    "source": "Example",
                }
            ],
            "RSS",
            "Example",
            24,
        )

        assert result is not None
        assert "OpenAI’s release — supply risks—read now" in result["text"]
        assert "\u00e2\u0080" not in result["text"]
        assert result["grounding_sources"][0]["title"] == "OpenAI’s release"


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
        assert pro_adapter.model == "claude-sonnet-4-6"
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

    def test_partial_json_repair_is_opt_in(self):
        from utils import extract_json_from_llm_response
        import pytest
        import json

        raw = '```json\n{"summary":{"passed":1},"high_trust":["ok"]'
        with pytest.raises(json.JSONDecodeError):
            extract_json_from_llm_response(raw)

        result = extract_json_from_llm_response(raw, allow_partial=True)
        assert result["summary"]["passed"] == 1
        assert result["high_trust"] == ["ok"]

    def test_partial_json_repairs_unescaped_inner_quotes(self):
        from utils import extract_json_from_llm_response

        raw = (
            '{"summary":{"passed":1,"deleted":0},'
            '"high_trust":["劳资双方达成"2026年薪资协议暂定合约"；工会暂停罢工"],'
            '"medium_trust":[],"deleted":[]}'
        )

        result = extract_json_from_llm_response(raw, allow_partial=True)

        assert result["high_trust"][0] == '劳资双方达成"2026年薪资协议暂定合约"；工会暂停罢工'

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


class TestTextMojibakeNormalization:
    def test_common_utf8_punctuation_mojibake_is_repaired_recursively(self):
        from utils import normalize_common_mojibake_artifacts, normalize_common_text_mojibake

        dirty_text = "OpenAI\u00e2\u0080\u0099s release\u00e2\u0080\u0094read now"
        assert normalize_common_text_mojibake(dirty_text) == "OpenAI’s release—read now"

        dirty_payload = {
            "summary": "investor\u00e2\u20ac\u2122s update",
            "items": ["risk\u00e2\u0080\u0093reward", {"body": "wait\u00e2\u0080\u00a6"}],
        }

        assert normalize_common_mojibake_artifacts(dirty_payload) == {
            "summary": "investor’s update",
            "items": ["risk–reward", {"body": "wait…"}],
        }


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

    def test_stale_war_report_resumes_downstream(self, tmp_path, monkeypatch):
        """战报旧于上游 P8 时应恢复生成 P9，而不是直接跳过。"""
        monkeypatch.setattr('checkpoint.get_today_output_dir', lambda: str(tmp_path))
        monkeypatch.setattr('artifact_store.get_today_output_dir', lambda: str(tmp_path))
        report_path = tmp_path / '战报_202604091200.md'
        p8_path = tmp_path / 'P8_技术面_202604091230.md'
        report_path.write_text('old report', encoding='utf-8')
        p8_path.write_text('fresh p8', encoding='utf-8')
        os.utime(report_path, (1000, 1000))
        os.utime(p8_path, (2000, 2000))
        from checkpoint import detect_checkpoint
        ckpt = detect_checkpoint()
        assert ckpt['stage'] == 'p9_resume'

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
