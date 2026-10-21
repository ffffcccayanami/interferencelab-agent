"""任务执行器：把 LangGraph 的一次调用落到 runs / events / 指标表。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .db.store import utcnow
from .deps import Deps
from .graph.state import initial_state
from .schemas import ErrorCode, RunCancelled, RunFailure
from .services import server_metrics
from .services.llm import redact

logger = logging.getLogger(__name__)


def _ms_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return max(0, int(delta.total_seconds() * 1000))


class RunWorker:
    def __init__(self, deps: Deps, graph: Any) -> None:
        self.deps = deps
        self.graph = graph

    # 队列 handler（在 asyncio.to_thread 中执行）
    def __call__(self, run_id: str) -> None:
        self.execute(run_id)

    def execute(self, run_id: str) -> None:
        store = self.deps.store
        settings = self.deps.settings
        run = store.get_run(run_id)
        if run is None:
            return
        if run["status"] in ("cancelled", "interrupted", "succeeded", "failed"):
            return

        started_at = run.get("started_at") or utcnow()
        queue_wait_ms = _ms_between(run.get("created_at"), started_at)

        store.update_run(
            run_id,
            status="running",
            stage="running",
            started_at=started_at,
            queue_wait_ms=queue_wait_ms,
            error_code=None,
            error_message=None,
            error_details=None,
        )
        self.deps.emit(run_id, "run_status", status="running", stage="running", queue_wait_ms=queue_wait_ms)

        start_snapshot: dict[str, Any] | None = None
        if settings.llm_metrics_url:
            start_snapshot = server_metrics.capture(settings)
            store.add_metrics_snapshot(run_id, "start", start_snapshot)

        uploads = self._load_uploads(run_id)
        try:
            final_state = self.graph.invoke(initial_state(run, uploads))
            if final_state.get("awaiting_approval"):
                store.update_run(run_id, status="awaiting_approval", stage="awaiting_approval")
                self.deps.emit(run_id, "run_status", status="awaiting_approval", stage="awaiting_approval")
                return

            warnings = list(final_state.get("warnings") or [])
            finished_at = utcnow()
            store.update_run(
                run_id,
                status="succeeded",
                stage="done",
                degraded=bool(final_state.get("degraded")),
                warnings_json=json.dumps(warnings, ensure_ascii=False),
                finished_at=finished_at,
                duration_ms=_ms_between(started_at, finished_at),
                llm_calls_count=len(store.list_llm_calls(run_id)),
            )
            self.deps.emit(
                run_id,
                "run_status",
                status="succeeded",
                stage="done",
                degraded=bool(final_state.get("degraded")),
                warnings=warnings,
            )
        except RunCancelled:
            finished_at = utcnow()
            store.update_run(
                run_id,
                status="cancelled",
                stage="cancelled",
                finished_at=finished_at,
                duration_ms=_ms_between(started_at, finished_at),
                llm_calls_count=len(store.list_llm_calls(run_id)),
            )
            self.deps.emit(run_id, "run_status", status="cancelled", stage="cancelled")
        except RunFailure as exc:
            self._fail(run_id, exc.code, exc.message, exc.details, started_at=started_at)
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("任务执行出现未预期异常: run_id=%s", run_id)
            self._fail(
                run_id,
                ErrorCode.INTERNAL,
                "任务执行出现未预期异常",
                {"error": redact(f"{type(exc).__name__}: {exc}")},
                started_at=started_at,
            )
        finally:
            if start_snapshot is not None:
                self._finish_server_metrics(run_id, start_snapshot)
            self.deps.release_run(run_id)
            self.deps.emit(run_id, "done", status=(store.get_run(run_id) or {}).get("status"))

    def _finish_server_metrics(self, run_id: str, start_snapshot: dict[str, Any]) -> None:
        try:
            end_snapshot = server_metrics.capture(self.deps.settings)
            self.deps.store.add_metrics_snapshot(run_id, "end", end_snapshot)
            delta = server_metrics.diff_snapshots(start_snapshot, end_snapshot)
            if delta.get("status") == "ok":
                self.deps.emit(run_id, "server_metrics", **delta)
        except Exception:  # pragma: no cover - 观测失败不影响任务
            logger.warning("采集服务端指标失败: run_id=%s", run_id, exc_info=True)

    def _fail(
        self,
        run_id: str,
        code: ErrorCode,
        message: str,
        details: Any,
        *,
        started_at: str | None,
    ) -> None:
        safe_message = redact(message)
        safe_details: Any = None
        if details is not None:
            try:
                safe_details = json.loads(redact(json.dumps(details, ensure_ascii=False, default=str)))
            except (TypeError, ValueError):  # pragma: no cover - 极端不可序列化对象
                safe_details = {"error": redact(str(details))}
        finished_at = utcnow()
        self.deps.store.update_run(
            run_id,
            status="failed",
            stage="failed",
            error_code=code.value,
            error_message=safe_message,
            error_details=json.dumps(safe_details, ensure_ascii=False) if safe_details is not None else None,
            finished_at=finished_at,
            duration_ms=_ms_between(started_at, finished_at),
            llm_calls_count=len(self.deps.store.list_llm_calls(run_id)),
        )
        self.deps.emit(
            run_id,
            "error",
            code=code.value,
            message=safe_message,
            details=safe_details,
        )
        self.deps.emit(run_id, "run_status", status="failed", stage="failed", error_code=code.value)

    def _load_uploads(self, run_id: str) -> list[dict[str, Any]]:
        run = self.deps.store.get_run(run_id) or {}
        uploads: list[dict[str, Any]] = []
        for upload_id in self.deps.store.list_run_upload_ids(run_id):
            row = self.deps.store.get_upload(upload_id)
            if row is None:
                raise RunFailure(ErrorCode.NOT_FOUND, f"上传文件不存在：{upload_id}")
            uploads.append(
                {
                    "upload_id": row["id"],
                    "filename": row["original_name"],
                    "stored_path": row["stored_path"],
                    "detected_type": row["detected_type"],
                    "size": row["size"],
                    "sha256": row.get("sha256", ""),
                }
            )
        if not uploads and run.get("task_type") in ("analysis", "research"):
            raise RunFailure(ErrorCode.VALIDATION_ERROR, "缺少上传文件")
        return uploads


def _now() -> str:
    return utcnow()
