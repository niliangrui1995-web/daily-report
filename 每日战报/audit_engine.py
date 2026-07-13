"""
P5 / P7a / P7.5 审计层 — Sonnet/GPT-5.6 Sol/Gemini 独立审计引擎。

从 daily_report.py 拆分。负责对 P1 原始数据、P7 深度研究和 P7.5 补充搜索结果进行
可信度打分、时效性验证、去重汇总与数据清洗。

【⚠️核心维护规则 / Agent 指令】
1. 严禁擅自绕过或修改时效性窗口（如 24h/48h/72h/120h）的强制拦截逻辑，过期旧闻必须直接被判死刑剔除。
2. 剥离和解析大模型返回的 JSON 结果时，必须强制保留现有的正则宽容解析（容忍前后出现 md 标记或乱码）与 try-catch 兜底，绝不能假定大模型每次都会吐出完美合规的 JSON。
3. P5 与 P7.5 的 JSON 结果落盘阶段，必须严格按照层级变量隔离文件名前缀，严禁互相覆盖污染数据轴。
4. Gemini Pro 的单次请求必须保留 SDK 传输超时；审计阶段的墙钟硬截止由 orchestrator 的可终止子进程统一执行，禁止用线程池 `future.result(timeout=...)` 冒充可终止超时。
5. P7a 是 P7 深度研究 → P9 战报之间的「数据压缩器」，产出纯 Markdown（非 JSON），
   直接替代 P7 原始数据传递给 P9，避免 P9 输入中 P7 占比过大稀释注意力。
"""

import json
import logging
import time
from datetime import datetime

from artifact_store import update_run_manifest, write_json_artifact, write_text_artifact
from settings import pro_adapter, require_prompt
from industry_dict import ALL_OLIGARCHS_FLAT
from tdx_stock_universe import (
    build_rejected_stock_guardrail,
    filter_rejected_stock_lines,
    filter_rejected_stock_tree,
    normalize_rejected_stock_names,
)
from utils import extract_json_from_llm_response

AUDIT_JSON_RETRY_LIMIT = 1
AUDIT_OUTPUT_LIMITS = """

STRICT OUTPUT LIMITS FOR MACHINE PARSING:
- Return raw JSON only. Do not wrap it in ```json fences.
- Keep high_trust to at most 80 items and medium_trust to at most 80 items.
- Keep deleted to at most 30 representative items; summary.deleted may still count all removed items.
- If content is too long, prefer fewer items over invalid or truncated JSON.
"""

