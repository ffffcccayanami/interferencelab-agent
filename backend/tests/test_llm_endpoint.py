"""模型端点：配置解析、连通性探测、/api/llm/health 与 /api/metrics。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.llm import LLMClient
from app.settings import Settings

from .conftest import FIXTURES, create_run, upload, wait_for_run

FAKE_MODEL = "qwen2.5-7b-instruct"


def _settings(base_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_enabled": True,
        "llm_model_id": FAKE_MODEL,
        "llm_base_url": base_url,
        "llm_auth_mode": "none",
        "llm_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_probe_lists_models_without_auth(llm_server: Any) -> None:
    settings = _settings(llm_server.base_url)
    result = LLMClient(settings).probe()

    assert result["reachable"] is True
    assert result["status_code"] == 200
    assert result["model_present"] is True
    assert FAKE_MODEL in result["models"]
    assert result["endpoint"].startswith("127.0.0.1:")
    assert llm_server.captured["path"] == "/v1/models"
    assert llm_server.captured["authorization"] is None


def test_probe_sends_bearer_token_when_configured(llm_server: Any) -> None:
    settings = _settings(llm_server.base_url, llm_auth_mode="bearer", llm_api_key="sk-" + "b" * 24)
    result = LLMClient(settings).probe()

    assert result["reachable"] is True
    assert llm_server.captured["authorization"] == "Bearer " + "sk-" + "b" * 24


def test_probe_reports_http_error(llm_server: Any) -> None:
    llm_server.status = 503
    llm_server.payload = {"error": "busy"}
    result = LLMClient(_settings(llm_server.base_url)).probe()

    assert result["reachable"] is True
    assert result["status_code"] == 503
    assert result["error"] == "HTTP 503"


def test_probe_reports_unreachable_port() -> None:
    # 端口 1 基本不可能有服务监听
    result = LLMClient(_settings("http://127.0.0.1:1/v1", llm_health_timeout=1.0)).probe()

    assert result["reachable"] is False
    assert result["error"]


def test_health_endpoint_reports_endpoint_and_models(llm_server: Any, workdir: Path) -> None:
    settings = _settings(llm_server.base_url)
    settings.infl_data_dir = workdir / "var"
    settings.ensure_dirs()

    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/llm/health").json()

    assert payload["configured"] is True
    assert payload["reachable"] is True
    assert payload["auth_mode"] == "none"
    assert payload["models"] == [FAKE_MODEL]

    # 响应里不能出现密钥字段
    body = str(payload).lower()
    assert "sk-" not in body and "api_key" not in body


def test_health_endpoint_offline_mode(client: Any) -> None:
    payload = client.get("/api/llm/health").json()
    assert payload["enabled"] is False
    assert payload["reachable"] is False


def test_metrics_endpoint_is_deterministic(client: Any) -> None:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="analysis", question="指标导出", upload_ids=[upload_id])
    wait_for_run(client, run_id)

    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'inferencelab_runs_total{status="succeeded"} 1' in body
    assert "inferencelab_queue_pending 0" in body
    assert "inferencelab_queue_capacity 2" in body
    assert "inferencelab_llm_configured 0" in body


def test_meta_exposes_endpoint_without_key(client: Any) -> None:
    payload = client.get("/api/meta").json()
    assert payload["llm_endpoint"] in ("unset", "127.0.0.1:8000")
    assert payload["llm_auth_mode"] == "bearer"
    assert payload["config_source"]
    assert "api_key" not in payload
