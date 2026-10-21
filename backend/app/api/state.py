"""API 层共享对象。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..db.store import Store
from ..events import EventBus
from ..jobqueue import RunQueue
from ..settings import Settings
from ..worker import RunWorker


@dataclass
class AppState:
    settings: Settings
    store: Store
    bus: EventBus
    queue: RunQueue
    worker: RunWorker


def get_state(request: Request) -> AppState:
    return request.app.state.core  # type: ignore[no-any-return]
