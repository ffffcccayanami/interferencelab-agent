"""任务隔离：两次任务不串目录、不串产物。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .conftest import FIXTURES, create_run, upload, wait_for_run


def test_two_runs_have_isolated_directories(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")

    first_id = create_run(client, task_type="analysis", question="任务一", upload_ids=[upload_id])
    second_id = create_run(client, task_type="analysis", question="任务二", upload_ids=[upload_id])

    first = wait_for_run(client, first_id)
    second = wait_for_run(client, second_id)
    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"

    store = client.app.state.core.store
    first_dir = Path(store.get_run(first_id)["run_dir"]).resolve()
    second_dir = Path(store.get_run(second_id)["run_dir"]).resolve()
    assert first_dir != second_dir
    assert first_dir.name == first_id
    assert second_dir.name == second_id

    first_artifacts = {item["artifact_id"] for item in first["artifacts"]}
    second_artifacts = {item["artifact_id"] for item in second["artifacts"]}
    assert first_artifacts.isdisjoint(second_artifacts)

    first_report = client.get(f"/api/runs/{first_id}/report").text
    second_report = client.get(f"/api/runs/{second_id}/report").text
    assert "任务一" in first_report
    assert "任务一" not in second_report
    assert (first_dir / "artifacts" / "report.md").exists()
    assert (second_dir / "artifacts" / "report.md").exists()


def test_uploads_are_reused_without_cross_contamination(client: Any) -> None:
    upload_a = upload(client, FIXTURES / "bench_concurrency_sample.json")
    upload_b = upload(client, FIXTURES / "prometheus_sample.prom")

    run_a = wait_for_run(
        client,
        create_run(client, task_type="analysis", question="benchmark", upload_ids=[upload_a]),
    )
    run_b = wait_for_run(
        client,
        create_run(client, task_type="analysis", question="prometheus", upload_ids=[upload_b]),
    )

    assert run_a["status"] == "succeeded"
    assert run_b["status"] == "succeeded"
    assert run_a["status"] != "failed"
    assert "直方图分位" not in client.get(f"/api/runs/{run_a['run_id']}/report").text
    assert "直方图分位" in client.get(f"/api/runs/{run_b['run_id']}/report").text
