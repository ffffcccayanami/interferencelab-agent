# InferenceLab Agent

面向推理实验的本地工作台：**上传实验产物 → 确定性复算指标 → 证据引用 → 判据结论 → 报告/PDF**，并把模型调用与任务执行做成可观测、可审计的服务。

> 两条边界：数值只由代码算，模型只负责解释与成文；密钥只在本地 `.env`，仓库里只有占位符。

## 界面速览

- **任务页**：左侧新建任务（分析 / 研究 / 受控验证），右侧"运行中"面板实时显示阶段、耗时、模型调用数；下方只列最近 5 条任务，点"查看全部"进入子页。
- **任务详情**：总耗时 / 首 token / decode 速率 / token 四个 KPI，阶段耗时条形图，**判据与结论**卡片，模型调用明细表，产物下载，SSE 事件流。
- **监控页**：成功率、TTFT/decode 分位、折线趋势（TTFT、decode、任务量、耗时）、判据通过率与回归列表、节点 P95、失败码。
- **全部任务**：`#/runs`，最多 100 条的精简表格。
- 视觉：浅色主题、白卡片 + 柔和阴影、状态色（绿=通过/成功，琥珀=等待/回归，红=失败）。

## 快速启动

### 0. 依赖

- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)
- Node.js 20+（仅前端开发/构建需要）

### 1. 配置

```powershell
cd inferencelab-agent
copy .env.example .env
```

编辑 `.env`，最少填三项（以 DeepSeek 为例）：

```dotenv
LLM_PROFILE=LLM_MODEL
LLM_MODEL_ID=YOUR_LLM_MODEL
LLM_API_KEY=YOUR_API_KEY
```

> 本地 vLLM/Ollama 不需要 key，用 `LLM_PROFILE=vllm` / `ollama` 即可；没有模型也能跑（`LLM_ENABLED=false`，只是不生成模型解释）。

### 2. 启动后端

```powershell
cd backend
uv run python -m app.main
```

默认 `http://127.0.0.1:8010`，接口文档在 `/docs`。启动前可自检：

```powershell
uv run python ..\scripts\doctor.py     # 配置 + 端点连通性（不消耗 token）
```

### 3. 启动前端

**单端口模式（推荐演示）**：

```powershell
cd frontend
npm install
npm run build          # 产出 dist/，后端自动挂到 /app/
```

然后直接打开 **`http://127.0.0.1:8010/app/`**（访问 `/` 会自动 307 跳转）。

**开发模式（热更新）**：

```powershell
cd frontend
npm run dev            # http://127.0.0.1:5175，/api 已代理到 8010
```

## 环境配置

完整配置见 [`.env.example`](.env.example)；只列关键项。

### 模型接入

| 变量 | 说明 | 示例 |
|---|---|---|
| `LLM_PROFILE` | 部署预设，自动补 base_url/鉴权/健康路径/默认模型 | `deepseek`、`vllm`、`anthropic`、`gemini` |
| `LLM_PROVIDER` | 传输层，`auto` 由预设推导 | `openai` / `anthropic` / `gemini` |
| `LLM_MODEL_ID` | 模型 id，必须与服务端 `/models` 一致 | `YOUR_LLM_MODEL` |
| `LLM_BASE_URL` | 显式端点（覆盖预设） | `YOUR_LLM_BASE_URL` |
| `LLM_API_KEY` | 仅后端读取，前端永不可见 | `...` |
| `LLM_AUTH_MODE` | `auto` / `bearer` / `none` / `x-api-key` / `query` | `auto` |
| `LLM_DEPLOYMENT` | 展示用：`auto` / `local` / `tunnel` / `cloud` | `auto` |
| `LLM_MAX_OUTPUT_TOKENS` | 单次输出上限（推理模型思考也占预算） | `2048` |
| `LLM_STREAMING` | 流式（TTFT 依赖它） | `true` |

