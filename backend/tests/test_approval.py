"""受控验证：审批、哈希绑定、脚本执行与产物。"""

from __future__ import annotations

from typing import Any

from .conftest import artifact, create_run, wait_for_run, wait_for_status


def _start_validation(client: Any, entry_id: str = "bench_smoke") -> tuple[str, dict[str, Any]]:
    run_id = create_run(
        client,
        task_type="validation",
        question="执行一次固定验证脚本",
        options={"entry_id": entry_id},
    )
    run = wait_for_status(client, run_id, "awaiting_approval")
    return run_id, run


def test_validation_waits_for_approval_then_runs(client: Any) -> None:
    run_id, run = _start_validation(client)

    approval = run["pending_approval"]
    assert approval is not None
    assert len(approval["approval_id"]) == 64
    assert approval["params"]["concurrency"] == 4

    response = client.post(
        f"/api/runs/{run_id}/approve",
        json={"approval_id": approval["approval_id"], "params": approval["params"]},
    )
    assert response.status_code == 200, response.text

    finished = wait_for_run(client, run_id)
    assert finished["status"] == "succeeded", finished.get("error")
    kinds = {item["kind"] for item in finished["artifacts"]}
    assert "validation_stdout" in kinds
    assert "report" in kinds

    report = client.get(f"/api/runs/{run_id}/report").text
    assert "退出码：0" in report
    assert "受控验证报告" in report


def test_tampered_params_are_rejected(client: Any) -> None:
    run_id, run = _start_validation(client)
    approval = run["pending_approval"]
    tampered = dict(approval["params"])
    tampered["concurrency"] = 7

    response = client.post(
        f"/api/runs/{run_id}/approve",
        json={"approval_id": approval["approval_id"], "params": tampered},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_MISMATCH"

    still = client.get(f"/api/runs/{run_id}").json()
    assert still["status"] == "awaiting_approval"


def test_approval_requires_pending_state(client: Any) -> None:
    from .conftest import FIXTURES, upload

    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="analysis", question="普通分析", upload_ids=[upload_id])
    wait_for_run(client, run_id)

    response = client.post(
        f"/api/runs/{run_id}/approve",
        json={"approval_id": "0" * 64, "params": {}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_unregistered_entry_fails_with_validation_error(client: Any) -> None:
    run_id = create_run(
        client,
        task_type="validation",
        question="未登记条目",
        options={"entry_id": "does_not_exist"},
    )
    run = wait_for_run(client, run_id)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_params_fail_before_execution(client: Any) -> None:
    run_id = create_run(
        client,
        task_type="validation",
        question="参数越界",
        options={"entry_id": "bench_smoke", "params": {"concurrency": 99}},
    )
    run = wait_for_run(client, run_id)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "VALIDATION_ERROR"
    assert artifact(client, run, "validation_stdout") is None


def test_cancel_waiting_approval(client: Any) -> None:
    run_id, _ = _start_validation(client)
    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "cancelled"


def test_cancel_finished_run_conflicts(client: Any) -> None:
    run_id, run = _start_validation(client)
    approval = run["pending_approval"]
    client.post(
        f"/api/runs/{run_id}/approve",
        json={"approval_id": approval["approval_id"], "params": approval["params"]},
    )
    wait_for_run(client, run_id)

    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 409
