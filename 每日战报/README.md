# VCP Hunter — 北美 AI 产业链情报战报系统 v4.3

> 全自动化的 AI 产业链情报采集、审计、映射和战报生成系统。
> 每个交易日早盘前 09:00 自动运行，将海外产业链动态映射为 A 股投资信号。

---

## ✅ 2026-04-12 同步状态

当前代码与文档已经同步到以下状态：

- `P2/P3` 不再以松散文本直通下游，而是先整理成 `claim + source + confidence` 结构，再进入 `P6/P9`
- `P6/P7/P7.5` 已共用同一套近 5 天主题去重与研究规划逻辑，避免双份策略漂移
- `P1/P3/P4/P7.5` 已接入真实收益评估，主流程结束后会生成 `Layer_ROI_*.json/.md`
- `P8/P9` 已完成模块拆分，`p8_technical.py` / `p9_reporter.py` 只保留主流程装配职责

---

## 📐 系统架构

```text
P1 Phase 1 - RSS+网页发现 (feedparser + Scrapling Parser)
    ↓ 25+ RSS/新闻源并发抓取 + Reddit + HackerNews + 供应链数据源
    ↓ URL 黑名单过滤非文章页面
P1 Phase 2 - Scrapling 正文深挖
    ↓ 逐篇访问文章 URL，定制 CSS 选择器提取正文摘要（≤5000字）
    ↓ 失败时自动回退到 RSS 摘要 + meta description
    ↓ 失败链接收集进「阵亡名单」→ 交给 P3 锦囊一破防
P2 X平台情报 (Grok API) ──────────────── 与 P1 并行启动
    ↓ Grok 原生 X(Twitter) 实时扫描，15 赛道关键词矩阵
    ↓ 输出先整理为 Claim / Source / Confidence 结构化情报，再直送 P6 + P9
P3 机动搜索 (Gemini 2.5 Flash Search) ── 与 P1 并行启动
    ↓ 锦囊一：动态阵亡名单空袭（P1 深挖失败的高价值链接二次补缺）
    ↓ 锦囊二：极客圈非共识扫雷（GitHub/HackerNews/独立博客/VC公告）
    ↓ 锦囊三：产业链异动地震仪（全球 10 个核心半导体产区地方新闻）
    ↓ 输出先整理为 Claim / Source / Confidence 结构化情报，再拼入 P6 证据链 + P9 独立层
P4 海外盘面 (yfinance) ──────────────── 与 P1 并行启动
    ↓ 美股昨夜收盘行情 → 客观资金投票验证锚点
P5 审计清洗 (Gemini 3.1 Pro High)
    ↓ 时效验证 + 可信度打分 + 产业链关联度筛选 + 去主观噪声
P6 A 股映射 (Claude Opus 4.6 Thinking)
    ↓ 产业链事件 → A 股标的 + 弹性评级
    ├── 统一研究计划（与 P7 共用最近 5 天主题去重）
    ├── P7 深度研究（按触发条件自适应 1-3 主题 × 1-3 轮搜索） ─┐
    └── P7.5 补充搜索（Opus 下达 SEARCH 密令） ─────────────────┴→ 统一规划后执行
        ↓ (按顺序串行启动严格的脱水盲审)
        ├── P7a 深度研究审计 (Gemini 3.1 Pro High) ── 将 40K P7源数据去重提取，JSON+MD双轨落盘
        └── P7.5 补充审计 (Gemini 3.1 Pro High) ──    对 P7.5生肉盲审排雷，JSON+MD双轨落盘
P8 股价分析 (本地通达信 K 线数据) ── 承接前线搜索和清洗完毕后安全执行
    ↓ 提取 P6 映射标的，获取 250 日 K 线 + 14 项技术指标 + 财务数据
P9 终极战报 (Claude Opus 4.6 Thinking)
    ↓ 九层数据综合 → 标的狙击表 + 操作建议
    ↓ (P5 + P2 + P3 + P4 + P6 + P7a + P7.5 + P8 股价 + 海外行情)
Layer ROI 贡献复盘
    ↓ 对 P1 / P3 / P4 / P7.5 做独有贡献、采纳率、单位成本复盘
📧 邮件推送
```

---

## 📁 项目结构

