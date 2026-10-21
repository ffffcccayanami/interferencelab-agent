"""输入解析：benchmark JSON / CSV / Prometheus 文本 / 技术文档。

解析阶段只做"格式校验 + 归一化"，不做任何统计推断；数值计算全部在
``metrics.py`` 中完成，保证报告里的每个数字都可复算。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import ErrorCode, RunFailure

BENCHMARK_JSON = "benchmark_json"
BENCHMARK_CSV = "benchmark_csv"
PROMETHEUS = "prometheus"
DOCUMENT = "document"

SUPPORTED_SUFFIXES = {".json", ".csv", ".md", ".txt", ".prom", ".log", ".pdf"}

_METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[^\s]+)"
    r"(?:\s+[0-9.eE+\-]+)?\s*$"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


class ParseError(RunFailure):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(ErrorCode.PARSE_ERROR, message, details)


@dataclass(frozen=True)
class RequestRecord:
    """一条请求级 benchmark 记录。"""

    request_id: str
    config_label: str
    concurrency: int
    input_tokens: int
    output_tokens: int
    ttft_ms: float
    start_ts: float | None = None
    end_ts: float | None = None
    status: str = "ok"


@dataclass(frozen=True)
class PromSample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass
class ParseResult:
    kind: str
    records: list[RequestRecord] = field(default_factory=list)
    samples: list[PromSample] = field(default_factory=list)
    text: str = ""
    notes: list[str] = field(default_factory=list)


def detect_type(filename: str, path: Path | None = None) -> str:
    """按扩展名 + 内容特征判定输入类型。"""

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ParseError(
            f"不支持的文件类型 {suffix or '(无扩展名)'}",
            {"suffix": suffix, "supported": sorted(SUPPORTED_SUFFIXES)},
        )
    if suffix == ".json":
        return BENCHMARK_JSON
    if suffix == ".csv":
        return BENCHMARK_CSV
    if suffix == ".pdf":
        return DOCUMENT
    if suffix == ".md":
        return DOCUMENT
    if suffix == ".prom":
        return PROMETHEUS
    # .txt / .log：优先当 Prometheus 文本，其次当技术文档
    if path is not None and path.exists():
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        if "metric" in head or re.search(r"^[a-zA-Z_:][a-zA-Z0-9_:]*\{?.*\}\s+[0-9.eE+\-]+$", head, re.MULTILINE):
            return PROMETHEUS
    return DOCUMENT


def _number(raw: Any, field_name: str, index: int, *, required: bool = True) -> float | None:
    if raw is None or raw == "":
        if required:
            raise ParseError(f"第 {index} 条记录缺少字段 {field_name}", {"index": index, "field": field_name})
        return None
    if isinstance(raw, bool):
        raise ParseError(f"第 {index} 条记录字段 {field_name} 不是数值", {"index": index, "field": field_name})
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ParseError(
            f"第 {index} 条记录字段 {field_name} 不是数值: {raw!r}",
            {"index": index, "field": field_name},
        ) from exc


def _coerce_record(raw: dict[str, Any], index: int) -> RequestRecord:
    if not isinstance(raw, dict):
        raise ParseError(f"第 {index} 条记录不是对象", {"index": index})

    def text_of(key: str, default: str) -> str:
        value = raw.get(key, default)
        return str(value).strip() if value is not None else default

    concurrency = _number(raw.get("concurrency"), "concurrency", index)
    input_tokens = _number(raw.get("input_tokens"), "input_tokens", index)
    output_tokens = _number(raw.get("output_tokens"), "output_tokens", index)
    ttft_ms = _number(raw.get("ttft_ms"), "ttft_ms", index)
    start_ts = _number(raw.get("start_ts"), "start_ts", index, required=False)
    end_ts = _number(raw.get("end_ts"), "end_ts", index, required=False)

    if concurrency is None or concurrency < 1:
        raise ParseError(f"第 {index} 条记录 concurrency 必须 >= 1", {"index": index})
    if input_tokens is None or input_tokens < 1:
        raise ParseError(f"第 {index} 条记录 input_tokens 必须 >= 1", {"index": index})
    if output_tokens is None or output_tokens < 0:
        raise ParseError(f"第 {index} 条记录 output_tokens 必须 >= 0", {"index": index})
    if ttft_ms is None or ttft_ms < 0:
        raise ParseError(f"第 {index} 条记录 ttft_ms 必须 >= 0", {"index": index})
    if start_ts is not None and end_ts is not None and end_ts < start_ts:
        raise ParseError(f"第 {index} 条记录 end_ts 早于 start_ts", {"index": index})

    return RequestRecord(
        request_id=text_of("request_id", f"req-{index}"),
        config_label=text_of("config_label", "default"),
        concurrency=int(concurrency),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        ttft_ms=float(ttft_ms),
        start_ts=start_ts,
        end_ts=end_ts,
        status=text_of("status", "ok").lower(),
    )


def parse_benchmark_json(path: Path) -> list[RequestRecord]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 解析失败：{exc.msg} (line {exc.lineno})", {"line": exc.lineno}) from exc

    if isinstance(payload, dict):
        records_raw = payload.get("requests")
        if records_raw is None:
            raise ParseError(
                "JSON 顶层对象缺少 requests 数组",
                {"expected": {"requests": [{...}]}, "actual_keys": sorted(payload)[:10]},
            )
    elif isinstance(payload, list):
        records_raw = payload
    else:
        raise ParseError("JSON 顶层必须是对象或数组", {"actual": type(payload).__name__})

    if not isinstance(records_raw, list) or not records_raw:
        raise ParseError("requests 必须是非空数组", {"count": len(records_raw) if isinstance(records_raw, list) else None})

    records: list[RequestRecord] = []
    errors: list[str] = []
    for index, raw in enumerate(records_raw, start=1):
        try:
            records.append(_coerce_record(raw, index))
        except ParseError as exc:
            errors.append(exc.message)
    if errors:
        raise ParseError(f"benchmark JSON 有 {len(errors)} 条记录不合法", {"errors": errors[:5]})
    return records


def parse_benchmark_csv(path: Path) -> list[RequestRecord]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ParseError("CSV 缺少表头", {"expected": ["request_id", "config_label", "concurrency", "..."]})
        required = {"config_label", "concurrency", "input_tokens", "output_tokens", "ttft_ms"}
        missing = required - {name.strip() for name in reader.fieldnames}
        if missing:
            raise ParseError("CSV 缺少必需列", {"missing": sorted(missing), "actual": reader.fieldnames})

        records: list[RequestRecord] = []
        errors: list[str] = []
        for index, row in enumerate(reader, start=1):
            try:
                records.append(_coerce_record(dict(row), index))
            except ParseError as exc:
                errors.append(exc.message)
    if errors:
        raise ParseError(f"benchmark CSV 有 {len(errors)} 行不合法", {"errors": errors[:5]})
    if not records:
        raise ParseError("benchmark CSV 没有数据行", None)
    return records


def parse_prometheus_text(text: str) -> list[PromSample]:
    samples: list[PromSample] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = {
            key: value.replace('\\"', '"').replace("\\\\", "\\")
            for key, value in _LABEL.findall(match.group("labels") or "")
        }
        samples.append(PromSample(name=match.group("name"), labels=labels, value=value))
    if not samples:
        raise ParseError("Prometheus 文本中未解析出任何样本", {"lines": len(text.splitlines())})
    return samples


def _histogram_quantile(buckets: list[tuple[float, float]], quantile: float) -> float | None:
    """由累积直方图桶线性插值估算分位（近似值）。"""

    finite = [(le, count) for le, count in buckets if le != float("inf")]
    if not finite:
        return None
    total = buckets[-1][1] if buckets[-1][0] == float("inf") else finite[-1][1]
    if total <= 0:
        return None
    target = quantile * total
    previous_le = 0.0
    previous_count = 0.0
    for le, cumulative in finite:
        if cumulative >= target:
            if cumulative == previous_count:
                return le
            ratio = (target - previous_count) / (cumulative - previous_count)
            return previous_le + (le - previous_le) * max(0.0, min(1.0, ratio))
        previous_le, previous_count = le, cumulative
    return finite[-1][0]


def summarize_prometheus(samples: list[PromSample]) -> dict[str, Any]:
    """把 Prometheus 样本聚合成可报告的结构（计数器取末值、直方图给分位）。"""

    gauges: dict[str, Any] = {}
    buckets: dict[str, dict[float, float]] = {}
    histogram_labels: dict[str, dict[str, str]] = {}

    for sample in samples:
        if sample.name.endswith("_bucket") and "le" in sample.labels:
            base = sample.name[: -len("_bucket")]
            try:
                le = float(sample.labels["le"])
            except ValueError:
                continue
            buckets.setdefault(base, {})[le] = sample.value
            histogram_labels.setdefault(base, {k: v for k, v in sample.labels.items() if k != "le"})
            continue
        if sample.name.endswith(("_sum", "_count")):
            gauges[sample.name] = sample.value
            continue
        gauges[sample.name] = sample.value

    histograms = []
    for name, bucket_map in sorted(buckets.items()):
        ordered = sorted(bucket_map.items())
        if not any(le == float("inf") for le, _ in ordered):
            ordered.append((float("inf"), ordered[-1][1]))
        histograms.append(
            {
                "name": name,
                "labels": histogram_labels.get(name, {}),
                "count": ordered[-1][1],
                "quantiles": {
                    "p50": _histogram_quantile(ordered, 0.50),
                    "p90": _histogram_quantile(ordered, 0.90),
                    "p95": _histogram_quantile(ordered, 0.95),
                },
            }
        )

    return {
        "kind": PROMETHEUS,
        "gauges": gauges,
        "histograms": histograms,
        "sample_count": len(samples),
        "notes": [
            "Prometheus 输入是服务端聚合指标，无法做请求级配对对照；分位由累积直方图线性插值估算。",
        ],
    }


def load_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ParseError("PDF 解析需要 pypdf，请重新安装依赖", None) from exc
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(pages).strip()
        if not text:
            raise ParseError("PDF 未提取到文本（可能是扫描件）", {"pages": len(pages)})
        return text
    return path.read_text(encoding="utf-8", errors="replace")


def parse_input(path: Path, filename: str) -> ParseResult:
    kind = detect_type(filename, path)
    if kind == BENCHMARK_JSON:
        return ParseResult(kind=kind, records=parse_benchmark_json(path))
    if kind == BENCHMARK_CSV:
        return ParseResult(kind=kind, records=parse_benchmark_csv(path))
    if kind == PROMETHEUS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return ParseResult(kind=kind, samples=parse_prometheus_text(text), text=text)
    return ParseResult(kind=DOCUMENT, text=load_document_text(path))
