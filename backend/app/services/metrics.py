"""确定性指标计算：配对检查、吞吐、TTFT 分位、TPOT。

所有公式都在本模块内，报告只展示这里算出的数字；LLM 不参与任何数值计算。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..schemas import ErrorCode, RunFailure
from .parsing import RequestRecord

GROUP_FIELDS = ("concurrency", "input_tokens", "output_tokens")

# 报告里默认把语义上的"改动前"配置作为基线，避免出现 apc → baseline 这种反向对照。
BASELINE_HINTS = ("baseline", "base", "control", "before", "off", "disabled", "default")

FORMULAS = {
    "output_throughput": "sum(output_tokens) / (max(end_ts) - min(start_ts))",
    "ttft_ms_pXX": "numpy.percentile(ttft_ms, XX, method='linear')",
    "tpot_ms_mean": "mean((end_ts - start_ts - ttft_ms/1000) / (output_tokens - 1))",
    "latency_ms_mean": "mean((end_ts - start_ts) * 1000)",
    "error_rate": "error_count / request_count",
    "delta_pct": "(candidate - baseline) / baseline * 100",
    "reduction_pct": "(baseline - candidate) / baseline * 100",
}


@dataclass
class PairingResult:
    ok: bool
    groups: int
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    comparisons: list[dict[str, Any]] = field(default_factory=list)


def group_key(record: RequestRecord) -> tuple[int, int, int]:
    return (record.concurrency, record.input_tokens, record.output_tokens)


def group_records(records: list[RequestRecord]) -> dict[tuple[int, int, int], dict[str, list[RequestRecord]]]:
    grouped: dict[tuple[int, int, int], dict[str, list[RequestRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[group_key(record)][record.config_label].append(record)
    return {key: dict(value) for key, value in grouped.items()}


def _pick_baseline(labels: list[str]) -> tuple[str, list[str]]:
    """选择对照基线：优先语义化的 baseline/control，否则取字典序第一个。"""

    for label in labels:
        if label.strip().lower() in BASELINE_HINTS:
            return label, [item for item in labels if item != label]
    return labels[0], labels[1:]


def check_pairing(
    records: list[RequestRecord],
    *,
    min_samples: int = 2,
    max_compared_groups: int = 4,
) -> PairingResult:
    """检查"可比性"：同一组键下每个 config_label 都要有足够样本。

    组键 = (并发, 输入长度, 输出长度)。组键不同意味着负载不同，不能互相加减。
    """

    if not records:
        raise RunFailure(ErrorCode.INSUFFICIENT_DATA, "没有可分析的请求记录")

    grouped = group_records(records)
    reasons: list[str] = []
    notes: list[str] = []
    comparisons: list[dict[str, Any]] = []

    for key, labels in sorted(grouped.items()):
        concurrency, input_tokens, output_tokens = key
        for label, items in sorted(labels.items()):
            if len(items) < min_samples:
                reasons.append(
                    f"负载(并发={concurrency}, 输入={input_tokens}, 输出={output_tokens}) 的 "
                    f"{label} 只有 {len(items)} 条样本，少于下限 {min_samples}"
                )
        if len(labels) < 2:
            continue
        baseline_label, candidate_labels = _pick_baseline(sorted(labels))
        baseline_items = labels[baseline_label]
        for candidate_label in candidate_labels:
            candidate_items = labels[candidate_label]
            if len(baseline_items) < min_samples or len(candidate_items) < min_samples:
                continue
            if len(comparisons) >= max_compared_groups:
                break
            comparisons.append(
                {
                    "key": {
                        "concurrency": concurrency,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                    "baseline": baseline_label,
                    "candidate": candidate_label,
                }
            )

    if not comparisons and not reasons:
        notes.append("所有记录属于同一组配置，没有可对照的两组数据（只出单组指标）")

    return PairingResult(
        ok=not reasons,
        groups=len(grouped),
        reasons=reasons,
        notes=notes,
        comparisons=comparisons,
    )


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), quantile, method="linear"))


def compute_group_metrics(records: list[RequestRecord]) -> dict[str, Any]:
    """单组（同 config_label + 同负载）指标。"""

    if not records:
        return {}

    ok_records = [record for record in records if record.status in ("ok", "success", "")]
    error_count = len(records) - len(ok_records)
    ttft = [record.ttft_ms for record in ok_records]
    inputs = {record.input_tokens for record in records}
    outputs = {record.output_tokens for record in records}
    concurrency = sorted({record.concurrency for record in records})

    output_tokens_total = sum(record.output_tokens for record in ok_records)
    has_timestamps = all(record.start_ts is not None and record.end_ts is not None for record in ok_records)
    warnings: list[str] = []
    wall_time_s: float | None = None
    throughput: float | None = None
    latency_mean: float | None = None
    tpot_values: list[float] = []

    if has_timestamps and ok_records:
        start = min(record.start_ts for record in ok_records if record.start_ts is not None)
        end = max(record.end_ts for record in ok_records if record.end_ts is not None)
        wall_time_s = max(0.0, end - start)
        if wall_time_s > 0:
            throughput = output_tokens_total / wall_time_s
        latency_mean = float(
            np.mean([(record.end_ts - record.start_ts) * 1000 for record in ok_records])  # type: ignore[operator]
        )
        for record in ok_records:
            if record.end_ts is None or record.start_ts is None or record.output_tokens <= 1:
                continue
            decode_s = max(0.0, (record.end_ts - record.start_ts) - record.ttft_ms / 1000)
            tpot_values.append(decode_s * 1000 / (record.output_tokens - 1))
    else:
        warnings.append("缺少 start_ts/end_ts，无法计算吞吐、TPOT 与平均时延")

    return {
        "config_label": records[0].config_label,
        "concurrency": concurrency[0] if len(concurrency) == 1 else concurrency,
        "input_tokens": inputs.pop() if len(inputs) == 1 else sorted(inputs),
        "output_tokens": outputs.pop() if len(outputs) == 1 else sorted(outputs),
        "request_count": len(records),
        "error_count": error_count,
        "error_rate": round(error_count / len(records), 4) if records else None,
        "output_tokens_total": output_tokens_total,
        "wall_time_s": round(wall_time_s, 4) if wall_time_s is not None else None,
        "output_throughput": round(throughput, 4) if throughput is not None else None,
        # 时延类保留 2 位，与报告表格、模型看到的 JSON 完全一致，避免"表格 209.97 / 正文 209.98"
        "ttft_ms_p50": round(_percentile(ttft, 50), 2) if ttft else None,
        "ttft_ms_p90": round(_percentile(ttft, 90), 2) if ttft else None,
        "ttft_ms_p95": round(_percentile(ttft, 95), 2) if ttft else None,
        "tpot_ms_mean": round(float(np.mean(tpot_values)), 2) if tpot_values else None,
        "latency_ms_mean": round(latency_mean, 2) if latency_mean is not None else None,
        "warnings": warnings,
    }


def _delta_pct(baseline: float | None, candidate: float | None) -> float | None:
    if baseline in (None, 0) or candidate is None:
        return None
    return round((candidate - baseline) / baseline * 100, 3)


def _reduction_pct(baseline: float | None, candidate: float | None) -> float | None:
    if baseline in (None, 0) or candidate is None:
        return None
    return round((baseline - candidate) / baseline * 100, 3)


def compute_metrics(
    records: list[RequestRecord],
    *,
    min_samples: int = 2,
) -> dict[str, Any]:
    """完整指标包：分组指标 + 配对结果 + 对照差值。"""

    pairing = check_pairing(records, min_samples=min_samples)
    grouped = group_records(records)

    groups: list[dict[str, Any]] = []
    metrics_by_key: dict[tuple[int, int, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for key, labels in sorted(grouped.items()):
        for label, items in sorted(labels.items()):
            metrics = compute_group_metrics(items)
            groups.append(metrics)
            metrics_by_key[key][label] = metrics

    comparisons: list[dict[str, Any]] = []
    for item in pairing.comparisons:
        key_tuple = (
            item["key"]["concurrency"],
            item["key"]["input_tokens"],
            item["key"]["output_tokens"],
        )
        baseline = metrics_by_key[key_tuple].get(item["baseline"], {})
        candidate = metrics_by_key[key_tuple].get(item["candidate"], {})
        comparisons.append(
            {
                **item,
                "output_throughput_baseline": baseline.get("output_throughput"),
                "output_throughput_candidate": candidate.get("output_throughput"),
                "output_throughput_pct": _delta_pct(
                    baseline.get("output_throughput"), candidate.get("output_throughput")
                ),
                "throughput_ratio": (
                    round(candidate["output_throughput"] / baseline["output_throughput"], 4)
                    if baseline.get("output_throughput") and candidate.get("output_throughput")
                    else None
                ),
                "ttft_ms_p50_baseline": baseline.get("ttft_ms_p50"),
                "ttft_ms_p50_candidate": candidate.get("ttft_ms_p50"),
                "ttft_p50_reduction_pct": _reduction_pct(baseline.get("ttft_ms_p50"), candidate.get("ttft_ms_p50")),
                "latency_ms_mean_baseline": baseline.get("latency_ms_mean"),
                "latency_ms_mean_candidate": candidate.get("latency_ms_mean"),
                "latency_mean_pct": _delta_pct(baseline.get("latency_ms_mean"), candidate.get("latency_ms_mean")),
            }
        )

    return {
        "record_count": len(records),
        "pairing": {
            "ok": pairing.ok,
            "groups": pairing.groups,
            "reasons": pairing.reasons,
            "notes": pairing.notes,
            "compare_count": len(comparisons),
        },
        "groups": groups,
        "comparisons": comparisons,
        "formulas": FORMULAS,
        "notes": [
            "组键 = (并发, 输入长度, 输出长度)；组键不同即视为不同负载，不做横向对比。",
            "分位采用 numpy 线性插值（method='linear'），与 Prometheus 直方图近似值不是同一算法。",
        ],
    }


def require_pairwise(metrics: dict[str, Any]) -> None:
    """在生成"对比结论"前调用；配对不成立就拒绝输出对比。"""

    pairing = metrics.get("pairing", {})
    if not pairing.get("ok"):
        raise RunFailure(
            ErrorCode.NON_PAIRWISE_INPUT,
            "输入不满足配对条件，拒绝生成对比结论",
            {"reasons": pairing.get("reasons", [])},
        )