```text
每日战报/
├── .venv/                       # Python 虚拟环境
├── test_channels.py             # 🔌 双通道连通性测试脚本
├── 每日战报/                    # 核心代码
│   ├── orchestrator.py          # 🚀 主入口：全流程调度器（纯编排逻辑）
│   ├── scheduled_launcher.py    # ⏰ 定时任务启动器（交易日判断 + 管道启动）
│   ├── config.py                # 📡 数据源配置（RSS/CSS选择器/黑名单/常量）
│   ├── settings.py              # 配置中心（.env 加载 + 适配器单例）
│   ├── search_engine.py         # 搜索引擎（Key 轮换 + 熔断器 + 三级降级）
│   ├── scraper_utils.py         # Scrapling 公共工具（正文深挖 + URL 过滤）
│   ├── audit_engine.py          # P5/P7a/P7.5 Pro 审计
│   ├── p1_rss.py                # P1 Phase1+2：RSS 发现 + Scrapling 正文深挖
│   ├── p1_web.py                # P1 网页列表页发现 + 正文深挖
│   ├── p2_x_intel.py            # 📱 P2 X(Twitter) 产业链情报采集（Grok API）
│   ├── p3_agent.py              # 🚀 P3 机动搜索（Gemini 联网三锦囊 + 结构化输出）
│   ├── p4_vanguard.py           # 📈 P4 海外产业链收盘行情（yfinance）
│   ├── p6_mapper.py             # P6 Opus A 股纯映射引擎
│   ├── p7_deep_research.py      # P7/P7.5 统一研究计划 + 深度扩展研究
│   ├── run_config.py            # 运行时环境配置与时效控制
│   ├── intel_structuring.py     # P2/P3 结构化 claim/source/confidence 归一化
│   ├── contribution_metrics.py  # P1/P3/P4/P7.5 真实收益评估与 Layer ROI 输出
│   ├── p8_extractors.py         # P8 标的提取、通达信目录探测与 .day 读取
│   ├── p8_indicators.py         # P8 技术指标计算
│   ├── p8_storage.py            # P8 双轨产物落盘
│   ├── p8_technical.py          # P8 技术面主流程装配
│   ├── p9_rendering.py          # P9 Markdown 战报渲染
│   ├── p9_email.py              # P9 邮件 HTML 构建与 SMTP 发送
│   ├── p9_reporter.py           # P9 战报生成主流程装配
│   ├── checkpoint.py            # 断点自适应检测（manifest + 目录扫描双保险）
│   ├── trading_calendar.py      # A 股交易日历（节假日 + 调休）
│   ├── industry_dict.py         # 产业链 15 赛道字典 + 海外标的池
│   ├── native_llm_adapter.py    # 多模型适配器（Claude/Gemini/OpenAI 统一接口）
│   ├── .env                     # 🔒 密钥配置（不入库）
│   ├── .env.example             # 密钥配置模板
│   ├── requirements.txt         # Python 依赖清单
│   ├── prompts/                 # 🧠 提示词外置库（按层级解耦）
│   │   ├── p2_system.txt        # P2 System Prompt
│   │   ├── p2_user.txt          # P2 User Prompt
│   │   ├── p3_jinnang1.txt      # P3 锦囊一：动态阵亡名单空袭
│   │   ├── p3_jinnang2.txt      # P3 锦囊二：极客圈非共识扫雷
│   │   ├── p3_jinnang3.txt      # P3 锦囊三：产业链异动地震仪
│   │   ├── p6_mapping.txt       # P6 A股映射逻辑
│   │   ├── p7a_audit.txt        # P7a 深度研究提取压缩审计
│   │   ├── audit.txt            # P5/P7.5 常规脱水盲审
│   │   ├── p9_report.txt        # P9 最终战报模板
│   │   └── ...                  # 其它各阶段提示词
│   └── tests/                   # 单元测试（当前 67 个离线测试用例）
│       ├── test_core.py         # 核心逻辑测试
│       ├── test_p7_workflow.py  # P7 研究计划 / 自适应停止 / 时间窗
│       ├── test_contribution_metrics.py # Layer ROI 贡献复盘
│       └── test_workflow_refactors.py   # P2/P3 结构化 + P9 渲染 + 共享去重
├── 实时看板_v2/                 # 实时看板（独立子系统，不互引）
├── 每日热点输出/                # 每日战报输出目录
│   └── {YYYYMMDD}/             # 按日期分目录
└── README.md                    # 本文件
```

---

## 🚀 快速开始

### 安装依赖

