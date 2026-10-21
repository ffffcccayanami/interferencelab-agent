"""事件总线：事件先落库，再唤醒 SSE 订阅者。

跨线程安全：worker 在 ``asyncio.to_thread`` 中执行，``notify`` 通过
``call_soon_threadsafe`` 回到事件循环，并用版本号避免丢唤醒。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

from .db.store import Store


class EventBus:
    """按 run 维护"版本号 + 唤醒器"的轻量发布订阅。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._versions: dict[str, int] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def version(self, run_id: str) -> int:
        with self._lock:
            return self._versions.get(run_id, 0)

    def notify(self, run_id: str) -> None:
        with self._lock:
            self._versions[run_id] = self._versions.get(run_id, 0) + 1
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._wake, run_id)
        except RuntimeError:  # 事件循环已关闭
            return

    def _wake(self, run_id: str) -> None:
        event = self._waiters.get(run_id)
        if event is not None:
            event.set()

    async def wait(self, run_id: str, seen: int, timeout: float) -> tuple[int, bool]:
        """等待新事件；返回 (最新版本, 是否被唤醒)。"""

        if self.version(run_id) != seen:
            return self.version(run_id), True

        event = asyncio.Event()
        self._waiters[run_id] = event
        woke = False
        try:
            if self.version(run_id) != seen:
                return self.version(run_id), True
            await asyncio.wait_for(event.wait(), timeout=timeout)
            woke = True
        except TimeoutError:
            woke = False
        finally:
            if self._waiters.get(run_id) is event:
                self._waiters.pop(run_id, None)
        return self.version(run_id), woke


def emit(
    store: Store,
    bus: EventBus,
    run_id: str,
    event_type: str,
    *,
    node: str = "",
    **payload: Any,
) -> int:
    """写入一条事件并唤醒订阅者，返回事件序号。"""

    seq = store.append_event(run_id, event_type, node=node, payload=payload)
    bus.notify(run_id)
    return seq


def format_sse(event: dict[str, Any]) -> str:
    payload = dict(event.get("payload") or {})
    # 节点名单独存在 events.node 列，这里补回 data，前端与监控才能按节点聚合
    if event.get("node"):
        payload.setdefault("node", event["node"])
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


async def stream_events(
    store: Store,
    bus: EventBus,
    run_id: str,
    *,
    after: int = 0,
    heartbeat_s: float = 15.0,
) -> AsyncIterator[str]:
    """SSE 生成器：先补齐历史事件，再增量推送；任务终止后关闭。"""

    last = after
    version = bus.version(run_id)
    while True:
        events = store.list_events(run_id, after=last)
        for event in events:
            last = int(event["seq"])
            yield format_sse(event)

        run = store.get_run(run_id)
        if run is None:
            yield format_sse(
                {"seq": last, "type": "error", "payload": {"code": "NOT_FOUND", "message": "运行不存在"}}
            )
            return

        terminal = run["status"] in ("succeeded", "failed", "cancelled", "interrupted")
        if terminal and not events:
            return

        version, woke = await bus.wait(run_id, version, timeout=heartbeat_s)
        if not woke:
            yield ": ping\n\n"
