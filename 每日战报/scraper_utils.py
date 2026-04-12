"""
Scrapling 公共工具 — 统一的正文深挖和 URL 过滤逻辑。

消除原先在 daily_report.py、p1_rss.py、p1_web.py 中的 3 处重复代码。

【⚠️核心维护规则 / Agent 指令】
1. 本文件是 `p1_rss.py`、`p1_web.py`、`search_engine.py` 三方共用的正文深挖工具库。修改任何函数签名或返回格式时，必须同时检查三个调用方的兼容性。
2. `enrich_article_url()` 中针对 digitimes 走 Google WebCache 镜像的绕过逻辑，是已验证的付费墙破防手段，严禁删除。
3. `enrich_search_urls()` 的分级深挖策略（HIGH_VALUE_DOMAINS 完整正文深挖 vs 普通域名仅 meta 提取）是在"深度 vs 速度"之间精心平衡的折中方案，修改排序权重或扩大深挖范围前必须评估总耗时影响。
"""

import time
import random
import logging
import requests
from urllib.parse import urlparse
from config import SUMMARY_LEN, SOURCE_CONTENT_SELECTORS, GENERIC_CONTENT_SELECTORS, CONTENT_NOISE_KEYWORDS, FAKE_UA, URL_BLACKLIST
from http_security import get_tls_policy_for_url, log_tls_policy

scrapling_available = False
_Selector = None

try:
    from scrapling import Selector as _Selector
    scrapling_available = True
except ImportError:
    try:
        from scrapling.parser import Selector as _Selector
        scrapling_available = True
    except ImportError:
        pass

HEADERS = {
    'User-Agent': FAKE_UA,
    'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8'
}

HIGH_VALUE_DOMAINS: set[str] = frozenset({
    'seekingalpha.com', 'skhynix.com', 'sec.gov', 'broadcom.com', 'eetimes.com',
    'semiengineering.com', 'reuters.com', 'tomshardware.com', 'asml.com', 'trendforce.com',
    'datacenterknowledge.com', 'digitimes.com', 'bloomberg.com', 'nvidia.com', 'amd.com',
    'semianalysis.com', 'semi.org', 'intel.com', 'servethehome.com', 'tsmc.com',
    'techpowerup.com', 'micron.com', 'thelec.kr', 'anandtech.com'
})

