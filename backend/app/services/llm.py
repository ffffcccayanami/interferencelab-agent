"""LLM 调用层：流式观测、预算、截断重试与密钥脱敏。

观测口径
--------
* ``ttft_first_token_ms``：请求发出到**第一个非空 delta**（正文或思考），含网络/排队；
* ``ttft_first_content_ms``：到第一个非空正文 delta（推理模型下明显更大）；
* ``decode_tokens_per_s``：``completion_tokens / stream_ms``（含思考 token）；
* ``content_tokens_per_s``：``(completion - reasoning) / 正文窗口``；
* ``prefill_tokens_per_s_est``：``prompt_tokens / ttft`` 的估算值，把网络算进 prefill。

非流式时 TTFT/stream 记为 ``None``，不估算冒充真实值。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from ..schemas import ErrorCode, RunFailure
from ..settings import Settings

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"tvly-[A-Za-z0-9\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{8,}"),
)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def redact(text: str) -> str:
    """把文本中可能出现的密钥替换为 [redacted]。"""

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


class LLMUnavailable(RunFailure):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.LLM_UNAVAILABLE, message)


@dataclass
class Budget:
    max_calls: int
    max_chars: int
    calls: int = 0
    chars: int = 0

    def spend(self, prompt_chars: int) -> None:
        if self.calls + 1 > self.max_calls:
            raise RunFailure(
                ErrorCode.BUDGET_EXCEEDED,
                f"单次任务模型调用次数超过上限 {self.max_calls}",
                {"calls": self.calls, "max_calls": self.max_calls},
            )
        if self.chars + prompt_chars > self.max_chars:
            raise RunFailure(
                ErrorCode.BUDGET_EXCEEDED,
                f"单次任务提示词字符数超过上限 {self.max_chars}",
                {"chars": self.chars, "prompt_chars": prompt_chars, "max_chars": self.max_chars},
            )
        self.calls += 1
        self.chars += prompt_chars


@dataclass
class LLMCallRecord:
    """一次模型尝试的观测结果（只含数字与枚举，不含 prompt）。"""

    run_id: str = ""
    node: str = ""
    attempt: int = 1
    model: str = ""
    endpoint_host: str = ""
    streamed: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    ttft_first_token_ms: float | None = None
    ttft_first_content_ms: float | None = None
    total_ms: float | None = None
    stream_ms: float | None = None
    prefill_tokens_per_s_est: float | None = None
    decode_tokens_per_s: float | None = None
    content_tokens_per_s: float | None = None
    finish_reason: str | None = None
    outcome: str = "ok"
    error_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "attempt": self.attempt,
            "model": self.model,
            "endpoint_host": self.endpoint_host,
            "streamed": self.streamed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "ttft_first_token_ms": self.ttft_first_token_ms,
            "ttft_first_content_ms": self.ttft_first_content_ms,
            "total_ms": self.total_ms,
            "stream_ms": self.stream_ms,
            "prefill_tokens_per_s_est": self.prefill_tokens_per_s_est,
            "decode_tokens_per_s": self.decode_tokens_per_s,
            "content_tokens_per_s": self.content_tokens_per_s,
            "finish_reason": self.finish_reason,
            "outcome": self.outcome,
            "error_code": self.error_code,
        }


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fence = _FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("响应中没有 JSON 对象")
    payload = json.loads(candidate[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层不是对象")
    return payload


def _usage_dict(usage: Any) -> dict[str, int | None]:
    if usage is None:
        return {}
    details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details is not None else None,
    }


def _open_url(request: urllib.request.Request, timeout: float) -> Any:
    """可被测试替换的 HTTP 打开函数。"""

    return urllib.request.urlopen(request, timeout=timeout)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _open_url(request, timeout) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise RuntimeError("响应不是 JSON 对象")
    return data


class LLMClient:
    """OpenAI 兼容客户端（真实密钥仅来自本地 .env；无鉴权端点可用 ``LLM_AUTH_MODE=none``）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        budget: Budget | None = None,
        on_call: Callable[[LLMCallRecord], None] | None = None,
        run_id: str = "",
    ) -> None:
        self.settings = settings
        self.budget = budget or Budget(
            max_calls=settings.llm_max_calls_per_run,
            max_chars=settings.llm_max_prompt_chars,
        )
        self._on_call = on_call
        self.run_id = run_id
        self._client: Any | None = None
        self._init_error: str | None = None

    # ------------------------------------------------------------------
    # 能力与健康
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """是否"已配置"（不代表端点可达，可达性由 ``probe()`` 判断）。"""

        if not self.settings.llm_configured:
            return False
        if self.settings.llm_provider == "openai":
            return self._client_or_none() is not None
        # Anthropic / Gemini 走 urllib，不需要 SDK
        return True

    def _client_or_none(self) -> Any | None:
        if self._client is not None:
            return self._client
        if self._init_error is not None:
            return None
        try:
            from openai import OpenAI
        except ModuleNotFoundError:  # pragma: no cover - 依赖缺失
            self._init_error = "openai 未安装"
            return None
        try:
            api_key = self.settings.llm_api_key_value.strip()
            if not api_key:
                api_key = "not-needed"
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.settings.llm_base_url_normalized or None,
                timeout=self.settings.llm_timeout,
                max_retries=2,
            )
        except Exception as exc:  # pragma: no cover - 参数非法
            self._init_error = redact(str(exc))
            return None
        return self._client

    def probe(self) -> dict[str, Any]:
        """连通性探测：按传输选择 GET 路径（不消耗 token）。"""

        settings = self.settings
        profile = settings.profile
        provider = settings.llm_provider
        health_url = settings.llm_health_url
        result: dict[str, Any] = {
            "enabled": settings.llm_enabled,
            "configured": settings.llm_configured,
            "provider": provider,
            "profile": profile.name if profile else "",
            "deployment": settings.deployment,
            "endpoint": settings.llm_endpoint,
            "auth_mode": settings.llm_auth_mode,
            "model_id": settings.llm_model_id,
            "health_path": settings.llm_health_path,
            "reachable": False,
            "status_code": None,
            "latency_ms": None,
            "models": [],
            "model_present": None,
            "streaming_supported": "yes" if provider in ("openai", "anthropic") else "no",
            "error": None,
        }
        if not settings.llm_enabled:
            result["error"] = "LLM_ENABLED=false（离线模式）"
            return result
        if not settings.llm_base_url_normalized:
            result["error"] = "LLM_BASE_URL 未配置"
            return result

        headers = {"Accept": "application/json", "User-Agent": "inferencelab-agent/0.1"}
        key = settings.llm_api_key_value.strip()
        if provider == "anthropic":
            health_url = f"{settings.llm_base_url_normalized}/v1/models"
            if key:
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"
        elif provider == "gemini":
            health_url = (
                f"{settings.llm_base_url_normalized}/models?key={urllib.parse.quote(key, safe='')}"
            )
        elif key and settings.llm_auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {key}"

        started = time.perf_counter()
        status_code: int | None = None
        body = ""
        try:
            request = urllib.request.Request(health_url, headers=headers, method="GET")
            with _open_url(request, settings.llm_health_timeout) as response:
                status_code = int(response.status)
                body = response.read(512 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            try:
                body = exc.read(512 * 1024).decode("utf-8", "replace")
            except Exception:  # pragma: no cover
                body = ""
            result["error"] = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            result["error"] = redact(f"连接失败：{exc.reason}")
            return result
        except Exception as exc:  # pragma: no cover - 超时等
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            result["error"] = redact(f"{type(exc).__name__}: {exc}")
            return result

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        models = _extract_models(body, provider)
        result.update(
            {
                "reachable": True,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "models": models[:20],
                "model_present": (settings.llm_model_id in models) if models else None,
            }
        )
        if status_code is not None and not 200 <= status_code < 300 and not result["error"]:
            result["error"] = f"HTTP {status_code}"
        return result

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 900,
        node: str = "",
    ) -> dict[str, Any]:
        content = self._chat(system=system, user=user, max_tokens=max_tokens, json_mode=True, node=node)
        try:
            return _extract_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            retry_user = f"{user}\n\n注意：上一次输出不是合法 JSON（{exc}），请只输出一个 JSON 对象。"
            content = self._chat(
                system=system, user=retry_user, max_tokens=max_tokens, json_mode=True, node=node
            )
            try:
                return _extract_json(content)
            except (ValueError, json.JSONDecodeError) as retry_exc:
                raise RunFailure(
                    ErrorCode.LLM_FORMAT_ERROR,
                    "模型两次都未返回合法 JSON",
                    {"error": redact(str(retry_exc))},
                ) from retry_exc

    def complete_text(self, *, system: str, user: str, max_tokens: int = 900, node: str = "") -> str:
        return self._chat(system=system, user=user, max_tokens=max_tokens, json_mode=False, node=node)

    def _record(self, record: LLMCallRecord) -> None:
        if self._on_call is None:
            return
        try:
            self._on_call(record)
        except Exception:  # pragma: no cover - 观测失败不能影响任务
            logger.exception("记录模型调用指标失败（已忽略）")

    def _chat(self, *, system: str, user: str, max_tokens: int, json_mode: bool, node: str) -> str:
        prompt_chars = len(system) + len(user)
        self.budget.spend(prompt_chars)

        attempt = 1
        current_max = max_tokens
        while True:
            try:
                content, finish_reason, metrics = self._invoke(system, user, current_max, json_mode)
            except LLMUnavailable:
                self._record(
                    LLMCallRecord(
                        run_id=self.run_id,
                        node=node,
                        attempt=attempt,
                        model=self.settings.llm_model_id,
                        endpoint_host=self.settings.llm_endpoint,
                        outcome="error",
                        error_code=ErrorCode.LLM_UNAVAILABLE.value,
                    )
                )
                raise

            text = (content or "").strip()
            outcome = "ok" if text else ("truncated" if finish_reason == "length" else "empty")
            metrics.update(
                {
                    "run_id": self.run_id,
                    "node": node,
                    "attempt": attempt,
                    "model": self.settings.llm_model_id,
                    "endpoint_host": self.settings.llm_endpoint,
                    "finish_reason": finish_reason,
                    "outcome": outcome,
                }
            )
            self._record(LLMCallRecord(**metrics))
            if text:
                return text

            bump = min(current_max * 2, self.settings.llm_max_output_tokens)
            if finish_reason == "length" and bump > current_max:
                logger.warning("模型输出被截断（max_tokens=%s），按推理模型处理：提升到 %s 重试", current_max, bump)
                current_max = bump
                attempt += 1
                continue
            break

        if finish_reason == "length":
            raise RunFailure(
                ErrorCode.LLM_FORMAT_ERROR,
                "模型输出被 max_tokens 截断（推理模型的思考过程也会占用预算），"
                "请提高 LLM_MAX_OUTPUT_TOKENS 或缩短输入证据",
                {"finish_reason": "length", "max_tokens": current_max},
            )
        raise RunFailure(
            ErrorCode.LLM_FORMAT_ERROR,
            "模型返回空内容",
            {"finish_reason": finish_reason},
        )

    # ------------------------------------------------------------------
    # 传输层分发：OpenAI 兼容 / Anthropic / Gemini
    # ------------------------------------------------------------------
    def _invoke(
        self, system: str, user: str, max_tokens: int, json_mode: bool
    ) -> tuple[str, str | None, dict[str, Any]]:
        provider = self.settings.llm_provider
        if provider == "anthropic":
            return self._invoke_anthropic(system, user, max_tokens, json_mode)
        if provider == "gemini":
            return self._invoke_gemini(system, user, max_tokens, json_mode)
        return self._invoke_openai(system, user, max_tokens, json_mode)

    def _invoke_openai(
        self, system: str, user: str, max_tokens: int, json_mode: bool
    ) -> tuple[str, str | None, dict[str, Any]]:
        client = self._client_or_none()
        if client is None:
            raise LLMUnavailable(f"模型不可用：{self._init_error or '未配置 LLM_API_KEY'}")

        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            return self._invoke_ladder(client, dict(kwargs))
        except LLMUnavailable as exc:
            # 少数网关不支持 response_format：去掉后重走一遍降级阶梯
            if "response_format" in kwargs and "response_format" in exc.message.lower():
                fallback = dict(kwargs)
                fallback.pop("response_format", None)
                return self._invoke_ladder(client, fallback)
            raise

    def _invoke_ladder(self, client: Any, kwargs: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
        errors: list[str] = []

        if self.settings.llm_streaming:
            usage_options = (True, False) if self.settings.llm_stream_include_usage else (False,)
            for include_usage in usage_options:
                try:
                    return self._invoke_stream(client, dict(kwargs), include_usage=include_usage)
                except Exception as exc:  # noqa: BLE001 - 逐级降级
                    errors.append(redact(f"stream(usage={include_usage}): {exc}"))
                    continue

        try:
            return self._invoke_plain(client, dict(kwargs))
        except Exception as exc:  # noqa: BLE001
            errors.append(redact(f"plain: {exc}"))

        raise LLMUnavailable("模型调用失败：" + " | ".join(errors[-2:]))

    def _invoke_stream(
        self,
        client: Any,
        kwargs: dict[str, Any],
        *,
        include_usage: bool,
    ) -> tuple[str, str | None, dict[str, Any]]:
        kwargs["stream"] = True
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}

        started = time.perf_counter()
        first_any: float | None = None
        first_content: float | None = None
        last_token: float | None = None
        parts: list[str] = []
        reasoning_chars = 0
        usage: Any = None
        finish_reason: str | None = None
        chunks = 0

        stream: Iterator[Any] = client.chat.completions.create(**kwargs)
        for chunk in stream:
            chunks += 1
            now = time.perf_counter()
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            for choice in (getattr(chunk, "choices", None) or []):
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                piece = getattr(delta, "content", None)
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_chars += len(reasoning)
                if piece or reasoning:
                    if first_any is None:
                        first_any = now
                    last_token = now
                if piece:
                    if first_content is None:
                        first_content = now
                    parts.append(piece)
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason

        if chunks == 0:
            raise RuntimeError("流式响应为空")

        total_ms = (time.perf_counter() - started) * 1000
        content = "".join(parts)
        metrics = self._stream_metrics(
            started=started,
            first_any=first_any,
            first_content=first_content,
            last_token=last_token,
            total_ms=total_ms,
            usage=_usage_dict(usage),
            streamed=True,
        )
        if not content and usage is None:
            # 没有任何可用内容也没有 usage：当作该级失败，继续降级
            raise RuntimeError("流式响应没有内容与 usage")
        return content, finish_reason, metrics

    def _invoke_plain(self, client: Any, kwargs: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
        started = time.perf_counter()
        response = client.chat.completions.create(**kwargs)
        total_ms = (time.perf_counter() - started) * 1000
        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        usage = _usage_dict(getattr(response, "usage", None))
        metrics = {
            "streamed": False,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "ttft_first_token_ms": None,
            "ttft_first_content_ms": None,
            "total_ms": round(total_ms, 2),
            "stream_ms": None,
            "prefill_tokens_per_s_est": None,
            "decode_tokens_per_s": None,
            "content_tokens_per_s": None,
        }
        return content, getattr(choice, "finish_reason", None), metrics

    # ------------------------------------------------------------------
    # Anthropic Messages API
    # ------------------------------------------------------------------
    def _anthropic_headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.settings.llm_api_key_value,
        }

    def _invoke_anthropic(
        self, system: str, user: str, max_tokens: int, json_mode: bool
    ) -> tuple[str, str | None, dict[str, Any]]:
        system_text = system
        if json_mode:
            system_text = f"{system}\n\n只输出一个 JSON 对象，不要输出其他文字。"
        payload = {
            "model": self.settings.llm_model_id,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "system": system_text,
            "messages": [{"role": "user", "content": user}],
        }
        url = f"{self.settings.llm_base_url_normalized}/v1/messages"

        if self.settings.llm_streaming:
            try:
                return self._anthropic_stream(url, payload)
            except Exception as exc:  # noqa: BLE001 - 回退非流式
                logger.warning("Anthropic 流式调用失败，回退非流式：%s", redact(str(exc)))

        started = time.perf_counter()
        data = _post_json(url, self._anthropic_headers(), payload, self.settings.llm_timeout)
        total_ms = (time.perf_counter() - started) * 1000
        blocks = data.get("content") or []
        content = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        stop = data.get("stop_reason")
        finish = "length" if stop == "max_tokens" else (str(stop) if stop else None)
        metrics = self._plain_metrics(
            total_ms,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
        )
        return content, finish, metrics

    def _anthropic_stream(self, url: str, payload: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
        headers = {**self._anthropic_headers(), "accept": "text/event-stream"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")

        started = time.perf_counter()
        first_any: float | None = None
        first_content: float | None = None
        last_token: float | None = None
        parts: list[str] = []
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        finish: str | None = None
        events = 0

        with _open_url(request, self.settings.llm_timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if not data_text:
                    continue
                try:
                    event = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                events += 1
                now = time.perf_counter()
                etype = event.get("type")
                if etype == "message_start":
                    usage = (event.get("message") or {}).get("usage") or {}
                    prompt_tokens = usage.get("input_tokens", prompt_tokens)
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        piece = str(delta.get("text") or "")
                        if piece:
                            if first_any is None:
                                first_any = now
                            if first_content is None:
                                first_content = now
                            last_token = now
                            parts.append(piece)
                    elif dtype == "thinking_delta":
                        thought = str(delta.get("thinking") or "")
                        if thought:
                            if first_any is None:
                                first_any = now
                            last_token = now
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    completion_tokens = usage.get("output_tokens", completion_tokens)
                    stop = (event.get("delta") or {}).get("stop_reason")
                    if stop:
                        finish = "length" if stop == "max_tokens" else str(stop)
                elif etype == "error":
                    raise RuntimeError(str(event.get("error"))[:200])

        if events == 0:
            raise RuntimeError("Anthropic 流式响应为空")

        total_ms = (time.perf_counter() - started) * 1000
        metrics = self._stream_metrics(
            started=started,
            first_any=first_any,
            first_content=first_content,
            last_token=last_token,
            total_ms=total_ms,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": None,
            },
            streamed=True,
        )
        return "".join(parts), finish, metrics

    # ------------------------------------------------------------------
    # Gemini generateContent（非流式）
    # ------------------------------------------------------------------
    def _invoke_gemini(
        self, system: str, user: str, max_tokens: int, json_mode: bool
    ) -> tuple[str, str | None, dict[str, Any]]:
        key = urllib.parse.quote(self.settings.llm_api_key_value, safe="")
        url = (
            f"{self.settings.llm_base_url_normalized}/models/"
            f"{self.settings.llm_model_id}:generateContent?key={key}"
        )
        generation: dict[str, Any] = {"temperature": 0.1, "maxOutputTokens": max_tokens}
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation,
        }

        started = time.perf_counter()
        data = _post_json(url, {"content-type": "application/json"}, payload, self.settings.llm_timeout)
        total_ms = (time.perf_counter() - started) * 1000

        candidates = data.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        content = "".join(
            str(part.get("text") or "") for part in parts if isinstance(part, dict)
        )
        raw_finish = candidate.get("finishReason")
        finish = "length" if raw_finish == "MAX_TOKENS" else (str(raw_finish).lower() if raw_finish else None)
        usage = data.get("usageMetadata") or {}
        metrics = self._plain_metrics(
            total_ms,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
            reasoning_tokens=usage.get("thoughtsTokenCount"),
        )
        return content, finish, metrics

    @staticmethod
    def _plain_metrics(
        total_ms: float,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        reasoning_tokens: int | None = None,
    ) -> dict[str, Any]:
        return {
            "streamed": False,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "ttft_first_token_ms": None,
            "ttft_first_content_ms": None,
            "total_ms": round(total_ms, 2),
            "stream_ms": None,
            "prefill_tokens_per_s_est": None,
            "decode_tokens_per_s": None,
            "content_tokens_per_s": None,
        }

    @staticmethod
    def _stream_metrics(
        *,
        started: float,
        first_any: float | None,
        first_content: float | None,
        last_token: float | None,
        total_ms: float,
        usage: dict[str, int | None],
        streamed: bool,
    ) -> dict[str, Any]:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        reasoning = usage.get("reasoning_tokens")

        ttft_token_raw = (first_any - started) * 1000 if first_any is not None else None
        ttft_content_raw = (first_content - started) * 1000 if first_content is not None else None
        stream_s = (last_token - first_any) if (last_token and first_any) else None
        content_s = (last_token - first_content) if (last_token and first_content) else None

        decode_tps = None
        if completion and stream_s and stream_s > 0:
            decode_tps = round(completion / stream_s, 2)

        content_tokens = None
        if completion is not None and reasoning is not None:
            content_tokens = max(0, completion - reasoning)
        content_tps = None
        if content_tokens and content_s and content_s > 0:
            content_tps = round(content_tokens / content_s, 2)

        prefill_est = None
        if prompt and ttft_token_raw and ttft_token_raw > 0:
            prefill_est = round(prompt / (ttft_token_raw / 1000), 2)

        return {
            "streamed": streamed,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "ttft_first_token_ms": round(ttft_token_raw, 2) if ttft_token_raw is not None else None,
            "ttft_first_content_ms": round(ttft_content_raw, 2) if ttft_content_raw is not None else None,
            "total_ms": round(total_ms, 2),
            "stream_ms": round(stream_s * 1000, 2) if stream_s is not None else None,
            "prefill_tokens_per_s_est": prefill_est,
            "decode_tokens_per_s": decode_tps,
            "content_tokens_per_s": content_tps,
        }


def _extract_model_ids(body: str) -> list[str]:
    """从 /models 响应里提取模型 id（兼容 OpenAI 与少量变体）。"""

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            identifier = item.get("id") or item.get("model")
            if identifier:
                models.append(str(identifier))
        elif isinstance(item, str):
            models.append(item)
    return models


def _extract_gemini_models(body: str) -> list[str]:
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name:
            names.append(str(name).removeprefix("models/"))
    return names


def _extract_models(body: str, provider: str) -> list[str]:
    if provider == "gemini":
        return _extract_gemini_models(body)
    return _extract_model_ids(body)
