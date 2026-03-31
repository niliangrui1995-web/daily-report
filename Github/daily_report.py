import os, re, json, time, random, logging, smtplib, requests, threading
from datetime import datetime, timedelta, date
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from ratelimit import limits, sleep_and_retry

import yfinance as yf
from google import genai
from google.genai import types as genai_types

from native_llm_adapter import NativeLLMAdapter

# ==============================================================================
# VCP Hunter 核心引擎 v3.1 (抗风控重构版)
# 架构：L1 Flash(搜集) → L1.5 Pro(审计清洗) → L2 Opus(映射) → L3 Flash(补充)+通达信 → L3.5 Pro(审计) → L4 Opus(战报)
# 备用：Tavily（仅当 Gemini 搜索成功率 < 50% 时启用）
# ==============================================================================

# ----------------- A股交易日历 -----------------
HOLIDAYS_2026 = {
    (1, 1), (1, 2), (1, 26), (1, 27), (1, 28), (1, 29), (1, 30),
    (4, 6), (5, 1), (5, 2), (5, 5), (5, 31), (6, 1),
    (10, 1), (10, 2), (10, 5), (10, 6), (10, 7), (10, 8),
}
WORKDAYS_2026 = {(1, 24), (2, 1), (4, 4), (4, 26), (10, 10)}

def is_trading_day(d: date) -> bool:
    md = (d.month, d.day)
    if md in HOLIDAYS_2026:
        return False
    if d.weekday() >= 5:
        return md in WORKDAYS_2026
    return True

# ----------------- 配置 -----------------
ANTIGRAVITY_BASE_URL = os.environ.get("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:8045")
ANTIGRAVITY_API_KEY = os.environ.get("ANTIGRAVITY_API_KEY", "REMOVED_CREDENTIAL")

# === Gemini 官方搜索配置（通过 Cloudflare Worker 跳板访问 Google API） ===
# 支持多 Key 轮换：GEMINI_SEARCH_API_KEYS（逗号分隔），429 时自动切换到下一个 Key
GEMINI_SEARCH_BASE_URL = os.environ.get("GEMINI_SEARCH_BASE_URL", "https://api.niliangrui.cloud")
_search_keys_env = os.environ.get("GEMINI_SEARCH_API_KEYS", "")
_search_single_key = os.environ.get("GEMINI_SEARCH_API_KEY", "")

# 5 个不同项目的 Key，每个 500 次/天，总计 2500 次/天
DEFAULT_SEARCH_KEY_POOL = [
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
]

if _search_keys_env:
    SEARCH_API_KEY_POOL = [k.strip() for k in _search_keys_env.split(",") if k.strip()]
elif _search_single_key:
    SEARCH_API_KEY_POOL = [_search_single_key]
else:
    SEARCH_API_KEY_POOL = DEFAULT_SEARCH_KEY_POOL

# 【风控修改】伪装正常的浏览器 User-Agent，抹除 Python SDK 的机器指纹
FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


class SearchKeyRotator:
    """线程安全的搜索 API Key 轮换器"""
    def __init__(self, keys, base_url):
        self._keys = keys
        self._index = 0
        self._exhausted = set()
        self._lock = threading.Lock()
        self._clients = {}
        for i, key in enumerate(self._keys):
            self._clients[i] = genai.Client(
                api_key=key,
                http_options={'api_version': 'v1beta', 'base_url': base_url, 'headers': FAKE_HEADERS, 'timeout': 120.0}
            )

    @property
    def total(self):
        return len(self._keys)

    @property
    def available(self):
        return self.total - len(self._exhausted)

    def get_client(self):
        """返回 (client, key_index, key_last4)，每次调用自动轮转到下一个 Key"""
        with self._lock:
            if len(self._exhausted) >= len(self._keys):
                return None, -1, "N/A"
            while self._index in self._exhausted:
                self._index = (self._index + 1) % len(self._keys)
            idx = self._index
            # 调用后自动轮转到下一个可用 Key，分散并发压力
            self._index = (self._index + 1) % len(self._keys)
            return self._clients[idx], idx, self._keys[idx][-4:]

    def mark_exhausted(self, key_index):
        """标记 Key 耗尽，自动切换"""
        with self._lock:
            self._exhausted.add(key_index)
            remaining = self.total - len(self._exhausted)
            key_last4 = self._keys[key_index][-4:]
            if remaining > 0:
                self._index = (key_index + 1) % len(self._keys)
                while self._index in self._exhausted:
                    self._index = (self._index + 1) % len(self._keys)
                next_last4 = self._keys[self._index][-4:]
                logging.warning(f"🔄 搜索 Key ...{key_last4} 额度耗尽，切换到 ...{next_last4}（剩余 {remaining}/{self.total}）")
            else:
                logging.error(f"❌ 所有 {self.total} 个搜索 Key 额度已用完！")


# 全局搜索 Key 轮换器
search_key_rotator = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_BASE_URL)
logging.info(f"[搜索 Key 池] 已加载 {search_key_rotator.total} 个 Key")

# Gemini 统一配置（反代，用于大模型解析/写作如 Gemini 3.1 Pro）
gemini_client = genai.Client(
    api_key=ANTIGRAVITY_API_KEY,
    http_options={'api_version': 'v1beta', 'base_url': ANTIGRAVITY_BASE_URL, 'headers': FAKE_HEADERS, 'timeout': 300.0}
)



# L1/L2.5/L3a 联网搜索引擎：gemini-2.5-flash + Google Search Grounding（官方 Key，多 Key 轮换）
GEMINI_SEARCH_MODEL = "gemini-2.5-flash"

# 并发数 = Key 池大小（每个线程对应一个 Key，最大化并行效率）
GEMINI_CONCURRENCY = search_key_rotator.total  # 5 Key → 5 线程

# L1.5 / L3.5 审计引擎：Gemini 3.1 Pro High + Google Search Grounding
GEMINI_PRO_MODEL = "gemini-3.1-pro-high"

# L2/L4 分析引擎：Claude Opus 4.6 Thinking
OPUS_MODEL = "claude-opus-4-6-thinking"

# === NativeLLMAdapter 统一适配器（替代直接 SDK 调用） ===
# 联网搜索用 search_key_rotator（官方 Key + gemini-2.5-flash）
# 以下适配器仅用于纯文本调用，内建三层防风控重试

pro_adapter = NativeLLMAdapter(model=GEMINI_PRO_MODEL, base_url=ANTIGRAVITY_BASE_URL, api_key=ANTIGRAVITY_API_KEY)
opus_adapter = NativeLLMAdapter(model=OPUS_MODEL, base_url=ANTIGRAVITY_BASE_URL, api_key=ANTIGRAVITY_API_KEY, timeout=600.0)

# 应急备用：Tavily
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "REMOVED_CREDENTIAL")

# 邮件
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "372425237@qq.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "REMOVED_CREDENTIAL")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "niliangrui1995@gmail.com,18795867096@163.com,372425237@qq.com")

# 输出目录（所有 L1/L2/L3/战报保存到这里）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "每日热点输出")
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_today_output_dir() -> str:
    """获取当天的输出子目录（如 每日热点输出/20260323/），自动创建"""
    today_str = datetime.now().strftime('%Y%m%d')
    day_dir = os.path.join(OUTPUT_DIR, today_str)
    os.makedirs(day_dir, exist_ok=True)
    return day_dir

# ----------------- 产业链 54 寡头监控字典 -----------------
# 摘自《大V源集成与系统重构方案》1.2节，覆盖五大赛道全链路
OLIGARCH_DICT = {
    "算力/代工/封装": [
        "TSMC (台积电)", "Samsung Foundry", "ARM", "AMD", "Intel",
        "Amkor (安靠)", "ASE (日月光)", "Grand Process (弘塑)", "Scientech (辛耘)"
    ],
    "HBM/存储": [
        "SK Hynix (SK海力士)", "Samsung Memory", "Micron (美光)",
        "NAMICS (纳美仕)", "JSR", "Asahi Kasei (旭化成)"
    ],
    "基建/电力/温控": [
        "Eaton (伊顿)", "Vertiv (维谛)", "Schneider (施耐德)",
        "Delta (台达电)", "Auras (奇鋐)", "AVC (双鸿)", "Sunon (建准)",
        "CoolIT", "Boyd", "Nidec (日本电产)", "AGC (旭硝子)", "3M"
    ],
    "PCB/高频基材/CCL": [
        "Ibiden (揖斐电)", "Unimicron (欣兴)", "Kingboard (建滔)",
        "Nan Ya (南亚)", "EMC (台光电)", "Doosan (斗山)",
        "Nittobo (日东纺)", "Panasonic MEGTRON", "Isola", "Shengyi (生益)"
    ],
    "光模块/硅光/连接器": [
        "Fabrinet", "Coherent (高意)", "Lumentum", "Innolight (中际旭创)",
        "Astera Labs", "Broadcom (博通)", "Marvell (迈威尔)",
        "Luxshare (立讯)", "Amphenol (安费诺)", "US Conec",
        "TE Connectivity", "Toray (东丽)", "Shin-Etsu (信越化学)"
    ],
}

# 展平为搜索用关键词
ALL_OLIGARCHS_FLAT = []
for entities in OLIGARCH_DICT.values():
    ALL_OLIGARCHS_FLAT.extend(entities)

# 十六太保 (基准 Tickers，用于财报日历感知)
SIXTEEN_VANGUARDS = {
    "NVIDIA": "NVDA", "Broadcom": "AVGO", "TSMC": "TSM", "ASML": "ASML", 
    "AMD": "AMD", "Marvell": "MRVL", "Meta": "META", "Google": "GOOGL", 
    "Microsoft": "MSFT", "Amazon": "AMZN", "SK Hynix": "000660.KS", 
    "Samsung": "005930.KS", "Micron": "MU", "Lumentum": "LITE", "Coherent": "COHR",
    "Vertiv": "VRT"
}

