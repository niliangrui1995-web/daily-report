"""
P5 / P7.5 审计层 — Gemini 3.1 Pro High 独立审计引擎。

从 daily_report.py 拆分。负责对 P1 原始数据和 P7.5 补充搜索结果进行
可信度打分、时效性验证、数据清洗。

【⚠️核心维护规则 / Agent 指令】
1. 严禁擅自绕过或修改时效性窗口（24h/72h）的强制拦截逻辑，过期旧闻必须直接被判死刑剔除。
2. 剥离和解析大模型返回的 JSON 结果时，必须强制保留现有的正则宽容解析（容忍前后出现 md 标记或乱码）与 try-catch 兜底，绝不能假定大模型每次都会吐出完美合规的 JSON。
3. P5 与 P7.5 的 JSON 结果落盘阶段，必须严格按照层级变量隔离文件名前缀，严禁互相覆盖污染数据轴。
4. 调用 Gemini Pro 时使用 `ThreadPoolExecutor(max_workers=1)` + `timeout=480s` 做超时保护，是从生产事故（Gemini 首次调用偶发无限挂起 10 分钟）中沉淀的机制。严禁"觉得用线程池包一层太重"而改成直接调用——那样会在某些边际条件下把整条流水线卡死。
"""

import concurrent.futures
import json
import logging
import os
import time
from datetime import datetime

from settings import pro_adapter, load_prompt
from industry_dict import ALL_OLIGARCHS_FLAT
from utils import extract_json_from_llm_response

def gemini_pro_audit(raw_data: str, audit_type: str = 'p5', backtrack: str = '24h') -> str:
    layer_name = 'P5' if audit_type == 'p5' else 'P7.5'
    logging.info(f'[{layer_name} 审计] 启动 Gemini Pro 独立审计（{len(raw_data)} 字输入）')

    today_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')
    vanguard_list = ', '.join(ALL_OLIGARCHS_FLAT)

    if audit_type == 'p5':
        time_section = (
            f'⚠️ 当前北京时间：{today_str}\n'
            f'⚠️ 扫描时间窗口：{backtrack}\n'
            '⚠️ 下方 RSS 采集结果中的所有时间均为北京时间（UTC+8）。\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━━━\n\n'
            '【任务一：时效性验证（纯文本推理逻辑验证）】\n\n'
            '阅读每条事实自带的日期戳，通过严密的纯文本数学与逻辑推理，验证其是否在设定时间窗内：\n'
            f'- 发生时间距离现在 {today_str} 在 {backtrack} 内（24h 或 72h）→ 保留\n'
            '- 超出时间窗的旧闻（如数月、数年前的资料）→ 回绝并直接删除\n'
            '- 时间模糊但事件特征表明是近期的 → 保留并标注"时间待确认"\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━━━'
        )
        header_text = '你收到的数据来自多源 RSS/新闻站直采的原始结果，可能包含过期信息、重复条目和低质量噪声。'
    else:
        time_section = 'ℹ️ 本次审计无时效性限制（补充搜索为深度调研，不受 24h/72h 窗口约束）。\n\n━━━━━━━━━━━━━━━━━━━━━━━━'
        if '[Tavily Fallback]' in raw_data:
            header_text = '你收到的数据是深度调研层的补充搜索结果，由 Tavily 搜索引擎执行（Gemini 搜索不可用时的降级通道）。可能包含低质量杂音和主观内容。'
        else:
            header_text = '你收到的数据是深度调研层的补充搜索结果，由 Gemini Flash 执行搜索后返回。可能包含幻觉、低质量杂音和主观内容。'

    task_two = '任务二' if audit_type == 'p5' else '任务一'
    task_three = '任务三' if audit_type == 'p5' else '任务二'

    prompt_template = load_prompt('audit.txt')
    prompt = prompt_template.format(
        layer_name=layer_name,
        header_text=header_text,
        time_section=time_section,
        task_two=task_two,
        task_three=task_three,
        vanguard_list=vanguard_list,
        raw_data=raw_data
    )

    t0 = time.time()
    _AUDIT_TIMEOUT_SECONDS = 480

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
            _future = _pool.submit(pro_adapter.invoke, prompt)
            resp = _future.result(timeout=_AUDIT_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        logging.error(f'🚨 [{layer_name} 审计] Gemini Pro 响应超时（{_AUDIT_TIMEOUT_SECONDS}s），降级跳过审计')
        return ''
    except Exception as e:
        logging.error(f'🚨 [{layer_name} 审计] 调用异常: {e}')
        return ''

    elapsed = time.time() - t0
    raw_content = resp.content if resp.content else ''

    if not raw_content:
        logging.warning(f'[{layer_name} 审计] Pro 返回空结果')
        return ''

    try:
        data = extract_json_from_llm_response(raw_content, expect_type='dict')

        try:
            from settings import get_today_output_dir
            now_str = datetime.now().strftime('%Y%m%d%H%M')
            file_prefix = 'P7.5_补充审计' if layer_name == 'P7.5' else 'P5_审计'
            json_fname = os.path.join(get_today_output_dir(), f'{file_prefix}_{now_str}.json')
            with open(json_fname, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logging.info(f'💾 [{layer_name} 审计] 完美架构JSON数据已强制分离落盘: {json_fname}')
        except Exception as e:
            logging.warning(f'⚠️ [{layer_name} 审计] JSON落盘失败: {e}')

        clean_lines = []
        summary = data.get('summary', {})
        clean_lines.append('## 审计摘要')
        clean_lines.append(f"通过: {summary.get('passed', 0)}条 / 删除: {summary.get('deleted', 0)}条")
        clean_lines.append('')

        clean_lines.append('## 🟢 高可信情报')
        high_trust = data.get('high_trust', [])
        if high_trust:
            clean_lines.extend(high_trust)
        else:
            clean_lines.append('无')

        clean_lines.append('')
        clean_lines.append('## 🟡 中可信情报')
        medium_trust = data.get('medium_trust', [])
        if medium_trust:
            clean_lines.extend(medium_trust)
        else:
            clean_lines.append('无')

        clean_text = '\n'.join(clean_lines).strip()

        deleted_lines = []
        deleted_lines.append('## 🔴 删除清单（不传给 Opus，仅记录）')
        deleted = data.get('deleted', [])
        if deleted:
            for d in deleted:
                if isinstance(d, dict):
                    reason = d.get('reason', '未知原因')
                    content = d.get('content', '')
                    deleted_lines.append(f'- 【删除】{reason} -> {content}')
                else:
                    deleted_lines.append(f'- 【删除】{d}')
        else:
            deleted_lines.append('无')

        deleted_log = '\n'.join(deleted_lines).strip()
        full_text = clean_text + '\n\n' + deleted_log

        try:
            md_fname = os.path.join(get_today_output_dir(), f'{file_prefix}_{now_str}.md')
            with open(md_fname, 'w', encoding='utf-8') as f:
                f.write(full_text)
        except Exception as e:
            logging.error(f'[{layer_name}] 写入 md 文件失败: {e}')

        logging.info(f'✅ [{layer_name} 审计] 成功解析 JSON 并逆向渲染为 MarkDown ({elapsed:.1f}s)')
        return clean_text

    except json.JSONDecodeError as e:
        logging.error(f'❌ [{layer_name} 审计] JSON解析失败: {e}\n大模型幻觉原文: {raw_content[:200]}')
        return raw_content
