"""
P2 X(Twitter) 产业链情报采集 — 使用 Grok API 抓取 X 平台 AI 产业链爆料。

设计原理：
  - Grok 天生拥有对 X 平台实时数据的原生访问能力，这是其他 LLM 不具备的独家优势
  - 通过精心设计的 system prompt 让 Grok 扮演产业链情报分析师，
    在 X 上按 15 个核心赛道搜刮最新的产业链爆料、insider leak、供应链异动
  - P2 的输出不经过 P5 审计（因为 Grok 本身就是一手信源提取器，
    审计会损失 X 独有的即时性和爆料性信息）
  - P2 输出直接与 P5 审计后数据拼接，一起送给 Opus 做 P6 映射

连接方式：
  - 走本机翻墙代理（与 Gemini Search 直连共享同一个自动探测端口）
  - xAI 原生 REST API（直连 xAI 服务器，无任何中间代理）
  - 端点：https://api.x.ai/v1/chat/completions

【⚠️核心维护规则 / Agent 指令】
1. P2 作为直连 Grok 的“第一手推特爆料池”，按架构规矩绝对不允许流入 P5 进行二次过滤审计（会导致时限迟滞及洗白），修改时严禁引入任何 P5 依赖。
2. 对于 Grok 幻觉推文 ID 查杀逻辑（如 12345/23456 连号拦截），属于关键抗污染屏障，不得删除。
3. 处理模型分批轮询（Chunked Calling）时，严禁合并请求以图省事，因为超过 5 个赛道塞入单次 Prompt 会引起严重的注意力涣散和选择性失明症。
"""

import os
import time
import random
import logging
import requests
from datetime import datetime
import re

from settings import get_today_output_dir, load_prompt, GROK_API_KEY
from industry_dict import OLIGARCH_DICT, SUPER_GIANTS

GROK_BASE_URL = 'https://api.x.ai'
GROK_MODEL = 'grok-4-1-fast-reasoning'

INDUSTRY_SCAN_KEYWORDS = [
    'NVIDIA GPU AI chip supply chain',
    'TSMC advanced packaging CoWoS HBM',
    'Broadcom ASIC custom chip',
    'ASML EUV lithography semiconductor equipment',
    'SK Hynix Samsung Micron HBM memory TSV TC-NCF MR-MUF ALD precursor EMC',
    'AI server datacenter cooling liquid cooling cold plate CDU manifold fluorinated coolant quick disconnect',
    'optical module CPO silicon photonics 800G 1.6T EML VCSEL Faraday isolator',
    'PCB CCL high frequency material AI server copper foil electronic glass fiber PTFE resin ABF substrate',
    'advanced packaging hybrid bonding chiplet',
    'edge AI inference chip Qualcomm MediaTek',
    'AI infrastructure capex cloud hyperscaler',
    'semiconductor test Advantest probe card',
    'fiber optic cable telecom infrastructure',
    'China semiconductor self-sufficiency SMIC Huawei',
    'AI model training inference GPU shortage supply'
]

def _build_user_prompt(backtrack: str = '24h', keyword_chunk: list = None) -> str:
    if 'h' in backtrack:
        hours = int(backtrack.replace('h', ''))
    else:
        hours = 24

    targets = keyword_chunk if keyword_chunk else INDUSTRY_SCAN_KEYWORDS
    keyword_section = '\n'.join([f'  - {kw}' for kw in targets])

    template = load_prompt('p2_user.txt')
    return template.format(hours=hours, keyword_section=keyword_section, current_time=datetime.now().strftime('%Y-%m-%d %H:%M'))


