"""
dashboard_ui.py — Streamlit 实时情报大屏
A 股配色（红涨绿跌）、5星音频警报、赛道/紧急度/情感过滤、一键溯源原文。
"""
import os
import sys
import json

# 将当前目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from db import init_db, query_news, get_stats
from config import TRACK_LABELS

# ==============================================================================
# 页面配置
# ==============================================================================
st.set_page_config(
    page_title="🚨 VCP Hunter 实时情报雷达",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 30 秒自动刷新
st_autorefresh(interval=30_000, limit=None, key="auto_refresh")

# ==============================================================================
# 自定义 CSS（A 股红绿配色 + 闪烁动画）
# ==============================================================================
st.markdown("""
<style>
    /* 全局暗色主题增强 */
    .stApp { background-color: #0E1117; }

    /* 统计卡片 */
    .stat-card {
        background: linear-gradient(135deg, #1a1f2e, #252b3b);
        border-radius: 12px;
        padding: 16px 20px;
        text-align: center;
        border: 1px solid #333;
    }
    .stat-number { font-size: 32px; font-weight: 800; }
    .stat-label { font-size: 13px; color: #888; margin-top: 4px; }

    /* 紧急度配色 */
    .urgency-5 {
        background: linear-gradient(135deg, #8B0000, #B22222) !important;
        border-left: 5px solid #FF0000;
        animation: blink 1s infinite;
    }
    .urgency-4 {
        background: linear-gradient(135deg, #8B4513, #CD6600) !important;
        border-left: 5px solid #FF8C00;
    }
    .urgency-3 {
        background: linear-gradient(135deg, #333300, #555522) !important;
        border-left: 5px solid #FFD700;
    }
    .urgency-2 {
        background: #1a1f2e !important;
        border-left: 3px solid #555;
    }
    .urgency-1 {
        background: #151820 !important;
        border-left: 3px solid #333;
        opacity: 0.7;
    }

    /* 新闻卡片 */
    .news-card {
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 10px;
        transition: transform 0.2s;
    }
    .news-card:hover { transform: translateX(4px); }
    .news-title { font-size: 16px; font-weight: 700; color: #F0F0F0; }
    .news-summary { font-size: 13px; color: #AAA; margin-top: 4px; }
    .news-meta { font-size: 12px; color: #777; margin-top: 8px; }

    /* A 股配色：利好红、利空绿 */
    .tag-bullish {
        background: #CC0000; color: white;
        padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600;
    }
    .tag-bearish {
        background: #00AA00; color: white;
        padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600;
    }
    .tag-neutral {
        background: #555; color: #CCC;
        padding: 2px 8px; border-radius: 4px; font-size: 12px;
    }
    .tag-track {
        background: #1E3A5F; color: #7CB9E8;
        padding: 2px 8px; border-radius: 4px; font-size: 11px;
    }
    .tag-company {
        background: #2D1B4E; color: #C8A2FF;
        padding: 2px 8px; border-radius: 4px; font-size: 11px;
        cursor: pointer;
    }
    .tag-mapping {
        background: #3D1919; color: #FF9999;
        padding: 2px 8px; border-radius: 4px; font-size: 11px;
    }

    /* 5星爆料闪烁 */
    @keyframes blink {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
    }

    /* 隐藏 Streamlit 默认的 footer 和 hamburger menu */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# ==============================================================================
# 5 星音频警报 (JS 注入)
# ==============================================================================
def inject_alarm_audio(has_5star: bool):
    """当存在 5 星爆料时，通过 JS 播放浏览器提示音"""
    if has_5star:
        # 使用 Web Audio API 生成警报音（无需外部音频文件）
        st.markdown("""
        <script>
        (function() {
            if (window._alarmPlayed) return;
            window._alarmPlayed = true;
            try {
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                var osc = ctx.createOscillator();
                var gain = ctx.createGain();
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.frequency.value = 880;
                osc.type = 'square';
                gain.gain.value = 0.3;
                osc.start();
                setTimeout(function() { osc.stop(); }, 500);
                setTimeout(function() {
                    var osc2 = ctx.createOscillator();
                    osc2.connect(gain);
                    osc2.frequency.value = 1200;
                    osc2.type = 'square';
                    osc2.start();
                    setTimeout(function() { osc2.stop(); }, 500);
                }, 600);
            } catch(e) {}
        })();
        </script>
        """, unsafe_allow_html=True)


# ==============================================================================
# 辅助函数
# ==============================================================================

def urgency_icon(level: int) -> str:
    icons = {5: "🚨", 4: "🔴", 3: "⚡", 2: "📰", 1: "💤"}
    return icons.get(level, "📰")


def sentiment_tag(s: str) -> str:
    if s == "利好":
        return '<span class="tag-bullish">🔴 利好</span>'
    elif s == "利空":
        return '<span class="tag-bearish">🟢 利空</span>'
    return '<span class="tag-neutral">⚪ 中性</span>'


def render_news_card(item: dict):
    """渲染单条新闻卡片"""
    urgency = item.get("urgency", 1)
    cn_title = item.get("cn_title", item.get("raw_title", "无标题"))
    cn_summary = item.get("cn_summary", "")
    track = item.get("track", "")
    companies_raw = item.get("companies", "[]")
    mapping_raw = item.get("a_share_mapping", "[]")
    sent = item.get("sentiment", "中性")
    url = item.get("url", "")
    created = item.get("created_at", "")
    source = item.get("source", "")

    # 解析 JSON 字段
    try:
        companies = json.loads(companies_raw) if isinstance(companies_raw, str) else (companies_raw or [])
    except (json.JSONDecodeError, TypeError):
        companies = []
    try:
        mapping = json.loads(mapping_raw) if isinstance(mapping_raw, str) else (mapping_raw or [])
    except (json.JSONDecodeError, TypeError):
        mapping = []

    # 时间显示
    time_display = ""
    if created:
        try:
            time_display = created.split(" ")[-1][:5] if " " in str(created) else str(created)[:5]
        except Exception:
            time_display = str(created)[:10]

    # 公司标签
    company_tags = " ".join(f'<span class="tag-company">{c}</span>' for c in companies[:5])
    # A 股映射标签
    mapping_tags = ""
    if mapping:
        label = "关联暴雷" if sent == "利空" else "直接催化" if sent == "利好" else "相关标的"
        mapping_str = "、".join(mapping[:4])
        mapping_tags = f'<span class="tag-mapping">{label}: {mapping_str}</span>'

    # 溯源链接
    source_link = ""
    if url:
        source_link = f'<a href="{url}" target="_blank" style="color:#7CB9E8;font-size:12px;text-decoration:none;">🔗 原文查证</a>'

    icon = urgency_icon(urgency)

    html = f"""
    <div class="news-card urgency-{urgency}">
        <div>
            {icon} <span style="color:#888;font-size:12px;">[{time_display}]</span>
            <span style="font-size:12px;">({'⭐' * urgency})</span>
            {sentiment_tag(sent)}
            <span class="tag-track">{track}</span>
        </div>
        <div class="news-title" style="margin-top:6px;">{cn_title}</div>
        <div class="news-summary">{cn_summary}</div>
        <div class="news-meta">
            {company_tags}
            {f'<br>' if mapping_tags else ''}{mapping_tags}
            <span style="float:right;">{source_link} <span style="color:#555;margin-left:8px;">via {source}</span></span>
        </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


# ==============================================================================
# 主界面
# ==============================================================================

def main():
    # 初始化数据库（确保表存在）
    init_db()

    # ── 头部标题 ──
    st.markdown("""
    <div style="text-align:center;padding:10px 0 20px 0;">
        <h1 style="margin:0;font-size:28px;">🚨 VCP Hunter 实时情报雷达</h1>
        <p style="color:#666;font-size:13px;margin:4px 0 0 0;">AI 产业链 7×24 突发新闻监控 · 每 30 秒自动刷新</p>
    </div>
    """, unsafe_allow_html=True)

    # ── 侧边栏过滤引擎 ──
    with st.sidebar:
        st.markdown("### 📌 快速过滤引擎")

        st.markdown("**⚡ 紧急度**")
        min_urgency = st.slider("最低紧急度", 1, 5, 1, key="urgency_slider")

        st.markdown("**🎯 覆盖赛道**")
        all_tracks = st.checkbox("全部 16 个赛道", value=True, key="all_tracks")
        selected_tracks = None
        if not all_tracks:
            selected_tracks = st.multiselect("选择赛道", TRACK_LABELS, key="track_select")

        st.markdown("**📰 情感倾向**")
        sentiment_filter = st.radio(
            "筛选",
            ["全部", "只看暴雷 (利空)", "只看催化 (利好)"],
            key="sentiment_radio",
        )
        sentiment_val = None
        if sentiment_filter == "只看暴雷 (利空)":
            sentiment_val = "利空"
        elif sentiment_filter == "只看催化 (利好)":
            sentiment_val = "利好"

        st.markdown("**🕒 时间切片**")
        time_range = st.selectbox(
            "时间范围",
            [("过去 1 小时", 1), ("过去 6 小时", 6), ("过去 12 小时", 12), ("过去 24 小时", 24), ("过去 72 小时", 72)],
            index=3,
            format_func=lambda x: x[0],
            key="time_range",
        )
        hours = time_range[1]

        st.markdown("**🔍 公司快搜**")
        company_search = st.text_input("输入公司名或 A 股简称", key="company_search")

    # ── 统计卡片 ──
    stats = get_stats(hours=hours)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-number" style="color:#7CB9E8;">{stats['total']}</div>
            <div class="stat-label">今日总情报</div>
        </div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-number" style="color:#FF4444;">{stats['urgent_5star']}</div>
            <div class="stat-label">🚨 5星爆料</div>
        </div>""", unsafe_allow_html=True)
    with c3:
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-number" style="color:#CC0000;">{stats['bullish']}</div>
            <div class="stat-label">🔴 利好信号</div>
        </div>""", unsafe_allow_html=True)
    with c4:
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-number" style="color:#00AA00;">{stats['bearish']}</div>
            <div class="stat-label">🟢 利空预警</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── 实时信息流 ──
    news_list = query_news(
        hours=hours,
        tracks=selected_tracks if not all_tracks and selected_tracks else None,
        min_urgency=min_urgency,
        sentiment=sentiment_val,
        company=company_search if company_search else None,
        limit=100,
    )

    # 5 星警报注入
    has_5star = any(n.get("urgency", 0) >= 5 for n in news_list)
    inject_alarm_audio(has_5star)

    if not news_list:
        st.info("📭 当前时间范围内无匹配的情报。等待后台采集器抓取新数据...")
    else:
        st.markdown(f"**⬇️ 实时突发信息流** — 共 {len(news_list)} 条")
        for item in news_list:
            render_news_card(item)

    # ── 页脚 ──
    st.markdown("""
    <div style="text-align:center;padding:30px 0 10px 0;color:#444;font-size:12px;">
        © VCP Hunter 实时情报雷达 v1.0 · 后台采集器 + Gemini Flash 洗稿 + Streamlit 大屏
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
