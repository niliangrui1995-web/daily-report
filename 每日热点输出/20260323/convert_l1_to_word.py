"""
L1 原始数据 JSON → 中文 Word 文档转换器
将 Tavily 搜索返回的英文原始数据，按分类结构化输出为可读的 Word 文档。
"""
import json
import re
import os
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

# 分类中文翻译
CATEGORY_CN = {
    "算力核心": "算力核心（GPU/ASIC芯片）",
    "PCB/CCL材料": "PCB/CCL 高速材料",
    "液冷全链路": "液冷散热全链路",
    "先进封装": "先进封装（CoWoS/HBM）",
    "光模块": "光模块/光通信",
    "存储HBM": "存储器/HBM",
    "AI应用": "AI 应用与商业化",
    "电源管理": "电源管理/供电",
    "连接器": "高速连接器",
}

def clean_text(text: str) -> str:
    """清理搜索结果中的无关内容（导航栏、广告等）"""
    # 去除 markdown 图片链接
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # 去除 markdown 链接，只保留文字
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # 去除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def extract_articles(text: str) -> list:
    """从搜索结果文本中提取各条文章摘要"""
    articles = []
    # 按 "数字. [标题]" 分割
    parts = re.split(r'\n(\d+)\.\s*\[', text)
    
    if len(parts) <= 1:
        # 无法分割，整段作为一条
        return [{"title": "搜索摘要", "content": clean_text(text)}]
    
    # 提取搜索摘要（第一段概述）
    summary_match = re.match(r'\[搜索摘要\]\s*(.*?)(?=\n1\.\s*\[)', text, re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else ""
    if summary:
        articles.append({"title": "📋 搜索总结", "content": summary})
    
    # parts[0] 是摘要部分，后面按对分割
    for i in range(1, len(parts) - 1, 2):
        num = parts[i]
        raw = parts[i + 1]
        # 提取标题（到 ] 为止）
        title_end = raw.find(']')
        if title_end > 0:
            title = raw[:title_end].strip()
            content = raw[title_end + 1:].strip()
            # 去除来源标记 【...】
            content = re.sub(r'【[^】]*】', '', content)
            content = clean_text(content)
            # 截取前 800 字符，避免过长
            if len(content) > 800:
                content = content[:800] + "……（篇幅过长已截断）"
            articles.append({"title": f"{num}. {title}", "content": content})
    
    return articles

def create_word(json_path: str, output_path: str):
    """生成 Word 文档"""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    doc = Document()
    
    # 设置默认字体
    style = doc.styles['Normal']
    font = style.font
    font.name = '微软雅黑'
    font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    
    # 标题
    title = doc.add_heading('L1 原始搜索数据报告', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # 元信息
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(f"生成时间：{data.get('time', 'N/A')}    搜索模型：{data.get('model', 'N/A')}")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)
    
    doc.add_paragraph()  # 空行
    
    # 统计信息
    results = data.get('results', [])
    categories = {}
    for r in results:
        cat = r.get('category', '未分类')
        categories.setdefault(cat, []).append(r)
    
    summary_para = doc.add_paragraph()
    summary_para.add_run(f"共 {len(results)} 条搜索查询，覆盖 {len(categories)} 个分类板块：").bold = True
    for cat, items in categories.items():
        cn_cat = CATEGORY_CN.get(cat, cat)
        doc.add_paragraph(f"• {cn_cat}：{len(items)} 条查询", style='List Bullet')
    
    doc.add_page_break()
    
    # 按分类输出
    cat_idx = 0
    for cat, items in categories.items():
        cat_idx += 1
        cn_cat = CATEGORY_CN.get(cat, cat)
        
        # 分类大标题
        heading = doc.add_heading(f"第{cat_idx}部分：{cn_cat}", level=1)
        
        for q_idx, item in enumerate(items, 1):
            query = item.get('query', '')
            text = item.get('text', '')
            sources = item.get('grounding_sources', [])
            
            # 查询标题
            doc.add_heading(f"{cat_idx}.{q_idx} 搜索查询", level=2)
            
            # 查询关键词
            kw_para = doc.add_paragraph()
            kw_para.add_run("🔍 关键词：").bold = True
            kw_para.add_run(query)
            
            # 解析各条文章
            articles = extract_articles(text)
            
            for article in articles:
                # 文章标题
                art_heading = doc.add_paragraph()
                run = art_heading.add_run(article['title'])
                run.bold = True
                run.font.size = Pt(11)
                run.font.color.rgb = RGBColor(0, 51, 153)
                
                # 文章内容
                content = article['content']
                for para_text in content.split('\n\n'):
                    para_text = para_text.strip()
                    if para_text:
                        p = doc.add_paragraph(para_text)
                        p.paragraph_format.space_after = Pt(4)
                        for run in p.runs:
                            run.font.size = Pt(9.5)
            
            # 信息来源
            if sources:
                src_heading = doc.add_paragraph()
                src_heading.add_run("📚 信息来源：").bold = True
                for s_idx, src in enumerate(sources, 1):
                    title_text = src.get('title', 'N/A')
                    url = src.get('url', '')
                    src_para = doc.add_paragraph(f"  {s_idx}. {title_text}")
                    src_para.paragraph_format.space_after = Pt(2)
                    for run in src_para.runs:
                        run.font.size = Pt(8)
                        run.font.color.rgb = RGBColor(100, 100, 100)
                    if url:
                        url_para = doc.add_paragraph(f"     {url}")
                        url_para.paragraph_format.space_after = Pt(1)
                        for run in url_para.runs:
                            run.font.size = Pt(7)
                            run.font.color.rgb = RGBColor(0, 102, 204)
            
            # 分隔线
            doc.add_paragraph("─" * 60)
        
        if cat_idx < len(categories):
            doc.add_page_break()
    
    # 保存
    doc.save(output_path)
    print(f"Word 文档已生成：{output_path}")
    print(f"共 {len(results)} 条查询，{len(categories)} 个分类")

if __name__ == '__main__':
    json_path = r'd:\vcp_hunter\每日热点汇总\每日热点输出\20260323\L1_原始数据_202603231132.json'
    output_path = r'd:\vcp_hunter\每日热点汇总\每日热点输出\20260323\L1_原始数据_中文报告.docx'
    create_word(json_path, output_path)