def _is_high_value_domain(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace('www.', '')
        return any(hv in domain for hv in HIGH_VALUE_DOMAINS)
    except Exception as e:
        logging.debug(f'Ignored return False exception: {e}')
        return False

def is_article_url(url: str) -> bool:
    url_lower = url.lower()
    for pattern in URL_BLACKLIST:
        if pattern in url_lower:
            return False
    return True

def _quick_meta_extract(url: str) -> str:
    if not scrapling_available or not _Selector:
        return ''
    try:
        tls_policy = get_tls_policy_for_url(url)
        log_tls_policy('ScraperMeta', url, tls_policy)
        resp = requests.get(url, headers=HEADERS, timeout=8, verify=tls_policy['verify'])
        if resp.status_code >= 400:
            return ''
        page = _Selector(resp.text)
        for ms in ["meta[name='description']", "meta[property='og:description']"]:
            try:
                metas = page.css(ms)
                if metas:
                    c = metas[0].attrib.get('content', '').strip()
                    if c and len(c) > 30:
                        return c[:SUMMARY_LEN]
            except Exception as e:
                logging.debug(f'[Scrapling] meta 选择器 {ms} 未命中: {e}')
        return ''
    except Exception as e:
        logging.debug(f'[Scrapling] _quick_meta_extract 请求失败 {url}: {e}')
        return ''

def extract_content_from_html(html_text: str, source_name: str = '', url: str = '') -> str:
    if not scrapling_available or not _Selector:
        return ''
    page = _Selector(html_text)
    selectors = []
    if source_name and source_name in SOURCE_CONTENT_SELECTORS:
        selectors = list(SOURCE_CONTENT_SELECTORS[source_name])
    elif url:
        domain = urlparse(url).netloc.lower()
        for source_key, sels in SOURCE_CONTENT_SELECTORS.items():
            if source_key.lower() in domain or domain in source_key.lower():
                selectors = list(sels)
                break
    
    selectors.extend(GENERIC_CONTENT_SELECTORS)
    for sel in selectors:
        try:
            paras = page.css(sel)
            texts = []
            for p in paras:
                t = p.text.strip() if p.text else ''
                if len(t) > 30 and not any(kw in t.lower() for kw in CONTENT_NOISE_KEYWORDS):
                    texts.append(t)
            if texts and len(' '.join(texts)) > 100:
                return ' '.join(texts)[:SUMMARY_LEN]
        except Exception as e:
            logging.debug(f'[Scrapling] CSS 选择器 {sel} 未命中: {e}')
            
    for ms in ["meta[name='description']", "meta[property='og:description']"]:
        try:
            metas = page.css(ms)
            if metas:
                c = metas[0].attrib.get('content', '').strip()
                if c and len(c) > 30:
                    return c[:SUMMARY_LEN]
        except Exception as e:
            logging.debug(f'[Scrapling] meta fallback 选择器未命中: {e}')
            
    return ''

def enrich_article_url(url: str, source_name: str = '', return_meta: bool = False):
    """一手源正文深挖——增强版。

    Why: 原版只用 requests.get 裸拉 HTML，对 JS 渲染站点（seekingalpha/bloomberg）
         和 Cloudflare 保护站点命中率为 0%。增强策略：
         1. 直连原站 → 2. Google WebCache 镜像 → 3. og:description 降级
    """
    if not scrapling_available or not url or not is_article_url(url):
        return ''
    try:
        time.sleep(random.uniform(0.2, 0.5))

        # Why: 构建尝试列表：原站 + Google WebCache 镜像
        fetch_urls = [url]
        # digitimes 等付费墙站点优先走 WebCache
        if 'digitimes' in source_name.lower() or 'digitimes' in url.lower():
            fetch_urls.insert(0, f'https://webcache.googleusercontent.com/search?q=cache:{url}')
        else:
            # Why: 对所有高价值域名通用追加 WebCache 回退
            if _is_high_value_domain(url):
                fetch_urls.append(f'https://webcache.googleusercontent.com/search?q=cache:{url}')

        last_meta = {
            'requested_url': url,
            'resolved_url': '',
            'tls_mode': 'strict',
            'insecure_fetch': False,
        }

        for fetch_url in fetch_urls:
            try:
                tls_policy = get_tls_policy_for_url(fetch_url)
                log_tls_policy('ScraperFetch', fetch_url, tls_policy)
                last_meta = {
                    'requested_url': url,
                    'resolved_url': fetch_url,
                    'tls_mode': tls_policy['tls_mode'],
                    'insecure_fetch': tls_policy['insecure_fetch'],
                }
                resp = requests.get(fetch_url, headers=HEADERS, timeout=12, verify=tls_policy['verify'])
                if resp.status_code >= 400:
                    continue
                content = extract_content_from_html(resp.text, source_name=source_name, url=url)
                if content and len(content) > 100:
                    return (content, last_meta) if return_meta else content
                # Why: 正文提取失败（JS 渲染空壳），降级到 meta 标签
                content = _extract_meta_from_html(resp.text)
                if content:
                    return (content, last_meta) if return_meta else content
            except requests.exceptions.RequestException:
                continue

        return ('', last_meta) if return_meta else ''
    except Exception as e:
        logging.debug(f'[Scrapling] enrich_article_url 请求失败 {url}: {e}')
        empty_meta = {
            'requested_url': url,
            'resolved_url': '',
            'tls_mode': 'strict',
            'insecure_fetch': False,
        }
        return ('', empty_meta) if return_meta else ''


def _extract_meta_from_html(html_text: str) -> str:
    """从 HTML 中提取 og:description / description meta 标签内容。

    Why: 即使 JS 渲染站点的正文 CSS 选择器全部失效，meta 标签通常仍在
         服务端渲染的 <head> 中可用，是最后一道非结构化数据防线。
    """
    if not scrapling_available or not _Selector:
        return ''
    try:
        page = _Selector(html_text)
        for ms in ["meta[property='og:description']", "meta[name='description']"]:
            try:
                metas = page.css(ms)
                if metas:
                    c = metas[0].attrib.get('content', '').strip()
                    if c and len(c) > 50:
                        return c[:SUMMARY_LEN]
            except Exception:
                continue
    except Exception:
        pass
    return ''

def enrich_search_urls(result: dict) -> dict:
    if not scrapling_available or not isinstance(result, dict):
        return result or {}
    sources = result.get('grounding_sources', [])
    if not sources:
        return result
        
    sorted_sources = sorted(sources[:12], key=lambda s: 0 if _is_high_value_domain(s.get('url', '')) else 1)
    
    enriched_parts = []
    full_count = 0
    meta_count = 0
    
    for src in sorted_sources:
        url = src.get('url', '')
        title = src.get('title', '')
        if not url:
            continue
            
        if _is_high_value_domain(url):
            content = enrich_article_url(url, source_name='')
            if content:
                enriched_parts.append(f'\n---\n【正文深挖·一手源】{title}\n{content}')
                full_count += 1
                continue
                
        content = _quick_meta_extract(url)
        if content:
            enriched_parts.append(f'\n---\n【摘要提取】{title}\n{content}')
            meta_count += 1
            
    if enriched_parts:
        result['text'] = result.get('text', '') + '\n' + '\n'.join(enriched_parts)
        logging.info(f'  [Scrapling增强] 深挖 {full_count} 一手源 + {meta_count} 摘要 / {len(sorted_sources)} 个 URL')
        
    return result
