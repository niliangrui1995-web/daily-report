"""
P1 RSS Collector — 方案 C v3：RSS 发现 + Scrapling 正文深挖。

架构：
  Phase 1: feedparser/requests 抓取 RSS、Reddit、HackerNews → 发现文章 URL
  Phase 2: Scrapling 逐篇深挖正文摘要（≤5000 字）→ 丰富 P5 审计数据
输出与 collect_intelligence() 相同的格式供 P5 审计使用。

【⚠️核心维护规则 / Agent 指令】
1. P1 属于“无极过滤”的底座收集层。严禁在此处加入针对特定新闻词汇的白名单或黑名单软过滤截断，把甄别工作留给后面的大模型。
2. 对于时间戳解析（_parse_rss_date_与_is_within_window）必须坚守严格的 UTC 转北京时间的时区修正逻辑，不许改动，防止因为时区污染导致超期废旧新闻漏网。
3. Phase 1.5 的纯文本 Jaccard 查重与 Phase 2 的 Scrapling 正文深挖构成了防爆上下文的关键，修改时严禁干掉查重步骤。
"""

from __future__ import annotations
import logging
import re
import time
import random
import warnings
import urllib3
from datetime import datetime, timedelta, timezone
import feedparser
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, ALL_COMPLETED

from config import (
    RSS_FEEDS as V2_RSS_FEEDS,
    REDDIT_SUBS as V2_REDDIT_SUBS,
    FAKE_UA,
    SEC_USER_AGENT,
    REQUEST_TIMEOUT,
    SUMMARY_LEN,
)
from scraper_utils import is_article_url as _is_article_url
from utils import parse_backtrack_hours

BEIJING_TZ = timezone(timedelta(hours=8))

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)

try:
    from scraper_utils import scrapling_available as _scrapling_available
except ImportError:
    _scrapling_available = False
    logging.warning('⚠️ scraper_utils 导入失败，Phase 2 正文深挖不可用')

HEADERS = {
    'User-Agent': FAKE_UA,
    'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8'
}

def _parse_rss_date(entry: dict) -> str:
    for key in ('published_parsed', 'updated_parsed'):
        value = entry.get(key)
        if value:
            try:
                dt_utc = datetime(*value[:6], tzinfo=timezone.utc)
                dt_bj = dt_utc.astimezone(BEIJING_TZ)
                return dt_bj.strftime('%Y-%m-%dT%H:%M:%S (北京时间)')
            except Exception as e:
                logging.debug(f'Ignored pass exception: {e}')
    pub = entry.get('published') or entry.get('updated') or ''
    return pub[:19]

def _fetch_rss(name: str, url: str, source_tag: str) -> list[dict]:
    if source_tag.startswith('sec_'):
        headers = {'User-Agent': SEC_USER_AGENT, 'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8'}
    else:
        headers = {'User-Agent': FAKE_UA, 'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8'}
    
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        feed = feedparser.parse(url, agent=headers['User-Agent'])
        
    if getattr(feed, 'bozo', False) and not getattr(feed, 'entries', False):
        return []
        
    entries = []
    for entry in feed.entries[:50]:
        title = entry.get('title', '').strip()
        if not title:
            continue
        summary = entry.get('summary') or entry.get('description') or ''
        summary = re.sub('<[^>]+>', '', summary).strip()
        link = entry.get('link', '')
        pub = _parse_rss_date(entry)
        entries.append({
            'title': title,
            'content': summary[:500],
            'url': link,
            'published_at': pub,
            'source': source_tag
        })
    return entries

def _fetch_reddit(sub: str) -> list[dict]:
    url = f'https://www.reddit.com/r/{sub}/new.json?limit=50'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            entries = []
            for child in data.get('data', {}).get('children', []):
                post = child.get('data', {})
                created = post.get('created_utc', 0)
                if created:
                    pub = datetime.fromtimestamp(created, tz=timezone.utc).astimezone(BEIJING_TZ).strftime('%Y-%m-%dT%H:%M:%S (北京时间)')
                else:
                    pub = ''
                    
                entries.append({
                    'title': post.get('title', ''),
                    'content': post.get('selftext', '')[:500],
                    'url': f'https://www.reddit.com{post.get("permalink", "")}',
                    'published_at': pub,
                    'source': f'reddit_{sub}'
                })
            return entries
    except Exception as e:
        logging.debug(f'Ignored pass exception: {e}')
        pass
        
    rss_url = f'https://www.reddit.com/r/{sub}/new/.rss?limit=50'
    return _fetch_rss(f'Reddit/{sub}', rss_url, f'reddit_{sub}')

