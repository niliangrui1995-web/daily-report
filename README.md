# VCP Hunter 北美 AI 产业链情报战报系统

> 按 A 股交易日运行的多阶段情报流水线：采集北美 AI 产业链信息，审计证据，映射 A 股受益方向，生成战报并邮件分发。

Last verified: 2026-07-16

---

## 项目定位

本仓库是本地 Python 版每日战报流水线，主要通过 API、RSS、网页抓取和本地调度运行。

它和 `每日战报_浏览器自动化` 是两个不同工作流：本仓库不要求 Browser Use / Gemini 网页 / Grok 网页作为硬门槛；浏览器采集型自动化应放在对应浏览器自动化项目里维护。

---

## 当前运行事实

- 根目录 `run_daily.bat` 是 Windows 手工运行和计划任务入口。
- 项目主体位于 `每日战报/` 子目录，控制面入口是 `pipeline_cli.py`。
- `pipeline_cli.py run/resume/force-run/restart` 在取得运行锁后记录严格 preflight，通过后才进入 orchestrator。
- `run_daily.bat` 默认失败后自动 `resume` 一次：`RUN_DAILY_AUTO_RESUME=on`，延迟 `10` 秒。
- 无参数或参数为 `run` 时，`run_daily.bat` 优先进入 `scheduled_launcher.py`。
- `scheduled_launcher.py` 会先判断当天是否为 A 股交易日；当前代码规则是 `schedule_rule=run_every_trading_day`，交易日运行，非交易日跳过。
- `pipeline_lock.py` 使用全局 run lock 防止重复启动。
- `run_manifest.json` 固定写入当日输出目录根部，记录阶段状态、运行上下文和 preflight 摘要。

---

## 目录结构

```text
D:\vcp_hunter\每日战报\
├── run_daily.bat                 # Windows 入口：计划任务/双击/手工命令
├── pyproject.toml                # 直接依赖、测试和静态检查配置
├── uv.lock                       # 跨机器可复现的完整依赖锁
├── .venv\                        # 本地虚拟环境
├── docs\                         # 非运行时的历史设计资料
├── 每日战报\                     # Python 流水线源码
│   ├── pipeline_cli.py            # 控制面：run/resume/status/doctor/preflight/clean/prune
│   ├── scheduled_launcher.py      # 交易日门禁后再启动 pipeline_cli.py run
│   ├── run_config.py              # 调度决策和回溯窗口计算
│   ├── orchestrator.py            # P1-P9 主流程编排
│   ├── pipeline_runtime.py        # 阶段执行、子进程隔离、超时落盘
│   ├── settings.py                # .env 加载、模型/API/SMTP/TLS/NO_PROXY 配置
│   ├── prompts\                   # 各阶段提示词模板
│   └── tests\                     # 离线回归与控制面测试
└── 每日热点输出\                 # 每日运行产物
```

### 设计资料

以下资料用于理解历史设计背景，不是当前运行契约：

- [VCP Hunter 架构全地图与源码解析](docs/VCP_Hunter_架构全地图与源码解析.md)
- [VCP Hunter 实盘时序工作流模拟](docs/VCP_Hunter_实盘时序工作流模拟.md)

---

## 快速开始

### 1. 进入项目根目录

```powershell
cd D:\vcp_hunter\每日战报
```

### 2. 安装依赖

使用根目录锁文件创建或同步虚拟环境：

```powershell
uv sync --locked
```

`uv` 会按 `.python-version` 准备 Python 3.14，并创建根目录 `.venv`。兼容旧 pip 环境时，可安装由同一锁文件导出的根目录清单：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

### 3. 配置 `.env`

复制模板：

```powershell
Copy-Item .\每日战报\.env.example .\每日战报\.env
notepad .\每日战报\.env
```

常用变量：

