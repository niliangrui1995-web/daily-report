"""
config.py — 全局配置中心
所有数据源、API Key、赛道映射、轮询间隔等集中管理。
"""
import os

# ==============================================================================
# 数据库
# ==============================================================================
# SQLite 数据库路径（与本文件同目录）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.db")

# ==============================================================================
# Gemini API（复用主系统的 Key 池与 Cloudflare 反代跳板）
# ==============================================================================
# Gemini API 端点（优先用环境变量，否则直连 Google 官方）
# 如果反代可用，设置环境变量 GEMINI_SEARCH_BASE_URL 即可切换
GEMINI_BASE_URL = os.environ.get(
    "GEMINI_SEARCH_BASE_URL",
    "https://generativelanguage.googleapis.com"
)

_keys_env = os.environ.get("GEMINI_SEARCH_API_KEYS", "")
_key_single = os.environ.get("GEMINI_SEARCH_API_KEY", "")

DEFAULT_KEY_POOL = [
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
]

if _keys_env:
    GEMINI_API_KEY_POOL: list[str] = [k.strip() for k in _keys_env.split(",") if k.strip()]
elif _key_single:
    GEMINI_API_KEY_POOL = [_key_single]
else:
    GEMINI_API_KEY_POOL = DEFAULT_KEY_POOL

# 洗稿使用的模型 — Flash 极速极便宜
GEMINI_MODEL = "gemini-2.5-flash"

# ==============================================================================
# 反爬伪装
# ==============================================================================
FAKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
FAKE_HEADERS = {"User-Agent": FAKE_UA}

# ==============================================================================
# 数据源轮询间隔 (秒)
# ==============================================================================
INTERVAL_CLS = 60          # 财联社 — 1 分钟
INTERVAL_TWITTER = 60      # X (Twitter / Nitter) — 1 分钟
INTERVAL_BLOOMBERG = 180   # Bloomberg / Reuters RSS — 3 分钟
INTERVAL_DIGITIMES = 300   # DigiTimes / TrendForce — 5 分钟
INTERVAL_DEEP_RSS = 600    # SemiAnalysis / Seeking Alpha — 10 分钟
INTERVAL_REDDIT = 600      # Reddit — 10 分钟
INTERVAL_SEC = 1800         # SEC EDGAR 8-K — 30 分钟
INTERVAL_AI_BATCH = 30     # AI 洗稿批处理间隔 — 30 秒

# ==============================================================================
# Nitter / Twitter RSS 数据源
# ==============================================================================
# 核心 AI 产业链大V列表 — 每个账号对应一个 Nitter RSS Feed
# 格式：(显示名, Twitter 用户名)
TWITTER_WATCHLIST: list[tuple[str, str]] = [
    ("kopite7kimi", "kopite7kimi"),       # GPU 参数泄露之王
    ("Dylan Patel", "dylan522p"),          # SemiAnalysis 创始人
    ("Semianalysis", "SemiAnalysis"),      # SemiAnalysis 官方
    ("Jukanlosransen", "Jukanlosransen"),  # 芯片产业链爆料
    ("chiloyal", "chiloyal"),             # 半导体行业观察
    ("Kepler_L2", "Kepler_L2"),           # GPU 架构分析
    ("harukaze5719", "harukaze5719"),      # 日本硬件泄露
    ("VideoCardz", "VideoCardz"),          # GPU 新闻
    ("Moore's Law Is Dead", "MooresLawDead"),  # 科技 YouTuber
    ("Patrick Moorhead", "PatrickMoorhead"),  # Moor Insights
]

# 可用的 Nitter 实例列表（公共或自建）
NITTER_INSTANCES: list[str] = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
]

