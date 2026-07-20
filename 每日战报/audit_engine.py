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
from settings import get_audit_fallback_adapter, pro_adapter, require_prompt
from semantic_output import is_no_evidence_text
from industry_dict import ALL_OLIGARCHS_FLAT
from tdx_stock_universe import (
    build_rejected_stock_guardrail,
    filter_rejected_stock_lines,
    filter_rejected_stock_tree,
    normalize_rejected_stock_names,
)
from utils import extract_json_from_llm_response

AUDIT_RETRY_LIMIT = 1
AUDIT_OUTPUT_LIMITS = """

STRICT OUTPUT LIMITS FOR MACHINE PARSING:
- Return raw JSON only. Do not wrap it in ```json fences.
- Keep high_trust to at most 80 items and medium_trust to at most 80 items.
- Keep deleted to at most 30 representative items; summary.deleted may still count all removed items.
- Set summary.passed to exactly len(high_trust) + len(medium_trust) after finalizing both arrays.
- Do not include omitted or unlisted passed items in summary.passed.
- If content is too long, prefer fewer items over invalid or truncated JSON.
"""
AUDIT_SEMANTIC_RETRY_REASONS = frozenset(
    {
        'passed_count_mismatch',
        'deleted_count_mismatch',
        'deleted_count_without_items',
        'deleted_items_without_count',
    }
)


