"""
P2 X(Twitter) 产业链情报采集 — 使用 Grok API 抓取 X 平台 AI 产业链爆料。

设计原理：
  - Grok 天生拥有对 X 平台实时数据的原生访问能力，这是其他 LLM 不具备的独家优势
  - 通过精心设计的 system prompt 让 Grok 扮演产业链情报分析师，
    在 X 上按新版核心细分赛道搜刮最新的产业链爆料、insider leak、供应链异动
  - P2 的输出不经过 P5 审计（因为 Grok 本身就是一手信源提取器，
    审计会损失 X 独有的即时性和爆料性信息）
  - P2 输出直接与 P5 审计后数据拼接，一起送给 GPT-5.6 Sol Max 做 P6 映射

连接方式：
  - 走本机翻墙代理（与 Gemini Search 直连共享同一个自动探测端口）
  - xAI 原生 REST API（直连 xAI 服务器，无任何中间代理）
  - 端点：https://api.x.ai/v1/responses

【⚠️核心维护规则 / Agent 指令】
1. P2 作为直连 Grok 的“第一手推特爆料池”，按架构规矩绝对不允许流入 P5 进行二次过滤审计（会导致时限迟滞及洗白），修改时严禁引入任何 P5 依赖。
2. 对于 Grok 幻觉推文 ID 查杀逻辑（如 12345/23456 连号拦截），属于关键抗污染屏障，不得删除。
3. 处理模型分批轮询（Chunked Calling）时，严禁合并请求以图省事，因为超过 5 个赛道塞入单次 Prompt 会引起严重的注意力涣散和选择性失明症。
"""

import time
import random
import logging
import requests
import os
from datetime import datetime
import re

from artifact_store import update_run_manifest, write_json_artifact, write_text_artifact
from intel_structuring import parse_p2_structured_items, render_p2_structured_markdown
from utils import parse_backtrack_hours
from settings import require_prompt, GROK_API_KEY
from industry_dict import OLIGARCH_DICT, SUPER_GIANTS, STRATEGIC_GIANTS


def _env_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, '').strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        logging.warning('%s is not a positive integer: %s', name, raw_value)
        return default
    if parsed <= 0:
        logging.warning('%s must be positive: %s', name, raw_value)
        return default
    return parsed


GROK_BASE_URL = os.environ.get('GROK_BASE_URL', 'https://api.x.ai').strip().rstrip('/')
GROK_FALLBACK_BASE_URL = os.environ.get('GROK_FALLBACK_BASE_URL', 'https://cldapi.com').strip().rstrip('/')
GROK_FALLBACK_API_KEY = os.environ.get('GROK_FALLBACK_API_KEY', '').strip()
GROK_FALLBACK_MODEL = os.environ.get('GROK_FALLBACK_MODEL', 'grok-4.5').strip() or 'grok-4.5'
GROK_MODEL = os.environ.get('GROK_MODEL', 'grok-4.3').strip() or 'grok-4.3'
GROK_REASONING_EFFORT = os.environ.get('GROK_REASONING_EFFORT', 'none').strip().lower() or 'none'
GROK_MAX_OUTPUT_TOKENS = _env_positive_int('GROK_MAX_OUTPUT_TOKENS', 4096)
P2_PRIMARY_BUDGET_SECONDS = 300
P2_FALLBACK_BUDGET_SECONDS = 120
P2_TOTAL_BUDGET_SECONDS = P2_PRIMARY_BUDGET_SECONDS + P2_FALLBACK_BUDGET_SECONDS
P2_REQUEST_TIMEOUT_SECONDS = 75
P2_MIN_REMAINING_SECONDS = 10
_X_LEAK_BLOCK_PATTERN = re.compile(r'<x_leaks>.*?</x_leaks>', flags=re.DOTALL)
_X_STATUS_ID_PATTERN = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/]+/status/(\d+)')
_FAKE_TWEET_ID_PATTERN = re.compile(
    r'(01234|12345|23456|34567|45678|56789|67890|98765|87654|76543|65432|54321|43210|0000|1111)'
)

