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
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, ALL_COMPLETED
from urllib.parse import urljoin, urlparse

import requests
from scraper_utils import enrich_article_url, is_article_url as _is_article_url
from http_security import get_tls_policy_for_url, log_tls_policy


from config import (
    WEB_SCRAPE_SOURCES as V2_WEB_SOURCES,
    FAKE_UA,
)
from utils import parse_backtrack_hours

BEIJING_TZ = timezone(timedelta(hours=8))

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
_FUTURE_TIME_GRACE = timedelta(minutes=10)
_ASML_NEWS_NAV_PATHS = frozenset({
    '/en/news',
    '/en/news/stories',
    '/en/news/share-buyback',
    '/en/news/media-library',
    '/en/news/contact-media-relations',
    '/en/news/news-subscription',
})


def _source_candidate_urls(source_cfg: dict) -> list[str]:
    urls = []
    for candidate in [source_cfg['url'], *source_cfg.get('fallback_urls', [])]:
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _is_known_navigation_url(source_name: str, link: str) -> bool:
    if not link:
        return False
    parsed = urlparse(link)
    path = parsed.path.rstrip('/').lower()
    if source_name == 'ASML_News' and path in _ASML_NEWS_NAV_PATHS:
        return True
    return False


_WEB_DATE_PATTERNS = [
    re.compile(
        r'(?P<dt>\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?:\s?(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}))?)',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?P<dt>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s?(?:AM|PM|UTC|GMT)?)?)',
        re.IGNORECASE,
    ),
]

_DATE_ONLY_RE = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$')
_URL_DATE_RE = re.compile(r'(?P<y>20\d{2})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})')