# 超级巨头名单（万亿级市值，波动本身就是信号，阈值更低）
# 其余十六太保自动归为中型标的（波动较大，需更高阈值才触发信号）
SUPER_GIANTS = {"NVIDIA", "Broadcom", "TSMC", "ASML", "Meta", "Google", "Microsoft", "Amazon"}


def classify_vanguard_signal(name: str, pct_change: float) -> str:
    """根据公司分类和涨跌幅，返回预标注的信号标签。

    超级巨头（NVDA/AVGO/TSM/ASML + 四大 CSP）：阈值低
    中型标的（AMD/MRVL/LITE/COHR/VRT 等）：阈值高
    """
    if name in SUPER_GIANTS:
        # 万亿级巨头：微小波动也是信号
        if pct_change >= 3:
            return "🚀 大涨催化"
        elif pct_change <= -5:
            return "💥 暴跌警惕"
        elif pct_change <= -2:
            return "📉 下跌注意"
        else:
            return "📈 正常"
    else:
        # 中型标的：波动大，需更高阈值
        if pct_change >= 5:
            return "🚀 大涨催化"
        elif pct_change <= -8:
            return "💥 暴跌警惕"
        elif pct_change <= -3:
            return "📉 下跌注意"
        else:
            return "📈 正常"



# ----------------- L1 搜索查询矩阵 -----------------
# 按方案文件模式A：70%核心极客+产业链, 30%官方验证
SEARCH_QUERIES = [
    # === 70% 核心：产业链五大赛道 ===
    ("算力核心", "NVIDIA B200 B300 Blackwell GPU supply shortage lead time 2026"),
    ("算力核心", "NVIDIA Rubin R100 Vera next generation GPU roadmap architecture NVLink 2026 2027"),
    ("算力核心", "Broadcom custom ASIC AI chip revenue customers capacity 2026"),
    ("PCB/CCL材料", "PCB CCL copper clad laminate AI server Nittobo MEGTRON high speed demand 2026"),
    ("PCB/CCL材料", "AI server high layer count PCB Ibiden Unimicron supply chain pricing 2026"),
    ("液冷全链路", "liquid cooling CDU coldplate coolant AI data center Vertiv Delta CoolIT 2026"),
    ("液冷全链路", "AGC fluorinated coolant liquid cooling supply 3M replacement 2026"),
    ("先进封装", "TSMC CoWoS advanced packaging capacity Amkor outsource 2026"),
    ("硅光/光模块", "silicon photonics 1.6T optical transceiver Innolight Coherent EML chip 2026"),
    ("硅光/光模块", "optical module 800G 1.6T order backlog supply shortage latest 2026"),
    ("HBM存储", "SK Hynix HBM4 HBM3E production NAMICS underfill supply 2026"),
    ("电力基建", "data center power transformer HVDC PDU Eaton Schneider capacity 2026"),
    # === 大V 爆料（间接搜索） ===
    ("大V爆料", "kopite7kimi dylan522p SemiAnalysis semiconductor AI chip leak latest"),
    # === 30% 验证：官方源 + 行业会议 ===
    ("官方验证", "NVIDIA developer blog GTC 2026 architecture server topology"),
    ("行业会议", "OFC DesignCon 2026 optical transceiver silicon photonics latest"),
    ("财务锚点", "NVIDIA Broadcom TSMC earnings call capex guidance latest 2026"),
    # === 十六太保对外合作/参股/巨单 ===
    ("巨头合作", "NVIDIA Meta xAI Microsoft Amazon Google AI investment partnership supply deal 2026"),
    ("巨头合作", "Broadcom ASML Vertiv Marvell Lumentum Coherent OpenAI Anthropic strategic order contract expansion 2026"),

]


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# 阶段一：动态加权雷达与 Prompt 路由 (Time-Aware Fact Crawler)
# =====================================================================

def check_calendar_and_events(backtrack: str = "24h"):
    """静默查询十六太保的财报日和突发事件，决定其适用模式 (A/B/C)"""
    # 根据回溯窗口计算检查天数
    bt_hours = int(backtrack.replace("h", ""))
    bt_days = max(bt_hours // 24, 1)  # 24h→1天, 72h→3天
    logging.info(f"[阶段一] 启动日历拦截器与突发事件哨兵（回溯 {backtrack} / {bt_days} 天）...")
    modes = {}
    today = datetime.now()

    def process_ticker(name, ticker):
        mode = "MODE_A"
        try:
            yt = yf.Ticker(ticker)
            cal = yt.calendar
            earnings_dates = []
            if isinstance(cal, dict) and 'Earnings Date' in cal:
                earnings_dates = cal['Earnings Date']
            elif hasattr(cal, 'iloc') and cal is not None:
                if 'Earnings Date' in cal.index:
                    earnings_dates = cal.loc['Earnings Date'].values
                elif 'Earnings Date' in cal.columns:
                    earnings_dates = cal['Earnings Date'].values

            for d in earnings_dates:
                if hasattr(d, 'date'):
                    d = d.date()
                if isinstance(d, date):
                    diff = (d - today.date()).days
                    if -bt_days <= diff <= 0:
                        mode = "MODE_B"
                        break
        except Exception:
            pass
        return name, mode

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(process_ticker, n, t): n for n, t in SIXTEEN_VANGUARDS.items()}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=60):
                try:
                    name, mode = fut.result(timeout=10)
                    modes[name] = mode
                    if mode != "MODE_A":
                        logging.warning(f"  [哨兵触发] 巨头 {name} 触发 {mode}！")
                except concurrent.futures.TimeoutError:
                    logging.error(f"  [阶段一] 巨头 {futures[fut]} 日历检测或哨兵超时熔断！")
                except Exception as e:
                    logging.error(f"  [阶段一] 巨头 {futures[fut]} 检测异常: {e}")
        except concurrent.futures.TimeoutError:
            logging.warning("  [阶段一] 部分巨头日历检查超时，跳过未完成的检测继续执行")

    return modes

# =====================================================================
# L1 搜索引擎：Gemini 3.1 Pro + Google Search Grounding
# =====================================================================

def build_l1_prompt(category, query, backtrack="24h", modes=None):
    if modes is None:
        modes = {}
        
    oligarch_keywords = ", ".join([
        e.split("(")[0].strip() for entities in OLIGARCH_DICT.values() for e in entities
    ])

    today_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')
    
    target_mode = "MODE_A"
    hit_name = None
    for name, mode in modes.items():
        if mode != "MODE_A" and name.lower() in query.lower():
            target_mode = mode
            hit_name = name
            break

    if target_mode == "MODE_B":
        prompt = f"""你是 L1 情报矿工。当前 {hit_name} 财报已发布或即将发布！
⚠️ 当前时间：{today_str}（只关注最近 {backtrack} 内的信息）
警告：当前处于财报及指引落地期！将 90% 算力锁死在官方通道原声，决不允许去查任何券商的二手解读研报！

搜索方向：{query}
赛道：{category}

【铁律与权重分配】：
1. [60% 绝对核心权重] 深度挖掘 SeekingAlpha 或官网关于本次业绩电话会（Earnings Call）的 Q&A Session。找出管理层对“产能瓶颈、毛利下行、发货推迟”等问题的刀刀见血的回应。
2. [30% 基础提取权重] 提取会议前半段宣布的下季度资本开支（Capex）、发货规模等确切数字。
3. [10% 交叉验证权重] 提取极客大V（如 kopite7kimi 等）对财报的暗黑验证和拆解。
4. 格式：序号. [实体] 客观事实内容 【信源|URL|日期】
5. 搜不到的如实说"未搜索到"。严禁编造。
"""
    else:
        prompt = f"""你是 L1 无情情报矿工。当前处于新闻平淡期。
⚠️ 当前时间：{today_str}（只关注最近 {backtrack} 内的信息）
警告：跳过所有表层财经新闻！启动全链条客观监控雷达！

搜索方向：{query}
赛道：{category}

【铁律与权重分配】：
1. [70% 绝对核心权重] 在极客爆料源、核心外媒中执行底层信息扫描，提取过去 48 小时内的物理参数变化。不要带寻找涨停因子的倾向，做个事实搬运工。
2. [30% 验证权重] 对照官方技术博客，将提取的参数与官方底稿做中立并列呈现。
3. 格式：序号. [实体] 客观事实内容 【信源|URL|日期】
4. 搜不到的如实说"未搜索到"。严禁编造。

寡头关键词（命中即提取）：{oligarch_keywords}"""

    return prompt


# （ratelimit 已在文件顶部导入）

