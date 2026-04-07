"""
搜索引擎 — SearchKeyRotator + ChannelCircuitBreaker + gemini_search。

从 daily_report.py 拆分，包含：
- 线程安全的 API Key 轮换器
- 会话级通道熔断器（直连 Google → CF Worker → Tavily 三级降级）
- gemini_search() 联网搜索函数

【⚠️核心维护规则 / Agent 指令】
1. 搜索轮询器是极其精密的并发状态机，修改 SearchKeyRotator 轮询或 ChannelCircuitBreaker 熔断时，必须绝对遵守 `threading.Lock()` 加锁规矩，防止多线程死锁。
2. 严禁改动 `gemini_search` 身上的 `@sleep_and_retry` 和 `@limits(calls=1, period=1)` 装饰器限流频率，这是保证免费 API 不被拔管的最后物理护城河。
3. 获取网页时必须留有优雅降级通道，严禁去掉“直连Google → CF Worker镜像 → Tavily”这种三梯队递减的防弹衣。
"""

import time
import logging
import threading
import requests
from ratelimit import limits, sleep_and_retry

from settings import SEARCH_API_KEY_POOL, GEMINI_SEARCH_DIRECT_URL, GEMINI_SEARCH_CF_URL, GEMINI_SEARCH_MODEL, TAVILY_API_KEY
from scraper_utils import enrich_search_urls

class SearchKeyRotator:
    """线程安全的搜索 API Key 轮换器"""
    def __init__(self, keys: list[str], base_url: str):
        self._keys = keys
        self._index = 0
        self._exhausted = set()
        self._lock = threading.Lock()
        self._base_url = base_url

    @property
    def total(self) -> int:
        return len(self._keys)

    @property
    def available(self) -> int:
        return self.total - len(self._exhausted)

    def get_client(self):
        with self._lock:
            if len(self._exhausted) >= len(self._keys):
                return None, None, -1, 'N/A'
            while self._index in self._exhausted:
                self._index = (self._index + 1) % len(self._keys)

            idx = self._index
            self._index = (self._index + 1) % len(self._keys)
            key = self._keys[idx]
            return self._base_url, key, idx, key[-4:]

    def mark_exhausted(self, key_index: int):
        with self._lock:
            self._exhausted.add(key_index)
            remaining = self.total - len(self._exhausted)
            key_last4 = self._keys[key_index][-4:]
            if remaining > 0:
                self._index = (key_index + 1) % len(self._keys)
                while self._index in self._exhausted:
                    self._index = (self._index + 1) % len(self._keys)
                next_last4 = self._keys[self._index][-4:]
                logging.warning(f'🔄 搜索 Key ...{key_last4} 额度耗尽，切换到 ...{next_last4}（剩余 {remaining}/{self.total}）')
            else:
                logging.error(f'❌ 所有 {self.total} 个搜索 Key 额度已用完！')

class ChannelCircuitBreaker:
    """会话级通道熔断器：半开冷却恢复机制（自动探流）

    Why: 与 SearchKeyRotator 一样需要 threading.Lock() 保护并发安全，
         否则多线程同时 record_failure 时计数会丢失，熔断器永远不触发。
    """
    def __init__(self, threshold: int = 2, cooldown_sec: int = 300):
        self._failures = {}
        self._tripped = set()
        self._tripped_time = {}
        self._threshold = threshold
        self._cooldown = cooldown_sec
        self._lock = threading.Lock()

    def record_failure(self, channel_name: str):
        with self._lock:
            self._failures[channel_name] = self._failures.get(channel_name, 0) + 1
            if self._failures[channel_name] >= self._threshold:
                self._tripped.add(channel_name)
                self._tripped_time[channel_name] = time.time()
                logging.warning(f'⚡ 熔断器触发：{channel_name} 连续失败 {self._threshold} 次，进入冷却 ({self._cooldown}s)')

    def record_success(self, channel_name: str):
        with self._lock:
            self._failures[channel_name] = 0
            self._tripped.discard(channel_name)
            if channel_name in self._tripped_time:
                del self._tripped_time[channel_name]

    def is_tripped(self, channel_name: str) -> bool:
        with self._lock:
            if channel_name in self._tripped:
                elapsed = time.time() - self._tripped_time.get(channel_name, 0)
                if elapsed > self._cooldown:
                    logging.info(f'🛡️ 熔断器半开：{channel_name} 冷却期 ({self._cooldown}s) 结束，放行探流一次')
                    self._tripped.discard(channel_name)
                    return False
                return True
            return False

