# 项目说明：InferenceLab Agent

> 面向简历/面试的项目描述。写法参照 `个人简历（大模型推理框架实习生）` 的条目风格：先给技术栈，再给功能与实现路径，最后给可核对的结果指标。
> 文中数字均可在仓库内复现或追溯到对应命令，未做估算。

## 1. 项目定位

**InferenceLab Agent —— 大模型推理实验的分析、判据与验证工作台。**

部署侧（4090 + Qwen3.6-27B + AWQ 4-bit + vLLM）产出 benchmark 与日志后，工作台负责：把实验产物**确定性复算**成指标、给出**通过/不通过**判据、对同 benchmark 的多次实验做**收益回归**检测，并输出可追溯的报告与 PDF。它解决的是"实验做完了，但结论不可复现、无法横向比较、说不清有没有退化"的问题。

一句话：**数值由代码算，模型只负责解释；每次实验都有判据、有基线、有报告。**

## 2. 技术栈

| 层次 | 选型 |
|---|---|
| 语言/后端 | Python 3.12、FastAPI、Pydantic v2、pydantic-settings、LangGraph、SQLite（WAL）、asyncio、numpy、reportlab |
| 模型接入 | OpenAI 兼容（DeepSeek / vLLM / Ollama / LM Studio / 通义）、Anthropic Messages、Gemini；SSE 流式、usage 统计、预算与降级 |
| 前端 | Vue 3、TypeScript、Vite、手写 hash 路由、手写 SSE 读取、纯 SVG 折线图（无图表库） |
| 工程与安全 | uv、pytest（128 用例）、ruff、gitleaks、pre-commit hooks、`.env` + `SecretStr`、密钥扫描器 |
| 观测 | `llm_calls` / `node_timings` / `runs.result_summary`、Prometheus 文本端点、时间分桶趋势 |
| 评测对象 | vLLM、AWQ 4-bit、Automatic Prefix Caching、Chunked Prefill（部署侧实验，数据经上传导入复算） |

## 3. 实现功能

1. **三类任务闭环**：实验分析（benchmark JSON/CSV、Prometheus 文本）、技术研究（本地 BM25 + 引用校验）、受控验证（registry 白名单 + 人工审批哈希）。
2. **确定性指标**：按 `(并发, 输入长度, 输出长度)` 分组，复算总输出吞吐、TTFT P50/P95、TPOT、错误率，并只在同负载组之间做对照。
3. **可追溯报告**：Markdown + PDF，包含数据概况、分组指标、同负载对照、判据结论、模型解释、结论边界与附录；每条证据带来源文件与截断标记。
4. **模型调用观测**：每次调用记录首 token / 首正文 TTFT、decode 速率、prompt/completion/reasoning tokens、是否流式、outcome，并汇总成监控页与 Prometheus。
5. **判据与回归**：吞吐提升、TTFT 降低、错误率上限三项阈值判定；以"上传文件 sha256 + 任务类型"为 benchmark 键，自动挂接最近成功基线，收益下滑超过容忍值标记回归。
6. **并行执行**：N 个 worker 共享有界队列，`analysis` 走 GPU 闸门（默认 1，串行独占），`research/validation` 走 CPU 闸门（默认 2），每任务独立目录、独立预算。
7. **多厂商接入**：`LLM_PROFILE` 预设一键切换 DeepSeek/OpenAI/通义/vLLM/Ollama/LM Studio/Anthropic/Gemini，显式配置优先，健康探测按传输选路径与鉴权头。
8. **安全与隔离**：密钥只在后端 `.env`，模板占位符 + 预提交扫描 + gitleaks CI；上传类型/大小校验、路径穿越防护、任务级目录隔离。

## 4. 具体实现路径

### 4.1 编排：LangGraph 主图 + 三类子图

```text
validate → route(条件边) → analysis / research / validation 子图
        → explain（可选模型解释）→ persist（报告、结果 JSON、PDF、判据）
```

- 主图用 `StateGraph` 承载统一状态（上传、解析结果、指标、证据、解释、告警）；
- 子图独立编译，`analysis` 内部分 `parse → compute`，`research` 分 `retrieve → draft → verify`，`validation` 先挂起等审批再执行固定脚本；
- 每个节点写 `node_timings` 并发事件，失败节点保留错误上下文，报告仍可产出确定性部分。

### 4.2 指标：唯一数值来源

