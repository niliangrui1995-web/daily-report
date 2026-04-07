"""
P3 机动搜索引擎 — Gemini 2.5 联网搜索三锦囊。

设计原理：
  - P3 是纯 AI 搜索引擎，100% 依赖 Gemini 的联网搜索能力（Google Search Grounding）
  - 不使用任何传统爬虫（无 Scrapling、无 requests 正文抓取）
  - 三个锦囊各自独立，锦囊二/三可与 P1 并行启动，锦囊一需等 P1 完成后获取阵亡名单
  - 输出不经过 P5 审计层（AI 精炼后的情报无需"AI 审计 AI"）
  - 走 P2 的"VIP 快车道"，直接拼入 P6 输入

连接方式：
  - 复用 settings.py 的 Gemini Search 配置（直连 Google / CF Worker 双通道）
  - 复用 search_engine.py 的 Key 轮换 + 熔断器机制

【⚠️核心维护规则 / Agent 指令】
1. P3 是专属于 Gemini Search Grounding 的“纯机动搜索引擎”，修改时严禁混入传统的 requests/Scrapling 静态爬虫逻辑（这是 P1 的活）。
2. P3（包括三大锦囊）的输出由于已经是经过大模型精炼的摘要情报，绝不能流入 P5 参与由另一个模型裁决的“二次审查”，必须直接放行进入 P6。
3. _call_gemini_search 里的通道切换和熔断机制（channel_breaker 和 rotator）是防死锁保命机制，严禁为了强行死磕而关闭超时截断。
"""

import concurrent.futures
import json
import logging
import os
import time
from datetime import datetime

from settings import SEARCH_API_KEY_POOL, get_today_output_dir, load_prompt
from utils import extract_json_from_llm_response
from search_engine import raw_gemini_search

PRIORITY_KEYWORDS = [
    'NVDA', 'NVIDIA', 'nvidia', 'HBM', 'hbm', 'CoWoS', 'cowos', 'COWOS', '涨价', 'shortage', '缺货', '价格上涨', 'price hike', 'price increase', '短缺', 'supply shortage', 'undersupply', '封装', 'packaging', 'advanced packaging', '先进封装', '光模块', 'optical module', 'transceiver', '800G', '1.6T', '液冷', 'liquid cooling', 'cold plate', 'CDU', '产能', 'capacity', 'fab', 'foundry', '产线', '扩产', 'expansion', 'ramp up', '量产', '良率', 'yield', 'defect rate', 'TSMC', '台积电', 'tsmc', 'ASML', 'asml', 'EUV', 'SK Hynix', 'SK海力士', '海力士', 'Samsung', '三星', 'Micron', '美光', 'Broadcom', '博通', 'AVGO', 'AMD', 'amd', 'Intel', 'intel', '英特尔', 'Qualcomm', '高通', 'Apple', '苹果', 'Microsoft', '微软', 'Google', '谷歌', 'Meta', 'meta', 'PCB', 'CCL', '覆铜板', '铜箔', '光刻胶', 'photoresist', '靶材', 'target material', '电子气体', 'electronic gas', 'NF3', 'WF6', '探针卡', 'probe card', '测试', 'test', 'CPO', 'silicon photonics', '硅光', 'chiplet', '异构集成', 'Hybrid Bonding', 'hybrid bonding', '混合键合', 'ABF', '载板', 'substrate', 'financial results', 'earnings', 'revenue', 'guidance', '财报', '营收', '指引'
]


def _call_gemini_search(prompt: str, max_retries: int = 3) -> str:
    return raw_gemini_search(prompt, max_retries=max_retries)


def _parse_json_response(raw_text: str) -> list[dict]:
    if not raw_text:
        return []
    try:
        result = extract_json_from_llm_response(raw_text, expect_type='list')
        if isinstance(result, list):
            return result
        return []
    except json.JSONDecodeError:
        logging.warning('[P3] 无法从 Gemini 响应中解析出 JSON 数组')
        return []