| 变量 | 用途 |
| --- | --- |
| `TDX_ROOT` | 本地通达信数据根，默认 `D:\HT`；需包含 `T0002\hq_cache` 和 `vipdoc` |
| `ANTIGRAVITY_BASE_URL` | OpenAI 兼容模型服务地址；回环地址可用 HTTP，远端地址必须用 HTTPS；默认 `http://127.0.0.1:8045` |
| `ANTIGRAVITY_API_KEY` | Pro/GPT-5.6 Sol/Agent 兼容模型鉴权 |
| `PRO_OPUS_BASE_URL` / `PRO_OPUS_API_KEY` | 本地模型服务：P5/P7a/P7.5 主审计走 Chat Completions，P6/P9 主备模型固定复用 Responses 接口；未配置时沿用 Antigravity 配置 |
| `AUDIT_FALLBACK_BASE_URL` / `AUDIT_FALLBACK_API_KEY` | P5/P7.5/P7a 的 Claude 专用备用接口；当前最终请求为 `https://cldapi.com/v1/messages`，不得与 Grok 备用密钥混用 |
| `GEMINI_SEARCH_API_KEYS` | Gemini 搜索 key 池，逗号分隔 |
| `GEMINI_SEARCH_MODEL` | Gemini 搜索模型，默认 `gemini-2.5-flash` |
| `AUDIT_MODEL` | P5/P7a/P7.5 审计模型，默认 `claude-sonnet-4-6`；兼容旧变量 `GEMINI_PRO_MODEL` |
| `GEMINI_PRO_API_KEY` | Gemini 审计模型专用 key；仅当 `AUDIT_MODEL` 使用 Gemini 3.5 时生效 |
| `TAVILY_API_KEY` | Tavily 备用搜索 |
| `GROK_API_KEY` | P2 X/Grok 情报采集 |
| `GROK_BASE_URL` | P2 Grok 主接口根地址，默认 `https://api.x.ai`，最终请求路径为 `/v1/responses` |
| `GROK_MODEL` | P2 Grok 模型，默认 `grok-4.3` |
| `GROK_FALLBACK_BASE_URL` / `GROK_FALLBACK_API_KEY` | P2 Grok 专用备用接口和密钥；当前最终请求为 `https://cldapi.com/v1/responses`，不得与 Claude 审计备用密钥混用 |
| `GROK_FALLBACK_MODEL` | P2 Grok 备用模型，默认 `grok-4.5` |
| `GROK_REASONING_EFFORT` | P2 Grok 推理强度，默认 `none` 降低推理 token 成本 |
| `GROK_MAX_OUTPUT_TOKENS` | P2 Grok 单批输出上限，默认 `4096` |
| `GROK_NATIVE_REASONING_EFFORT` | 通用 Grok adapter 推理强度覆盖，默认沿用 `GROK_REASONING_EFFORT` |
| `GROK_NATIVE_MAX_OUTPUT_TOKENS` | 通用 Grok adapter 输出上限，默认 `4096` |
| `GROK_NATIVE_ENABLE_X_SEARCH` | 通用 Grok adapter 是否挂载 `x_search`，默认 `false` |
| `EMAIL_ADDRESS` / `EMAIL_PASSWORD` | SMTP 发件账号和授权码 |
| `RECEIVER_EMAIL` | 收件人，多个邮箱用逗号分隔 |
| `INSECURE_TLS_ALLOWLIST` | 允许降级为不安全 TLS 的域名白名单，默认空 |

所有密钥只应写入 `.env` 或外部环境变量，不要写进代码。

---

## 常用命令

### 查看运行状态

```powershell
uv run python .\每日战报\pipeline_cli.py status
```

### 自检

```powershell
uv run python .\每日战报\pipeline_cli.py doctor
uv run python .\每日战报\pipeline_cli.py doctor --strict
uv run python .\每日战报\pipeline_cli.py preflight
```

退出码约定：

- `0`：通过
- `1`：检查失败
- `2`：发现活动 run lock

### 按计划任务路径运行

```powershell
.\run_daily.bat
```

这会走 `scheduled_launcher.py`，先判断交易日，再按当前调度规则决定是否启动主流程。

### 直接运行控制面

```powershell
uv run python .\每日战报\pipeline_cli.py run
uv run python .\每日战报\pipeline_cli.py resume
uv run python .\每日战报\pipeline_cli.py force-run
uv run python .\每日战报\pipeline_cli.py restart
```

命令含义：

| 命令 | 用途 |
| --- | --- |
| `run` | 正常入口；当天已有 checkpoint 时自动断点续跑 |
| `resume` | 显式手工恢复；恢复逻辑与 `run` 相同 |
| `force-run` | 跳过交易日门禁，但仍使用当天 checkpoint 断点续跑 |
| `restart` | 跳过交易日门禁，归档旧 manifest，建立全新的 manifest 后从 P1 重新运行 |
| `status` | 查看当日输出、manifest、run lock 和阶段状态 |