- 分组键：`(concurrency, input_tokens, output_tokens)`，避免不同负载混算；
- 总输出吞吐：`Σ output_tokens / (max(end_ts) − min(start_ts))`，按墙钟时间而非请求数平均；
- TTFT：`numpy.percentile(..., method="linear")` 取 P50/P95；
- TPOT：`(end − start − ttft) / (output_tokens − 1)`；
- 对照：只在同组键内配对，输出 `output_throughput_pct` 与 `ttft_p50_reduction_pct`；
- 基线挑选：优先 `baseline/base/control` 语义标签，避免按字母序误选。

### 4.3 事件与队列

- 事件**先落库再推送**：`run_events` 自增 `seq`，SSE 支持 `?after=seq` 续传，断线不丢状态；
- 有界队列 + `asyncio.to_thread` 执行同步图；进入 GPU/CPU 信号量前的时间计入 `queue_wait_ms`；
- 任务取消通过状态标记 + 节点边界检查实现，取消后事件流与产物保持可查。

### 4.4 模型调用与观测口径

- 流式优先：`stream=True` + `stream_options={"include_usage": True}`；网关不支持时降级为无 usage 流式，再降级非流式；
- **首 token** 与 **首正文** 分开计时：推理模型的思考 token 也会先到，两个口径都记录，避免把"思考完成"误当"首字延迟"；
- `decode_tokens_per_s = completion_tokens / stream_s`；`content_tokens_per_s = (completion − reasoning) / content_window`；
- `prefill_tokens_per_s_est = prompt_tokens / (ttft / 1000)`（估算值，字段名显式标注 est）；
- 非流式（如 Gemini）只记总耗时与 token，TTFT 留空，**不估算**；
- 推理模型输出被 `max_tokens` 截断时，按上限翻倍重试一次，仍空则报可操作的格式错误。

### 4.5 判据与回归

- benchmark 键 = 上传文件 `sha256` 排序 + 任务类型 → 32 位哈希；
- 判据从配置或 `options.criteria` 解析，产出 `checks[]`（阈值/实测/通过与否）；
- 状态优先级：判据未过 → `fail`；否则相对基线收益下滑 → `regression`；否则 `pass`；没有两组对照 → `not_applicable`；
- 回归 = 当前吞吐或 TTFT 收益 < 基线 − 容忍值（默认 5 个百分点），明细写入 `result_summary.evaluation.regression`。

### 4.6 前端：轻量、无重型依赖

- hash 路由 + 视图切换（无 vue-router）；运行详情用 SSE 增量刷新，列表用 2.5s 轮询；
- 折线图直接生成 SVG path（网格、双系列、悬停数值），避免引入图表库；
- 浅色主题通过 CSS 变量统一（`--ok/--warn/--bad/--brand`），单端口 `/app/` 由后端托管 `dist/`。

### 4.7 安全

- `.env` 双路径（`backend/.env`、仓库根 `.env`），字段用 `SecretStr`；
- `env_template.py` 生成占位符模板，`secret_scan.py` 拦截真实 key，pre-commit/pre-push + gitleaks CI 双层；
- `/api/meta`、`/api/llm/health` 只返回 `host:port`、掩码指纹、布尔值。

## 5. 优化指标与验证结果

### 5.1 指标口径

| 指标 | 口径 |
|---|---|
| 总输出吞吐（tok/s） | Σ output_tokens / 墙钟窗口 |
| TTFT P50 / P95 | 请求发出到首 token 的分位值 |
| TPOT（ms） | 首 token 之后每 token 平均耗时 |
| 对照收益 | 同 `(并发, 输入, 输出)` 组的候选 vs 基线 |
| 错误率 | error_count / request_count |
| 模型调用 TTFT | 首 token / 首正文两个口径，客户端测量（含网络） |

### 5.2 部署实验数据的复算结果（与简历项目一对应）

条件：单卡 RTX 4090、Qwen3.6-27B、AWQ 4-bit、vLLM；输入 ≈ 2.8K、输出 128、并发 4；baseline vs 共享前缀优化（APC）。

| 指标 | baseline | APC | 变化 |
|---|---|---|---|
| 总输出吞吐 | 74.94 tok/s | 108.95 tok/s | **+45.38%** |
| TTFT P50 | — | — | **−36.67%** |
| 错误率 | 0 | 0 | 0 |

工作台对同形状数据复算得到 `+45.383%` / `−36.661%`，与部署侧原始结论一致，判据判定为 `pass`。这一步的价值是：**同一份数据换人复算、换机器复算，结论不变**；且第二次实验会自动挂到第一次的基线，出现收益下滑会被标成 `regression`。