@sleep_and_retry
@limits(calls=1, period=1)
def gemini_search(category, query, backtrack="24h", max_retries=3, modes=None):
    """L1 搜索：Gemini Search Grounding（主搜索）/ Tavily（备用）
    
    L1 的职责是搜索获取客观信息。
    主逻辑：使用新版 SDK (google-genai) 自带的 GoogleSearch 工具进行联网搜索并总结。
    回退逻辑：若 Gemini 报错（如 429/400），降级使用 Tavily Search API。
    """
    prompt = build_l1_prompt(category, query, backtrack, modes)
    
    # === 优先尝试 Gemini Search Grounding（支持多 Key 轮换） ===
    # 网络错误与 Key 轮换独立计数，互不干扰
    max_network_retries = max_retries  # 网络错误重试上限
    network_retries = 0

    while True:
        try:
            # 从 Key 轮换器获取当前可用的 Client
            client_to_use, key_idx, key_last4 = search_key_rotator.get_client()
            if client_to_use is None:
                logging.error(f"  [{category}] 所有搜索 Key 额度已用尽，转 Tavily")
                break

            config = genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                temperature=1.0,
                max_output_tokens=8192,
            )
            
            response = client_to_use.models.generate_content(
                model=GEMINI_SEARCH_MODEL,
                contents=prompt,
                config=config,
            )
            
            text = response.text if response.text else ""
            
            grounding_sources = []
            
            # 提取 Google Search Grounding 来源链接
            if response.candidates and hasattr(response.candidates[0], 'grounding_metadata'):
                gm = response.candidates[0].grounding_metadata
                if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
                    for chunk in gm.grounding_chunks:
                        if hasattr(chunk, 'web') and chunk.web:
                            grounding_sources.append({
                                "title": getattr(chunk.web, 'title', 'N/A'),
                                "url": getattr(chunk.web, 'uri', '')
                            })
            
            if not text.strip():
                text = "未搜索到相关信息。"
            
            return {
                "category": category,
                "query": query,
                "text": text,
                "grounding_sources": grounding_sources,
            }
            
        except Exception as e:
            err_str = str(e).lower()
            # 429 / 配额耗尽 → 标记当前 Key 并切换（不计入网络重试）
            if '429' in err_str or 'quota' in err_str or 'resource_exhausted' in err_str:
                search_key_rotator.mark_exhausted(key_idx)
                if search_key_rotator.available > 0:
                    logging.info(f"  [{category}] Key ...{key_last4} 触发 429，切换下一个 Key...")
                    continue
                else:
                    logging.error(f"  [{category}] 所有搜索 Key 额度已用尽，转 Tavily")
                    break
            elif '400' in err_str or 'invalid' in err_str:
                logging.error(f"  [{category}] Gemini请求错误: {e}")
                break
            else:
                # 网络超时等错误，独立计数重试
                network_retries += 1
                if network_retries >= max_network_retries:
                    logging.error(f"  [{category}] 网络错误重试 {max_network_retries} 次仍失败: {type(e).__name__}")
                    break
                wait = 2 + random.uniform(0.5, 1.5)
                logging.warning(f"  [{category}] Gemini 搜索异常 ({type(e).__name__})，{wait:.1f}s 后重试 ({network_retries}/{max_network_retries})")
                time.sleep(wait)
                continue

    # === 回退尝试 Tavily 搜索 ===
    logging.warning(f"  [{category}] Gemini均失败或报错，回退到Tavily搜索...")
    try:
        resp = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": 10,
            "search_depth": "advanced",
            "include_answer": True,
            "days": 3 if "72" in backtrack else 1,
        }, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            grounding_sources = []
            text_parts = []
            
            if data.get("answer"):
                text_parts.append(f"[搜索摘要] {data['answer']}")
            
            for i, item in enumerate(data.get("results", []), 1):
                title = item.get("title", "")
                url = item.get("url", "")
                content = item.get("content", "")
                published = item.get("published_date", "N/A")
                
                text_parts.append(f"{i}. [{title}] {content} 【{title}|{url}|{published}】")
                grounding_sources.append({"title": title, "url": url})
            
            text = "\n".join(text_parts)
            if not text.strip():
                text = "未搜索到相关信息。"
            
            return {
                "category": category,
                "query": query,
                "text": f"[Tavily Fallback] {text}",
                "grounding_sources": grounding_sources,
            }
        else:
            logging.error(f"  [{category}] Tavily也失败 HTTP {resp.status_code}")
    except Exception as fallback_e:
        logging.error(f"  [{category}] Tavily异常: {fallback_e}")

    # 都失败的托底返回
    return {
        "category": category,
        "query": query,
        "text": "联网搜索失败 (Gemini & Tavily 均异常)",
        "grounding_sources": [],
    }


# =====================================================================
# L1.5 / L3.5 审计层：Gemini 3.1 Pro High + Google Search Grounding
# =====================================================================

def gemini_pro_audit(raw_data: str, audit_type: str = "l1.5", backtrack: str = "24h") -> str:
    layer_name = "L1.5" if audit_type == "l1.5" else "L3.5"
    logging.info(f"[{layer_name} 审计] 启动 Gemini Pro 独立审计（{len(raw_data)} 字输入）")

    today_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')

    if audit_type == "l1.5":
        time_section = f"""⚠️ 当前北京时间：{today_str}
⚠️ 扫描时间窗口：{backtrack}
⚠️ 下方 Flash 搜索结果中的所有时间均为北京时间（UTC+8）。

━━━━━━━━━━━━━━━━━━━━━━━━

【任务一：时效性验证（纯文本推理逻辑验证）】

阅读每条事实自带的日期戳，通过严密的纯文本数学与逻辑推理，验证其是否在设定时间窗内：
- 发生时间距离现在 {today_str} 在 {backtrack} 内（24h 或 72h）→ 保留
- 超出时间窗的旧闻（如数月、数年前的资料）→ 回绝并直接删除
- 时间模糊但事件特征表明是近期的 → 保留并标注"时间待确认"

━━━━━━━━━━━━━━━━━━━━━━━━"""
        header_text = "你收到的数据来自 Gemini Flash 联网搜索的原始结果，可能包含幻觉、过期信息和低质量噪声。"
    else:
        time_section = """ℹ️ 本次审计无时效性限制（补充搜索为深度调研，不受 24h/72h 窗口约束）。

━━━━━━━━━━━━━━━━━━━━━━━━"""
        header_text = "你收到的数据是 Opus 指挥官下达的补充搜索指令，由 Gemini Flash 执行搜索后返回的结果。可能包含幻觉、低质量杂音和主观内容。"

    prompt = f"""你是 VCP Hunter {layer_name} 层——独立纯文本情报审计员。

{header_text}
你的工作是基于已有数据进行闭卷交叉核对其信源与时标一致性，清洗后只保留高质量情报传给下游 Opus。（注意：本次审计仅做逻辑推理验证，不要主动搜索新信息，只基于下方提供的数据进行判断！）

{time_section}

【{"任务二" if audit_type == "l1.5" else "任务一"}：严格可信度打分与溯源隔离】

对每条事实基于【极其苛刻的】信源等级进行交叉核验并打分 1-10（纯断网推理判断，不联网）：

🟢 10/10分：必须是抓取到了纯正的官方源原生直接表述（如 nvidia.com、sec.gov、上交所公告原文）。若含有修饰性或推断性关联，绝不可给10分。
🟢 8/10分：彭博、路透等拥有严谨采编的一线机构首发客观数据。
🟡 5-7分：券商研报、行业公众号等带主观推演与卖方意图的资讯拼凑。
🔴 1-4分：股吧、雪球、自媒体社群爆料、小作文（直接删除）。

【强制打假与降级触发器】
1. 词条剥离：当新闻同时出现“官方文件”和“某A股公司名称”时，你必须严格隔离：官方原文件到底写了什么？国内媒体/券商为了吹票又主观关联了什么？必须在条目最后明确标注出：（如：“【券商/媒体主观推演，非官方点名】”）
2. 小作文特征词降级：只要正文中包含【唯一】、【最强受益】、【或将引爆】、【远低估】、【全网首发】等典型的“A股特色炒作词”，必须立即扣除可信度 3 分，并在该条目末尾强制挂上 `[⚠️疑似卖方吹票/小作文特征]` 警示标签！

━━━━━━━━━━━━━━━━━━━━━━━━

【{"任务三" if audit_type == "l1.5" else "任务二"}：数据清洗（去杂音、去主观）】

🚫 强制删除以下污染源：
- 主观判断词："前景广阔""表现良好""值得关注""看好""利好""投资机会"
- 分析师强因果倒推与评级（"买入""必定受益""唯一替代替代"）
- 模糊表述："预计将""有望""可能会"（除非有具体产量/产能/财报数字支撑）

⚠️ 豁免规则（以下数据即使看起来像"行情噪声"也必须完整保留）：
- 十六太保（NVIDIA/NVDA, Broadcom/AVGO, TSMC/TSM, ASML, AMD, Marvell/MRVL,
  Meta, Google/GOOGL, Microsoft/MSFT, Amazon/AMZN, Lumentum/LITE, Coherent/COHR, Vertiv/VRT 等核心标的）的最新股价、
  涨跌幅、成交量变化 → 这是客观市场信号，直接保留，可信度标 8/10 以上。

✅ 只保留：谁、什么时间、做了什么、具体数值是多少
格式：序号. [实体] 客观事实(数值) 【信源|日期】 可信度:X/10

━━━━━━━━━━━━━━━━━━━━━━━━

【输出格式】

## 审计摘要
通过: X条 / 删除: Y条

## 🟢 高可信情报
(列出)

## 🟡 中可信情报
(列出)

## 🔴 删除清单（不传给 Opus，仅记录）
(列出删除原因)

【铁律】：
- 你是审计员，不是分析师。只判断事实的真伪和时效，不预设涨跌倾向。
- 绝不编造验证结果。搜不到就标"无法验证"。
- 宁可误删一条真实信息，也不放过一条虚假信息。

━━━━━━━━━━━━━━━━━━━━━━━━
以下是需要审计的原始数据：
━━━━━━━━━━━━━━━━━━━━━━━━

{raw_data}"""

    # 使用 NativeLLMAdapter（内建三层防风控重试）
    t0 = time.time()
    resp = pro_adapter.invoke(prompt)
    elapsed = time.time() - t0
    text = resp.content if resp.content else ""

    if text:
        logging.info(f"[{layer_name} 审计] 完成 ({elapsed:.1f}s, {len(text)} 字输出)")
    else:
        logging.warning(f"[{layer_name} 审计] Pro 返回空结果")
    return text

# （已移除冗余的 tavily_fallback 函数——gemini_search 内部已自带 Tavily 回退逻辑，无需重复调用）


# =====================================================================
# L1 情报采集调度
# =====================================================================



def get_run_config(now: datetime):
    if os.environ.get("FORCE_72H"):
        logging.info("【强制触发】环境变量强制回溯 72h 全扫")
        return "72h"
    if os.environ.get("FORCE_24H"):
        logging.info("【强制触发】环境变量强制回溯 24h 全扫")
        return "24h"

    wd = now.weekday()
    bt = "72h" if wd == 0 else "24h"
    logging.info(f"【早盘全扫】{'周一' if wd == 0 else '平日'}，回溯 {bt}")
    return bt