`restart` 不删除旧产物。旧 manifest 会归档到当天 `meta/`，新 manifest 使用严格晚于旧批次的 `run_timestamp`；后续恢复只读取同一时间戳批次，避免同秒重启覆盖产物或把当天多次运行的产物混在一起。

### 清理

```powershell
.\.venv\Scripts\python.exe .\每日战报\pipeline_cli.py clean
.\.venv\Scripts\python.exe .\每日战报\pipeline_cli.py prune --keep-days 7
```

`clean` 清理源码树内的瞬态缓存；`prune` 清理旧输出目录，默认保留 7 天。

---

## `run_daily.bat` 行为

`run_daily.bat` 固定使用自身目录下的 `每日战报\pipeline_cli.py`，并使用根目录 `.venv\Scripts\python.exe`；入口不会扫描或执行其他同级目录。

默认环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_DAILY_AUTO_RESUME` | `on` | `run` 失败且退出码不是 `2` 时，自动 `resume` 一次 |
| `RUN_DAILY_RESUME_DELAY_SECONDS` | `10` | 自动恢复前等待秒数 |
| `RUN_DAILY_HOLD_SECONDS` | `15` | 命令结束后保留窗口秒数，设为 `0` 可立即退出 |

示例：

```bat
set RUN_DAILY_HOLD_SECONDS=0
run_daily.bat status
```

传参规则：

- `run_daily.bat` 或 `run_daily.bat run`：走 `scheduled_launcher.py`。
- `run_daily.bat status` / `resume` / `force-run` / `restart` / `preflight`：直接转发给 `pipeline_cli.py`。

---

## 调度逻辑

外层调度器可以每天固定时间唤醒 `run_daily.bat`，项目内层再决定是否真正运行。

当前内层规则：

1. `scheduled_launcher.py` 调用 `get_schedule_decision(date.today())`。
2. 非 A 股交易日：记录 skip，退出码 `0`。
3. A 股交易日：启动 `pipeline_cli.py run`。
4. `run_config.py` 用上一个交易日 09:00 作为回溯窗口锚点，并按实际间隔计算 `24h`/`72h` 等动态窗口。

日历依赖：

- PyPI 包名：`chinesecalendar`
- Python import 名：`chinese_calendar`
- 缺失或不支持当年数据时，会降级为仅排除周末的保守策略

### 最小权限计划任务

正式计划任务使用独立的 `.venv-service`，其基础解释器固定为机器级 `C:\Python314`，避免运行身份依赖 Administrator 用户目录：

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-service"
uv sync --locked --no-dev --python C:\Python314\python.exe
```

迁移脚本默认只执行零副作用 readiness 检查并打印计划，不修改账户、ACL、任务、代理或防火墙。凭据尚未完成轮换、任务正在运行、锁/端口/资源有冲突时返回退出码 `2`：

```powershell
.\ops\migrate_scheduled_task.ps1
```

必须先吊销并轮换 Git 历史中暴露过的 SMTP/API 凭据。自动检查确认当前 `.env` 中的秘密均不再出现在可达 Git 历史、且没有短于 8 个字符的弱秘密，再由操作者显式确认外部凭据已吊销后，`-Apply -ConfirmCredentialsRotated` 才会请求 UAC。

如果资产所有者明确接受未轮换凭据继续暴露的风险，可改用 `-AcceptUnrotatedCredentialsRisk`。该开关不会把轮换检查伪装成通过，迁移结果会永久记录风险接受；不得与 `-ConfirmCredentialsRotated` 同时使用：

```powershell
.\ops\migrate_scheduled_task.ps1 -Apply -AcceptUnrotatedCredentialsRisk
```

脚本会导出原任务和 ACL，先禁用原触发器并取得流水线维护锁，再创建强制密码、随机密码的专用标准账户。源码和代理程序仅授予读取/执行，输出目录可写；`D:\HT` 通过专用账号的 deny-write ACE 保持有效只读，不改变其他用户现有权限。`CLIProxyAPI` 目标目录在复制任何凭据前即锁定为私有 ACL，仅 `auths` 子目录允许专用账号修改。

已完成迁移的机器可独立、幂等地收紧 `.env` 权限；该模式不受首次迁移 readiness 条件限制，只保留 SYSTEM、Administrators、当前管理员的完全控制及专用账号的只读权限：

```powershell
.\ops\migrate_scheduled_task.ps1 -RepairEnvAcl -Apply
```

