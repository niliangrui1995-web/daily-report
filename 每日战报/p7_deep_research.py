"""
P7 深度研究层 — Opus 指定主题的多轮迭代搜索。

从 p6_mapper.py 拆分，原属"上帝模块"的一部分。

【⚠️核心维护规则 / Agent 指令】
1. `run_deep_research()` 的主题去重机制（`_get_recent_deep_topics` 扫描
   近 5 天已研究主题）是防止 P7 反复嚼旧馍的关键，不得删除或缩短去重窗口。
2. `iterative_deep_search()` 的多轮搜索中，每轮之间有 `time.sleep(2-5s)`
   随机间隔，这是防 API 限流的必要措施。
"""

import logging
import re
import random
import time
from datetime import datetime

_INVALID_REFINE_QUERIES = {
    'initiating the analysis',
    'initiating inquiry process',
    'initiating query generation',
    'analysis',
    'query generation',
    'search query',
}


def _pick_refined_query(raw_text: str, fallback: str) -> tuple[str, bool]:
    """从模型返回中提取可用查询词；无效则回退到原主题。"""
    if not raw_text or not raw_text.strip():
        return fallback, True

    lines = []
    for line in raw_text.splitlines():
        t = line.strip()
        if not t:
            continue
        if t.startswith('```') or t.startswith('#'):
            continue
        lines.append(t)

    if not lines:
        return fallback, True

    query = lines[0].strip().strip('"\'` ')
    query = re.sub(r'^\*+|\*+$', '', query).strip()
    query = re.sub(r'^[\-•\d\.\)\s]+', '', query).strip()
    query = re.sub(r'\s+', ' ', query)

    if len(query) < 8:
        return fallback, True

    q_lower = query.lower()
    if q_lower in _INVALID_REFINE_QUERIES:
        return fallback, True
    if re.match(r'^(initiating|starting|analysis|query generation)', q_lower):
        return fallback, True
    if not re.search(r'[A-Za-z0-9\u4e00-\u9fa5]', query):
        return fallback, True

    if len(query) > 220:
        query = query[:220].rstrip()

    return query, False


def pick_deep_topics(p6_mapping: str) -> list[str]:
    topics = re.findall(r'DEEP_RESEARCH:\s*(.+)', p6_mapping)
    topics = [t.strip() for t in topics if len(t.strip()) > 10][:3]
    if topics:
        logging.info(f'[P6.5 选题] Opus 指定 {len(topics)} 个深研主题:')
        for i, t in enumerate(topics):
            logging.info(f'  [{i + 1}] {t}')
        return topics
    logging.info('[P6.5 选题] Opus 未指定深研主题，跳过')
    return topics


def iterative_deep_search(topic: str, rounds: int = 3) -> str:
    from settings import pro_adapter
    from search_engine import gemini_search
    
    init_delay = random.uniform(2.0, 10.0)
    logging.info(f"[P6.5 深研起跑线] 主题 '{topic}' 触发错峰发车延时 {init_delay:.1f} 秒...")
    time.sleep(init_delay)
    
    logging.info(f"[P6.5 深研] 启动 '{topic}' 多轮迭代搜索 ({rounds} 轮)...")
    
    all_findings = []
    
    for r in range(rounds):
        if r == 0:
            query = topic
        else:
            now_str = datetime.now().strftime('%Y年%m月%d日 北京时间%H:%M')
            refine_prompt = f'⚠️ 当前北京时间：{now_str}\n\n基于以下已搜集到的信息，生成 1 条更深入的搜索查询。\n目标：挖掘上一轮结果中未解答的关键问题，例如：\n- 事件的根本原因（why）\n- 产业链上下游的具体影响（who wins/loses）\n- 替代方案或竞争格局变化\n\n已搜集信息摘要：\n{all_findings[-1][:2000]}\n\n只输出 1 行搜索关键词（英文），不要任何解释：'
            try:
                resp = pro_adapter.invoke(refine_prompt)
                if resp.content:
                    query, used_fallback = _pick_refined_query(resp.content, topic)
                    if used_fallback:
                        preview = resp.content.strip().split('\n')[0][:80] if resp.content else ''
                        logging.warning(f"[P6.5 深研] R{r + 1} 细化查询无效（{preview}），回退原主题")
                else:
                    query = topic
            except Exception as e:
                logging.warning(f'[P6.5 深研] R{r + 1} 细化失败，复用原主题: {e}')
                query = topic
                
        logging.info(f'  [R{r + 1}/{rounds}] {query}')
        time.sleep(random.uniform(2.0, 5.0))
        
        result = gemini_search(f'深研R{r + 1}', query, backtrack='72h')
        if result and result.get('text'):
            all_findings.append(result['text'])
            logging.info(f"  [R{r + 1}] 获取 {len(result['text'])} 字")
        else:
            logging.warning(f"  [R{r + 1}] 搜索失败")
            
    if not all_findings:
        return ''

    combined = '\n\n'.join([f'### 深度研究 第{i + 1}轮\n{f}' for i, f in enumerate(all_findings)])
    logging.info(f"[P6.5 深研] '{topic}' 完成: {len(combined)} 字 ({len(all_findings)}/{rounds} 轮成功)")
    return combined


def run_deep_research(p6_mapping: str) -> str:
    topics = pick_deep_topics(p6_mapping)
    if not topics:
        return ''
        
    import concurrent.futures
    all_research = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {topic: pool.submit(iterative_deep_search, topic, 3) for topic in topics}
        for topic in topics:
            try:
                result = futures[topic].result()
                if result:
                    all_research.append(f'## 深度研究：{topic}\n\n{result}')
            except Exception as e:
                logging.warning(f"[P6.5 深研] 独立线程深研异常 '{topic}': {e}")
                
    if all_research:
        combined = '\n\n' + '\n\n'.join(all_research)
        logging.info(f'[P6.5] 深度研究完成: {len(topics)} 主题, {len(combined)} 字')
        return combined

    return ''
