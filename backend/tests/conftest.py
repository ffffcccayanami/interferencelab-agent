"""测试夹具：每个用例独立数据目录 + 独立 SQLite，模型默认关闭。"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LLM_ENABLED", "false")

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
WORK_DIRS = ROOT / "backend" / "var" / "test-workdirs"


class FakeOpenAIServer:
    """本地假 OpenAI 端点：只实现 GET /models，用于探测逻辑测试（不联网）。"""

    def __init__(self) -> None:
        self.status = 200
        self.payload: dict[str, Any] = {
            "object": "list",
            "data": [{"id": "qwen2.5-7b-instruct", "object": "model"}],
        }
        self.captured: dict[str, Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.captured["path"] = self.path
                outer.captured["authorization"] = self.headers.get("Authorization")
                body = json.dumps(outer.payload).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # 静默
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture()
def llm_server() -> Any:
    server = FakeOpenAIServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def workdir() -> Path:
    """在仓库内创建临时目录（Windows 沙箱下系统临时目录可能不可写）。"""

    path = WORK_DIRS / uuid.uuid4().hex[:12]
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def client(workdir: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        llm_enabled=False,
        infl_data_dir=workdir / "var",
        infl_queue_size=2,
        infl_min_group_samples=2,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def upload(client: TestClient, path: Path) -> str:
    with path.open("rb") as handle:
        response = client.post("/api/uploads", files={"file": (path.name, handle)})
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def create_run(
    client: TestClient,
    *,
    task_type: str,
    question: str,
    upload_ids: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> str:
    response = client.post(
        "/api/runs",
        json={
            "task_type": task_type,
            "question": question,
            "upload_ids": upload_ids or [],
            "options": options or {},
        },
    )
    assert response.status_code == 202, response.text
    return str(response.json()["run_id"])


def wait_for_run(client: TestClient, run_id: str, timeout: float = 40.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    terminal = ("succeeded", "failed", "cancelled", "interrupted")
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["status"] in terminal:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态：{payload}")


def wait_for_status(client: TestClient, run_id: str, status: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["status"] == status:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"任务未进入 {status}：{payload}")


def artifact(client: TestClient, run: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for item in run.get("artifacts", []):
        if item.get("kind") == kind:
            return dict(item)
    return None