迁移依次完成 8318 隔离 canary、8317 live canary、正式 `cmd.exe` Action 的 `preflight` 实跑，最后才替换正式任务主体。回滚只删除本次创建的资源，并恢复本次实际修改的任务、ACL 和桌面代理。

```powershell
.\ops\migrate_scheduled_task.ps1 -Apply -ConfirmCredentialsRotated
```

服务账户 canary 会验证机器级 Python、临时目录、输出写入、本地 TDX 读取与写拒绝、严格 preflight、真实低 token 模型推理、Gemini/xAI 鉴权，以及 SMTP TLS 登录。SMTP 探针只登录不发信；迁移过程不会生成正式战报。

---

## 运行流程与门禁

正常依赖关系：

```text
P1 ──→ P5 ──┐
P2 ─────────┤
P3 ─────────┼──→ P6 ──→ P7 / P7a / P7.5 ──→ P8 ──→ P9
P4 ─────────┘    硬门禁          软门禁          软门禁     硬门禁
                                                              │
                                               保存 MD ──→ 邮件 ──→ ROI
```

- P1、P2、P3 并行采集；P1 先经过 P5 审计，再与 P2、P3、P4 一起进入 P6 映射。
- P1/P2/P3 会同时校验状态、正文和结构化计数：P1 标题壳、P2 空 `<x_leaks>` 或占位 `Claim`、P3 只有标题或占位核心字段都不会落成可复用成功；供应商明确返回无结果时则记录为合法空结果继续。
- P1/P3 会区分正常无结果、缺 Key、部分源失败和全部失败；部分成功不会再被伪装成干净完成。
- P4 至少需要一条有效收盘价；全是“无数据/获取失败”的 Markdown 只保留为诊断文件，后续恢复会重取 P4 并重做 P6。
- P5 同时核对通过数、删除数和实际列表；空壳审计会补跑 P5，合法“全部删除”则记录为 `completed_no_results`。通过正文与删除清单分开保存，断点恢复不会把已删除证据重新送入 P6/P9。
- P5 主备均失败但 P1 原始证据非空时，使用明确标记的未审计 P1 降级输入，避免静默中断证据链。
- P6 是硬门禁：不仅要求非空，还要求有效事件、至少 5 个唯一且通过当前身份校验版本的标的和完整赛道结构；不完整输出不落盘，P9 不会启动，状态保留为可恢复的 `p6_resume`。
- P7/P7a/P7.5 和 P8 是软门禁：模型空响应与“确实无结果”使用不同状态；合法空结果不会生成或复用旧占位文件，也不会在断点恢复时被重复调用。P7a 短输入属于原始 P7 透传，不会误判为空结果；P7.5 审计确认无有效证据后，ROI 不会重新采用被删除的原始文本。P8 请求了股票但没有任何可用本地 K 线时记为 `completed_structured_degraded`，不会伪装成干净完成。
- P9 是硬门禁：空报告、空章节或只有结构化占位对象的报告不落入可操作完成态；合法“无可操作信号”会保留完整章节并记录独立 outcome。写 Markdown 前先登记 `report_save_in_progress`，原子保存后登记 `report_saved_pending_delivery`，再进入邮件分发和 ROI。
- ROI 必须同时具备 Markdown、JSON 和完整分层统计，单有 `completed` 状态不能让整条任务进入终态。
- 邮件已配置但发送失败时，ROI 暂不运行；下次只恢复邮件，成功后继续 ROI。未配置邮件时，本地报告以 `completed_no_email` 完成并继续 ROI。

控制阶段如下：

| 控制阶段 | 主要内容 | 默认超时 |
| --- | --- | --- |
| `collect_intel` | P1 RSS/Web、P2 X/Grok、P3 机动搜索；`intel_resume` 时只运行失败分支 | 900s |
| `recover_p2` | 只补跑缺失或失败的 P2，不重复运行 P1/P3 | 480s |
| `audit_evidence` | P5 证据审计与 checkpoint 复用；`p5_resume` 时只补审计 | 900s |
| `market_mapping` | P4 海外龙头行情快照、P6 A 股映射 | 3900s |
| `research` | P7 深度研究、P7a 审计、P7.5 补充搜索；失败可继续 | 900s |
| `technical` | P8 技术面/股价/弹性层；区分有效数据、合法无股票和可恢复的语义空数据，失败可继续 | 300s |
| `report` | P9 战报生成、保存与首次邮件发送 | 3900s |
| `delivery_resume` | 复用已保存战报，仅重试邮件发送 | 600s |
| `roi` | Layer ROI 贡献评估或独立恢复 | 180s |

