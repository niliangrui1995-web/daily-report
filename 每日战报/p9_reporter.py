import re
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime

from artifact_store import get_run_timestamp, update_run_manifest, write_json_artifact, write_text_artifact
from settings import opus_adapter, EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL, require_prompt
from utils import extract_json_from_llm_response


def _format_confidence(confidence) -> str:
    if confidence in (None, ''):
        return ''
    try:
        value = float(confidence)
        if value <= 1:
            value *= 100
        return f'{value:.0f}%'
    except (TypeError, ValueError):
        return str(confidence).strip()


def _format_evidence_refs(evidence_refs) -> str:
    if not evidence_refs:
        return ''
    if isinstance(evidence_refs, list):
        return ' / '.join(str(item).strip() for item in evidence_refs if str(item).strip())
    return str(evidence_refs).strip()

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
            
        lines = []
        lines.append(f"# VCP Hunter P9 战报 | {today_str}\n")
        
        # X Platform Intel
        x_intel = data.get("x_intel_snipe", [])
        if x_intel:
            lines.append("## 🔴 X平台突发 (Grok未经审计)\n")
            for i, p in enumerate(x_intel, 1):
                if isinstance(p, dict):
                    title = p.get("title", f"情报{i}")
                    body = p.get("body", "")
                    lines.append(f"**{i}. 【{title}】**\n{body}\n")
                else:
                    lines.append(f"**{i}.** {str(p)}\n")
            lines.append("---\n")
            
        # P3 Machine Intel
        p3_intel = data.get("p3_machine_intel", [])
        if p3_intel:
            lines.append("## 🔵 P3机动情报 (Gemini防穿透)\n")
            for i, p in enumerate(p3_intel, 1):
                if isinstance(p, dict):
                    title = p.get("title", f"线索{i}")
                    body = p.get("body", "")
                    lines.append(f"**{i}. 【{title}】**\n{body}\n")
                else:
                    lines.append(f"**{i}.** {str(p)}\n")
            lines.append("---\n")
            
        # Core Signals
        lines.append("## 一、核心信号\n")
        signals = data.get("core_signals", [])
        if signals:
            for i, s in enumerate(signals, 1):
                if isinstance(s, dict):
                    source = s.get("source", "")
                    sig = s.get("significance", "")
                    title = s.get("title", f"信号{i}")
                    body = s.get("body", "")
                    
                    header_parts = []
                    if source:
                        header_parts.append(source)
                    if sig:
                        header_parts.append(sig)
                    
                    if header_parts:
                        prefix = f"[{' | '.join(header_parts)}]"
                    else:
                        prefix = ""
                        
                    lines.append(f"**{i}. {prefix} {title}**\n{body}\n")
                    meta_parts = []
                    confidence_text = _format_confidence(s.get("confidence"))
                    evidence_text = _format_evidence_refs(s.get("evidence_refs"))
                    if confidence_text:
                        meta_parts.append(f"置信度 {confidence_text}")
                    if evidence_text:
                        meta_parts.append(f"证据 {evidence_text}")
                    if meta_parts:
                        lines.append(f"> {' | '.join(meta_parts)}\n")
                else:
                    content = str(s)
                    lines.append(f"**{i}.**\n{content}\n")
        else:
            lines.append("今日无明显核心信号。\n")
            
        lines.append("---\n")
        
        # Sniper Tables
        lines.append("## 二、标的狙击表\n")
        tables = data.get("sniper_tables", [])
        
        # fallback to sniper_table
        if not tables and data.get("sniper_table"):
            tables = [{"track_name": "核心标的", "targets": data.get("sniper_table")}]
            
        if tables:
            for t in tables:
                track = t.get("track_name", "赛道：未知板块")
                lines.append(f"### {track}\n")
                lines.append("| 标的 | 代码 | 最新价 | 近3月 | 250日分位 | 股价弹性 | RS强度 | 周线趋势 | 催化剂窗口 | 风控 |")
                lines.append("|---|---|---|---|---|---|---|---|---|---|")
                
                targets = t.get("targets", [])
                if targets:
                    for row in targets:
                        cells = [
                            f"**{row.get('name', '无')}**",
                            row.get("code", "无"),
                            row.get("price", "无"),
                            row.get("chg_3m", "无"),
                            row.get("percentile_250d", "无"),
                            row.get("elasticity", "无"),
                            row.get("rs_strength", "无"),
                            row.get("weekly_level", "无"),
                            row.get("catalyst", "无"),
                            row.get("risk", "无")
                        ]
                        lines.append(f"| {' | '.join(cells)} |")
                else:
                    lines.append("| 无符合条件的标的 | - | - | - | - | - | - | - | - | - |")
                lines.append("")
        else:
            lines.append("| 无符合条件的标的 | - | - | - | - | - | - | - | - | - |")
            lines.append("")
            
        lines.append("---\n")
        
        # Fund Pathway
        lines.append("## 三、资金路径推演\n")
        pathway = data.get("fund_pathway", [])
        if pathway:
            for i, p in enumerate(pathway, 1):
                if isinstance(p, dict):
                    title = p.get("title", p.get("name", f"路径{i}"))
                    cascade = p.get("cascade", p.get("content", p.get("detail", "")))
                    lines.append(f"### 路径{i}：{title}\n")
                    
                    cascade_lines = str(cascade).replace(" -> ", "\n  → ").replace(" → ", "\n  → ")
                    lines.append(f"> {cascade_lines.strip()}\n")
                else:
                    raw_p = str(p)
                    clean_p = raw_p
                    if clean_p.startswith(f"{i}. "):
                        clean_p = clean_p[len(f"{i}. "):]
                    elif clean_p.startswith(f"{i}."):
                        clean_p = clean_p[len(f"{i}.")]
                        
                    bracket_match = re.search(r"【(.*?)】", clean_p)
                    if bracket_match:
                        title = bracket_match.group(0)
                        body = clean_p.replace(title, "", 1).strip()
                        lines.append(f"### 路径{i}：{title}\n")
                        cascade_text = body.replace(" -> ", "\n> → ").replace(" → ", "\n> → ")
                        lines.append(f"> {cascade_text.strip()}\n")
                    elif "：" in clean_p:
                        title, cascade = clean_p.split("：", 1)
                        lines.append(f"### 路径{i}：{title.strip()}\n")
                        cascade_text = cascade.replace(" -> ", "\n> → ").replace(" → ", "\n> → ")
                        lines.append(f"> {cascade_text.strip()}\n")
                    else:
                        lines.append(f"### 路径{i}\n")
                        cascade_text = clean_p.replace(" -> ", "\n> → ").replace(" → ", "\n> → ")
                        lines.append(f"> {cascade_text.strip()}\n")
        else:
            lines.append("推演路径不明确。\n")
            
        lines.append("---\n")
        
        # Today Advice
        lines.append("## 四、今日操作建议\n")
        lines.append("| 优先级 | 标的 | 代码 | 核心理由 | 操作思路 | 置信度 | 证据 |")
        lines.append("|---|---|---|---|---|---|---|")
        
        advice = data.get("today_advice", [])
        if advice:
            for item in advice:
                if isinstance(item, dict):
                    priority = item.get("priority", "★★")
                    name = f"**{item.get('name', '无')}**"
                    code = item.get("code", "无")
                    reason = item.get("reason", "无")
                    strategy = item.get("strategy", "无")
                    confidence_text = _format_confidence(item.get("confidence")) or "未标注"
                    evidence_text = _format_evidence_refs(item.get("evidence_refs")) or "未标注"
                    cells = [priority, name, code, reason, strategy, confidence_text, evidence_text]
                    lines.append(f"| {' | '.join(cells)} |")
                else:
                    lines.append(f"| - | **-** | - | {str(item)} | - | 未标注 | 未标注 |")
        else:
            lines.append("| 暂无建议 | - | - | - | - | - | - |")
            
        lines.append("")
        lines.append("---\n")
        
        # Risk Warning
        lines.append("## ⚠️ 风险提示\n")
        risks = data.get("risk_warning", [])
        if risks:
            for idx, r in enumerate(risks, 1):
                lines.append(f"**{idx}.** ⚠️ {str(r)}\n")
        else:
            lines.append("暂无明显风险。\n")
            
        result = "\n".join(lines).strip()
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
        import markdown
        html = markdown.markdown(content, extensions=['tables', 'fenced_code'])
        today_fmt = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        html_full = f'''<html><head><meta charset="utf-8"><style>
            * {{margin:0;padding:0;box-sizing:border-box}}
            body {{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.9;color:#1a1a2e;padding:0;background:#f5f6fa;font-size:15px}}
            .wrap {{max-width:960px;margin:0;padding:24px}}
            h1 {{font-size:22px;color:#0f3460;border-bottom:3px solid #e94560;padding-bottom:8px;margin:28px 0 16px}}
            h2 {{font-size:19px;color:#16213e;border-left:4px solid #e94560;padding-left:12px;margin:24px 0 14px}}
            h3 {{font-size:16px;color:#533483;margin:18px 0 10px}}
            p {{margin:10px 0;font-size:15px}}
            ul,ol {{padding-left:24px;margin:10px 0}}
            li {{margin:5px 0;font-size:15px}}
            table {{border-collapse:collapse;width:100%;font-size:13px;margin:14px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
            th {{background:linear-gradient(135deg,#0f3460,#16213e);color:#fff;font-weight:600;padding:10px 10px;text-align:left;font-size:12px;white-space:nowrap}}
            td {{padding:9px 10px;border-bottom:1px solid #eef2f7;font-size:13px}}
            tr:nth-child(even) {{background:#f8fafc}}
            tr:hover {{background:#e8f4fd}}
            blockquote {{border-left:4px solid #e94560;padding:14px 18px;background:linear-gradient(135deg,#fff5f5,#fff);margin:14px 0;border-radius:0 8px 8px 0;font-size:15px}}
            strong {{color:#e94560}}
            code {{background:#f0f2f5;padding:2px 6px;border-radius:4px;font-size:14px;color:#e94560}}
            hr {{border:none;height:1px;background:linear-gradient(90deg,transparent,#dee2e6,transparent);margin:18px 0}}
            .hdr {{background:linear-gradient(135deg,#0f3460,#533483);color:#fff;padding:24px;border-radius:12px;margin-bottom:24px;text-align:center}}
            .hdr h1 {{color:#fff;border:none;margin:0;font-size:24px;letter-spacing:1px}}
            .hdr p {{color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:14px;border:none}}
            .ftr {{text-align:center;color:#999;font-size:12px;margin-top:28px;padding:16px 0;border-top:1px solid #eee}}
        </style></head><body>
        <div class="wrap">
        <div class="hdr"><h1>🎯 VCP Hunter AI 产业链战报</h1><p>{today_fmt} · 北美产业链情报 · 自动生成</p></div>
        {html}
        <div class="ftr">VCP Hunter v4.1 · 数据来源: Grok / RSS / Gemini / Opus / 通达信 · 仅供研究参考，不构成投资建议</div>
        </div>
        </body></html>'''
        msg = MIMEText(html_full, 'html', 'utf-8')
    except ImportError:
        msg = MIMEText(f"<pre>{content}</pre>", 'html', 'utf-8')
        
    msg['From'] = formataddr(('VCP Hunter', EMAIL_ADDRESS))
    msg['To'] = RECEIVER_EMAIL
    msg['Subject'] = Header(f"【AI产业链战报】{now.strftime('%m月%d日 %H:%M')}", 'utf-8')
    
    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465) as s:
            s.set_debuglevel(0)
            s.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            s.sendmail(EMAIL_ADDRESS, [e.strip() for e in RECEIVER_EMAIL.split(',')], msg.as_string())
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
