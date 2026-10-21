"""报告渲染：确定性 Markdown（不含模型生成的数字）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt(value: Any, digits: int = 2, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        text = f"{value:,.{digits}f}" if isinstance(value, float) else f"{value:,}"
        return f"{text}{unit}" if unit else text
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_（无数据）_\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def _inputs_table(state: dict[str, Any]) -> str:
    rows = []
    for item in state.get("uploads", []):
        rows.append([item.get("filename", "—"), item.get("detected_type", "—"), _fmt(item.get("size", 0))])
    return _table(["文件", "类型", "字节"], rows)


def _group_rows(metrics: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for group in metrics.get("groups", []):
        rows.append(
            [
                group.get("config_label"),
                _fmt(group.get("concurrency")),
                _fmt(group.get("input_tokens")),
                _fmt(group.get("output_tokens")),
                _fmt(group.get("request_count")),
                _fmt(group.get("output_throughput")),
                _fmt(group.get("ttft_ms_p50")),
                _fmt(group.get("ttft_ms_p90")),
                _fmt(group.get("ttft_ms_p95")),
                _fmt(group.get("tpot_ms_mean")),
                _fmt(group.get("error_rate")),
            ]
        )
    return rows


def _comparison_rows(metrics: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for item in metrics.get("comparisons", []):
        key = item.get("key", {})
        rows.append(
            [
                f"并发 {key.get('concurrency')} / 输入 {key.get('input_tokens')} / 输出 {key.get('output_tokens')}",
                item.get("baseline"),
                item.get("candidate"),
                f"{_fmt(item.get('output_throughput_baseline'))} → {_fmt(item.get('output_throughput_candidate'))}",
                _fmt(item.get("output_throughput_pct"), 2, "%"),
                f"{_fmt(item.get('ttft_ms_p50_baseline'))} → {_fmt(item.get('ttft_ms_p50_candidate'))}",
                _fmt(item.get("ttft_p50_reduction_pct"), 2, "%"),
                _fmt(item.get("latency_mean_pct"), 2, "%"),
            ]
        )
    return rows


def _explanation_section(state: dict[str, Any], title: str | None = None) -> str:
    explanation = (state.get("explanation") or "").strip()
    source = state.get("explanation_source", "none")
    if title:
        header = f"## {title}\n\n"
    elif explanation and source == "llm":
        header = "## 模型解释（非确定性，未参与数值计算）\n\n"
    elif explanation and source == "template":
        header = "## 结论摘要（确定性模板）\n\n"
    else:
        header = "## 模型解释\n\n"
    if not explanation:
        reason = "、".join(state.get("warnings", [])) or "模型未启用"
        explanation = f"_本次未生成模型解释（{reason}）；以下结论仅来自确定性计算。_\n"
    return header + explanation.strip() + "\n"


def _limits_section(state: dict[str, Any]) -> str:
    lines = [
        "## 结论边界",
        "",
        "- 对照收益来自**同一环境、同一负载下的配置差异**，不能把全部收益归因于单个参数；",
        "- 分位由本仓库用 numpy 线性插值计算，与 Prometheus 直方图近似值不是同一算法；",
        "- 未覆盖多卡张量并行、PD 分离与生产容量结论；",
        "- 请求级指标依赖输入文件中的 ts 字段，缺少时间戳时吞吐与 TPOT 记为不可用。",
    ]
    if state.get("warnings"):
        lines.append("")
        lines.append("## 告警")
        lines.append("")
        lines.extend(f"- {warning}" for warning in state["warnings"])
    return "\n".join(lines) + "\n"


def _evaluation_section(state: dict[str, Any]) -> str:
    evaluation = state.get("evaluation")
    if not evaluation:
        return ""
    status = evaluation.get("status")
    labels = {
        "pass": "通过",
        "fail": "不通过",
        "regression": "相对基线回归",
        "not_applicable": "不适用",
    }
    lines = [
        "## 5. 判据与结论",
        "",
        f"**结论：{labels.get(status, status)}** — {evaluation.get('reason', '')}",
        "",
    ]
    checks = evaluation.get("checks") or []
    if checks:
        lines.append("| 检查项 | 阈值 | 实测 | 结果 |")
        lines.append("|---|---|---|---|")
        for check in checks:
            unit = check.get("unit") or ""
            actual = check.get("actual")
            threshold = check.get("threshold")
            actual_text = "—" if actual is None else f"{actual:.2f}{unit}"
            threshold_text = "—" if threshold is None else f"{threshold:.2f}{unit}"
            lines.append(
                f"| {check.get('label')} | {threshold_text} | {actual_text} | "
                f"{'通过' if check.get('passed') else '未通过'} |"
            )
        lines.append("")

    baseline = evaluation.get("baseline")
    if baseline:
        lines.append(
            f"基线：`{baseline.get('run_id')}`（{baseline.get('finished_at') or '—'}）"
            f" 吞吐提升 {baseline.get('throughput_pct')}% / TTFT 降低 "
            f"{baseline.get('ttft_p50_reduction_pct')}%"
        )
    regression = evaluation.get("regression")
    if regression:
        lines.append("")
        lines.append("回归明细：")
        for item in regression.get("details") or []:
            lines.append(
                f"- {item.get('label')}：{item.get('baseline')}% → {item.get('current')}%"
                f"（{item.get('delta')}%，容忍 {regression.get('tolerance_pct')}%）"
            )
    return "\n".join(lines) + "\n"


def render_analysis(state: dict[str, Any]) -> str:
    metrics = state.get("analysis") or {}
    pairing = metrics.get("pairing", {})
    comparisons = metrics.get("comparisons", [])

    parts = [
        "# 推理实验结果分析报告",
        "",
        f"生成时间：{_now()}",
        "",
        "## 1. 任务",
        "",
        f"**问题：** {state.get('question', '—')}",
        "",
        "**输入：**",
        "",
        _inputs_table(state),
        "## 2. 配对条件检查",
        "",
        f"- 记录数：{_fmt(metrics.get('record_count'))}",
        f"- 负载组数：{_fmt(pairing.get('groups'))}",
        f"- 可对照组数：{_fmt(pairing.get('compare_count', 0))}",
        f"- 配对结果：{'通过' if pairing.get('ok') else '未通过'}",
    ]
    for reason in pairing.get("reasons", []):
        parts.append(f"- 阻断原因：{reason}")
    for note in (metrics.get("notes") or [])[:2]:
        parts.append(f"- 说明：{note}")

    parts += [
        "",
        "## 3. 分组指标",
        "",
        _table(
            ["配置", "并发", "输入", "输出", "样本", "吞吐(tok/s)", "TTFT P50(ms)", "P90", "P95", "TPOT(ms)", "错误率"],
            _group_rows(metrics),
        ),
        "## 4. 同负载对照",
        "",
    ]
    if comparisons:
        parts.append(
            _table(
                ["负载", "基线", "候选", "吞吐", "吞吐变化", "TTFT P50", "P50 降低", "平均时延变化"],
                _comparison_rows(metrics),
            )
        )
    else:
        parts.append("_本批次没有可配对的两组配置，只输出单组指标，不生成对比结论。_\n")

    evaluation_block = _evaluation_section(state)
    if evaluation_block:
        parts += ["", evaluation_block]

    parts += ["", _explanation_section(state), _limits_section(state)]

    formulas = metrics.get("formulas", {})
    if formulas:
        parts += ["", "## 附录：计算口径", ""]
        parts.extend(f"- `{key}` = {value}" for key, value in formulas.items())

    return "\n".join(parts).strip() + "\n"


def render_prometheus(state: dict[str, Any]) -> str:
    payload = state.get("prometheus") or {}
    gauges = payload.get("gauges", {})
    gauge_rows = [[name, _fmt(value, 4)] for name, value in sorted(gauges.items())][:40]
    histogram_rows = []
    for item in payload.get("histograms", []):
        quantiles = item.get("quantiles", {})
        histogram_rows.append(
            [
                item.get("name"),
                _fmt(item.get("count"), 0),
                _fmt(quantiles.get("p50"), 4),
                _fmt(quantiles.get("p90"), 4),
                _fmt(quantiles.get("p95"), 4),
            ]
        )

    parts = [
        "# 推理服务指标分析报告（Prometheus）",
        "",
        f"生成时间：{_now()}",
        "",
        "## 1. 任务",
        "",
        f"**问题：** {state.get('question', '—')}",
        "",
        _inputs_table(state),
        "## 2. 采样概览",
        "",
        f"- 样本数：{_fmt(payload.get('sample_count'))}",
        f"- 直方图指标数：{_fmt(len(payload.get('histograms', [])))}",
        "",
        "## 3. 标量指标",
        "",
        _table(["指标", "值"], gauge_rows),
        "## 4. 直方图分位（近似值）",
        "",
        _table(["指标", "count", "P50", "P90", "P95"], histogram_rows),
    ]
    parts += ["", _explanation_section(state), _limits_section(state)]
    return "\n".join(parts).strip() + "\n"


def _quote_block(text: str) -> str:
    """多行引用逐行加 ``>``，避免原文里的 Markdown 标题破坏报告结构。"""

    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {line}".rstrip() for line in lines)


def render_research(state: dict[str, Any]) -> str:
    citations = state.get("citations", [])
    citation_rows = [
        [
            item.get("chunk_id"),
            item.get("filename"),
            f"{item.get('char_start')}-{item.get('char_end')}",
            _fmt(item.get("score"), 3),
        ]
        for item in citations
    ]
    evidence_blocks = [
        f"**[{item.get('chunk_id')}]** {item.get('filename')}（字符 {item.get('char_start')}-{item.get('char_end')}）\n\n"
        f"{_quote_block(str(item.get('quote', '')))}"
        for item in citations
    ]

    parts = [
        "# 技术研究分析报告",
        "",
        f"生成时间：{_now()}",
        "",
        "## 1. 问题",
        "",
        f"{state.get('question', '—')}",
        "",
        "## 2. 检索到的证据",
        "",
        _table(["片段", "来源", "字符区间", "相关度"], citation_rows),
        "## 3. 证据原文",
        "",
        "\n\n".join(evidence_blocks) if evidence_blocks else "_（无）_",
        "",
        _explanation_section(state, title="4. 结论（模型解释，未参与数值计算）"),
        "## 5. 结论边界",
        "",
        "- 检索范围仅限本次上传的文档，不包含联网资料；",
        "- 段落若无有效 `[[chunk_id]]` 引用会被自动剔除，因此解释可能不完整；",
        "- 引用区块给出字符区间，可回原文核对。",
    ]
    if state.get("warnings"):
        parts += ["", "## 告警", ""]
        parts.extend(f"- {warning}" for warning in state["warnings"])
    return "\n".join(parts).strip() + "\n"


def render_validation(state: dict[str, Any]) -> str:
    validation = state.get("validation") or {}
    params = validation.get("params", {})
    stdout = (validation.get("stdout") or "").strip()
    stderr = (validation.get("stderr") or "").strip()
    truncated = stdout if len(stdout) <= 4000 else stdout[:4000] + "\n…（已截断）"

    parts = [
        "# 受控验证报告",
        "",
        f"生成时间：{_now()}",
        "",
        "## 1. 验证项",
        "",
        f"- 条目：`{validation.get('entry_id', '—')}` — {validation.get('title', '')}",
        f"- 脚本：`{validation.get('script', '—')}`（registry 白名单内，参数经 JSON Schema 校验）",
        f"- 退出码：{_fmt(validation.get('exit_code'))}",
        f"- 耗时：{_fmt(validation.get('duration_ms'))} ms",
        f"- 是否超时：{_fmt(validation.get('timed_out'))}",
        "",
        "## 2. 参数",
        "",
        "```json",
        json.dumps(params, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## 3. 标准输出",
        "",
        "```text",
        truncated or "(空)",
        "```",
    ]
    if stderr:
        parts += ["", "## 4. 标准错误", "", "```text", stderr[:2000], "```"]

    parts += [
        "",
        "## 结论边界",
        "",
        "- 只执行 registry 中登记且经人工确认的脚本，不接受任意命令或路径；",
        "- 脚本在独立任务目录中运行，环境变量白名单不包含任何 API Key；",
        "- 验证结果仅代表该脚本的固定检查，不代表整模型或生产环境结论。",
    ]
    if state.get("warnings"):
        parts += ["", "## 告警", ""]
        parts.extend(f"- {warning}" for warning in state["warnings"])
    return "\n".join(parts).strip() + "\n"


def render_report(state: dict[str, Any]) -> str:
    task_type = state.get("task_type")
    if task_type == "analysis":
        if (state.get("analysis") or {}).get("kind") == "prometheus" or state.get("prometheus"):
            return render_prometheus(state)
        return render_analysis(state)
    if task_type == "research":
        return render_research(state)
    if task_type == "validation":
        return render_validation(state)
    return f"# 未知任务类型\n\n{task_type}\n"


def metrics_payload(state: dict[str, Any]) -> dict[str, Any]:
    """可下载的 JSON 结果包（只含确定性数据与引用）。"""

    payload: dict[str, Any] = {
        "task_type": state.get("task_type"),
        "question": state.get("question"),
        "generated_at": _now(),
        "warnings": state.get("warnings", []),
        "degraded": state.get("degraded", False),
    }
    if state.get("analysis"):
        payload["analysis"] = state["analysis"]
    if state.get("prometheus"):
        payload["prometheus"] = state["prometheus"]
    if state.get("citations"):
        payload["citations"] = state["citations"]
    if state.get("validation"):
        payload["validation"] = {key: value for key, value in state["validation"].items() if key != "stdout"}
    return payload


def result_summary(state: dict[str, Any]) -> dict[str, Any]:
    """一句话级别的任务结果摘要（落 runs.result_summary_json，供列表与监控使用）。"""

    task_type = state.get("task_type")
    summary: dict[str, Any] = {"kind": task_type, "degraded": bool(state.get("degraded"))}

    if task_type == "analysis":
        analysis = state.get("analysis") or {}
        if analysis.get("kind") == "prometheus":
            histograms = analysis.get("histograms") or []
            first = histograms[0] if histograms else {}
            summary.update(
                {
                    "mode": "prometheus",
                    "histograms": len(histograms),
                    "ttft_p50_s": (first.get("quantiles") or {}).get("p50"),
                }
            )
            return summary
        comparisons = analysis.get("comparisons") or []
        best = max(
            comparisons,
            key=lambda item: item.get("output_throughput_pct") or -10**9,
            default=None,
        )
        summary.update(
            {
                "mode": "benchmark",
                "groups": len(analysis.get("groups") or []),
                "comparisons": len(comparisons),
                "throughput_pct": best.get("output_throughput_pct") if best else None,
                "ttft_p50_reduction_pct": best.get("ttft_p50_reduction_pct") if best else None,
            }
        )
        if state.get("evaluation"):
            evaluation = state["evaluation"]
            summary["evaluation"] = {
                "status": evaluation.get("status"),
                "reason": evaluation.get("reason"),
                "checks": evaluation.get("checks"),
                "baseline_run_id": evaluation.get("baseline_run_id"),
                "regression": evaluation.get("regression"),
            }
        return summary

    if task_type == "research":
        summary.update(
            {
                "documents": len(state.get("documents") or []),
                "citations": len(state.get("citations") or []),
            }
        )
        return summary

    if task_type == "validation":
        validation = state.get("validation") or {}
        summary.update(
            {
                "entry_id": validation.get("entry_id"),
                "exit_code": validation.get("exit_code"),
                "duration_ms": validation.get("duration_ms"),
            }
        )
        return summary

    return summary
