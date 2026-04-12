# VCP Hunter 北美 AI 产业链情报战报系统

> 一个按交易日运行的多阶段情报流水线：采集、审计、映射、研究、战报生成与邮件分发。

---

## 当前状态

截至 2026-04-13，项目已经补上这一轮工程化硬化：

- 控制层 `timeout_seconds` 已真正生效，超时会把阶段标记为 `failed_timeout`
- HTTPS 默认严格校验，只有白名单域名允许降级为不安全 TLS
- 时间解析失败不再默认放行，新增 `parse_failed` / `unknown_time` 标记
- `doctor` / `preflight` 可做运行前自检，并返回稳定退出码
- `run_manifest.json` 新增运行时上下文与自检摘要，便于审计
- `NO_PROXY` 改为 merge，不再直接覆盖现有环境

---

## 架构概览

```text
pipeline_cli.py
  ├── doctor / preflight / run / resume / restart / clean / prune
  ├── preflight 结果写入环境，供 orchestrator 落盘
  └── run lock 保护，防止重复执行

orchestrator.py
  ├── 编排 P1 → P9 主流程
  ├── 汇总阶段指标与上下文
  └── 统一写 manifest / checkpoint / 输出目录

pipeline_runtime.py
  ├── StageSpec / run_stage_registry
  ├── 子进程级阶段执行
  └── timeout / failed_timeout / metrics 落盘

数据采集与处理
  ├── p1_rss.py / p1_web.py / scraper_utils.py
  ├── search_engine.py / p2_x_intel.py / p3_agent.py / p4_vanguard.py
  ├── audit_engine.py / p6_mapper.py / p7_deep_research.py
  └── p8_technical.py / p9_reporter.py / p9_email.py

运行基础设施
  ├── settings.py / trading_calendar.py / pipeline_lock.py
  ├── artifact_store.py / checkpoint.py / pipeline_maintenance.py
  └── native_llm_adapter.py / http_security.py
```

---

## 关键模块

| 文件 | 作用 |
| --- | --- |
| `orchestrator.py` | 主流程编排、阶段衔接、全局异常收口 |
| `pipeline_cli.py` | 控制面入口，自检、运行、清理、状态查看 |
| `pipeline_runtime.py` | 阶段执行框架、超时控制、阶段级 manifest 写入 |
| `pipeline_lock.py` | 运行锁，防止并发重复启动 |
| `artifact_store.py` | manifest、日志目录、输出目录、上下文落盘 |
| `settings.py` | 环境变量、模型 key、TLS 白名单、NO_PROXY 合并 |
| `trading_calendar.py` | 交易日判断与日历模式探测 |
| `p1_web.py` | 网页发现、时效窗口判定、来源元数据标记 |
| `scraper_utils.py` | 正文抓取、URL 过滤、TLS 安全策略应用 |
| `audit_engine.py` | 审计与清洗 |
| `search_engine.py` | 搜索、限流、降级与 key 轮换 |
| `native_llm_adapter.py` | 多模型统一适配 |
| `p9_reporter.py` | 战报总装配 |
| `p9_email.py` | HTML 邮件渲染与发送 |

---

## 快速开始

### 1. 安装依赖

```bash
cd 每日战报
pip install -r 每日战报/requirements.txt
```

### 2. 配置 `.env`

复制 `每日战报/.env.example` 为 `每日战报/.env`，填入实际密钥。

至少建议检查：

- `ANTIGRAVITY_API_KEY`
- `GEMINI_SEARCH_API_KEYS`
- `GROK_API_KEY`
- `EMAIL_ADDRESS`
- `EMAIL_PASSWORD`
- `RECEIVER_EMAIL`

### 3. 先跑自检

```bash
python 每日战报/pipeline_cli.py doctor
python 每日战报/pipeline_cli.py doctor --strict
python 每日战报/pipeline_cli.py preflight
```

退出码约定：

- `0`：通过
- `1`：检查失败
- `2`：发现活动 run lock

### 4. 启动主流程

