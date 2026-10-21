"""有界任务队列 + 并行 worker + 按任务类型的并发闸门。

并发模型
--------
* ``workers`` 个 worker 从同一个有界队列取任务；
* 每个任务带一个"资源类"（``gpu`` / ``cpu``），由对应的 ``asyncio.Semaphore`` 限制并发：
  - ``analysis`` 用 GPU，默认 1 个并发（benchmark 串行独占）；
  - ``research`` / ``validation`` 用 CPU/IO，默认 2 个并发。
* 队列满时拒绝新任务（429），不会无限堆积。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """队列已满。"""


class RunQueue:
    def __init__(
        self,
        size: int,
        handler: Callable[[str], None],
        *,
        workers: int = 1,
        gates: dict[str, int] | None = None,
    ) -> None:
        self._size = max(1, size)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=self._size)
        self._handler = handler
        self._workers = max(1, workers)
        self._gate_limits = {name: max(1, limit) for name, limit in (gates or {}).items()}
        self._gates: dict[str, asyncio.Semaphore] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._active = 0
        self._closing = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def gate_limits(self) -> dict[str, int]:
        return dict(self._gate_limits)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def running(self) -> int:
        """当前真正在执行的任务数（等待闸门的任务也算在 workers 占用内）。"""

        return self._active

    @property
    def full(self) -> bool:
        return self._queue.full()

    def start(self) -> None:
        self._gates = {
            name: asyncio.Semaphore(limit) for name, limit in self._gate_limits.items()
        }
        self._closing = False
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"inferencelab-worker-{index}")
            for index in range(self._workers)
        ]

    async def stop(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def submit(self, run_id: str, run_class: str = "cpu") -> int:
        try:
            self._queue.put_nowait((run_id, run_class))
        except asyncio.QueueFull as exc:  # pragma: no cover - 由 API 层先检查
            raise QueueFullError("任务队列已满") from exc
        return self._queue.qsize()

    async def _worker(self, index: int) -> None:
        while not self._closing:
            try:
                run_id, run_class = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover
                break
            self._active += 1
            gate = self._gates.get(run_class)
            try:
                if gate is not None:
                    async with gate:
                        await asyncio.to_thread(self._handler, run_id)
                else:
                    await asyncio.to_thread(self._handler, run_id)
            except Exception:  # pragma: no cover - handler 内部已兜底
                logger.exception("任务执行线程异常: worker=%s run_id=%s", index, run_id)
            finally:
                self._active -= 1
                self._queue.task_done()
