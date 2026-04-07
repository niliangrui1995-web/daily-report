# -*- coding: utf-8 -*-
"""主调度器 — VCP Hunter 每日战报全流程编排。

v3.6 (模块化 P-Series) - 2026-04-07
【核心破局】
1. 已全面切换为 P1~P9 架构。
2. P1/P2/P3 多源并采。
3. 彻底移除阻塞式 time.sleep 风控冷却时延。
4. 深度植入 log_heartbeat 心跳守护防假死。
"""

import os
import sys
import time
import logging
import json
import re
import concurrent.futures
import threading
import contextlib
from datetime import datetime

from settings import OUTPUT_DIR, get_today_output_dir
from trading_calendar import is_trading_day
from checkpoint import detect_checkpoint
from run_config import get_run_config

# P 系列全量模块导入
from p1_rss import collect_from_rss
from p1_web import collect_from_web
from p2_x_intel import collect_from_x
from p3_agent import collect_p3_parallel_only
from p4_vanguard import fetch_vanguard_prices
from audit_engine import gemini_pro_audit
from p6_mapper import format_evidence, generate_report
from p7_deep_research import run_deep_research
from p8_technical import (
    extract_stocks_from_report,
    extract_stock_tracks,
    extract_stock_elasticity,
    p8_search_stock_prices,
)
from p9_reporter import p9_risk_analysis, send_report_email
from search_engine import gemini_search, GEMINI_CONCURRENCY


@contextlib.contextmanager
def log_heartbeat(task_name: str, interval: int = 15):
    """守护线程心脏跳动机制，防止大模型长耗时任务在控制台假死。"""
    stop_event = threading.Event()

    def heartbeat():
        start_time = time.time()
        while not stop_event.wait(interval):
            elapsed = int(time.time() - start_time)
            logging.info(f"⏳ [{task_name}] 模型仍在深度运转，已等待 {elapsed} 秒...")

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop_event.set()
        t.join(timeout=1.0)