def _filter_priority_targets(failed_urls: list[dict], max_targets: int = 10) -> list[dict]:
    if not failed_urls:
        return []

    scored_targets = []
    for item in failed_urls:
        title = item.get('title', '')
        score = sum(1 for kw in PRIORITY_KEYWORDS if kw in title)
        if score > 0:
            scored_targets.append((item, score))

    scored_targets.sort(key=lambda x: x[1], reverse=True)
    selected = [item for item, _ in scored_targets[:max_targets]]

    if selected:
        logging.info(f'  [P3 锦囊一] 从 {len(failed_urls)} 个阵亡链接中筛选出 {len(selected)} 个高价值目标')
        return selected

    logging.info(f'  [P3 锦囊一] {len(failed_urls)} 个阵亡链接中无高价值目标（未命中优先级关键词），跳过空袭')
    return selected


def run_jinnang_1(failed_urls: list[dict]) -> list[dict]:
    targets = _filter_priority_targets(failed_urls)
    if not targets:
        return []

    logging.info(f'🎯 [P3 锦囊一] 启动动态空袭：{len(targets)} 个高价值目标')

    template = load_prompt('p3_jinnang1.txt')

    article_lines = []
    for i, target in enumerate(targets, 1):
        article_lines.append(f"{i}. 标题：{target.get('title', '未知')}\n   URL：{target.get('url', '未知')}\n   来源：{target.get('source', '未知')}")

    prompt = template.format(failed_articles='\n'.join(article_lines))

    all_results = []

    if len(targets) <= 5:
        raw_text = _call_gemini_search(prompt)
        all_results.extend(_parse_json_response(raw_text))
    else:
        mid = len(targets) // 2
        for batch_idx, batch in enumerate([targets[:mid], targets[mid:]], 1):
            batch_lines = []
            for i, target in enumerate(batch, 1):
                batch_lines.append(f"{i}. 标题：{target.get('title', '未知')}\n   URL：{target.get('url', '未知')}\n   来源：{target.get('source', '未知')}")

            batch_prompt = template.format(failed_articles='\n'.join(batch_lines))
            logging.info(f'  [锦囊一] 第 {batch_idx}/2 批空袭中...')
            raw_text = _call_gemini_search(batch_prompt)
            all_results.extend(_parse_json_response(raw_text))

            if batch_idx == 1:
                time.sleep(2)

    logging.info(f'✅ [P3 锦囊一] 空袭完成：抢救回 {len(all_results)} 条情报')
    return all_results


def run_jinnang_2(hours: int = 24) -> list[dict]:
    logging.info('🔭 [P3 锦囊二] 启动极客圈非共识扫雷...')
    template = load_prompt('p3_jinnang2.txt')
    prompt = template.format(hours=hours)
    raw_text = _call_gemini_search(prompt)
    results = _parse_json_response(raw_text)
    logging.info(f'✅ [P3 锦囊二] 扫雷完成：发现 {len(results)} 条非共识信号')
    return results


def run_jinnang_3(hours: int = 24) -> list[dict]:
    logging.info('🌋 [P3 锦囊三] 启动产业链异动侦测...')
    template = load_prompt('p3_jinnang3.txt')
    prompt = template.format(hours=hours)
    raw_text = _call_gemini_search(prompt)
    results = _parse_json_response(raw_text)
    logging.info(f'✅ [P3 锦囊三] 侦测完成：捕获 {len(results)} 条异动信号')
    return results