INDUSTRY_SCAN_KEYWORDS = [
    'Synopsys Cadence ARM Siemens EDA semiconductor IP',
    'NVIDIA AMD Broadcom Marvell Alchip Qualcomm AI accelerator ASIC',
    'TSMC Samsung Foundry Intel Foundry GlobalFoundries advanced node',
    'ASML Applied Materials Lam Research Tokyo Electron KLA Onto semiconductor equipment metrology',
    'Shin-Etsu SUMCO Soitec IQE Entegris silicon wafer specialty materials',
    'SK Hynix Samsung Micron HBM memory TSV TC-NCF MR-MUF',
    'Besi EV Group ASMPT Disco advanced packaging hybrid bonding chiplet',
    'IC substrate ABF Ajinomoto Ibiden Unimicron glass substrate',
    'Arista Broadcom NVIDIA Ethernet switching Astera Credo Amphenol TE Connectivity interconnect',
    'optical module optical engine 800G 1.6T 3.2T Coherent Fabrinet Ciena',
    'optical chip silicon photonics EML VCSEL laser photodiode modulator CPO Coherent Lumentum Intel Silicon Photonics Ayar Labs Lightmatter',
    'optical module IC DSP TIA driver CDR SerDes PAM4 Broadcom Marvell Semtech MACOM MaxLinear Credo',
    'optical passive components fiber array connector isolator lens ferrule FAU MPO MTP Amphenol TE Connectivity Corning Fujikura US Conec SENKO Molex',
    'PCB CCL high frequency material AI server copper foil electronic glass fiber PTFE resin ABF substrate',
    'AI PCB equipment LDI laser direct imaging laser drilling VCP plating AOI AVI electrical test PCB micro drill bits Atotech ESI Orbotech',
    'datacenter power distribution Vertiv Eaton Schneider nVent Delta MPS',
    'datacenter liquid cooling cold plate CDU manifold fluorinated coolant quick disconnect Auras AVC CoolIT',
    'semiconductor test Advantest Teradyne FormFactor probe card Accretech MPI Technoprobe',
    'non-GPU AI compute Groq Cerebras Tenstorrent',
    'AI cloud capex hyperscaler CoreWeave Oracle AWS Azure Google Meta Scale AI',
    'edge AI inference chip Apple Qualcomm MediaTek Intel',
    'embodied AI robotics Tesla Figure NVIDIA robotics',
    'fiber optic cable broadband access Ciena Calix ADTRAN Prysmian Corning Vistance'
]

def _build_user_prompt(backtrack: str = '24h', keyword_chunk: list = None) -> str:
    hours = parse_backtrack_hours(backtrack)

    targets = keyword_chunk if keyword_chunk else INDUSTRY_SCAN_KEYWORDS
    keyword_section = '\n'.join([f'  - {kw}' for kw in targets])

    template = require_prompt('p2_user.txt')
    return template.format(hours=hours, keyword_section=keyword_section, current_time=datetime.now().strftime('%Y-%m-%d %H:%M'))


def _remaining_budget_seconds(start_time: float, max_total_seconds: int | float) -> float:
    if max_total_seconds <= 0:
        return float('inf')
    return max_total_seconds - (time.time() - start_time)


def _bounded_request_timeout(
    start_time: float,
    max_total_seconds: int | float,
    request_timeout_seconds: int | float,
) -> float:
    remaining = _remaining_budget_seconds(start_time, max_total_seconds)
    if remaining == float('inf'):
        return float(request_timeout_seconds)
    if remaining <= P2_MIN_REMAINING_SECONDS:
        return 0
    return max(5.0, min(float(request_timeout_seconds), remaining - P2_MIN_REMAINING_SECONDS))


def _sleep_with_budget(wait: float, start_time: float, max_total_seconds: int | float) -> bool:
    remaining = _remaining_budget_seconds(start_time, max_total_seconds)
    if remaining != float('inf'):
        wait = min(wait, max(0.0, remaining - P2_MIN_REMAINING_SECONDS))
    if wait <= 0:
        return False
    time.sleep(wait)
    return True


def _filter_x_leak_blocks(content: str, seen_tweet_ids: set[str]) -> str:
    filtered_blocks: list[str] = []
    for match in _X_LEAK_BLOCK_PATTERN.finditer(content or ''):
        block = match.group(0).strip()
        block_valid = True
        for tweet_id in _X_STATUS_ID_PATTERN.findall(block):
            if tweet_id in seen_tweet_ids:
                block_valid = False
                logging.info(f'🔄   -> 剔除跨批次重复爆发：重复ID({tweet_id})')
            seen_tweet_ids.add(tweet_id)
            if _FAKE_TWEET_ID_PATTERN.search(tweet_id):
                block_valid = False
                logging.warning(f'🚫   -> 剔除幻觉：连号假ID({tweet_id})')
        if block_valid:
            filtered_blocks.append(block)
    return '\n'.join(filtered_blocks)


