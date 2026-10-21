# 架构说明

## 1. 任务状态机

```text
queued ──▶ running ──▶ succeeded
   │           │
   │           ├──▶ failed（含 error_code / error_message / error_details）
   │           ├──▶ cancelled（节点边界检查 cancel_requested）
   │           └──▶ awaiting_approval ──(approve)──▶ queued ──▶ running ──▶ ...
   └──▶ cancelled（排队阶段直接取消）

进程启动时：queued / running / awaiting_approval → interrupted
```

`interrupted` 明确表示"不知道任务做到哪里"，不提供断点恢复。

## 2. LangGraph 结构

主图 `app/graph/main_graph.py`：

| 节点 | 职责 |
|---|---|
| `validate_input` | 校验任务类型、问题、上传文件；校验取消标记 |
| `analysis`（子图） | `analysis_parse` 解析输入 → `analysis_compute` 配对 + 指标 |
| `research`（子图） | `research_ingest` 文档入库 → `research_retrieve` BM25 + 引用 |
| `validation`（子图） | `validation_select` 选 registry/校验参数/发审批 → `validation_execute` 执行 |
| `explain` | 调模型生成解释（research 走引用校验）；失败降级 |
| `persist` | 写 `report.md` / `result.json` / `report.pdf` 并登记产物 |

条件边：

- `validate_input → route`：按 `task_type` 分流；
- `validation → after_validation`：`awaiting_approval` 为真时直接 END（等待人工），否则进入 `explain`。

子图与主图共享 `RunState`（`app/graph/state.py`），只保存引用与结论，不保存整份日志。

## 3. 事件协议（SSE）

```text
id: 12
event: node_finished
data: {"node": "analysis_compute", "status": "ok", "duration_ms": 3.1}
```

事件类型：`run_status`、`node_started`、`node_finished`、`tool_call`、`metric_table`、`citation`、`approval_required`、`artifact_ready`、`warning`、`error`、`done`。

- 事件先写 `events` 表，再 `EventBus.notify()`；订阅者按 `seq` 增量读取；
- `GET /api/runs/{id}/events?after=<seq>` 用于断线续传；15s 无事件时发送 `: ping` 注释帧保活；
- 任务终态且无新事件时生成器返回，连接关闭。

## 4. 数据库

```sql
uploads(id, original_name, stored_path, size, sha256, content_type, detected_type, created_at)
runs(id, task_type, question, options_json, status, stage, error_code, error_message, error_details,
     approval_state, approval_id, approval_json, cancel_requested, degraded, warnings_json,
     run_dir, created_at, started_at, finished_at)
run_uploads(run_id, upload_id)
events(seq INTEGER PK AUTOINCREMENT, run_id, type, node, payload, created_at)
artifacts(id, run_id, kind, filename, rel_path, size, sha256, created_at)
```

`artifacts.rel_path` 是相对 run 目录的路径；下载时 `resolve()` 后必须仍在 run 目录内。

## 5. 指标口径

组键 = `(concurrency, input_tokens, output_tokens)`。组键相同才允许对照，并且每个 `config_label` 的样本数必须 `>= INFL_MIN_GROUP_SAMPLES`。

| 指标 | 公式 |
|---|---|
| `output_throughput` | `sum(output_tokens) / (max(end_ts) - min(start_ts))` |
| `ttft_ms_p50/p90/p95` | `numpy.percentile(ttft_ms, q, method="linear")` |
| `tpot_ms_mean` | `mean((end_ts - start_ts - ttft_ms/1000) * 1000 / (output_tokens - 1))` |
| `latency_ms_mean` | `mean((end_ts - start_ts) * 1000)` |
| `error_rate` | `error_count / request_count` |
| 对照差值 | `(candidate - baseline) / baseline * 100`（降低类用 `baseline - candidate`） |

基线选择：优先 `baseline/base/control/default` 等语义标签，否则取字典序第一个，避免出现 `apc → baseline` 的反向对照。

Prometheus 输入不参与请求级配对：仅输出标量指标与 `_bucket` 累积直方图的分位近似值（线性插值），并在报告中标注"近似"。

## 6. 错误码

| 码 | HTTP | 含义 |
|---|---|---|
| `UNSUPPORTED_FILE` | 415 / 任务失败 | 文件类型与任务不匹配 |
| `FILE_TOO_LARGE` | 413 | 超过 `INFL_MAX_UPLOAD_MB` |
| `PARSE_ERROR` | 任务失败 | 格式非法（缺列、非数值等） |
| `NON_PAIRWISE_INPUT` | 任务失败 | 负载不一致或样本不足，拒绝出对比 |
| `INSUFFICIENT_DATA` | 任务失败 | 没有可分析记录 |
| `NO_EVIDENCE_DOCUMENTS` | 任务失败 | 检索不到相关证据，拒绝作答 |
| `LLM_UNAVAILABLE` / `LLM_FORMAT_ERROR` | 降级 | 模型不可用或输出非 JSON：保留确定性结果 |
| `BUDGET_EXCEEDED` | 任务失败 | 调用次数/提示词长度超预算 |
| `APPROVAL_REQUIRED` / `APPROVAL_MISMATCH` | 409 | 未审批 / 审批参数被篡改 |
| `PATH_TRAVERSAL` | 403 | 产物路径越界 |
| `QUEUE_FULL` | 429 | 有界队列已满 |
| `CANCELLED` | — | 用户取消 |
| `VALIDATION_ERROR` | 400/422 | 请求或验证参数不合法 |
| `INTERNAL` | 500 | 未预期异常（已脱敏） |