def format_p3_output(jinnang_1_results: list[dict], jinnang_2_results: list[dict], jinnang_3_results: list[dict]) -> str:
    sections = []
    sections.append('============================================================')
    sections.append('[P3 机动情报 - Gemini 联网搜索]')
    sections.append(f'采集时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    sections.append('（以下情报来自 Gemini 2.5 联网搜索，覆盖传统爬虫的盲区）')
    sections.append('============================================================')

    def _format_section(label: str, results: list[dict]) -> str:
        if not results:
            return f'\n{label}\n（本锦囊无有效情报产出）\n'

        lines = [f'\n{label}']
        for i, item in enumerate(results, 1):
            title = item.get('title', '未知标题')
            source_url = item.get('source_url', '')
            source_name = item.get('source_name', '未知来源')
            track = item.get('track', '综合')
            summary = item.get('summary', '')
            lines.append(f"{i}. [{track}] {title}\n   摘要：{summary}\n   来源：{source_name} | {source_url}")

        return '\n'.join(lines)

    sections.append(_format_section('[锦囊一·爬虫攻坚补漏]', jinnang_1_results))
    sections.append(_format_section('[锦囊二·非共识黑马发现]', jinnang_2_results))
    sections.append(_format_section('[锦囊三·产业链异动预警]', jinnang_3_results))

    sections.append('============================================================')
    return '\n'.join(sections)


def save_p3_output(formatted_text: str, stats: dict) -> None:
    try:
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        output_dir = get_today_output_dir()
        md_filename = os.path.join(output_dir, f'P3_机动情报_{now_str}.md')

        with open(md_filename, 'w', encoding='utf-8') as md_file:
            md_file.write('# P3 机动情报（Gemini 联网搜索）\n\n')
            md_file.write(f"> 采集时间: {datetime.now().isoformat()}\n")
            md_file.write(f"> 锦囊一: {stats.get('jinnang_1_count', 0)} 条 | 锦囊二: {stats.get('jinnang_2_count', 0)} 条 | 锦囊三: {stats.get('jinnang_3_count', 0)} 条\n")
            md_file.write(f"> 总耗时: {stats.get('duration_seconds', 0)}s\n\n")
            md_file.write(formatted_text)

        logging.info(f'[P3] Markdown 输出已保存: {md_filename}')

        json_filename = os.path.join(output_dir, f'P3_机动情报_{now_str}.json')
        with open(json_filename, 'w', encoding='utf-8') as json_file:
            json.dump({
                'time': datetime.now().isoformat(),
                'source': 'P3_Gemini_Search',
                'stats': stats,
                'raw_results': stats.get('raw_results', {})
            }, json_file, ensure_ascii=False, indent=2)

        logging.info(f'[P3] JSON 输出已保存: {json_filename}')
    except Exception as exc:
        logging.warning(f'[P3] 保存输出文件失败: {exc}')


def collect_from_p3(failed_urls: list[dict] = None, backtrack: str = '24h') -> tuple[str, dict]:
    start_time = time.time()
    stats = {
        'source': 'P3 Gemini Search',
        'status': 'pending',
        'jinnang_1_count': 0,
        'jinnang_2_count': 0,
        'jinnang_3_count': 0,
        'duration_seconds': 0,
        'content_length': 0
    }

    if not SEARCH_API_KEY_POOL:
        logging.warning('⚠️ [P3] GEMINI_SEARCH_API_KEYS 未配置，跳过机动搜索')
        stats['status'] = 'skipped_no_key'
        return '', stats

    logging.info('🚀 [P3] 启动机动情报搜索引擎（三锦囊）')

    if 'h' in backtrack:
        hours = int(backtrack.replace('h', ''))
    else:
        hours = 24

    jinnang_1_results = []
    if failed_urls is not None:
        jinnang_1_results = run_jinnang_1(failed_urls)
        stats['jinnang_1_count'] = len(jinnang_1_results)

        if jinnang_1_results:
            time.sleep(3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_2 = pool.submit(run_jinnang_2, hours)
        future_3 = pool.submit(run_jinnang_3, hours)

        jinnang_2_results = future_2.result(timeout=300)
        jinnang_3_results = future_3.result(timeout=300)

    stats['jinnang_2_count'] = len(jinnang_2_results)
    stats['jinnang_3_count'] = len(jinnang_3_results)

    total_count = stats['jinnang_1_count'] + stats['jinnang_2_count'] + stats['jinnang_3_count']
    if total_count == 0:
        logging.warning('⚠️ [P3] 三个锦囊全部空手而归，无情报产出')
        stats['status'] = 'empty'
        return '', stats

    formatted_text = format_p3_output(jinnang_1_results, jinnang_2_results, jinnang_3_results)
    elapsed = time.time() - start_time
    stats['status'] = 'success'
    stats['duration_seconds'] = round(elapsed, 1)
    stats['content_length'] = len(formatted_text)

    stats['raw_results'] = {
        'jinnang_1': jinnang_1_results,
        'jinnang_2': jinnang_2_results,
        'jinnang_3': jinnang_3_results
    }

    logging.info(f"✅ [P3] 机动情报搜索完成: 锦囊一 {stats['jinnang_1_count']} + 锦囊二 {stats['jinnang_2_count']} + 锦囊三 {stats['jinnang_3_count']} = 共 {total_count} 条, {len(formatted_text)} 字, 耗时 {elapsed:.1f}s")

    save_p3_output(formatted_text, stats)
    return formatted_text, stats


def collect_p3_parallel_only(backtrack: str = '24h') -> tuple[str, dict]:
    start_time = time.time()
    stats = {
        'source': 'P3 Gemini Search (parallel: jinnang 2+3)',
        'status': 'pending',
        'jinnang_2_count': 0,
        'jinnang_3_count': 0,
        'duration_seconds': 0,
        'content_length': 0
    }

    if not SEARCH_API_KEY_POOL:
        logging.warning('⚠️ [P3] GEMINI_SEARCH_API_KEYS 未配置，跳过并行锦囊')
        stats['status'] = 'skipped_no_key'
        return '', stats

    logging.info('🚀 [P3] 启动并行锦囊（锦囊二 + 锦囊三）')

    if 'h' in backtrack:
        hours = int(backtrack.replace('h', ''))
    else:
        hours = 24

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_2 = pool.submit(run_jinnang_2, hours)
        future_3 = pool.submit(run_jinnang_3, hours)

        jinnang_2_results = future_2.result(timeout=300)
        jinnang_3_results = future_3.result(timeout=300)

    stats['jinnang_2_count'] = len(jinnang_2_results)
    stats['jinnang_3_count'] = len(jinnang_3_results)

    elapsed = time.time() - start_time
    stats['duration_seconds'] = round(elapsed, 1)

    total = stats['jinnang_2_count'] + stats['jinnang_3_count']
    if total == 0:
        stats['status'] = 'empty'
        return '', stats

    formatted = format_p3_output([], jinnang_2_results, jinnang_3_results)
    stats['status'] = 'success'
    stats['content_length'] = len(formatted)

    stats['raw_results'] = {
        'jinnang_2': jinnang_2_results,
        'jinnang_3': jinnang_3_results
    }

    logging.info(f"✅ [P3 并行] 锦囊二 {stats['jinnang_2_count']} + 锦囊三 {stats['jinnang_3_count']} = 共 {total} 条, 耗时 {elapsed:.1f}s")

    return formatted, stats

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    mock_failed = [
        {'title': 'SemiAnalysis: NVIDIA Blackwell B200 Yield Improvements', 'url': 'https://www.semianalysis.com/p/nvidia-blackwell-yield', 'source': 'SemiAnalysis'},
        {'title': 'HBM4 Mass Production Timeline Update from SK Hynix', 'url': 'https://example.com/hbm4-update', 'source': 'TheElec_KR'}
    ]

    text, stats = collect_from_p3(failed_urls=mock_failed)
    print(f"\n===== P3 输出 ({len(text)} 字) =====\n")
    print(text[:2000] if text else "(空)")
    print(f"\n统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")