def collect_intelligence(backtrack: str):
    queries = SEARCH_QUERIES
    logging.info("=" * 60)
    logging.info(f"[L1 情报采集] 早盘全量扫描")
    logging.info(f"[搜索引擎] Gemini Search Grounding (Tavily 备用) | [查询] {len(queries)}条")
    logging.info("=" * 60)
    
    # 【风控修改】阶段一：挂载日历感知与动态权重路由（传入回溯窗口）
    modes = check_calendar_and_events(backtrack)

    all_results = []

    def search_one(idx_cat_query):
        idx, (category, query) = idx_cat_query
        
        # 【风控修改】起跑前加入随机延迟，抹平瞬间并发洪峰，防 CC 拦截
        time.sleep(random.uniform(1.0, 6.0))  # 【风控优化】加大发射间隔，从 0.5~4s → 1~6s
        
        logging.info(f"[{idx+1}/{len(queries)}] [{category}] {query}")
        t0 = time.time()
        result = gemini_search(category, query, backtrack, modes=modes)
        elapsed = time.time() - t0
        is_success = result and "搜索失败" not in result.get("text", "") and "联网搜索失败" not in result.get("text", "")
        if is_success:
            logging.info(f"  [{idx+1}] 成功 ({elapsed:.1f}s, {len(result['text'])}字)")
        else:
            logging.warning(f"  [{idx+1}] 失败 ({elapsed:.1f}s)")
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=GEMINI_CONCURRENCY) as pool:
        for result in pool.map(search_one, enumerate(queries)):
            if result:
                all_results.append(result)

    rate = len(all_results) / len(queries) if queries else 0

    logging.info(f"\n[L1 采集完毕] 成功: {len(all_results)}/{len(queries)} ({rate*100:.0f}%)")

    try:
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        l1_fname = os.path.join(get_today_output_dir(), f"L1_原始数据_{now_str}.json")
        with open(l1_fname, "w", encoding="utf-8") as f:
            json.dump({"time": datetime.now().isoformat(), "model": GEMINI_SEARCH_MODEL,
                       "results": all_results}, f, ensure_ascii=False, indent=2)
        logging.info(f"[L1] 原始数据已保存: {l1_fname}")
    except Exception as e:
        logging.error(f"保存失败: {e}")

    return all_results


# =====================================================================
# L2 分析引擎：Opus 深度推理 A 股映射
# =====================================================================

def format_evidence(results):
    lines = ["=" * 60,
             "以下全部数据来自 Gemini Search Grounding / Tavily 实时联网搜索",
             "每条信息中的信源名称、URL、时间均为搜索引擎实时返回的真实数据",
             "=" * 60]
    for r in results:
        lines.append(f"\n### [{r['category']}] {r['query']}")
        lines.append(r["text"])
        if r.get("grounding_sources"):
            lines.append("\nGrounding 搜索来源:")
            for s in r["grounding_sources"][:5]:
                lines.append(f"  - {s['title']}: {s['url']}")
        lines.append("")
    return "\n".join(lines)

def fetch_vanguard_prices() -> str:
    logging.info("[yfinance] 正在获取十六太保最新客观股价行情...")
    lines = []
    lines.append("==================== 客观行情数据 (yfinance) ====================")
    lines.append("十六太保最新真实股价与信号标注（信号已按公司类型自动分级，直接引用即可）：")
    lines.append("分类规则：超级巨头(万亿级)≥3%触发催化/≤-5%触发警惕 | 中型标的≥5%触发催化/≤-8%触发警惕\n")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(yf.Ticker, ticker): name for name, ticker in SIXTEEN_VANGUARDS.items()}
        for fut in concurrent.futures.as_completed(futures, timeout=30):
            name = futures[fut]
            tier = "超级巨头" if name in SUPER_GIANTS else "中型标的"
            try:
                t = fut.result()
                hist = t.history(period="5d")  # 获取最近几天数据以确保能取到前收盘价
                if len(hist) >= 2:
                    prev_close = hist['Close'].iloc[-2]
                    curr_close = hist['Close'].iloc[-1]
                    pct_change = (curr_close - prev_close) / prev_close * 100
                    signal = classify_vanguard_signal(name, pct_change)
                    lines.append(f"- [{signal}] {name}({SIXTEEN_VANGUARDS[name]}) [{tier}]: ${curr_close:.2f}, {pct_change:+.2f}%")
                elif len(hist) == 1:
                    curr_close = hist['Close'].iloc[-1]
                    lines.append(f"- [📈 正常] {name}({SIXTEEN_VANGUARDS[name]}) [{tier}]: ${curr_close:.2f} (无足够历史对比)")
                else:
                    lines.append(f"- {name}({SIXTEEN_VANGUARDS[name]}) [{tier}]: 暂无最新交易数据")
            except Exception:
                lines.append(f"- {name}({SIXTEEN_VANGUARDS[name]}) [{tier}]: 获取股价失败")
                
    lines.append("================================================================\n")
    return "\n".join(lines)


def generate_report(evidence_text: str):
    logging.info(f"[L2 映射] 引擎: {OPUS_MODEL}")

    # 读取近 5 天深研历史，注入 L2 prompt 实现去重
    recent_history = _get_recent_deep_topics(days=5)
    dedup_section = ""
    if recent_history:
        dedup_section = f"""\n注意：以下主题近 5 天已深度研究过，除非有重大新进展（如新数据、新事件），否则请跳过：\n{recent_history}\n"""

    oligarch_display = "\n".join([
        f"  [{k}]: {', '.join(v)}" for k, v in OLIGARCH_DICT.items()
    ])

    prompt = f"""你是 VCP Hunter L2 层—— A 股映射引擎 + 情报指挥官。

【任务】：基于 L1.5 审计后的联网搜索数据，将海外产业链物理事件映射到 A 股标的。
你只需要做映射，不需要写战报。

【寡头字典】：
{oligarch_display}

==================== L1 联网搜索数据 ====================
{evidence_text}
==================== 搜索数据结束 ====================

【映射规则】：
1. 将 L1 数据按赛道分组（算力核心/先进封装/PCB材料/液冷/光模块/HBM/电力/巨头合作等）
2. 每个赛道至少映射 3 家 A 股公司（越多越好，宁多勿少）
3. 每家公司必须给出：公司名称、6位代码、所属赛道、映射理由（一句话）
4. 按弹性分三级：🔥高弹性 / ⚡中弹性 / 💀纯概念

【十六太保信号推理】（重点！）：
行情数据中的每只票已由系统按公司类型自动标注信号等级，你无需重新计算涨跌幅阈值，直接引用信号标签即可：
- [🚀 大涨催化] → 强催化剂，重点深挖对应产业链，在映射中标注"受 XXX 大涨 +Y% 催化"
- [📈 正常] → 常规映射
- [📉 下跌注意] → 在映射中标注"⚠️ XXX 下跌 -Y%，注意风险"
- [💥 暴跌警惕] → 在映射中标注"🚨 XXX 暴跌 -Y%，高度警惕"

【输出格式】：严格按以下格式输出，每行一个标的：

赛道: [赛道名]
- 公司名（代码） | 弹性级别 | 映射理由

示例：
赛道: 算力核心
- 寒武纪（688256） | 🔥高弹性 | 国产AI芯片龙头，受益算力需求爆发
- 海光信息（688041） | ⚡中弹性 | 服务器CPU受益数据中心扩张
- 景嘉微（300474） | 💀纯概念 | GPU概念但收入体量极小

请确保每个有实质内容的赛道都映射至少 3 家 A 股公司。总字数不限。

【指挥官权限——可选补充搜索】：
你拥有调度 Gemini 联网搜索的权限。如果你在分析 L1 数据时，发现某些关键信息不足、需要进一步验证，或需要补充特定公司/赛道的最新动态，你可以在映射结果末尾输出补充搜索指令。

格式（每行一条，放在映射结果的最后面）：
SEARCH: [搜索关键词]

【高质量调用说明书 (Examples)】：
❌ 错误示范图鉴（绝对禁止）：
- SEARCH: NVDA 最新消息 (太宽泛，会搜出一堆无用废话)
- SEARCH: 台湾发生地震 (缺乏主语和商业目标)
- SEARCH: 昨天美联储降息了多少 (不要问常识问题)

✅ 顶级指挥官示范（务必模仿）：
【情景】：L1数据中提到“B200产能受限”，但不清晰。
- SEARCH: NVIDIA B200 Blackwell yield issues TSMC CoWoS capacity 2026
【情景】：发现某小众票大涨，需要查验证据。
- SEARCH: Fabrinet 800G optical transceiver earnings guidance order 2026

规则：
- 这是可选的！如果 L1 数据已经足够充分，不需要补充搜索，就不要输出任何 SEARCH 行
- 每条搜索用英文或中文关键词均可
- 最多 5 条补充搜索
- 搜索结果将在 L3 阶段反馈给你

【深度研究指令——必填】：
你必须从 L1 情报中选出 1-2 个最值得深度研究的核心产业链事件，系统将对这些事件做多轮联网搜索挖掘因果链。

选择标准（按优先级）：
1. 首次出现的重大变化（非旧闻重复）
2. 有明确量化数据变化（订单量、产能、价格涨跌）
3. 涉及多个产业链环节（上下游联动）
4. 当前市场主线（AI/HBM/液冷/光模块/先进封装）
{dedup_section}
格式（每行一条，放在最后面，在 SEARCH 行之后）：
DEEP_RESEARCH: [用于多轮搜索的英文关键词短语]

示例：
DEEP_RESEARCH: Nittobo T-Glass fiber shortage AI server PCB impact alternative suppliers
DEEP_RESEARCH: TSMC CoWoS capacity expansion 2026 outsource Amkor ASE allocation"""

    # 使用 NativeLLMAdapter（内建三层防风控重试 + thinking 模型自动处理）
    resp = opus_adapter.invoke(prompt)
    report = resp.content if resp.content else ""
    if report:
        logging.info(f"[L2 映射] 输出 {len(report)} 字")
    else:
        logging.error(f"[L2 映射] 失败")
    return report


