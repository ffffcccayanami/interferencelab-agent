"""P4：判据、基准键与回归检测。"""

from __future__ import annotations

from typing import Any

from app.services.evaluation import (
    EVAL_FAIL,
    EVAL_NOT_APPLICABLE,
    EVAL_PASS,
    benchmark_key,
    detect_regression,
    evaluate_metrics,
    resolve_criteria,
)
from app.settings import Settings

from .conftest import FIXTURES, create_run, upload, wait_for_run

DEFAULT = {"min_throughput_pct": 10.0, "min_ttft_reduction_pct": 5.0, "max_error_rate": 0.05}


def test_evaluate_metrics_pass() -> None:
    metrics = {
        "comparisons": [{"output_throughput_pct": 25.0, "ttft_p50_reduction_pct": 12.0}],
        "groups": [{"error_rate": 0.0}, {"error_rate": 0.02}],
    }
    result = evaluate_metrics(metrics, DEFAULT)
    assert result["status"] == EVAL_PASS
    assert len(result["checks"]) == 3
    assert all(check["passed"] for check in result["checks"])
    assert result["comparison"]["throughput_pct"] == 25.0


def test_evaluate_metrics_fail_on_threshold() -> None:
    metrics = {
        "comparisons": [{"output_throughput_pct": 4.0, "ttft_p50_reduction_pct": 20.0}],
        "groups": [{"error_rate": 0.0}],
    }
    result = evaluate_metrics(metrics, DEFAULT)
    assert result["status"] == EVAL_FAIL
    assert "throughput_pct" in result["reason"]


def test_evaluate_metrics_fail_on_error_rate() -> None:
    metrics = {
        "comparisons": [{"output_throughput_pct": 30.0, "ttft_p50_reduction_pct": 10.0}],
        "groups": [{"error_rate": 0.2}],
    }
    result = evaluate_metrics(metrics, DEFAULT)
    assert result["status"] == EVAL_FAIL
    error_check = next(check for check in result["checks"] if check["name"] == "max_error_rate")
    assert error_check["passed"] is False


def test_evaluate_metrics_not_applicable_without_comparison() -> None:
    result = evaluate_metrics({"comparisons": [], "groups": []}, DEFAULT)
    assert result["status"] == EVAL_NOT_APPLICABLE


def test_resolve_criteria_prefers_options() -> None:
    settings = Settings(_env_file=None, llm_enabled=False, infl_eval_min_throughput_pct=3.0)
    criteria = resolve_criteria(settings, {"criteria": {"min_throughput_pct": 12, "max_error_rate": 0.5}})
    assert criteria["min_throughput_pct"] == 12.0
    assert criteria["max_error_rate"] == 0.5
    assert criteria["min_ttft_reduction_pct"] == settings.infl_eval_min_ttft_reduction_pct


def test_detect_regression() -> None:
    baseline = {"evaluation": {"comparison": {"throughput_pct": 30.0, "ttft_p50_reduction_pct": 20.0}}}
    current = {"comparison": {"throughput_pct": 10.0, "ttft_p50_reduction_pct": 18.0}}

    regression = detect_regression(current, baseline, tolerance_pct=5.0)
    assert regression is not None
    assert [item["metric"] for item in regression["details"]] == ["throughput_pct"]

    stable = {"comparison": {"throughput_pct": 28.0, "ttft_p50_reduction_pct": 17.0}}
    assert detect_regression(stable, baseline, tolerance_pct=5.0) is None


def test_benchmark_key_is_order_independent() -> None:
    a = [{"sha256": "aaa"}, {"sha256": "bbb"}]
    b = [{"sha256": "bbb"}, {"sha256": "aaa"}]
    assert benchmark_key(a, "analysis") == benchmark_key(b, "analysis")
    assert benchmark_key([{"sha256": ""}], "analysis") is None
    assert benchmark_key(a, "analysis") != benchmark_key(a, "research")


def test_analysis_run_writes_evaluation_and_baseline(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_concurrency_sample.json")

    first = create_run(client, task_type="analysis", question="判据 A", upload_ids=[upload_id])
    first_run = wait_for_run(client, first)
    assert first_run["status"] == "succeeded"
    assert first_run["eval_status"] in (EVAL_PASS, EVAL_FAIL)
    assert first_run["baseline_run_id"] is None

    first_detail = client.get(f"/api/runs/{first}").json()
    evaluation = first_detail["result_summary"]["evaluation"]
    assert evaluation["status"] == first_run["eval_status"]
    assert evaluation["checks"]
    assert any(check["name"] == "throughput_pct" for check in evaluation["checks"])

    report = client.get(f"/api/runs/{first}/report").content.decode("utf-8")
    assert "判据与结论" in report

    second = create_run(client, task_type="analysis", question="判据 B", upload_ids=[upload_id])
    second_run = wait_for_run(client, second)
    assert second_run["baseline_run_id"] == first

    summary = client.get("/api/monitor/summary").json()
    evaluation_stats = summary["runs"]["evaluation"]
    assert evaluation_stats["evaluated"] >= 2
    assert evaluation_stats["pass_rate"] is not None
    assert evaluation_stats["counts"]["pass"] + evaluation_stats["counts"]["fail"] >= 2