阶段由 `pipeline_runtime.py` 在可终止子进程中执行。控制阶段超时写入 `failed_timeout`，异常写入 `failed_exception`；软门禁失败后继续，硬门禁失败后停止。

### 各环节模型与接口总览

以下内容按 2026-07-16 的当前代码和本机非敏感配置核对。表中 URL 是最终请求路径，不包含任何密钥值。

| 环节 | 是否调用模型 | 主链路 | 备用/降级链路 | 鉴权变量 |
| --- | --- | --- | --- | --- |
| P1 RSS/Web 采集 | 否 | RSS 与网页抓取 | 单个来源失败时保留其他成功来源 | 按公开数据源访问，无统一模型密钥 |
| P2 X/Grok 情报 | 是 | `grok-4.3` → `https://api.x.ai/v1/responses` | `grok-4.5` → `https://cldapi.com/v1/responses` | 主：`GROK_API_KEY`；备：`GROK_FALLBACK_API_KEY` |
| P3 机动搜索 | 是 | `gemini-2.5-flash` + Google Search → `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent` | Tavily 搜索 API → `https://api.tavily.com/search`；Tavily 是搜索降级通道，不是指定的备用 LLM | 主：`GEMINI_SEARCH_API_KEYS`；备：`TAVILY_API_KEY` |
| P4 海外行情 | 否 | `yfinance` 行情数据 | 数据缺失时由阶段状态触发后续恢复 | 无模型密钥 |
| P5 证据审计 | 是 | `claude-sonnet-4-6` → `http://127.0.0.1:8317/v1/chat/completions` | 同模型 → `https://cldapi.com/v1/messages` | 主：`PRO_OPUS_API_KEY`；备：`AUDIT_FALLBACK_API_KEY` |
| P6 A 股映射 | 是 | `gpt-5.6-sol` → `http://127.0.0.1:8317/v1/responses` | `claude-opus-4-6-thinking` → 同一个 `/v1/responses` | 主备共用 `PRO_OPUS_API_KEY` |
| P7 深度研究 | 是 | 与 P3 相同的 `gemini-2.5-flash` 搜索链路 | 与 P3 相同的 Tavily 搜索降级通道 | `GEMINI_SEARCH_API_KEYS` / `TAVILY_API_KEY` |
| P7a 深研审计 | 条件调用 | P7 正文超过 200 字时，与 P5 使用相同 Claude 主链路 | 与 P5 使用相同 Claude 备用链路；审计失败时透传原始 P7 | `PRO_OPUS_API_KEY` / `AUDIT_FALLBACK_API_KEY` |
| P7.5 补充搜索 | 条件调用 | 与 P3 相同的 Gemini 搜索链路 | 与 P3 相同的 Tavily 搜索降级通道 | `GEMINI_SEARCH_API_KEYS` / `TAVILY_API_KEY` |
| P7.5 补充审计 | 条件调用 | 搜索产生有效正文后，与 P5 使用相同 Claude 主链路 | 与 P5 使用相同 Claude 备用链路 | `PRO_OPUS_API_KEY` / `AUDIT_FALLBACK_API_KEY` |
| P8 技术面 | 否 | 从 `TDX_ROOT=D:\HT` 读取本地通达信数据并计算指标 | 无可用 K 线时记录结构化降级 | `TDX_ROOT`，无模型密钥 |
| P9 战报生成 | 是 | `gpt-5.6-sol` → `http://127.0.0.1:8317/v1/responses` | `claude-opus-4-6-thinking` → 同一个 `/v1/responses` | 主备共用 `PRO_OPUS_API_KEY` |
| 邮件分发 | 否 | SMTP TLS 登录并发送已保存报告 | 失败后进入 `delivery_resume` | `EMAIL_ADDRESS` / `EMAIL_PASSWORD` / `RECEIVER_EMAIL` |
| Layer ROI | 否 | 本地规则计算贡献与分层统计 | 缺失时进入 `roi_resume` | 无模型密钥 |

密钥边界必须保持如下：