# =====================================================================
# L2.5：深度研究层 — 多轮迭代搜索（对 L2 高价值事件做因果链深挖）
# =====================================================================

def _get_recent_deep_topics(days: int = 5) -> str:
    """扫描最近 N 天的 L2.5 深度研究文件，提取已研究过的主题。"""
    from glob import glob
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    # 搜索所有日期子目录和根目录下的 L2.5 文件
    pattern = os.path.join(OUTPUT_DIR, "**", "L2.5_深度研究_*.md")
    recent_topics = []
    for fpath in sorted(glob(pattern, recursive=True), reverse=True):
        try:
            fname = os.path.basename(fpath)
            # 文件名格式: L2.5_深度研究_202603191008.md
            date_part = fname.replace("L2.5_深度研究_", "").replace(".md", "")
            file_date = datetime.strptime(date_part, "%Y%m%d%H%M")
            if file_date < cutoff:
                break
            # 读取文件，提取 "## 深度研究：xxx" 标题行
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("## 深度研究："):
                        topic = line.replace("## 深度研究：", "").strip()
                        if topic:
                            recent_topics.append(f"- {topic} ({file_date.strftime('%m/%d')})")
        except Exception:
            continue

    if recent_topics:
        logging.info(f"[L2.5 去重] 最近 {days} 天已研究 {len(recent_topics)} 个主题")
    return "\n".join(recent_topics)


def pick_deep_topics(l2_mapping: str) -> list:
    """从 L2 Opus 映射结果中解析 DEEP_RESEARCH 指令（Opus 在 L2 映射时已顺带选好）。"""
    topics = re.findall(r'DEEP_RESEARCH:\s*(.+)', l2_mapping)
    topics = [t.strip() for t in topics if len(t.strip()) > 10][:2]
    if topics:
        logging.info(f"[L2.5 选题] Opus 指定 {len(topics)} 个深研主题:")
        for i, t in enumerate(topics):
            logging.info(f"  [{i+1}] {t}")
    else:
        logging.info("[L2.5 选题] Opus 未指定深研主题，跳过")
    return topics


def iterative_deep_search(topic: str, rounds: int = 3) -> str:
    """对单个主题执行多轮 Flash 联网搜索，每轮根据上轮结果自动细化搜索方向。"""
    logging.info(f"[L2.5 深研] 启动 '{topic}' 多轮迭代搜索 ({rounds} 轮)...")

    all_findings: list[str] = []

    for r in range(rounds):
        if r == 0:
            # 第 1 轮：广度扫描
            query = topic
        else:
            # 第 2-3 轮：基于上轮结果自动生成新搜索方向
            refine_prompt = f"""基于以下已搜集到的信息，生成 1 条更深入的搜索查询。
目标：挖掘上一轮结果中未解答的关键问题，例如：
- 事件的根本原因（why）
- 产业链上下游的具体影响（who wins/loses）
- 替代方案或竞争格局变化

已搜集信息摘要：
{all_findings[-1][:2000]}

只输出 1 行搜索关键词（英文），不要任何解释："""

            try:
                resp = pro_adapter.invoke(refine_prompt)  # 用 3.1 Pro 确定搜索关键词
                query = resp.content.strip() if resp.content else topic
                # 清理可能的多行输出，只取第一行
                query = query.split('\n')[0].strip().strip('"\'')
            except Exception as e:
                logging.warning(f"[L2.5 深研] R{r+1} 细化失败，复用原主题: {e}")
                query = topic

        logging.info(f"  [R{r+1}/{rounds}] {query}")

        # 风控：轮间随机延迟
        time.sleep(random.uniform(2.0, 5.0))

        result = gemini_search(f"深研R{r+1}", query, backtrack="72h")
        if result and result.get("text"):
            all_findings.append(result["text"])
            logging.info(f"  [R{r+1}] 获取 {len(result['text'])} 字")
        else:
            logging.warning(f"  [R{r+1}] 搜索失败")

    if not all_findings:
        return ""

    combined = "\n\n".join([f"### 深度研究 第{i+1}轮\n{f}" for i, f in enumerate(all_findings)])
    logging.info(f"[L2.5 深研] '{topic}' 完成: {len(combined)} 字 ({len(all_findings)}/{rounds} 轮成功)")
    return combined


