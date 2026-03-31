"""
VCP Hunter — GitHub 云端搜索模块 (临时方案)
支持多 Key 轮换：429 时自动切换到下一个 Key
"""

import os
import json
import time
import random
import logging
import threading
import concurrent.futures
from datetime import datetime

from google import genai
from google.genai import types

# =====================================================================
# 多 Key 池配置（每个 Key 对应不同项目，额度独立）
# 环境变量 GEMINI_API_KEYS 用逗号分隔多个 Key
# 也可以用单个 GEMINI_API_KEY 环境变量（向下兼容）
# =====================================================================

_keys_env = os.environ.get("GEMINI_API_KEYS", "")
_single_key = os.environ.get("GEMINI_API_KEY", "")

# 5 个不同项目的 Key，每个 500 次/天，总计 2500 次/天
DEFAULT_KEY_POOL = [
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
    "REMOVED_CREDENTIAL",
]

# 优先使用环境变量，否则用默认 Key 池
if _keys_env:
    API_KEY_POOL = [k.strip() for k in _keys_env.split(",") if k.strip()]
elif _single_key:
    API_KEY_POOL = [_single_key]
else:
    API_KEY_POOL = DEFAULT_KEY_POOL


class KeyRotator:
    """线程安全的 API Key 轮换器"""

    def __init__(self, keys: list[str]):
        self._keys = keys
        self._index = 0
        self._exhausted: set[int] = set()  # 已耗尽额度的 Key 索引
        self._lock = threading.Lock()
        self._clients: dict[int, genai.Client] = {}
        # 预创建所有 Client
        for i, key in enumerate(self._keys):
            self._clients[i] = genai.Client(api_key=key)

    @property
    def total(self) -> int:
        return len(self._keys)

    @property
    def available(self) -> int:
        return self.total - len(self._exhausted)

    def get_client(self) -> tuple[genai.Client, int, str]:
        """获取当前可用的 Client，每次调用自动轮转到下一个 Key"""
        with self._lock:
            if len(self._exhausted) >= len(self._keys):
                return None, -1, "N/A"
            # 找到下一个未耗尽的 Key
            while self._index in self._exhausted:
                self._index = (self._index + 1) % len(self._keys)
            idx = self._index
            key_last4 = self._keys[idx][-4:]
            # 调用后自动轮转到下一个可用 Key，分散并发压力
            self._index = (self._index + 1) % len(self._keys)
            return self._clients[idx], idx, key_last4

    def mark_exhausted(self, key_index: int):
        """标记某个 Key 为已耗尽，自动切换到下一个"""
        with self._lock:
            self._exhausted.add(key_index)
            remaining = self.total - len(self._exhausted)
            key_last4 = self._keys[key_index][-4:]
            if remaining > 0:
                # 切换到下一个可用的 Key
                self._index = (key_index + 1) % len(self._keys)
                while self._index in self._exhausted:
                    self._index = (self._index + 1) % len(self._keys)
                next_last4 = self._keys[self._index][-4:]
                logging.warning(
                    f"🔄 Key ...{key_last4} 额度耗尽，切换到 Key ...{next_last4}"
                    f"（剩余 {remaining}/{self.total} 个 Key）"
                )
            else:
                logging.error(f"❌ Key ...{key_last4} 耗尽，所有 {self.total} 个 Key 额度已用完！")


# 全局 Key 轮换器
key_rotator = KeyRotator(API_KEY_POOL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info(f"[Key 池] 已加载 {key_rotator.total} 个 API Key")

# 输出目录
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =====================================================================
# 搜索查询矩阵
# =====================================================================

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
    # === 大V 爆料 ===
    ("大V爆料", "kopite7kimi dylan522p SemiAnalysis semiconductor AI chip leak latest"),
    # === 30% 验证 ===
    ("官方验证", "NVIDIA developer blog GTC 2026 architecture server topology"),
    ("行业会议", "OFC DesignCon 2026 optical transceiver silicon photonics latest"),
    ("财务锚点", "NVIDIA Broadcom TSMC earnings call capex guidance latest 2026"),
    # === 巨头合作 ===
    ("巨头合作", "NVIDIA Meta xAI Microsoft Amazon Google AI investment partnership supply deal 2026"),
    ("巨头合作", "Broadcom ASML Vertiv Marvell Lumentum Coherent OpenAI Anthropic strategic order contract expansion 2026"),
]

# =====================================================================
# 产业链寡头字典
# =====================================================================

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

OLIGARCH_KEYWORDS = ", ".join([
    e.split("(")[0].strip()
    for entities in OLIGARCH_DICT.values()
    for e in entities
])

# L1 搜索 Prompt（系统指令，与 daily_report.py 用法一致）
SYSTEM_INSTRUCTION = f"""
你是 L1 无情情报矿工。
警告：跳过所有表层财经新闻！启动全链条客观监控雷达！

【铁律】：
1. 在极客爆料源、核心外媒中执行底层信息扫描，提取过去 48 小时内的物理参数变化。
2. 对照官方技术博客，将提取的参数与官方底稿做中立并列呈现。
3. 格式：序号. [实体] 客观事实内容 【信源|URL|日期】
4. 搜不到的如实说"未搜索到"。严禁编造。

寡头关键词（命中即提取）：{OLIGARCH_KEYWORDS}
"""


