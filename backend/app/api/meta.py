"""元信息、模型连通性探测与 Prometheus 指标（均不返回密钥）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from ..schemas import LLMHealthOut, MetaOut
from ..services import monitor
from ..validation.registry import list_entries
from .state import AppState, get_state

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaOut)
def get_meta(state: AppState = Depends(get_state)) -> MetaOut:
    settings = state.settings
    return MetaOut(
        task_types=["analysis", "research", "validation"],
        llm_configured=settings.llm_configured,
        llm_model_id=settings.llm_model_id if settings.llm_enabled else "disabled",
        llm_key_fingerprint=settings.llm_key_fingerprint(),
        llm_endpoint=settings.llm_endpoint,
        llm_auth_mode=settings.llm_auth_mode,
        llm_provider=settings.llm_provider,
        llm_profile=settings.llm_profile,
        llm_deployment=settings.deployment,
        config_source=settings.config_source or "env vars only",
        max_upload_mb=settings.infl_max_upload_mb,
        queue_size=settings.infl_queue_size,
        workers=settings.infl_workers,
        gpu_concurrency=settings.infl_gpu_concurrency,
        cpu_concurrency=settings.infl_cpu_concurrency,
        validation_entries=list_entries(),
    )


@router.get("/llm/health", response_model=LLMHealthOut)
def llm_health(state: AppState = Depends(get_state)) -> LLMHealthOut:
    """探测模型端点是否可达（默认 GET /models，免费且不产生 token 消耗）。"""

    payload = state.worker.deps.llm.probe()
    return LLMHealthOut(**payload)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(state: AppState = Depends(get_state)) -> PlainTextResponse:
    """Prometheus 文本指标：任务、队列、模型调用（全部来自 SQLite，不触发模型调用）。"""

    body = monitor.render_prometheus(
        state.store,
        state.settings,
        queue_pending=state.queue.pending,
        queue_capacity=state.queue.size,
        running=state.queue.running,
        workers=state.queue.workers,
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok"}
