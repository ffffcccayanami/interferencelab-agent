"""任务接口：创建 / 查询 / SSE / 审批 / 取消 / 产物下载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from ..db.store import TERMINAL_STATUSES, new_id
from ..events import stream_events
from ..schemas import ApiError, ApproveRequest, ErrorCode, RunCreate, RunFailure
from ..services import server_metrics
from ..services.files import ensure_within
from ..validation.registry import approval_id_for, get_entry, validate_params
from .state import AppState, get_state

router = APIRouter(tags=["runs"])


def _serialize_artifact(run_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["id"],
        "kind": artifact["kind"],
        "filename": artifact["filename"],
        "size": artifact["size"],
        "sha256": artifact["sha256"],
        "download_url": f"/api/runs/{run_id}/artifacts/{artifact['id']}",
    }


def _serialize_llm_call(call: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "node",
        "attempt",
        "model",
        "endpoint_host",
        "streamed",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "ttft_first_token_ms",
        "ttft_first_content_ms",
        "total_ms",
        "stream_ms",
        "prefill_tokens_per_s_est",
        "decode_tokens_per_s",
        "content_tokens_per_s",
        "finish_reason",
        "outcome",
        "error_code",
        "created_at",
    )
    return {key: call.get(key) for key in keys}


def _server_metrics(state: AppState, run_id: str) -> dict[str, Any] | None:
    snapshots = state.store.list_metrics_snapshots(run_id)
    if not snapshots:
        return None
    start = next((item for item in snapshots if item["phase"] == "start"), None)
    end = next((item for item in reversed(snapshots) if item["phase"] == "end"), None)
    if start is None or end is None:
        return {"status": "partial", "snapshots": [item["phase"] for item in snapshots]}
    delta = server_metrics.diff_snapshots(start["payload"], end["payload"])
    delta["captured_at"] = {"start": start["created_at"], "end": end["created_at"]}
    return delta


def _serialize_run(
    state: AppState,
    run: dict[str, Any],
    *,
    detail: bool = False,
    artifact_count: int | None = None,
) -> dict[str, Any]:
    artifacts = [_serialize_artifact(run["id"], item) for item in state.store.list_artifacts(run["id"])]
    error = None
    if run.get("error_code"):
        error = {
            "code": run["error_code"],
            "message": run.get("error_message"),
            "details": run.get("error_details"),
        }

    pending_approval = None
    if run["status"] == "awaiting_approval" and run.get("approval"):
        try:
            entry = get_entry(run["approval"]["entry_id"])
            pending_approval = {
                "approval_id": run.get("approval_id"),
                "entry": entry.public(),
                "params": run["approval"].get("params", {}),
            }
        except RunFailure:  # pragma: no cover - 条目被移除时仍展示原参数
            pending_approval = {"approval_id": run.get("approval_id"), "params": run["approval"].get("params", {})}

    payload: dict[str, Any] = {
        "run_id": run["id"],
        "task_type": run["task_type"],
        "status": run["status"],
        "stage": run["stage"],
        "question": run["question"],
        "degraded": run["degraded"],
        "warnings": run.get("warnings", []),
        "error": error,
        "pending_approval": pending_approval,
        "artifacts": artifacts,
        "artifact_count": artifact_count if artifact_count is not None else len(artifacts),
        "result_summary": run.get("result_summary"),
        "duration_ms": run.get("duration_ms"),
        "queue_wait_ms": run.get("queue_wait_ms"),
        "llm_calls_count": run.get("llm_calls_count"),
        "eval_status": run.get("eval_status"),
        "benchmark_key": run.get("benchmark_key"),
        "baseline_run_id": run.get("baseline_run_id"),
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }
    if detail:
        payload["node_timings"] = state.store.list_node_timings(run["id"])
        payload["llm_calls"] = [_serialize_llm_call(item) for item in state.store.list_llm_calls(run["id"])]
        payload["server_metrics"] = _server_metrics(state, run["id"])
    return payload


@router.post("/runs", status_code=202)
def create_run(payload: RunCreate, state: AppState = Depends(get_state)) -> dict[str, Any]:
    upload_rows = []
    for upload_id in payload.upload_ids:
        row = state.store.get_upload(upload_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"上传文件不存在：{upload_id}", status_code=404)
        upload_rows.append(row)

    if payload.task_type in ("analysis", "research") and not upload_rows:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "该任务类型至少需要一个上传文件", status_code=400)

    if state.queue.full:
        raise ApiError(
            ErrorCode.QUEUE_FULL,
            f"任务队列已满（并发上限 {state.queue.size}），请稍后重试",
            status_code=429,
        )

    run_id = new_id("run")
    run_class = "gpu" if payload.task_type == "analysis" else "cpu"
    run_dir = state.settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state.store.create_run(
        run_id=run_id,
        task_type=payload.task_type,
        question=payload.question.strip(),
        options=payload.options,
        upload_ids=[row["id"] for row in upload_rows],
        run_dir=str(run_dir),
    )
    position = state.queue.submit(run_id, run_class)
    state.bus.notify(run_id)

    return {"run_id": run_id, "status": "queued", "queue_position": position, "run_class": run_class}


@router.get("/runs")
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    runs = state.store.list_runs(limit=limit, offset=offset)
    counts = state.store.artifact_counts([run["id"] for run in runs])
    return {
        "runs": [
            _serialize_run(state, run, artifact_count=counts.get(run["id"], 0))
            for run in runs
        ]
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)
    return _serialize_run(state, run, detail=True)


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)

    start = after
    if start == 0 and last_event_id and last_event_id.isdigit():
        start = int(last_event_id)

    return StreamingResponse(
        stream_events(state.store, state.bus, run_id, after=start),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/approve")
def approve_run(
    run_id: str,
    payload: ApproveRequest,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)
    if run["status"] != "awaiting_approval" or not run.get("approval"):
        raise ApiError(
            ErrorCode.CONFLICT,
            f"当前状态（{run['status']}）不需要审批",
            status_code=409,
        )

    entry = get_entry(run["approval"]["entry_id"])
    try:
        params = validate_params(entry, payload.params)
    except RunFailure as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, exc.message, status_code=400, details=exc.details) from exc

    expected = approval_id_for(run_id, entry, params)
    if payload.approval_id != expected or run.get("approval_id") != expected:
        state.bus.notify(run_id)
        raise ApiError(
            ErrorCode.APPROVAL_MISMATCH,
            "审批参数与登记值不一致（参数被修改或审批已过期）",
            status_code=409,
            details={"expected_prefix": expected[:12]},
        )
    if state.queue.full:
        raise ApiError(ErrorCode.QUEUE_FULL, "任务队列已满，请稍后重试审批", status_code=429)

    state.store.update_run(
        run_id,
        status="queued",
        stage="queued",
        approval_state="approved",
        approval_json=json.dumps({"entry_id": entry.entry_id, "params": params}, ensure_ascii=False),
        error_code=None,
        error_message=None,
        error_details=None,
    )
    position = state.queue.submit(run_id, "cpu")
    state.bus.notify(run_id)
    return {"run_id": run_id, "status": "queued", "queue_position": position}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)
    if run["status"] in TERMINAL_STATUSES:
        raise ApiError(ErrorCode.CONFLICT, f"任务已结束（{run['status']}），无法取消", status_code=409)

    if run["status"] in ("queued", "awaiting_approval"):
        state.store.update_run(run_id, status="cancelled", stage="cancelled", finished_at=_now())
        state.store.append_event(run_id, "run_status", payload={"status": "cancelled", "stage": "cancelled"})
        state.store.append_event(run_id, "done", payload={"status": "cancelled"})
        state.bus.notify(run_id)
        return {"run_id": run_id, "status": "cancelled"}

    state.store.request_cancel(run_id)
    state.store.append_event(
        run_id,
        "warning",
        payload={"message": "取消请求已记录，将在当前节点边界终止"},
    )
    state.bus.notify(run_id)
    return {"run_id": run_id, "status": "cancelling"}


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def download_artifact(
    run_id: str,
    artifact_id: str,
    state: AppState = Depends(get_state),
) -> FileResponse:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)
    artifact = state.store.get_artifact(run_id, artifact_id)
    if artifact is None:
        raise ApiError(ErrorCode.NOT_FOUND, "产物不存在", status_code=404)

    run_dir = Path(run["run_dir"]).resolve()
    target = ensure_within(run_dir, run_dir / artifact["rel_path"])
    if not target.exists():
        raise ApiError(ErrorCode.NOT_FOUND, "产物文件已被删除", status_code=404)
    return FileResponse(path=target, filename=artifact["filename"])


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str, state: AppState = Depends(get_state)) -> PlainTextResponse:
    run = state.store.get_run(run_id)
    if run is None:
        raise ApiError(ErrorCode.NOT_FOUND, "运行不存在", status_code=404)
    for artifact in state.store.list_artifacts(run_id):
        if artifact["kind"] == "report" and artifact["filename"] == "report.md":
            run_dir = Path(run["run_dir"]).resolve()
            target = ensure_within(run_dir, run_dir / artifact["rel_path"])
            if target.exists():
                return PlainTextResponse(target.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
    raise ApiError(ErrorCode.NOT_FOUND, "报告尚未生成", status_code=404)


def _now() -> str:
    from ..db.store import utcnow

    return utcnow()
