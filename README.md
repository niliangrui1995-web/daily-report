# VCP Hunter — 北美 AI 产业链情报战报系统 v3.1

> 全自动化的 AI 产业链情报采集、审计、映射和战报生成系统。  
> 每日早盘前自动运行，将海外产业链动态映射为 A 股投资信号。

---

## 📐 系统架构

```
L1 情报采集 (Gemini 2.5 Flash + Google Search Grounding)
    ↓ 18 条搜索查询 × 5 Key 并发
L1.5 审计清洗 (Gemini 3.1 Pro High)
    ↓ 时效验证 + 可信度打分 + 去主观噪声
L2 A 股映射 (Claude Opus 4.6 Thinking)
    ↓ 产业链事件 → A 股标的 + 弹性评级
    ├── L2.5 深度研究 (多轮迭代搜索，可选)
    └── L3a 补充搜索 (Opus 指挥官下达搜索指令)
L3 股价分析 (本地通达信 K 线数据)
    ↓ 250 日 K 线 + 8 项技术指标 + 财务数据
L3.5 审计 (Gemini 3.1 Pro High)
    ↓ 补充搜索结果二次审计
L4 终极战报 (Claude Opus 4.6 Thinking)
    ↓ 五层数据综合 → 标的狙击表 + 操作建议
📧 邮件推送
```

### 产业链全赛道覆盖

| 赛道 | 关键实体 |
|------|---------|
| 芯片设计与底层IP | Synopsys, Cadence, ARM, Arteris, Qualcomm/Alphawave, proteanTecs |
| 定制化ASIC与代工 | TSMC, Intel Foundry Services, Broadcom, Marvell, Alchip |
| 晶圆制造与材料设备 | ASML, Applied Materials, Lam Research, TEL, Soitec, IQE, PVA TePla |
| HBM与核心存储矩阵 | SK Hynix, Samsung, Micron, Hanmi, NAMICS |
| CXL与数据存储 | Western Digital, Phison, Montage |
| 先进封装与混合键合 | Besi, EV Group, ASMPT, TSMC, ASE, Amkor |
| 核心基板与封装材料 | Ibiden, Unimicron, Ajinomoto, Intel Foundry, Absolics, Corning |
| 网络交换与高速电互连 | Broadcom, Nvidia, Marvell, Astera Labs, Credo, Amphenol, TE Connectivity |
| 光模块、硅光与CPO | Intel, 中际旭创, 高意, 新易盛, Ayar Labs, Lightmatter, Fabrinet |
| 高频PCB与覆铜板材料 | Rogers, Panasonic, EMC, Nittobo, Wus, Compeq |
| AI服务器基建与温控 | nVent, Vertiv, Eaton, Schneider, Delta, MPS, CoolIT, Auras, AVC |
| 能源基建与核电(SMR) | Constellation Energy, Vistra, Talen Energy, Oklo, TerraPower |
| 测试、老化与探针卡 | Advantest, Teradyne, KYEC, FormFactor, MJC, Technoprobe, MPI |
| 算力颠覆与主权生态 | Groq, Cerebras, Tenstorrent, SMIC, Huawei, NAURA, AMEC |
| 云巨头、算力租赁与数据 | Microsoft, Meta, Google, AWS, OpenAI, Anthropic, CoreWeave, Scale AI, Reddit |
| 边缘AI与具身智能 | Apple, Qualcomm, MediaTek, Figure AI, Tesla |

---

## 🔀 双通道 API 路由

系统使用两条独立的 API 通道，按"搜索"和"推理"职责分离：

| 通道 | 地址 | 认证 | 用途 | 涉及层级 |
|------|------|------|------|---------|
| ① Cloudflare Worker 跳板 | `api.niliangrui.cloud` | 5 个官方 Google API Key 轮换 | 联网搜索（Google Search Grounding） | L1、L2.5、L3a |
| ② 本地反代 (Antigravity) | `127.0.0.1:8045` | 1 个 Antigravity API Key | 纯文本推理（不联网） | L1.5、L2、L3.5、L4 |

