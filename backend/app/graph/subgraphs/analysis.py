"""实验分析子图：解析 → 配对检查 / 确定性指标。"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.graph import END, StateGraph

from ...deps import Deps
from ...schemas import ErrorCode, RunFailure
from ...services.metrics import compute_metrics
from ...services.parsing import DOCUMENT, PROMETHEUS, parse_input, summarize_prometheus
from ..state import RunState

NODE_PARSE = "analysis_parse"
NODE_COMPUTE = "analysis_compute"


def build_analysis_subgraph(deps: Deps):
    def parse_inputs(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_PARSE):
            deps.check_cancel(run_id)
            records = []
            prometheus = None
            warnings = list(state.get("warnings") or [])

            if not state.get("uploads"):
                raise RunFailure(ErrorCode.VALIDATION_ERROR, "实验分析至少需要一个上传文件")

            for item in state["uploads"]:
                result = parse_input(Path(item["stored_path"]), item["filename"])
                if result.kind == DOCUMENT:
                    raise RunFailure(
                        ErrorCode.UNSUPPORTED_FILE,
                        "实验分析只接受 benchmark JSON / CSV 或 Prometheus 文本",
                        {"file": item["filename"], "detected_type": result.kind},
                    )
                if result.kind == PROMETHEUS:
                    if prometheus is not None:
                        raise RunFailure(
                            ErrorCode.VALIDATION_ERROR,
                            "一次只能分析一个 Prometheus 文本",
                            {"file": item["filename"]},
                        )
                    prometheus = summarize_prometheus(result.samples)
                    warnings.extend(prometheus.get("notes", []))
                else:
                    records.extend(result.records)

            if prometheus is not None and records:
                raise RunFailure(
                    ErrorCode.NON_PAIRWISE_INPUT,
                    "请求级 benchmark 与服务端聚合指标不能混在一起对比",
                    {"hint": "分开两次运行：一次上传 benchmark JSON/CSV，一次上传 Prometheus 文本"},
                )

            deps.emit(
                run_id,
                "tool_call",
                node=NODE_PARSE,
                tool="benchmark_parser",
                result={
                    "records": len(records),
                    "prometheus_samples": prometheus.get("sample_count") if prometheus else 0,
                },
            )
            return {"records": records, "prometheus": prometheus, "warnings": warnings}

    def compute(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_COMPUTE):
            deps.check_cancel(run_id)
            warnings = list(state.get("warnings") or [])

            if state.get("prometheus"):
                payload = dict(state["prometheus"])
                payload["kind"] = "prometheus"
                deps.emit(run_id, "metric_table", node=NODE_COMPUTE, rows=len(payload.get("histograms", [])))
                return {"analysis": payload, "warnings": warnings}

            metrics = compute_metrics(
                state.get("records") or [],
                min_samples=deps.settings.infl_min_group_samples,
            )
            pairing = metrics["pairing"]
            if not pairing["ok"]:
                raise RunFailure(
                    ErrorCode.NON_PAIRWISE_INPUT,
                    "输入不满足配对条件，拒绝生成对比结论",
                    {"reasons": pairing["reasons"], "groups": metrics["groups"]},
                )
            warnings.extend(pairing.get("notes") or [])
            deps.emit(
                run_id,
                "metric_table",
                node=NODE_COMPUTE,
                groups=len(metrics["groups"]),
                comparisons=len(metrics["comparisons"]),
                payload=json.loads(json.dumps(metrics["pairing"], ensure_ascii=False)),
            )
            return {"analysis": metrics, "warnings": warnings}

    builder = StateGraph(RunState)
    builder.add_node(NODE_PARSE, parse_inputs)
    builder.add_node(NODE_COMPUTE, compute)
    builder.set_entry_point(NODE_PARSE)
    builder.add_edge(NODE_PARSE, NODE_COMPUTE)
    builder.add_edge(NODE_COMPUTE, END)
    return builder.compile()