def collect_from_x(  # noqa: C901 - chunk, retry, and independent failover budgets share one state machine
    backtrack: str = '24h',
    max_retries: int = 3,
    *,
    max_total_seconds: int = P2_PRIMARY_BUDGET_SECONDS,
    request_timeout_seconds: int = P2_REQUEST_TIMEOUT_SECONDS,
    fallback_total_seconds: int = P2_FALLBACK_BUDGET_SECONDS,
) -> tuple[str, dict]:
    stats = {
        'source': 'X(Twitter) via Grok',
        'model': GROK_MODEL,
        'attempted_models': [GROK_MODEL],
        'status': 'pending',
        'duration_seconds': 0,
        'content_length': 0,
        'reasoning_effort': GROK_REASONING_EFFORT,
        'max_output_tokens': GROK_MAX_OUTPUT_TOKENS,
    }

    if not GROK_API_KEY:
        logging.warning('⚠️ [P2] GROK_API_KEY 未配置，跳过 X 平台采集')
        stats['status'] = 'skipped_no_key'
        update_run_manifest('p2', status='skipped_no_key', metrics=stats)
        return '', stats

    logging.info(f'📱 [P2] 启动 X(Twitter) 产业链情报采集 (Grok {GROK_MODEL})...')
    logging.info(f'  回溯窗口: {backtrack}')
    update_run_manifest(
        'p2',
        status='running',
        metrics={
            **stats,
            'primary_budget_seconds': max_total_seconds,
            'fallback_budget_seconds': fallback_total_seconds,
        },
    )

    try:
        # Why: 先显式验证用户 Prompt 存在，实际检索 prompt 在分批循环内按 chunk 重建
        _build_user_prompt(backtrack)
        raw_system_prompt = require_prompt('p2_system.txt')
    except FileNotFoundError as e:
        logging.error(f'🚨 [P2] 关键 Prompt 缺失: {e}')
        stats['status'] = 'failed_missing_prompt'
        update_run_manifest('p2', status='failed_missing_prompt', metrics={**stats, 'error': str(e)})
        return '', stats

    track_lines = []
    for i, (track, companies) in enumerate(OLIGARCH_DICT.items(), 1):
        companies_str = '/'.join(companies)
        track_lines.append(f'{i}. {track} ({companies_str})')

    industry_tracks = '\n'.join(track_lines)
    super_giants_str = ', '.join(sorted(SUPER_GIANTS))
    strategic_giants_str = ', '.join(sorted(STRATEGIC_GIANTS))

    system_prompt_content = raw_system_prompt.replace('{{track_count}}', str(len(OLIGARCH_DICT)))
    system_prompt_content = system_prompt_content.replace('{{industry_tracks}}', industry_tracks)
    system_prompt_content = system_prompt_content.replace('{{super_giants}}', super_giants_str)
    system_prompt_content = system_prompt_content.replace('{{strategic_giants}}', strategic_giants_str)

    headers = {
        'Authorization': f'Bearer {GROK_API_KEY}',
        'Content-Type': 'application/json'
    }

    CHUNK_SIZE = 5
    chunks = [INDUSTRY_SCAN_KEYWORDS[i:i + CHUNK_SIZE] for i in range(0, len(INDUSTRY_SCAN_KEYWORDS), CHUNK_SIZE)]

    all_filtered_leaks = []
    seen_tweet_ids = set()
    overall_start_time = time.time()
    fallback_start_time = None
    attempted_chunks = 0
    successful_chunks = 0
    fallback_chunks = 0
    fallback_only = False
    stop_reason = ''

    for c_idx, chunk in enumerate(chunks, 1):
        primary_remaining = _remaining_budget_seconds(overall_start_time, max_total_seconds)
        fallback_remaining = (
            float(fallback_total_seconds)
            if fallback_start_time is None
            else _remaining_budget_seconds(fallback_start_time, fallback_total_seconds)
        )
        primary_available = not fallback_only and primary_remaining > P2_MIN_REMAINING_SECONDS
        fallback_available = bool(GROK_FALLBACK_API_KEY) and (
            fallback_remaining > P2_MIN_REMAINING_SECONDS
        )
        if not primary_available and not fallback_available:
            stop_reason = 'budget_exhausted'
            logging.warning(
                '[P2] 主备预算均已耗尽，停止后续分批: attempted=%s/%s primary=%.1fs fallback=%.1fs',
                attempted_chunks,
                len(chunks),
                max(0.0, primary_remaining),
                max(0.0, fallback_remaining),
            )
            break

        attempted_chunks = c_idx
        logging.info(f'📦 [P2] 开始处理检索分批 {c_idx}/{len(chunks)}，包含 {len(chunk)} 个赛道词...')
        user_prompt = _build_user_prompt(backtrack, chunk)

        payload = {
            'model': GROK_MODEL,
            'input': [
                {'role': 'system', 'content': system_prompt_content},
                {'role': 'user', 'content': user_prompt}
            ],
            'tools': [{'type': 'x_search'}],
            'max_output_tokens': GROK_MAX_OUTPUT_TOKENS,
            'temperature': 0.7,
            'prompt_cache_key': 'vcp-hunter-l1a-daily-batch'
        }
        if GROK_REASONING_EFFORT not in {'', '0', 'false', 'off', 'disabled'}:
            payload['reasoning'] = {'effort': GROK_REASONING_EFFORT}

        primary_success = False
        response = None
        err_msg = ""
        stop_reason = ""

        # 1. Primary official API attempt loop
        if not primary_available:
            if fallback_only:
                err_msg = 'primary_unavailable_latched'
                logging.info('[P2] 主模型已判定不可用，当前分批直接使用备胎')
            else:
                err_msg = 'primary_budget_exhausted'
                logging.warning('[P2] 主模型预算已耗尽，当前及后续分批直接尝试备胎')
        for attempt in (range(max_retries) if primary_available else ()):
            try:
                request_timeout = _bounded_request_timeout(
                    overall_start_time,
                    max_total_seconds,
                    request_timeout_seconds,
                )
                if request_timeout <= 0:
                    stop_reason = 'budget_exhausted'
                    logging.warning('[P2] 剩余预算不足，跳过当前 Grok 请求')
                    break
                
                logging.info(f'🔄   -> 正在请求官方 Grok API (第 {attempt+1}/{max_retries} 次尝试)...')
                response = requests.post(
                    f'{GROK_BASE_URL}/v1/responses',
                    json=payload,
                    headers=headers,
                    timeout=request_timeout
                )
                
                if response.status_code == 200:
                    primary_success = True
                    break
                
                # Check for permanent authentication errors (no need to retry primary)
                is_auth_error = response.status_code in (401, 403) or (
                    response.status_code == 400 and "Incorrect API key" in response.text
                )
                if is_auth_error:
                    err_msg = f"HTTP {response.status_code}: {response.text}"
                    logging.error(f'❌   -> 官方 API 鉴权失败 ({err_msg})，将尝试切换备用中转')
                    break
                
                # Handle transient 429 rate limit
                if response.status_code == 429:
                    wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
                    logging.warning(f'⏳   -> 官方 API 触发限流(429)，等待 {wait:.1f}s 重试...')
                    if not _sleep_with_budget(wait, overall_start_time, max_total_seconds):
                        stop_reason = 'budget_exhausted'
                        break
                    continue
                
                # Other non-200 errors
                err_msg = f"HTTP {response.status_code}: {response.text}"
                wait = 3
                logging.warning(f'⚠   -> 官方 API 失败 ({err_msg})，等待 {wait}s 后重试...')
                if not _sleep_with_budget(wait, overall_start_time, max_total_seconds):
                    stop_reason = 'budget_exhausted'
                    break
                    
            except Exception as e:
                err_msg = str(e)
                wait = 3
                logging.warning(f'⚠   -> 官方 API 网络异常 ({err_msg})，等待 {wait}s 后重试...')
                if not _sleep_with_budget(wait, overall_start_time, max_total_seconds):
                    stop_reason = 'budget_exhausted'
                    break
        
        # 2. Fallback attempt if primary failed
        if not primary_success and GROK_FALLBACK_API_KEY:
            logging.warning("⚠   -> 官方 Grok API 最终失败或鉴权失效，正尝试切换至备用中转站...")
            if not fallback_only:
                fallback_only = True
                stats['fallback_only_latched'] = True
            if fallback_start_time is None:
                fallback_start_time = time.time()
            if GROK_FALLBACK_MODEL not in stats['attempted_models']:
                stats['attempted_models'].append(GROK_FALLBACK_MODEL)
            stats['fallback_model'] = GROK_FALLBACK_MODEL
            stop_reason = ''
            fallback_headers = {
                'Authorization': f'Bearer {GROK_FALLBACK_API_KEY}',
                'Content-Type': 'application/json'
            }
            fallback_payload = dict(payload)
            fallback_payload['model'] = GROK_FALLBACK_MODEL
            
            fallback_endpoint = GROK_FALLBACK_BASE_URL
            if fallback_endpoint.endswith('/v1'):
                fallback_url = f'{fallback_endpoint}/responses'
            else:
                fallback_url = f'{fallback_endpoint}/v1/responses'
            
            for f_attempt in range(2):  # Try fallback up to 2 times
                try:
                    request_timeout = _bounded_request_timeout(
                        fallback_start_time,
                        fallback_total_seconds,
                        request_timeout_seconds,
                    )
                    if request_timeout <= 0:
                        stop_reason = 'fallback_budget_exhausted'
                        break
                    
                    logging.info(f"🔄   -> 正在请求中转站 Grok API ({GROK_FALLBACK_MODEL}) (第 {f_attempt+1}/2 次尝试)...")
                    fallback_response = requests.post(
                        fallback_url,
                        json=fallback_payload,
                        headers=fallback_headers,
                        timeout=request_timeout
                    )
                    response = fallback_response
                    if fallback_response.status_code == 200:
                        primary_success = True
                        fallback_chunks += 1
                        stop_reason = ''
                        logging.info("✅   -> 中转站 Grok 响应成功！")
                        break
                    else:
                        err_msg = f"Fallback HTTP {fallback_response.status_code}: {fallback_response.text}"
                        if fallback_response.status_code in (401, 403):
                            logging.error(f'❌   -> 中转站 API 鉴权失败 (HTTP {fallback_response.status_code})')
                            break
                        # Sleep before retry
                        wait = 3
                        if not _sleep_with_budget(wait, fallback_start_time, fallback_total_seconds):
                            stop_reason = 'fallback_budget_exhausted'
                            break
                except Exception as fe:
                    err_msg = f"Fallback Error: {fe}"
                    wait = 3
                    if not _sleep_with_budget(wait, fallback_start_time, fallback_total_seconds):
                        stop_reason = 'fallback_budget_exhausted'
                        break

        # 3. Process the final result
        if not primary_success:
            status_code = response.status_code if response is not None else 500
            if status_code in (401, 403):
                stop_reason = f'auth_failed_{status_code}'
                if not all_filtered_leaks:
                    stats['status'] = stop_reason
                    update_run_manifest('p2', status=stop_reason, metrics=stats)
                    return '', stats
            else:
                if all_filtered_leaks:
                    stop_reason = stop_reason or 'failed_empty'
                else:
                    stats['status'] = (
                        'failed_timeout'
                        if stop_reason in {'budget_exhausted', 'fallback_budget_exhausted'}
                        else 'failed_empty'
                    )
                    if stop_reason:
                        stats['stop_reason'] = stop_reason
                    update_run_manifest('p2', status=stats['status'], metrics=stats)
                    return '', stats
            break

        try:
            response.raise_for_status()
            data = response.json()

            usage = data.get('usage', {})
            total_tok = usage.get('total_tokens', 0)
            input_tok = usage.get('input_tokens', 0)
            output_tok = usage.get('output_tokens', 0)
            input_dtls = usage.get('input_tokens_details', {})
            output_dtls = usage.get('output_tokens_details', {})
            reason_tok = usage.get('reasoning_tokens', 0) or output_dtls.get('reasoning_tokens', 0)
            cache_tok = input_dtls.get('cached_tokens', 0)

            logging.info(
                '💡   -> 本次耗费 Tokens: %s (输入: %s, 输出: %s, 内部推演: %s, 命中免单缓存: %s)',
                total_tok,
                input_tok,
                output_tok,
                reason_tok,
                cache_tok,
            )
            stats['total_tokens'] = stats.get('total_tokens', 0) + total_tok
            stats['input_tokens'] = stats.get('input_tokens', 0) + input_tok
            stats['output_tokens'] = stats.get('output_tokens', 0) + output_tok
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

            chunk_final = _filter_x_leak_blocks(content, seen_tweet_ids)
            if chunk_final.strip():
                all_filtered_leaks.append(chunk_final)
                successful_chunks += 1
                logging.info(f'✅   -> 本批次获取有效爆料，长度: {len(chunk_final)}')

        except Exception as e:
            wait = 3
            logging.error(f'❌   -> 解析返回数据错误 ({e})，等待 {wait}s')
            if not _sleep_with_budget(wait, overall_start_time, max_total_seconds):
                stop_reason = 'budget_exhausted'
                break
        if stop_reason:
            break

    content = '\n\n'.join(all_filtered_leaks)
    elapsed = time.time() - overall_start_time
    stats['duration_seconds'] = round(elapsed, 1)
    stats['attempted_chunks'] = attempted_chunks
    stats['successful_chunks'] = successful_chunks
    stats['total_chunks'] = len(chunks)
    stats['max_total_seconds'] = max_total_seconds
    stats['primary_budget_seconds'] = max_total_seconds
    stats['fallback_budget_seconds'] = fallback_total_seconds
    stats['fallback_chunks'] = fallback_chunks

    if not content.strip() or '<x_leaks>' not in content:
        logging.warning('⚠️ [P2] 所有批次均未获取到有效情报（全部作废）。')
        stats['status'] = (
            'failed_timeout'
            if stop_reason in {'budget_exhausted', 'fallback_budget_exhausted'}
            else 'failed_empty'
        )
        if stop_reason:
            stats['stop_reason'] = stop_reason
        update_run_manifest('p2', status=stats['status'], metrics=stats)
        return '', stats

    structured_items = parse_p2_structured_items(content)
    if stop_reason in {'budget_exhausted', 'fallback_budget_exhausted'}:
        stats['status'] = 'partial_timeout'
    elif stop_reason:
        stats['status'] = f'partial_{stop_reason}'
    else:
        stats['status'] = 'success'
    if stop_reason:
        stats['stop_reason'] = stop_reason
    stats['content_length'] = len(content)
    stats['structured_items'] = len(structured_items)

    logging.info(
        '✅ [P2] X 平台情报分批采集完成: 成功 %s/%s 批, 总计 %s 字, 总耗时 %.1fs, status=%s',
        successful_chunks,
        len(chunks),
        len(content),
        elapsed,
        stats['status'],
    )

    formatted = f'============================================================\n以下数据来自 X(Twitter) 平台实时分批采集（Grok {GROK_MODEL}）\n信息来源: X平台产业链insider/leaker/分析师/官方账号\n采集时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n⚠️ 注意: X平台信息未经 P5 审计，可信度参差不齐，需结合其他数据源交叉验证\n============================================================\n\n{content}'

    if structured_items:
        formatted = render_p2_structured_markdown(
            structured_items,
            model=GROK_MODEL,
            collected_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    output_path, json_path = _save_p2_artifacts(formatted, structured_items, stats)
    manifest_status = (
        'completed'
        if stats['status'] == 'success'
        else 'completed_structured_degraded'
    )
    update_run_manifest(
        'p2',
        status=manifest_status,
        files={k: v for k, v in {'md': output_path, 'json': json_path}.items() if v} or None,
        metrics=stats,
    )
    return formatted, stats


def _save_p2_artifacts(content: str, structured_items: list[dict], stats: dict) -> tuple[str, str]:
    filename = ''
    json_filename = ''
    try:
        md_body = (
            '# P2 X(Twitter) 结构化情报\n\n'
            f'> 采集时间: {datetime.now().isoformat()}\n'
            f"> 模型: {stats.get('model', GROK_MODEL)}\n"
            f"> 耗时: {stats.get('duration_seconds', 0)}s\n"
            f"> 原始长度: {stats.get('content_length', 0)} 字\n"
            f"> 结构化条目: {stats.get('structured_items', 0)}\n\n"
            f'{content}'
        )
        filename = write_text_artifact('P2_X平台情报', md_body)
    except Exception as e:
        logging.warning(f'[P2] Markdown 保存失败: {e}')

    try:
        json_filename = write_json_artifact(
            'P2_X平台情报',
            {
                'generated_at': datetime.now().isoformat(),
                'model': stats.get('model', GROK_MODEL),
                'items': structured_items,
            },
        )
    except Exception as e:
        logging.warning(f'[P2] JSON 保存失败: {e}')

    if filename:
        logging.info(f'[P2] 结构化情报已保存: {filename}')
    return filename, json_filename
