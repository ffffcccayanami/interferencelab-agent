"""监控聚合：任务与模型调用的窗口统计 + Prometheus 文本渲染。

所有数字都来自 SQLite（runs / llm_calls / node_timings），不触发任何模型调用。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from ..db.store import Store
from ..settings import Settings

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

TTFT_BUCKETS = (0.1, 0.2, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
TOKPS_BUCKETS = (1.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return round(float(np.percentile(np.asarray(values, dtype=float), quantile, method="linear")), 2)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def trend(
    store: Store,
    *,
    hours: float = 24,
    buckets: int = 24,
    max_calls: int = 2000,
) -> dict[str, Any]:
    """时间分桶趋势：给前端折线图用（TTFT / decode / 任务量）。"""

    bucket_count = max(4, min(96, int(buckets)))
    hours = max(1.0, min(24.0 * 30, float(hours)))
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    width = (end - start).total_seconds() / bucket_count
    since = start.isoformat(timespec="seconds")

    slots: list[dict[str, Any]] = [
        {
            "ts": (start + timedelta(seconds=width * index)).isoformat(timespec="seconds"),
            "calls": 0,
            "runs": 0,
            "failures": 0,
            "ttft": [],
            "decode": [],
            "durations": [],
        }
        for index in range(bucket_count)
    ]

    def slot_index(ts: str | None) -> int | None:
        moment = _parse_ts(ts)
        if moment is None:
            return None
        offset = (moment - start).total_seconds()
        if offset < 0:
            return None
        return min(bucket_count - 1, int(offset // width))

    for call in store.llm_calls_since(since, limit=max_calls):
        index = slot_index(call.get("created_at"))
        if index is None:
            continue
        slot = slots[index]
        slot["calls"] += 1
        if call.get("ttft_first_token_ms") is not None:
            slot["ttft"].append(float(call["ttft_first_token_ms"]))
        if call.get("decode_tokens_per_s") is not None:
            slot["decode"].append(float(call["decode_tokens_per_s"]))

    for run in store.runs_since(since):
        index = slot_index(run.get("created_at"))
        if index is None:
            continue
        slot = slots[index]
        slot["runs"] += 1
        if run.get("status") in ("failed", "interrupted"):
            slot["failures"] += 1
        duration = _run_duration_ms(run)
        if duration is not None:
            slot["durations"].append(duration)

    series = [
        {
            "ts": slot["ts"],
            "calls": slot["calls"],
            "runs": slot["runs"],
            "failures": slot["failures"],
            "ttft_p50": percentile(slot["ttft"], 50) if slot["ttft"] else None,
            "ttft_p95": percentile(slot["ttft"], 95) if slot["ttft"] else None,
            "decode_p50": percentile(slot["decode"], 50) if slot["decode"] else None,
            "duration_p50": percentile(slot["durations"], 50) if slot["durations"] else None,
        }
        for slot in slots
    ]
    return {
        "hours": hours,
        "buckets": bucket_count,
        "since": since,
        "generated_at": end.isoformat(timespec="seconds"),
        "series": series,
    }


def _since(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _run_duration_ms(run: dict[str, Any]) -> float | None:
    if run.get("duration_ms") is not None:
        return float(run["duration_ms"])
    try:
        started = datetime.fromisoformat(run["started_at"])
        finished = datetime.fromisoformat(run["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((finished - started).total_seconds() * 1000, 2)


def summary(store: Store, settings: Settings, *, hours: float | None = None) -> dict[str, Any]:
    window_hours = float(hours if hours is not None else settings.infl_monitor_window_hours)
    limit = settings.infl_monitor_max_calls
    since = _since(window_hours)

    runs = store.runs_since(since)
    calls = store.llm_calls_since(since, limit=limit)
    timings = store.node_timings_since(since, limit=limit)

    status_counts = Counter(run["status"] for run in runs)
    finished = [run for run in runs if run["status"] in TERMINAL]
    succeeded = sum(1 for run in finished if run["status"] == "succeeded")
    failure_codes = Counter(run["error_code"] for run in finished if run.get("error_code"))

    durations = [value for value in (_run_duration_ms(run) for run in runs) if value is not None]

    ttft_token = [c["ttft_first_token_ms"] for c in calls if c.get("ttft_first_token_ms") is not None]
    ttft_content = [c["ttft_first_content_ms"] for c in calls if c.get("ttft_first_content_ms") is not None]
    decode = [c["decode_tokens_per_s"] for c in calls if c.get("decode_tokens_per_s") is not None]
    content_rate = [c["content_tokens_per_s"] for c in calls if c.get("content_tokens_per_s") is not None]

    token_totals = {
        "prompt": sum(c["prompt_tokens"] or 0 for c in calls),
        "completion": sum(c["completion_tokens"] or 0 for c in calls),
        "reasoning": sum(c["reasoning_tokens"] or 0 for c in calls),
    }

    by_node: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in timings:
        grouped[item["node"]].append(float(item["duration_ms"]))
    for node, values in sorted(grouped.items()):
        by_node[node] = {
            "calls": len(values),
            "p50_ms": percentile(values, 50),
            "p95_ms": percentile(values, 95),
            "max_ms": round(max(values), 2),
        }

    node_llm: dict[str, list[float]] = defaultdict(list)
    node_llm_calls: Counter[str] = Counter()
    for call in calls:
        node_llm_calls[call["node"]] += 1
        if call.get("ttft_first_token_ms") is not None:
            node_llm[call["node"]].append(float(call["ttft_first_token_ms"]))
    llm_by_node = [
        {
            "node": node,
            "calls": node_llm_calls[node],
            "ttft_p95_ms": percentile(values, 95) if values else None,
        }
        for node, values in sorted(node_llm.items(), key=lambda item: -node_llm_calls[item[0]])
    ]

    slow_runs = sorted(
        (
            {
                "run_id": run["id"],
                "task_type": run["task_type"],
                "status": run["status"],
                "duration_ms": _run_duration_ms(run),
            }
            for run in runs
            if _run_duration_ms(run) is not None
        ),
        key=lambda item: item["duration_ms"] or 0,
        reverse=True,
    )[:5]

    ttft_p95 = percentile(ttft_token, 95) if ttft_token else None
    error_rate = round(1 - succeeded / len(finished), 4) if finished else None

    eval_counts: Counter[str] = Counter(run.get("eval_status") or "none" for run in runs)
    evaluated = [run for run in runs if run.get("eval_status") in ("pass", "fail", "regression")]
    eval_passed = sum(1 for run in evaluated if run["eval_status"] == "pass")
    regressions = [
        {
            "run_id": run["id"],
            "task_type": run["task_type"],
            "eval_status": run.get("eval_status"),
            "reason": ((run.get("result_summary") or {}).get("evaluation") or {}).get("reason"),
            "finished_at": run.get("finished_at"),
            "baseline_run_id": run.get("baseline_run_id"),
        }
        for run in runs
        if run.get("eval_status") in ("fail", "regression")
    ][:10]

    alerts: list[dict[str, Any]] = []
    if settings.infl_alert_ttft_p95_ms and ttft_p95 is not None and ttft_p95 > settings.infl_alert_ttft_p95_ms:
        alerts.append(
            {
                "code": "TTFT_P95_HIGH",
                "message": f"首 token P95 {ttft_p95} ms 超过阈值 {settings.infl_alert_ttft_p95_ms} ms",
            }
        )
    if (
        settings.infl_alert_run_error_rate
        and error_rate is not None
        and error_rate > settings.infl_alert_run_error_rate
    ):
        alerts.append(
            {
                "code": "RUN_ERROR_RATE_HIGH",
                "message": f"任务失败率 {error_rate:.0%} 超过阈值 {settings.infl_alert_run_error_rate:.0%}",
            }
        )

    return {
        "window_hours": window_hours,
        "since": since,
        "truncated": len(calls) >= limit,
        "runs": {
            "total": len(runs),
            "finished": len(finished),
            "by_status": {status: status_counts.get(status, 0) for status in (*TERMINAL, "queued", "running", "awaiting_approval")},
            "success_rate": round(succeeded / len(finished), 4) if finished else None,
            "duration_ms": {
                "p50": percentile(durations, 50),
                "p95": percentile(durations, 95),
                "max": round(max(durations), 2) if durations else None,
            },
            "failure_codes": [{"code": code, "count": count} for code, count in failure_codes.most_common()],
            "slowest": slow_runs,
            "evaluation": {
                "counts": {status: eval_counts.get(status, 0) for status in ("pass", "fail", "regression", "not_applicable", "none")},
                "evaluated": len(evaluated),
                "pass_rate": round(eval_passed / len(evaluated), 4) if evaluated else None,
                "regressions": regressions,
            },
        },
        "llm": {
            "calls": len(calls),
            "errors": sum(1 for call in calls if call.get("outcome") not in ("ok", None)),
            "streamed": sum(1 for call in calls if call.get("streamed")),
            "tokens": token_totals,
            "ttft_first_token_ms": {
                "p50": percentile(ttft_token, 50) if ttft_token else None,
                "p95": ttft_p95,
                "max": round(max(ttft_token), 2) if ttft_token else None,
            },
            "ttft_first_content_ms": {
                "p50": percentile(ttft_content, 50) if ttft_content else None,
                "p95": percentile(ttft_content, 95) if ttft_content else None,
            },
            "decode_tokens_per_s": {
                "p50": percentile(decode, 50) if decode else None,
                "p95": percentile(decode, 95) if decode else None,
            },
            "content_tokens_per_s": {
                "p50": percentile(content_rate, 50) if content_rate else None,
                "p95": percentile(content_rate, 95) if content_rate else None,
            },
            "by_node": llm_by_node,
        },
        "nodes": by_node,
        "alerts": alerts,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _histogram(lines: list[str], name: str, values: list[float], buckets: tuple[float, ...], help_text: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} histogram")
    total = len(values)
    for bucket in buckets:
        count = sum(1 for value in values if value <= bucket)
        lines.append(f'{name}_bucket{{le="{bucket}"}} {count}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {total}')
    lines.append(f"{name}_count {total}")
    lines.append(f"{name}_sum {round(sum(values), 4)}")


def render_prometheus(
    store: Store,
    settings: Settings,
    *,
    queue_pending: int,
    queue_capacity: int,
    running: int = 0,
    workers: int = 1,
) -> str:
    """渲染 Prometheus 文本（窗口固定 24h，避免抓取越来越慢）。"""

    since = _since(24)
    calls = store.llm_calls_since(since, limit=settings.infl_monitor_max_calls)
    timings = store.node_timings_since(since, limit=settings.infl_monitor_max_calls)
    runs = store.runs_since(since)

    lines: list[str] = []
    counts = store.count_runs_by_status()
    lines += [
        "# HELP inferencelab_runs_total 任务数量（按状态，全量）",
        "# TYPE inferencelab_runs_total counter",
    ]
    for status in (*TERMINAL, "queued", "running", "awaiting_approval"):
        lines.append(f'inferencelab_runs_total{{status="{status}"}} {counts.get(status, 0)}')

    lines += [
        "# HELP inferencelab_queue_pending 当前排队任务数",
        "# TYPE inferencelab_queue_pending gauge",
        f"inferencelab_queue_pending {queue_pending}",
        "# HELP inferencelab_queue_capacity 队列容量",
        "# TYPE inferencelab_queue_capacity gauge",
        f"inferencelab_queue_capacity {queue_capacity}",
        "# HELP inferencelab_runs_running 正在执行的任务数",
        "# TYPE inferencelab_runs_running gauge",
        f"inferencelab_runs_running {running}",
        "# HELP inferencelab_workers 并行 worker 数",
        "# TYPE inferencelab_workers gauge",
        f"inferencelab_workers {workers}",
        "# HELP inferencelab_llm_configured 模型是否已配置（1/0，不代表可达）",
        "# TYPE inferencelab_llm_configured gauge",
        f"inferencelab_llm_configured {1 if settings.llm_configured else 0}",
        "# HELP inferencelab_llm_auth_mode 模型鉴权模式",
        "# TYPE inferencelab_llm_auth_mode gauge",
        f'inferencelab_llm_auth_mode{{mode="{settings.llm_auth_mode}"}} 1',
    ]

    outcomes: Counter[tuple[str, str]] = Counter(
        (str(call.get("outcome") or "unknown"), str(call.get("node") or "")) for call in calls
    )
    lines += [
        "# HELP inferencelab_llm_calls_total 模型调用次数（近 24h）",
        "# TYPE inferencelab_llm_calls_total counter",
    ]
    for (outcome, node), count in sorted(outcomes.items()):
        lines.append(f'inferencelab_llm_calls_total{{outcome="{outcome}",node="{node}"}} {count}')

    tokens = Counter()
    for call in calls:
        for kind in ("prompt", "completion", "reasoning"):
            tokens[kind] += call.get(f"{kind}_tokens") or 0
    lines += [
        "# HELP inferencelab_llm_tokens_total 模型 token 用量（近 24h）",
        "# TYPE inferencelab_llm_tokens_total counter",
    ]
    for kind, total in tokens.items():
        lines.append(f'inferencelab_llm_tokens_total{{kind="{kind}"}} {total}')

    _histogram(
        lines,
        "inferencelab_llm_ttft_seconds",
        [value / 1000 for value in (c["ttft_first_token_ms"] for c in calls) if value is not None],
        TTFT_BUCKETS,
        "首 token 延迟（秒，客户端口径）",
    )
    _histogram(
        lines,
        "inferencelab_llm_call_duration_seconds",
        [value / 1000 for value in (c["total_ms"] for c in calls) if value is not None],
        TTFT_BUCKETS,
        "单次模型调用总耗时（秒）",
    )
    _histogram(
        lines,
        "inferencelab_llm_decode_tokens_per_second",
        [float(c["decode_tokens_per_s"]) for c in calls if c.get("decode_tokens_per_s") is not None],
        TOKPS_BUCKETS,
        "decode 吐字速率（token/s，含思考 token）",
    )

    run_durations = [value / 1000 for value in (_run_duration_ms(run) for run in runs) if value is not None]
    _histogram(
        lines,
        "inferencelab_run_duration_seconds",
        run_durations,
        TTFT_BUCKETS,
        "任务总耗时（秒，近 24h）",
    )

    node_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    for item in timings:
        node_values[(item["node"], item["status"])].append(float(item["duration_ms"]) / 1000)
    lines += [
        "# HELP inferencelab_node_duration_seconds 节点耗时（秒，近 24h）",
        "# TYPE inferencelab_node_duration_seconds histogram",
    ]
    for (node, status), values in sorted(node_values.items()):
        for bucket in TTFT_BUCKETS:
            count = sum(1 for value in values if value <= bucket)
            lines.append(f'inferencelab_node_duration_seconds_bucket{{node="{node}",status="{status}",le="{bucket}"}} {count}')
        lines.append(f'inferencelab_node_duration_seconds_bucket{{node="{node}",status="{status}",le="+Inf"}} {len(values)}')
        lines.append(f'inferencelab_node_duration_seconds_count{{node="{node}",status="{status}"}} {len(values)}')
        lines.append(
            f'inferencelab_node_duration_seconds_sum{{node="{node}",status="{status}"}} {round(sum(values), 4)}'
        )

    return "\n".join(lines) + "\n"
