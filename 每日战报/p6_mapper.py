"""
P6 Opus A 股映射 — 产业链情报→A股标的映射。

从 daily_report.py 拆分，包含：
- format_evidence(): 搜索数据格式化
- generate_report(): P6 Opus 映射（产业链情报→A股标的表格）

已拆分至独立模块（此处不再包含）：
- fetch_vanguard_prices → p4_vanguard.py
- get_run_config/infer_backtrack → run_config.py
- iterative_deep_search/run_deep_research → p7_deep_research.py

【⚠️核心维护规则 / Agent 指令】
1. `generate_report()` 的 A 股映射必须且仅以 `industry_dict.py` 中的合法标的为映射锚点，严禁自行新增口袋兜底赛道或硬编码不在寡头字典中的票。
2. `format_evidence()` 只做纯数据拼接格式化，严禁在这里加入筛选逻辑（筛选是 P5 审计的活）。
"""

import json
import re
import time
import logging
from datetime import datetime

from artifact_store import update_run_manifest, write_json_artifact, write_text_artifact
from settings import opus_adapter, OPUS_MODEL, require_prompt
from industry_dict import OLIGARCH_DICT
from utils import extract_json_from_llm_response
from p7_deep_research import build_recent_topic_guardrail

P6_JSON_RETRY_LIMIT = 1
P6_OUTPUT_LIMITS = """
JSON output hard limits:
- Return raw JSON only. Do not wrap it in Markdown fences.
- events: max 8 tracks.
- targets: max 5 targets per event.
- optional_searches: max 3 strings.
- deep_research: max 3 strings.
- Prefer fewer complete, high-conviction tracks over long output. Never stop mid-object or mid-string.
"""

HALLUCINATION_MAP = {
    '佰维存储': '688525',
    '江波龙': '301308',
    '德明利': '001309',
    '兆易创新': '603986',
    '北京君正': '300223',
    '香农芯创': '300475',
    '聚辰股份': '688123',
    '海光信息': '688041',
    '寒武纪': '688256',
    '澜起科技': '688008',
    '龙芯中科': '688047',
    '景嘉微': '300474',
    '紫光国微': '002049',
    '中国软件': '600536',
    '中微公司': '688012',
    '北方华创': '002371',
    '拓荆科技': '688072',
    '芯源微': '688037',
    '华海清科': '688120',
    '长川科技': '300604',
    '盛美上海': '688082',
    '源杰科技': '688498',
    '中际旭创': '300308',
    '新易盛': '300502',
    '天孚通信': '300394',
    '剑桥科技': '603083',
    '光迅科技': '002281',
    '浪潮信息': '000977',
    '工业富联': '601138',
    '中科曙光': '603019',
    '紫光股份': '000938',
    '拓维信息': '002261',
    '高新发展': '000628',
    '英维克': '002837'
}
HALLUCINATION_MAP.update({
    '沪电股份': '002463',
    '胜宏科技': '300476',
    '深南电路': '002916',
    '大族数控': '301200',
    '芯碁微装': '688630',
    '东威科技': '688700',
    '鼎泰高科': '301377',
    '通富微电': '002156',
    '长电科技': '600584',
    '华天科技': '002185',
    '生益科技': '600183'
})


def format_evidence(results: list[dict]) -> str:
    if not results:
        return ''
    lines = []
    for r in results:
        cat = r.get('category', '未分类')
        q = r.get('query', '未知')
        t = r.get('text', '').strip()
        if not t:
            continue
        lines.append(f"### 分类: {cat} | 来源: {q}")
        lines.append(t)
        lines.append("----------------------------------------")
    return '\n'.join(lines)