def run_deep_research(l2_mapping: str) -> str:
    """L2.5 深度研究入口：选题 → 多轮搜索 → 汇总返回。"""
    topics = pick_deep_topics(l2_mapping)
    if not topics:
        return ""

    all_research = []
    for topic in topics:
        result = iterative_deep_search(topic, rounds=3)
        if result:
            all_research.append(f"## 深度研究：{topic}\n\n{result}")

    if all_research:
        combined = "\n\n" + "\n\n".join(all_research)
        logging.info(f"[L2.5] 深度研究完成: {len(topics)} 主题, {len(combined)} 字")

        # 保存 L2.5 结果
        try:
            now_str = datetime.now().strftime('%Y%m%d%H%M')
            fname = os.path.join(get_today_output_dir(), f"L2.5_深度研究_{now_str}.md")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(f"# L2.5 深度研究结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                f.write(combined)
            logging.info(f"[L2.5] 已保存: {fname}")
        except Exception as e:
            logging.warning(f"[L2.5] 保存失败: {e}")

        return combined
    return ""


# =====================================================================
# L3：Gemini 搜索 A 股标的近期股价表现
# =====================================================================

def extract_stocks_from_report(report: str):
    patterns = [
        r'([\u4e00-\u9fa5]{2,6})[（(]\s*(\d{6})\s*[）)]',
        r'([\u4e00-\u9fa5]{2,6})\s+(\d{6})',
        r'\|\s*([\u4e00-\u9fa5]{2,6})\s*\|\s*(\d{6})\s*\|',
    ]

    stocks = {}
    for pattern in patterns:
        for match in re.finditer(pattern, report):
            name, code = match.group(1), match.group(2)
            if code not in stocks:
                stocks[code] = name

    result = [(name, code) for code, name in stocks.items()]
    logging.info(f"[L3 准备] 从战报中提取到 {len(result)} 只 A 股标的: {', '.join([f'{n}({c})' for n, c in result])}")
    return result


def l3_search_stock_prices(stocks: list):
    if not stocks:
        logging.warning("[L3] 未提取到 A 股标的，跳过股价分析")
        return ""

    try:
        import sys
        vcp_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if vcp_root not in sys.path:
            sys.path.insert(0, vcp_root)
        from vcp.utils import _load_tdx_local_config
    except ImportError:
        logging.warning("[L3] 无法导入 vcp.utils，尝试直接定位通达信数据")
        _load_tdx_local_config = None

    vipdoc = None
    for candidate in ["D:\\HT\\vipdoc", "D:\\vcp_qt\\vipdoc", "C:\\new_tdx\\vipdoc"]:
        if os.path.isdir(candidate):
            vipdoc = candidate
            break

    if not vipdoc:
        try:
            vipdoc = _load_tdx_local_config()
        except Exception:
            pass

    if not vipdoc:
        logging.error("[L3] 未找到通达信 vipdoc 目录")
        return ""

    logging.info(f"[L3 股价分析] {len(stocks)} 只标的 × 本地通达信数据（{vipdoc}）")

    import struct


    finance_data = {}
    try:
        from vcp.engine import VCPEngine
        codes_list = [code for _, code in stocks]
        finance_data = VCPEngine.batch_get_finance_info(codes_list)
        logging.info(f"[L3] 通达信财务数据获取成功: {len(finance_data)} 只")
    except Exception as e:
        logging.warning(f"[L3] 财务数据获取失败（不影响K线分析）: {e}")

    def read_day_file_simple(filepath):
        if not os.path.isfile(filepath):
            return []
        try:
            with open(filepath, 'rb') as f:
                buf = f.read()
            n = len(buf) // 32
            if n == 0:
                return []
            records = []
            for i in range(n):
                chunk = buf[i*32:(i+1)*32]
                date_int, o, h, low, c, _amount_f, vol, _ = struct.unpack('<IIIIIfII', chunk)
                date_str = str(date_int)
                if len(date_str) == 8:
                    records.append((date_str, o/100.0, h/100.0, low/100.0, c/100.0, vol))
            return records
        except Exception:
            return []

    price_summaries = []

    for name, code in stocks:
        if code.startswith(('6', '9')):
            market = 'sh'
        else:
            market = 'sz'
        day_file = os.path.join(vipdoc, market, 'lday', f'{market}{code}.day')

        records = read_day_file_simple(day_file)

        if not records or len(records) < 20:
            price_summaries.append(f"### {name}（{code}）\n未找到足够的本地K线数据\n")
            logging.warning(f"  [L3] {name}({code}) 数据不足（{len(records)}条）")
            continue

        recent = records[-250:]
        latest = recent[-1]
        latest_close = latest[4]
        latest_date = latest[0]

        if len(recent) >= 20:
            close_20d_ago = recent[-20][4]
            chg_1m = (latest_close / close_20d_ago - 1) * 100
        else:
            chg_1m = 0

        if len(recent) >= 60:
            close_60d_ago = recent[-60][4]
            chg_3m = (latest_close / close_60d_ago - 1) * 100
        else:
            chg_3m = 0

        highs = [r[2] for r in recent]
        lows = [r[3] for r in recent]
        high_250d = max(highs)
        low_250d = min(lows)
        position_pct = (latest_close - low_250d) / (high_250d - low_250d) * 100 if high_250d > low_250d else 50

        vol_5d = sum(r[5] for r in recent[-5:]) / 5 if len(recent) >= 5 else 0
        vol_20d = sum(r[5] for r in recent[-20:]) / 20 if len(recent) >= 20 else 0
        vol_60d = sum(r[5] for r in recent[-60:]) / 60 if len(recent) >= 60 else vol_20d
        vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1

        closes = [r[4] for r in recent]
        opens = [r[1] for r in recent]
        highs_list = [r[2] for r in recent]
        lows_list = [r[3] for r in recent]
        vols = [r[5] for r in recent]
        n = len(recent)

        if n >= 20:
            ma20 = sum(closes[-20:]) / 20
            std20 = (sum((c - ma20)**2 for c in closes[-20:]) / 20) ** 0.5
            boll_width = (2 * std20 * 2) / ma20 * 100 if ma20 > 0 else 0
        else:
            boll_width = 0

        mas = {}
        for period in [5, 10, 20, 60]:
            if n >= period:
                mas[period] = sum(closes[-period:]) / period
        if len(mas) >= 3:
            ma_vals = list(mas.values())
            ma_spread = (max(ma_vals) - min(ma_vals)) / latest_close * 100
        else:
            ma_spread = 0

        if n >= 60:
            high_60d = max(highs_list[-60:])
            drawdown_60d = (1 - latest_close / high_60d) * 100 if high_60d > 0 else 0
        else:
            drawdown_60d = 0

        if n >= 5:
            amp_5d = sum((highs_list[-i] - lows_list[-i]) / closes[-i] for i in range(1, 6)) / 5 * 100
        else:
            amp_5d = 0

        if n >= 35:
            ema12, ema26 = closes[0], closes[0]
            dif_list = []
            for c in closes:
                ema12 = ema12 * 11/13 + c * 2/13
                ema26 = ema26 * 25/27 + c * 2/27
                dif_list.append(ema12 - ema26)
            dea = dif_list[0]
            for d in dif_list:
                dea = dea * 8/10 + d * 2/10
            macd_bar = (dif_list[-1] - dea) * 2
            macd_status = "由负转正" if len(dif_list) >= 2 and dif_list[-1] > dea and dif_list[-2] <= dea else ("金叉上方" if dif_list[-1] > dea else "死叉下方")
        else:
            macd_bar = 0
            macd_status = "数据不足"

        liutong = finance_data.get(code, {}).get('liutongguben', 0)
        if n >= 10 and liutong and liutong > 0:
            turnover_10d = sum(vols[-10:]) / 10 / liutong * 100
        else:
            turnover_10d = 0

        if n >= 20:
            yang_count = sum(1 for i in range(-20, 0) if closes[i] > opens[i])
            yang_ratio = yang_count / 20 * 100
        else:
            yang_ratio = 50

        vol_shrink = vol_5d / vol_60d if vol_60d > 0 else 1

        vol_desc = "明显放量" if vol_ratio > 1.5 else ("温和缩量" if vol_ratio < 0.7 else "正常")
        pos_desc = "历史高位区间" if position_pct > 80 else ("中位区间" if position_pct > 40 else "低位区间")

        fin_lines = ""
        fin = finance_data.get(code)
        if fin:
            zongguben = fin.get('zongguben', 0)
            liutongguben = fin.get('liutongguben', 0)
            if zongguben and zongguben > 0:
                cap_yi = zongguben * latest_close / 1e8
                fin_lines += f"- 总市值：{cap_yi:.0f} 亿元（总股本 {zongguben/1e8:.2f} 亿股，流通 {liutongguben/1e8:.2f} 亿股）\n"
            zongzichan = fin.get('zongzichan', 0)
            jingzichan = fin.get('jingzichan', 0)
            zhuyingshouru = fin.get('zhuyingshouru', 0)
            jinglirun = fin.get('jinglirun', 0)
            yingyelirun = fin.get('yingyelirun', 0)
            jingyingxianjinliu = fin.get('jingyingxianjinliu', 0)
            meigujingzichan = fin.get('meigujingzichan', 0)
            gudongrenshu = fin.get('gudongrenshu', 0)
            if zongzichan:
                fin_lines += f"- 总资产：{zongzichan/1e8:.0f} 亿 | 净资产：{jingzichan/1e8:.0f} 亿\n"
            if zhuyingshouru:
                fin_lines += f"- 主营收入：{zhuyingshouru/1e8:.0f} 亿 | 净利润：{jinglirun/1e8:.0f} 亿 | 营业利润：{yingyelirun/1e8:.0f} 亿\n"
            if jingyingxianjinliu:
                fin_lines += f"- 经营现金流：{jingyingxianjinliu/1e8:.0f} 亿\n"
            if meigujingzichan:
                fin_lines += f"- 每股净资产：{meigujingzichan:.2f} 元\n"
            if gudongrenshu:
                fin_lines += f"- 股东人数：{gudongrenshu/1e4:.1f} 万\n"

        summary = (
            f"### {name}（{code}）\n"
            f"- 最新收盘价：{latest_close:.2f} 元（{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:]}）\n"
            f"{fin_lines}"
            f"- 近 1 个月涨跌幅：{chg_1m:+.1f}%\n"
            f"- 近 3 个月涨跌幅：{chg_3m:+.1f}%\n"
            f"- 250 日区间：{low_250d:.2f} ~ {high_250d:.2f}，当前位于 {pos_desc}（{position_pct:.0f}%分位）\n"
            f"- 近期量能：{vol_desc}（5日/20日量比 {vol_ratio:.2f}，5日/60日缩量度 {vol_shrink:.2f}）\n"
            f"- 布林带宽度(20日)：{boll_width:.1f}%{'（极窄→蓄力中）' if boll_width < 10 else ''}\n"
            f"- 均线密集度(5/10/20/60日)：{ma_spread:.1f}%{'（粘合→方向选择临界）' if ma_spread < 3 else ''}\n"
            f"- 距60日高回撤：{drawdown_60d:.1f}%\n"
            f"- 近5日振幅均值：{amp_5d:.1f}%{'（波动压缩）' if amp_5d < 3 else ''}\n"
            f"- MACD柱：{macd_bar:.3f}（{macd_status}）\n"
            f"- 10日换手率均值：{turnover_10d:.2f}%\n"
            f"- 20日阳线占比：{yang_ratio:.0f}%\n"
        )
        price_summaries.append(summary)
        logging.info(f"  [L3] {name}({code}) 最新{latest_close:.2f}元, 3月{chg_3m:+.1f}%, {pos_desc}")

    combined = "\n".join(price_summaries)
    logging.info(f"[L3 完成] 股价数据 {len(combined)} 字")

    try:
        now_str = datetime.now().strftime('%Y%m%d%H%M')
        l3_fname = os.path.join(get_today_output_dir(), f"L3_股价数据_{now_str}.md")
        with open(l3_fname, "w", encoding="utf-8") as f:
            f.write(f"# L3 股价分析数据（本地通达信 250 日 K 线）\n\n> 读取时间: {datetime.now().isoformat()}\n\n")
            f.write(combined)
        logging.info(f"[L3] 原始数据已保存: {l3_fname}")
    except Exception as e:
        logging.error(f"[L3] 保存失败: {e}")

    return combined


def l4_risk_analysis(evidence_text: str, l2_mapping: str, price_data: str):
    if not price_data or len(price_data) < 50:
        logging.warning("[L4] 股价数据不足，跳过")
        return ""

    logging.info("[L4 战报] Opus 综合分析（五层数据）")

    today_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')

    prompt = f"""你是 VCP Hunter L4 层——最终战报生成器，深谙 A 股投机规律。

⚠️ 当前时间：{today_str}。
过期旧闻（如 2025 年或更早）标注为【过期信息】或直接忽略。

你将收到五层数据：
1. L1.5 审计后情报（已经过 Pro 可信度打分+时效验证+主观清洗）
2. L2.5 深度研究（对核心产业链事件的多轮迭代搜索，包含因果链分析。如无则跳过）
3. L2 映射
4. L3.5 审计后补充搜索结果（已经过 Pro 可信度打分+主观清洗）
5. L3 数据：通达信 250 日 K 线 + 8项技术指标 + 财务数据

==================== L1.5 审计后产业链情报 ====================
{evidence_text}
==================== L2 A 股映射 ====================
{l2_mapping}
==================== L3 真实股价 + 财务数据（通达信 250 日）====================
{price_data}
==================== 数据结束 ====================

【战报结构】：

## 一、核心信号（≤5 条）
从 Gemini 联网搜索信息中提炼最值得关注的产业链信号。
标注【信源|日期】和置信度（🟢高/🟡中/🔴低）。

## 二、标的狙击表（重点！每个细分行业最多 3 只）
| 标的 | 代码 | 总市值 | 最新价 | 近 3 月 | 250 日分位 | 量能 | 弹性 | 短期爆发力 | 风控 |

短期爆发力判断（核心关注！）：
- ⚡⚡⚡ 极强：市值轻+低位+缩量蓄势+催化剂明确
- ⚡⚡ 较强：有催化但已涨一波
- ⚡ 一般：大盘股或已高位

风控评级：基于 250 日 K 线 + 技术指标自行判断 🟢/🟡/🔴

## 三、资金路径推演
海外事件 → 产业传导 → A 股标的排列

## 四、今日操作建议
3-5 只最值得关注的标的 + 理由

基于 L3 真实数据，不要编造。字数不限，写多少算多少。

【铁律】：尽量简洁！每条结论一句话说清楚，不要废话套话，不要重复已知信息。表格优于文字。"""

    # 使用 NativeLLMAdapter（内建三层防风控重试 + thinking 模型自动处理）
    resp = opus_adapter.invoke(prompt)
    result = resp.content if resp.content else ""
    if result:
        logging.info(f"[L4 战报] 完成 ({len(result)} 字)")
    else:
        logging.error(f"[L4 战报] 失败")
    return result


# =====================================================================
# 邮件推送
# =====================================================================

def send_report_email(content: str):
    now = datetime.now()
    fname = os.path.join(get_today_output_dir(), f"战报_{now.strftime('%Y%m%d%H%M')}.md")
    with open(fname, "w", encoding='utf-8') as f:
        f.write(content)
    logging.info(f"战报已保存: {fname}")

    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, RECEIVER_EMAIL]):
        logging.warning("邮件未配置")
        return

    try:
        import markdown
        html = markdown.markdown(content, extensions=['tables', 'fenced_code'])
        html_full = f"""<html><head><style>
            table{{border-collapse:collapse;width:100%;font-size:14px}}
            th,td{{border:1px solid #e0e0e0;padding:12px 15px;text-align:left}}
            th{{background:#f8f9fa;font-weight:bold;border-bottom:2px solid #dee2e6}}
            h1,h2,h3{{color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px}}
            body{{font-family:'Segoe UI',sans-serif;line-height:1.6;color:#333;padding:20px}}
            blockquote{{border-left:5px solid #3498db;padding:10px 20px;background:#f8f9fa}}
            a{{color:#3498db}}
        </style></head><body>{html}</body></html>"""
        msg = MIMEText(html_full, 'html', 'utf-8')
    except ImportError:
        msg = MIMEText(f"<pre>{content}</pre>", 'html', 'utf-8')

    msg['From'] = formataddr(("VCP Hunter", EMAIL_ADDRESS))
    msg['To'] = RECEIVER_EMAIL
    msg['Subject'] = Header(f"【Gemini联网战报】{now.strftime('%H:%M')}", 'utf-8')  # type: ignore

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465) as s:
            s.set_debuglevel(1)
            s.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            s.sendmail(EMAIL_ADDRESS, RECEIVER_EMAIL.split(','), msg.as_string())
        logging.info(f"邮件已推送至 {RECEIVER_EMAIL}")
    except Exception as e:
        logging.error(f"邮件失败: {e}")