预设一览与排错见 **[docs/PROVIDERS.md](docs/PROVIDERS.md)**；三种部署形态（云端 / SSH 隧道 / 本机）见 **[docs/LLM_ENDPOINT.md](docs/LLM_ENDPOINT.md)**。

### 执行与判据

| 变量 | 默认 | 说明 |
|---|---|---|
| `INFL_WORKERS` | 3 | 总 worker 数 |
| `INFL_GPU_CONCURRENCY` | 1 | analysis 并发（默认串行，避免 benchmark 互相干扰） |
| `INFL_CPU_CONCURRENCY` | 2 | research / validation 并发 |
| `INFL_QUEUE_SIZE` | 4 | 队列上限，满了返回 429 |
| `INFL_EVAL_ENABLED` | true | 是否启用判据 |
| `INFL_EVAL_MIN_THROUGHPUT_PCT` | 0 | 吞吐提升下限（0=不得更慢） |
| `INFL_EVAL_MIN_TTFT_REDUCTION_PCT` | 0 | TTFT 降低下限 |
| `INFL_EVAL_MAX_ERROR_RATE` | 1.0 | 错误率上限（1.0=不限制） |
| `INFL_EVAL_REGRESSION_TOLERANCE_PCT` | 5 | 相对基线允许的收益下滑（百分点） |

判据语义、基线与回归规则见 **[docs/EVALUATION.md](docs/EVALUATION.md)**。


## 主要功能

### 1. 三类任务

| 任务 | 输入 | 产出 |
|---|---|---|
| **实验分析** | benchmark JSON/CSV 或 Prometheus 文本 | 分组指标、同负载对照、判据结论、报告/PDF |
| **技术研究** | `.md/.txt/.pdf` 文档 | 本地 BM25 检索证据 + 带引用校验的说明 |
| **受控验证** | registry 白名单条目 + 参数 | 人工审批后执行固定脚本，产出结果与日志 |

### 2. 数值可信

- 吞吐、TTFT 分位、错误率全部由 `services/metrics.py` 复算，模型不参与；
- 对照只在 `(并发, 输入长度, 输出长度)` 相同的组之间做；
- 样本不足会明确提示，不硬凑结论。

### 3. 判据与回归（P4）

- 每次分析任务给出 `pass / fail / regression / not_applicable`；
- 阈值可全局配置，也可在 `options.criteria` 里按任务覆盖；
- 同 benchmark（上传文件 sha256 集合）自动找基线，收益下滑超过容忍值标记回归；
- 报告新增"判据与结论"，监控页有通过率与回归列表。

### 4. 并行执行

- `analysis` 走 GPU 闸门（默认 1，串行独占），`research/validation` 走 CPU 闸门（默认 2）；
- 每任务独立目录、独立 SQLite 记录、独立预算，互不污染。

### 5. 观测

- 每次模型调用记录：首 token / 首正文 TTFT、decode 速率、prompt/completion/reasoning tokens、是否流式、outcome；
- 每个图节点记录耗时；`/api/monitor/summary`、`/api/monitor/trend`、`/api/metrics`（Prometheus）；
- 流式优先，失败自动降级为无 usage 流式 / 非流式，指标缺失时留空不估算。

### 6. 安全与隔离

- 上传类型/大小校验，路径穿越防护，任务级目录隔离；
- 受控验证必须人工审批，审批哈希绑定参数；
- 报告与事件中不出现密钥。

## 界面说明

| 页面 | 路由 | 内容 |
|---|---|---|
| 任务 | `#/` | 新建任务、运行中面板、最近 5 条任务 |
| 全部任务 | `#/runs` | 最近 100 条，含判据徽章 |
| 任务详情 | `#/runs/:id` | KPI、阶段耗时、判据、模型调用、产物、事件 |
| 报告 | `#/runs/:id/report` | Markdown 报告 + PDF 下载 |
| 监控 | `#/monitor` | 1h/24h/7d 窗口、趋势折线、判据与回归、节点 P95 |

