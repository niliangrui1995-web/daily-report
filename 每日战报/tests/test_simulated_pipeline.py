# -*- coding: utf-8 -*-
"""Offline simulation tests for launcher and end-to-end pipeline stitching."""

import json
import os
import struct
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _patch_output_dir(monkeypatch, tmp_path):
    import artifact_store

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(artifact_store, "get_today_output_dir", lambda: str(out_dir))
    return out_dir


def _write_day_file(path: Path, prices: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    start = date(2026, 1, 2)
    with open(path, "wb") as f:
        for idx, close in enumerate(prices):
            day = start + timedelta(days=idx)
            date_int = int(day.strftime("%Y%m%d"))
            open_price = int(round((close * 0.99) * 100))
            high_price = int(round((close * 1.02) * 100))
            low_price = int(round((close * 0.98) * 100))
            close_price = int(round(close * 100))
            amount = float(close * 1_000_000)
            volume = int(close * 10_000)
            f.write(
                struct.pack(
                    "<IIIIIfII",
                    date_int,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    amount,
                    volume,
                    0,
                )
            )


class TestScheduledLauncherSimulation:
    def test_launcher_invokes_pipeline_cli_on_trading_day(self, monkeypatch):
        import scheduled_launcher

        captured = {}

        monkeypatch.setattr(scheduled_launcher, "is_trading_day", lambda d: True)
        monkeypatch.setattr(
            scheduled_launcher.subprocess,
            "run",
            lambda cmd, cwd=None: captured.setdefault(
                "result",
                SimpleNamespace(returncode=0, cmd=cmd, cwd=cwd),
            ),
        )

        try:
            scheduled_launcher.main()
        except SystemExit as exc:
            assert exc.code == 0

        cmd = captured["result"].cmd
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("pipeline_cli.py")
        assert cmd[2] == "run"
        assert captured["result"].cwd.endswith("每日战报")


class TestP8AndP9Simulation:
    def test_p8_search_stock_prices_reads_fake_vipdoc(self, monkeypatch, tmp_path):
        _patch_output_dir(monkeypatch, tmp_path)

        import p8_technical

        vipdoc = tmp_path / "vipdoc"
        _write_day_file(vipdoc / "sh" / "lday" / "sh999999.day", [3000 + i * 2 for i in range(90)])
        _write_day_file(vipdoc / "sz" / "lday" / "sz399006.day", [2200 + i * 3 for i in range(90)])
        _write_day_file(vipdoc / "sh" / "lday" / "sh600183.day", [18 + i * 0.12 for i in range(90)])

        monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: str(vipdoc))

        markdown = p8_technical.p8_search_stock_prices(
            [("生益科技", "600183")],
            {"600183": "覆铜板"},
            {"600183": "高弹性"},
        )

        assert "600183" in markdown
        assert "MACD" in markdown

    def test_p9_reporter_simulation_generates_report_and_md(self, monkeypatch, tmp_path):
        out_dir = _patch_output_dir(monkeypatch, tmp_path)

        import p9_reporter

        monkeypatch.setattr(
            p9_reporter,
            "require_prompt",
            lambda name: "{today_str}\n{p5_evidence}\n{p7_section}\n{p6_mapping}\n{p75_section}\n{price_data}\n{p2_section}\n{p3_section}\n{yfinance_section}",
        )
        monkeypatch.setattr(
            p9_reporter.opus_adapter,
            "invoke",
            lambda prompt: SimpleNamespace(
                content=json.dumps(
                    {
                        "core_signals": [
                            {
                                "source": "P2",
                                "significance": "高优先级",
                                "title": "HBM 交期拉长",
                                "body": "覆铜板和载板同步吃紧。",
                                "confidence": 0.81,
                                "evidence_refs": ["P2-1", "P5-1"],
                            }
                        ],
                        "today_advice": [
                            {
                                "priority": "★★★",
                                "name": "生益科技",
                                "code": "600183",
                                "reason": "覆铜板涨价传导",
                                "strategy": "只做分歧低吸",
                                "confidence": "76%",
                                "evidence_refs": ["P6-1"],
                            }
                        ],
                        "risk_warning": ["海外寡头波动可能导致映射失真"],
                    },
                    ensure_ascii=False,
                ),
                thinking_content="p9 reasoning",
            ),
        )
        monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
        monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
        monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")

        report, run_ts = p9_reporter.p9_risk_analysis(
            p5_evidence="P5 evidence",
            p7_deep="P7 evidence",
            p75_supplement="P7.5 evidence",
            p6_mapping="### 赛道: 覆铜板\n- 生益科技（600183）| 高弹性 | 涨价传导",
            price_data="P8 data",
            p2_x_intel_data="P2 data",
            p3_intel_data="P3 data",
            p4_yfinance="P4 data",
        )

        assert report
        assert "600183" in report
        assert run_ts

        p9_reporter.send_report_email(report, run_ts)
        saved = list(out_dir.rglob("*.md"))
        assert any(path.name.startswith("战报_") for path in saved)