```bash
cd 每日战报
pip install -r requirements.txt
```

### 配置密钥

复制 `.env.example` 为 `.env`，填入真实的 API Key：

```bash
cp 每日战报/.env.example 每日战报/.env
# 编辑 .env 填入 API Key
```

### 手动运行

```bash
# ✅ 推荐入口（模块化版本，自动断点续跑）
python 每日战报/orchestrator.py

# 非交易日执行（保留断点）
python 每日战报/orchestrator.py --force-run

# 强制从 P1 清除断点重跑
python 每日战报/orchestrator.py --force-restart

# 非交易日 + 清除断点重跑（等价 --force）
python 每日战报/orchestrator.py --force

# 跳过 P1/P5，从最新 P5 文件直接进 P6
python 每日战报/orchestrator.py --skip-to-l2
```

### 连通性测试

```bash
# 测试直连 Google + CF Worker 跳板双通道
python test_channels.py
```

### 定时自动运行

系统已配置 Windows 计划任务 `VCPHunter_DailyReport`：

- **触发时间**：每周一至周五 09:00
- **入口文件**：`scheduled_launcher.py` → `orchestrator.py`
- **交易日判断**：自动排除法定节假日，非交易日秒退跳过
- **自适应回溯**：自动计算上一交易日 09:00 到当前的小时数，节假日后自动扩大采集窗口

| 场景 | 上一交易日 | 自动回溯 |
| --- | --- | --- |
| 周二 09:00 | 周一 | 24h |
| 周一 09:00 | 上周五 | 72h |
| 国庆后 10/8 09:00 | 9/30 | 192h |
| 春节后 2/2 09:00 | 1/23 | 240h（上限） |

> 🤖 **智能日历**：系统已接入 `chinesecalendar` 库，自动获取国务院放假调休安排，彻底免除每年人工修改节假日的维护成本。

### 运行测试

```bash
cd 每日战报
python -m pytest tests/ -v
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--force-run` | 跳过交易日检测（非交易日也执行） |
| `--force-restart` | 清除断点状态，从 P1 从头重跑 |
| `--force` | 等价于 `--force-run` + `--force-restart` |
| `--skip-to-l2` | 跳过 P1/P5，从最新 P5 文件直接进 P6 |
| `--search-cache <file>` | 从指定 JSON 文件读取 P1 数据 |

---

## ⚙️ 配置

### 环境变量（.env 文件）

| 变量名 | 说明 |
| --- | --- |
| `ANTIGRAVITY_BASE_URL` | 大模型反代地址（Pro/Opus） |
| `ANTIGRAVITY_API_KEY` | 反代 API Key |
| `GEMINI_SEARCH_API_KEYS` | Gemini 搜索 Key 池（逗号分隔，当前 9 个） |
| `GEMINI_SEARCH_BASE_URL` | CF Worker 跳板地址 |
| `TAVILY_API_KEY` | Tavily 备用搜索 Key |
| `GROK_API_KEY` | xAI Grok API Key（P2 X平台情报采集） |
| `EMAIL_ADDRESS` | 发件邮箱 |
| `EMAIL_PASSWORD` | 邮箱授权码 |
| `RECEIVER_EMAIL` | 收件人（逗号分隔多人） |

---

## 📱 P2 X平台情报采集（Grok）

### 设计理念

Grok 是目前唯一天生拥有 X(Twitter) 实时数据访问能力的大模型。P2 利用这一独家优势，在 X 平台上定向扫描 AI 产业链的爆料、insider leak 和供应链异动。

### 工作方式

- **与 P1 完全并行**：P2 在后台独立运行，不阻塞 P1 RSS 采集和 P5 审计
- **不经审计**：X 平台信息的价值在于即时性和爆料性，审计会损失这些独有优势
- **双重注入**：P2 输出同时注入 P6（拼接到 evidence）和 P9（作为独立第 6 层）
- **交叉验证**：P9 Opus 被指令在生成战报时对 X 平台信息进行交叉验证

### 扫描维度

覆盖 15 赛道关键词矩阵 + 超级巨头特别监控，重点关注：
1. 🔴 产能/良率/订单变化
2. 🔴 未公开的产品规格或路线图泄露
3. 🟡 供应链价格异动
4. 🟡 大厂资本开支计划变更
5. 🟢 新技术突破或合作关系变动

### 连接方式

