# VCP Hunter 北美 AI 产业链情报战报系统

> 按 A 股交易日运行的多阶段情报流水线：采集北美 AI 产业链信息，审计证据，映射 A 股受益方向，生成战报并邮件分发。

Last verified: 2026-07-10

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
| `TDX_ROOT` | 本地通达信数据根，默认 `C:\zd_huatai`；需包含 `T0002\hq_cache` 和 `vipdoc` |
| `ANTIGRAVITY_BASE_URL` | 本地/远端 OpenAI 兼容模型服务地址，默认 `http://127.0.0.1:8045` |
| `ANTIGRAVITY_API_KEY` | Pro/GPT-5.6 Sol/Agent 兼容模型鉴权 |
| `PRO_OPUS_BASE_URL` / `PRO_OPUS_API_KEY` | 覆盖 P6/P9 GPT-5.6 Sol Max 模型服务；未配置时沿用 Antigravity 配置 |
| `GEMINI_SEARCH_API_KEYS` | Gemini 搜索 key 池，逗号分隔 |
| `GEMINI_SEARCH_BASE_URL` | Gemini 搜索代理地址 |
| `AUDIT_MODEL` | P5/P7a/P7.5 审计模型，默认 `claude-sonnet-4-6`；兼容旧变量 `GEMINI_PRO_MODEL` |
| `GEMINI_PRO_API_KEY` | Gemini 审计模型专用 key；仅当 `AUDIT_MODEL` 使用 Gemini 3.5 时生效 |
| `TAVILY_API_KEY` | Tavily 备用搜索 |
| `GROK_API_KEY` | P2 X/Grok 情报采集 |
| `GROK_MODEL` | P2 Grok 模型，默认 `grok-4.3` |
| `GROK_REASONING_EFFORT` | P2 Grok 推理强度，默认 `none` 降低推理 token 成本 |
| `GROK_MAX_OUTPUT_TOKENS` | P2 Grok 单批输出上限，默认 `4096` |
| `GROK_NATIVE_REASONING_EFFORT` | 通用 Grok adapter 推理强度覆盖，默认沿用 `GROK_REASONING_EFFORT` |
| `GROK_NATIVE_MAX_OUTPUT_TOKENS` | 通用 Grok adapter 输出上限，默认 `4096` |
| `GROK_NATIVE_ENABLE_X_SEARCH` | 通用 Grok adapter 是否挂载 `x_search`，默认 `false` |
| `EMAIL_ADDRESS` / `EMAIL_PASSWORD` | SMTP 发件账号和授权码 |
| `RECEIVER_EMAIL` | 收件人，多个邮箱用逗号分隔 |
| `INSECURE_TLS_ALLOWLIST` | 允许降级为不安全 TLS 的域名白名单，默认空 |
| `AGENT_ASSIST_ENABLED` / `AGENT_ASSIST_MODEL` | 可选 Agent 诊断/审校助手 |

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
| `run` | 正常从头运行 |
| `resume` | 基于 checkpoint 尝试恢复 |
| `force-run` | 传入 `--force-run`，跳过 orchestrator 内部的交易日跳过逻辑 |
| `restart` | 传入 `--force-restart`，清理恢复状态后重跑 |
| `status` | 查看当日输出、manifest、run lock 和阶段状态 |

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

脚本会导出原任务和 ACL，先禁用原触发器并取得流水线维护锁，再创建强制密码、随机密码的专用标准账户。源码和代理程序仅授予读取/执行，输出目录可写；`C:\zd_huatai` 通过专用账号的 deny-write ACE 保持有效只读，不改变其他用户现有权限。`CLIProxyAPI` 目标目录在复制任何凭据前即锁定为私有 ACL，仅 `auths` 子目录允许专用账号修改。

迁移依次完成 8318 隔离 canary、8317 live canary、正式 `cmd.exe` Action 的 `preflight` 实跑，最后才替换正式任务主体。回滚只删除本次创建的资源，并恢复本次实际修改的任务、ACL 和桌面代理。

```powershell
.\ops\migrate_scheduled_task.ps1 -Apply -ConfirmCredentialsRotated
```

服务账户 canary 会验证机器级 Python、临时目录、输出写入、本地 TDX 读取与写拒绝、严格 preflight、真实低 token 模型推理、Gemini/xAI 鉴权，以及 SMTP TLS 登录。SMTP 探针只登录不发信；迁移过程不会生成正式战报。

---

## 流水线阶段

控制层当前阶段：

| 控制阶段 | 主要内容 | 默认超时 |
| --- | --- | --- |
| `collect_intel` | P1 RSS/Web、P2 X/Grok、P3 机动搜索与 checkpoint 恢复 | 900s |
| `audit_evidence` | P5 证据审计与 checkpoint 复用 | 480s |
| `market_mapping` | P4 海外龙头行情快照、P6 A 股映射 | 900s |
| `research` | P7 深度研究、P7a 审计、P7.5 补充搜索 | 900s |
| `technical` | P8 技术面/股价/弹性层 | 300s |
| `report` | P9 战报生成、Agent 审校、邮件发送 | 900s |
| `roi` | Layer ROI 贡献评估 | 180s |

阶段由 `pipeline_runtime.py` 在子进程中执行。超时会写入 `failed_timeout`，异常会写入 `failed_exception`，成功写入 `completed`。

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
    ├── agent\
    ├── meta\
    └── 各阶段 md/json 产物
```

说明：

- `run_manifest.json` 是排查运行状态的第一入口。
- `logs/pipeline.log` 记录主流程日志。
- `logs/error.log` 记录错误日志。
- `agent/` 存放预检解释、失败诊断和终稿审校产物。
- `meta/` 存放 `Layer_ROI` 等辅助元数据。

---

## Preflight / Doctor 检查项

控制面会检查：

- 输出根目录、当日目录和日志目录
- `prompts/` 目录和关键 prompt 文件
- `ANTIGRAVITY_API_KEY`
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