- `GROK_FALLBACK_API_KEY` 只用于 P2 的 Grok 备用调用。
- `AUDIT_FALLBACK_API_KEY` 只用于 P5、P7a、P7.5 的 Claude 备用审计。
- `PRO_OPUS_API_KEY` 用于本地 `8317` 模型服务；P6/P9 切换到 Claude 备模时仍复用该密钥和 `/v1/responses`，不会切到 cldapi。
- `PRO_OPUS_BASE_URL` / `PRO_OPUS_API_KEY` 未配置时才沿用 `ANTIGRAVITY_BASE_URL` / `ANTIGRAVITY_API_KEY`。
- Tavily 只作为 Gemini 搜索失败后的降级通道；P3 缺少 Gemini 搜索密钥池时会直接跳过，不能只靠 `TAVILY_API_KEY` 单独启动。
- README 只记录变量名，不记录密钥值；实际密钥保存在被 Git 忽略的 `.env` 或外部秘密管理系统中。

### 主模型、备胎与时间预算

| 环节 | 主模型预算 | 备胎预算 | 切换规则 |
| --- | --- | --- | --- |
| P2 | `GROK_MODEL` 300s，单请求最多 75s | `grok-4.5` 120s | 401/403 立即切备；首次切备后锁定为 fallback-only，后续分批不再回主模型侵占备胎预算 |
| P5 | 主审计接口 `AUDIT_MODEL` 240s | 备用审计接口 `AUDIT_MODEL` 180s | 认证、风控、权限、超时或服务端错误停止主接口重试并切备 |
| P6 | `gpt-5.6-sol` 900s | `claude-opus-4-6-thinking` 900s | 认证、风控、权限、超时、服务端错误或 HTTP 200 空正文立即切备；JSON/完整性失败后的第二轮直接使用备胎 |
| P9 | `gpt-5.6-sol` 900s | `claude-opus-4-6-thinking` 900s | 与 P6 相同，主备各自拥有完整 900s；无效报告修复轮不再重复消耗主模型 |

P2 和 P5 只有在对应备用接口及密钥已配置时才能切备。P2 已成功的分批会保留；后续分批失败时记录降级，不丢弃前面的结果。P5 的 900s 控制阶段可容纳两轮 JSON 生成/修复所需的主备调用。P6/P9 的主模型 `gpt-5.6-sol` 与备胎模型 `claude-opus-4-6-thinking` 固定复用 `PRO_OPUS_BASE_URL` / `PRO_OPUS_API_KEY`，并继续使用 Responses API；切备只更换模型，不会改用 `AUDIT_FALLBACK_*`。首轮最坏主备共 1800s，语义修复轮只使用 900s 备胎，3900s 控制阶段仍为落盘、P4 和进程收尾保留余量。429 限流按剩余预算做有限退避，不会无限重试挤占备胎时间。

### 当天断点续跑

`run`、`resume` 和 `force-run` 都先取得全局运行锁，再检测当天 checkpoint。manifest 存在时只接受与根 `run_timestamp` 一致的阶段和产物；manifest 不可用时，只选择当天最新的一个时间戳批次，不跨批次拼接。

恢复优先级从高到低：

1. `p2_resume`：只有 P2 缺失或失败且无非空产物时，即使旧流程已误到达报告/邮件/ROI，也先只补跑 P2；复用已有 P1/P3/P5，然后重新进入 P6。
2. `p9_done`：P2 完整，且报告、分发、ROI 和根状态均完整，直接结束。
3. `delivery_resume`：已有报告但邮件未完成，只恢复原定投递策略；邮件成功后先恢复 ROI 所需的原始阶段上下文，再继续 ROI，避免用全空输入覆盖贡献统计。
4. `roi_resume`：报告分发已完成但 ROI 缺失，只补 ROI。
5. `intel_resume`：P1/P3 单独失败，或 P1/P2/P3 多支同时失败时，只补跑失败且无产物的分支；P1 被重采后会重做 P5，成功分支不重复运行。
6. `p5_resume`：P1 有效但 P5 失败、空壳或计数不一致时，只重做 P5；成功后重做依赖它的 P6 及下游。
7. `p6_resume`：P4 无有效行情，或 P6 已尝试但失败/结构不完整时，从 P4/P6 必要位置继续，不重复采集有效的 P1/P2/P3/P5。
8. `p9_resume`：P8 存在且语义有效时，从 P9 继续；可复用状态仅包括“非空 Markdown + 至少一只有效行情”和“P6 确实没有 A 股标的”的合法空结果。
9. `p8_resume`：研究层已完成（包括合法空结果），或 P8 为零字节、仅有 JSON、`skipped_no_vipdoc`、异常占位、请求股票但有效行情为零等可恢复状态时，从 P8 继续。
10. 已有 P6、P5 或 P1：分别从相应 checkpoint 继续；否则从 P1 开始。

