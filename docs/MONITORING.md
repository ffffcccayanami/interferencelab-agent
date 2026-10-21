# 监控与指标

P5 之后，工作台记录两层指标：**模型调用**（prefill/decode 的实际表现）与**任务执行**（状态、节点耗时、产物、结果摘要）。所有数字来自 SQLite，抓取与查看都不触发模型调用。

## 1. 指标口径

### 1.1 客户端观测（主口径，每次模型调用一行 `llm_calls`）

| 指标 | 定义 | 说明 |
|---|---|---|
| `ttft_first_token_ms` | 请求发出 → 第一个非空 delta（正文或思考） | 含网络 + 排队 + prefill，是"首字延迟"的上界 |
| `ttft_first_content_ms` | 请求发出 → 第一个正文 delta | 推理模型下会明显大于上一项 |
| `total_ms` | 请求发出 → 流结束 | |
| `stream_ms` | 首 token → 最后一个正文/思考 delta | |
| `prompt_tokens` / `completion_tokens` / `reasoning_tokens` | 来自 `usage` | 网关不返回 `usage` 时全部为 `null`，不做估算 |
| `prefill_tokens_per_s_est` | `prompt_tokens / (ttft_first_token_ms/1000)` | **估算**，把网络与排队算进 prefill，只能横向自比 |
| `decode_tokens_per_s` | `completion_tokens / (stream_ms/1000)` | 含思考 token |
| `content_tokens_per_s` | `(completion − reasoning) / 正文窗口` | 更接近用户感知的正文速度 |
| `outcome` | `ok / truncated / error / empty` | 截断由"翻倍重试"逻辑判定 |

> 实测示例（deepseek-flash）：研究任务首 token 598 ms、首正文 2293 ms（reasoning 445 tokens）、decode 286 tok/s；分析任务首 token 814 ms、decode 213 tok/s。

### 1.2 服务端观测（可选，vLLM 类端点）

配置 `LLM_METRICS_URL` 后，任务开始/结束各抓一次 `/metrics`，保存快照并计算增量：`vllm:prompt_tokens_total`、`vllm:generation_tokens_total`、`vllm:time_to_first_token_seconds_*`、`vllm:request_prefill_time_seconds_*`、`vllm:request_decode_time_seconds_*`、`vllm:num_requests_running`、`vllm:gpu_cache_usage_perc`。

抓不到（401/404/超时）时任务不受影响，详情页显示 `server_metrics: unavailable`。公网网关通常不暴露 `/metrics`，建议只在 SSH 隧道内配置。

### 1.3 任务级

- `duration_ms`、`queue_wait_ms`、`llm_calls_count`；
- `node_timings`：每个节点的状态与耗时（`validate_input` / `analysis_parse` / … / `persist`）；
- `result_summary`：分析=组数/对照数/吞吐与 TTFT 变化；研究=文档数/引用数；验证=退出码/耗时。

## 2. 接口

| 接口 | 用途 |
|---|---|
| `GET /api/runs/{id}` | 增加 `duration_ms`、`queue_wait_ms`、`node_timings[]`、`llm_calls[]`、`result_summary`、`server_metrics` |
| `GET /api/runs` | 列表增加 `duration_ms`、`artifact_count`、`degraded`、`error_code` |
| `GET /api/monitor/summary?hours=24` | 窗口聚合：成功率、耗时/延迟分位、token、失败码、按节点 p95、慢任务、告警 |
| `GET /api/monitor/trend?hours=24&buckets=24` | 时间分桶序列（TTFT P50/P95、decode P50、任务数/失败数、耗时 P50），前端折线图数据源 |
| `GET /api/metrics` | Prometheus 文本（近 24h 窗口） |
| `GET /api/llm/health` | 端点可达性 + `streaming_supported` |

分位统一使用 `numpy.percentile(..., method="linear")`，与报告口径一致。

## 3. Prometheus

```text
inferencelab_llm_calls_total{outcome,node}
inferencelab_llm_tokens_total{kind="prompt|completion|reasoning"}
inferencelab_llm_ttft_seconds_bucket{le}        # 首 token
inferencelab_llm_call_duration_seconds_bucket{le}
inferencelab_llm_decode_tokens_per_second_bucket{le}
inferencelab_run_duration_seconds_bucket{le}
inferencelab_node_duration_seconds_bucket{node,status,le}
inferencelab_queue_pending / _capacity
inferencelab_llm_configured / inferencelab_llm_auth_mode{mode}
```

常用查询：

```promql
# 首 token P95（近 5 分钟）
histogram_quantile(0.95, sum(rate(inferencelab_llm_ttft_seconds_bucket[5m])) by (le))

# decode 速率 P50
histogram_quantile(0.50, sum(rate(inferencelab_llm_decode_tokens_per_second_bucket[5m])) by (le))

# 每分钟 token 消耗
sum(rate(inferencelab_llm_tokens_total[1m])) by (kind)

# 失败任务率
sum(inferencelab_runs_total{status="failed"}) / sum(inferencelab_runs_total)
```

## 4. 前端

- 任务列表：耗时、产物数、`degraded`、错误码；
- 任务详情：总耗时/排队、模型调用表（TTFT 两口径、decode、tokens、outcome）、节点耗时、结果摘要、服务端指标（若有）；
- 监控页（`#/monitor`）：窗口 1h/24h/7d、成功率、TTFT P50/P95、decode P50/P95、token 汇总、失败码、节点 p95、按节点模型调用、最慢任务、阈值告警；5s 轮询。

## 5. 告警（确定性，不做 LLM 判官）

```dotenv
INFL_MONITOR_WINDOW_HOURS=24
INFL_MONITOR_MAX_CALLS=2000
INFL_ALERT_TTFT_P95_MS=0        # 0 = 关闭
INFL_ALERT_RUN_ERROR_RATE=0.2   # 失败率超过 20% 提示
```

告警只出现在 `/api/monitor/summary.alerts[]` 与监控页横幅，不阻断任务、不自动重试、不发送外部通知。

## 6. 边界与已知限制

- TTFT 是**客户端口径**，包含网络与排队，不是 vLLM 内部 prefill 时间；要精确值需配置 `LLM_METRICS_URL` 并用服务端直方图；
- `decode_tokens_per_s` 含思考 token，正文速度看 `content_tokens_per_s`；
- 网关不返回 `usage` 时 token/速率字段为 `null`（UI 显示"—"），不估算；
- 非流式调用（网关不支持或 `LLM_STREAMING=false`）没有 TTFT/stream，只记总耗时与 token；
- 监控窗口按 `INFL_MONITOR_MAX_CALLS` 截断，返回 `truncated=true`；
- 指标表不保存 prompt 与回复正文，只有数字与枚举。