def _fetch_hackernews() -> list[dict]:
    try:
        resp = requests.get('https://hacker-news.firebaseio.com/v0/newstories.json', timeout=REQUEST_TIMEOUT)
        ids = resp.json()[:50]
    except Exception as e:
        logging.debug(f'Ignored return [] exception: {e}')
        return []
        
    def _fetch_one_hn_story(item_id: int) -> dict | None:
        try:
            item = requests.get(f'https://hacker-news.firebaseio.com/v0/item/{item_id}.json', timeout=5).json()
            if not item or item.get('type') != 'story':
                return None
            created = item.get('time', 0)
            if created:
                pub = datetime.fromtimestamp(created, tz=timezone.utc).astimezone(BEIJING_TZ).strftime('%Y-%m-%dT%H:%M:%S (北京时间)')
            else:
                pub = ''
            return {
                'title': item.get('title', ''),
                'content': '',
                'url': item.get('url', f'https://news.ycombinator.com/item?id={item_id}'),
                'published_at': pub,
                'source': 'hackernews'
            }
        except Exception as e:
            logging.debug(f'Ignored continue exception: {e}')
            return None

    entries = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one_hn_story, item_id): item_id for item_id in ids}
        done, not_done = wait(futures.keys(), timeout=60, return_when=ALL_COMPLETED)

        for future in done:
            try:
                entry = future.result()
                if entry:
                    entries.append(entry)
            except Exception as e:
                logging.debug(f'Ignored continue exception: {e}')

        for future in not_done:
            future.cancel()
            item_id = futures[future]
            logging.debug(f'Ignored timeout item_id exception: {item_id}')

    return entries
def _is_within_window(published_at: str | None, hours: int) -> bool:
    if not published_at:
        return True
    try:
        is_beijing = '(北京时间)' in published_at
        clean = published_at.replace('(北京时间)', '').replace('Z', '+00:00').replace('T', ' ').strip()
        try:
            dt = datetime.fromisoformat(clean)
        except ValueError:
            dt = datetime.strptime(clean[:19], '%Y-%m-%d %H:%M:%S')
            
        if dt.tzinfo is None:
            if is_beijing:
                dt = dt.replace(tzinfo=BEIJING_TZ)
            else:
                dt = dt.replace(tzinfo=timezone.utc)
                
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return dt >= cutoff
    except Exception as e:
        logging.debug(f'Ignored return True exception: {e}')
        return True

def _enrich_article(entry: dict) -> str:
    if not _scrapling_available:
        return ''
    url = entry.get('url', '')
    source_name = entry.get('_feed_name', entry.get('source', ''))
    if url:
        if _is_article_url(url):
            try:
                from scraper_utils import enrich_article_url
                return enrich_article_url(url, source_name=source_name)
            except Exception as e:
                logging.debug(f'Ignored return  exception: {e}')
                return ''
    return ''

def _entries_to_p1_result(entries: list[dict], category: str, source_name: str, hours: int) -> dict | None:
    filtered = [e for e in entries if _is_within_window(e.get('published_at'), hours)]
    if not filtered:
        return None
    lines = []
    grounding_sources = []
    for i, entry in enumerate(filtered, 1):
        title = entry.get('title', '').strip()
        content = entry.get('content', '').strip()
        url = entry.get('url', '')
        pub = entry.get('published_at', 'N/A')
        source = entry.get('source', source_name)
        text_line = f'{i}. [{source}] {title}'
        if content and content != title:
            text_line += f' — {content[:300]}'
        text_line += f' 【{source}|{url}|{pub}】'
        lines.append(text_line)
        if url:
            grounding_sources.append({'title': title, 'url': url})
            
    if not lines:
        return None
        
    return {
        'category': category,
        'query': f'[RSS直采] {source_name}',
        'text': '\n'.join(lines),
        'grounding_sources': grounding_sources[:10]
    }

