"""API 边界：非法输入、路径越界、队列上限。"""

from __future__ import annotations

from typing import Any

from .conftest import FIXTURES, create_run, upload, wait_for_run


def test_unsupported_upload_is_rejected(client: Any) -> None:
    response = client.post(
        "/api/uploads",
        files={"file": ("weights.bin", b"\x00\x01\x02", "application/octet-stream")},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE"


def test_empty_question_is_rejected(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    response = client.post(
        "/api/runs",
        json={"task_type": "analysis", "question": "", "upload_ids": [upload_id], "options": {}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_analysis_requires_upload(client: Any) -> None:
    response = client.post(
        "/api/runs",
        json={"task_type": "analysis", "question": "没有文件", "upload_ids": [], "options": {}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_upload_id_is_404(client: Any) -> None:
    response = client.post(
        "/api/runs",
        json={"task_type": "analysis", "question": "x", "upload_ids": ["upl_missing"], "options": {}},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_run_and_artifact_are_404(client: Any) -> None:
    assert client.get("/api/runs/run_missing").status_code == 404
    assert client.get("/api/runs/run_missing/report").status_code == 404


def test_artifact_path_traversal_is_blocked(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="analysis", question="路径守卫", upload_ids=[upload_id])
    wait_for_run(client, run_id)

    store = client.app.state.core.store
    malicious = store.add_artifact(
        run_id,
        kind="data",
        filename="passwd",
        rel_path="../../../etc/passwd",
        size=1,
        sha256="0" * 64,
    )
    response = client.get(f"/api/runs/{run_id}/artifacts/{malicious['id']}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PATH_TRAVERSAL"


def test_artifact_cannot_be_read_from_another_run(client: Any) -> None:
    upload_a = upload(client, FIXTURES / "bench_single_config.csv")
    run_a = create_run(client, task_type="analysis", question="A", upload_ids=[upload_a])
    finished_a = wait_for_run(client, run_a)
    artifact_id = finished_a["artifacts"][0]["artifact_id"]

    upload_b = upload(client, FIXTURES / "bench_single_config.csv")
    run_b = create_run(client, task_type="analysis", question="B", upload_ids=[upload_b])

    response = client.get(f"/api/runs/{run_b}/artifacts/{artifact_id}")
    assert response.status_code == 404


def test_meta_never_returns_secret(client: Any) -> None:
    payload = client.get("/api/meta").json()
    assert payload["llm_configured"] is False
    assert "api_key" not in str(payload).lower() or payload["llm_key_fingerprint"] == "unset"
    assert "YOUR_" not in str(payload)
