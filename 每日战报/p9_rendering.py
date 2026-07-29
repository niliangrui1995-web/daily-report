"""P9 markdown rendering helpers."""

from __future__ import annotations

import re


def format_confidence(confidence) -> str:
    if confidence in (None, ''):
        return ''
    try:
        value = float(confidence)
        if value <= 1:
            value *= 100
        return f'{value:.0f}%'
    except (TypeError, ValueError):
        return str(confidence).strip()


def format_evidence_refs(evidence_refs) -> str:
    if not evidence_refs:
        return ''
    if isinstance(evidence_refs, list):
        return ' / '.join(str(item).strip() for item in evidence_refs if str(item).strip())
    return str(evidence_refs).strip()


def build_report_markdown(data: dict, today_str: str) -> str:
    lines = []
    lines.append(f"# VCP Hunter P9 战报 | {today_str}\n")

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

                prefix = f"[{' | '.join(header_parts)}]" if header_parts else ""
                lines.append(f"**{i}. {prefix} {title}**\n{body}\n")

                meta_parts = []
                confidence_text = format_confidence(s.get("confidence"))
                evidence_text = format_evidence_refs(s.get("evidence_refs"))
                if confidence_text:
                    meta_parts.append(f"置信度 {confidence_text}")
                if evidence_text:
                    meta_parts.append(f"证据 {evidence_text}")
                if meta_parts:
                    lines.append(f"> {' | '.join(meta_parts)}\n")
            else:
                lines.append(f"**{i}.**\n{str(s)}\n")
    else:
        lines.append("今日无明显核心信号。\n")

    lines.append("---\n")
    lines.append("## 二、标的狙击表\n")
    tables = data.get("sniper_tables", [])
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
                        row.get("risk", "无"),
                    ]
                    lines.append(f"| {' | '.join(cells)} |")
            else:
                lines.append("| 无符合条件的标的 | - | - | - | - | - | - | - | - | - |")
            lines.append("")
    else:
        lines.append("| 无符合条件的标的 | - | - | - | - | - | - | - | - | - |")
        lines.append("")

    lines.append("---\n")
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
                clean_p = str(p)
                if clean_p.startswith(f"{i}. "):
                    clean_p = clean_p[len(f"{i}. "):]
                elif clean_p.startswith(f"{i}."):
                    clean_p = clean_p[len(f"{i}."):]

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
                confidence_text = format_confidence(item.get("confidence")) or "未标注"
                evidence_text = format_evidence_refs(item.get("evidence_refs")) or "未标注"
                lines.append(f"| {' | '.join([priority, name, code, reason, strategy, confidence_text, evidence_text])} |")
            else:
                lines.append(f"| - | **-** | - | {str(item)} | - | 未标注 | 未标注 |")
    else:
        lines.append("| 暂无建议 | - | - | - | - | - | - |")

    lines.append("")
    lines.append("---\n")
    lines.append("## ⚠️ 风险提示\n")
    risks = data.get("risk_warning", [])
    if risks:
        for idx, r in enumerate(risks, 1):
            lines.append(f"**{idx}.** ⚠️ {str(r)}\n")
    else:
        lines.append("暂无明显风险。\n")

    return "\n".join(lines).strip()


def build_degraded_checkpoint_report(
    sections: dict[str, str],
    today_str: str,
    checkpoint_labels: list[str],
) -> str:
    """Render a deterministic, non-synthetic report from validated checkpoints."""

    lines = [
        f"# VCP Hunter P9 战报（降级生成） | {today_str}\n",
        "> 【降级生成】P9 主模型与备用模型均未返回可用结果；本报告由已完成且有效的 "
        "P1–P8 checkpoint 在本地直接渲染。\n",
        "> 降级边界：未使用无效 P9 原始输出，不新增模型归纳、标的筛选、资金路径或操作建议。\n",
        f"> 已验证 checkpoint：{'、'.join(checkpoint_labels)}\n",
    ]

    def append_checkpoint(title: str, key: str) -> None:
        content = str(sections.get(key) or "").strip()
        if not content:
            return
        lines.append(f"### {title}\n")
        lines.append("\n".join(f"> {line}" if line else ">" for line in content.splitlines()))
        lines.append("")

    lines.append("## 一、核心信号\n")
    lines.append("P9 未进行二次归纳；以下保留 P5 审计结果与 P7a 已审计深研 checkpoint。\n")
    append_checkpoint("P5 审计结果（承接 P1）", "p5")
    append_checkpoint("P7a 已审计深研", "p7a")

    lines.append("---\n")
    lines.append("## 二、标的狙击表\n")
    lines.append("P9 未重新筛选标的；以下保留 P6 映射与 P8 技术面 checkpoint，不能视为新增推荐。\n")
    append_checkpoint("P6 市场映射", "p6")
    append_checkpoint("P8 技术面", "p8")

    lines.append("---\n")
    lines.append("## 三、资金路径推演\n")
    lines.append("P9 未生成新的资金路径；以下仅保留 P7.5 补充审计 checkpoint。\n")
    append_checkpoint("P7.5 补充审计", "p75")

    lines.append("---\n")
    lines.append("## 四、今日操作建议\n")
    lines.append("降级模式不生成新的买卖建议；以下 P4 行情 checkpoint 仅供核对市场状态。\n")
    append_checkpoint("P4 核心行情", "p4")

    lines.append("---\n")
    lines.append("## ⚠️ 风险提示\n")
    lines.append("**模型生成风险：** P9 主模型与备用模型均失败，本报告没有完成常规 P9 综合判断。\n")
    lines.append("**使用边界：** P2/P3 属于软情报层，必须结合 P5–P8 的硬证据继续核验。\n")
    append_checkpoint("P2 X 平台情报", "p2")
    append_checkpoint("P3 机动情报", "p3")

    return "\n".join(lines).strip()