> **一句话总结**：凡是需要"搜东西"的走 Cloudflare + 官方 Key，凡是需要"动脑子分析"的走本地反代。

---

## 🧠 各层级 Prompt 指令详解

### L1 情报采集

- **模型**：`gemini-2.5-flash`（通道 ① Cloudflare）
- **工具**：Google Search Grounding（联网搜索）
- **触发逻辑**：根据日历检测十六太保财报日，自动切换 MODE_A（平淡期）或 MODE_B（财报期）

**MODE_A 平淡期 Prompt**：

```
你是 L1 无情情报矿工。当前处于新闻平淡期。
⚠️ 当前时间：{today_str}（只关注最近 {backtrack} 内的信息）
警告：跳过所有表层财经新闻！启动全链条客观监控雷达！

【铁律与权重分配】：
1. [70% 绝对核心权重] 在极客爆料源、核心外媒中执行底层信息扫描，
   提取过去 48 小时内的物理参数变化。不要带寻找涨停因子的倾向，做个事实搬运工。
2. [30% 验证权重] 对照官方技术博客，将提取的参数与官方底稿做中立并列呈现。
3. 格式：序号. [实体] 客观事实内容 【信源|URL|日期】
4. 搜不到的如实说"未搜索到"。严禁编造。

寡头关键词（命中即提取）：TSMC, Samsung Foundry, ARM, AMD, Intel, ...
```

**MODE_B 财报期 Prompt**：

```
你是 L1 情报矿工。当前 {hit_name} 财报已发布或即将发布！
警告：当前处于财报及指引落地期！将 90% 算力锁死在官方通道原声！

【铁律与权重分配】：
1. [60%] 深度挖掘 SeekingAlpha 业绩电话会 Q&A Session
2. [30%] 提取下季度资本开支（Capex）、发货规模等确切数字
3. [10%] 提取极客大V对财报的暗黑验证和拆解
```

---

### L1.5 审计清洗

- **模型**：`gemini-3.1-pro-high`（通道 ② 本地反代）
- **工具**：无（纯文本闭卷推理）

**Prompt 核心指令**：

```
你是 VCP Hunter L1.5 层——独立纯文本情报审计员。
本次审计仅做逻辑推理验证，不要主动搜索新信息。

【任务一：时效性验证】
- 发生时间距现在在 {backtrack} 内 → 保留
- 超出时间窗的旧闻 → 直接删除
- 时间模糊但事件特征表明是近期的 → 保留并标注"时间待确认"

【任务二：严格可信度打分 1-10】
🟢 10/10：官方源原生直接表述（nvidia.com、sec.gov）
🟢 8/10：彭博、路透一线机构
🟡 5-7：券商研报、行业公众号
🔴 1-4：股吧、雪球、自媒体（直接删除）

【强制打假触发器】
- "官方文件"与"A股公司"同时出现 → 必须隔离官方原文 vs 券商主观推演
- 包含【唯一】【最强受益】【远低估】等炒作词 → 扣 3 分 + 挂警示标签

【任务三：数据清洗】
🚫 删除：主观判断词、分析师评级、模糊表述
⚠️ 豁免：十六太保的最新股价涨跌幅（客观市场信号，保留）
✅ 只保留：谁、什么时间、做了什么、具体数值是多少
```

---

### L2 A 股映射 + 指挥官

- **模型**：`claude-opus-4-6-thinking`（通道 ② 本地反代）
- **工具**：无

**Prompt 核心指令**：

```
你是 VCP Hunter L2 层——A 股映射引擎 + 情报指挥官。

【映射规则】：
1. 将 L1 数据按赛道分组
2. 每个赛道至少映射 3 家 A 股公司
3. 每家给出：公司名称、6位代码、所属赛道、映射理由
4. 弹性分级：🔥高弹性 / ⚡中弹性 / 💀纯概念

【十六太保信号推理】：
超级巨头（NVDA/AVGO/TSM/ASML + 四大 CSP）：
- 🚀 大涨 ≥3% → 标注催化剂，重点深挖
- 💥 暴跌 ≤-5% → 标注高度警惕

输出: 赛道 → 公司名（代码）| 弹性级别 | 映射理由

【指挥官权限——可选】：
SEARCH: [补充搜索关键词]      ← 最多 5 条，L3a 阶段执行
DEEP_RESEARCH: [深研关键词]    ← 最多 2 条，L2.5 阶段执行
```

