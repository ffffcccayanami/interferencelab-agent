# P4：判据与回归

> 目标：把"能算指标"推进到"能下结论"——每次分析任务给出**通过/不通过**，并与同一 benchmark 的上一次运行比较，发现**收益回归**。

## 1. 判据

| 判据 | 含义 | 默认阈值 | 配置 |
|---|---|---|---|
| 吞吐提升 | 最佳对照组的 `output_throughput_pct` ≥ 阈值 | `0`（不得更慢） | `INFL_EVAL_MIN_THROUGHPUT_PCT` |
| TTFT 降低 | 同一对照组的 `ttft_p50_reduction_pct` ≥ 阈值 | `0`（不得更慢） | `INFL_EVAL_MIN_TTFT_REDUCTION_PCT` |
| 错误率上限 | 所有分组 `error_rate` ≤ 阈值 | `1.0`（不限制） | `INFL_EVAL_MAX_ERROR_RATE` |

- 阈值语义：`0` 表示"不劣于基线组"；想要求"至少提升 10%"就设 `10`。
- 只对**包含两组及以上对照**的分析任务判定；单配置 / Prometheus 模式 / 研究 / 验证 → `not_applicable`。
- 判据随任务可覆盖：创建任务时传

```json
{
  "task_type": "analysis",
  "question": "验证 APC 是否达到 10% 吞吐提升",
  "upload_ids": ["upl_xxx"],
  "options": { "criteria": { "min_throughput_pct": 10, "min_ttft_reduction_pct": 5, "max_error_rate": 0.01 } }
}
```

## 2. 判定状态

| 状态 | 含义 |
|---|---|
| `pass` | 所有判据通过，且没有相对基线的收益下滑 |
| `fail` | 至少一条判据未通过 |
| `regression` | 判据通过，但相对基线吞吐或 TTFT 收益下降超过容忍值 |
| `not_applicable` | 没有可对照的两组 / 非分析任务 / 评测关闭 |

## 3. 基线与回归

- **benchmark 键**：同一批上传文件的 `sha256` 排序后 + 任务类型 → 32 位哈希；同文件重复实验自动归为同一基准。
- **基线选择**：同键、`succeeded`、有结果摘要的最近一次运行（排除当前 run）。
- **回归判定**：当前吞吐提升或 TTFT 降低幅度 **低于基线 − 容忍值** 即触发；容忍值默认 5 个百分点，配置项 `INFL_EVAL_REGRESSION_TOLERANCE_PCT`。
- 回归明细会写进 `result_summary.evaluation.regression.details`，并在监控页"判据与回归"列出。

## 4. 结果落在哪

| 位置 | 字段/表现 |
|---|---|
| `runs.eval_status` | `pass / fail / regression / not_applicable`，列表页直接筛/看 |
| `runs.benchmark_key` / `baseline_run_id` | 基准键与基线 run id |
| `runs.result_summary.evaluation` | `status / checks / reason / baseline / regression` |
| 报告 Markdown | `## 5. 判据与结论`：检查表 + 结论 + 基线 + 回归明细 |
| 任务详情页 | "判据与结论"卡片（阈值/实测/通过与否） |
| 监控页 | 通过率、各类计数、未通过/回归列表 |
| `/api/monitor/summary` | `runs.evaluation.{counts, evaluated, pass_rate, regressions}` |

## 5. 设计边界

- 判据只读确定性指标，**不让模型参与判定**；
- 没有可比的两组时明确显示"不适用"，不伪造结论；
- 回归只在同一 benchmark 键内比较，换文件/换任务类型不会误判；
- 阈值是"业务约定"，不是统计显著性检验；样本量仍由 `INFL_MIN_GROUP_SAMPLES` 控制。