def gemini_pro_audit(raw_data: str, audit_type: str = 'p5', backtrack: str = '24h', _retry_count: int = 0) -> str:
    layer_name = 'P5' if audit_type == 'p5' else 'P7.5'
    stage_key = 'p5' if audit_type == 'p5' else 'p75'
    file_prefix = 'P7.5_补充审计' if layer_name == 'P7.5' else 'P5_审计'
    logging.info(f'[{layer_name} 审计] 启动 Gemini Pro 独立审计（{len(raw_data)} 字输入）')

    if not raw_data or not raw_data.strip():
        logging.warning(f'[{layer_name} 审计] 输入为空，跳过审计')
        update_run_manifest(
            stage_key,
            status='skipped_empty_input',
            metrics={'input_chars': len(raw_data)},
        )
        return ''

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
            f'- 发生时间距离现在 {today_str} 在 {backtrack} 内（以本次动态窗口为准）→ 保留\n'
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

    try:
        prompt_template = require_prompt('audit.txt')
    except FileNotFoundError as e:
        logging.error(f'🚨 [{layer_name} 审计] 关键 Prompt 缺失: {e}')
        update_run_manifest(
            stage_key,
            status='failed_missing_prompt',
            metrics={'input_chars': len(raw_data), 'error': str(e)},
        )
        return ''

    prompt = prompt_template.format(
        layer_name=layer_name,
        header_text=header_text,
        time_section=time_section,
        task_two=task_two,
        task_three=task_three,
        vanguard_list=vanguard_list,
        raw_data=raw_data
    ) + AUDIT_OUTPUT_LIMITS

    t0 = time.time()
    try:
        # NativeLLMAdapter enforces the provider transport timeout. The enclosing
        # pipeline stage runs in a subprocess and supplies the hard wall-clock deadline.
        resp = pro_adapter.invoke(prompt)
    except Exception as e:
        logging.error(f'🚨 [{layer_name} 审计] 调用异常: {e}')
        update_run_manifest(
            stage_key,
            status='failed_invoke',
            metrics={'input_chars': len(raw_data), 'error': str(e)},
        )
        return ''

    elapsed = time.time() - t0
    raw_content = resp.content if resp.content else ''

    if not raw_content:
        logging.warning(f'[{layer_name} 审计] Pro 返回空结果')
        failure_metrics = {
            'input_chars': len(raw_data),
            'elapsed_seconds': round(elapsed, 1),
        }
        last_error_category = getattr(pro_adapter, 'last_error_category', '')
        last_error_message = getattr(pro_adapter, 'last_error_message', '')
        if last_error_category:
            failure_metrics['failure_category'] = last_error_category
        if last_error_message:
            failure_metrics['error'] = last_error_message[:500]
        update_run_manifest(
            stage_key,
            status='failed_empty_output',
            metrics=failure_metrics,
        )
        return ''

    try:
        data = extract_json_from_llm_response(raw_content, expect_type='dict', allow_partial=True)
        json_path = write_json_artifact(file_prefix, data)
        logging.info(f'💾 [{layer_name} 审计] 完美架构 JSON 数据已落盘: {json_path}')

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
        deleted_lines.append('## 🔴 删除清单（不传给 GPT-5.6 Sol Max，仅记录）')
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

        md_path = write_text_artifact(file_prefix, full_text)
        update_run_manifest(
            stage_key,
            status='completed',
            files={'md': md_path, 'json': json_path},
            metrics={
                'input_chars': len(raw_data),
                'output_chars': len(clean_text),
                'raw_output_chars': len(raw_content),
                'elapsed_seconds': round(elapsed, 1),
                'retry_count': _retry_count,
            },
        )

        logging.info(f'✅ [{layer_name} 审计] 成功解析 JSON 并逆向渲染为 MarkDown ({elapsed:.1f}s)')
        return clean_text

    except json.JSONDecodeError as e:
        raw_path = ''
        try:
            raw_path = write_text_artifact(f'{file_prefix}_RAW_INVALID_{_retry_count + 1}', raw_content)
        except Exception as write_exc:
            logging.warning(f'[{layer_name} audit] failed to save invalid raw output: {write_exc}')
        logging.error(f'❌ [{layer_name} 审计] JSON解析失败: {e}\n大模型幻觉原文: {raw_content[:200]}')
        if _retry_count < AUDIT_JSON_RETRY_LIMIT:
            logging.warning(f'[{layer_name} audit] JSON parse failed; retrying once with stricter output limits')
            time.sleep(5)
            return gemini_pro_audit(raw_data, audit_type=audit_type, backtrack=backtrack, _retry_count=_retry_count + 1)
        update_run_manifest(
            stage_key,
            status='fallback_raw_llm',
            files={'raw': raw_path} if raw_path else None,
            metrics={
                'input_chars': len(raw_data),
                'raw_output_chars': len(raw_content),
                'elapsed_seconds': round(elapsed, 1),
                'retry_count': _retry_count,
                'error': str(e),
            },
        )
        return raw_content