def collect_from_x(backtrack: str = '24h', max_retries: int = 3) -> tuple[str, dict]:
    stats = {
        'source': 'X(Twitter) via Grok',
        'model': GROK_MODEL,
        'status': 'pending',
        'duration_seconds': 0,
        'content_length': 0
    }

    if not GROK_API_KEY:
        logging.warning('⚠️ [P2] GROK_API_KEY 未配置，跳过 X 平台采集')
        stats['status'] = 'skipped_no_key'
        return '', stats

    logging.info(f'📱 [P2] 启动 X(Twitter) 产业链情报采集 (Grok {GROK_MODEL})...')
    logging.info(f'  回溯窗口: {backtrack}')

    user_prompt = _build_user_prompt(backtrack)
    raw_system_prompt = load_prompt('p2_system.txt')

    track_lines = []
    for i, (track, companies) in enumerate(OLIGARCH_DICT.items(), 1):
        companies_str = '/'.join(companies)
        track_lines.append(f'{i}. {track} ({companies_str})')

    industry_tracks = '\n'.join(track_lines)
    super_giants_str = ', '.join(SUPER_GIANTS)

    system_prompt_content = raw_system_prompt.replace('{{track_count}}', str(len(OLIGARCH_DICT)))
    system_prompt_content = system_prompt_content.replace('{{industry_tracks}}', industry_tracks)
    system_prompt_content = system_prompt_content.replace('{{super_giants}}', super_giants_str)

    headers = {
        'Authorization': f'Bearer {GROK_API_KEY}',
        'Content-Type': 'application/json'
    }

    CHUNK_SIZE = 5
    chunks = [INDUSTRY_SCAN_KEYWORDS[i:i + CHUNK_SIZE] for i in range(0, len(INDUSTRY_SCAN_KEYWORDS), CHUNK_SIZE)]

    all_filtered_leaks = []
    overall_start_time = time.time()

    for c_idx, chunk in enumerate(chunks, 1):
        logging.info(f'📦 [P2] 开始处理检索分批 {c_idx}/{len(chunks)}，包含 {len(chunk)} 个赛道词...')
        user_prompt = _build_user_prompt(backtrack, chunk)

        payload = {
            'model': GROK_MODEL,
            'input': [
                {'role': 'system', 'content': system_prompt_content},
                {'role': 'user', 'content': user_prompt}
            ],
            'tools': [{'type': 'x_search'}],
            'temperature': 0.7,
            'prompt_cache_key': 'vcp-hunter-l1a-daily-batch'
        }

        for attempt in range(max_retries):
            try:
                logging.info(f'🔄   -> 正在请求 Grok API (第 {attempt+1}/{max_retries} 次尝试)...')
                response = requests.post(
                    f'{GROK_BASE_URL}/v1/responses',
                    json=payload,
                    headers=headers,
                    timeout=180
                )

                if response.status_code in (401, 403):
                    logging.error(f'❌   -> API 鉴权失败 (HTTP {response.status_code})')
                    stats['status'] = f'auth_failed_{response.status_code}'
                    return '', stats

                if response.status_code == 429:
                    # Why: 渐进退避，从 15s 起步，上限 120s，而非第一次就等 120s
                    wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
                    logging.warning(f'⏳   -> 触发限流(429)，等待 {wait:.1f}s')
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()

                usage = data.get('usage', {})
                total_tok = usage.get('total_tokens', 0)
                reason_tok = usage.get('reasoning_tokens', 0)
                input_dtls = usage.get('input_tokens_details', {})
                cache_tok = input_dtls.get('cached_tokens', 0)

                logging.info(f'💡   -> 本次耗费 Tokens: {total_tok} (内部推演: {reason_tok}, 命中免单缓存: {cache_tok})')
                stats['total_tokens'] = stats.get('total_tokens', 0) + total_tok
                stats['reasoning_tokens'] = stats.get('reasoning_tokens', 0) + reason_tok
                stats['cached_tokens'] = stats.get('cached_tokens', 0) + cache_tok

                content = ''
                if 'output' in data:
                    for item in data['output']:
                        if item.get('role') == 'assistant' and item.get('type') == 'message':
                            for c in item.get('content', []):
                                if c.get('type') == 'output_text':
                                    content += c.get('text', '')

                if not content:
                    raise ValueError('XAI API返回空内容')

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
                        continue
                    if '</x_leaks>' in line:
                        if in_block:
                            block_buffer.append(line)
                            if block_valid:
                                filtered_content.extend(block_buffer)
                        in_block = False
                        block_buffer = []
                        continue
                    if in_block:
                        block_buffer.append(line)
                        match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/]+/status/(\d+)', line)
                        if match:
                            tweet_id = match.group(1)
                            if re.search(r'(01234|12345|23456|34567|45678|56789|67890|98765|87654|76543|65432|54321|43210|0000|1111)', tweet_id):
                                block_valid = False
                                logging.warning(f'🚫   -> 剔除幻觉：连号假ID({tweet_id})')

                chunk_final = '\n'.join(filtered_content)
                if chunk_final.strip():
                    all_filtered_leaks.append(chunk_final)
                    logging.info(f'✅   -> 本批次获取有效爆料，长度: {len(chunk_final)}')

                break

            except requests.exceptions.Timeout:
                wait = 3
                logging.warning(f'⚠️   -> 请求超时，等待 {wait}s ({attempt+1}/{max_retries})')
                time.sleep(wait)
            except Exception as e:
                wait = 3
                logging.error(f'❌   -> 未知错误 ({e})，等待 {wait}s ({attempt+1}/{max_retries})')
                time.sleep(wait)

    content = '\n\n'.join(all_filtered_leaks)

    if not content.strip() or '<x_leaks>' not in content:
        logging.warning('⚠️ [P2] 所有批次均未获取到有效情报（全部作废）。')
        stats['status'] = 'failed_empty'
        return '', stats

    elapsed = time.time() - overall_start_time
    stats['status'] = 'success'
    stats['duration_seconds'] = round(elapsed, 1)
    stats['content_length'] = len(content)

    logging.info(f'✅ [P2] X 平台情报分批采集完成: 共 {len(chunks)} 批, 总计 {len(content)} 字, 总耗时 {elapsed:.1f}s')

    formatted = f'============================================================\n以下数据来自 X(Twitter) 平台实时分批采集（Grok {GROK_MODEL}）\n信息来源: X平台产业链insider/leaker/分析师/官方账号\n采集时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n⚠️ 注意: X平台信息未经 P5 审计，可信度参差不齐，需结合其他数据源交叉验证\n============================================================\n\n{content}'

    _save_p2_output(formatted, stats)
    return formatted, stats


def _save_p2_output(content: str, stats: dict) -> None:
    try:
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        output_dir = get_today_output_dir()
        filename = os.path.join(output_dir, f'P2_X平台情报_{now_str}.md')

        with open(filename, 'w', encoding='utf-8') as f:
            f.write('# P2 X(Twitter) 产业链情报\n\n')
            f.write(f'> 采集时间: {datetime.now().isoformat()}\n')
            f.write(f"> 模型: {stats.get('model', GROK_MODEL)}\n")
            f.write(f"> 耗时: {stats.get('duration_seconds', 0)}s\n")
            f.write(f"> 内容长度: {stats.get('content_length', 0)} 字\n\n")
            f.write(content)

        logging.info(f'[P2] 原始数据已保存: {filename}')
    except Exception as e:
        logging.warning(f'[P2] 保存失败: {e}')
