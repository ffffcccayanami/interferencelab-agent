"""LangGraph 共享状态。"""

from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    """主图与子图共享的状态（只放引用与结论，不放整份日志）。"""

    run_id: str
    task_type: str
    question: str
    uploads: list[dict[str, Any]]
    options: dict[str, Any]

    # 分析分支
    records: list[Any]
    analysis: dict[str, Any]
    prometheus: dict[str, Any]

    # 研究分支
    documents: list[dict[str, Any]]
    document_texts: list[Any]
    citations: list[dict[str, Any]]
    evidence_block: str

    # 验证分支
    validation: dict[str, Any]
    awaiting_approval: bool
    approval: dict[str, Any] | None

    # 公共
    explanation: str
    explanation_source: str
    warnings: list[str]
    degraded: bool
    artifacts: list[dict[str, Any]]
    report_markdown: str


def initial_state(run: dict[str, Any], uploads: list[dict[str, Any]]) -> RunState:
    return RunState(
        run_id=run["id"],
        task_type=run["task_type"],
        question=run["question"],
        uploads=uploads,
        options=run.get("options") or {},
        approval=run.get("approval"),
        warnings=[],
        degraded=False,
        explanation="",
        explanation_source="none",
        awaiting_approval=False,
        artifacts=[],
    )