---

### L2.5 深度研究

- **模型**：`gemini-2.5-flash` 搜索 + `gemini-3.1-pro-high` 生成关键词（混合通道）
- **轮次**：3 轮迭代

```
第 1 轮：广度扫描（使用 Opus 指定的 DEEP_RESEARCH 关键词）
第 2-3 轮：基于上轮结果，由 Pro 自动生成更深入的搜索方向

Pro 细化 Prompt：
  "基于以下已搜集信息，生成 1 条更深入的搜索查询。
   目标：挖掘上一轮未解答的关键问题（why / who wins / 替代方案）
   只输出 1 行搜索关键词（英文），不要解释。"
```

---

### L3 股价分析

- **数据源**：本地通达信 K 线文件（无模型调用）
- **分析内容**：

```
对 L2 映射出的每只 A 股标的，读取本地 250 日 K 线：
- 最新收盘价 + 总市值 + 财务数据
- 近 1/3 个月涨跌幅
- 250 日区间分位
- 量能分析（5日/20日/60日量比）
- 布林带宽度、均线密集度
- MACD 状态、换手率、阳线占比
```

---

### L3.5 审计

- **模型**：`gemini-3.1-pro-high`（通道 ② 本地反代）
- **与 L1.5 相同的审计框架**，但无时效性限制（补充搜索为深度调研）

---

### L4 终极战报

- **模型**：`claude-opus-4-6-thinking`（通道 ② 本地反代）

**Prompt 核心指令**：

```
你是 VCP Hunter L4 层——最终战报生成器，深谙 A 股投机规律。

你将收到五层数据：
1. L1.5 审计后情报
2. L2.5 深度研究（如有）
3. L2 映射
4. L3.5 审计后补充搜索
5. L3 通达信 250 日 K 线 + 8项技术指标 + 财务数据

【战报结构】：
## 一、核心信号（≤5 条）
## 二、标的狙击表（每个细分行业最多 3 只）
   | 标的 | 代码 | 总市值 | 最新价 | 近3月 | 250日分位 | 量能 | 弹性 | 短期爆发力 | 风控 |

   短期爆发力：⚡⚡⚡极强 / ⚡⚡较强 / ⚡一般

## 三、资金路径推演
   海外事件 → 产业传导 → A 股标的排列

## 四、今日操作建议
   3-5 只最值得关注的标的 + 理由

铁律：尽量简洁！表格优于文字。基于 L3 真实数据，不要编造。
```

---

## 📁 项目结构

```
Github/
├── daily_report.py          # 核心引擎：L1→L4 全流程调度
├── native_llm_adapter.py    # 多模型适配器（Claude/Gemini/OpenAI 统一接口）
├── cloudflare_worker.js     # Cloudflare Worker 反代（跳板访问 Google API）
├── resume_from_l2.bat       # 接力脚本：跳过 L1 直接从 L2 开始
├── test_l1_search.py        # L1 搜索单元测试
├── test_models.py           # 模型连通性测试
├── test_official_search.py  # Google 官方直连测试
└── utils_and_tests/
    ├── format_tables.py     # 表格格式化工具
    ├── generate_chart.py    # 图表生成工具
    ├── test_l2.py           # L2 映射测试
    └── test_xquik.py        # 行情数据测试
```

---

## ⚙️ 配置

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `GEMINI_SEARCH_API_KEYS` | Gemini 搜索 Key 池（逗号分隔多个） | 代码内置 5 个 |
| `GEMINI_SEARCH_BASE_URL` | Gemini API 反代地址 | `https://api.niliangrui.cloud` |
| `ANTIGRAVITY_BASE_URL` | 大模型反代地址（Pro/Opus） | `http://127.0.0.1:8045` |
| `ANTIGRAVITY_API_KEY` | 反代 API Key | 代码内置 |
| `TAVILY_API_KEY` | Tavily 备用搜索 Key | 代码内置 |
| `EMAIL_ADDRESS` | 发件邮箱 | 代码内置 |
| `EMAIL_PASSWORD` | 邮箱授权码 | 代码内置 |
| `RECEIVER_EMAIL` | 收件人（逗号分隔多个） | 代码内置 |
| `FORCE_72H` | 强制 72 小时回溯 | — |
| `FORCE_24H` | 强制 24 小时回溯 | — |

