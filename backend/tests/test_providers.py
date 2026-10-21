"""P3：厂商 API 与本地/云端部署预设、Anthropic/Gemini 传输。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import llm as llm_module
from app.services.llm import LLMClient
from app.services.providers import detect_deployment, get_profile, list_profiles
from app.settings import ConfigError, Settings


def make(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "llm_enabled": True}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class FakeResponse:
    """同时兼容流式（可迭代）与普通（read）响应的假对象。"""

    status = 200

    def __init__(self, *, lines: list[bytes] | None = None, payload: dict[str, Any] | None = None) -> None:
        self._lines = lines or []
        self._payload = payload or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self._lines)

    def read(self, _size: int | None = None) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_profiles_and_deployment_detection() -> None:
    names = {item["name"] for item in list_profiles()}
    assert {"deepseek", "openai", "qwen", "vllm", "ollama", "lmstudio", "anthropic", "gemini"} <= names
    assert get_profile("VLLM") is not None
    assert get_profile("nope") is None
    assert detect_deployment("http://127.0.0.1:8000/v1") == "local"
    assert detect_deployment("https://api.deepseek.com") == "cloud"


def test_deepseek_profile_fills_defaults() -> None:
    settings = make(llm_profile="deepseek", llm_api_key="sk-" + "a" * 24)
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_auth_mode == "bearer"
    assert settings.llm_provider == "openai"
    assert settings.llm_model_id == "deepseek-chat"
    assert settings.deployment == "cloud"
    settings.validate_for_runtime()


def test_vllm_profile_needs_no_key() -> None:
    settings = make(llm_profile="vllm", llm_model_id="qwen2.5-7b-instruct")
    assert settings.llm_base_url == "http://127.0.0.1:8000/v1"
    assert settings.llm_auth_mode == "none"
    assert settings.deployment == "local"
    assert settings.llm_configured is True
    settings.validate_for_runtime()


def test_explicit_values_override_profile() -> None:
    settings = make(
        llm_profile="vllm",
        llm_base_url="http://10.0.0.5:9000/v1",
        llm_model_id="custom-model",
        llm_deployment="tunnel",
    )
    assert settings.llm_base_url == "http://10.0.0.5:9000/v1"
    assert settings.llm_model_id == "custom-model"
    assert settings.deployment == "tunnel"


def test_anthropic_profile_and_key_requirement() -> None:
    settings = make(llm_profile="anthropic", llm_model_id="claude-3-5-sonnet")
    assert settings.llm_provider == "anthropic"
    assert settings.llm_auth_mode == "x-api-key"
    assert settings.llm_health_path == "/v1/models"
    with pytest.raises(ConfigError):
        settings.validate_for_runtime()

    with_key = make(
        llm_profile="anthropic",
        llm_model_id="claude-3-5-sonnet",
        llm_api_key="sk-ant-" + "b" * 20,
    )
    with_key.validate_for_runtime()


def test_gemini_profile() -> None:
    settings = make(llm_profile="gemini", llm_model_id="gemini-2.0-flash", llm_api_key="AIza" + "c" * 20)
    assert settings.llm_provider == "gemini"
    assert settings.llm_auth_mode == "query"
    settings.validate_for_runtime()


def test_provider_profile_mismatch_is_rejected() -> None:
    settings = make(
        llm_profile="deepseek",
        llm_provider="anthropic",
        llm_api_key="sk-" + "d" * 24,
    )
    with pytest.raises(ConfigError) as excinfo:
        settings.validate_for_runtime()
    assert "LLM_PROVIDER" in str(excinfo.value)


def _anthropic_stream_lines() -> list[bytes]:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 50}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": '{"summary": '}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": '"ok"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
    ]
    return [f"data: {json.dumps(event)}\n".encode() for event in events]


def test_anthropic_stream_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        llm_profile="anthropic",
        llm_model_id="claude-3-5-sonnet",
        llm_api_key="sk-ant-" + "e" * 20,
    )
    records: list[Any] = []
    client = LLMClient(settings, on_call=records.append, run_id="run_x")
    monkeypatch.setattr(llm_module, "_open_url", lambda request, timeout: FakeResponse(lines=_anthropic_stream_lines()))

    payload = client.complete_json(system="s", user="u", node="explain")

    assert payload == {"summary": "ok"}
    record = records[0]
    assert record.streamed is True
    assert record.prompt_tokens == 50
    assert record.completion_tokens == 10
    assert record.ttft_first_token_ms is not None
    assert record.node == "explain"


def test_anthropic_falls_back_to_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        llm_profile="anthropic",
        llm_model_id="claude-3-5-sonnet",
        llm_api_key="sk-ant-" + "f" * 20,
    )
    records: list[Any] = []
    client = LLMClient(settings, on_call=records.append, run_id="run_x")

    def broken(request: Any, timeout: float) -> Any:
        raise RuntimeError("stream not supported")

    plain_payload = {
        "content": [{"type": "text", "text": '{"summary": "fallback"}'}],
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "stop_reason": "end_turn",
    }
    monkeypatch.setattr(llm_module, "_open_url", broken)
    monkeypatch.setattr(llm_module, "_post_json", lambda *args, **kwargs: plain_payload)

    payload = client.complete_json(system="s", user="u")

    assert payload == {"summary": "fallback"}
    assert records[0].streamed is False
    assert records[0].prompt_tokens == 12


def test_gemini_plain_call(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        llm_profile="gemini",
        llm_model_id="gemini-2.0-flash",
        llm_api_key="AIza" + "g" * 20,
    )
    records: list[Any] = []
    client = LLMClient(settings, on_call=records.append, run_id="run_x")

    captured: dict[str, Any] = {}

    def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        return {
            "candidates": [{"content": {"parts": [{"text": '{"summary": "g"}'}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5},
        }

    monkeypatch.setattr(llm_module, "_post_json", fake_post)
    payload = client.complete_json(system="s", user="u")

    assert payload == {"summary": "g"}
    assert "generateContent" in captured["url"]
    record = records[0]
    assert record.streamed is False
    assert record.ttft_first_token_ms is None
    assert record.prompt_tokens == 20
    assert record.completion_tokens == 5


def test_probe_uses_anthropic_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        llm_profile="anthropic",
        llm_model_id="claude-3-5-sonnet",
        llm_api_key="sk-ant-" + "h" * 20,
    )
    captured: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        return FakeResponse(payload={"data": [{"id": "claude-3-5-sonnet"}]})

    monkeypatch.setattr(llm_module, "_open_url", fake_open)
    result = LLMClient(settings).probe()

    assert captured["url"].endswith("/v1/models")
    assert "x-api-key" in captured["headers"]
    assert captured["headers"].get("anthropic-version") == "2023-06-01"
    assert result["provider"] == "anthropic"
    assert result["model_present"] is True


def test_probe_uses_gemini_query_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        llm_profile="gemini",
        llm_model_id="gemini-2.0-flash",
        llm_api_key="AIza" + "i" * 20,
    )
    captured: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        return FakeResponse(payload={"models": [{"name": "models/gemini-2.0-flash"}]})

    monkeypatch.setattr(llm_module, "_open_url", fake_open)
    result = LLMClient(settings).probe()

    assert "/models?key=" in captured["url"]
    assert result["models"] == ["gemini-2.0-flash"]
    assert result["streaming_supported"] == "no"