- 走本机翻墙代理（与 Gemini Search 共享端口自动探测）
- xAI 原生 REST API（直连 xAI 服务器）
- 端点：`https://api.x.ai/v1/chat/completions`
- 模型：`grok-4-1-fast-non-reasoning`

---

## 🔀 搜索三级降级 + 熔断器

- **熔断器**：连续 ConnectTimeout 2 次后自动跳过该通道
- **搜索增强**：搜索结果按域名优先级分级深挖（一手源完整正文 + 普通源 meta 摘要，最多 12 个 URL）
- **域名优先级**：25 个高价值一手信源（sec.gov/nvidia.com/reuters.com 等）优先完整深挖
- **Key 轮换**：9 个 Gemini API Key 线程安全轮转，429/403 自动切换

---

## 🛡️ 风控策略

| 措施 | 说明 |
| --- | --- |
| 随机延迟 | 搜索间隔 1~6 秒随机抖动 |
| 阶段冷却 | P1→P5→P6→P9 之间强制暂停 5~15 秒 |
| 统一重试 | 任何错误（401/403/429/超时/其他）均等待 5~10 秒后重试，最多 5 次 |
| 反代多账号 | 反代自动切换后端账号，重试即可命中可用账号 |
| 并发控制 | yfinance 8 线程 / 搜索 9 线程 / P1 采集 8 线程 / P2+P3 独立 2 线程 |

---

## 🔄 并发执行模型

```text
全新执行阶段（P1 + P2 + P3 三线并行）:
├── ThreadPool(2): 侧翼情报池
│   ├── 线程A: P2 Grok X平台扫描
│   └── 线程B: P3 锦囊二三并行（极客扫雷 + 产业链异动）
└── ThreadPool(2): P1 双引擎
    ├── 线程A: RSS 采集 (feedparser)
    └── 线程B: Web 采集 (Scrapling)
↓ P1 完成 → P3 锦囊一（阵亡名单空袭）→ P5 审计 → 收割 P2/P3
↓ P5 + P2 + P3 拼接 → 送入 P6

P6 完成后（P7 + P7.5 双轨并跑）:
ThreadPoolExecutor(max_workers=2)
├── 线程A: 深度研究 (run_deep_research)
│   └── 串行：每个主题 3 轮，每轮调 gemini_search
└── 主线程: 补充搜索发射 (P7.5补充)
    └── 内建并发池：针对 Opus 密令并发补充搜索

等上述原石数据（P7 与 P7.5 搜索）全部采集组装后，进入串行安全清洗：
1. P7.5 审计: 对刚带回来的补充搜索原始生肉脱水 (JSON+MD 双轨落盘)
2. P7a 审计: 对 4 万字深研精华强力压榨提取 (JSON+MD 双轨落盘)
3. P8 股价提取: 基于 P6 的标的获取纯本地技术面
4. P9 终极战报: 总领全局撰写战役简报
```

---

## 📊 输出文件

所有输出保存在 `每日热点输出/{YYYYMMDD}/` 目录：

| 文件 | 内容 |
| --- | --- |
| `P1_RSS原始数据_*.json` | P1 原始 JSON（供断点续跑） |
| `P1_RSS原始数据_*.md` | P1 可读 Markdown + 采集统计 |
| `P2_X平台情报_*.md` | P2 Grok X平台采集（未审计） |
| `P3_机动情报_*.md` | P3 Gemini 联网搜索三锦囊（未审计） |
| `P3_机动情报_*.json` | P3 结构化原始数据（断点恢复用） |
| `P5_审计_*.md` | Pro 审计后的高可信情报 |
| `P6_映射_*.md` | Opus A 股映射 + 指令 |
| `P7_深度研究_*.md` | 1-3 主题深研原始结果 (保留以备查) |
| `P7a_深研审计_*.md` | P7a 跨轮去重+过滤后的精炼深研 |
| `P7a_深研审计_*.json`| P7a 提取的结构化 JSON 数据 |
| `P8_技术面_*.md` | 通达信 K 线 + 技术指标 + 财务 |
| `P7.5_补充搜索原始数据_*.md` | Opus 补充搜索抓取的生肉文本 |
| `P7.5_补充审计_*.md` | 补充搜索提取清洗后的核心报告 |
| `P7.5_补充审计_*.json`| 补充搜索的结构化 JSON 数据 |
| `战报_*.md` | 最终战报（邮件同步推送） |

---