### 依赖安装

```bash
pip install google-genai yfinance requests ratelimit markdown anthropic openai
```

---

## 🚀 运行方式

### 正常运行（全流程 L1→L4）

```bash
python daily_report.py
```

- 自动判断是否为 A 股交易日（非交易日自动跳过）
- 周一回溯 72 小时，其他日回溯 24 小时
- 运行结束后通过邮件推送战报

### 接力模式（跳过 L1/L1.5，从 L2 开始）

```bash
python daily_report.py --skip-to-l2
```

> 读取最新的 `L1.5_审计_*.md` 文件，直接进入 L2 Opus 映射。  
> 适用场景：L1 搜索已完成但后续步骤中断，需要接力继续。

### GitHub Actions 云端搜索缓存模式

```bash
python daily_report.py --search-cache L1_results.json
```

> 从 GitHub Actions 云端搜索模块（`github_search.py`）生成的 JSON 缓存读取 L1 数据。

---

## 🔑 多 Key 轮换机制

系统使用 **5 个 Gemini API Key**（不同 Google Cloud 项目），实现：

- **轮转分压**：每次 API 调用自动切换到下一个 Key，分散并发压力
- **429 自动切换**：某个 Key 触发限流后，标记为耗尽并切换到下一个
- **线程安全**：`SearchKeyRotator` 使用锁保证多线程下的 Key 状态一致
- **网络重试独立**：网络超时与 Key 轮换分开计数，互不干扰

---

## 🛡️ 风控策略

| 措施 | 说明 |
|------|------|
| UA 伪装 | 所有请求携带 Chrome 浏览器 User-Agent |
| 随机延迟 | 搜索间隔 1~6 秒随机抖动，避免并发洪峰 |
| 阶段冷却 | L1→L1.5→L2→L4 之间强制暂停 5~15 秒 |
| 三层重试 | 致命熔断(401/403) → 限流退避(429) → 超时重试 |
| Tavily 兜底 | Gemini 全部失败时自动降级到 Tavily Search |
| 并发控制 | 搜索线程数 = Key 池大小（默认 5） |

---

## 📊 输出文件

所有输出保存在 `每日热点输出/{YYYYMMDD}/` 目录：

| 文件 | 内容 |
|------|------|
| `L1_原始数据_*.json` | L1 搜索原始结果（JSON） |
| `L1.5_审计_*.md` | Pro 审计后的高可信情报 |
| `L2_映射_*.md` | Opus A 股映射结果 |
| `L2.5_深度研究_*.md` | 多轮迭代深度研究（可选） |
| `L3_股价数据_*.md` | 通达信 250 日 K 线分析 |
| `L3.5_审计_*.md` | 补充搜索审计结果 |
| `战报_*.md` | 最终战报（邮件同步推送） |
| `error.log` | 错误日志（仅 ERROR 级别） |

---

## 🌐 Cloudflare Worker 反代

`cloudflare_worker.js` 部署到 Cloudflare Workers，将请求转发至 Google Gemini API 官方地址，实现国内无障碍直连。

```
客户端 → 自定义域名 → Cloudflare Worker → generativelanguage.googleapis.com
```

---

## 📝 NativeLLMAdapter 适配器

`native_llm_adapter.py` 是多模型统一调用层，核心特性：

- **自动路由**：根据模型名自动选择 Anthropic / Google / OpenAI 协议
- **LangChain 兼容**：实现 `invoke()` 和 `bind_tools()` 接口
- **三层防风控**：致命熔断 / 限流退避 / 超时重试
- **工具调用**：通过 prompt 注入实现 tool calling（无需原生支持）
