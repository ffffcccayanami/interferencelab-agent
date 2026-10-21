"""P5 观测：llm_calls / node_timings / 迁移 / 监控聚合 / Prometheus 文本。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.db.store import Store

from .conftest import FIXTURES, create_run, upload, wait_for_run

SAMPLE_CALL = {
    "node": "explain",
    "attempt": 1,
    "model": "deepseek-flash",
    "endpoint_host": "api.example.com",
    "streamed": True,
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "reasoning_tokens": 12,
    "ttft_first_token_ms": 100.0,
    "ttft_first_content_ms": 150.0,
    "total_ms": 400.0,
    "stream_ms": 300.0,
    "prefill_tokens_per_s_est": 1200.0,
    "decode_tokens_per_s": 100.0,
    "content_tokens_per_s": 60.0,
    "finish_reason": "stop",
    "outcome": "ok",
    "error_code": None,
}


def test_store_llm_calls_and_node_timings(workdir: Path) -> None:
    store = Store(workdir / "app.db")
    store.create_run(
        run_id="run_x",
        task_type="analysis",
        question="q",
        options={},
        upload_ids=[],
        run_dir=str(workdir / "run_x"),
    )

    row = store.add_llm_call("run_x", SAMPLE_CALL)
    assert row["streamed"] is True
    assert row["ttft_first_token_ms"] == 100.0

    calls = store.list_llm_calls("run_x")
    assert len(calls) == 1
    assert calls[0]["decode_tokens_per_s"] == 100.0

    store.add_node_timing("run_x", "explain", "ok", 12.5)
    timings = store.list_node_timings("run_x")
    assert timings[0]["node"] == "explain"
    assert timings[0]["duration_ms"] == 12.5

    assert store.llm_calls_since("2000-01-01T00:00:00+00:00") == calls


def test_legacy_database_is_migrated(workdir: Path) -> None:
    db = workdir / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, task_type TEXT, question TEXT, status TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO runs (id, task_type, question, status, created_at) "
        "VALUES ('run_old', 'analysis', 'q', 'succeeded', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = Store(db)
    run = store.get_run("run_old")
    assert run is not None
    assert run["result_summary"] is None
    assert run["duration_ms"] is None

    store.update_run("run_old", duration_ms=120, result_summary_json='{"kind": "analysis"}')
    migrated = store.get_run("run_old")
    assert migrated["duration_ms"] == 120
    assert migrated["result_summary"] == {"kind": "analysis"}

    # 幂等：再次打开不会重复加列或报错
    Store(db)


def _finished_analysis(client: Any) -> str:
    upload_id = upload(client, FIXTURES / "bench_single_config.csv")
    run_id = create_run(client, task_type="analysis", question="监控字段", upload_ids=[upload_id])
    run = wait_for_run(client, run_id)
    assert run["status"] == "succeeded"
    return str(run_id)


def test_run_detail_exposes_timings_and_summary(client: Any) -> None:
    run_id = _finished_analysis(client)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["duration_ms"] is not None and detail["duration_ms"] >= 0
    assert detail["artifact_count"] >= 3
    assert detail["result_summary"]["kind"] == "analysis"
    assert detail["result_summary"]["comparisons"] == 0
    assert detail["llm_calls"] == []

    nodes = {item["node"] for item in detail["node_timings"]}
    assert {"analysis_parse", "analysis_compute", "persist"} <= nodes


def test_run_detail_lists_llm_calls(client: Any) -> None:
    run_id = _finished_analysis(client)
    store = client.app.state.core.store
    store.add_llm_call(run_id, SAMPLE_CALL)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert len(detail["llm_calls"]) == 1
    call = detail["llm_calls"][0]
    assert call["node"] == "explain"
    assert call["ttft_first_token_ms"] == 100.0
    assert call["decode_tokens_per_s"] == 100.0
    assert "prompt" not in str(call).lower() or "prompt_tokens" in call


def test_monitor_summary_aggregates(client: Any) -> None:
    run_id = _finished_analysis(client)
    store = client.app.state.core.store
    store.add_llm_call(run_id, SAMPLE_CALL)
    store.add_llm_call(run_id, {**SAMPLE_CALL, "outcome": "error", "error_code": "LLM_UNAVAILABLE"})

    payload = client.get("/api/monitor/summary").json()
    assert payload["window_hours"] == 24
    assert payload["runs"]["total"] >= 1
    assert payload["runs"]["success_rate"] == 1.0
    assert payload["llm"]["calls"] == 2
    assert payload["llm"]["errors"] == 1
    assert payload["llm"]["ttft_first_token_ms"]["p50"] == 100.0
    assert payload["llm"]["tokens"] == {"prompt": 240, "completion": 60, "reasoning": 24}
    assert payload["nodes"]["analysis_compute"]["calls"] >= 1
    assert payload["alerts"] == []


def test_metrics_endpoint_exports_llm_histograms(client: Any) -> None:
    run_id = _finished_analysis(client)
    store = client.app.state.core.store
    store.add_llm_call(run_id, SAMPLE_CALL)

    body = client.get("/api/metrics").text
    assert 'inferencelab_llm_calls_total{outcome="ok",node="explain"} 1' in body
    assert 'inferencelab_llm_tokens_total{kind="prompt"} 120' in body
    assert "inferencelab_llm_ttft_seconds_bucket" in body
    assert 'inferencelab_llm_ttft_seconds_bucket{le="0.1"} 1' in body
    assert "inferencelab_llm_decode_tokens_per_second_bucket" in body
    assert "inferencelab_node_duration_seconds_bucket" in body
    assert "inferencelab_run_duration_seconds_bucket" in body


def test_health_reports_streaming_field(client: Any) -> None:
    payload = client.get("/api/llm/health").json()
    assert payload["streaming_supported"] in ("yes", "no", "unknown")
    assert payload["provider"] == "openai"


def test_trend_endpoint_buckets(client: Any) -> None:
    run_id = _finished_analysis(client)
    store = client.app.state.core.store
    store.add_llm_call(run_id, SAMPLE_CALL)

    payload = client.get("/api/monitor/trend?hours=24&buckets=24").json()
    assert payload["buckets"] == 24
    assert payload["hours"] == 24
    assert len(payload["series"]) == 24

    points = payload["series"]
    assert sum(point["calls"] for point in points) == 1
    assert sum(point["runs"] for point in points) >= 1

    filled = [point for point in points if point["calls"]]
    assert filled and filled[-1]["ttft_p50"] == 100.0
    assert filled[-1]["decode_p50"] == 100.0
    assert all("ts" in point for point in points)


def test_trend_rejects_invalid_window(client: Any) -> None:
    assert client.get("/api/monitor/trend?hours=0").status_code == 422
    assert client.get("/api/monitor/trend?buckets=3").status_code == 422