## 🔄 断点续跑机制

系统自动扫描当天输出目录，从最新完成的层级恢复：

- 检测到 `战报_*.md` → 全流程完成，跳过
- 检测到 `P6_映射_*.md` + `P7_*` 但无战报 → P8 崩溃恢复，复用深研数据 + P2/P3 数据
- 检测到 `P6_映射_*.md` → 从 P7/P8 继续（复用 P2/P3）
- 检测到 `P5_审计_*.md` → 从 P6 继续
- P2 / P3 / P7 / P7.5 数据磁盘兜底：即使内存丢失也能从文件恢复

---

## 📝 技术栈

| 组件 | 技术 |
| --- | --- |
| P1 采集 | feedparser + requests + Scrapling |
| P2 采集 | Grok API (xAI) + X(Twitter) 实时数据 |
| P3 采集 | Gemini 2.5 Flash (Search Grounding) 联网三锦囊 |
| P4 行情 | yfinance 美股隔夜行情 |
| P5/P7.5 审计 | Gemini 3.1 Pro High（反代） |
| P6/P9 分析 | Claude Opus 4.6 Thinking（反代） |
| P7 深研 | Gemini 2.5 Flash（Search Grounding）+ Pro（导航） |
| P8 股价 | 本地通达信 K 线 + vcp.engine 财务 |
| 数据模型 | Pydantic v2 |
| 单元测试 | pytest（36 个离线测试） |
| 搜索反代 | Cloudflare Worker |
| LLM 适配器 | NativeLLMAdapter（Anthropic/Gemini/OpenAI 统一接口） |
| 定时调度 | Windows Task Scheduler（周一~五 09:00 自动触发） |

---

## 📡 监听信息源列表 (持续更新)

目前系统并发采集全球 **25+** 个精选高质量技术、财经与供应链独立前端源（v4.2 瘦身移除 6 个低效源），覆盖以下大类：

### 1. 核心科技与财经媒体 (RSS)

- Bloomberg Tech, CNBC Tech, TechCrunch, TechPowerUp, Tom's Hardware, The Register AI, EE Times, DatacenterKnowledge

### 2. 深度分析与行业机构 (RSS & 网页采集)

- **分析库**: SemiEngineering, SeekingAlpha Tech（SemiAnalysis 已移交 P3 锦囊一动态破防）
- **机构直采**: SEMI (国际半导体产业协会), TrendForce (集邦咨询), ASML 官方新闻

### 3. 全球核心供应链区域媒体

- **台湾地区**: TechNews (台湾科技新报), DIGITIMES (电子时报前端直采), DigiTimes Asia
- **韩国地区**: TheElec KR
- **垂直细分**: Lightwave (光通信), FierceTelecom, FierceElectronics

### 4. 离岸供应链与大宗商品情报

- **深水区大宗专网**: Metal.com (上海有色网 SMM 国际离岸英文数据专线，包含前沿稀土与有色金属供给异动)

### 5. 巨头官方动态与技术博客

- **SEC 8-K 披露文件**: 已移交 P3 锦囊二/三自然覆盖（v4.2 瘦身：SEC RSS 批处理推送机制导致连续多日超时间窗空转）
- **技术布道**: AWS ML Blog, ServeTheHome (硬件服务器微架构)（NVIDIA Blog 已移交 P3 锦囊二自然覆盖）

### 6. 极客与开发者论坛

- **Reddit 核心讨论版**: r/hardware, r/nvidia, r/amd
- **HackerNews**: Top 50 最新科技趋势

### 7. X(Twitter) 产业链 Insider（P2）

- **Grok 实时扫描**: 通过 xAI Grok API 原生访问 X 平台数据
- **重点监控账号**: @kopite7kimi, @dylan522p (SemiAnalysis), @harukaze5719, @VideoCardz 等产业链 leaker
- **扫描范围**: 15 赛道关键词矩阵 × 过去 N 小时内的新帖（自适应回溯窗口）

---

## 🏭 产业链 15 大核心赛道与标的池

VCP Hunter 将复杂的 AI 与半导体生态抽象为 15 个核心网格（即 A 股映射时的终极归宿目录）。当任何海外信息流被判定具备"可投资性"时，大模型必须将其强力映射至以下某一赛道：

