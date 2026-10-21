"""主图：输入校验 → 任务路由 → 子图 → 解释 → 报告持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

from ..deps import Deps
from ..schemas import ErrorCode, RunFailure
from ..services.artifacts import register_artifact, write_text_artifact
from ..services.evaluation import build_evaluation
from ..services.llm import redact
from ..services.pdf_export import build_pdf_bytes
from ..services.reporting import metrics_payload, render_report, result_summary
from ..services.retrieval import Citation, verify_citations
from .state import RunState
from .subgraphs.analysis import build_analysis_subgraph
from .subgraphs.research import build_research_subgraph
from .subgraphs.validation import build_validation_subgraph

NODE_VALIDATE = "validate_input"
NODE_EXPLAIN = "explain"
NODE_PERSIST = "persist"

ANALYSIS_SYSTEM = (
    "你是推理服务性能评测助手。你只能解释已经算好的指标，禁止重新计算或编造数字。"
    "必须说明收益来自同环境同负载的配置对照，不能声称单参数因果。"
    '只输出 JSON：{"summary": "不超过 220 字的中文解释"}'
)

RESEARCH_SYSTEM = (
    "你是技术研究助手。只能使用给定证据作答，禁止引入证据之外的事实。"
    "每一条结论句末尾必须标注来源，格式为 [[chunk_id]]，chunk_id 必须来自给定的证据列表。"
    '只输出 JSON：{"summary": "Markdown 段落，段落之间用空行分隔，每段至少一个 [[chunk_id]] 标注"}'
)


def _compact_analysis(analysis: dict) -> dict:
    return {
        "pairing": analysis.get("pairing"),
        "groups": analysis.get("groups"),
        "comparisons": analysis.get("comparisons"),
    }


def _compact_prometheus(payload: dict) -> dict:
    gauges = payload.get("gauges", {})
    return {
        "gauges": dict(sorted(gauges.items())[:25]),
        "histograms": payload.get("histograms", [])[:10],
        "sample_count": payload.get("sample_count"),
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def build_main_graph(deps: Deps):
    analysis_graph = build_analysis_subgraph(deps)
    research_graph = build_research_subgraph(deps)
    validation_graph = build_validation_subgraph(deps)

    def validate_input(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_VALIDATE):
            deps.check_cancel(run_id)
            if not (state.get("question") or "").strip():
                raise RunFailure(ErrorCode.VALIDATION_ERROR, "问题不能为空")
            task_type = state.get("task_type")
            if task_type not in ("analysis", "research", "validation"):
                raise RunFailure(ErrorCode.VALIDATION_ERROR, f"未知任务类型：{task_type}")
            if task_type in ("analysis", "research") and not state.get("uploads"):
                raise RunFailure(ErrorCode.VALIDATION_ERROR, "该任务类型至少需要一个上传文件")
            if task_type == "validation" and state.get("uploads"):
                deps.emit(
                    run_id,
                    "warning",
                    node=NODE_VALIDATE,
                    message="受控验证不使用上传文件，已忽略 upload_ids",
                )
            return {"warnings": list(state.get("warnings") or [])}

    def route(state: RunState) -> str:
        return str(state.get("task_type"))

    def explain(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_EXPLAIN):
            deps.check_cancel(run_id)
            task_type = state.get("task_type")
            warnings = list(state.get("warnings") or [])

            if task_type == "validation":
                validation = state.get("validation") or {}
                summary = (
                    f"脚本 `{validation.get('entry_id')}` 在隔离目录执行完毕，"
                    f"退出码 {validation.get('exit_code')}，耗时 {validation.get('duration_ms')} ms。"
                )
                return {"explanation": summary, "explanation_source": "template", "warnings": warnings}

            client = deps.llm_for_run(run_id)
            if not client.available:
                warnings.append("模型未启用（LLM_ENABLED=false 或 key 未配置），已省略模型解释")
                deps.emit(run_id, "warning", node=NODE_EXPLAIN, message=warnings[-1])
                return {
                    "explanation": "",
                    "explanation_source": "none",
                    "degraded": True,
                    "warnings": warnings,
                }

            try:
                if task_type == "research":
                    citations = [Citation(**item) for item in state.get("citations") or []]
                    known = ", ".join(citation.chunk_id for citation in citations)
                    user = (
                        f"问题：{state.get('question')}\n\n"
                        f"可用 chunk_id：{known}\n\n"
                        f"证据：\n{_clip(state.get('evidence_block') or '', 6000)}"
                    )
                    payload = client.complete_json(
                        system=RESEARCH_SYSTEM, user=user, max_tokens=1600, node=NODE_EXPLAIN
                    )
                    raw_summary = str(payload.get("summary") or "").strip()
                    if not raw_summary:
                        raise RunFailure(ErrorCode.LLM_FORMAT_ERROR, "模型未给出 summary 字段")
                    verified, citation_warnings = verify_citations(raw_summary, citations)
                    warnings.extend(citation_warnings)
                    for warning in citation_warnings:
                        deps.emit(run_id, "warning", node=NODE_EXPLAIN, message=warning)
                    return {
                        "explanation": verified,
                        "explanation_source": "llm" if verified else "none",
                        "degraded": not verified,
                        "warnings": warnings,
                    }

                analysis = state.get("analysis") or {}
                if analysis.get("kind") == "prometheus":
                    body = _compact_prometheus(analysis)
                else:
                    body = _compact_analysis(analysis)
                user = (
                    f"问题：{state.get('question')}\n\n"
                    f"确定性结果（JSON）：\n{_clip(json.dumps(body, ensure_ascii=False), 12000)}"
                )
                payload = client.complete_json(
                    system=ANALYSIS_SYSTEM, user=user, max_tokens=1200, node=NODE_EXPLAIN
                )
                summary = str(payload.get("summary") or "").strip()
                if not summary:
                    raise RunFailure(ErrorCode.LLM_FORMAT_ERROR, "模型未给出 summary 字段")
                return {"explanation": summary, "explanation_source": "llm", "warnings": warnings}
            except RunFailure as exc:
                if exc.code is ErrorCode.BUDGET_EXCEEDED:
                    raise
                message = f"模型解释已省略：{exc.code.value} - {redact(exc.message)}"
                warnings.append(message)
                deps.emit(run_id, "warning", node=NODE_EXPLAIN, message=message)
                return {
                    "explanation": "",
                    "explanation_source": "none",
                    "degraded": True,
                    "warnings": warnings,
                }

    def persist(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_PERSIST):
            deps.check_cancel(run_id)
            run_dir = deps.run_dir(run_id)
            warnings = list(state.get("warnings") or [])
            artifacts: list[dict] = []

            evaluation = build_evaluation(state=state, settings=deps.settings, store=deps.store, run_id=run_id)
            report_state: dict[str, Any] = {**state, "evaluation": evaluation}

            report = render_report(report_state)
            artifacts.append(
                write_text_artifact(
                    deps.store, run_id=run_id, run_dir=run_dir, name="report.md", text=report, kind="report"
                )
            )

            result_json = json.dumps(metrics_payload(report_state), ensure_ascii=False, indent=2)
            artifacts.append(
                write_text_artifact(
                    deps.store,
                    run_id=run_id,
                    run_dir=run_dir,
                    name="result.json",
                    text=result_json,
                    kind="data",
                )
            )

            title = f"InferenceLab {state.get('task_type')} 报告"
            try:
                pdf_bytes = build_pdf_bytes(title, report)
                pdf_path = run_dir / "artifacts" / "report.pdf"
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(pdf_bytes)
                artifacts.append(
                    register_artifact(deps.store, run_id=run_id, run_dir=run_dir, path=pdf_path, kind="report_pdf")
                )
            except Exception as exc:  # pragma: no cover - 字体/布局异常时降级
                message = f"PDF 导出失败，已跳过：{type(exc).__name__}"
                warnings.append(message)
                deps.emit(run_id, "warning", node=NODE_PERSIST, message=message)

            validation = state.get("validation") or {}
            for key, kind in (("stdout_path", "validation_stdout"), ("stderr_path", "validation_stderr")):
                raw_path = validation.get(key)
                if not raw_path:
                    continue
                path = Path(raw_path)
                if path.exists() and path.stat().st_size > 0:
                    artifacts.append(
                        register_artifact(deps.store, run_id=run_id, run_dir=run_dir, path=path, kind=kind)
                    )

            for artifact in artifacts:
                deps.emit(
                    run_id,
                    "artifact_ready",
                    node=NODE_PERSIST,
                    artifact_id=artifact["id"],
                    kind=artifact["kind"],
                    filename=artifact["filename"],
                )

            summary = result_summary(report_state)
            deps.store.update_run(
                run_id,
                result_summary_json=json.dumps(summary, ensure_ascii=False),
                eval_status=str(evaluation.get("status") or "not_applicable"),
                benchmark_key=evaluation.get("benchmark_key"),
                baseline_run_id=evaluation.get("baseline_run_id"),
            )
            return {"report_markdown": report, "artifacts": artifacts, "warnings": warnings}

    def after_validation(state: RunState) -> str:
        return "await" if state.get("awaiting_approval") else "continue"

    builder = StateGraph(RunState)
    builder.add_node(NODE_VALIDATE, validate_input)
    builder.add_node("analysis", analysis_graph)
    builder.add_node("research", research_graph)
    builder.add_node("validation", validation_graph)
    builder.add_node(NODE_EXPLAIN, explain)
    builder.add_node(NODE_PERSIST, persist)

    builder.set_entry_point(NODE_VALIDATE)
    builder.add_conditional_edges(
        NODE_VALIDATE,
        route,
        {"analysis": "analysis", "research": "research", "validation": "validation"},
    )
    builder.add_edge("analysis", NODE_EXPLAIN)
    builder.add_edge("research", NODE_EXPLAIN)
    builder.add_conditional_edges(
        "validation",
        after_validation,
        {"await": END, "continue": NODE_EXPLAIN},
    )
    builder.add_edge(NODE_EXPLAIN, NODE_PERSIST)
    builder.add_edge(NODE_PERSIST, END)
    return builder.compile()