```bash
python 每日战报/pipeline_cli.py run
python 每日战报/pipeline_cli.py resume
python 每日战报/pipeline_cli.py force-run
python 每日战报/pipeline_cli.py restart
python 每日战报/pipeline_cli.py status
```

仍需兼容旧入口时，也可以直接运行：

```bash
python 每日战报/orchestrator.py
```

但推荐统一走 `pipeline_cli.py`。

### 5. 清理缓存与历史瞬态产物

```bash
python 每日战报/pipeline_cli.py clean
python 每日战报/pipeline_cli.py prune --keep-days 7
```

### 6. 测试

```bash
python -m compileall 每日战报
python -m pytest 每日战报/tests -q
```

---

## Preflight / Doctor 检查项

当前控制面会检查以下项目：

- 输出根目录、当日目录、日志目录
- `prompts/` 与关键 prompt 文件
- `ANTIGRAVITY_API_KEY` / `GROK_API_KEY`
- 搜索 key 池数量
- 关键依赖可导入状态
- 日历库模式与探测结果
- TLS 白名单配置
- run lock 是否占用

`doctor` 为普通检查。

`doctor --strict` / `preflight` 为严格检查：

- warnings 会升级为失败
- 活动 run lock 会返回退出码 `2`
- 更适合计划任务和外层调度器

---

## `run_daily.bat`

根目录的 `run_daily.bat` 用于 Windows 计划任务或手工双击运行。

默认行为：

- 自动定位项目内 `pipeline_cli.py`
- 使用 `.venv\Scripts\python.exe`
- merge 现有 `NO_PROXY`，并补齐 `127.0.0.1,localhost,::1`
- 运行结束后按 `RUN_DAILY_HOLD_SECONDS` 保持窗口

可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_DAILY_PREFLIGHT` | `off` | `off`/`0` 跳过；`doctor` 先跑普通自检；`strict` 或 `preflight` 先跑严格自检 |
| `RUN_DAILY_HOLD_SECONDS` | `15` | 运行结束后保留窗口秒数，设为 `0` 可立即退出 |

示例：

```bat
set RUN_DAILY_PREFLIGHT=strict
run_daily.bat
```

---

## TLS / 抓取安全策略

默认策略：

- 所有 `requests` 抓取默认 `verify=True`
- 不再全局关闭 `InsecureRequestWarning`
- 只有白名单域名允许不安全 TLS

相关配置：

- 环境变量：`INSECURE_TLS_ALLOWLIST`
- 代码入口：`settings.py` / `http_security.py`

抓取结果和日志中会带：

- `tls_mode`
- `insecure_fetch`

---

## manifest 与可审计性

`run_manifest.json` 继续兼容原有 `stages` 结构，同时增加了根级上下文：

- `manifest_version`
- `runtime_context.python_version`
- `runtime_context.platform`
- `runtime_context.cwd`
- `runtime_context.calendar_mode`
- `runtime_context.search_keys_count`
- `runtime_context.tls_policy`
- `preflight`

阶段级状态现在可以区分：

- `completed`
- `failed_exception`
- `failed_timeout`

---

## 时间窗口策略

`p1_web.py` 当前对发布时间分三类处理：

- `recent`：解析成功且在时间窗内，允许进入下游
- `unknown_time`：缺失时间字段，会保留但显式标记
- `parse_failed`：时间解析失败，默认不放行

这保证了旧闻和脏时间字段不会再静默混入。

---

## 日历依赖命名说明

这里有三个名字，含义不同：

- PyPI 包名：`chinesecalendar`
- Python import 名：`chinese_calendar`
- manifest / 运行模式字段：`chinese_calendar`

安装时请使用：

```bash
pip install chinesecalendar
```

代码中使用：

```python
import chinese_calendar
```

---

## 输出目录

主输出仍保持兼容：

```text
每日热点输出/
  └── YYYYMMDD/
      ├── run_manifest.json
      ├── logs/
      ├── checkpoint/
      ├── output/
      └── 各阶段 md/json 产物
```

本轮整改没有改动 `manifest` / `checkpoint` / `output` 的既有目录约定。