def p7a_deep_research_audit(
    raw_deep_research: str,
    *,
    rejected_target_names: list[str] | None = None,
) -> str:
    """P7a 深度研究审计 — 对 P7 原始搜索数据做去重、降噪、结构化汇总。

    Why: P7 原始输出通常 40K+ 字，跨 3 轮搜索存在大量重复信息和搜索噪声。
         如果直接送入 P9，会占据 GPT-5.6 Sol Max 近一半的注意力窗口，稀释战报质量。
         P7a 用独立审计模型做一次审计汇总，将信息密度提升 3~5 倍，
         输出 8~12K 字的精炼 Markdown 报告替代原始数据送入 P9。

    Args:
        raw_deep_research: P7 run_deep_research() 返回的原始 Markdown 全文

    Returns:
        str: 审计汇总后的 Markdown 报告（纯文本，不含 JSON）
    """
    rejected_names = normalize_rejected_stock_names(rejected_target_names)
    raw_deep_research, input_identity_lines_removed = filter_rejected_stock_lines(
        raw_deep_research,
        rejected_names,
    )
    input_identity_metrics = {
        'rejected_target_names': rejected_names,
        'rejected_identity_items_removed': input_identity_lines_removed,
    }

    if not raw_deep_research or len(raw_deep_research.strip()) < 200:
        logging.warning('[P7a 审计] P7 深研数据不足 200 字，跳过审计直接透传')
        update_run_manifest(
            'p7a',
            status='skipped_short_input',
            metrics={'input_chars': len(raw_deep_research or ''), **input_identity_metrics},
        )
        return raw_deep_research or ''

    logging.info(f'[P7a 审计] 启动 Gemini Pro 深度研究审计（{len(raw_deep_research)} 字输入）')

    today_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')
    vanguard_list = ', '.join(ALL_OLIGARCHS_FLAT)

    try:
        prompt_template = require_prompt('p7a_audit.txt')
    except FileNotFoundError as e:
        logging.error(f'🚨 [P7a 审计] 关键 Prompt 缺失: {e}')
        update_run_manifest(
            'p7a',
            status='failed_missing_prompt',
            metrics={'input_chars': len(raw_deep_research), 'error': str(e), **input_identity_metrics},
        )
        return raw_deep_research

    prompt = prompt_template.format(
        today_str=today_str,
        vanguard_list=vanguard_list,
        raw_deep_research=raw_deep_research,
    )
    identity_guardrail = build_rejected_stock_guardrail(rejected_names)
    if identity_guardrail:
        prompt = f'{identity_guardrail}\n\n{prompt}'

    t0 = time.time()
    try:
        # Transport timeout is enforced by the adapter; the research stage process
        # remains the killable hard deadline for a provider that ignores cancellation.
        resp = pro_adapter.invoke(prompt)
    except Exception as e:
        logging.error(f'🚨 [P7a 审计] 调用异常: {e}，降级透传原始数据')
        update_run_manifest(
            'p7a',
            status='failed_invoke',
            metrics={'input_chars': len(raw_deep_research), 'error': str(e), **input_identity_metrics},
        )
        return raw_deep_research

    elapsed = time.time() - t0
    raw_content = resp.content if resp.content else ''

    if not raw_content or len(raw_content.strip()) < 100:
        logging.warning('[P7a 审计] Pro 返回空结果或过短，降级透传原始数据')
        failure_metrics = {
            'input_chars': len(raw_deep_research),
            'elapsed_seconds': round(elapsed, 1),
            **input_identity_metrics,
        }
        last_error_category = getattr(pro_adapter, 'last_error_category', '')
        last_error_message = getattr(pro_adapter, 'last_error_message', '')
        if last_error_category:
            failure_metrics['failure_category'] = last_error_category
        if last_error_message:
            failure_metrics['error'] = last_error_message[:500]
        update_run_manifest(
            'p7a',
            status='failed_empty_output',
            metrics=failure_metrics,
        )
        return raw_deep_research

    # Why: P7a prompt 要求按 `===MARKDOWN===` 分隔符输出 JSON + Markdown 双轨数据
    #       解析这个分隔符，分别落盘 .json 和 .md 文件
    file_prefix = 'P7a_深研审计'

    json_data = None
    markdown_text = raw_content  # 默认整体作为 Markdown（降级兜底）
    structured_degraded = False
    degrade_reason = ""

    if '===MARKDOWN===' in raw_content:
        parts = raw_content.split('===MARKDOWN===', 1)
        json_part = parts[0].strip()
        markdown_part = parts[1].strip() if len(parts) > 1 else ''

        # Why: JSON 部分可能被模型包在 ```json``` 代码块里，需要剥离
        json_part = json_part.strip('`').strip()
        if json_part.startswith('json'):
            json_part = json_part[4:].strip()

        try:
            json_data = extract_json_from_llm_response(json_part, expect_type='dict')
            logging.info('[P7a 审计] 成功解析 JSON 结构化数据')
        except Exception as e:
            logging.warning(f'⚠️ [P7a 审计] JSON 解析失败（不影响 Markdown 输出）: {e}')
            structured_degraded = True
            degrade_reason = f'json_parse_failed: {e}'
            # Why: JSON 解析失败时仍尝试手动构建基本结构
            json_data = {
                'meta': {
                    'input_chars': len(raw_deep_research),
                    'output_chars': len(markdown_part) if markdown_part else len(raw_content),
                    'parse_error': str(e),
                },
                'raw_json_attempt': json_part[:2000],
            }

        if markdown_part:
            markdown_text = markdown_part
    else:
        logging.warning('[P7a 审计] 响应中未找到 ===MARKDOWN=== 分隔符，整体作为 Markdown 处理')
        structured_degraded = True
        degrade_reason = 'missing_markdown_separator'

    json_identity_items_removed = 0
    if json_data:
        json_data, json_identity_items_removed = filter_rejected_stock_tree(
            json_data,
            rejected_names,
        )
    markdown_text, output_identity_lines_removed = filter_rejected_stock_lines(
        markdown_text,
        rejected_names,
    )
    rejected_identity_items_removed = (
        input_identity_lines_removed
        + json_identity_items_removed
        + output_identity_lines_removed
    )
    if rejected_identity_items_removed:
        logging.warning(
            '[P7a 身份约束] 移除 %s 个含本地拒绝标的的输入/输出项',
            rejected_identity_items_removed,
        )

    json_path = ''
    if json_data:
        try:
            json_path = write_json_artifact(file_prefix, json_data)
            logging.info(f'💾 [P7a 审计] JSON 数据已落盘: {json_path}')
        except Exception as e:
            logging.warning(f'⚠️ [P7a 审计] JSON 落盘失败: {e}')

    md_body = (
        '# P7a 深度研究审计报告\n\n'
        f'> 生成时间: {datetime.now().isoformat()}\n'
        f'> 原始数据: {len(raw_deep_research)} 字 → 审计后: {len(markdown_text)} 字\n\n'
        f'{markdown_text}'
    )
    try:
        md_path = write_text_artifact(file_prefix, md_body)
        logging.info(f'💾 [P7a 审计] Markdown 报告已落盘: {md_path}')
    except Exception as e:
        logging.warning(f'⚠️ [P7a 审计] Markdown 落盘失败: {e}')
        md_path = ''

    compression_ratio = len(raw_deep_research) / max(len(markdown_text), 1)
    manifest_files = {}
    if md_path:
        manifest_files['md'] = md_path
    if json_path:
        manifest_files['json'] = json_path
    manifest_status = 'completed_structured_degraded' if structured_degraded else 'completed'
    manifest_metrics = {
        'input_chars': len(raw_deep_research),
        'output_chars': len(markdown_text),
        'compression_ratio': round(compression_ratio, 2),
        'elapsed_seconds': round(elapsed, 1),
        'topics_count': len(json_data.get('topics', [])) if isinstance(json_data, dict) else 0,
        'rejected_target_names': rejected_names,
        'rejected_identity_items_removed': rejected_identity_items_removed,
    }
    if structured_degraded:
        manifest_metrics['structured_degraded'] = True
        manifest_metrics['degrade_reason'] = degrade_reason or 'structured_json_unavailable'
        if isinstance(json_data, dict):
            meta = json_data.get('meta', {})
            if isinstance(meta, dict) and meta.get('parse_error'):
                manifest_metrics['parse_error'] = str(meta.get('parse_error'))[:500]
            raw_attempt = json_data.get('raw_json_attempt')
            if raw_attempt:
                manifest_metrics['raw_json_attempt_chars'] = len(str(raw_attempt))
    update_run_manifest(
        'p7a',
        status=manifest_status,
        files=manifest_files or None,
        metrics=manifest_metrics,
    )
    logging.info(f'✅ [P7a 审计] 完成 ({elapsed:.1f}s) | {len(raw_deep_research)} → {len(markdown_text)} 字 | 压缩比 {compression_ratio:.1f}x')

    # Why: 返回的是 Markdown 纯文本，这是真正送给 P9 的内容
    return markdown_text
