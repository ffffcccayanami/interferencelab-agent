"""输入解析测试：类型判定、格式校验、Prometheus 分位估算。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import ErrorCode
from app.services import parsing
from app.services.parsing import DOCUMENT, PROMETHEUS, ParseError, parse_benchmark_csv, parse_benchmark_json

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_benchmark_json_parsed() -> None:
    records = parse_benchmark_json(FIXTURES / "bench_concurrency_sample.json")
    assert len(records) == 40
    assert {record.config_label for record in records} == {"baseline", "apc"}
    assert records[0].concurrency == 4


def test_benchmark_csv_parsed() -> None:
    records = parse_benchmark_csv(FIXTURES / "bench_single_config.csv")
    assert len(records) == 10
    assert all(record.config_label == "apc" for record in records)


def test_benchmark_json_missing_field_is_rejected(workdir: Path) -> None:
    broken = workdir / "broken.json"
    broken.write_text(
        json.dumps({"requests": [{"config_label": "baseline", "concurrency": 1, "input_tokens": 10, "output_tokens": 5}]}),
        encoding="utf-8",
    )
    with pytest.raises(ParseError) as excinfo:
        parse_benchmark_json(broken)
    assert excinfo.value.code is ErrorCode.PARSE_ERROR
    assert "ttft_ms" in json.dumps(excinfo.value.details, ensure_ascii=False)


def test_unsupported_suffix_rejected() -> None:
    with pytest.raises(ParseError) as excinfo:
        parsing.detect_type("weights.bin")
    assert "不支持" in excinfo.value.message


def test_prometheus_gauges_and_histogram_quantiles() -> None:
    text = (FIXTURES / "prometheus_sample.prom").read_text(encoding="utf-8")
    samples = parsing.parse_prometheus_text(text)
    summary = parsing.summarize_prometheus(samples)

    assert summary["gauges"]["vllm:num_requests_running"] == pytest.approx(4.0)
    histogram = summary["histograms"][0]
    assert histogram["name"] == "vllm:time_to_first_token_seconds"
    assert histogram["count"] == pytest.approx(20.0)
    # 目标 10/20：落在 (0.1, 0.2] 桶内，线性插值 ≈ 0.1667 s
    assert histogram["quantiles"]["p50"] == pytest.approx(0.1667, abs=0.01)
    assert histogram["quantiles"]["p95"] > histogram["quantiles"]["p90"]


def test_text_document_detection(workdir: Path) -> None:
    doc = workdir / "notes.txt"
    doc.write_text("前缀缓存按块匹配，命中率取决于块大小。\n", encoding="utf-8")
    assert parsing.detect_type(doc.name, doc) == DOCUMENT


def test_prometheus_text_detection(workdir: Path) -> None:
    prom = workdir / "metrics.txt"
    prom.write_text('vllm:num_requests_running{model="x"} 2\n', encoding="utf-8")
    assert parsing.detect_type(prom.name, prom) == PROMETHEUS


def test_markdown_document_loads() -> None:
    result = parsing.parse_input(FIXTURES / "docs" / "apc_notes.md", "apc_notes.md")
    assert result.kind == DOCUMENT
    assert "前缀缓存" in result.text
