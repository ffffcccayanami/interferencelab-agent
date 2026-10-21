"""确定性指标与配对逻辑的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import ErrorCode, RunFailure
from app.services.metrics import (
    check_pairing,
    compute_metrics,
    require_pairwise,
)
from app.services.parsing import RequestRecord, parse_benchmark_csv, parse_benchmark_json

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def record(label: str, *, ttft: float = 100.0, start: float = 0.0, end: float = 1.0, status: str = "ok") -> RequestRecord:
    return RequestRecord(
        request_id=f"{label}-{start}",
        config_label=label,
        concurrency=4,
        input_tokens=2800,
        output_tokens=128,
        ttft_ms=ttft,
        start_ts=start,
        end_ts=end,
        status=status,
    )


def test_fixture_throughput_and_ttft_match_expected() -> None:
    records = parse_benchmark_json(FIXTURES / "bench_concurrency_sample.json")
    metrics = compute_metrics(records, min_samples=2)

    assert metrics["pairing"]["ok"] is True
    assert metrics["pairing"]["compare_count"] == 1

    groups = {group["config_label"]: group for group in metrics["groups"]}
    assert groups["baseline"]["output_throughput"] == pytest.approx(74.94, abs=0.05)
    assert groups["apc"]["output_throughput"] == pytest.approx(108.95, abs=0.05)

    comparison = metrics["comparisons"][0]
    assert comparison["baseline"] == "baseline"
    assert comparison["candidate"] == "apc"
    assert comparison["output_throughput_pct"] == pytest.approx(45.38, abs=0.2)
    assert comparison["ttft_p50_reduction_pct"] == pytest.approx(36.66, abs=0.2)
    assert comparison["throughput_ratio"] == pytest.approx(1.4538, abs=0.005)


def test_pairing_rejects_insufficient_samples() -> None:
    records = parse_benchmark_json(FIXTURES / "bench_uneven_samples.json")
    metrics = compute_metrics(records, min_samples=2)
    assert metrics["pairing"]["ok"] is False
    assert any("少于下限" in reason for reason in metrics["pairing"]["reasons"])

    with pytest.raises(RunFailure) as excinfo:
        require_pairwise(metrics)
    assert excinfo.value.code is ErrorCode.NON_PAIRWISE_INPUT


def test_single_config_is_allowed_but_has_no_comparison() -> None:
    records = parse_benchmark_csv(FIXTURES / "bench_single_config.csv")
    metrics = compute_metrics(records, min_samples=2)

    assert metrics["pairing"]["ok"] is True
    assert metrics["pairing"]["compare_count"] == 0
    assert metrics["comparisons"] == []
    assert metrics["pairing"]["notes"]


def test_missing_timestamps_disable_throughput() -> None:
    records = [RequestRecord("a", "baseline", 4, 100, 64, 10.0), RequestRecord("b", "baseline", 4, 100, 64, 12.0)]
    metrics = compute_metrics(records, min_samples=2)
    group = metrics["groups"][0]

    assert group["output_throughput"] is None
    assert group["latency_ms_mean"] is None
    assert any("start_ts" in warning for warning in group["warnings"])


def test_errors_are_counted_but_excluded_from_ttft() -> None:
    records = [record("baseline", ttft=100.0), record("baseline", ttft=0.0, status="error")]
    metrics = compute_metrics(records, min_samples=1)
    group = metrics["groups"][0]

    assert group["request_count"] == 2
    assert group["error_count"] == 1
    assert group["error_rate"] == 0.5
    assert group["ttft_ms_p50"] == pytest.approx(100.0)


def test_different_loads_are_separate_groups() -> None:
    same = [record("baseline"), record("baseline", start=1.0, end=2.0)]
    other = [
        RequestRecord("x", "apc", 8, 2800, 128, 90.0, 0.0, 1.0),
        RequestRecord("y", "apc", 8, 2800, 128, 95.0, 1.0, 2.0),
    ]
    result = check_pairing(same + other, min_samples=2)

    assert result.groups == 2
    assert result.comparisons == []
    assert result.ok is True


def test_latency_metrics_keep_two_decimals() -> None:
    """时延类指标固定 2 位小数，保证表格、JSON 与模型解释里的数字一致。"""

    records = [record("baseline", ttft=209.975), record("baseline", ttft=210.025)]
    metrics = compute_metrics(records, min_samples=2)
    group = metrics["groups"][0]

    for key in ("ttft_ms_p50", "ttft_ms_p90", "ttft_ms_p95", "latency_ms_mean"):
        value = group[key]
        assert value is not None
        assert value == round(value, 2)
