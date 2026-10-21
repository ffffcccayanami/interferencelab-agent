# 演示脚本（5 分钟）

前置：后端已启动（`python -m app.main`），前端 `npm run dev`，`backend/.env` 可留空（`LLM_ENABLED=false` 也能完整演示确定性链路）。

## 1. 实验分析（主线）

1. 打开 `http://127.0.0.1:5175`，任务类型选"实验分析"；
2. 问题填：`对比 baseline 与 apc 在并发 4、2.8K 输入、128 输出下的吞吐与 TTFT`；
3. 上传 `fixtures/bench_concurrency_sample.json`；
4. 提交 → 详情页观察阶段 `analysis_parse → analysis_compute → explain → persist`；
5. 报告页确认：
   - 分组指标：`74.94` / `108.95` tok/s；
   - 同负载对照：吞吐 `+45.4%`，TTFT P50 降低 `≈36.7%`；
   - "结论边界"里写明这是同环境配置对照，不是单参数因果；
6. 下载 `report.pdf` 与 `result.json`。

**讲解点**：数字来自 `metrics.py` 的公式，模型不参与计算；没有模型时报告标记 `degraded`，指标仍然完整。

## 2. 拒绝错误对比（体现严谨性）

1. 新任务，上传 `fixtures/bench_uneven_samples.json`；
2. 任务将失败，错误码 `NON_PAIRWISE_INPUT`，`details.reasons` 指出"apc 只有 1 条样本，少于下限 2"；
3. 说明：宁可不出报告，也不生成不可比的对比结论。

## 3. 技术研究（证据引用）

1. 任务类型"技术研究"，问题：`前缀缓存为什么可能不命中？`；
2. 上传 `fixtures/docs/apc_notes.md` 与 `fixtures/docs/chunked_prefill.md`；
3. 报告里每条引用带 `chunk_id` 与字符区间，可回原文核对；
4. 再单独上传 `fixtures/docs/unrelated.md` 提问同一问题 → `NO_EVIDENCE_DOCUMENTS`（不编造来源）。

## 4. 受控验证（人工确认）

1. 任务类型"受控验证"，问题随意，条目选 `bench_smoke`；
2. 提交后状态 `awaiting_approval`，详情页出现审批卡片与 `approval_id`；
3. 先改一个参数（如并发 4 → 7）再点"确认并执行" → 409 `APPROVAL_MISMATCH`；
4. 恢复默认参数后确认 → `succeeded`，产物含 `validation_stdout` 与验证报告；
5. 说明：LLM 只能选条目和填参数，脚本路径与命令不可被模型控制。

## 5. 并发隔离与取消

1. 连续提交两个任务，观察事件流互不串、产物目录不同；
2. 对排队中的任务点"取消" → 状态 `cancelled`，不产生报告。

## 6. 监控页（P5）

1. 顶栏点"监控"（`#/monitor`）；
2. 卡片：任务成功率、TTFT P50/P95（首 token）、decode P50/P95、token 用量；
3. 表格：失败码分布、节点耗时 P95、按节点的模型调用、最慢任务；
4. 点进任一任务 → "模型调用"表里能看到 `ttft_first_token_ms`、`ttft_first_content_ms`、decode tok/s 与 token 明细；
5. 讲解点：TTFT 是客户端口径（含网络/排队），推理模型的"首正文"会明显晚于"首 token"；非流式或网关不返回 usage 时对应字段显示"—"，不估算。

## 7. 录屏前检查

- [ ] 屏幕上没有打开 `backend/.env`
- [ ] 浏览器地址栏只有 `127.0.0.1`
- [ ] `python scripts/secret_scan.py --worktree` 通过
- [ ] 报告里的数字与 `fixtures` 一致（合成数据已注明）
- [ ] 录制后不把 `.env`、token、内网地址带进视频
