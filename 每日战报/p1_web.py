"""
P1 Web Collector — 方案 C v3：基于 Scrapling 的网页采集 + 正文深挖。

与 p1_rss.py 并行运行，补充 RSS 无法覆盖的信息源。
架构：requests 获取 HTML + Scrapling Parser 解析提取 + Phase 2 正文深挖。
输出格式与 collect_from_rss() 完全一致，供 P5 审计使用。

【⚠️核心维护规则 / Agent 指令】
1. 基于 requests + Scrapling Selector 的网页防抓架构具有极脆性，扩展爬取规则时必须保留现有的 fallback 选择器兜底逻辑（fb_elems）。
2. 本文件的正文深挖能力已抽取到 `scraper_utils.py` 的 `enrich_article_url()` 函数中，涉及 digitimes Google WebCache 镜像绕过等反爬手段。修改深挖逻辑请去 scraper_utils.py，严禁在 p1_web.py 里重复造轮子。
3. P1 阶段绝对不许自己决定哪些链接不要（除非是黑名单过滤），必须通过原样输出交给后续层级判断。
"""

from __future__ import annotations
import logging
import time
import random
from datetime import timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
import warnings
import urllib3

from config import (
    WEB_SCRAPE_SOURCES as V2_WEB_SOURCES,
    FAKE_UA,
    SUMMARY_LEN,
    SOURCE_CONTENT_SELECTORS,
    GENERIC_CONTENT_SELECTORS,
    URL_BLACKLIST,
    CONTENT_NOISE_KEYWORDS
)

BEIJING_TZ = timezone(timedelta(hours=8))

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)

_parser_available = False
try:
    from scrapling import Selector
    _parser_available = True
except ImportError:
    try:
        from scrapling.parser import Selector
        _parser_available = True
    except ImportError:
        logging.warning('⚠️ Scrapling 未安装，网页采集引擎不可用 (pip install scrapling)')

HEADERS = {
    'User-Agent': FAKE_UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8'
}

SOURCE_TIMEOUT = 20

def _is_article_url(url: str) -> bool:
    url_lower = url.lower()
    for pattern in URL_BLACKLIST:
        if pattern in url_lower:
            return False
    return True

def _enrich_article_content(url: str, source_name: str) -> str:
    if not _parser_available or not url:
        return ''
        
    try:
        time.sleep(random.uniform(0.2, 0.5))
        fetch_url = url
        if 'digitimes' in source_name.lower():
            fetch_url = f'https://webcache.googleusercontent.com/search?q=cache:{url}'
            
        resp = requests.get(fetch_url, headers=HEADERS, timeout=15, verify=False)
        if resp.status_code >= 400:
            if fetch_url != url:
                resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
                if resp.status_code >= 400:
                    return ''
            else:
                return ''
                
        page = Selector(resp.text)
        selectors = SOURCE_CONTENT_SELECTORS.get(source_name, []) + GENERIC_CONTENT_SELECTORS
        for sel in selectors:
            try:
                paras = page.css(sel)
                texts = []
                for p in paras:
                    t = p.text.strip() if p.text else ''
                    if len(t) > 30 and not any(kw in t.lower() for kw in CONTENT_NOISE_KEYWORDS):
                        texts.append(t)
                if texts and len(' '.join(texts)) > 50:
                    return ' '.join(texts)[:SUMMARY_LEN]
            except Exception as e:
                logging.warning(f"[P1 Web] 选择器 {sel} 解析异常: {e}")
                continue
                
        for meta_sel in ("meta[name='description']", "meta[property='og:description']"):
            try:
                metas = page.css(meta_sel)
                if metas:
                    c = metas[0].attrib.get('content', '').strip()
                    if c and len(c) > 30:
                        return c[:SUMMARY_LEN]
            except Exception as e:
                logging.warning(f"[P1 Web] Meta 选择器解析异常: {e}")
                continue
                
        return ''
    except Exception as e:
        logging.warning(f"[P1 Web] {url} 正文深度抓取失败: {e}")
        return ''