1. **芯片设计与底层IP**: Synopsys, Cadence, ARM, Qualcomm, Alphawave...
2. **定制化ASIC与代工**: TSMC, Broadcom, Marvell, Alchip (世芯)...
3. **晶圆制造与材料设备**: ASML, Applied Materials, KLA, Lam Research, TEL, Onto Innovation, Soitec...
4. **HBM与核心存储矩阵**: SK Hynix, Samsung Memory, Micron, Hanmi...
5. **先进封装与混合键合**: Besi, EV Group, ASMPT, TSMC, ASE (日月光), Amkor...
6. **核心基板与封装材料**: Ibiden (揖斐电), Unimicron (欣兴), Ajinomoto (味之素)...
7. **网络交换与高速电互连**: Broadcom, NVIDIA, Astera Labs, Credo, Amphenol...
8. **光模块/硅光/CPO**: Coherent (高意), Lumentum, II-VI, Sumitomo Electric (住友电工)...
9. **高频PCB与覆铜板材料**: Rogers, Panasonic MEGTRON, EMC (台光电), Wus (华通)...
10. **AI服务器基建与温控**: nVent, Vertiv (维谛), Nidec (日本电产), Schneider (施耐德)...
11. **测试/老化与探针卡**: Advantest, Teradyne, KYEC, FormFactor, Technoprobe...
12. **算力颠覆与主权生态**: Groq, Cerebras, Tenstorrent, SMIC (中芯国际), Huawei...
13. **云巨头/算力租赁与数据**: Microsoft, Meta, Google, AWS, OpenAI, CoreWeave...
14. **边缘AI与具身智能**: Apple, Qualcomm, MediaTek, Tesla, Figure AI...
15. **光纤光缆与通信基建**: Corning Optical (康宁), Prysmian, CommScope, Ciena...

> **万亿级"超级巨头"特别监控名单（极其敏感的信号阈值）**：  
> `NVIDIA` `Broadcom` `TSMC` `ASML` `Apple` `Microsoft` `Meta` `Google` `Amazon` `Tesla`

---

