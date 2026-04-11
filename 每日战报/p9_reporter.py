import re
import json
import logging
from datetime import datetime

from artifact_store import get_run_timestamp, update_run_manifest, write_json_artifact, write_text_artifact
from settings import opus_adapter, EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL, require_prompt
from utils import extract_json_from_llm_response
from p9_email import send_report_via_email
from p9_rendering import build_report_markdown

def p9_risk_analysis(p5_evidence, p7_deep, p75_supplement, p6_mapping=None, price_data=None, p2_x_intel_data=None, p3_intel_data="", p4_yfinance=""):
    """
    P9 Opus 综合战报：融合八层数据生成最终战报。

    Returns:
        tuple[str, str]: (战报内容, 统一时间戳) 供 send_report_email 复用
    """
    # Why: 必须在函数入口就定义 now_str，防止 except 分支引用未绑定变量导致 NameError
    run_ts = get_run_timestamp()

    if not price_data or len(price_data) < 50:
        logging.warning("[P9] 股价数据不足，战报将缺少技术面分析")
        price_data = "（本次运行未获取到通达信本地K线数据，技术面分析不可用）"
        
    logging.info("[P9 战报] Opus 综合分析（八层数据）")
    
    # Prune noise
    if p7_deep:
        p7_deep = re.sub(r'\n---\n【(?:摘要提取|正文深挖·一手源)】.*?(?=\n###|\Z)', '', p7_deep, flags=re.DOTALL)
    if p6_mapping:
        p6_mapping = re.sub(r'(\|\s*(?:🔥高弹性|⚡中弹性|💀纯概念\s*)\|.*)', r'\1', p6_mapping)
        # 移除 P6 尾部的工具调用遗留文本
        p6_mapping = re.sub(r'^(?:SEARCH|DEEP_RESEARCH):\s*.*$', '', p6_mapping, flags=re.MULTILINE).strip()
        
    today_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")
    
    if p7_deep and len(p7_deep.strip()) > 50:
        p7_section = p7_deep.strip()
    else:
        p7_section = "（本次运行无 P7 深度研究数据）"
        
    if p75_supplement and len(p75_supplement.strip()) > 50:
        p75_section = p75_supplement.strip()
    else:
        p75_section = "（本次运行无 P7.5 补充搜索数据）"
        
    if p2_x_intel_data and len(p2_x_intel_data.strip()) > 50:
        p2_section = p2_x_intel_data.strip()
    else:
        p2_section = "（本次运行无 P2 X平台情报数据）"
        
    if p3_intel_data and len(p3_intel_data.strip()) > 50:
        p3_section = p3_intel_data.strip()
    else:
        p3_section = "（本次运行无 P3 机动情报数据）"
        
    if p4_yfinance and len(p4_yfinance.strip()) > 50:
        yfinance_section = p4_yfinance.strip()
    else:
        yfinance_section = "（本次运行无 P4 海外行情数据）"
        
    try:
        prompt_template = require_prompt("p9_report.txt")
    except FileNotFoundError as e:
        logging.error(f"🚨 [P9 战报] 关键 Prompt 缺失: {e}")
        update_run_manifest(
            'p9',
            status='failed_missing_prompt',
            metrics={'error': str(e)},
        )
        return "", run_ts

    prompt = prompt_template.format(
        today_str=today_str, 
        p5_evidence=p5_evidence, 
        p7_section=p7_section, 
        p6_mapping=p6_mapping, 
        p75_section=p75_section, 
        price_data=price_data, 
        p2_section=p2_section, 
        p3_section=p3_section, 
        yfinance_section=yfinance_section
    )
    
    resp = opus_adapter.invoke(prompt)
    raw_content = resp.content if resp.content else ''
    thinking_content = getattr(resp, 'thinking_content', '')
    
    if thinking_content:
        try:
            out_fn = write_text_artifact("P9_Opus推理链", thinking_content)
            logging.info(f"[P9] 已转存 Opus 推理链至 {out_fn}")
        except Exception as e:
            logging.error(f"[P9] 保存 Opus 推理链失败: {e}")
            out_fn = ''
    else:
        out_fn = ''

    if not raw_content:
        logging.error("[P9 战报] 失败: 大模型返回为空")
        update_run_manifest(
            'p9',
            status='failed_empty_output',
            files={'thinking': out_fn} if out_fn else None,
            metrics={'price_data_chars': len(price_data or '')},
        )
        return "", run_ts
        
    try:
        data = extract_json_from_llm_response(raw_content, expect_type='dict')
        # Why: JSON 落盘统一在函数末尾执行一次，此处不再重复写入
            
        result = build_report_markdown(data, today_str)
        logging.info(f"✅ [P9 战报] 成功从 JSON 数据还原出 Markdown 排版 ({len(result)} 字)")
        
        try:
            json_fname = write_json_artifact("战报", data)
            manifest_files = {'json': json_fname}
            if out_fn:
                manifest_files['thinking'] = out_fn
            update_run_manifest(
                'p9',
                status='json_ready',
                files=manifest_files,
                metrics={'markdown_chars': len(result), 'raw_output_chars': len(raw_content)},
            )
        except Exception as e:
            logging.error(f"❌ [P9] 战报 JSON 写入失败: {e}")
            update_run_manifest(
                'p9',
                status='json_ready',
                files={'thinking': out_fn} if out_fn else None,
                metrics={'markdown_chars': len(result), 'raw_output_chars': len(raw_content)},
            )
            
        return result, run_ts
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ [P9 战报] JSON解析绝望失败，降级返回大模型幻觉全文: {e}\n{raw_content[:200]}")
        update_run_manifest(
            'p9',
            status='fallback_raw_llm',
            files={'thinking': out_fn} if out_fn else None,
            metrics={'raw_output_chars': len(raw_content), 'error': str(e)},
        )
        return raw_content, run_ts

def send_report_email(content, now_str: str = ''):
    """
    保存战报并通过 QQ SMTP 邮件推送。
    """
    if not content or not content.strip():
        logging.error("❌ [P9] 最终战报为空，跳过落盘和邮件推送")
        update_run_manifest('p9', status='failed_empty_report')
        return None

    now = datetime.now()
    # Why: 复用 P9 传入的 now_str 确保 MD 和 JSON 文件名时间戳一致
    file_ts = now_str if now_str else get_run_timestamp()
    try:
        fname = write_text_artifact("战报", content, timestamp=file_ts)
        logging.info(f"战报已保存: {fname}")
    except Exception as e:
        logging.error(f"❌ [P9] 最终战报 MD 写入失败: {e}")
        update_run_manifest('p9', status='failed_md_write', metrics={'error': str(e)})
        return None
        
    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL]):
        logging.warning("邮件未配置")
        update_run_manifest(
            'p9',
            status='completed_no_email',
            files={'md': fname},
            metrics={'email_configured': False},
        )
        return None
        
    try:
        send_report_via_email(
            content,
            sender_email=EMAIL_ADDRESS,
            sender_password=EMAIL_PASSWORD,
            receiver_email=RECEIVER_EMAIL,
            now=now,
        )
        logging.info("邮件已推送")
        update_run_manifest(
            'p9',
            status='completed_email_sent',
            files={'md': fname},
            metrics={'email_configured': True, 'email_sent': True},
        )
    except Exception as e:
        logging.error(f"邮件失败: {e}")
        update_run_manifest(
            'p9',
            status='completed_email_failed',
            files={'md': fname},
            metrics={'email_configured': True, 'email_sent': False, 'error': str(e)},
        )