# =====================================================================
# 断点自适应：自动检测当天已完成的层级，从断点续跑
# =====================================================================

def detect_checkpoint() -> dict:
    """扫描当天输出目录，检测已完成的最远层级。
    返回 dict: stage ("none"/"l1"/"l1.5"/"l2"/"l4_done"), files (各阶段最新文件)
    """
    from glob import glob
    today_dir = get_today_output_dir()
    ckpt: dict = {"stage": "none", "files": {}}

    war_files = sorted(glob(os.path.join(today_dir, "战报_*.md")), reverse=True)
    if war_files:
        ckpt["stage"] = "l4_done"
        ckpt["files"]["war_report"] = war_files[0]
        return ckpt

    l2_files = sorted(glob(os.path.join(today_dir, "L2_映射_*.md")), reverse=True)
    if l2_files:
        ckpt["stage"] = "l2"
        ckpt["files"]["l2"] = l2_files[0]

    l15_files = sorted(glob(os.path.join(today_dir, "L1.5_审计_*.md")), reverse=True)
    if l15_files:
        ckpt["files"]["l1.5"] = l15_files[0]
        if ckpt["stage"] == "none":
            ckpt["stage"] = "l1.5"

    l1_files = sorted(glob(os.path.join(today_dir, "L1_原始数据_*.json")), reverse=True)
    if l1_files:
        ckpt["files"]["l1"] = l1_files[0]
        if ckpt["stage"] == "none":
            ckpt["stage"] = "l1"

    l25_files = sorted(glob(os.path.join(today_dir, "L2.5_深度研究_*.md")), reverse=True)
    if l25_files:
        ckpt["files"]["l2.5"] = l25_files[0]
    l35_files = sorted(glob(os.path.join(today_dir, "L3.5_审计_*.md")), reverse=True)
    if l35_files:
        ckpt["files"]["l3.5"] = l35_files[0]

    return ckpt


# =====================================================================
# 主流程
# =====================================================================

