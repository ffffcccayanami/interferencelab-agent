"""LLM 客户端：流式观测指标、截断重试、降级阶梯与 response_format 兼容。"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.schemas import ErrorCode, RunFailure
from app.services.llm import LLMCallRecord, LLMClient
from app.settings import Settings


def chunk(content: str | None = None, reasoning: str | None = None, finish: str | None = None, usage: Any = None) -> Any:
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice] if (content or reasoning or finish) else [], usage=usage)


def usage(prompt: int, completion: int, reasoning: int | None = None) -> Any:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


class Stub:
    """可编程的假客户端：stream=True 返回迭代器，否则返回完整响应。"""

    def __init__(self, *, stream_chunks: list[Any] | None = None, plain: Any = None, reject_format: bool = False) -> None:
        self.stream_chunks = stream_chunks or []
        self.plain = plain
        self.reject_format = reject_format
        self.calls: list[dict[str, Any]] = []
        self.completions = SimpleNamespace(create=self.create)

    @property
    def chat(self) -> Any:
        return SimpleNamespace(completions=self.completions)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self.reject_format and "response_format" in kwargs:
            raise RuntimeError("response_format is not supported by this model")
        if kwargs.get("stream"):
            return iter(self.stream_chunks)
        if self.plain is None:
            raise RuntimeError("no plain response configured")
        return self.plain


def build(settings_overrides: dict[str, Any] | None = None, *, budget: Any = None) -> tuple[LLMClient, list[LLMCallRecord]]:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_enabled": True,
        "llm_model_id": "deepseek-flash",
        "llm_base_url": "https://gateway.example.com/v1",
        "llm_auth_mode": "none",
        "llm_api_key": "",
        "llm_max_output_tokens": 4096,
    }
    values.update(settings_overrides or {})
    settings = Settings(**values)  # type: ignore[arg-type]
    records: list[LLMCallRecord] = []
    client = LLMClient(settings, budget=budget, on_call=records.append, run_id="run_test")
    return client, records


def stream_sequence() -> list[Any]:
    """首 token 是思考，随后正文，最后带 usage 的空 choices chunk。"""

    return [
        chunk(reasoning="思", ),
        chunk(reasoning="考"),
        chunk(content='{"summary": "'),
        chunk(content='ok"}'),
        chunk(usage=usage(prompt=120, completion=30, reasoning=12)),
    ]


def test_streaming_metrics_are_recorded() -> None:
    client, records = build()
    client._client = Stub(stream_chunks=stream_sequence())  # noqa: SLF001

    payload = client.complete_json(system="s", user="u", max_tokens=500, node="explain")

    assert payload == {"summary": "ok"}
    assert len(records) == 1
    record = records[0]
    assert record.streamed is True
    assert record.node == "explain"
    assert record.run_id == "run_test"
    assert record.prompt_tokens == 120
    assert record.completion_tokens == 30
    assert record.reasoning_tokens == 12
    assert record.ttft_first_token_ms is not None
    assert record.ttft_first_content_ms is not None
    assert record.ttft_first_content_ms >= record.ttft_first_token_ms
    assert record.decode_tokens_per_s is not None and record.decode_tokens_per_s > 0
    assert record.content_tokens_per_s is not None and record.content_tokens_per_s > 0
    assert record.outcome == "ok"


def test_stream_failure_falls_back_to_plain() -> None:
    client, records = build()
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary": "fallback"}'), finish_reason="stop")],
        usage=usage(prompt=10, completion=5),
    )
    stub = Stub(stream_chunks=[], plain=plain)
    client._client = stub  # noqa: SLF001

    payload = client.complete_json(system="s", user="u", node="explain")

    assert payload == {"summary": "fallback"}
    assert records[0].streamed is False
    assert records[0].ttft_first_token_ms is None
    assert records[0].total_ms is not None
    # 至少尝试过流式（第一个调用 stream=True）
    assert stub.calls[0].get("stream") is True


def test_truncation_retries_and_records_each_attempt() -> None:
    client, records = build()
    first = [chunk(reasoning="很长很长的思考", finish="length"), chunk(usage=usage(prompt=10, completion=4096))]
    second = [chunk(content='{"summary": "ok"}'), chunk(usage=usage(prompt=10, completion=20))]
    stub = Stub()

    calls: list[list[Any]] = [first, second]

    def create(**kwargs: Any) -> Any:
        stub.calls.append(dict(kwargs))
        return iter(calls.pop(0) if calls else second)

    stub.completions = SimpleNamespace(create=create)
    client._client = stub  # noqa: SLF001

    payload = client.complete_json(system="s", user="u", max_tokens=900)

    assert payload == {"summary": "ok"}
    assert [record.attempt for record in records] == [1, 2]
    assert records[0].outcome == "truncated"
    assert records[1].outcome == "ok"
    assert [call["max_tokens"] for call in stub.calls] == [900, 1800]


def test_response_format_is_dropped_when_gateway_rejects_it() -> None:
    client, _ = build()
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary": "ok"}'), finish_reason="stop")],
        usage=usage(prompt=10, completion=5),
    )
    stub = Stub(stream_chunks=[], plain=plain, reject_format=True)
    client._client = stub  # noqa: SLF001

    payload = client.complete_json(system="s", user="u")

    assert payload == {"summary": "ok"}
    assert any("response_format" in call for call in stub.calls)
    assert "response_format" not in stub.calls[-1]


def test_missing_usage_keeps_tokens_null() -> None:
    client, records = build()
    client._client = Stub(stream_chunks=[chunk(content='{"summary": "ok"}')])  # noqa: SLF001

    client.complete_json(system="s", user="u")

    record = records[0]
    assert record.prompt_tokens is None
    assert record.completion_tokens is None
    assert record.decode_tokens_per_s is None


def test_plain_mode_disables_streaming() -> None:
    client, records = build({"llm_streaming": False})
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"), finish_reason="stop")],
        usage=usage(prompt=8, completion=2),
    )
    client._client = Stub(stream_chunks=[chunk(content="x")], plain=plain)  # noqa: SLF001

    text = client.complete_text(system="s", user="u")

    assert text == "hello"
    assert records[0].streamed is False


def test_budget_is_per_client() -> None:
    """每个任务一个 client → 预算不跨任务累计。"""

    settings = {
        "_env_file": None,
        "llm_enabled": True,
        "llm_model_id": "m",
        "llm_base_url": "https://gw.example.com/v1",
        "llm_auth_mode": "none",
        "llm_api_key": "",
        "llm_max_calls_per_run": 1,
    }
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=usage(prompt=1, completion=1),
    )
    first, _ = build(settings)
    first._client = Stub(stream_chunks=[chunk(content="ok")], plain=plain)  # noqa: SLF001
    first.complete_text(system="s", user="u")

    second, _ = build(settings)
    second._client = Stub(stream_chunks=[chunk(content="ok")], plain=plain)  # noqa: SLF001
    second.complete_text(system="s", user="u")  # 不应因第一个 client 用满而失败

    with pytest.raises(RunFailure) as excinfo:
        first.complete_text(system="s", user="u")
    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED


def test_stream_timing_ordering() -> None:
    client, records = build()

    def slow_stream() -> Any:
        yield chunk(reasoning="r")
        time.sleep(0.02)
        yield chunk(content="c")
        time.sleep(0.02)
        yield chunk(usage=usage(4, 2))

    client._client = Stub(stream_chunks=slow_stream())  # noqa: SLF001

    client.complete_text(system="s", user="u")

    record = records[0]
    assert record.ttft_first_content_ms >= record.ttft_first_token_ms
    assert record.stream_ms is not None and record.stream_ms >= 15