恢复检测采用“状态优先 + 结构校验 + 上游依赖”三层判断，不再以文件存在作为成功依据。业务产物时间使用独立的 `output_updated_at`，ROI 指标回写只更新记账时间，不会把旧阶段伪装成新产物。任一上游变更或语义失效会级联移除旧下游 checkpoint；合法空结果会清掉同阶段遗留文件，但只在下游产物早于该结果时使其失效，避免误删基于该空结果生成的新报告。P6 更新后，即使新计划不再触发 P7/P7a/P7.5，也会用 `skipped_not_triggered` 写入当前 P6 哈希并清除旧哈希状态；旧研究结论、P8 和 P9 不会跨 P6 版本复用。P8 还会联合检查 manifest 状态、`stocks_requested`、`stocks_with_data`、当前 P6 唯一标的数和带数值的收盘行；JSON 只作为 P9 的辅助数据，不再被当作 P8 checkpoint 主产物。历史运行从未尝试 P8 时不会误删已保存报告，但已尝试且语义无效的 P8 会阻断 P9/ROI。

### P9 保存、邮件和 ROI 恢复

P9 写 Markdown 前先记录 `report_save_in_progress`，原子保存后立即写入 `report_saved_pending_delivery`。两次状态之间中断时，只要同批次非空报告已经存在，下次就进入 `delivery_resume`，不会重新调用 P9 模型。恢复会保留原任务的“必须发邮件/仅本地保存”策略，不会因当前环境变量变化而误发或静默降级。邮件成功后若进程在 ROI 前中断，则进入 `roi_resume`，只做本地 ROI 计算。SMTP 与本地 manifest 无法组成同一事务，因此远端已接收但本地尚未记账的极端崩溃窗仍按 at-least-once 语义处理，可能重复投递但不会静默漏发。

恢复型 preflight 也按需要缩小范围：`delivery_resume` 只检查本地目录、运行锁和本次确实需要的邮件配置；原本就是本地报告的任务不会因缺少邮件配置卡住。`roi_resume` / `p9_done` 只检查本地目录与运行锁，不会被无关的模型或搜索接口阻塞。

---

## 输出目录

当日输出写入：

```text
每日热点输出\
└── YYYYMMDD\
    ├── run_manifest.json
    ├── logs\
    │   ├── pipeline.log
    │   └── error.log
    ├── meta\
    └── 各阶段 md/json 产物
```

说明：

- `run_manifest.json` 是排查运行状态的第一入口。
- `logs/pipeline.log` 记录主流程日志。
- `logs/error.log` 记录错误日志。
- `meta/` 存放 `Layer_ROI` 等辅助元数据。

---

## Preflight / Doctor 检查项

控制面会检查：

- 输出根目录、当日目录和日志目录
- `prompts/` 目录和关键 prompt 文件
- `ANTIGRAVITY_API_KEY`
- P6/P9 主备模型共用的 `PRO_OPUS_BASE_URL` 端点连通性
- `GROK_API_KEY`
- Gemini 搜索 key 池数量
- 关键依赖可导入状态
- 日历库模式和版本
- TLS 白名单配置
- run lock 是否占用

`doctor --strict` 和 `preflight` 会把 warning 升级为失败，更适合计划任务与发布前检查。

---

## 抓取与网络安全

默认策略：

- `requests` 默认 `verify=True`。
- 不全局关闭 `InsecureRequestWarning`。
- 只有 `INSECURE_TLS_ALLOWLIST` 命中的域名允许不安全 TLS。
- `NO_PROXY` 会合并现有值，并补齐 `127.0.0.1,localhost,::1`，避免本地反代被代理误拦。

相关入口：

- `settings.py`
- `http_security.py`
- `scraper_utils.py`

---

## 验证

代码级验证：

```powershell
uv lock --check
uv run ruff check .\每日战报
uv run python -m compileall -q .\每日战报
uv run pytest -q
```

快速离线回归可优先跑：

```powershell
uv run pytest .\每日战报\tests\test_simulated_pipeline.py -q
```

这些测试不依赖真实邮件发送、真实网页采集或 live 模型调用，适合验证控制面、调度、输出和关键拼接逻辑。
