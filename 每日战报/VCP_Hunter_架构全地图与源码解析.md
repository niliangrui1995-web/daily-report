# VCP Hunter (v4.7) 核心架构与源码级作用全解

这份文档旨在梳理 `d:\vcp_hunter\每日战报\每日战报` 目录下各个模块的
关键职责及其在整个"情报 -> 映射 -> 决策"数据流中的独特价值。

---

## 一、 系统引擎心脏与脉络控制器
这是整个项目的**物理发电机**和**指挥官**，它们不产生情报，但这决定首情
报怎么跑、跑多稳。

- **`health_check.py` (全管道健康预检跳板)**
  - **价值**：大军未动，粮草先行。在 P1 起跑前的 30 秒内光速探测 GPT-5.5 XHigh、Gemini、Grok 以及 yfinance 的 API 连通性，将所有“因网络代理挂掉导致的阻断”扼杀在摇篮里，极大节省排错时间。

- **`orchestrator.py` (主编排引擎)**
  - **价值**：整个系统的心智核心。它负责串联起 P1 到 P9 的全部 9 个数据情
    清洗层。内部实现了粗暴且极具韧性的多线程并发（如将 P2 X平台、P3 机动
    搜索与 P1 RSS/网页采集三线同时发射），严格掌控每一个下游大模型的超时容忍度。

- **`scheduled_launcher.py` (Windows 定时发射器)**
  - **价值**：专为 Windows Task Scheduler 打造的穿甲弹。用绝对路径突
    破了系统底层的当前路径错乱问题。判断当天为交易日后，用 `--force-run` 
    强势唤醒 orchestrator。

- **`checkpoint.py` (断点熔断与记忆恢复)**
  - **价值**：最省钱、最省命的容灾堡垒。通过自适应扫描每天磁盘里生成
    的 Markdown / JSON，若遇断网或大模型宕机被掐断，下次重启必将从最
    后一个文件处精准接续，完美保留战果。

- **`run_config.py` (运行时参数与回溯适配器)**
  - **价值**：计算“上个交易日到此时的小时数”，提供 24h / 72h 等自适应
    回溯时效。

- **`trading_calendar.py` (智能休市传感器)**
  - **价值**：利用 `chinese-calendar` 彻底终结了人工手写节假日列表
    的痛苦。不仅分得清周末，还分得清国务院调休。自动物理熔断 API 账单。

- **`settings.py` (全局基岩配置)**
  - **价值**：包含了大模型实例（Lazy Load 懒加载）、请求缓存、代理检测
    及日志格式化配置的兵器库。

---

## 二、 战术情报雷达网络 (P1 / P2 / P3 / P4 数据采集)
这是你安插在全球产业链各个阴暗角落的窃听器，它们带回的是最泥沙俱下的金砂。

- **`p4_vanguard.py` (P4：全球核心标的客观行情)** 📈 `资金投票锚点`
  - **价值**：通过 yfinance 获取 25+ 核心硅谷巨头及关键材料商的昨夜收盘数据，
    按“超大巨头”和“中型标的”动态计算区间跌涨幅，为大模型提供最真实的资金
    博弈证据。

- **`p2_x_intel.py` (P2：X平台先遣哨兵)** 🌟 `巨头爆料专线`
  - **价值**：直连 Grok 原始接口，在全球最大的硅谷内部"八卦阵" (Twitter)
    里按预设字典抓取第一手的泄密情报。当前输出会先被整理为
    `claim / source / confidence / source_url` 结构，再送入 P6/P9。

- **`p1_rss.py` (P1：RSS/网媒并发爬网机)**  
  - **价值**：通过近三十个官方、财报源、行业媒体获取宏观及科技业公开信息。

- **`p1_web.py` (P1：外部特殊探测器)**
  - **价值**：用来辅助 RSS 爬虫获取非订阅源的数据抓取能力。

- **`p3_agent.py` (P3：Gemini 联网搜索机动部队)** 🆕 `爬虫盲区终结者`
  - **价值**：纯 AI 搜索引擎，不用任何传统爬虫。利用 Gemini 2.5 Flash 的
    Google Search Grounding 能力执行三个锦囊任务：锦囊一空袭 P1 深挖失败的高价值
    文章，锦囊二扫雷 GitHub/HackerNews 等极客圈非共识信号，锦囊三侦测全球
    10 个半导体核心产区的地方新闻异动。输出已经统一为结构化 Markdown + JSON 双文件。

- **`config.py` (搜刮器静态参数配置)**
  - **价值**：存放大盘监控清单、浏览器反爬请求头和 RSS 备用节点库。

- **`scraper_utils.py` (反反爬工具箱)**
  - **价值**：对各种难以直接抓取的 URL 提供伪装清理、绕过 Cloudflare 
    或请求重试的黑科技。

---

## 三、 绝对真理与客观降噪枢纽 (风控风向标)
这是连接"情报界"和"交易界"的多功能变压器。

- **`industry_dict.py` (中央神经词典与真理源)** 🏆 `系统的潜意识`
  - **全域映射**: 向 P2 提供 X 平台扒取清单，向 P5 审计提供黑白过滤
    名单，向 P6 提供全球核心标的客观测温名单，并内置涨幅分类器给巨头加戏。

- **`search_engine.py` (实时搜商补丁)**
  - **价值**：当大模型在审计或深研时对自己知识库的数据不自信或模糊时，
    通过向 Google 反向查证，避免一切模型幻觉 (`Hallucination`)。

---