def search_one(category: str, query: str) -> dict:
    """单条搜索——支持多 Key 轮换，429 时自动切换"""
    today_str = datetime.now().strftime('%Y年%m月%d日')
    prompt = f"当前时间：{today_str}。搜索方向：{query}。赛道：{category}。请按系统指令格式输出。"

    max_network_retries = 3  # 网络错误重试上限（与 Key 轮换独立）
    network_retries = 0

    while True:
        try:
            current_client, key_idx, key_last4 = key_rotator.get_client()
            if current_client is None:
                logging.error(f"  [{category}] 所有 Key 额度用尽！")
                break

            response = current_client.models.generate_content(
                model="gemini-2.5-flash",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                ),
                contents=prompt
            )

            text = response.text if response.text else "未搜索到相关信息。"

            # 提取 grounding 来源
            grounding_sources = []
            if response.candidates and hasattr(response.candidates[0], 'grounding_metadata'):
                gm = response.candidates[0].grounding_metadata
                if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
                    for chunk in gm.grounding_chunks:
                        if hasattr(chunk, 'web') and chunk.web:
                            grounding_sources.append({
                                "title": getattr(chunk.web, 'title', 'N/A'),
                                "url": getattr(chunk.web, 'uri', '')
                            })

            return {
                "category": category,
                "query": query,
                "text": text,
                "grounding_sources": grounding_sources,
                "key_used": f"...{key_last4}",
            }

        except Exception as e:
            error_str = str(e)
            # 429 / RESOURCE_EXHAUSTED → 标记当前 Key 耗尽，换下一个（不计入网络重试）
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                key_rotator.mark_exhausted(key_idx)
                if key_rotator.available > 0:
                    logging.info(f"  [{category}] Key ...{key_last4} 触发 429，切换下一个 Key...")
                    continue
                else:
                    logging.error(f"  [{category}] 所有 Key 额度用尽！")
                    break
            else:
                # 网络超时等错误，独立计数重试
                network_retries += 1
                if network_retries >= max_network_retries:
                    logging.error(f"  [{category}] 网络错误重试 {max_network_retries} 次仍失败: {type(e).__name__}")
                    break
                wait = 2 + random.uniform(0.5, 1.5)
                logging.warning(f"  [{category}] 搜索异常 ({type(e).__name__})，{wait:.1f}s 后重试 ({network_retries}/{max_network_retries})...")
                time.sleep(wait)
                continue

    return {
        "category": category,
        "query": query,
        "text": "搜索失败: 所有 Key 额度已用尽或发生异常",
        "grounding_sources": [],
        "key_used": "N/A",
    }




def main():
    start_time = time.time()
    now_str = datetime.now().strftime('%Y%m%d%H%M')

    logging.info("=" * 60)
    logging.info("[L1 情报采集] GitHub 云端搜索（直连 Google API）")
    logging.info(f"[模型] gemini-2.5-flash | [查询] {len(SEARCH_QUERIES)} 条 | [并发] {key_rotator.total}")
    logging.info(f"[Key 池] {key_rotator.total} 个 Key | 每个 Key 500 次/天 | 总额度 {key_rotator.total * 500} 次")
    logging.info("=" * 60)

    all_results = []

    def _search(idx_cat_query):
        idx, (category, query) = idx_cat_query
        time.sleep(random.uniform(8.0, 15.0))
        logging.info(f"[{idx+1}/{len(SEARCH_QUERIES)}] [{category}] 搜索中...")
        t0 = time.time()
        result = search_one(category, query)
        elapsed = time.time() - t0
        is_success = "搜索失败" not in result["text"] and "联网搜索失败" not in result["text"]
        status = "成功" if is_success else "失败"
        logging.info(f"  [{idx+1}] {status}! ({elapsed:.1f}s, {len(result['text'])}字)")
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=key_rotator.total) as pool:
        for result in pool.map(_search, enumerate(SEARCH_QUERIES)):
            all_results.append(result)

    # 保存结果
    l1_file = os.path.join(OUTPUT_DIR, f"L1_results_{now_str}.json")
    with open(l1_file, "w", encoding="utf-8") as f:
        json.dump({
            "time": datetime.now().isoformat(),
            "model": "gemini-2.5-flash",
            "backtrack": "24h",
            "results": all_results,
        }, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - start_time
    success = sum(1 for r in all_results if "搜索失败" not in r["text"] and "联网搜索失败" not in r["text"])
    logging.info(f"\n[完成] {success}/{len(SEARCH_QUERIES)} 成功")
    logging.info(f"耗时: {int(elapsed//60)}分{int(elapsed%60)}秒")
    logging.info(f"文件: {l1_file}")


if __name__ == "__main__":
    for lib in ['httpx', 'httpcore', 'google', 'google.genai', 'google.auth', 'urllib3']:
        logging.getLogger(lib).setLevel(logging.WARNING)
    main()
