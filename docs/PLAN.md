# InferenceLab Agent 主线计划

> 本文件是项目的**唯一主线计划**：记录已完成能力、当前阶段、后续阶段与验收口径。
> 历史编号（P0–P5）与主线编号（P1–P5）对照见第 2 节。

## 1. 目标

面向推理实验的本地工作台：**上传实验产物 → 确定性复算指标 → 证据引用 → 报告/PDF**，并把"模型调用"与"任务执行"做成可观测、可审计的服务。

两条不可妥协的边界：

1. **数值只由代码算**，模型只做解释与成文；
2. **密钥只在本地 `.env`**，仓库只保留占位符，提交/CI 双层拦截。

## 2. 阶段总览

| 主线 | 内容 | 状态 | 历史编号 |
|---|---|---|---|
| **P1 工程基座** | FastAPI + SQLite + 有界队列 + SSE + 隔离 + 密钥防线（钩子/CI/扫描器） | ✅ 完成 | P0 + P1 |
| **P2 闭环与观测** | 三类任务、报告与 PDF、真实模型接入、流式 TTFT、监控页、并行执行、浅色 UI | ✅ 完成 | P2 + P5 |
| **P3 模型接入兼容** | 厂商 API（OpenAI 兼容 / Anthropic / Gemini）+ 本地/隧道/云端部署预设 | ✅ 完成 | — |
| **P4 评测与判据** | benchmark 模板与"通过/不通过"判据、跨运行基线、回归检测 | ✅ 完成 | — |
| **P5 运维加固** | 配额与审计、备份/恢复、服务端指标联动 | ⬜ 待开始 | — |

## 3. 已完成能力（P1–P2 摘要）

**执行层**

- 三类任务：`analysis`（benchmark JSON/CSV、Prometheus 文本）、`research`（本地 BM25 + 引用校验）、`validation`（registry 白名单 + 审批哈希）；
- 队列：`INFL_WORKERS` 个 worker，`analysis` 走 GPU 闸门（默认 1，串行独占），`research/validation` 走 CPU 闸门（默认 2）；
- 隔离：每任务独立目录、独立 SQLite 记录、独立 `Budget` 与 `LLMClient`；
- 事件：事件先落库再推 SSE，支持 `?after=seq` 续传，事件带 `node` 字段。

**观测层**

- `llm_calls`：TTFT（首 token / 首正文）、decode/正文速率、prompt/completion/reasoning tokens、outcome；
- `node_timings`：节点状态与耗时；`runs.result_summary`：任务结果摘要；
- `/api/monitor/summary`、`/api/monitor/trend`、`/api/metrics`（Prometheus）、`/api/llm/health`（免费探测）。

**界面层**

- 浅色主题、侧边栏、KPI 卡片、阶段耗时条、折线趋势（TTFT / decode / 任务量 / 耗时）；
- 首页只展示最近 5 条任务与运行中面板，`#/runs` 为全部任务子页，`#/monitor` 为监控页。

**安全层**

- `.env` 双路径（backend / 仓库根）读取，`.gitignore` + 模板生成 + 预提交钩子 + gitleaks CI；
- `/api/meta`、`/api/llm/health` 不返回密钥，仅返回 `host:port`、掩码指纹与布尔。

## 4. P3：模型接入兼容（已完成）

### 4.1 目标

同一套任务/观测代码，既能接**厂商托管 API**，也能接**本地或云端部署的推理服务**，切换只改配置。

### 4.2 支持矩阵

| 传输 | 覆盖的部署形态 | 鉴权 | 流式 | TTFT |
|---|---|---|---|---|
| `openai`（OpenAI 兼容） | DeepSeek、OpenAI、通义兼容模式、vLLM、Ollama、LM Studio、OpenRouter 等 | Bearer / 无 | ✅ | ✅ |
| `anthropic` | Anthropic Messages API | `x-api-key` + `anthropic-version` | ✅ | ✅ |
| `gemini` | Google Gemini `generateContent` | `key` 查询参数 | ⬜（非流式） | ⬜（记总耗时/token） |

### 4.3 配置模型

- `LLM_PROFILE`：部署预设（`deepseek` / `openai` / `qwen` / `vllm` / `ollama` / `lmstudio` / `anthropic` / `gemini`），提供默认 `base_url`、鉴权方式、健康检查路径、默认模型；
- `LLM_PROVIDER`：传输类型，缺省由 profile 推导；
- `LLM_AUTH_MODE`：`auto`（按 profile）/ `bearer` / `none` / `x-api-key` / `query`；
- 显式变量永远覆盖预设，便于"预设 + 私有网关"混用；
- `LLM_DEPLOYMENT`：`auto`（按 base_url 推导 local/cloud）/ `local` / `tunnel` / `cloud`，仅用于展示与文档。

### 4.4 观测一致性