## 7. 受控验证- `app/validation/registry.py` 是唯一入口：条目声明脚本名、参数 schema、超时；
- `approval_id = sha256(run_id | entry_id | canonical(params))`，审批与执行各校验一次；
- 执行：`[sys.executable, <registry 内脚本>, "--params", json]`，`shell=False`，`cwd=<run>/work`，环境变量白名单（不含 `LLM_*`），stdout/stderr 直接写文件（不走管道）；
- 非 0 退出或超时 → 任务 `failed`，保留 stderr 摘要。

## 8. 模型端点接入与探测

配置（`app/settings.py`）：

| 变量 | 作用 |
|---|---|
| `LLM_ENABLED` | 关闭后进入离线模式，任务仍完成但 `degraded=true` |
| `LLM_BASE_URL` | OpenAI 兼容根地址，需带 `/v1` 等前缀；末尾斜杠会被去掉 |
| `LLM_MODEL_ID` | 必须与端点 `/models` 返回的 `id` 一致（vLLM 用 `--served-model-name`） |
| `LLM_AUTH_MODE` | `bearer`（默认，带 Authorization）/ `none`（本机或隧道端点） |
| `LLM_HEALTH_PATH` | 探测路径，默认 `/models` |
| `LLM_HEALTH_TIMEOUT` | 探测超时，默认 5s（与生成用的 `LLM_TIMEOUT` 分开） |
| `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_PROMPT_CHARS` | 单任务预算 |

探测语义（`LLMClient.probe()`，`GET /api/llm/health`）：

- 只发一次 `GET`，**不产生 token 消耗**；
- `reachable=true` 表示"收到了 HTTP 响应"（包括 4xx/5xx），`status_code` 给出具体码；
- 连不上（隧道未开、端口错）时 `reachable=false`，`error` 为脱敏后的原因；
- `model_present` 表示 `LLM_MODEL_ID` 是否出现在 `/models` 列表中；端点不实现 `/models` 时为 `null`；
- 响应中不包含任何请求头或密钥。

预算与降级：

- 每次 `complete_json` / `complete_text` 前先 `Budget.spend()`，超限抛 `BUDGET_EXCEEDED`（任务失败，但确定性结果已落盘）；
- `LLM_UNAVAILABLE` / `LLM_FORMAT_ERROR` 只降级：写 `warning`、`degraded=true`，报告保留确定性章节；
- **推理模型**（deepseek-flash / R1 类）的思考 token 也计入 `max_tokens`：若 `content` 为空且 `finish_reason=length`，客户端把上限翻倍重试一次（上限由 `LLM_MAX_OUTPUT_TOKENS` 控制），仍为空才报 `LLM_FORMAT_ERROR`；
- 每次成功调用会记一行 token 用量日志（`prompt_tokens` / `completion_tokens`，不含提示词内容），便于核对账单。

## 9. 并行执行模型

```text
浏览器 ── 多次 POST /api/runs ──▶ 有界队列（INFL_QUEUE_SIZE）
                                     │
                    ┌────────────────┴────────────────┐
              INFL_WORKERS 个 worker（默认 3）
                    │                                  │
          run_class = "gpu"                    run_class = "cpu"
          （analysis）                          （research / validation）
                    │                                  │
      Semaphore(INFL_GPU_CONCURRENCY=1)     Semaphore(INFL_CPU_CONCURRENCY=2)
                    │                                  │
              串行执行 benchmark                    并行执行检索 / 验证
```

- 每个 `POST /api/runs` 按 `task_type` 决定资源类：`analysis → gpu`，其余 `→ cpu`；
- worker 取到任务后先进入对应闸门，再 `asyncio.to_thread` 执行图；闸门等待时间计入 `queue_wait_ms`；
- 默认 `gpu=1` 保证单卡 benchmark 串行独占，避免两个实验互相干扰；`cpu=2` 让研究与验证可以同时跑；
- SQLite 使用 WAL + `busy_timeout`，多 worker 并发写事件/指标仍安全（每次操作独立连接）；
- 每个任务独立的 `LLMClient` 与 `Budget`，并行任务不会互相消耗预算；
- `/api/metrics` 暴露 `inferencelab_runs_running` 与 `inferencelab_workers`；`/api/meta` 暴露 `workers / gpu_concurrency / cpu_concurrency` 供前端展示。

> 调参建议：单卡 24GB 保持 `INFL_GPU_CONCURRENCY=1`；CPU 并发取决于检索/验证脚本的资源占用，`INFL_WORKERS` 不小于 `gpu + cpu` 上限时排队最顺畅。