# ==============================================================================
# RSS 数据源 — 标题+摘要白嫖
# ==============================================================================
# 格式：(数据源名称, RSS URL, 优先级标签)
RSS_FEEDS: list[tuple[str, str, str]] = [
    ("Bloomberg_Tech", "https://feeds.bloomberg.com/technology/news.rss", "bloomberg"),
    ("CNBC_Tech", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910", "reuters"), # 替代失效的 Reuters
    ("TechCrunch", "https://techcrunch.com/feed/", "techcrunch"), # 替代失效的 DigiTimes
    ("TrendForce", "https://www.trendforce.com/rss/RSS_all.xml", "trendforce"),
    ("SemiAnalysis", "https://www.semianalysis.com/feed", "semianalysis"),
    ("SeekingAlpha_Tech", "https://seekingalpha.com/sector/technology.xml", "seekingalpha"),
]

# ==============================================================================
# Reddit 数据源
# ==============================================================================
REDDIT_SUBS: list[str] = ["hardware", "nvidia", "amd"]

# ==============================================================================
# 16 大赛道标签体系（对齐 AI_Chain.xlsx）
# ==============================================================================
TRACK_LABELS: list[str] = [
    "芯片设计与底层IP",
    "定制化ASIC与代工",
    "晶圆制造与材料设备",
    "HBM与核心存储矩阵",
    "CXL与数据存储",
    "先进封装与混合键合",
    "核心基板与封装材料",
    "网络交换与高速电互连",
    "光模块、硅光与CPO",
    "高频PCB与覆铜板材料",
    "AI服务器基建与温控",
    "能源基建与核电(SMR)",
    "测试、老化与探针卡",
    "算力颠覆与主权生态",
    "云巨头、算力租赁与数据",
    "边缘AI与具身智能",
]

# 赛道 → 关键实体映射（用于 AI Prompt）
TRACK_ENTITY_MAP: dict[str, list[str]] = {
    "芯片设计与底层IP": ["Synopsys", "Cadence", "ARM", "Arteris", "Qualcomm/Alphawave", "proteanTecs"],
    "定制化ASIC与代工": ["TSMC", "Intel Foundry Services", "Broadcom", "Marvell", "Alchip"],
    "晶圆制造与材料设备": ["ASML", "Applied Materials", "Lam Research", "TEL", "Soitec", "IQE", "PVA TePla"],
    "HBM与核心存储矩阵": ["SK Hynix", "Samsung", "Micron", "Hanmi", "NAMICS"],
    "CXL与数据存储": ["Western Digital", "Phison", "Montage"],
    "先进封装与混合键合": ["Besi", "EV Group", "ASMPT", "TSMC", "ASE", "Amkor"],
    "核心基板与封装材料": ["Ibiden", "Unimicron", "Ajinomoto", "Intel Foundry", "Absolics", "Corning"],
    "网络交换与高速电互连": ["Broadcom", "Nvidia", "Marvell", "Astera Labs", "Credo", "Amphenol", "TE Connectivity"],
    "光模块、硅光与CPO": ["Intel", "中际旭创", "高意", "新易盛", "Ayar Labs", "Lightmatter", "Fabrinet"],
    "高频PCB与覆铜板材料": ["Rogers", "Panasonic", "EMC", "Nittobo", "Wus", "Compeq"],
    "AI服务器基建与温控": ["nVent", "Vertiv", "Eaton", "Schneider", "Delta", "MPS", "CoolIT", "Auras", "AVC"],
    "能源基建与核电(SMR)": ["Constellation Energy", "Vistra", "Talen Energy", "Oklo", "TerraPower"],
    "测试、老化与探针卡": ["Advantest", "Teradyne", "KYEC", "FormFactor", "MJC", "Technoprobe", "MPI"],
    "算力颠覆与主权生态": ["Groq", "Cerebras", "Tenstorrent", "SMIC", "Huawei", "NAURA", "AMEC"],
    "云巨头、算力租赁与数据": ["Microsoft", "Meta", "Google", "AWS", "OpenAI", "Anthropic", "CoreWeave", "Scale AI", "Reddit"],
    "边缘AI与具身智能": ["Apple", "Qualcomm", "MediaTek", "Figure AI", "Tesla"],
}