- 三种传输都写同一张 `llm_calls` 表、同一套字段；
- 流式传输记录 TTFT（首 token / 首正文）与 decode 速率；非流式记录总耗时与 token，TTFT 为 `null`（不估算）；
- `/api/llm/health` 按传输选择探测路径与鉴权头，返回 `provider / profile / deployment / streaming_supported`。

### 4.5 完成记录

| 项 | 落地内容 |
|---|---|
| 预设 | `backend/app/services/providers.py`：8 个预设，含 base_url、鉴权、健康路径、部署形态 |
| 配置 | `LLM_PROFILE` / `LLM_PROVIDER` / `LLM_DEPLOYMENT` / `LLM_AUTH_MODE=auto`；显式变量优先，冲突时报错 |
| 传输 | OpenAI 兼容沿用原流式管线；新增 Anthropic Messages（SSE 解析）与 Gemini generateContent（非流式） |
| 探测 | `/api/llm/health` 按传输选择路径与鉴权头；前端侧边栏显示 `provider · deployment · auth` |
| 测试 | `tests/test_providers.py` 12 个用例；全量 **120** 通过 |
| 文档 | `docs/PROVIDERS.md`（矩阵、快速配置、优先级、排错） |

### 4.6 验收结果

1. `LLM_PROFILE=deepseek` 只填 key 即可运行（base_url/auth/model 由预设补齐）——✅ 单测覆盖；
2. `LLM_PROFILE=vllm` 无 key 可运行，鉴权自动为 `none`——✅；
3. `LLM_PROFILE=anthropic` 走 Messages API，流式写入 TTFT 与 token——✅（流式解析 + 回退非流式）；
4. `LLM_PROFILE=gemini` 非流式成功，`streamed=false`、TTFT `null`、token 正确——✅；
5. 显式 `LLM_BASE_URL` 覆盖预设；provider 与 profile 冲突报配置错误——✅；
6. 全量测试、`ruff`、`vue-tsc`、`vite build` 通过——✅。

## 5. P4：评测与判据（已完成）

### 5.1 目标

把"能算指标"推进到"能下结论"：每次分析任务给出通过/不通过，并与同 benchmark 的上一次运行比较，发现收益回归。

### 5.2 判据

- 吞吐提升 ≥ `INFL_EVAL_MIN_THROUGHPUT_PCT`（默认 0，即不得更慢）；
- TTFT P50 降低 ≥ `INFL_EVAL_MIN_TTFT_REDUCTION_PCT`（默认 0）；
- 最大错误率 ≤ `INFL_EVAL_MAX_ERROR_RATE`（默认 1.0，即不限制）；
- 阈值可在创建任务时用 `options.criteria` 覆盖；
- 没有两组对照 / 非分析任务 / 关闭评测 → `not_applicable`。

### 5.3 基线与回归

- benchmark 键 = 上传文件 sha256 排序 + 任务类型；
- 基线 = 同键、成功、有结果摘要的最近一次运行；
- 回归 = 当前吞吐提升或 TTFT 降低幅度低于"基线 − 容忍值"（默认 5 个百分点）；
- 状态优先级：判据未过 → `fail`；否则收益下滑 → `regression`；否则 `pass`。

### 5.4 完成记录

| 项 | 落地内容 |
|---|---|
| 服务 | `services/evaluation.py`：`benchmark_key` / `resolve_criteria` / `evaluate_metrics` / `detect_regression` / `build_evaluation` |
| 数据 | `runs.eval_status / benchmark_key / baseline_run_id`（含幂等迁移），`result_summary.evaluation` |
| 报告 | `## 5. 判据与结论`：检查表、结论、基线、回归明细 |
| 监控 | `runs.evaluation.{counts, evaluated, pass_rate, regressions}`，监控页"判据与回归"面板 |
| 界面 | 列表/全部任务页判据徽章；详情页"判据与结论"卡片（阈值/实测/通过与否/基线） |
| 配置 | `INFL_EVAL_*` 五项，已写入 `.env.example` |
| 测试 | `tests/test_evaluation.py` 8 个用例；全量 **128** 通过 |
| 文档 | `docs/EVALUATION.md`、`docs/STRUCTURE.md` |

### 5.5 验收结果

- 两次同 benchmark 分析：第一次 `eval_status=pass`、无基线；第二次自动挂到第一次基线上；✅
- 报告包含"判据与结论"；监控页显示通过率 1.0、pass=2；✅
- 阈值覆盖、错误率判据、回归检测、benchmark 键稳定性均有单测；✅
- `ruff`、`vue-tsc`、`vite build` 通过。✅

## 6. P5 概要（运维加固）

- 审计：操作日志（谁在何时跑了什么），本地单用户也保留；
- 备份/恢复：`var/` 目录快照与恢复脚本；
- 可选：vLLM `/metrics` 与客户端指标联动（已有采集骨架，补关联视图）。

## 7. 明确不做

- 不做多租户、不做公网暴露模型端口、不做自动训练/微调；
- 不把"使用推理框架"包装成"研发推理框架"；
- 不承诺非流式传输的 TTFT（无数据就不填）。
