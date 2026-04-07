import os
import re
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime

from settings import opus_adapter, EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL, get_today_output_dir, load_prompt
from utils import extract_json_from_llm_response

def p9_risk_analysis(p5_evidence, p7_deep, p75_supplement, p6_mapping=None, price_data=None, p2_x_intel_data=None, p3_intel_data="", p4_yfinance=""):
    """
    P9 Opus 综合战报：融合八层数据生成最终战报。
    """
    if not price_data or len(price_data) < 50:
        logging.warning("[P9] 股价数据不足，战报将缺少技术面分析")
        price_data = "（本次运行未获取到通达信本地K线数据，技术面分析不可用）"
        
    logging.info("[P9 战报] Opus 综合分析（八层数据）")
    
    # Prune noise
    if p7_deep:
        p7_deep = re.sub(r'---[\s\n]*【摘要提取】.*?---', '', p7_deep, flags=re.DOTALL)
    if p6_mapping:
        p6_mapping = re.sub(r'(\|\s*(?:🔥高弹性|💧中弹性|🪨纯概念\s*)\|.*)', r'\1', p6_mapping)
        
    today_str = datetime.now().strftime("%Y年%m月%d日 北京时间%H:%M")
    
    if p7_deep and len(p7_deep.strip()) > 50:
        p7_section = f"==================== 第2层：P7 深度研究结果（☢最高价值！多轮迭代搜索）====================\n{p7_deep}\n==================== P7 深研结束 ===================="
    else:
        p7_section = "==================== 第2层：P7 深度研究结果 ====================\n（本次运行无 P7 深度研究数据）\n==================== P7 深研结束 ===================="
        
    if p75_supplement and len(p75_supplement.strip()) > 50:
        p75_section = f"==================== 第4层：P7.5 审计后补充搜索结果 ====================\n{p75_supplement}\n==================== P7.5 审计结束 ===================="
    else:
        p75_section = "==================== 第4层：P7.5 审计后补充搜索结果 ====================\n（本次运行无 P7.5 补充搜索数据）\n==================== P7.5 审计结束 ===================="
        
    if p2_x_intel_data and len(p2_x_intel_data.strip()) > 50:
        p2_section = f"==================== 第6层：P2 X平台实时情报（【🔥极度重要】Grok 采集未经审计）====================\n{p2_x_intel_data}\n==================== P2 X平台情报结束 ===================="
    else:
        p2_section = "==================== 第6层：P2 X平台实时情报 ====================\n（本次运行无 P2 X平台情报数据）\n==================== P2 X平台情报结束 ===================="
        
    if p3_intel_data and len(p3_intel_data.strip()) > 50:
        p3_section = f"==================== 第7层：P3 机动情报（Gemini 联网搜索，覆盖爬虫盲区）====================\n{p3_intel_data}\n==================== P3 机动情报结束 ===================="
    else:
        p3_section = "==================== 第7层：P3 机动情报 ====================\n（本次运行无 P3 机动情报数据）\n==================== P3 机动情报结束 ===================="
        
    if p4_yfinance and len(p4_yfinance.strip()) > 50:
        yfinance_section = f"==================== 第8层：P4 海外客观行情（yfinance 真实资金投票）====================\n{p4_yfinance}\n==================== P4 海外行情结束 ===================="
    else:
        yfinance_section = "==================== 第8层：P4 海外客观行情 ====================\n（本次运行无 P4 海外行情数据）\n==================== P4 海外行情结束 ===================="
        
    prompt_template = load_prompt("p9_report.txt")
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
        now = datetime.now()
        out_fn = os.path.join(get_today_output_dir(), f"P9_Opus推理链_{now.strftime('%Y%m%d%H%M')}.md")
        try:
            with open(out_fn, 'w', encoding='utf-8') as f:
                f.write(thinking_content)
            logging.info(f"[P9] 已转存 Opus 推理链至 {out_fn}")
        except Exception as e:
            logging.error(f"[P9] 保存 Opus 推理链失败: {e}")

    if not raw_content:
        logging.error("[P9 战报] 失败: 大模型返回为空")
        return ""
        
    try:
        data = extract_json_from_llm_response(raw_content, expect_type='dict')
        
        # Save JSON output for debugging
        now = datetime.now()
        json_out = os.path.join(get_today_output_dir(), f"战报_{now.strftime('%Y%m%d%H%M')}.json")
        try:
            with open(json_out, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"❌ [P9] 战报中间 JSON 写入失败: {e}")
            
        lines = []
        lines.append(f"# VCP Hunter P9 战报 | {today_str}\n")
        
        # X Platform Intel
        x_intel = data.get("x_intel_snipe", [])
        if x_intel:
            lines.append("## 🔴 X平合突发 (Grok未经审计)\n")
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
        lines.append("| 优先级 | 标的 | 代码 | 核心理由 | 操作思路 |")
        lines.append("|---|---|---|---|---|")
        
        advice = data.get("today_advice", [])
        if advice:
            for item in advice:
                if isinstance(item, dict):
                    priority = item.get("priority", "★★")
                    name = f"**{item.get('name', '无')}**"
                    code = item.get("code", "无")
                    reason = item.get("reason", "无")
                    strategy = item.get("strategy", "无")
                    cells = [priority, name, code, reason, strategy]
                    lines.append(f"| {' | '.join(cells)} |")
                else:
                    lines.append(f"| - | **-** | - | {str(item)} | - |")
        else:
            lines.append("| 暂无建议 | - | - | - | - |")
            
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
        
        # Save JSON to disk (Added to match old implementation)
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        json_fname = os.path.join(get_today_output_dir(), f"战报_{now_str}.json")
        try:
            with open(json_fname, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"❌ [P9] 战报 JSON 写入失败: {e}")
            
        return result
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ [P9 战报] JSON解析绝望失败，降级返回大模型幻觉全文: {e}\n{raw_content[:200]}")
        return raw_content

def send_report_email(content):
    """
    保存战报并通过 QQ SMTP 邮件推送。
    """
    now = datetime.now()
    fname = os.path.join(get_today_output_dir(), f"战报_{now.strftime('%Y%m%d%H%M')}.md")
    try:
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"战报已保存: {fname}")
    except Exception as e:
        logging.error(f"❌ [P9] 最终战报 MD 写入失败: {e}")
        
    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL]):
        logging.warning("邮件未配置")
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
        logging.info(f"邮件已推送至 {RECEIVER_EMAIL}")
    except Exception as e:
        logging.error(f"邮件失败: {e}")