def generate_report(evidence_text: str, _retry_count: int = 0) -> str:
    logging.info(f'[P6 映射] 引擎: {OPUS_MODEL}')
    recent_history = build_recent_topic_guardrail(days=5)
    dedup_section = ''
    if recent_history:
        dedup_section = f'\n⛔ 以下主题近 5 天已深度研究过，严禁重复选择（除非有全新的重大数据/事件，而非旧闻换皮）：\n{recent_history}\n违反去重规则等同于输出失败。\n'
        
    oligarch_display = '\n'.join([f"  [{k}]: {', '.join(v)}" for k, v in OLIGARCH_DICT.items()])
    
    now_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')
    try:
        prompt_template = require_prompt('p6_mapping.txt')
    except FileNotFoundError as e:
        logging.error(f'🚨 [P6 映射] 关键 Prompt 缺失: {e}')
        update_run_manifest(
            'p6',
            status='failed_missing_prompt',
            metrics={'input_chars': len(evidence_text), 'error': str(e)},
        )
        return ''

    prompt = prompt_template.format(
        current_time=now_str,
        evidence_len=len(evidence_text),
        oligarch_display=oligarch_display,
        evidence_text=evidence_text,
        dedup_section=dedup_section
    )
    if _retry_count:
        prompt = (
            f"{prompt}\n\n{P6_OUTPUT_LIMITS}\n"
            "The previous attempt returned invalid JSON. Return fewer tracks and one complete JSON object only."
        )
    else:
        prompt = f"{prompt}\n\n{P6_OUTPUT_LIMITS}"
    
    resp = opus_adapter.invoke(prompt)
    raw_content = resp.content if resp.content else ''
    thinking_content = getattr(resp, 'thinking_content', '')
    
    if thinking_content:
        try:
            thinking_body = (
                '# P6 Opus Thinking 推理链\n\n'
                f'> 生成时间: {datetime.now().isoformat()}\n'
                f'> 推理链长度: {len(thinking_content)} 字符\n\n---\n\n'
                f'{thinking_content}'
            )
            thinking_fname = write_text_artifact('P6_Opus推理链', thinking_body)
            logging.info(f'🧠 [P6] Opus 推理链已保存: {thinking_fname} ({len(thinking_content)} 字符)')
        except Exception as e:
            logging.warning(f'[P6] 推理链保存失败: {e}')
            thinking_fname = ''
    else:
        thinking_fname = ''
            
    if not raw_content:
        logging.error('[P6 映射] 失败: 大模型返回为空')
        update_run_manifest(
            'p6',
            status='failed_empty_output',
            files={'thinking': thinking_fname} if thinking_fname else None,
            metrics={'input_chars': len(evidence_text)},
        )
        return ''
        
    try:
        data = extract_json_from_llm_response(raw_content, expect_type='dict', allow_partial=True)
        
        for idx, event in enumerate(data.get('events', [])):
            if not isinstance(event, dict):
                continue
            for t_idx, target in enumerate(event.get('targets', [])):
                if not isinstance(target, dict):
                    continue
                name = target.get('name', '未知公司')
                code = target.get('code', '000000')
                if name in HALLUCINATION_MAP and code != HALLUCINATION_MAP[name]:
                    logging.warning(f'⚠️ [P6纠错拦截] 大模型犯蠢！把 {name} 的代码错写成了 {code}，已强行掰正为 {HALLUCINATION_MAP[name]}')
                    data['events'][idx]['targets'][t_idx]['code'] = HALLUCINATION_MAP[name]
                    
        try:
            json_fname = write_json_artifact('P6_映射', data)
            logging.info(f'💾 [P6 映射] 完美架构 JSON 数据已落盘: {json_fname}')
        except Exception as e:
            logging.warning(f'⚠️ [P6 映射] JSON落盘失败: {e}')
            json_fname = ''
            
        lines = []
        for event in data.get('events', []):
            if not isinstance(event, dict):
                continue
            track_name = event.get('track_name', '未知赛道')
            event_summary = event.get('event_summary', '')
            lines.append(f'### 赛道: {track_name}')
            if event_summary:
                lines.append(f'> 事件触发: {event_summary}')
            for target in event.get('targets', []):
                if not isinstance(target, dict):
                    continue
                name = target.get('name', '未知公司')
                code = target.get('code', '000000')
                elas = target.get('elasticity', '⚡中弹性')
                reason = target.get('reasoning', '无')
                lines.append(f'- {name}（{code}） | {elas} | {reason}')
            lines.append('')
            
        for s in data.get('optional_searches', []):
            if str(s).strip():
                lines.append(f'SEARCH: {s}')
        for dr in data.get('deep_research', []):
            if str(dr).strip():
                lines.append(f'DEEP_RESEARCH: {dr}')
        report = '\n'.join(lines).strip()
        
        stock_pattern = re.compile(r'[\u4e00-\u9fa5]{2,6}[（(]\s*\d{6}\s*[）)]')
        stock_count = len(stock_pattern.findall(report))
        if len(report) < 1000 or stock_count < 5:
            if _retry_count < 1:
                logging.warning(f'⚠️ [P6 映射] 渲染不完整（{len(report)} 字, {stock_count} 只标的），疑似 Opus 返回了压缩版，自动重试第 {_retry_count + 1} 次...')
                time.sleep(10)
                return generate_report(evidence_text, _retry_count=_retry_count + 1)
            else:
                logging.error(f'❌ [P6 映射] 连续 {_retry_count + 1} 次充当残缺渲染，已达强行放弃阈值，放行当前结果以防死循环。')
                
        manifest_files = {}
        if json_fname:
            manifest_files['json'] = json_fname
        if thinking_fname:
            manifest_files['thinking'] = thinking_fname
        update_run_manifest(
            'p6',
            status='json_ready',
            files=manifest_files or None,
            metrics={
                'input_chars': len(evidence_text),
                'output_chars': len(report),
                'stock_count': stock_count,
                'retry_count': _retry_count,
            },
        )
        logging.info(f'✅ [P6 映射] 成功解析 JSON 并逆向渲染为 MarkDown，字数 {len(report)}')
        return report
        
    except json.JSONDecodeError as e:
        logging.error(f'❌ [P6 映射] JSON解析绝望失败(大模型幻觉了): {e}\n原文开头: {raw_content[:200]}')
        raw_path = ''
        try:
            raw_path = write_text_artifact(f'P6_raw_invalid_output_retry{_retry_count}', raw_content)
        except Exception as write_exc:
            logging.warning(f'[P6] invalid raw output save failed: {write_exc}')
        manifest_files = {
            key: value
            for key, value in {
                'raw': raw_path,
                'thinking': thinking_fname,
            }.items()
            if value
        } or None
        if _retry_count < P6_JSON_RETRY_LIMIT:
            logging.warning('[P6] retrying mapping JSON generation after parse failure')
            update_run_manifest(
                'p6',
                status='retrying_parse',
                files=manifest_files,
                metrics={'input_chars': len(evidence_text), 'raw_output_chars': len(raw_content), 'error': str(e), 'retry_count': _retry_count},
            )
            time.sleep(5)
            return generate_report(evidence_text, _retry_count=_retry_count + 1)
        update_run_manifest(
            'p6',
            status='fallback_raw_llm',
            files=manifest_files,
            metrics={'input_chars': len(evidence_text), 'raw_output_chars': len(raw_content), 'error': str(e), 'retry_count': _retry_count},
        )
        return raw_content