def _normalize_web_datetime(raw_text: str) -> str:
    """将网页上的时间文本归一化为北京时间格式。"""
    if not raw_text:
        return ''

    clean = re.sub(r'\s+', ' ', raw_text.strip())
    clean = clean.replace('Published', '').replace('Updated', '').replace('posted', '')
    clean = clean.replace('|', ' ').replace('•', ' ').strip(' :-')
    if not clean:
        return ''

    dt = None
    is_date_only = False
    parse_attempts = [clean]

    normalized = clean.replace('/', '-').replace('.', '-')
    if normalized != clean:
        parse_attempts.append(normalized)

    if _DATE_ONLY_RE.match(normalized):
        is_date_only = True
        parse_attempts.extend([f'{normalized} 23:59:59', f'{normalized}T23:59:59'])

    for candidate in parse_attempts:
        c = candidate.strip()
        if not c:
            continue
        iso_candidate = c.replace('Z', '+00:00')
        iso_candidate = re.sub(r'\bUTC\b', '+00:00', iso_candidate, flags=re.IGNORECASE)
        iso_candidate = re.sub(r'\bGMT\b', '+00:00', iso_candidate, flags=re.IGNORECASE)
        try:
            dt = datetime.fromisoformat(iso_candidate)
            break
        except ValueError:
            pass

        for fmt in (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M',
            '%b %d, %Y %I:%M %p',
            '%b %d, %Y %H:%M',
            '%b %d, %Y',
            '%B %d, %Y %I:%M %p',
            '%B %d, %Y %H:%M',
            '%B %d, %Y',
        ):
            try:
                dt = datetime.strptime(c, fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            break

    if dt is None:
        return ''

    if dt.tzinfo is None:
        # Why: 网页列表页时间通常无时区，保守按北京时间看待，避免误杀刚发布新闻
        dt = dt.replace(tzinfo=BEIJING_TZ)
    else:
        dt = dt.astimezone(BEIJING_TZ)

    if is_date_only:
        dt = dt.replace(hour=23, minute=59, second=59)

    return dt.strftime('%Y-%m-%dT%H:%M:%S (北京时间)')


def _extract_web_published_at(page_html: str, raw_link: str, link: str, title: str) -> str:
    """从列表页上下文或 URL 中尽量提取发布时间。"""
    search_targets = []
    for value in (raw_link, link, title):
        if value and value not in search_targets:
            search_targets.append(value)

    for target in search_targets:
        idx = page_html.find(target)
        if idx < 0:
            continue
        context = page_html[max(0, idx - 800): idx + 800]
        for pattern in _WEB_DATE_PATTERNS:
            match = pattern.search(context)
            if match:
                normalized = _normalize_web_datetime(match.group('dt'))
                if normalized:
                    return normalized

    for candidate in (raw_link, link):
        if not candidate:
            continue
        match = _URL_DATE_RE.search(candidate)
        if match:
            normalized = _normalize_web_datetime(f"{match.group('y')}-{match.group('m')}-{match.group('d')}")
            if normalized:
                return normalized

    return ''


def _classify_time_window(published_at: str | None, hours: int) -> dict:
    if not published_at:
        return {
            'allowed': True,
            'time_status': 'unknown_time',
            'time_reason': 'missing_published_at',
        }

    try:
        clean = published_at.replace('(北京时间)', '').replace('T', ' ').strip()
        dt = datetime.strptime(clean[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING_TZ)
        now = datetime.now(BEIJING_TZ)
        if dt.year < now.year - 2 or dt.year > now.year + 1:
            return {
                'allowed': False,
                'time_status': 'invalid_time',
                'time_reason': f'year_out_of_range:{dt.year}',
            }
        if dt > now + _FUTURE_TIME_GRACE:
            return {
                'allowed': False,
                'time_status': 'future',
                'time_reason': 'published_at_in_future',
            }
        cutoff = datetime.now(BEIJING_TZ) - timedelta(hours=hours)
        is_recent = dt >= cutoff
        return {
            'allowed': is_recent,
            'time_status': 'recent' if is_recent else 'stale',
            'time_reason': '',
        }
    except Exception as exc:
        logging.warning('[P1 Web] drop entry due to unparseable published_at=%s error=%s', published_at, exc)
        return {
            'allowed': False,
            'time_status': 'parse_failed',
            'time_reason': str(exc),
        }

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
        
    source_timeout = source_cfg.get('timeout', SOURCE_TIMEOUT)

    for fetch_url in _source_candidate_urls(source_cfg):
        try:
            tls_policy = get_tls_policy_for_url(fetch_url)
            log_tls_policy('P1 Web', fetch_url, tls_policy)
            resp = requests.get(fetch_url, headers=HEADERS, timeout=source_timeout, verify=tls_policy['verify'])
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logging.warning(f'[Web] {name}: 请求超时 ({source_timeout}s) - {fetch_url}')
            continue
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 'unknown'
            logging.warning(f'[Web] {name}: HTTP {status_code} - {fetch_url}')
            continue
        except Exception as exc:
            logging.warning(f'[Web] {name}: 请求失败 - {exc} - {fetch_url}')
            continue

        try:
            page = Selector(resp.text)
        except Exception as exc:
            logging.warning(f'[Web] {name}: HTML 解析失败 - {exc} - {fetch_url}')
            continue

        try:
            elements = page.css(selector)
        except Exception as exc:
            logging.warning(f'[Web] {name}: CSS 选择器异常 ({selector}) - {exc} - {fetch_url}')
            continue

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
            logging.info(f'[Web] {name}: 所有选择器均未匹配（网站可能改版或需要 JS 渲染）- {fetch_url}')
            continue

        entries = []
        for el in elements[:max_items]:
            if title_attr == 'text':
                title = el.text.strip() if hasattr(el, 'text') and el.text else ''
            else:
                title = el.attrib.get(title_attr, '').strip() if hasattr(el, 'attrib') else ''

            if not title or len(title) < 4:
                continue

            raw_link = el.attrib.get(link_attr, '') if hasattr(el, 'attrib') else ''
            link = urljoin(fetch_url, raw_link) if raw_link else ''
            if link and (not _is_article_url(link) or _is_known_navigation_url(name, link)):
                continue
            published_at = _extract_web_published_at(resp.text, raw_link, link, title)

            entries.append({
                'title': title,
                'content': '',
                'url': link,
                'published_at': published_at,
                'source': name,
                'tls_mode': tls_policy['tls_mode'],
                'insecure_fetch': tls_policy['insecure_fetch'],
            })

        if entries:
            return entries

    return []

def _entries_to_p1_result(entries: list[dict], category: str, source_name: str, hours: int) -> dict | None:
    filtered = []
    time_window_stats = {
        'recent': 0,
        'unknown_time': 0,
        'stale': 0,
        'parse_failed': 0,
        'future': 0,
        'invalid_time': 0,
    }
    for entry in entries:
        time_window = _classify_time_window(entry.get('published_at'), hours)
        entry['time_status'] = time_window['time_status']
        entry['time_reason'] = time_window['time_reason']
        time_window_stats[time_window['time_status']] = time_window_stats.get(time_window['time_status'], 0) + 1
        if time_window['allowed']:
            filtered.append(entry)
    if not filtered:
        return None
        
    lines = []
    grounding_sources = []
    
    for i, entry in enumerate(filtered, 1):
        title = entry.get('title', '').strip()
        content = entry.get('content', '').strip()
        url = entry.get('url', '')
        source = entry.get('source', source_name)
        published_at = entry.get('published_at', '')
        
        text_line = f'{i}. [{source}] {title}'
        if content and content != title:
            text_line += f' — {content[:300]}'
        if published_at:
            text_line += f' [{published_at}]'
        elif entry.get('time_status') == 'unknown_time':
            text_line += ' [unknown_time]'
            
        text_line += f' 【{source}|{url}|】'
        lines.append(text_line)
        
        if url:
            grounding_sources.append({
                'title': title,
                'url': url,
                'tls_mode': entry.get('tls_mode', 'strict'),
                'insecure_fetch': bool(entry.get('insecure_fetch', False)),
                'published_at': published_at,
                'time_status': entry.get('time_status', 'unknown_time'),
                'time_reason': entry.get('time_reason', ''),
            })
            
    if not lines:
        return None
        
    return {
        'category': category,
        'query': f'[网页采集] {source_name}',
        'text': '\n'.join(lines),
        'grounding_sources': grounding_sources[:10],
        '_filtered_entries': filtered,
        'time_window_stats': time_window_stats,
    }

def collect_from_web(backtrack: str = '24h') -> tuple[list[dict], dict]:
    hours = parse_backtrack_hours(backtrack)
    stats = {
        'total': 0,
        'success': [],
        'empty': [],
        'failed': [],
        'time_recent': 0,
        'time_unknown': 0,
        'time_stale': 0,
        'time_parse_failed': 0,
        'time_future': 0,
        'time_invalid': 0,
    }
    
    if not _parser_available:
        logging.warning('[P1 Web] Scrapling 未安装，跳过网页采集')
        return [], stats
        
    if not V2_WEB_SOURCES:
        logging.info('[P1 Web] 未配置网页数据源，跳过网页采集')
        return [], stats
        
    all_results = []
    stats['total'] = len(V2_WEB_SOURCES)
    
    logging.info('============================================================')
    logging.info(f'[P1 Web 采集] requests + Scrapling Parser（{len(V2_WEB_SOURCES)} 个源，回溯 {backtrack}）')
    logging.info('============================================================')
    
    def _run_task(source_cfg: dict) -> tuple[dict | None, str, str]:
        name = source_cfg.get('name', 'unknown')
        category = source_cfg.get('category', '网页采集')
        
        time.sleep(random.uniform(0.3, 1.5))
        try:
            entries = _scrape_one_source(source_cfg)
            if not entries:
                return None, 'empty', '选择器未匹配或页面不可达'
                
            result = _entries_to_p1_result(entries, category, name, hours)
            if result is None:
                return None, 'empty', f'{len(entries)} 条但均超出 {hours}h 时间窗'
            # Why: 保留原始条目供 Phase 2 深挖后回写主文本，最后返回前会清理内部字段
            result['_entries'] = result.get('_filtered_entries', [])
            result['_source_name'] = name
            result['_category'] = category
            return result, 'ok', ''
        except Exception as exc:
            return None, 'error', str(exc)
            
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_run_task, src): src for src in V2_WEB_SOURCES}
        done, not_done = wait(futures.keys(), timeout=180, return_when=ALL_COMPLETED)

        for future in done:
            source_cfg = futures[future]
            source_name = source_cfg.get('name', 'unknown')
            try:
                result, status, reason = future.result()
                if status == 'ok' and result:
                    n_items = len(result['grounding_sources'])
                    time_window_stats = result.get('time_window_stats', {})
                    stats['time_recent'] += int(time_window_stats.get('recent', 0))
                    stats['time_unknown'] += int(time_window_stats.get('unknown_time', 0))
                    stats['time_stale'] += int(time_window_stats.get('stale', 0))
                    stats['time_parse_failed'] += int(time_window_stats.get('parse_failed', 0))
                    stats['time_future'] += int(time_window_stats.get('future', 0))
                    stats['time_invalid'] += int(time_window_stats.get('invalid_time', 0))
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

        for future in not_done:
            source_cfg = futures[future]
            source_name = source_cfg.get('name', 'unknown')
            future.cancel()
            reason = '采集任务超时（180s），已保留其他已完成结果'
            stats['failed'].append((source_name, reason))
            logging.warning(f'  ⏱️ {source_name}: {reason}')
                
    logging.info(f'\n[P1 Web Phase 1 完毕] {len(stats["success"])}/{stats["total"]} 源成功 | 发现: {sum(n for _, n in stats["success"])} 篇')
    
    if _parser_available and all_results:
        enrich_ok = 0
        enrich_fail = 0
        work_items = []
        
        for result in all_results:
            source_name = result.get('_source_name', result['query'].replace('[网页采集] ', ''))
            for entry in result.get('_entries', []):
                url = entry.get('url', '')
                if url and _is_article_url(url):
                    work_items.append((entry, url, source_name))
                    
        if work_items:
            random.shuffle(work_items)
            
            def _process_one_web_item(item):
                entry, url, source_name = item
                return entry, enrich_article_url(url, source_name)
                
            total_items = len(work_items)
            processed = 0
            
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_process_one_web_item, item): item for item in work_items}
                
                for future in as_completed(futures):
                    try:
                        entry_ref, content = future.result()
                        if content:
                            # Why: 深挖内容直接回填 entry['content']，确保进入 _entries_to_p1_result 主文本
                            entry_ref['content'] = content
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

            # Why: 深挖后重建文本，将 entry.content 真正写入 P1 输出
            rebuilt_results = []
            for result in all_results:
                source_name = result.get('_source_name', result.get('query', '').replace('[网页采集] ', ''))
                category = result.get('_category', result.get('category', '网页采集'))
                entries = result.get('_entries', [])

                refreshed = _entries_to_p1_result(entries, category, source_name, hours) if entries else None
                if refreshed:
                    rebuilt_results.append(refreshed)
            all_results = rebuilt_results

    # 清理内部字段，避免外部调用方感知中间态结构
    for result in all_results:
        result.pop('_entries', None)
        result.pop('_source_name', None)
        result.pop('_category', None)
        result.pop('_filtered_entries', None)
            
    return all_results, stats

if __name__ == '__main__':
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