class _AuditSemanticError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _audit_count(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _AuditSemanticError(f'invalid_{field_name}_count')
    return value


def _validate_audit_payload(data: dict, *, allow_empty: bool = False) -> dict:
    if not isinstance(data, dict):
        raise _AuditSemanticError('audit_payload_not_object')

    summary = data.get('summary')
    if not isinstance(summary, dict):
        raise _AuditSemanticError('missing_summary')

    passed_count = _audit_count(summary.get('passed'), 'passed')
    deleted_count = _audit_count(summary.get('deleted'), 'deleted')

    collections = {}
    for field_name in ('high_trust', 'medium_trust', 'deleted'):
        value = data.get(field_name)
        if not isinstance(value, list):
            raise _AuditSemanticError(f'invalid_{field_name}_collection')
        collections[field_name] = [
            item
            for item in value
            if not (isinstance(item, str) and is_no_evidence_text(item))
        ]

    high_trust = collections['high_trust']
    medium_trust = collections['medium_trust']
    deleted_items = collections['deleted']
    if passed_count != len(high_trust) + len(medium_trust):
        raise _AuditSemanticError('passed_count_mismatch')
    if deleted_count < len(deleted_items):
        raise _AuditSemanticError('deleted_count_mismatch')
    if deleted_count > 0 and not deleted_items:
        raise _AuditSemanticError('deleted_count_without_items')
    if deleted_count == 0 and deleted_items:
        raise _AuditSemanticError('deleted_items_without_count')
    if passed_count == 0 and deleted_count == 0 and not allow_empty:
        raise _AuditSemanticError('empty_audit_payload')

    return {
        'passed_count': passed_count,
        'deleted_count': deleted_count,
        'high_trust': high_trust,
        'medium_trust': medium_trust,
        'deleted_items': deleted_items,
        'no_results': passed_count == 0,
    }


def _adapter_error(adapter) -> str:
    category = str(getattr(adapter, 'last_error_category', '') or '').strip()
    message = str(getattr(adapter, 'last_error_message', '') or '').strip()
    if category and message:
        return f'{category}: {message}'
    return message or category


def _invoke_audit_with_fallback(prompt: str, layer_name: str, *, min_content_chars: int = 1):
    primary_response = None
    primary_error = ''
    try:
        primary_response = pro_adapter.invoke(prompt)
    except Exception as exc:
        primary_error = str(exc)

    primary_content = str(getattr(primary_response, 'content', '') or '')
    if len(primary_content.strip()) >= min_content_chars:
        return primary_response, pro_adapter, {
            'audit_fallback_used': False,
            'audit_primary_error': '',
        }

    primary_error = primary_error or _adapter_error(pro_adapter) or 'empty_or_short_response'
    fallback_adapter = get_audit_fallback_adapter()
    if fallback_adapter is None:
        if primary_response is not None:
            return primary_response, pro_adapter, {
                'audit_fallback_used': False,
                'audit_primary_error': primary_error[:500],
            }
        raise RuntimeError(primary_error)

    logging.warning(
        '[%s 审计] 主模型接口失败，切换备用接口（model=%s, reason=%s）',
        layer_name,
        getattr(fallback_adapter, 'model', ''),
        primary_error[:300],
    )
    fallback_response = fallback_adapter.invoke(prompt)
    return fallback_response, fallback_adapter, {
        'audit_fallback_used': True,
        'audit_primary_error': primary_error[:500],
    }

def gemini_pro_audit(
    raw_data: str,
    audit_type: str = 'p5',
    backtrack: str = '24h',
    _retry_count: int = 0,
    _retry_reason: str = '',
) -> str:
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
    if _retry_reason:
        prompt += (
            '\nRETRY CORRECTION REQUIRED:\n'
            f'- The previous response failed validation with: {_retry_reason}.\n'
            '- Recalculate every summary count from the final arrays before returning JSON.\n'
        )

    t0 = time.time()
    try:
        # NativeLLMAdapter enforces the provider transport timeout. The enclosing
        # pipeline stage runs in a subprocess and supplies the hard wall-clock deadline.
        resp, active_adapter, fallback_metrics = _invoke_audit_with_fallback(prompt, layer_name)
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
            **fallback_metrics,
        }
        last_error_category = getattr(active_adapter, 'last_error_category', '')
        last_error_message = getattr(active_adapter, 'last_error_message', '')
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
        audit_state = _validate_audit_payload(data, allow_empty=audit_type != 'p5')
        json_path = write_json_artifact(file_prefix, data)
        logging.info(f'💾 [{layer_name} 审计] 完美架构 JSON 数据已落盘: {json_path}')

        clean_lines = []
        summary = data['summary']
        clean_lines.append('## 审计摘要')
        clean_lines.append(f"通过: {summary.get('passed', 0)}条 / 删除: {summary.get('deleted', 0)}条")
        clean_lines.append('')

        clean_lines.append('## 🟢 高可信情报')
        high_trust = audit_state['high_trust']
        if high_trust:
            clean_lines.extend(high_trust)
        else:
            clean_lines.append('无')

        clean_lines.append('')
        clean_lines.append('## 🟡 中可信情报')
        medium_trust = audit_state['medium_trust']
        if medium_trust:
            clean_lines.extend(medium_trust)
        else:
            clean_lines.append('无')

        clean_text = '\n'.join(clean_lines).strip()

        deleted_lines = []
        deleted_lines.append('## 🔴 删除清单（不传给 GPT-5.6 Sol Max，仅记录）')
        deleted = audit_state['deleted_items']
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

        # Downstream recovery must load exactly the same audited body returned
        # during the first run.  Keep rejected evidence in a separate
        # diagnostic artifact so a P6/P9 resume cannot feed it back in.
        md_path = write_text_artifact(file_prefix, clean_text)
        deleted_path = ''
        if deleted:
            try:
                deleted_path = write_text_artifact(f'{file_prefix}删除清单', deleted_log)
            except Exception as write_exc:
                logging.warning(f'[{layer_name} 审计] 删除清单落盘失败: {write_exc}')
        manifest_files = {'md': md_path, 'json': json_path}
        if deleted_path:
            manifest_files['deleted'] = deleted_path
        update_run_manifest(
            stage_key,
            status='completed_no_results' if audit_state['no_results'] else 'completed',
            files=manifest_files,
            metrics={
                'input_chars': len(raw_data),
                'output_chars': len(clean_text),
                'raw_output_chars': len(raw_content),
                'elapsed_seconds': round(elapsed, 1),
                'retry_count': _retry_count,
                'passed_count': audit_state['passed_count'],
                'high_trust_count': len(high_trust),
                'medium_trust_count': len(medium_trust),
                'deleted_count': audit_state['deleted_count'],
                'deleted_items_count': len(deleted),
                'data_state': 'valid_empty' if audit_state['no_results'] else 'valid',
                **fallback_metrics,
            },
        )

        logging.info(f'✅ [{layer_name} 审计] 成功解析 JSON 并逆向渲染为 MarkDown ({elapsed:.1f}s)')
        return '' if audit_state['no_results'] else clean_text

    except _AuditSemanticError as e:
        should_retry = (
            _retry_count < AUDIT_RETRY_LIMIT
            and e.reason in AUDIT_SEMANTIC_RETRY_REASONS
        )
        raw_path = ''
        try:
            raw_prefix = (
                f'{file_prefix}_RAW_SEMANTIC_INVALID_{_retry_count + 1}'
                if should_retry
                else f'{file_prefix}_RAW_SEMANTIC_INVALID'
            )
            raw_path = write_text_artifact(
                raw_prefix,
                raw_content,
            )
        except Exception as write_exc:
            logging.warning(f'[{layer_name} audit] failed to save semantically invalid raw output: {write_exc}')
        logging.error(f'❌ [{layer_name} 审计] 结构化输出语义校验失败: {e.reason}')
        if should_retry:
            logging.warning(
                f'[{layer_name} audit] semantic validation failed; retrying once with count correction'
            )
            time.sleep(5)
            return gemini_pro_audit(
                raw_data,
                audit_type=audit_type,
                backtrack=backtrack,
                _retry_count=_retry_count + 1,
                _retry_reason=e.reason,
            )
        update_run_manifest(
            stage_key,
            status='failed_semantic_output',
            files={'raw': raw_path} if raw_path else None,
            metrics={
                'input_chars': len(raw_data),
                'raw_output_chars': len(raw_content),
                'elapsed_seconds': round(elapsed, 1),
                'retry_count': _retry_count,
                'semantic_error': e.reason,
                'data_state': 'invalid',
                **fallback_metrics,
            },
        )
        return ''

    except json.JSONDecodeError as e:
        raw_path = ''
        try:
            raw_path = write_text_artifact(f'{file_prefix}_RAW_INVALID_{_retry_count + 1}', raw_content)
        except Exception as write_exc:
            logging.warning(f'[{layer_name} audit] failed to save invalid raw output: {write_exc}')
        logging.error(f'❌ [{layer_name} 审计] JSON解析失败: {e}\n大模型幻觉原文: {raw_content[:200]}')
        if _retry_count < AUDIT_RETRY_LIMIT:
            logging.warning(f'[{layer_name} audit] JSON parse failed; retrying once with stricter output limits')
            time.sleep(5)
            return gemini_pro_audit(
                raw_data,
                audit_type=audit_type,
                backtrack=backtrack,
                _retry_count=_retry_count + 1,
                _retry_reason='json_parse_failed',
            )
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
                **fallback_metrics,
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
        resp, active_adapter, fallback_metrics = _invoke_audit_with_fallback(
            prompt,
            'P7a',
            min_content_chars=100,
        )
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
            **fallback_metrics,
        }
        last_error_category = getattr(active_adapter, 'last_error_category', '')
        last_error_message = getattr(active_adapter, 'last_error_message', '')
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
    has_markdown_separator = '===MARKDOWN===' in raw_content

    if has_markdown_separator:
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

    normalized_markdown = markdown_text.strip()
    filtered_to_empty = bool(not normalized_markdown and output_identity_lines_removed)
    semantic_no_results = bool(
        filtered_to_empty
        or (normalized_markdown and is_no_evidence_text(normalized_markdown))
    )
    looks_like_json_only = bool(
        not has_markdown_separator
        and normalized_markdown.startswith(('{', '['))
        and normalized_markdown.endswith(('}', ']'))
    )
    if semantic_no_results:
        update_run_manifest(
            'p7a',
            status='completed_no_results',
            files={'json': json_path} if json_path else None,
            metrics={
                'input_chars': len(raw_deep_research),
                'output_chars': 0,
                'data_state': 'valid_empty',
                'rejected_target_names': rejected_names,
                'rejected_identity_items_removed': rejected_identity_items_removed,
                **fallback_metrics,
            },
        )
        logging.info('[P7a 审计] 审计后无可用证据，记录合法空结果')
        return ''
    if not normalized_markdown or looks_like_json_only:
        reason = 'json_without_markdown' if looks_like_json_only else 'empty_markdown_output'
        update_run_manifest(
            'p7a',
            status='failed_semantic_output',
            files={'json': json_path} if json_path else None,
            metrics={
                'input_chars': len(raw_deep_research),
                'output_chars': 0,
                'data_state': 'invalid',
                'semantic_error': reason,
                'rejected_target_names': rejected_names,
                'rejected_identity_items_removed': rejected_identity_items_removed,
                **fallback_metrics,
            },
        )
        logging.warning('[P7a 审计] %s，当前运行降级透传原始 P7', reason)
        return raw_deep_research

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
        **fallback_metrics,
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
