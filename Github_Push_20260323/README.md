# VCP Hunter — GitHub 云端搜索

> 临时方案：在 GitHub 上运行 Gemini 搜索（直连 Google API），DNS 生效后恢复本地全量运行。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行 L1 全量搜索（默认 24h）
python github_search.py

# 3. 周一模式（72h 回溯）
python github_search.py --backtrack 72h
```

## 输出

运行后在 `search_output/` 目录下生成：

| 文件 | 说明 |
|------|------|
| `L1_results_YYYYMMDDHHMM.json` | L1 全量搜索结果 |
| `L2.5_deep_research_*.json` | L2.5 深度研究（需手动指定主题） |
| `L3a_supplementary_*.json` | L3a 补充搜索（需手动指定查询） |

## 本地使用流程

1. 在 GitHub 上运行 `github_search.py` 获取搜索结果
2. 下载 `search_output/` 目录到本地 `每日热点输出/YYYYMMDD/` 下
3. 在本地运行 `daily_report.py --search-cache 每日热点输出/YYYYMMDD/L1_results_*.json`

## L2.5 深度研究 & L3a 补充搜索

当本地 L2 Opus 完成后，如果输出中包含以下指令：

```
DEEP_RESEARCH: 某个深研主题
SEARCH: 某个补充搜索查询
```

可以将这些指令复制出来，在 GitHub 上二次运行：

```bash
python github_search.py \
  --deep-topics "HBM4 产能瓶颈与 SK Hynix 扩产时间表" \
  --extra-queries "TSMC CoWoS 2026 Q2 产已利用率" "Broadcom VMware 整合进展"
```

然后下载结果补充到本地。