def _scrape_one_source(source_cfg: dict) -> list[dict]:
    url = source_cfg['url']
    name = source_cfg.get('name', url)
    selector = source_cfg.get('selector', '')
    title_attr = source_cfg.get('title_attr', 'text')
    link_attr = source_cfg.get('link_attr', 'href')
    max_items = source_cfg.get('max_items', 50)
    
    if not selector:
        logging.warning(f'[Web] {name}: 未配置 CSS 选择器，跳过')
        return []
        
    try:
        resp = requests.get(url, headers=HEADERS, timeout=SOURCE_TIMEOUT, verify=False)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logging.warning(f'[Web] {name}: 请求超时 ({SOURCE_TIMEOUT}s)')
        return []
    except requests.exceptions.HTTPError as exc:
        logging.warning(f'[Web] {name}: HTTP {exc.response.status_code}')
        return []
    except Exception as exc:
        logging.warning(f'[Web] {name}: 请求失败 - {exc}')
        return []
        
    try:
        page = Selector(resp.text)
    except Exception as exc:
        logging.warning(f'[Web] {name}: HTML 解析失败 - {exc}')
        return []
        
    try:
        elements = page.css(selector)
    except Exception as exc:
        logging.warning(f'[Web] {name}: CSS 选择器异常 ({selector}) - {exc}')
        return []
        
    if not elements:
        fallback_selectors = ['h2 a', 'h3 a', 'article a', '.title a', 'a[title]']
        for fb_sel in fallback_selectors:
            try:
                fb_elems = page.css(fb_sel)
                if fb_elems and len(fb_elems) >= 3:
                    logging.info(f"[Web] {name}: 主选择器未匹配，自动发现 '{fb_sel}' ({len(fb_elems)} 个)")
                    elements = fb_elems
                    break
            except Exception as e:
                logging.warning(f"[P1 Web] 解析选择器异常: {e}")
                continue
                
    if not elements:
        logging.info(f'[Web] {name}: 所有选择器均未匹配（网站可能改版或需要 JS 渲染）')
        return []
        
    entries = []
    for el in elements[:max_items]:
        if title_attr == 'text':
            title = el.text.strip() if hasattr(el, 'text') and el.text else ''
        else:
            title = el.attrib.get(title_attr, '').strip() if hasattr(el, 'attrib') else ''
            
        if not title or len(title) < 4:
            continue
            
        raw_link = el.attrib.get(link_attr, '') if hasattr(el, 'attrib') else ''
        link = urljoin(url, raw_link) if raw_link else ''
        
        entries.append({
            'title': title,
            'content': '',
            'url': link,
            'published_at': '',
            'source': name
        })
    return entries

def _entries_to_p1_result(entries: list[dict], category: str, source_name: str) -> dict | None:
    if not entries:
        return None
        
    lines = []
    grounding_sources = []
    
    for i, entry in enumerate(entries, 1):
        title = entry.get('title', '').strip()
        content = entry.get('content', '').strip()
        url = entry.get('url', '')
        source = entry.get('source', source_name)
        
        text_line = f'{i}. [{source}] {title}'
        if content and content != title:
            text_line += f' — {content[:300]}'
            
        text_line += f' 【{source}|{url}|】'
        lines.append(text_line)
        
        if url:
            grounding_sources.append({'title': title, 'url': url})
            
    if not lines:
        return None
        
    return {
        'category': category,
        'query': f'[网页采集] {source_name}',
        'text': '\n'.join(lines),
        'grounding_sources': grounding_sources[:10]
    }