class TestOrchestratorSimulation:
    def test_orchestrator_main_runs_full_pipeline_offline(self, monkeypatch, tmp_path):
        out_dir = _patch_output_dir(monkeypatch, tmp_path)

        import artifact_store
        import orchestrator
        import p9_reporter

        monkeypatch.setattr(orchestrator, "is_trading_day", lambda day: True)
        monkeypatch.setattr(orchestrator, "detect_checkpoint", lambda: {"stage": "none", "files": {}, "run_timestamp": ""})
        monkeypatch.setattr(orchestrator, "get_run_config", lambda now: "24h")
        monkeypatch.setattr(
            orchestrator,
            "collect_from_rss",
            lambda backtrack: (
                [{"category": "rss", "query": "rss", "text": "HBM demand keeps rising"}],
                {"success": ["rss1"], "failed": [], "enrich_ok": 1, "enrich_fail": 0, "failed_urls": []},
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "collect_from_web",
            lambda backtrack: (
                [{"category": "web", "query": "web", "text": "PCB lead time extends"}],
                {"success": ["web1"], "failed": [], "enrich_ok": 1, "enrich_fail": 0},
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "collect_from_x",
            lambda backtrack: (
                "### [HBM] 交期拉长\n- Claim: HBM 基板交期继续拉长\n- Source: @supplychain\n- Confidence: 82% (high)",
                {"status": "success", "duration_seconds": 1.2, "content_length": 120, "structured_items": 1},
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "collect_p3_parallel_only",
            lambda backtrack: (
                "### 1. [锦囊二| 液冷] 北美冷板厂扩产\n- Claim: 冷板扩产确认\n- Source: HN\n- Confidence: 64% (medium)",
                {
                    "status": "success",
                    "duration_seconds": 1.1,
                    "content_length": 110,
                    "structured_items": 1,
                    "jinnang_1_count": 0,
                    "jinnang_2_count": 1,
                    "jinnang_3_count": 0,
                    "raw_results": {
                        "jinnang_1": [],
                        "jinnang_2": [
                            {
                                "title": "北美冷板厂扩产",
                                "summary": "冷板扩产确认",
                                "track": "液冷",
                                "source_name": "HN",
                                "source_url": "https://example.com/h",
                            }
                        ],
                        "jinnang_3": [],
                    },
                },
            ),
        )
        monkeypatch.setattr(orchestrator, "gemini_pro_audit", lambda raw, audit_type="p5", backtrack="24h": f"{audit_type} audited")
        monkeypatch.setattr(orchestrator, "fetch_vanguard_prices", lambda: "NVIDIA 区间涨跌 +5.2%")
        monkeypatch.setattr(
            orchestrator,
            "generate_report",
            lambda evidence: "### 赛道: 覆铜板\n- 生益科技（600183）| 高弹性 | 涨价传导\nSEARCH: 覆铜板提价持续性\nDEEP_RESEARCH: 覆铜板涨价如何向A股传导",
        )
        monkeypatch.setattr(
            orchestrator,
            "build_research_plan",
            lambda p6_mapping, p4_data="", p5_evidence="": {
                "goal": "close the supply chain loop",
                "triggers": ["explicit_topics", "second_order_chain"],
                "deep_topics": ["覆铜板涨价如何向A股传导"],
                "supplement_queries": ["覆铜板提价持续性"],
                "should_run_p7": True,
                "should_run_p75": True,
                "max_rounds": 2,
                "round_backtracks": ["72h", "168h"],
                "skipped_recent_topics": [],
            },
        )
        monkeypatch.setattr(
            orchestrator,
            "run_deep_research",
            lambda plan: {
                "status": "completed",
                "topics_planned": 1,
                "topics_completed": 1,
                "rounds_run": 2,
                "successful_rounds": 2,
                "text": "## 深度研究：覆铜板涨价如何向A股传导\n#### Claim\n覆铜板涨价已经开始传导。",
            },
        )
        monkeypatch.setattr(orchestrator, "p7a_deep_research_audit", lambda text: "P7a audited")
        monkeypatch.setattr(orchestrator, "gemini_search", lambda stage, query, backtrack: {"text": f"{query} search result", "grounding_sources": []})
        monkeypatch.setattr(orchestrator, "extract_stocks_from_report", lambda report: [("生益科技", "600183")])
        monkeypatch.setattr(orchestrator, "extract_stock_tracks", lambda report: {"600183": "覆铜板"})
        monkeypatch.setattr(orchestrator, "extract_stock_elasticity", lambda report: {"600183": "高弹性"})
        monkeypatch.setattr(orchestrator, "p8_search_stock_prices", lambda stocks, tracks, elasticity: "P8 技术面数据")
        monkeypatch.setattr(
            orchestrator,
            "p9_risk_analysis",
            lambda **kwargs: ("# simulated report\n\n## 一、核心信号\n- 覆铜板涨价传导", artifact_store.get_run_timestamp()),
        )
        monkeypatch.setattr(p9_reporter, "EMAIL_ADDRESS", "")
        monkeypatch.setattr(p9_reporter, "EMAIL_PASSWORD", "")
        monkeypatch.setattr(p9_reporter, "RECEIVER_EMAIL", "")
        monkeypatch.setattr(orchestrator.sys, "argv", ["orchestrator.py"])

        orchestrator.orchestrator_main()

        report_files = list(out_dir.rglob("*.md"))
        roi_files = [path for path in report_files if path.name.startswith("Layer_ROI_")]
        manifest_path = out_dir / "run_manifest.json"

        assert any(path.name.startswith("战报_") for path in report_files)
        assert roi_files
        assert manifest_path.exists(), "manifest should be generated"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["stages"]["p1"]["status"] == "completed"
        assert manifest["stages"]["p3"]["metrics"]["structured_items"] >= 1
        assert manifest["stages"]["roi"]["status"] == "completed"