def orchestrator_main():
    """主流程编排"""
    total_start = time.time()
    now = datetime.now()

    # 全局变量初始化
    p5_evidence = ""
    p2_data = ""
    p3_data = ""
    p4_data = ""
    p6_mapping = ""
    p7_deep = ""
    p75_supplement = ""
    price_data = ""

    logging.info("=" * 60)
    logging.info(
        f"VCP Hunter v3.6 (P-Series 全并发极速版) | {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    logging.info("=" * 60)

    # 强制与交易日判断
    # Why: scheduled_launcher.py 传入 --force-run，必须与这里匹配
    force_restart = "--force" in sys.argv or "--force-restart" in sys.argv or "--force-run" in sys.argv
    if not force_restart and not is_trading_day(now.date()):
        logging.info("非交易日，休眠。如需强制执行请加 --force-run 参数。")
        return

    # 断点自适应检测
    ckpt = {"stage": "none", "files": {}}
    if not force_restart:
        ckpt = detect_checkpoint()

    resume_from = ckpt["stage"]
    if resume_from == "p9_done":
        logging.info("✅ [断点检测] 今天的战报已生成，任务结束。")
        return

    # ==================== P1~P3: 并行感知层 ====================
    if resume_from in ("none", "p1"):
        backtrack = get_run_config(now)

        logging.info("📡 [P1-P3] 并行采集：RSS + Web + X + Agent")
        p1_results = []
        p1_failed_urls = []  # Why: 收集 P1 阵亡 URL，供锦囊一补漏
        # Why: RSS/Web/X/Agent 四路并行，开 4 个 worker 避免第四路等待
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rss_fut = pool.submit(collect_from_rss, backtrack)
            web_fut = pool.submit(collect_from_web, backtrack)
            x_fut = pool.submit(collect_from_x, backtrack)
            agent_fut = pool.submit(collect_p3_parallel_only, backtrack)

            try:
                rss_results, rss_stats = rss_fut.result()
                p1_results.extend(rss_results)
                # Why: P1 RSS 的 stats 中包含正文深挖失败的 URL 列表
                p1_failed_urls.extend(rss_stats.get("failed_urls", []))
            except Exception as e:
                logging.warning(f"RSS采集异常: {e}")
            try:
                web_results, web_stats = web_fut.result()
                p1_results.extend(web_results)
            except Exception as e:
                logging.warning(f"Web采集异常: {e}")
            try:
                p2_data, _ = x_fut.result()
            except Exception as e:
                logging.warning(f"X情报采集异常: {e}")
            try:
                p3_data, _ = agent_fut.result()
            except Exception as e:
                logging.warning(f"Grok Agent异常: {e}")

        # Why: P1 结束后用阵亡 URL 触发锦囊一"爬虫攻坚补漏"
        if p1_failed_urls:
            logging.info(f"🎯 [P3 锦囊一] 收到 {len(p1_failed_urls)} 个 P1 阵亡 URL，启动空袭补漏...")
            try:
                from p3_agent import run_jinnang_1, format_p3_output
                jinnang_1_results = run_jinnang_1(p1_failed_urls)
                if jinnang_1_results:
                    # Why: 将锦囊一的结果拼接到 p3_data 末尾
                    jinnang_1_text = format_p3_output(jinnang_1_results, [], [])
                    p3_data = (p3_data + "\n\n" + jinnang_1_text) if p3_data else jinnang_1_text
                    logging.info(f"✅ [P3 锦囊一] 抢救回 {len(jinnang_1_results)} 条情报，已拼入 P3 数据")
            except Exception as e:
                logging.warning(f"⚠️ [P3 锦囊一] 补漏失败: {e}")

        evidence_raw = format_evidence(p1_results) if p1_results else ""
        logging.info(
            f"[P1→P5] 信息源聚合获取完成，准备提交 P5 审计: {len(evidence_raw)} 字"
        )

        # 保存 P1/P2/P3/P4 中间件（匹配老架构观测功能）
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        out_dir = get_today_output_dir()

        # P1
        try:
            with open(os.path.join(out_dir, f"P1_RSS原始数据_{now_str}.json"), "w", encoding="utf-8") as f:
                json.dump(p1_results, f, ensure_ascii=False, indent=2)
            with open(os.path.join(out_dir, f"P1_RSS原始数据_{now_str}.md"), "w", encoding="utf-8") as f:
                f.write(evidence_raw)
        except Exception as e:
            logging.error(f"[P1] 原始数据保存失败: {e}")

        # P2
        if p2_data:
            try:
                with open(os.path.join(out_dir, f"P2_X平台情报_{now_str}.md"), "w", encoding="utf-8") as f:
                    f.write(p2_data)
            except Exception as e:
                logging.error(f"[P2] 情报保存失败: {e}")

        # P3
        if p3_data:
            try:
                with open(os.path.join(out_dir, f"P3_机动情报_{now_str}.md"), "w", encoding="utf-8") as f:
                    f.write(p3_data)
                with open(os.path.join(out_dir, f"P3_机动情报_{now_str}.json"), "w", encoding="utf-8") as f:
                    # just basic save as requested format
                    json.dump({"data": p3_data}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.error(f"[P3] 情报保存失败: {e}")

        # ==================== P5: 主观清洗审计 ====================
        with log_heartbeat("P5 情报审计"):
            p5_evidence = gemini_pro_audit(
                evidence_raw, audit_type="l1.5", backtrack=backtrack
            )

        p5_fname_md = os.path.join(
            get_today_output_dir(),
            f"P5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.md",
        )
        p5_fname_json = os.path.join(
            get_today_output_dir(),
            f"P5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.json",
        )
        try:
            with open(p5_fname_md, "w", encoding="utf-8") as f:
                f.write(p5_evidence if p5_evidence else evidence_raw)
            with open(p5_fname_json, "w", encoding="utf-8") as f:
                json.dump({"audit_text": p5_evidence if p5_evidence else evidence_raw}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"[P5] 文件保存失败: {e}")

    elif "p5" in ckpt["files"]:
        logging.info("🔄 [断点恢复] 从本地恢复 P5 审计结果...")
        with open(ckpt["files"]["p5"], "r", encoding="utf-8") as f:
            p5_evidence = f.read()
        # Why: 断点恢复时 P2/P3 数据也必须加载，否则 P9 战报缺少 50% 情报维度
        if "p2" in ckpt["files"]:
            try:
                with open(ckpt["files"]["p2"], "r", encoding="utf-8") as f:
                    p2_data = f.read()
                logging.info("  ↳ 已恢复 P2 X平台情报")
            except Exception as e:
                logging.warning(f"  ↳ P2 数据恢复失败: {e}")
        if "p3" in ckpt["files"]:
            try:
                with open(ckpt["files"]["p3"], "r", encoding="utf-8") as f:
                    p3_data = f.read()
                logging.info("  ↳ 已恢复 P3 机动情报")
            except Exception as e:
                logging.warning(f"  ↳ P3 数据恢复失败: {e}")

    # ==================== P6: 核心映射与脉络推演 ====================
    if resume_from in ("none", "p1", "p5") and not p6_mapping:
        # Why: P4 在 p4_vanguard.py 内部已自动落盘，这里不重复保存
        try:
            p4_data = fetch_vanguard_prices()
        except Exception as e:
            logging.error(f"[P4] 获取海外行情失败: {e}")

        combined_evidence = (
            p5_evidence + "\n" + p4_data + "\n" + p2_data + "\n" + p3_data
        )

        with log_heartbeat("P6 核心脉络映射"):
            p6_mapping = generate_report(combined_evidence)

        # 这里已经有 P6 保存逻辑了 (不变)
        p6_fname = os.path.join(
            get_today_output_dir(),
            f"P6_映射_{datetime.now().strftime('%Y%m%d%H%M')}.md",
        )
        p6_fname_json = os.path.join(
            get_today_output_dir(),
            f"P6_映射_{datetime.now().strftime('%Y%m%d%H%M')}.json",
        )
        try:
            with open(p6_fname, "w", encoding="utf-8") as f:
                f.write(p6_mapping)
            with open(p6_fname_json, "w", encoding="utf-8") as f:
                json.dump({"mapping": p6_mapping}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"[P6] 映射写入失败: {e}")

    elif "p6" in ckpt["files"]:
        logging.info("🔄 [断点恢复] 从本地恢复 P6 映射结果...")
        with open(ckpt["files"]["p6"], "r", encoding="utf-8") as f:
            p6_mapping = f.read()

    # ==================== P7 & P7.5: 深度研究与搜索补充 ====================
    search_queries = re.findall(r"^\s*SEARCH:\s*(.+)$", p6_mapping, re.MULTILINE)

    with log_heartbeat("P7 深研与 P7.5 搜索"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            p7_fut = pool.submit(run_deep_research, p6_mapping)

            sup_results = []
            if search_queries:
                logging.info(f"[P7.5] 触发 {len(search_queries)} 个补充搜索指令")
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=GEMINI_CONCURRENCY
                ) as spool:
                    futures = {
                        spool.submit(gemini_search, "P7.5补充", q, "24h"): q
                        for q in search_queries[:5] # 最多补5个
                    }
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            res = future.result()
                            if res and res.get("text"):
                                sup_results.append(res["text"])
                        except Exception as e:
                            logging.error(f"[P7.5] 补充搜索失败: {e}")

            try:
                p7_deep = p7_fut.result(timeout=600)
            except concurrent.futures.TimeoutError:
                logging.warning("[P7] 深度研究超时")
                p7_deep = ""

            if sup_results:
                raw_p75 = "\n".join(sup_results)
                with log_heartbeat("P7.5 搜索结果审计"):
                    p75_supplement = gemini_pro_audit(raw_p75, audit_type="p7.5", backtrack="24h")
            else:
                p75_supplement = ""

    # 保存 P7 深研 (注: P7.5 已经在 audit_engine 里自动落盘双轨 json/md)
    now_str = datetime.now().strftime('%Y%m%d%H%M')
    if p7_deep:
        try:
            with open(os.path.join(get_today_output_dir(), f"P7_深度研究_{now_str}.md"), "w", encoding="utf-8") as f:
                f.write(p7_deep)
        except Exception as e:
            logging.error(f"[P7] 深度研究文件写入失败: {e}")

    # ==================== P8: 客观技术数据与资金测算 ====================
    with log_heartbeat("P8 资金异动分析"):
        stocks = extract_stocks_from_report(p6_mapping)
        if stocks:
            tracks = extract_stock_tracks(p6_mapping)
            elasticity = extract_stock_elasticity(p6_mapping)
            price_data = p8_search_stock_prices(stocks, tracks, elasticity)

    # ==================== P9: 终极拼图战报 ====================
    with log_heartbeat("P9 终极聚合战报生成"):
        logging.info(
            f"[P9 完整性] 八层数据就绪 (P5:{len(p5_evidence)}, P2:{len(p2_data)}, P3:{len(p3_data)}, P4:{len(p4_data)}, P7:{len(p7_deep)}, P6:{len(p6_mapping)}, P7.5:{len(p75_supplement)}, P8:{len(price_data)})"
        )
        final_report = p9_risk_analysis(
            p5_evidence=p5_evidence,
            p7_deep=p7_deep,
            p75_supplement=p75_supplement,
            p6_mapping=p6_mapping,
            price_data=price_data,
            p2_x_intel_data=p2_data,
            p3_intel_data=p3_data,
            p4_yfinance=p4_data,
        )

    send_report_email(final_report)

    total_elapsed = time.time() - total_start
    logging.info(
        f"🎯 VCP Hunter 运转完毕。总耗时: {int(total_elapsed // 60)}分{int(total_elapsed % 60)}秒 ({total_elapsed:.1f}s)"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
    for lib in ["httpx", "httpcore", "google", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    # 完整流程日志写入文件：包含 INFO 及以上自动保存
    pipeline_log_path = os.path.join(OUTPUT_DIR, "pipeline.log")
    pipeline_handler = logging.FileHandler(pipeline_log_path, encoding="utf-8", mode="a")
    pipeline_handler.setLevel(logging.INFO)
    pipeline_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(pipeline_handler)

    # 错误日志单独分流
    error_log_path = os.path.join(OUTPUT_DIR, "error.log")
    file_handler = logging.FileHandler(error_log_path, encoding="utf-8", mode="a")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    orchestrator_main()