def collect_from_web(backtrack: str = '24h') -> tuple[list[dict], dict]:
    stats = {'total': 0, 'success': [], 'empty': [], 'failed': []}
    
    if not _parser_available:
        logging.warning('[P1 Web] Scrapling 未安装，跳过网页采集')
        return [], stats
        
    if not V2_WEB_SOURCES:
        logging.info('[P1 Web] 未配置网页数据源，跳过网页采集')
        return [], stats
        
    all_results = []
    stats['total'] = len(V2_WEB_SOURCES)
    
    logging.info('============================================================')
    logging.info(f'[P1 Web 采集] requests + Scrapling Parser（{len(V2_WEB_SOURCES)} 个源）')
    logging.info('============================================================')
    
    def _run_task(source_cfg: dict) -> tuple[dict | None, str, str]:
        name = source_cfg.get('name', 'unknown')
        category = source_cfg.get('category', '网页采集')
        
        time.sleep(random.uniform(0.3, 1.5))
        try:
            entries = _scrape_one_source(source_cfg)
            if not entries:
                return None, 'empty', '选择器未匹配或页面不可达'
                
            result = _entries_to_p1_result(entries, category, name)
            if result is None:
                return None, 'empty', f'{len(entries)} 条但转换失败'
            return result, 'ok', ''
        except Exception as exc:
            return None, 'error', str(exc)
            
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_run_task, src): src for src in V2_WEB_SOURCES}
        
        for future in as_completed(futures, timeout=180):
            source_cfg = futures[future]
            source_name = source_cfg.get('name', 'unknown')
            try:
                result, status, reason = future.result(timeout=30)
                if status == 'ok' and result:
                    n_items = len(result['grounding_sources'])
                    all_results.append(result)
                    stats['success'].append((source_name, n_items))
                    logging.info(f'  ✅ {source_name}: {n_items} 条')
                elif status == 'empty':
                    stats['empty'].append((source_name, reason))
                    logging.info(f'  ⏭️  {source_name}: {reason}')
                else:
                    stats['failed'].append((source_name, reason))
                    logging.warning(f'  ❌ {source_name}: {reason}')
            except Exception as exc:
                stats['failed'].append((source_name, str(exc)))
                logging.warning(f'  ❌ {source_name}: {exc}')
                
    logging.info(f'\n[P1 Web Phase 1 完毕] {len(stats["success"])}/{stats["total"]} 源成功 | 发现: {sum(n for _, n in stats["success"])} 篇')
    
    if _parser_available and all_results:
        enrich_ok = 0
        enrich_fail = 0
        work_items = []
        
        for result in all_results:
            source_name = result['query'].replace('[网页采集] ', '')
            for gs in result.get('grounding_sources', []):
                url = gs.get('url', '')
                if url and _is_article_url(url):
                    work_items.append((gs, url, source_name))
                    
        if work_items:
            random.shuffle(work_items)
            
            def _process_one_web_item(item):
                gs, url, source_name = item
                return gs, _enrich_article_content(url, source_name)
                
            total_items = len(work_items)
            processed = 0
            
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_process_one_web_item, item): item for item in work_items}
                
                for future in as_completed(futures):
                    try:
                        gs_ref, content = future.result()
                        if content:
                            gs_ref['content_summary'] = content
                            enrich_ok += 1
                        else:
                            enrich_fail += 1
                    except Exception:
                        enrich_fail += 1
                        
                    processed += 1
                    if processed % 10 == 0 or processed == total_items:
                        logging.info(f'  [P1 Web Phase 2] 进度: {processed}/{total_items} | ✅ {enrich_ok} | ❌ {enrich_fail}')
                        
            logging.info(f'[P1 Web Phase 2] 正文深挖完毕: ✅ {enrich_ok} / ❌ {enrich_fail}')
            stats['enrich_ok'] = enrich_ok
            stats['enrich_fail'] = enrich_fail
            
    return all_results, stats

if __name__ == '__main__':
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    results, stats = collect_from_web('24h')
    print(f'\n===== 网页采集结果: {len(results)} 个数据源 =====\n')
    total_items = 0
    for r in results:
        lines = r['text'].split('\n')
        total_items += len(lines)
        print(f"[{r['category']}] {r['query']}: {len(lines)} 条")
        for line in lines[:3]:
            print(f"  {line[:120]}")
        if len(lines) > 3:
            print(f"  ... 还有 {len(lines)-3} 条")
        print()
    print(f"总计: {total_items} 条新闻")
    print(f"\n📊 统计: 成功 {len(stats['success'])} | 空 {len(stats['empty'])} | 失败 {len(stats['failed'])}")
