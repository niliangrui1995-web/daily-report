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
import re
import concurrent.futures
import threading
import contextlib
from datetime import datetime

from artifact_store import (
    set_run_timestamp,
    update_run_manifest,
    write_json_artifact,
    write_text_artifact,
)
from settings import OUTPUT_DIR
from trading_calendar import is_trading_day
from checkpoint import detect_checkpoint
from run_config import get_run_config

# P 系列全量模块导入
from p1_rss import collect_from_rss
from p1_web import collect_from_web
from p2_x_intel import collect_from_x
from p3_agent import collect_p3_parallel_only
from p4_vanguard import fetch_vanguard_prices
from audit_engine import gemini_pro_audit, p7a_deep_research_audit
from p6_mapper import format_evidence, generate_report
from p7_deep_research import run_deep_research
from p8_technical import (
    extract_stocks_from_report,
    extract_stock_tracks,
    extract_stock_elasticity,
    p8_search_stock_prices,
)
from p9_reporter import p9_risk_analysis, send_report_email
from search_engine import gemini_search


def _configure_stdio() -> None:
    """避免 Windows 控制台默认编码导致 emoji/中文日志写出失败。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


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


def _recover_p2_p3_from_checkpoint(ckpt: dict) -> tuple[str, str]:
    """从断点文件恢复 P2/P3 数据。独立函数避免 P5/P6 恢复分支重复代码。"""
    p2_data = ""
    p3_data = ""
    if "p2" in ckpt["files"]:
        try:
            with open(ckpt["files"]["p2"], "r", encoding="utf-8") as f:
                p2_data = f.read()
            logging.info("  ↳ 已恢复 P2 X平台情报")
        except FileNotFoundError:
            logging.warning("  ↳ P2 文件已被删除（竞态）")
        except Exception as e:
            logging.warning(f"  ↳ P2 数据恢复失败: {e}")
    if "p3" in ckpt["files"]:
        try:
            with open(ckpt["files"]["p3"], "r", encoding="utf-8") as f:
                p3_data = f.read()
            logging.info("  ↳ 已恢复 P3 机动情报")
        except FileNotFoundError:
            logging.warning("  ↳ P3 文件已被删除（竞态）")
        except Exception as e:
            logging.warning(f"  ↳ P3 数据恢复失败: {e}")
    return p2_data, p3_data


def _read_text_checkpoint(path: str, label: str) -> str:
    """读取断点文本文件，统一容错和日志。"""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        logging.info(f"  ↳ 已恢复 {label}")
        return data
    except FileNotFoundError:
        logging.warning(f"  ↳ {label} 文件已被删除（竞态）")
    except Exception as e:
        logging.warning(f"  ↳ {label} 恢复失败: {e}")
    return ""


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
    p7a_audited = ""  # Why: P7a 审计后的精炼版深研，替代 p7_deep 送入 P9
    p75_supplement = ""
    price_data = ""

    logging.info("=" * 60)
    logging.info(
        f"VCP Hunter v3.6 (P-Series 全并发极速版) | {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    logging.info("=" * 60)

    # 强制与交易日判断
    # Why: --force 同时打开两个开关（跳过交易日门禁 + 忽略断点重头跑），
    #       如果只想强制运行但保留断点，应使用 --force-run
    #       如果只想忽略断点但不跳过交易日检查，应使用 --force-restart
    force_run = "--force" in sys.argv or "--force-run" in sys.argv
    force_restart = "--force" in sys.argv or "--force-restart" in sys.argv
    if not force_run and not is_trading_day(now.date()):
        logging.info("非交易日，休眠。如需强制执行请加 --force-run 参数。")
        return

    # 断点自适应检测
    ckpt = {"stage": "none", "files": {}, "run_timestamp": ""}
    if not force_restart:
        ckpt = detect_checkpoint()
        run_ts = set_run_timestamp(ckpt.get("run_timestamp") or None)
    else:
        run_ts = set_run_timestamp()

    resume_from = ckpt["stage"]
    update_run_manifest(
        "orchestrator",
        status="started",
        metrics={"resume_from": resume_from, "force_restart": force_restart, "force_run": force_run},
        timestamp=run_ts,
    )
    if resume_from == "p9_done":
        logging.info("✅ [断点检测] 今天的战报已生成，任务结束。")
        return

    # ==================== P1~P3: 并行感知层 ====================
    evidence_raw = ""
    backtrack = ""
    if resume_from in ("none", "p1"):
        backtrack = get_run_config(now)

    if resume_from == "none":
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
                web_results, _ = web_fut.result()
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
                from p3_agent import run_jinnang_1, format_p3_section
                jinnang_1_results = run_jinnang_1(p1_failed_urls)
                if jinnang_1_results:
                    # Why: 将锦囊一的结果精准替换掉原 p3_data 中的空区，保持排版一致性，避免头部重复
                    jinnang_1_text = format_p3_section('[锦囊一·爬虫攻坚补漏]', jinnang_1_results)
                    empty_placeholder = format_p3_section('[锦囊一·爬虫攻坚补漏]', [])
                    if p3_data and empty_placeholder in p3_data:
                        p3_data = p3_data.replace(empty_placeholder, jinnang_1_text)
                    else:
                        p3_data = (p3_data + "\n\n" + jinnang_1_text) if p3_data else jinnang_1_text
                    logging.info(f"✅ [P3 锦囊一] 抢救回 {len(jinnang_1_results)} 条情报，已无缝拼入 P3 数据")
            except Exception as e:
                logging.warning(f"⚠️ [P3 锦囊一] 补漏失败: {e}")

        evidence_raw = format_evidence(p1_results) if p1_results else ""
        logging.info(
            f"[P1→P5] 信息源聚合获取完成，准备提交 P5 审计: {len(evidence_raw)} 字"
        )

        # 保存 P1/P2/P3/P4 中间件（匹配老架构观测功能）
        # P1
        try:
            p1_json_path = write_json_artifact("P1_RSS原始数据", p1_results)
            p1_md_path = write_text_artifact("P1_RSS原始数据", evidence_raw)
            update_run_manifest(
                "p1",
                status="completed",
                files={"json": p1_json_path, "md": p1_md_path},
                metrics={"records": len(p1_results), "failed_urls": len(p1_failed_urls), "evidence_chars": len(evidence_raw)},
            )
        except Exception as e:
            logging.error(f"[P1] 原始数据保存失败: {e}")

        # Why: P2 已在 p2_x_intel.py._save_p2_output() 内部落盘，此处不再重复保存
        # Why: P3 已在 orchestrator 外部格式化为纯文本，JSON({"data": text}) 毫无结构化价值，删除
        if p3_data:
            try:
                p3_path = write_text_artifact("P3_机动情报", p3_data)
                update_run_manifest(
                    "p3",
                    status="completed",
                    files={"md": p3_path},
                    metrics={"content_chars": len(p3_data)},
                )
            except Exception as e:
                logging.error(f"[P3] 情报保存失败: {e}")

    elif resume_from == "p1":
        logging.info("🔄 [断点恢复] 从本地恢复 P1原始数据及相关情报，跳过网络采集...")
        if "p1_md" in ckpt["files"]:
            try:
                with open(ckpt["files"]["p1_md"], "r", encoding="utf-8") as f:
                    evidence_raw = f.read()
            except Exception as e:
                logging.warning(f"⚠️ P1 MD 恢复失败: {e}")
                evidence_raw = ""
        else:
            evidence_raw = ""
            
        p2_data, p3_data = _recover_p2_p3_from_checkpoint(ckpt)

    if resume_from in ("none", "p1"):
        # ==================== P5: 主观清洗审计 ====================
        with log_heartbeat("P5 情报审计"):
            p5_evidence = gemini_pro_audit(
                evidence_raw, audit_type="p5", backtrack=backtrack
            )
        # Why: P5 审计结果的 JSON/MD 落盘已在 audit_engine.py 内部完成，此处不再重复保存

    elif "p5" in ckpt["files"]:
        logging.info("🔄 [断点恢复] 从本地恢复 P5 审计结果...")
        p5_evidence = _read_text_checkpoint(ckpt["files"]["p5"], "P5 审计结果")
        # Why: 断点恢复时 P2/P3 数据也必须加载，否则 P9 战报缺少 50% 情报维度
        p2_data, p3_data = _recover_p2_p3_from_checkpoint(ckpt)

    # ==================== P6: 核心映射与脉络推演 ====================
    if resume_from in ("none", "p1", "p5") and not p6_mapping:
        # Why: P4 在 p4_vanguard.py 内部已自动落盘，这里不重复保存
        if "p4" in ckpt["files"]:
            logging.info("🔄 [断点恢复] 从本地恢复 P4 海外行情...")
            try:
                with open(ckpt["files"]["p4"], "r", encoding="utf-8") as f:
                    p4_data = f.read()
            except Exception as e:
                logging.warning(f"⚠️ P4 数据恢复失败，重新获取: {e}")
                try:
                    p4_data = fetch_vanguard_prices()
                except Exception as ex:
                    logging.error(f"[P4] 重新获取海外行情失败: {ex}")
        else:
            try:
                p4_data = fetch_vanguard_prices()
            except Exception as e:
                logging.error(f"[P4] 获取海外行情失败: {e}")

        combined_evidence = (
            p5_evidence + "\n" + p4_data + "\n" + p2_data + "\n" + p3_data
        )

        # Why: P6 调 Opus 也有超时挂死风险，参照 P5 的 ThreadPoolExecutor 保护
        _P6_TIMEOUT_SECONDS = 600
        with log_heartbeat("P6 核心脉络映射"):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _p6_pool:
                    _p6_fut = _p6_pool.submit(generate_report, combined_evidence)
                    p6_mapping = _p6_fut.result(timeout=_P6_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logging.error(f"🚨 [P6] Opus 映射超时（{_P6_TIMEOUT_SECONDS}s），降级返回空映射")
                p6_mapping = ""
            except Exception as e:
                logging.error(f"🚨 [P6] Opus 映射异常: {e}")
                p6_mapping = ""

        # Why: P6 JSON 已在 p6_mapper.py generate_report() 内部落盘，此处只保存 MD 格式
        try:
            p6_fname = write_text_artifact("P6_映射", p6_mapping)
            update_run_manifest(
                "p6",
                status="completed",
                files={"md": p6_fname},
                metrics={"mapping_chars": len(p6_mapping)},
            )
        except Exception as e:
            logging.error(f"[P6] 映射写入失败: {e}")

    elif "p6" in ckpt["files"]:
        logging.info("🔄 [断点恢复] 从本地恢复 P6 映射结果...")
        p6_mapping = _read_text_checkpoint(ckpt["files"]["p6"], "P6 映射结果")
        # Why: P6 恢复时也必须同步恢复 P4/P5/P2/P3 数据，否则 P9 战报缺少绝大部分基础情报
        if "p5" in ckpt["files"]:
            p5_evidence = _read_text_checkpoint(ckpt["files"]["p5"], "P5 审计数据")
        
        if "p4" in ckpt["files"]:
            p4_data = _read_text_checkpoint(ckpt["files"]["p4"], "P4 海外行情")

        p2_data, p3_data = _recover_p2_p3_from_checkpoint(ckpt)
        
        if "p7" in ckpt["files"]:
            p7_deep = _read_text_checkpoint(ckpt["files"]["p7"], "P7 深度研究")
                
        if "p7a" in ckpt["files"]:
            p7a_audited = _read_text_checkpoint(ckpt["files"]["p7a"], "P7a 深度研究审计")

        if "p75" in ckpt["files"]:
            p75_supplement = _read_text_checkpoint(ckpt["files"]["p75"], "P7.5 补充审计")

        if resume_from == "p9_resume" and "p8" in ckpt["files"]:
            price_data = _read_text_checkpoint(ckpt["files"]["p8"], "P8 技术面数据")

    # ==================== P7 & P7.5: 深度研究与搜索补充 ====================
    search_queries = re.findall(r"^\s*SEARCH:\s*(.+)$", p6_mapping, re.MULTILINE)
    need_run_p7 = True
    need_run_p75 = True

    # Why: p8_resume 语义 = P7 已完成，恢复时应优先复用历史结果，避免重复高成本调用
    if resume_from in ("p8_resume", "p9_resume"):
        if p7_deep.strip():
            need_run_p7 = False
        # Why: 若已存在 P7.5 文件则直接复用；若本次无 SEARCH 指令则也无需补跑 P7.5
        if "p75" in ckpt.get("files", {}) or not search_queries:
            need_run_p75 = False
        logging.info(
            f"🔄 [断点恢复] {resume_from} 命中：P7={'复用' if not need_run_p7 else '补跑'} | "
            f"P7.5={'复用' if not need_run_p75 else '补跑'}"
        )

    if need_run_p7 or need_run_p75:
        with log_heartbeat("P7 深研与 P7.5 搜索"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                p7_fut = pool.submit(run_deep_research, p6_mapping) if need_run_p7 else None

                sup_results = []
                if need_run_p75 and search_queries:
                    capped_queries = search_queries[:5]
                    logging.info(f"[P7.5] 触发 {len(capped_queries)} 个补充搜索指令（串行执行，避免线程池套全局限速）")
                    for q in capped_queries:
                        try:
                            res = gemini_search("P7.5补充", q, "24h")
                            if res and res.get("text"):
                                sup_results.append(res["text"])
                        except Exception as e:
                            logging.error(f"[P7.5] 补充搜索失败: {e}")

                if p7_fut is not None:
                    try:
                        p7_deep = p7_fut.result(timeout=600)
                    except concurrent.futures.TimeoutError:
                        logging.warning("[P7] 深度研究超时")
                        p7_deep = ""

                if need_run_p75:
                    if sup_results:
                        raw_p75 = "\n".join(sup_results)

                        try:
                            raw_fname = write_text_artifact("P7.5_补充搜索原始数据", raw_p75)
                            logging.info(f"💾 [P7.5] 原始搜索结果已落盘: {raw_fname}")
                            update_run_manifest(
                                "p75",
                                status="raw_ready",
                                files={"raw": raw_fname},
                                metrics={"queries": len(search_queries[:5]), "raw_chars": len(raw_p75)},
                            )
                        except Exception as e:
                            logging.error(f"[P7.5] 补充搜索原始数据保存失败: {e}")

                        with log_heartbeat("P7.5 搜索结果审计"):
                            p75_supplement = gemini_pro_audit(raw_p75, audit_type="p7.5", backtrack="24h")
                            if not p75_supplement:
                                logging.warning("⚠️ [P7.5] 审计失败或返回为空，强制降级透传原始搜索数据")
                                p75_supplement = raw_p75
                    else:
                        p75_supplement = ""
                        update_run_manifest(
                            "p75",
                            status="skipped_no_results",
                            metrics={"queries": len(search_queries[:5]), "raw_chars": 0},
                        )

    # 保存 P7 深研原始数据 (注: P7.5 已经在 audit_engine 里自动落盘双轨 json/md)
    if p7_deep:
        try:
            p7_body = (
                "# P7 深度研究结果\n\n"
                f"> 生成时间: {datetime.now().isoformat()}\n\n"
                f"{p7_deep}"
            )
            p7_path = write_text_artifact("P7_深度研究", p7_body)
            update_run_manifest(
                "p7",
                status="completed",
                files={"md": p7_path},
                metrics={"output_chars": len(p7_deep), "topics": p7_deep.count("## 深度研究：")},
            )
        except Exception as e:
            logging.error(f"[P7] 深度研究文件写入失败: {e}")

    # ==================== P7a: 深度研究审计（去重+汇总） ====================
    # Why: P7 原始输出通常 40K+ 字，直接送入 P9 会占据近一半注意力窗口
    #       P7a 用 Gemini 3.1 Pro 做跨轮次去重 + 噪声过滤 + 结构化汇总
    #       输出 8~12K 字的精炼 Markdown 替代原始数据送入 P9
    if p7a_audited:
        logging.info("♻️ [P7a] 已由断点底层复用深度研究审计结果，智能跳过开销昂贵的二次大模型过滤")
    elif p7_deep and len(p7_deep.strip()) > 200:
        with log_heartbeat("P7a 深度研究审计"):
            p7a_audited = p7a_deep_research_audit(p7_deep)
    else:
        p7a_audited = p7_deep

    # ==================== P8: 客观技术数据与资金测算 ====================
    if resume_from == "p9_resume" and price_data.strip():
        logging.info("♻️ [P8] 已从断点复用技术面数据，跳过重算")
    else:
        with log_heartbeat("P8 资金异动分析"):
            stocks = extract_stocks_from_report(p6_mapping)
            tracks = extract_stock_tracks(p6_mapping)
            elasticity = extract_stock_elasticity(p6_mapping)
            price_data = p8_search_stock_prices(stocks, tracks, elasticity)

    # ==================== P9: 终极拼图战报 ====================
    # Why: P9 调 Opus 是流水线最后一步，挂死 = 前面所有工作白费，必须加超时保护
    #      P7 数据使用 P7a 审计后的精炼版本，避免原始数据（40K+字）稀释 Opus 注意力
    _P9_TIMEOUT_SECONDS = 600
    with log_heartbeat("P9 终极聚合战报生成"):
        logging.info(
            f"[P9 完整性] 八层数据就绪 (P5:{len(p5_evidence)}, P2:{len(p2_data)}, P3:{len(p3_data)}, P4:{len(p4_data)}, P7a:{len(p7a_audited)}, P6:{len(p6_mapping)}, P7.5:{len(p75_supplement)}, P8:{len(price_data)})"
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _p9_pool:
                _p9_fut = _p9_pool.submit(
                    p9_risk_analysis,
                    p5_evidence=p5_evidence,
                    p7_deep=p7a_audited,  # Why: 传 P7a 审计后的精炼数据，不是 P7 原始数据
                    p75_supplement=p75_supplement,
                    p6_mapping=p6_mapping,
                    price_data=price_data,
                    p2_x_intel_data=p2_data,
                    p3_intel_data=p3_data,
                    p4_yfinance=p4_data,
                )
                # Why: p9_risk_analysis 现在返回 (报告内容, 统一时间戳)
                final_report, report_ts = _p9_fut.result(timeout=_P9_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logging.error(f"🚨 [P9] Opus 战报生成超时（{_P9_TIMEOUT_SECONDS}s），降级为空战报")
            final_report = ""
            report_ts = ""
        except Exception as e:
            logging.error(f"🚨 [P9] Opus 战报异常: {e}")
            final_report = ""
            report_ts = ""

    send_report_email(final_report, report_ts)

    total_elapsed = time.time() - total_start
    update_run_manifest(
        "orchestrator",
        status="completed",
        metrics={"total_elapsed_seconds": round(total_elapsed, 1), "final_report_chars": len(final_report or "")},
    )
    logging.info(
        f"🎯 VCP Hunter 运转完毕。总耗时: {int(total_elapsed // 60)}分{int(total_elapsed % 60)}秒 ({total_elapsed:.1f}s)"
    )


if __name__ == "__main__":
    _configure_stdio()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
    for lib in ["httpx", "httpcore", "google", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    # Why: 改用 RotatingFileHandler 防止日志文件无限增长（单文件上限 10MB，保留 5 份历史）
    from logging.handlers import RotatingFileHandler

    # 完整流程日志写入文件：包含 INFO 及以上自动保存
    pipeline_log_path = os.path.join(OUTPUT_DIR, "pipeline.log")
    pipeline_handler = RotatingFileHandler(
        pipeline_log_path, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    pipeline_handler.setLevel(logging.INFO)
    pipeline_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(pipeline_handler)

    # 错误日志单独分流
    error_log_path = os.path.join(OUTPUT_DIR, "error.log")
    file_handler = RotatingFileHandler(
        error_log_path, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    orchestrator_main()