## 四、 大语言模型意识流 (P5 / P6 / P7 / P7a / P8 / P7.5 / P9)
这是真实的"硅基大脑阵列"，由轻量级的门神（Gemini）和深度的智库（GPT-5.5 XHigh）接力。

- **`native_llm_adapter.py` (双模型通信协议栈)**
  - **价值**：封装并统一了 Claude (Anthropic) 和 Gemini (Google) 那差
    异巨大的底层 API 调用规范，对外暴露一套丝滑的 `invoke` 方法。

- **`audit_engine.py` (P5/P7a/P7.5：洗稿门神 - Gemini Pro)**
  - **价值**：超廉价但智商够用。它像个安检员，把刚从 RSS 抓来的上万字泥沙
    进行去重和精炼降噪。**v4.6 升级点**：新增 P7a 深研审计层，将 P7 产生的大量（超 40K 字）冗余溯源数据，强制压缩为 10K 内的双轨（JSON+MD）精华研报，有效防止 GPT-5.5 XHigh 在 P9 生成战报时产生"Token 稀释注意力"的问题。三层审计均支持结构化 JSON 和 Markdown 可读纯文本的双轨落盘。

- **`p6_mapper.py` (P6：A股映射大脑 - GPT-5.5 XHigh)**
  - **价值**：核心发力点。GPT-5.5 XHigh 的 reasoning summary 会在这里记录昨夜的科技巨头消息
    和客观股价捏合并严谨映射到咱们大 A 的可炒作题材上。当前已与 `P7` 共用最近
    5 天主题去重护栏，不再各自维护一套历史主题扫描逻辑。

- **`p7_deep_research.py` (P7：深度研究大脑 - Gemini Flash + Pro)**
  - **价值**：接续 P6 输出的深研指令，按主题进行 3 轮自动衍生和补网搜索，
    最终生成带关联溯源的深度研报，解决快餐信息的浅薄。当前已升级为统一研究计划
    中枢，负责 P7/P7.5 触发判断、近 5 天主题去重、自适应轮数和停止条件。

- **`p8_technical.py` (P8：本地技术面装配层)**
  - **价值**：基于本地通达信 `.day` 数据，为 P6 标的补齐 250 日位置、均线、RS、
    布林、ATR、周线趋势等客观技术特征。当前已拆分为 `p8_extractors.py /
    p8_indicators.py / p8_storage.py / p8_technical.py` 四块。

- **`p9_reporter.py` (P9：终极文胆与分发者)**
  - **价值**：统帅全案，把 P6 产业链因果和 P8 诊断收拢。当前已拆分为
    `p9_rendering.py / p9_email.py / p9_reporter.py`，其中 `p9_reporter.py`
    只负责 LLM 调用、JSON 解析、产物落盘和邮件调度。

---

> **系统性哲学**：VCP Hunter 的本质，就是用工程学去锁死大模型的随机性，
> 用物理级强并行的抓取去解决数据的时间差，最后将判断的权力交给最强模型。
> 你修改 `industry_dict.py` 等字典模块，就是在撬动机器大军攻向新高地。
## 2026-04-12 架构同步说明

这份架构文档按当前代码现状补充 4 项优化的实际进度：

1. `P2/P3 -> P6/P9` 的“结构化 claim + source + confidence”门禁已落地。
   - `intel_structuring.py` 负责将 P2 的 `<x_leaks>` 和 P3 三锦囊结果归一为结构化条目。
   - `p2_x_intel.py` / `p3_agent.py` 现在都会产出结构化 Markdown，并同步落盘 `.json` 供下游追溯。

2. `P6/P7/P7.5` 的规划与去重逻辑已统一。
   - `p7_deep_research.py` 对外提供共享的最近 5 天主题护栏。
   - `p6_mapper.py` 不再私有维护另一套历史主题扫描逻辑。

3. `P1/P3/P4/P7.5` 真实收益指标已落地。
   - `contribution_metrics.py` 会输出 `Layer_ROI_*.json/.md`。
   - `orchestrator.py` 在 P9 结束后自动补跑 ROI 复盘，并把 `roi_score / roi_recommendation` 回填到 `run_manifest.json`。

4. `P8/P9` 模块拆分已完成。
   - `P8` 拆为 `p8_extractors.py / p8_indicators.py / p8_storage.py / p8_technical.py`
   - `P9` 拆为 `p9_rendering.py / p9_email.py / p9_reporter.py`

### 新增模块

- `contribution_metrics.py`
  - 负责抽取 `P1/P3/P4/P7.5` 的有效信息单元
  - 计算跨层独有贡献、P6/P9 采纳率、单位成本
  - 输出 `Layer_ROI_*.json` 与 `Layer_ROI_*.md`

- `intel_structuring.py`
  - 将 P2/P3 结果归一到 `claim / source / confidence / source_url`
  - 让 P6/P9 吃到结构化情报而不是松散自由文本

- `p8_extractors.py / p8_indicators.py / p8_storage.py`
  - 将 P8 的通达信读取、技术指标计算、双轨落盘拆开
  - `p8_technical.py` 只保留装配与业务编排

- `p9_rendering.py / p9_email.py`
  - 将 P9 的 Markdown 渲染和邮件构建/发送从主流程中拆出
  - `p9_reporter.py` 只保留 LLM 调用、JSON 解析、产物落盘与调度

### 新增运行产物

- `Layer_ROI_*.json`
- `Layer_ROI_*.md`
- `run_manifest.json` 中的每层 ROI 指标字段
