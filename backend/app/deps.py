"""图执行依赖容器（store / 事件总线 / LLM / 设置）。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .db.store import Store, utcnow
from .events import EventBus, emit
from .schemas import ErrorCode, RunCancelled, RunFailure
from .services.llm import LLMClient
from .services.llm_metrics import LLMCallRecorder
from .settings import Settings


class Deps:
    def __init__(self, settings: Settings, store: Store, bus: EventBus, llm: LLMClient) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        # 仅用于 /api/llm/health 的探测客户端
        self.llm = llm
        # 每个任务一个客户端：Budget 按任务独立，指标记录器绑定 run_id
        self._run_clients: dict[str, LLMClient] = {}

    def llm_for_run(self, run_id: str) -> LLMClient:
        client = self._run_clients.get(run_id)
        if client is None:
            client = LLMClient(
                self.settings,
                on_call=LLMCallRecorder(self.store, self.bus, run_id),
                run_id=run_id,
            )
            self._run_clients[run_id] = client
        return client

    def release_run(self, run_id: str) -> None:
        self._run_clients.pop(run_id, None)

    def run_dir(self, run_id: str) -> Path:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunFailure(ErrorCode.NOT_FOUND, "运行不存在")
        path = Path(run["run_dir"])
        path.mkdir(parents=True, exist_ok=True)
        return path

    def emit(self, run_id: str, event_type: str, *, node: str = "", **payload: Any) -> int:
        return emit(self.store, self.bus, run_id, event_type, node=node, **payload)

    def check_cancel(self, run_id: str) -> None:
        if self.store.is_cancel_requested(run_id):
            raise RunCancelled(f"运行 {run_id} 已请求取消")

    @contextmanager
    def node(self, run_id: str, name: str) -> Iterator[None]:
        """统一的节点事件：开始 / 成功 / 失败 / 取消，并落一行耗时到 node_timings。"""

        self.emit(run_id, "node_started", node=name, at=utcnow())
        started = time.perf_counter()
        status = "ok"
        try:
            yield
        except RunCancelled:
            status = "cancelled"
            raise
        except RunFailure:
            status = "failed"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self.emit(
                run_id,
                "node_finished",
                node=name,
                status=status,
                duration_ms=duration_ms,
            )
            try:
                self.store.add_node_timing(run_id, name, status, duration_ms)
            except Exception:  # pragma: no cover - 观测写入失败不能影响任务
                pass