def _rss_name_to_category(name: str) -> str:
    name_lower = name.lower()
    if any(kw in name_lower for kw in ('bloomberg', 'cnbc', 'reuters')):
        return '主流外媒'
    if any(kw in name_lower for kw in ('semi', 'chip', 'semiconductor')):
        return '半导体产业链'
    if any(kw in name_lower for kw in ('seeking', 'alpha')):
        return '财务锚点'
    if any(kw in name_lower for kw in ('sec', 'gov', 'edgar')):
        return '官方公告'
    if any(kw in name_lower for kw in ('nvidia', 'intel', 'amd')):
        return '算力核心'
    if any(kw in name_lower for kw in ('lightwave', 'fierce_telecom', 'telecom', 'fiber', 'optical')):
        return '光通信产业链'
    if any(kw in name_lower for kw in ('datacenter', 'data_center')):
        return 'AI服务器基建'
    if any(kw in name_lower for kw in ('elec_kr', 'thelec', 'hbm', 'hynix', 'samsung_semi')):
        return 'HBM与存储'
    if any(kw in name_lower for kw in ('fierceelectronics', 'fierce_electronics')):
        return '电子供应链'
    if any(kw in name_lower for kw in ('tech', 'trend', 'digitimes')):
        return '产业链动态'
    return '综合RSS'

