"""实验分析闭环：上传 → 排队 → 指标 → 报告 → SSE。"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import FIXTURES, artifact, create_run, upload, wait_for_run

BENCH = FIXTURES / "bench_concurrency_sample.json"


def _result_payload(client: Any, run: dict[str, Any], kind: str = "data") -> dict[str, Any]:
    item = artifact(client, run, kind)
    assert item is not None
    response = client.get(f"/api/runs/{run['run_id']}/artifacts/{item['artifact_id']}")
    assert response.status_code == 200
    return response.json()


def test_analysis_run_produces_metrics_report_and_pdf(client: Any) -> None:
    upload_id = upload(client, BENCH)
    run_id = create_run(
        client,
        task_type="analysis",
        question="对比 baseline 与 apc 在并发 4、2.8K 输入、128 输出下的吞吐与 TTFT",
        upload_ids=[upload_id],
    )
    run = wait_for_run(client, run_id)

    assert run["status"] == "succeeded", run.get("error")
    kinds = {item["kind"] for item in run["artifacts"]}
    assert {"report", "data", "report_pdf"} <= kinds

    payload = _result_payload(client, run)
    groups = {group["config_label"]: group for group in payload["analysis"]["groups"]}
    assert groups["baseline"]["output_throughput"] == pytest.approx(74.94, abs=0.01)
    assert groups["apc"]["output_throughput"] == pytest.approx(108.95, abs=0.01)

    comparison = payload["analysis"]["comparisons"][0]
    assert comparison["baseline"] == "baseline"
    assert comparison["ttft_p50_reduction_pct"] > 30

    report = client.get(f"/api/runs/{run_id}/report")
    assert report.status_code == 200
    assert "同负载对照" in report.text
    assert "结论边界" in report.text


def test_analysis_run_emits_sse_events(client: Any) -> None:
    upload_id = upload(client, BENCH)
    run_id = create_run(client, task_type="analysis", question="复算吞吐", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    response = client.get(f"/api/runs/{run_id}/events?after=0")
    assert response.status_code == 200
    body = response.text
    assert "event: node_started" in body
    assert "event: metric_table" in body
    assert "event: done" in body
    # 节点名必须出现在 data 里，前端与监控才能按节点聚合
    assert '"node": "analysis_compute"' in body
    assert '"node": "persist"' in body

    # after 续传：不重复已消费事件
    seqs = [
        int(line.split(":", 1)[1].strip())
        for line in body.splitlines()
        if line.startswith("id: ")
    ]
    assert seqs == sorted(seqs)
    tail = client.get(f"/api/runs/{run_id}/events?after={seqs[-1]}").text
    assert "event: node_started" not in tail
    assert run["status"] == "succeeded"


def test_csv_single_config_has_no_comparison(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="analysis", question="单组指标", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "succeeded", run.get("error")
    payload = _result_payload(client, run)
    assert payload["analysis"]["comparisons"] == []
    assert payload["analysis"]["pairing"]["ok"] is True

    report = client.get(f"/api/runs/{run_id}/report").text
    assert "没有可配对" in report


def test_prometheus_input_uses_histogram_quantiles(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "prometheus_sample.prom")
    run_id = create_run(client, task_type="analysis", question="服务端指标概览", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "succeeded", run.get("error")
    payload = _result_payload(client, run)
    histogram = payload["prometheus"]["histograms"][0]
    assert histogram["quantiles"]["p50"] is not None

    report = client.get(f"/api/runs/{run_id}/report").text
    assert "直方图分位" in report


def test_uneven_samples_fail_instead_of_producing_comparison(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_uneven_samples.json")
    run_id = create_run(client, task_type="analysis", question="样本不足时不应出对比", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)

    assert run["status"] == "failed"
    assert run["error"]["code"] == "NON_PAIRWISE_INPUT"
    assert artifact(client, run, "report") is None
    events = client.get(f"/api/runs/{run_id}/events").text
    assert "event: error" in events