def orchestrator_main():
    import time as _time
    total_start = _time.time()
    now = datetime.now()
    evidence = ""  # 防御性初始化
    l2_mapping = ""  # 断点续跑时可能已有 L2 数据
    logging.info("=" * 60)
    logging.info(f"VCP Hunter v3.1 | {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info(f"L1 搜索: {GEMINI_SEARCH_MODEL} (Google Search Grounding)")
    logging.info(f"L1.5/L3.5 审计: {GEMINI_PRO_MODEL} (Pro + Google Search)")
    logging.info(f"L2/L4 分析: {OPUS_MODEL}")
    logging.info("=" * 60)

    if not is_trading_day(now.date()):
        logging.info("非交易日，休眠。")
        return

    import sys
    force_restart = "--force" in sys.argv
    skip_to_l2 = "--skip-to-l2" in sys.argv
    search_cache = "--search-cache" in sys.argv

    # ==================== 断点自适应检测 ====================
    resume_from = "none"
    if not skip_to_l2 and not search_cache and not force_restart:
        ckpt = detect_checkpoint()
        resume_from = ckpt["stage"]

        if resume_from == "l4_done":
            logging.info(f"✅ [断点检测] 今天的战报已生成: {os.path.basename(ckpt['files']['war_report'])}")
            logging.info("全流程已完成，无需重复执行。如需强制重跑请加 --force 参数。")
            return

        if resume_from == "l2":
            logging.info(f"🔄 [断点续跑] 检测到 L2 映射已完成: {os.path.basename(ckpt['files']['l2'])}")
            logging.info("跳过 L1→L1.5→L2，直接从 L2.5/L3 继续...")
            with open(ckpt['files']['l2'], "r", encoding="utf-8") as f:
                l2_mapping = f.read()
            if "l1.5" in ckpt["files"]:
                with open(ckpt["files"]["l1.5"], "r", encoding="utf-8") as f:
                    evidence = f.read()

        elif resume_from == "l1.5":
            logging.info(f"🔄 [断点续跑] 检测到 L1.5 审计已完成: {os.path.basename(ckpt['files']['l1.5'])}")
            logging.info("跳过 L1→L1.5，直接从 L2 Opus 映射继续...")
            with open(ckpt["files"]["l1.5"], "r", encoding="utf-8") as f:
                evidence = f.read()
            logging.info("♻️ [风控冷却] 暂停 5 秒，准备调度 Opus 模型...")
            _time.sleep(5)

        elif resume_from == "l1":
            logging.info(f"🔄 [断点续跑] 检测到 L1 原始数据: {os.path.basename(ckpt['files']['l1'])}")
            logging.info("跳过 L1 搜索，从 L1.5 审计继续...")
            try:
                with open(ckpt['files']['l1'], "r", encoding="utf-8") as f:
                    l1_data = json.load(f)
                l1_results = l1_data.get("results", [])
                if l1_results:
                    backtrack = l1_data.get("backtrack", get_run_config(now))
                    evidence_raw = format_evidence(l1_results)
                    logging.info(f"[L1→L1.5] 本地 L1 数据 {len(evidence_raw)} 字，提交 Pro 审计")
                    audited_evidence = gemini_pro_audit(evidence_raw, audit_type="l1.5", backtrack=backtrack)
                    try:
                        l15_fname = os.path.join(get_today_output_dir(), f"L1.5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.md")
                        with open(l15_fname, "w", encoding="utf-8") as wf:
                            wf.write(f"# L1.5 Pro 审计结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                            wf.write(audited_evidence if audited_evidence else "(审计失败，使用原始数据)")
                        logging.info(f"[L1.5] 审计结果已保存: {l15_fname}")
                    except Exception as e:
                        logging.warning(f"[L1.5] 保存失败: {e}")
                    evidence = audited_evidence if audited_evidence else evidence_raw
                    if not audited_evidence:
                        logging.warning("[L1.5] 审计失败，降级使用 Flash 原始数据")
                    logging.info("♻️ [风控冷却] 暂停 15 秒，准备调度 Opus 模型...")
                    _time.sleep(15)
                else:
                    logging.warning("[断点续跑] L1 文件中无搜索结果，回退到全流程")
                    resume_from = "none"
            except Exception as e:
                logging.warning(f"[断点续跑] 读取 L1 文件失败: {e}，回退到全流程")
                resume_from = "none"

        else:
            logging.info("📡 [断点检测] 今天尚无任何中间文件，从 L1 开始全流程执行")

    if force_restart:
        logging.info("🔄 [强制重跑] --force 参数指定，忽略断点检测，从 L1 开始")
        resume_from = "none"

    # ==================== 手动模式分支 ====================
    if skip_to_l2:
        logging.info("【测试/接力模式】跳过 L1/L1.5，直接从最新的 L1.5 文件读取数据进入 L2...")
        try:
            from glob import glob
            pattern = os.path.join(OUTPUT_DIR, "**", "L1.5_审计_*.md")
            l15_files = sorted(glob(pattern, recursive=True), reverse=True)
            if not l15_files:
                logging.error("找不到 L1.5 文件，无法跳过！")
                return
            latest_l15 = l15_files[0]
            with open(latest_l15, "r", encoding="utf-8") as f:
                evidence = f.read()
            logging.info(f"成功读取本地 L1.5 文件: {os.path.basename(latest_l15)}")
            logging.info("♻️ [风控冷却] 暂停 5 秒，准备调度核心大文本 Opus 模型...")
            _time.sleep(5)
        except Exception as e:
            logging.error(f"读取本地 L1.5 失败: {e}")
            return
    elif search_cache:
        # 从 GitHub 云端搜索结果 JSON 文件中读取 L1 数据（临时方案）
        cache_idx = sys.argv.index("--search-cache") + 1
        if cache_idx >= len(sys.argv):
            logging.error("请指定搜索缓存文件路径: --search-cache <L1_results.json>")
            return
        cache_file = sys.argv[cache_idx]
        logging.info(f"【GitHub 搜索缓存模式】从文件读取 L1 数据: {cache_file}")
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            results = cache_data.get("results", [])
            if not results:
                logging.error(f"缓存文件中无搜索结果！")
                return
            logging.info(f"成功读取 {len(results)} 条 L1 搜索结果 (来源: {cache_data.get('model', 'unknown')})")
        except Exception as e:
            logging.error(f"读取搜索缓存失败: {e}")
            return

        backtrack = cache_data.get("backtrack", "24h")

        # L1.5 审计（走本地反代，不需要联网）
        evidence_raw = format_evidence(results)
        logging.info(f"[L1→L1.5] 缓存数据 {len(evidence_raw)} 字，提交 Pro 审计")
        audited_evidence = gemini_pro_audit(evidence_raw, audit_type="l1.5", backtrack=backtrack)

        try:
            l15_fname = os.path.join(get_today_output_dir(), f"L1.5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.md")
            with open(l15_fname, "w", encoding="utf-8") as f:
                f.write(f"# L1.5 Pro 审计结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                f.write(audited_evidence if audited_evidence else "(审计失败，使用原始数据)")
            logging.info(f"[L1.5] 审计结果已保存: {l15_fname}")
        except Exception as e:
            logging.warning(f"[L1.5] 保存失败: {e}")

        evidence = audited_evidence if audited_evidence else evidence_raw
        if not audited_evidence:
            logging.warning("[L1.5] 审计失败，降级使用 Flash 原始数据")

        logging.info("♻️ [风控冷却] 暂停 15 秒，准备调度 Opus 模型...")
        _time.sleep(15)
    elif resume_from in ("none",):
        # 全新执行：L1 搜索 + L1.5 审计
        backtrack = get_run_config(now)
        results = collect_intelligence(backtrack)

        if not results:
            logging.error("L1 采集全部失败，放弃。")
            return

        logging.info("♻️ [风控冷却] 暂停 10 秒，让反代账号池缓冲...")
        _time.sleep(10)

        # ==================== L1.5：Pro 审计清洗 ====================
        evidence_raw = format_evidence(results)
        logging.info(f"[L1→L1.5] Flash 原始情报 {len(evidence_raw)} 字，提交 Pro 审计")

        audited_evidence = gemini_pro_audit(evidence_raw, audit_type="l1.5", backtrack=backtrack)

        try:
            l15_fname = os.path.join(get_today_output_dir(), f"L1.5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.md")
            with open(l15_fname, "w", encoding="utf-8") as f:
                f.write(f"# L1.5 Pro 审计结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                f.write(audited_evidence if audited_evidence else "(审计失败，使用原始数据)")
            logging.info(f"[L1.5] 审计结果已保存: {l15_fname}")
        except Exception as e:
            logging.warning(f"[L1.5] 保存失败: {e}")

        evidence = audited_evidence if audited_evidence else evidence_raw
        if not audited_evidence:
            logging.warning("[L1.5] 审计失败，降级使用 Flash 原始数据")

        logging.info("♻️ [风控冷却] 暂停 15 秒，降低 TPM 密度，准备调度核心大文本 Opus 模型...")
        _time.sleep(15)

    # ==================== L2：Opus A 股映射（断点续跑时跳过） ====================
    if not l2_mapping:
        try:
            yfinance_data = fetch_vanguard_prices()
            evidence = yfinance_data + "\n" + evidence
        except Exception as e:
            logging.error(f"[yfinance] 获取客观行情失败: {e}")

        l2_mapping = generate_report(evidence)

        if not l2_mapping or "失败" in l2_mapping[:20]:
            logging.error("L2 映射异常，跳过。")
            return

        try:
            l2_fname = os.path.join(get_today_output_dir(), f"L2_映射_{datetime.now().strftime('%Y%m%d%H%M')}.md")
            with open(l2_fname, "w", encoding="utf-8") as f:
                f.write(f"# L2 Opus A股映射\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                f.write(l2_mapping)
            logging.info(f"[L2] 映射结果已保存: {l2_fname}")
        except Exception as e:
            logging.warning(f"[L2] 保存失败: {e}")
    else:
        logging.info(f"[L2] 使用断点续跑的 L2 映射数据 ({len(l2_mapping)} 字)")

    # ==================== L2.5 + L3a：深度研究 & 补充搜索（并行执行） ====================
    search_queries = re.findall(r'^\s*SEARCH:\s*(.+)$', l2_mapping, re.MULTILINE)
    supplementary_data = ""
    deep_research_data = ""

    # 用线程池并行执行 L2.5 深度研究 和 L3a 补充搜索
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as parallel_pool:
        # 线程 1：L2.5 深度研究（可选）
        dr_future = parallel_pool.submit(run_deep_research, l2_mapping)
        logging.info("[并行] L2.5 深度研究已启动")

        # 线程 2：L3a 补充搜索（原有逻辑，封装为 lambda）
        def _run_l3a_search():
            if not search_queries:
                logging.info("[L2→L3] Opus 未下达补充搜索指令，跳过")
                return ""
            logging.info(f"[L2→L3] Opus 下达 {len(search_queries)} 条补充搜索指令")
            sup_results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=GEMINI_CONCURRENCY) as pool:
                futures = {}
                for i, q in enumerate(search_queries[:5]):
                    _time.sleep(random.uniform(0.5, 2.0))
                    logging.info(f"  [补充搜索 {i+1}] {q}")
                    
                    # 【风控拦截层】剥离 A 股小作文发源地
                    modified_q = q
                    if any(kw in modified_q.upper() for kw in ["白皮书", "WHITEPAPER", "官方", "公告", "财报", "纪要"]):
                        modified_q += " -site:xueqiu.com -site:guba.eastmoney.com -site:10jqka.com.cn -site:toutiao.com -site:sohu.com"
                        
                    futures[pool.submit(gemini_search, f"Opus补充搜索{i+1}", modified_q, "24h")] = q
                for future in concurrent.futures.as_completed(futures):
                    q = futures[future]
                    try:
                        result = future.result()
                        if result and result.get("text"):
                            sup_results.append(f"### [Opus 补充搜索] {q}\n{result['text']}\n")
                    except Exception as e:
                        logging.warning(f"  补充搜索失败: {q} - {e}")
            if sup_results:
                combined = "\n".join(sup_results)
                logging.info(f"[L3a 补充] Gemini 返回 {len(sup_results)} 条补充数据（{len(combined)} 字）")
                return combined
            return ""

        l3a_future = parallel_pool.submit(_run_l3a_search)
        logging.info("[并行] L3a 补充搜索已启动")

        # 等待两个任务完成
        try:
            deep_research_data = dr_future.result(timeout=600)  # L2.5 超时 10 分钟
            if deep_research_data:
                logging.info(f"[并行] L2.5 完成: {len(deep_research_data)} 字")
        except concurrent.futures.TimeoutError:
            logging.warning("[L2.5] 深度研究超时（10min），跳过")
            dr_future.cancel()
        except Exception as e:
            logging.warning(f"[L2.5] 深度研究异常: {e}")

        try:
            supplementary_data = l3a_future.result(timeout=300)
        except Exception as e:
            logging.warning(f"[L3a] 补充搜索异常: {e}")

    # ==================== L3.5：Pro 审计补充搜索结果 ====================
    audited_supplementary = ""
    if supplementary_data:
        logging.info(f"[L3→L3.5] 补充搜索 {len(supplementary_data)} 字，提交 Pro 审计")
        
        # 【风控修改】再次冷却
        logging.info("♻️ [风控冷却] 暂停 5 秒，准备审计补充数据...")
        _time.sleep(5)
        
        audited_supplementary = gemini_pro_audit(supplementary_data, audit_type="l3.5")

        # 保存 L3.5 审计结果
        try:
            l35_fname = os.path.join(get_today_output_dir(), f"L3.5_审计_{datetime.now().strftime('%Y%m%d%H%M')}.md")
            with open(l35_fname, "w", encoding="utf-8") as f:
                f.write(f"# L3.5 Pro 补充搜索审计结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n")
                f.write(audited_supplementary if audited_supplementary else "(审计失败，使用原始数据)")
            logging.info(f"[L3.5] 审计结果已保存: {l35_fname}")
        except Exception as e:
            logging.warning(f"[L3.5] 保存失败: {e}")

        # 审计失败时降级使用原始数据
        if not audited_supplementary:
            logging.warning("[L3.5] 审计失败，降级使用 Flash 原始补充数据")
            audited_supplementary = supplementary_data

    # ==================== L3b：本地通达信全量数据 ====================
    stocks = extract_stocks_from_report(l2_mapping)
    price_data = ""
    final_report = ""

    if stocks:
        price_data = l3_search_stock_prices(stocks)
        # ==================== L4：Opus 综合战报 ====================
        if price_data:
            # 【风控修改】最终写战报前冷却
            logging.info("♻️ [风控冷却] 暂停 10 秒，准备生成终极战报...")
            _time.sleep(10)
            
            # 拼装五层数据给 L4（新增 L2.5）
            full_evidence = evidence  # L1.5 审计后情报
            if deep_research_data:
                full_evidence += "\n\n==================== L2.5 深度研究结果 ====================\n" + deep_research_data
            if audited_supplementary:
                full_evidence += "\n\n==================== L3.5 审计后补充搜索结果 ====================\n" + audited_supplementary
            final_report = l4_risk_analysis(full_evidence, l2_mapping, price_data)

    if not final_report:
        final_report = l2_mapping

    send_report_email(final_report)
    total_elapsed = _time.time() - total_start
    logging.info(f"执行完毕。总耗时: {int(total_elapsed//60)}分{int(total_elapsed%60)}秒 ({total_elapsed:.1f}s)")


if __name__ == "__main__":


    # 第三方库日志静音：正常不打印，只在报错时才输出
    for lib in ['httpx', 'httpcore', 'google', 'google.genai', 'google.auth', 'urllib3']:
        logging.getLogger(lib).setLevel(logging.WARNING)

    # 错误日志写入文件：仅 ERROR 及以上自动保存
    error_log_path = os.path.join(OUTPUT_DIR, "error.log")
    file_handler = logging.FileHandler(error_log_path, encoding="utf-8", mode="a")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(file_handler)

    # 命令行参数解析
    # ---------------------------------------------------------
    # 强制以单次运行模式执行（已全面迁移至 Windows 原生任务计划程序进行防双开调度）
    # 彻底废除原有的 while True 阻塞轮询逻辑，实现“事毕即关”。
    # ---------------------------------------------------------
    orchestrator_main()