search_rotator_direct = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_DIRECT_URL)
search_rotator_cf = SearchKeyRotator(SEARCH_API_KEY_POOL, GEMINI_SEARCH_CF_URL)
search_key_rotator = search_rotator_direct
channel_breaker = ChannelCircuitBreaker(threshold=2)

GEMINI_CONCURRENCY = max(search_key_rotator.total, 1)

logging.info(f'[搜索 Key 池] 已加载 {search_rotator_direct.total} 个 Key（双通道：直连 Google + CF Worker + 熔断器）')

def raw_gemini_search(prompt: str, max_retries: int = 3) -> str:
    channels = [
        ('直连Google', search_rotator_direct),
        ('CF-Worker', search_rotator_cf)
    ]

    for channel_name, rotator in channels:
        if channel_breaker.is_tripped(channel_name):
            logging.debug(f'  [raw_search] [{channel_name}] 已熔断，跳过')
            continue

        for _attempt_key in range(rotator.total):
            base_url, key, key_idx, key_last4 = rotator.get_client()
            if base_url is None:
                logging.error(f'  [raw_search] [{channel_name}] 所有搜索 Key 额度已用尽')
                break

            for attempt in range(max_retries):
                try:
                    url = f'{base_url}/v1beta/models/{GEMINI_SEARCH_MODEL}:generateContent?key={key}'
                    payload = {
                        'contents': [{'parts': [{'text': prompt}]}],
                        'tools': [{'googleSearch': {}}],
                        'generationConfig': {
                            'temperature': 0.7,
                            'maxOutputTokens': 8192
                        }
                    }
                    response = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=180)

                    if response.status_code in (429, 403):
                        logging.warning(f'  [raw_search] [{channel_name}] Key ...{key_last4} 触发限流({response.status_code})，标记耗尽')
                        rotator.mark_exhausted(key_idx)
                        break

                    response.raise_for_status()
                    data = response.json()
                    text = ''

                    if data.get('candidates') and len(data['candidates']) > 0:
                        candidate = data['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            text = candidate['content']['parts'][0].get('text', '')

                    if text.strip():
                        channel_breaker.record_success(channel_name)
                        return text

                    logging.warning(f'  [raw_search] [{channel_name}] Gemini 返回空内容，重试 ({attempt + 1}/{max_retries})')
                    time.sleep(3)

                except requests.exceptions.ConnectTimeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f'  [raw_search] [{channel_name}] 连接超时，熔断器计数 +1')
                    break
                except Exception as exc:
                    err_str = str(exc).lower()
                    if '429' in err_str or '403' in err_str or 'quota' in err_str:
                        logging.warning(f'  [raw_search] [{channel_name}] Key ...{key_last4} 额度耗尽，标记并切换')
                        rotator.mark_exhausted(key_idx)
                        break

                    wait = 3
                    logging.warning(f'  [raw_search] [{channel_name}] 搜索异常: {exc}，{wait}s 后重试 ({attempt + 1}/{max_retries})')
                    time.sleep(wait)

    # Why: Gemini 双通道全部失败后降级到 Tavily 搜索引擎
    if TAVILY_API_KEY:
        try:
            logging.info('  [raw_search] ↘️ 降级到 Tavily 搜索引擎...')
            tavily_resp = requests.post(
                'https://api.tavily.com/search',
                json={
                    'api_key': TAVILY_API_KEY,
                    'query': prompt[:400],
                    'search_depth': 'advanced',
                    'max_results': 5,
                    'include_answer': True,
                },
                timeout=30,
            )
            tavily_resp.raise_for_status()
            tavily_data = tavily_resp.json()
            answer = tavily_data.get('answer', '')
            results_list = tavily_data.get('results', [])
            parts = []
            if answer:
                parts.append(f'[Tavily Fallback] {answer}')
            for r in results_list[:5]:
                title = r.get('title', '')
                content = r.get('content', '')
                url = r.get('url', '')
                parts.append(f'- {title}: {content[:300]} 【{url}】')
            if parts:
                channel_breaker.record_success('Tavily')
                logging.info(f'  [raw_search] ✅ Tavily 降级成功，返回 {len(parts)} 条结果')
                return '\n'.join(parts)
        except Exception as e:
            logging.warning(f'  [raw_search] Tavily 降级也失败: {e}')

    logging.error('❌ [raw_search] 所有通道和 Key 均失败（含 Tavily 降级），返回空结果')
    return ''

