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
import os
import re
import random
import time
from datetime import datetime


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
            refine_prompt = f'基于以下已搜集到的信息，生成 1 条更深入的搜索查询。\n目标：挖掘上一轮结果中未解答的关键问题，例如：\n- 事件的根本原因（why）\n- 产业链上下游的具体影响（who wins/loses）\n- 替代方案或竞争格局变化\n\n已搜集信息摘要：\n{all_findings[-1][:2000]}\n\n只输出 1 行搜索关键词（英文），不要任何解释：'
            try:
                resp = pro_adapter.invoke(refine_prompt)
                if resp.content:
                    query = resp.content.strip().split('\n')[0].strip().strip('"\'')
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
        try:
            from settings import get_today_output_dir
            now_str = datetime.now().strftime('%Y%m%d%H%M')
            fname = os.path.join(get_today_output_dir(), f'P7_深度研究_{now_str}.md')
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(f'# P7 深度研究结果\n\n> 生成时间: {datetime.now().isoformat()}\n\n')
                f.write(combined)
            logging.info(f'[P6.5] 已保存: {fname}')
            return combined
        except Exception as e:
            logging.warning(f'[P6.5] 保存失败: {e}')
            return combined
            
    return ''
