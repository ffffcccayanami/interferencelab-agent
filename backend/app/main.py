"""FastAPI 应用工厂与启动入口。

启动：``python -m app.main``（在 backend/ 目录下）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .api.state import AppState
from .db.store import Store
from .deps import Deps
from .events import EventBus
from .graph.main_graph import build_main_graph
from .jobqueue import RunQueue
from .schemas import ApiError, ErrorCode, RunFailure
from .services.llm import LLMClient, redact
from .settings import BACKEND_DIR, Settings, load_settings
from .worker import RunWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)
logger = logging.getLogger("inferencelab")


def _error_response(status_code: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


class SPAStaticFiles(StaticFiles):
    """静态文件挂载：index.html 不缓存，避免重建后仍引用旧 hash 资源。"""

    async def get_response(self, path: str, scope: dict) -> Any:
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate_for_runtime()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    interrupted = store.mark_interrupted()
    if interrupted:
        logger.warning("启动恢复：%s 个未完成任务已标记为 interrupted", interrupted)

    bus = EventBus()
    llm = LLMClient(settings)
    if settings.llm_configured:
        # 预热：把 openai 导入与客户端构造放到启动阶段，避免首个任务解释节点多花 1~2s
        _ = llm.available
    deps = Deps(settings, store, bus, llm)
    graph = build_main_graph(deps)
    worker = RunWorker(deps, graph)
    queue = RunQueue(
        settings.infl_queue_size,
        worker,
        workers=settings.infl_workers,
        gates={"gpu": settings.infl_gpu_concurrency, "cpu": settings.infl_cpu_concurrency},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        queue.start()
        logger.info(
            "InferenceLab 启动：data_dir=%s llm_enabled=%s model=%s key=%s",
            settings.data_dir,
            settings.llm_enabled,
            settings.llm_model_id,
            settings.llm_key_fingerprint(),
        )
        try:
            yield
        finally:
            await queue.stop()

    app = FastAPI(
        title="InferenceLab Agent",
        version="0.1.0",
        description="推理实验分析与技术研究工作台（本地单用户）",
        lifespan=lifespan,
    )
    app.state.core = AppState(settings=settings, store=store, bus=bus, queue=queue, worker=worker)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    dist = BACKEND_DIR.parent / "frontend" / "dist"
    has_frontend = (dist / "index.html").is_file()

    @app.get("/")
    def index() -> Any:
        # 构建过前端时，根路径直接进工作台；否则返回 API 信息
        if has_frontend:
            return RedirectResponse(url="/app/", status_code=307)
        return {"name": "InferenceLab Agent", "api": "/api", "docs": "/docs"}

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code.value, exc.message, exc.details)

    @app.exception_handler(RunFailure)
    async def handle_run_failure(request: Request, exc: RunFailure) -> JSONResponse:
        return _error_response(400, exc.code.value, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, ErrorCode.VALIDATION_ERROR.value, "请求参数不合法", exc.errors())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("未处理异常: %s", type(exc).__name__)
        return _error_response(500, ErrorCode.INTERNAL.value, "服务内部错误", {"error": redact(str(exc))})

    if has_frontend:  # 构建过前端时顺带托管，便于单端口演示
        app.mount("/app", SPAStaticFiles(directory=str(dist), html=True), name="frontend")

    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.infl_api_host, port=settings.infl_api_port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = ["create_app", "main"]
