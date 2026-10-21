"""存储、队列与预算的单元测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.db.store import Store
from app.jobqueue import QueueFullError, RunQueue
from app.schemas import ErrorCode, RunFailure
from app.services.llm import Budget


def make_store(workdir: Path) -> Store:
    return Store(workdir / "app.db")


def test_mark_interrupted_after_restart(workdir: Path) -> None:
    store = make_store(workdir)
    run = store.create_run(
        run_id="run_1",
        task_type="analysis",
        question="q",
        options={},
        upload_ids=[],
        run_dir=str(workdir / "run_1"),
    )
    assert run["status"] == "queued"

    store.update_run("run_1", status="running")
    assert store.mark_interrupted() == 1
    assert store.get_run("run_1")["status"] == "interrupted"

    # 已完成的任务不会被改写
    store.update_run("run_1", status="succeeded")
    assert store.mark_interrupted() == 0


def test_events_are_ordered_and_can_be_tailed(workdir: Path) -> None:
    store = make_store(workdir)
    store.create_run(
        run_id="run_2",
        task_type="analysis",
        question="q",
        options={},
        upload_ids=[],
        run_dir=str(workdir / "run_2"),
    )
    first = store.append_event("run_2", "node_started", node="a")
    second = store.append_event("run_2", "node_finished", node="a")
    assert second > first

    assert len(store.list_events("run_2", after=0)) == 2
    assert [event["seq"] for event in store.list_events("run_2", after=first)] == [second]


def test_update_run_rejects_unknown_column(workdir: Path) -> None:
    store = make_store(workdir)
    store.create_run(
        run_id="run_3",
        task_type="analysis",
        question="q",
        options={},
        upload_ids=[],
        run_dir=str(workdir / "run_3"),
    )
    with pytest.raises(ValueError):
        store.update_run("run_3", password="x")


def test_queue_rejects_when_full() -> None:
    queue = RunQueue(1, lambda run_id: None)
    queue.submit("run_a")
    with pytest.raises(QueueFullError):
        queue.submit("run_b")
    assert queue.full is True


def test_queue_runs_jobs_in_order() -> None:
    seen: list[str] = []
    queue = RunQueue(4, seen.append)

    async def scenario() -> None:
        queue.start()
        queue.submit("run_a")
        queue.submit("run_b")
        for _ in range(50):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.02)
        await queue.stop()

    asyncio.run(scenario())
    assert seen == ["run_a", "run_b"]


def test_budget_blocks_extra_calls() -> None:
    budget = Budget(max_calls=1, max_chars=10)
    budget.spend(5)
    with pytest.raises(RunFailure) as excinfo:
        budget.spend(5)
    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED


def test_budget_blocks_too_long_prompt() -> None:
    budget = Budget(max_calls=3, max_chars=100)
    with pytest.raises(RunFailure) as excinfo:
        budget.spend(101)
    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert budget.calls == 0


def _run_parallel(
    count: int, *, workers: int, gates: dict[str, int], run_class: str
) -> list[tuple[float, float]]:
    """提交 count 个任务，返回每个任务的 (start, end) 相对时间。"""

    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    def handler(run_id: str) -> None:
        start = time.perf_counter()
        time.sleep(0.15)
        end = time.perf_counter()
        with lock:
            spans.append((start, end))

    queue = RunQueue(8, handler, workers=workers, gates=gates)

    async def scenario() -> None:
        queue.start()
        for _ in range(count):
            queue.submit("run", run_class)
        for _ in range(300):
            if len(spans) >= count:
                break
            await asyncio.sleep(0.01)
        await queue.stop()

    asyncio.run(scenario())
    base = min(start for start, _ in spans)
    return sorted((round(start - base, 3), round(end - base, 3)) for start, end in spans)


def test_workers_run_cpu_jobs_concurrently() -> None:
    spans = _run_parallel(2, workers=2, gates={"gpu": 1, "cpu": 2}, run_class="cpu")
    assert spans[0][0] < spans[0][1]
    # 第二个任务在第一个结束前开始 → 真正并行
    assert spans[1][0] < spans[0][1]


def test_gpu_gate_serializes_analysis_jobs() -> None:
    spans = _run_parallel(2, workers=2, gates={"gpu": 1, "cpu": 2}, run_class="gpu")
    assert spans[1][0] >= spans[0][1] - 0.001


def test_cpu_gate_limits_parallelism() -> None:
    spans = _run_parallel(3, workers=3, gates={"gpu": 1, "cpu": 1}, run_class="cpu")
    assert spans[1][0] >= spans[0][1] - 0.001
    assert spans[2][0] >= spans[1][1] - 0.001