## 🔄 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v4.5 | 2026-04-04 | 🛡️ **P8 纯概念股性能极速提升 + 全域前置健康检查**：新增 `health_check.py` 模块，在每次 P1 采集前并发探测海外端所有模型及代理服务可用性，直接把因为抽风导致跑到一半失败的愚蠢错误率降为 0！P8 通达信日线聚合模块实现“高低弹性物理隔离”，一旦判定为纯概念/低弹性股，直接**短路跳过**耗时的 MACD 及周线计算流程，每日运行时间大幅度压缩。 |
| v4.4 | 2026-04-04 | 🛠️ **God Module 彻底陨落与解耦**：彻底重构原本拥挤杂乱的 `p6_mapper.py` 上帝模块，将其强行大卸四块：拆分出独立的 `p4_vanguard.py`（专职海外金融行情）、`p7_deep_research.py`（专职深研指令派发）以及 `run_config.py`；并在 `orchestrator.py` 中更新了底层拓扑图。现在的项目结构轻盈、职责边界极其严密。精简行业词典赛道从 17→15，剔除A股ticker。 |
| v4.3 | 2026-04-03 | 🏗️ **全系统命名规范化 (P1→P9) 及降噪升级**：将混乱的 L1... 命名体系重构为线性的 P1→P9 代号流，消除认知负担；新增 P4 海外行情独立数据层；**算法升级**：引入 P8 高低弹性物理隔离策略（中低弹性伴随股极致压缩为单行），并在 P9 渲染前深度脱水清洗 P5/P6/P7 废料日志，将最终喂给大模型的“纯净信噪比”拉满；**代码净化**：实施全域静态审计（flake8），彻底清除所有死代码、废弃导入及游离变量，修复潜伏语法错误，保障系统在自动发车流中的生产级稳定性。 |
| v4.2 | 2026-04-03 | 🚀 **P3 机动搜索引擎**：新增 `p3_agent.py` + 三锦囊 Prompt，Gemini 2.5 联网搜索覆盖传统爬虫盲区；P1 瘦身移除 6 个低效 RSS 源（SEC 8-K ×4、SemiAnalysis、NVIDIA Blog）；P1 新增阵亡名单收集机制供锦囊一破防；P3 与 P2 一样双重注入 P6+P9；系统升级为**九层数据架构**；checkpoint.py 支持 P3 断点恢复 |
| v4.1 | 2026-03-30 | 📱 **P2 X平台情报采集**：新增 Grok API 模块 (`p2_x_intel.py`)，原生访问 X(Twitter) 实时数据，15 赛道关键词矩阵定向扫描产业链 insider/leaker 爆料；P2 与 P1 完全并行，不经审计直接拼入 P6 映射输入 + P9 战报独立第 6 层；系统升级为六层数据架构 |
| v4.0 | 2026-03-30 | 🏗️ **架构瘦身与自动化**：引入 `chinesecalendar` 库彻底消灭硬编码日历与年度维护成本；将 `orchestrator.py` 的意大利面条式断点恢复逻辑重构为基于 Dispatch 的策略路由函数；修复了带有中文路径的 `.bat` 执行崩溃的系统级 Bug |
| v3.9 | 2026-03-29 | 🛡️ **反代重试策略统一**：彻底消除反代 API（Anthropic/Gemini/OpenAI）遇 401/403 直接放弃的致命缺陷，所有错误类型统一等待 5~10 秒后重试（最多 5 次），让反代自动切换后端可用账号；Key 池扩充至 9 个；清理 `__pycache__`、`scheduler.log` 等运行残留 |
| v3.8 | 2026-03-29 | 🌐 **网络连通性与代理重构**：弃用引发死锁的官方 Google SDK，`search_engine.py` 拥抱原生 `requests`；`settings.py` 内置自适应代理嗅探器，动态拉取本地翻墙端口；搜索三级降级链路全面打通 |
| v3.7 | 2026-03-29 | ⏰ **定时任务 + 自适应回溯**：新增 `scheduled_launcher.py` 交易日判断启动器 + Windows 计划任务自动运行；`get_run_config()` 改为自动计算上一交易日到当前的小时数 |
| v3.6 | 2026-03-29 | 📡 **信息源扩充 + 审计升级**：新增 5 个供应链/产业数据源；P5 审计新增「产业链关联度」三级筛选维度；P7 深研选题注入行情异动优先规则 |
| v3.5 模块化 | 2026-03-28 | 🏗️ **架构重构**：将 2190 行单体拆为 11 个模块 + Pydantic 数据模型 + 36 个单元测试；密钥外化至 .env |
| v3.5 | 2026-03-28 | 新增光纤光缆产业链赛道、Lightwave/FierceTelecom RSS 源、邮件模板升级 |
| v3.4 | 2026-03-28 | 稳定性优化：`--force` 拆分、requirements.txt、SSL 静音、修复 P7 数据未注入 P9 bug |
| v3.3 | 2026-03-28 | Scrapling 搜索增强 + 网页采集引擎 + P8a 独立输出 |
| v3.2 | 2026-03-27 | 熔断器 + KDJ 修复 + P8 崩溃恢复 + Twitter RSSHub |
| v3.1 | 2026-03-27 | 搜索三级降级 + RSS 直采永久切换 + 16 赛道扩充 |
| v3.0 | 2026-03-26 | P1 RSS 直采模式 + NativeLLMAdapter |
> 2026-04-12 文档同步说明
>
> 当前 4 项重点优化的真实状态如下：
>
> | 优化项 | 状态 | 说明 |
> | --- | --- | --- |
> | P2/P3 结构化 claim + source + confidence 再进 P6/P9 | 未完成 | 当前 `p2_x_intel.py`、`p3_agent.py` 仍以文本为主进入下游 |
> | 统一 P6/P7/P7.5 规划与去重逻辑 | 部分完成 | `p7_deep_research.py` 已统一 P7/P7.5 研究计划与最近 5 天去重，但 `p6_mapper.py` 仍保留一套历史去重逻辑 |
> | 给 P1/P3/P4/P7.5 加真实收益指标 | 已完成 | 已新增 `contribution_metrics.py`，运行结束后会产出 `Layer_ROI_*.json/.md` 并回填 `run_manifest.json` |
> | 拆分 P8/P9 大模块 | 未完成 | `p8_technical.py` 与 `p9_reporter.py` 仍未拆模块 |
>
> 新增 ROI 指标：
>
> - `unique_units`：该层独有的有效信息单元
> - `adopted_units`：被 P6 或 P9 实际采纳的信息单元
> - `cost_seconds`：该层本轮耗时
> - `roi_score`：按独有贡献、下游采纳、成本效率综合打分
> - `roi_recommendation`：`keep / keep_but_shrink / gate_by_event / merge_or_gate / candidate_to_prune`