> 仓库内 `fixtures/bench_concurrency_sample.json` 是用于回归测试的合成校准数据（文件内已注明），真实实验数据由部署侧导入；指标计算链路与真实数据完全一致。
>
> 边界说明：本工作台**不包含** 4090 部署与 vLLM 调参脚本；部署侧产出 benchmark/日志后上传到工作台，由工作台完成复算、判据、基线与回归。两者是"部署实验 → 评测验证"的上下游关系。

### 5.3 工作台自身的观测结果（DeepSeek API 实测）

| 场景 | 首 token TTFT | 首正文 TTFT | decode |
|---|---|---|---|
| 分析任务（解释节点） | ≈ 814 ms | — | ≈ 212.9 tok/s |
| 研究任务（草稿节点） | ≈ 598 ms | ≈ 2293 ms | ≈ 286 tok/s |

- 研究任务的"首 token / 首正文"差值主要来自推理模型的思考 token，说明两个口径必须分开记录；
- 受控验证任务 0 次模型调用，验证了"离线也能出确定性结果"的设计；
- 并行实测：两个 research 任务同时处于 `running`（最大并行 2），两个 analysis 任务始终最多 1 个 `running`（GPU 闸门生效）。

### 5.4 工程与质量指标

| 项 | 结果 |
|---|---|
| 后端测试 | 128 passed（全部离线，不消耗 token） |
| 静态检查 | `ruff` 0 告警；`vue-tsc` 0 错误 |
| 前端构建 | CSS 11.41 kB / JS 111.22 kB（gzip 3.22 / 40.49 kB） |
| 密钥防线 | 模板占位符校验、工作区扫描 0 命中、git hook 拦截真实 key |
| 数据一致性 | 指标、报告、判据共用同一份计算结果，报告与 API 数值同源 |

## 6. 简历版（可直接粘贴）

**InferenceLab Agent —— 大模型推理实验分析与评测工作台**（个人项目，2026）

1. **确定性评测链路**：基于 LangGraph 编排"解析 → 指标复算 → 证据引用 → 判据 → 报告"五段式流程；按 `(并发, 输入, 输出)` 分组复算吞吐、TTFT P50/P95、TPOT 与错误率，只在同负载组间做对照，并对 4090 + Qwen3.6-27B + AWQ 4-bit 的部署数据复算出 **+45.38% 吞吐、−36.67% TTFT P50**，与部署侧结论一致。
2. **判据与回归**：设计"吞吐提升 / TTFT 降低 / 错误率上限"三项判据与任务级阈值覆盖；以文件 sha256 作为 benchmark 键自动挂接历史基线，收益下滑超过 5 个百分点标记 `regression`，把"跑完实验"推进到"能下结论、能防退化"。
3. **观测与并行**：流式调用区分首 token 与首正文 TTFT，记录 decode/正文速率与 token 结构；有界队列 + GPU/CPU 信号量实现"分析串行、研究并行"，实测两个 research 任务并发、analysis 始终单路；报告、监控页、Prometheus 三处指标同源。
4. **工程化**：FastAPI + SQLite(WAL) + SSE 续传 + Vue3/TS 前端（纯 SVG 折线、无图表库）；128 个离线测试、ruff/tsc/build 全绿；密钥仅存本地 `.env`，模板占位符 + 预提交扫描 + gitleaks CI 三层防护。

技术栈：Python、FastAPI、LangGraph、SQLite、asyncio、numpy、Vue3、TypeScript、Vite、SSE、Prometheus（评测对象：vLLM / AWQ）

## 7. 面试讲述要点

1. **为什么数值不让模型算**：模型只能解释，指标必须可复现；同一份 benchmark 换人复算结果一致，才敢写进报告。
2. **为什么区分首 token / 首正文**：推理模型先吐思考 token，如果只记"首个非空 chunk"，会把思考时间算进首字延迟，TTFT 被低估。
3. **为什么用 sha256 做 benchmark 键**：文件名会变、路径会变，内容不变才是同一个实验；这样回归比较不会因为重命名失效。
4. **为什么 analysis 串行、research 并行**：单卡 benchmark 并行会互相抢资源，指标失真；研究/验证是 IO 与 CPU 混合，适合并行。

## 8. 复现

```powershell
# 后端
cd backend
uv run python -m pytest -q                  # 128 passed
uv run python -m app.main                   # http://127.0.0.1:8010

# 前端（单端口演示）
cd ..\frontend
npm install && npm run build                # 打开 http://127.0.0.1:8010/app/

# 复算示例：上传 fixtures/bench_concurrency_sample.json 建 analysis 任务
# 详情页「判据与结论」可看到 +45.38% / −36.67% 与 pass 判定
```