状态色：绿=`succeeded`/判据通过；琥珀=`awaiting_approval`/回归；红=`failed`/判据未过；蓝=运行中/排队。

## 项目结构

```text
inferencelab-agent/
├─ backend/app/        FastAPI、LangGraph 子图、SQLite、LLM 客户端与观测
├─ backend/tests/      128 个离线用例
├─ frontend/src/       Vue 3 视图、SVG 折线、SSE 读取
├─ docs/               计划/架构/端点/监控/评测/提供商/安全
├─ fixtures/           离线演示数据
└─ scripts/            doctor、密钥扫描、模板生成、演示数据
```

模块职责、请求生命周期、数据表与"改哪里"索引见 **[docs/STRUCTURE.md](docs/STRUCTURE.md)**。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/uploads` | 上传文件（多部分表单） |
| POST | `/api/runs` | 创建任务（`task_type/question/upload_ids/options`） |
| GET | `/api/runs?limit=` | 任务列表 |
| GET | `/api/runs/{id}` | 详情（含节点耗时、模型调用、服务端指标） |
| GET | `/api/runs/{id}/events` | SSE 事件流（支持 `after`） |
| GET | `/api/runs/{id}/report` | Markdown 报告 |
| GET | `/api/runs/{id}/artifacts/{aid}` | 下载产物 |
| POST | `/api/runs/{id}/approve` | 受控验证审批 |
| POST | `/api/runs/{id}/cancel` | 取消任务 |
| GET | `/api/meta` | 配置摘要（含 provider/deployment/并发） |
| GET | `/api/llm/health` | 模型端点探测（不消耗 token） |
| GET | `/api/monitor/summary?hours=` | 监控聚合（含判据统计） |
| GET | `/api/monitor/trend?hours=&buckets=` | 折线趋势数据 |
| GET | `/api/metrics` | Prometheus 文本 |

## 测试与质量门

```powershell
cd backend
uv run ruff check .
uv run python -m pytest -q            # 128 passed，全部离线
cd ..\frontend
npm run typecheck
npm run build
```

覆盖：解析/指标/检索/报告、LLM 流式与降级、三厂商传输、判据与回归、队列并发、审批、隔离、密钥卫生、静态挂载。

## 演示数据

```powershell
uv run python scripts\make_fixtures.py
```

`fixtures/` 自带：`bench_concurrency_sample.json`（baseline vs apc）、`bench_single_config.csv`、`bench_uneven_samples.json`、`prometheus_sample.prom`、三篇说明文档。演示脚本见 **[docs/DEMO.md](docs/DEMO.md)**。

## 文档

- [docs/PLAN.md](docs/PLAN.md)：主线计划与阶段状态
- [docs/PROJECT_RESUME.md](docs/PROJECT_RESUME.md)：项目说明（简历版：技术栈/功能/实现路径/指标）
- [docs/STRUCTURE.md](docs/STRUCTURE.md)：项目结构与模块职责
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：架构与设计取舍
- [docs/PROVIDERS.md](docs/PROVIDERS.md)：厂商/本地部署接入
- [docs/LLM_ENDPOINT.md](docs/LLM_ENDPOINT.md)：端点与隧道
- [docs/MONITORING.md](docs/MONITORING.md)：观测指标
- [docs/EVALUATION.md](docs/EVALUATION.md)：判据与回归
- [docs/SECURITY.md](docs/SECURITY.md)：安全模型
- [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md)：密钥轮换

## 已知边界

- 单用户本地工具，不做多租户与公网暴露；
- 非流式传输（Gemini）不提供 TTFT；
- 判据是业务阈值判定，不是统计显著性检验；
- 受控验证只执行 registry 注册过的脚本与参数。

## License

MIT，见 [LICENSE](LICENSE)。