def collect_from_rss(backtrack: str = '24h') -> tuple[list[dict], dict]:
    hours = parse_backtrack_hours(backtrack)
    all_results = []
    stats = {'total': 0, 'success': [], 'empty': [], 'failed': []}
    
    logging.info('============================================================')
    logging.info(f'[P1 RSS 采集] 多源直接抓取模式（回溯 {backtrack}）')
    logging.info('============================================================')
    
    tasks = []
    
    for feed_cfg in V2_RSS_FEEDS:
        name = feed_cfg.get('name', 'RSS')
        url = feed_cfg.get('url', '')
        source_tag = feed_cfg.get('source', name.lower())
        if url:
            tasks.append((_fetch_rss, (name, url, source_tag), _rss_name_to_category(name), name))
            
    for sub in V2_REDDIT_SUBS:
        tasks.append((_fetch_reddit, (sub,), 'Reddit产业讨论', f'Reddit/r/{sub}'))
        
    tasks.append((_fetch_hackernews, (), '极客社区', 'HackerNews'))
    stats['total'] = len(tasks)
    logging.info(f'[P1 Phase 1] 共 {len(tasks)} 个采集任务，并发执行...')
    
    all_entries = []
    
    def _run_task(task_info):
        fn, args, category, source_name = task_info
        time.sleep(random.uniform(0.1, 0.5))
        try:
            entries = fn(*args)
            if not entries:
                return [], 'empty', 'RSS 返回 0 条目（可能近期无更新）'
            for e in entries:
                e['_feed_name'] = source_name
                e['_category'] = category
            return entries, 'ok', ''
        except Exception as exc:
            return [], 'error', str(exc)
            
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_run_task, t): t for t in tasks}
        done, not_done = wait(futures.keys(), timeout=180, return_when=ALL_COMPLETED)

        for future in done:
            task_info = futures[future]
            source_name = task_info[3]
            category = task_info[2]
            try:
                entries, status, reason = future.result()
                if status == 'ok' and entries:
                    filtered = [e for e in entries if _is_within_window(e.get('published_at'), hours)]
                    if filtered:
                        all_entries.extend(filtered)
                        stats['success'].append((source_name, len(filtered)))
                        logging.info(f'  ✅ {source_name}: {len(filtered)} 条 ({backtrack})')
                    else:
                        stats['empty'].append((source_name, f'有 {len(entries)} 条但均超出 {hours}h 时间窗'))
                        logging.info(f'  ⏭️  {source_name}: 有 {len(entries)} 条但超时间窗')
                elif status == 'error':
                    stats['failed'].append((source_name, reason))
                    logging.warning(f'  ❌ {source_name}: {reason}')
                else:
                    stats['empty'].append((source_name, reason))
                    logging.info(f'  ⏭️  {source_name}: {reason}')
            except Exception as exc:
                stats['failed'].append((source_name, str(exc)))
                logging.warning(f'  ❌ {source_name}: {exc}')

        for future in not_done:
            task_info = futures[future]
            source_name = task_info[3]
            future.cancel()
            reason = '采集任务超时（180s），已保留其他已完成结果'
            stats['failed'].append((source_name, reason))
            logging.warning(f'  ⏱️ {source_name}: {reason}')
                
    logging.info(f'\n[P1 Phase 1 完毕] 成功: {len(stats["success"])}/{stats["total"]} | 发现: {len(all_entries)} 篇')
    
    def _deduplicate_entries(entries: list[dict], threshold: float = 0.45) -> list[dict]:
        import re
        def _tokenize(text: str) -> set:
            eng = re.findall(r'[a-zA-Z0-9]+', text.lower())
            chn = re.findall(r'[\u4e00-\u9fa5]', text)
            return set(eng + chn)
            
        unique_entries = []
        unique_sets = []
        for entry in entries:
            is_duplicate = False
            text_a = f"{entry.get('title', '')} {entry.get('content', '')[:100]}"
            set_a = _tokenize(text_a)
            if not set_a:
                unique_entries.append(entry)
                unique_sets.append(set())
                continue
            for idx, u_set in enumerate(unique_sets):
                if not u_set:
                    continue
                intersection = len(set_a.intersection(u_set))
                union = len(set_a.union(u_set))
                jaccard = intersection / union if union > 0 else 0
                if jaccard > threshold:
                    is_duplicate = True
                    new_src = entry.get('source', '')
                    u_entry = unique_entries[idx]
                    if new_src and new_src not in u_entry.get('source', ''):
                        u_entry['source'] = f"{u_entry.get('source', '')}, {new_src}"
                    break
            if not is_duplicate:
                unique_entries.append(entry)
                unique_sets.append(set_a)
        return unique_entries
        
    if all_entries:
        original_count = len(all_entries)
        all_entries = _deduplicate_entries(all_entries)
        dedup_count = original_count - len(all_entries)
        if dedup_count > 0:
            logging.info(f'[P1 Phase 1.5 瘦身] ♻️ 基于本地词义折叠剔除了 {dedup_count} 篇高度重复新闻，剩余 {len(all_entries)} 篇进入深挖')
        else:
            logging.info(f'[P1 Phase 1.5 瘦身] 未发现高重复性新闻，维持 {len(all_entries)} 篇')
            
    enrich_ok = 0
    enrich_fail = 0
    failed_urls = []
    
    if _scrapling_available and all_entries:
        logging.info(f'\n[P1 Phase 2] Scrapling 正文深挖（{len(all_entries)} 篇 × ≤{SUMMARY_LEN}字）- 开启并发乱序模式')
        shuffled_entries = list(all_entries)
        random.shuffle(shuffled_entries)
        total_len = len(shuffled_entries)
        processed_count = 0
        
        def _process_one_entry(entry):
            return entry, _enrich_article(entry)
            
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_process_one_entry, e): e for e in shuffled_entries}
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    entry_ref, scrapled = future.result()
                    if scrapled:
                        entry_ref['content'] = scrapled
                        enrich_ok += 1
                    else:
                        enrich_fail += 1
                        failed_urls.append({'title': entry_ref.get('title', ''), 'url': entry_ref.get('url', ''), 'source': entry_ref.get('_feed_name', entry_ref.get('source', ''))})
                except Exception:
                    enrich_fail += 1
                    failed_urls.append({'title': entry.get('title', ''), 'url': entry.get('url', ''), 'source': entry.get('_feed_name', entry.get('source', ''))})
                processed_count += 1
                if processed_count % 10 == 0 or processed_count == total_len:
                    logging.info(f'  Phase 2 进度: {processed_count}/{total_len} | ✅ {enrich_ok} | ❌ {enrich_fail}')
                    
        logging.info(f'[P1 Phase 2 完毕] ✅ {enrich_ok} / ❌ {enrich_fail} ({(enrich_ok * 100 // (enrich_ok + enrich_fail)) if (enrich_ok + enrich_fail) else 0}%)')
    elif not _scrapling_available:
        logging.warning('[P1 Phase 2] Scrapling 不可用，跳过正文深挖')
        
    stats['enrich_ok'] = enrich_ok
    stats['enrich_fail'] = enrich_fail
    stats['failed_urls'] = failed_urls
    
    by_source = {}
    for e in all_entries:
        key = (e.get('_feed_name', 'unknown'), e.get('_category', '综合RSS'))
        if key not in by_source:
            by_source[key] = []
        by_source[key].append(e)
        
    for (source_name, category), entries in by_source.items():
        result = _entries_to_p1_result(entries, category, source_name, hours)
        if result:
            all_results.append(result)
            
    logging.info(f'[P1 采集完毕] 成功: {len(stats["success"])}/{stats["total"]} | 总文章: {len(all_entries)} | 正文深挖: ✅{enrich_ok}/❌{enrich_fail}')
    return all_results, stats

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    results, stats = collect_from_rss('72h')
    print(f'\n===== 采集结果: {len(results)} 个数据源 =====\n')
    total_items = 0
    for r in results:
        lines = r['text'].split('\n')
        total_items += len(lines)
        print(f"[{r['category']}] {r['query']}: {len(lines)} 条")
        for line in lines[:2]:
            print(f"  {line[:120]}")
        if len(lines) > 2:
            print(f"  ... 还有 {len(lines)-2} 条")
        print()
    print(f"总计: {total_items} 条新闻")
