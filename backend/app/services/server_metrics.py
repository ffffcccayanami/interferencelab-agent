"""可选的服务端指标采集（vLLM 类端点的 Prometheus /metrics）。

默认关闭：配置 ``LLM_METRICS_URL`` 后才生效，且只在任务开始/结束各抓一次，
抓不到就记 ``unavailable``，不影响任务本身。
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Any

from ..settings import Settings

logger = logging.getLogger(__name__)

# 我们关心的 vLLM 指标（存在就采集，不存在就跳过）
WANTED_PREFIXES = (
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:request_success_total",
    "vllm:time_to_first_token_seconds",
    "vllm:request_prefill_time_seconds",
    "vllm:request_decode_time_seconds",
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:gpu_cache_usage_perc",
)


def _wanted(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in WANTED_PREFIXES)


def parse_metrics_text(text: str) -> dict[str, list[dict[str, Any]]]:
    """把 Prometheus 文本简化成 ``{metric_name: [{labels, value}, ...]}``。"""

    result: dict[str, list[dict[str, Any]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, value_part = line.rpartition(" ")
        try:
            value = float(value_part)
        except ValueError:
            continue
        labels: dict[str, str] = {}
        if "{" in name_part:
            name, _, rest = name_part.partition("{")
            for pair in rest.rstrip("}").split(","):
                if "=" not in pair:
                    continue
                key, _, val = pair.partition("=")
                labels[key.strip()] = val.strip().strip('"')
        else:
            name = name_part.strip()
        if not _wanted(name):
            continue
        result.setdefault(name, []).append({"labels": labels, "value": value})
    return result


def capture(settings: Settings) -> dict[str, Any]:
    """抓一次服务端指标；返回 ``{status, metrics, error}``。"""

    url = settings.llm_metrics_url.strip()
    if not url:
        return {"status": "disabled", "metrics": {}, "error": None}

    headers = {"Accept": "text/plain", "User-Agent": "inferencelab-agent/0.1"}
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=settings.llm_metrics_timeout) as response:
            body = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"status": "unavailable", "metrics": {}, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - 任何异常都只降级
        return {"status": "unavailable", "metrics": {}, "error": f"{type(exc).__name__}: {exc}"}

    metrics = parse_metrics_text(body)
    return {"status": "ok", "metrics": metrics, "error": None}


def diff_snapshots(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    """两个快照的增量（只对单调计数器做差，gauge 取末值）。"""

    if start.get("status") != "ok" or end.get("status") != "ok":
        return {"status": "unavailable", "counters": {}, "gauges": {}}

    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}
    for name, samples in (end.get("metrics") or {}).items():
        if name.endswith("_bucket") or name.endswith("_sum") or name.endswith("_count") or name in (
            "vllm:num_requests_running",
            "vllm:num_requests_waiting",
            "vllm:gpu_cache_usage_perc",
        ):
            if samples:
                gauges[name] = samples[-1]["value"]
            continue
        start_total = sum(item["value"] for item in (start.get("metrics", {}).get(name) or []))
        end_total = sum(item["value"] for item in samples)
        counters[name] = round(end_total - start_total, 4)
    return {"status": "ok", "counters": counters, "gauges": gauges}
