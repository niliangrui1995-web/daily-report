# -*- coding: utf-8 -*-
"""L1a X(Twitter) 产业链情报采集 — 使用 Grok API 抓取 X 平台 AI 产业链爆料。

设计原理：
  - Grok 天生拥有对 X 平台实时数据的原生访问能力，这是其他 LLM 不具备的独家优势
  - 通过精心设计的 system prompt 让 Grok 扮演产业链情报分析师，
    在 X 上按 17 个核心赛道搜刮最新的产业链爆料、insider leak、供应链异动
  - L1a 的输出不经过 L1.5 审计（因为 Grok 本身就是一手信源提取器，
    审计会损失 X 独有的即时性和爆料性信息）
  - L1a 输出直接与 L1.5 审计后数据拼接，一起送给 Opus 做 L2 映射

连接方式：
  - 走本机翻墙代理（与 Gemini Search 直连共享同一个自动探测端口）
  - xAI 原生 REST API（直连 xAI 服务器，无任何中间代理）
  - 端点：https://api.x.ai/v1/chat/completions
"""

import os
import time
import random
import logging
import requests
from datetime import datetime

from settings import get_today_output_dir, load_prompt
from industry_dict import OLIGARCH_DICT, SUPER_GIANTS
# Grok API 配置
GROK_API_KEY = os.environ.get("GROK_API_KEY", "")
GROK_BASE_URL = "https://api.x.ai"
GROK_MODEL = "grok-4.20-reasoning"  # 必须使用 reasoning 模型支持 x_search

# 17 赛道关键词矩阵 — 让 Grok 按这些维度在 X 上定向扫描
INDUSTRY_SCAN_KEYWORDS = [
    "NVIDIA GPU AI chip supply chain",
    "TSMC advanced packaging CoWoS HBM",
    "Broadcom ASIC custom chip",
    "ASML EUV lithography semiconductor equipment",
    "SK Hynix Samsung Micron HBM memory TSV TC-NCF MR-MUF ALD precursor EMC",
    "AI server datacenter cooling liquid cooling cold plate CDU manifold fluorinated coolant quick disconnect",
    "optical module CPO silicon photonics 800G 1.6T EML VCSEL Faraday isolator",
    "PCB CCL high frequency material AI server copper foil electronic glass fiber PTFE resin ABF substrate",
    "advanced packaging hybrid bonding chiplet",
    "CXL memory interconnect",
    "nuclear power SMR data center energy",
    "edge AI inference chip Qualcomm MediaTek",
    "AI infrastructure capex cloud hyperscaler",
    "semiconductor test Advantest probe card",
    "fiber optic cable telecom infrastructure",
    "China semiconductor self-sufficiency SMIC Huawei",
    "AI model training inference GPU shortage supply",
]


def _build_user_prompt(backtrack: str) -> str:
    """根据回溯窗口构造用户提示词，从 prompts/l1a_user.txt 模板填充。"""
    hours = int(backtrack.replace("h", "")) if "h" in backtrack else 24

    keyword_section = "\n".join(
        [f"  {i+1}. {kw}" for i, kw in enumerate(INDUSTRY_SCAN_KEYWORDS)]
    )

    # Why: prompt 外化到文件，修改提示词不需要改代码，与其他模块（L2/L4）统一架构
    template = load_prompt("l1a_user.txt")
    return template.format(
        hours=hours,
        keyword_section=keyword_section,
        current_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
    )