@sleep_and_retry
@limits(calls=1, period=1)
def gemini_search(category: str, query: str, backtrack: str = '24h', max_retries: int = 3) -> dict:
    prompt = f'你是 AI 产业链深度研究员。请对以下主题进行深入搜索。\n搜索方向：{query}\n赛道：{category}\n格式：序号. [实体] 客观事实内容 【信源|URL|日期】\n搜不到的如实说"未搜索到"。严禁编造。'

    channels = [
        ('直连Google', search_rotator_direct),
        ('CF-Worker', search_rotator_cf)
    ]

    for channel_name, rotator in channels:
        if channel_breaker.is_tripped(channel_name):
            logging.debug(f'  [{category}] [{channel_name}] 已熔断，跳过')
            continue

        for _attempt_key in range(rotator.total):
            base_url, key, key_idx, key_last4 = rotator.get_client()
            if base_url is None:
                logging.error(f'  [{category}] [{channel_name}] 所有搜索 Key 额度已用尽')
                break

            for attempt in range(max_retries):
                try:
                    url = f'{base_url}/v1beta/models/{GEMINI_SEARCH_MODEL}:generateContent?key={key}'
                    payload = {
                        'contents': [{'parts': [{'text': prompt}]}],
                        'tools': [{'googleSearch': {}}],
                        'generationConfig': {
                            'temperature': 1.0,
                            'maxOutputTokens': 8192
                        }
                    }
                    r = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=180)

                    if r.status_code in (429, 403):
                        logging.warning(f'  [{category}] [{channel_name}] Key ...{key_last4} 限流({r.status_code})，跳过')
                        rotator.mark_exhausted(key_idx)
                        break

                    r.raise_for_status()
                    data = r.json()

                    if not data.get('candidates') or not data['candidates']:
                        return {}

                    candidate = data['candidates'][0]
                    text = candidate.get('content', {}).get('parts', [{}])[0].get('text', '')
                    grounding = candidate.get('groundingMetadata', {}).get('groundingChunks', [])

                    sources = []
                    for g in grounding:
                        web = g.get('web', {})
                        if web and web.get('uri'):
                            sources.append({'url': web.get('uri'), 'title': web.get('title', '')})

                    channel_breaker.record_success(channel_name)
                    result = {'text': text, 'grounding_sources': sources}
                    return enrich_search_urls(result)

                except requests.exceptions.ConnectTimeout:
                    channel_breaker.record_failure(channel_name)
                    logging.warning(f'  [{category}] [{channel_name}] 连接超时，熔断器计数 +1')
                    break
                except Exception as exc:
                    err_str = str(exc).lower()
                    if '429' in err_str or '403' in err_str or 'quota' in err_str:
                        logging.warning(f'  [{category}] [{channel_name}] Key ...{key_last4} 额度耗尽，标记不重试')
                        rotator.mark_exhausted(key_idx)
                        break

                    wait = 3
                    logging.warning(f'  [{category}] [{channel_name}] 异常: {exc}，{wait}s 后重试 ({attempt + 1}/{max_retries})')
                    time.sleep(wait)

    # Why: Gemini 双通道全部失败后降级到 Tavily
    if TAVILY_API_KEY:
        try:
            logging.info(f'  [{category}] ↘️ 降级到 Tavily 搜索引擎...')
            tavily_resp = requests.post(
                'https://api.tavily.com/search',
                json={
                    'api_key': TAVILY_API_KEY,
                    'query': query[:400],
                    'search_depth': 'advanced',
                    'max_results': 5,
                    'include_answer': True,
                },
                timeout=30,
            )
            tavily_resp.raise_for_status()
            tavily_data = tavily_resp.json()
            answer = tavily_data.get('answer', '')
            results_list = tavily_data.get('results', [])
            parts = []
            if answer:
                parts.append(f'[Tavily Fallback] {answer}')
            sources = []
            for r in results_list[:5]:
                title = r.get('title', '')
                content = r.get('content', '')
                url = r.get('url', '')
                parts.append(f'- {title}: {content[:300]} 【{url}】')
                if url:
                    sources.append({'url': url, 'title': title})
            if parts:
                channel_breaker.record_success('Tavily')
                logging.info(f'  [{category}] ✅ Tavily 降级成功，返回 {len(parts)} 条结果')
                result = {'text': '\n'.join(parts), 'grounding_sources': sources}
                return enrich_search_urls(result)
        except Exception as e:
            logging.warning(f'  [{category}] Tavily 降级也失败: {e}')

    logging.error(f'❌ [{category}] 所有搜索途径均失败（含 Tavily 降级）')
    return {}
