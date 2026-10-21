"""受控验证子图：登记项选择 → 参数校验 → 人工审批 → 固定脚本执行。"""

from __future__ import annotations

import json

from langgraph.graph import END, StateGraph

from ...deps import Deps
from ...schemas import ErrorCode, RunFailure
from ...validation.registry import approval_id_for, get_entry, validate_params
from ...validation.runner import run_entry
from ..state import RunState

NODE_SELECT = "validation_select"
NODE_EXECUTE = "validation_execute"


def build_validation_subgraph(deps: Deps):
    def select(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_SELECT):
            deps.check_cancel(run_id)
            options = state.get("options") or {}
            entry_id = str(options.get("entry_id") or "bench_smoke")
            entry = get_entry(entry_id)
            approval = state.get("approval")
            run = deps.store.get_run(run_id)

            if approval:
                # 审批恢复路径：参数重新校验，哈希必须与登记值完全一致。
                params = validate_params(entry, approval.get("params") or {})
                expected = approval_id_for(run_id, entry, params)
                if run is None or run.get("approval_id") != expected:
                    raise RunFailure(
                        ErrorCode.APPROVAL_MISMATCH,
                        "审批参数与已登记的哈希不一致，拒绝执行",
                        {"expected_prefix": (expected or "")[:12]},
                    )
                if run.get("approval_state") != "approved":
                    raise RunFailure(ErrorCode.APPROVAL_REQUIRED, "审批尚未确认，拒绝执行")
                deps.emit(
                    run_id,
                    "tool_call",
                    node=NODE_SELECT,
                    tool="validation_registry",
                    result={"entry_id": entry.entry_id, "approved": True},
                )
                return {
                    "validation": {
                        "entry_id": entry.entry_id,
                        "title": entry.title,
                        "script": entry.script,
                        "params": params,
                    }
                }

            params = validate_params(entry, options.get("params") or {})
            validation = {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "script": entry.script,
                "params": params,
            }
            if not entry.requires_approval:
                return {"validation": validation}

            approval_id = approval_id_for(run_id, entry, params)
            deps.store.update_run(
                run_id,
                approval_state="pending",
                approval_id=approval_id,
                approval_json=json.dumps(
                    {"entry_id": entry.entry_id, "params": params}, ensure_ascii=False
                ),
            )
            deps.emit(
                run_id,
                "approval_required",
                node=NODE_SELECT,
                approval_id=approval_id,
                entry=entry.public(),
                params=params,
            )
            return {"validation": validation, "awaiting_approval": True}

    def execute(state: RunState) -> RunState:
        run_id = state["run_id"]
        with deps.node(run_id, NODE_EXECUTE):
            deps.check_cancel(run_id)
            validation = state.get("validation") or {}
            entry = get_entry(str(validation.get("entry_id")))
            params = dict(validation.get("params") or {})
            run_dir = deps.run_dir(run_id)

            result = run_entry(
                entry,
                params,
                run_dir=run_dir,
                timeout_s=deps.settings.infl_validation_timeout_s,
            )
            deps.emit(
                run_id,
                "tool_call",
                node=NODE_EXECUTE,
                tool="validation_runner",
                result={
                    "entry_id": entry.entry_id,
                    "exit_code": result["exit_code"],
                    "timed_out": result["timed_out"],
                    "duration_ms": result["duration_ms"],
                },
            )
            if result["timed_out"]:
                raise RunFailure(
                    ErrorCode.VALIDATION_ERROR,
                    f"验证脚本超时（上限 {deps.settings.infl_validation_timeout_s}s）",
                    {"entry_id": entry.entry_id},
                )
            if result["exit_code"] != 0:
                raise RunFailure(
                    ErrorCode.VALIDATION_ERROR,
                    f"验证脚本返回非 0 退出码：{result['exit_code']}",
                    {"stderr": (result.get("stderr") or "")[-500:], "entry_id": entry.entry_id},
                )

            merged = {**validation, **result}
            return {"validation": merged}

    def after_select(state: RunState) -> str:
        return "await" if state.get("awaiting_approval") else "execute"

    builder = StateGraph(RunState)
    builder.add_node(NODE_SELECT, select)
    builder.add_node(NODE_EXECUTE, execute)
    builder.set_entry_point(NODE_SELECT)
    builder.add_conditional_edges(
        NODE_SELECT,
        after_select,
        {"await": END, "execute": NODE_EXECUTE},
    )
    builder.add_edge(NODE_EXECUTE, END)
    return builder.compile()