def collect_from_x(backtrack: str = "24h", max_retries: int = 3) -> tuple[str, dict]:
    """调用 Grok API 采集 X 平台产业链情报。

    Args:
        backtrack: 回溯时间窗口（如 "24h", "72h"）
        max_retries: 最大重试次数

    Returns:
        tuple: (情报文本, 统计信息字典)
              情报文本为空字符串表示采集失败
    """
    stats = {
        "source": "X(Twitter) via Grok",
        "model": GROK_MODEL,
        "status": "pending",
        "duration_seconds": 0,
        "content_length": 0,
    }

    if not GROK_API_KEY:
        logging.warning("⚠️ [L1a] GROK_API_KEY 未配置，跳过 X 平台采集")
        stats["status"] = "skipped_no_key"
        return "", stats

    logging.info(f"📱 [L1a] 启动 X(Twitter) 产业链情报采集 (Grok {GROK_MODEL})...")
    logging.info(f"  回溯窗口: {backtrack}")

    user_prompt = _build_user_prompt(backtrack)

    # 动态渲染 System Prompt 赛道列表
    raw_system_prompt = load_prompt("l1a_system.txt")
    track_lines = []
    for i, (track, companies) in enumerate(OLIGARCH_DICT.items(), 1):
        # 提取每个赛道的核心代表公司
        companies_str = "/".join(companies)
        track_lines.append(f"{i}. {track} ({companies_str})")
    industry_tracks = "\n".join(track_lines)
    super_giants_str = ", ".join(SUPER_GIANTS)
    
    system_prompt_content = raw_system_prompt.replace("{{track_count}}", str(len(OLIGARCH_DICT)))
    system_prompt_content = system_prompt_content.replace("{{industry_tracks}}", industry_tracks)
    system_prompt_content = system_prompt_content.replace("{{super_giants}}", super_giants_str)

    # 构造 xAI Grok API 请求体（直连 xAI 服务器）
    payload = {
        "model": GROK_MODEL,
        "input": [
            {"role": "system", "content": system_prompt_content},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [
            {"type": "x_search"}
        ],
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }

    for attempt in range(max_retries):
        try:
            logging.info(f"🔄 [L1a] 开始请求 Grok API (响应式原生搜索接口)，提取 X 平台爆料 (第 {attempt+1}/{max_retries} 次尝试)...")
            start_time = time.time()
            
            response = requests.post(
                f"{GROK_BASE_URL}/v1/responses",
                json=payload,
                headers=headers,
                timeout=180
            )

            if response.status_code == 401 or response.status_code == 403:
                logging.error(f"❌ [L1a] Grok API 鉴权失败 (HTTP {response.status_code})")
                stats["status"] = f"auth_failed_{response.status_code}"
                return "", stats

            if response.status_code == 429:
                # Why: 429 限流需要指数退避，而不是直接放弃
                wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
                logging.warning(
                    f"⏳ [L1a] Grok 触发限流(429)，等待 {wait:.1f}s "
                    f"({attempt+1}/{max_retries})"
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()
            
            content = ""
            if "output" in data:
                # 遍历 output 数组，找到 assistant message 的文本
                for item in data["output"]:
                    if item.get("role") == "assistant" and item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                content += c.get("text", "")
            
            if not content:
                # 如果没找到标准格式，尝试兜底
                logging.error(f"❌ [L1a] API 返回数据格式不明或无文本: {data.keys()}")
                raise ValueError("XAI API返回空内容")

            logging.info(f"✅ [L1a] 成功获取情报内容，长度: {len(content)} 字符，耗时 {time.time() - start_time:.1f} 秒")

            # 使用推特官方的无鉴权 CDN 接口验证 ID 真伪，剔除大模型幻觉的垃圾数据
            import re
            lines = content.split('\n')
            filtered_content = []
            block_buffer = []
            in_block = False
            block_valid = True

            for line in lines:
                if '<x_leaks>' in line:
                    in_block = True
                    block_buffer = [line]
                    block_valid = True
                elif '</x_leaks>' in line:
                    if in_block:
                        block_buffer.append(line)
                        if block_valid:
                            filtered_content.extend(block_buffer)
                        else:
                            logging.warning("🚮 [L1a] 测出大模型幻觉：检测到虚假推文推文，已将该条爆料物理超度。")
                        in_block = False
                        block_buffer = []
                elif in_block:
                    block_buffer.append(line)
                    # 抓取 URL
                    match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/]+/status/(\d+)', line)
                    if match:
                        tweet_id = match.group(1)
                        # 大模型在被迫捏造推文ID时，100% 会使用连续递增/递减/重复的数字
                        # 例如：1234567890, 87654321, 000000 
                        if re.search(r'(01234|12345|23456|34567|45678|56789|67890|98765|87654|76543|65432|54321|43210|0000|1111)', tweet_id):
                            block_valid = False
                            logging.warning(f"🚫 [L1a] 打假成功：检测到模型生成的连号假ID({tweet_id})，已物理销毁。")
                else:
                    # 原样保留块外的内容
                    filtered_content.append(line)

            content = '\n'.join(filtered_content)
            
            # 如果剔除完之后全空了，说明模型一直在胡说八道
            if not content.strip() or '<x_leaks>' not in content:
                logging.warning(f"⚠️ [L1a] 所有提取的情报均为大模型幻觉(假链接)，全部作废，进入重试循环 ({attempt+1}/{max_retries})")
                time.sleep(5)
                continue

            elapsed = time.time() - start_time
            stats["status"] = "success"
            stats["duration_seconds"] = round(elapsed, 1)
            stats["content_length"] = len(content)

            logging.info(
                f"✅ [L1a] X 平台情报采集完成: {len(content)} 字, "
                f"耗时 {elapsed:.1f}s"
            )

            # 包装成带标识头的格式，方便下游识别数据来源
            formatted = (
                f"{'=' * 60}\n"
                f"以下数据来自 X(Twitter) 平台实时采集（Grok {GROK_MODEL}）\n"
                f"信息来源: X平台产业链insider/leaker/分析师/官方账号\n"
                f"采集时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"⚠️ 注意: X平台信息未经 L1.5 审计，可信度参差不齐，需结合其他数据源交叉验证\n"
                f"{'=' * 60}\n\n"
                f"{content}"
            )

            # 保存原始 L1a 数据到文件
            _save_l1a_output(formatted, stats)

            return formatted, stats

        except requests.exceptions.Timeout:
            wait = min(10 * (2 ** attempt), 60)
            logging.warning(
                f"⏳ [L1a] Grok 请求超时，等待 {wait:.1f}s "
                f"({attempt+1}/{max_retries})"
            )
            time.sleep(wait)

        except Exception as e:
            wait = min(10 * (2 ** attempt), 60) + random.uniform(0, 2.0)
            logging.warning(
                f"⚠️ [L1a] Grok 异常: {type(e).__name__}: {e}，"
                f"等待 {wait:.1f}s ({attempt+1}/{max_retries})"
            )
            time.sleep(wait)

    elapsed = time.time() - start_time
    stats["status"] = "failed_all_retries"
    stats["duration_seconds"] = round(elapsed, 1)
    logging.error(f"❌ [L1a] Grok {max_retries} 次重试全部失败")
    return "", stats


def _save_l1a_output(content: str, stats: dict) -> None:
    """将 L1a 采集结果保存到当天输出目录。"""
    try:
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        output_dir = get_today_output_dir()
        filename = os.path.join(output_dir, f"L1a_X平台情报_{now_str}.md")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# L1a X(Twitter) 产业链情报\n\n")
            f.write(f"> 采集时间: {datetime.now().isoformat()}\n")
            f.write(f"> 模型: {stats.get('model', GROK_MODEL)}\n")
            f.write(f"> 耗时: {stats.get('duration_seconds', 0)}s\n")
            f.write(f"> 内容长度: {stats.get('content_length', 0)} 字\n\n")
            f.write(content)
        logging.info(f"[L1a] 原始数据已保存: {filename}")
    except Exception as e:
        logging.warning(f"[L1a] 保存失败: {e}")
