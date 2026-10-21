"""P4：分析任务的判据与回归检测。

判定只读取确定性指标（`metrics.compute_metrics` 的输出），不做任何模型推断：

* 判据：吞吐提升、TTFT P50 降低、错误率上限，阈值来自配置或本次任务的 ``options.criteria``；
* 基线：同一 benchmark（上传文件 sha256 集合）的最近一次成功运行；
* 回归：相对基线，吞吐提升或 TTFT 降低幅度下降超过容忍值。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..db.store import Store
from ..settings import Settings

EVAL_PASS = "pass"
EVAL_FAIL = "fail"
EVAL_REGRESSION = "regression"
EVAL_NOT_APPLICABLE = "not_applicable"


def benchmark_key(uploads: list[dict[str, Any]], task_type: str) -> str | None:
    """同一批上传（按 sha256 排序）+ 任务类型 → 稳定的基准键。"""

    hashes = sorted(str(item.get("sha256") or "") for item in uploads if item.get("sha256"))
    if not hashes:
        return None
    payload = "|".join([task_type, *hashes])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def resolve_criteria(settings: Settings, options: dict[str, Any] | None) -> dict[str, float]:
    overrides = (options or {}).get("criteria") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    def pick(key: str, fallback: float) -> float:
        value = overrides.get(key)
        try:
            return float(value) if value is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    return {
        "min_throughput_pct": pick("min_throughput_pct", settings.infl_eval_min_throughput_pct),
        "min_ttft_reduction_pct": pick(
            "min_ttft_reduction_pct", settings.infl_eval_min_ttft_reduction_pct
        ),
        "max_error_rate": pick("max_error_rate", settings.infl_eval_max_error_rate),
    }


def _best_comparison(metrics: dict[str, Any]) -> dict[str, Any] | None:
    comparisons = metrics.get("comparisons") or []
    if not comparisons:
        return None
    return max(comparisons, key=lambda item: item.get("output_throughput_pct") or -10**9)


def _max_error_rate(metrics: dict[str, Any]) -> float | None:
    rates = [group.get("error_rate") for group in metrics.get("groups") or [] if group.get("error_rate") is not None]
    return max(rates) if rates else None


def evaluate_metrics(metrics: dict[str, Any], criteria: dict[str, float]) -> dict[str, Any]:
    """按阈值判定一次分析结果；没有可对照的两组时返回 not_applicable。"""

    best = _best_comparison(metrics)
    if best is None:
        return {
            "status": EVAL_NOT_APPLICABLE,
            "checks": [],
            "reason": "没有可对照的两组配置，无法判定",
            "comparison": None,
        }

    throughput = best.get("output_throughput_pct")
    ttft_reduction = best.get("ttft_p50_reduction_pct")
    error_rate = _max_error_rate(metrics)

    checks = [
        {
            "name": "throughput_pct",
            "label": "吞吐提升",
            "actual": throughput,
            "threshold": criteria["min_throughput_pct"],
            "passed": throughput is not None and throughput >= criteria["min_throughput_pct"],
            "unit": "%",
        },
        {
            "name": "ttft_p50_reduction_pct",
            "label": "TTFT P50 降低",
            "actual": ttft_reduction,
            "threshold": criteria["min_ttft_reduction_pct"],
            "passed": ttft_reduction is not None and ttft_reduction >= criteria["min_ttft_reduction_pct"],
            "unit": "%",
        },
    ]
    if error_rate is not None:
        checks.append(
            {
                "name": "max_error_rate",
                "label": "最大错误率",
                "actual": error_rate,
                "threshold": criteria["max_error_rate"],
                "passed": error_rate <= criteria["max_error_rate"],
                "unit": "",
            }
        )

    failed = [check["name"] for check in checks if not check["passed"]]
    status = EVAL_FAIL if failed else EVAL_PASS
    reason = "全部判据通过" if not failed else "未通过：" + "、".join(failed)
    return {
        "status": status,
        "checks": checks,
        "reason": reason,
        "comparison": {
            "throughput_pct": throughput,
            "ttft_p50_reduction_pct": ttft_reduction,
        },
        "criteria": criteria,
    }


def detect_regression(
    current: dict[str, Any],
    baseline_summary: dict[str, Any] | None,
    *,
    tolerance_pct: float,
) -> dict[str, Any] | None:
    """当前相对基线的收益下滑；没有基线或数据不足时返回 None。"""

    if not baseline_summary:
        return None
    baseline = (baseline_summary.get("evaluation") or {}).get("comparison") or {}
    current_cmp = current.get("comparison") or {}
    if not baseline or not current_cmp:
        return None

    details: list[dict[str, Any]] = []
    for key, label in (("throughput_pct", "吞吐提升"), ("ttft_p50_reduction_pct", "TTFT 降低")):
        before = baseline.get(key)
        after = current_cmp.get(key)
        if before is None or after is None:
            continue
        if after < before - tolerance_pct:
            details.append(
                {
                    "metric": key,
                    "label": label,
                    "baseline": before,
                    "current": after,
                    "delta": round(after - before, 2),
                }
            )
    if not details:
        return None
    return {"tolerance_pct": tolerance_pct, "details": details}


def build_evaluation(
    *,
    state: dict[str, Any],
    settings: Settings,
    store: Store,
    run_id: str,
) -> dict[str, Any]:
    """在 persist 阶段调用：判定 + 基线查找 + 回归检测。"""

    task_type = state.get("task_type")
    analysis = state.get("analysis") or {}
    uploads = state.get("uploads") or []
    key = benchmark_key(uploads, str(task_type))

    result: dict[str, Any] = {
        "status": EVAL_NOT_APPLICABLE,
        "checks": [],
        "reason": "仅对包含两组对照的分析任务判定",
        "benchmark_key": key,
        "baseline_run_id": None,
        "baseline": None,
        "regression": None,
    }

    if not settings.infl_eval_enabled:
        result["reason"] = "评测已关闭（INFL_EVAL_ENABLED=false）"
        return result
    if task_type != "analysis" or analysis.get("kind") == "prometheus":
        return result

    criteria = resolve_criteria(settings, state.get("options") or {})
    evaluation = evaluate_metrics(analysis, criteria)
    result.update(evaluation)
    result["benchmark_key"] = key

    if key:
        baseline_run = store.latest_run_by_benchmark(key, exclude_run_id=run_id)
        if baseline_run is not None:
            baseline_summary = baseline_run.get("result_summary") or {}
            result["baseline_run_id"] = baseline_run["id"]
            result["baseline"] = {
                "run_id": baseline_run["id"],
                "finished_at": baseline_run.get("finished_at"),
                "throughput_pct": (baseline_summary.get("evaluation") or {})
                .get("comparison", {})
                .get("throughput_pct"),
                "ttft_p50_reduction_pct": (baseline_summary.get("evaluation") or {})
                .get("comparison", {})
                .get("ttft_p50_reduction_pct"),
            }
            regression = detect_regression(
                evaluation,
                baseline_summary,
                tolerance_pct=settings.infl_eval_regression_tolerance_pct,
            )
            if regression is not None:
                result["regression"] = regression
                if result["status"] == EVAL_PASS:
                    result["status"] = EVAL_REGRESSION
                    result["reason"] = "相对基线出现收益下滑"

    return result
