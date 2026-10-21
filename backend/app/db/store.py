"""SQLite 持久层（单用户工作台，无 ORM）。

每次操作独立连接 + WAL，避免跨线程共享连接的问题；事件表是 SSE 的唯一事实来源。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    size          INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    content_type  TEXT NOT NULL DEFAULT '',
    detected_type TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    question         TEXT NOT NULL,
    options_json     TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'queued',
    error_code       TEXT,
    error_message    TEXT,
    error_details    TEXT,
    approval_state   TEXT NOT NULL DEFAULT 'none',
    approval_id      TEXT,
    approval_json    TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    degraded         INTEGER NOT NULL DEFAULT 0,
    warnings_json    TEXT NOT NULL DEFAULT '[]',
    run_dir          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    result_summary_json TEXT,
    queue_wait_ms    INTEGER,
    duration_ms      INTEGER,
    llm_calls_count  INTEGER,
    eval_status      TEXT,
    benchmark_key    TEXT,
    baseline_run_id  TEXT
);

CREATE TABLE IF NOT EXISTS run_uploads (
    run_id    TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    PRIMARY KEY (run_id, upload_id)
);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    type       TEXT NOT NULL,
    node       TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);

-- ---------------------------------------------------------------------------
-- P5：模型调用指标 / 节点耗时 / 服务端指标快照
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_calls (
    id                      TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    node                    TEXT NOT NULL DEFAULT '',
    attempt                 INTEGER NOT NULL DEFAULT 1,
    model                   TEXT NOT NULL DEFAULT '',
    endpoint_host           TEXT NOT NULL DEFAULT '',
    streamed                INTEGER NOT NULL DEFAULT 0,
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    reasoning_tokens        INTEGER,
    ttft_first_token_ms     REAL,
    ttft_first_content_ms   REAL,
    total_ms                REAL,
    stream_ms               REAL,
    prefill_tokens_per_s_est REAL,
    decode_tokens_per_s     REAL,
    content_tokens_per_s    REAL,
    finish_reason           TEXT,
    outcome                 TEXT NOT NULL DEFAULT 'ok',
    error_code              TEXT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at DESC);

CREATE TABLE IF NOT EXISTS node_timings (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_node_timings_run ON node_timings(run_id);
CREATE INDEX IF NOT EXISTS idx_node_timings_created ON node_timings(created_at DESC);

CREATE TABLE IF NOT EXISTS llm_metrics_snapshots (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_snapshots_run ON llm_metrics_snapshots(run_id);
"""

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

RUN_UPDATABLE = {
    "status",
    "stage",
    "error_code",
    "error_message",
    "error_details",
    "approval_state",
    "approval_id",
    "approval_json",
    "cancel_requested",
    "degraded",
    "warnings_json",
    "run_dir",
    "started_at",
    "finished_at",
    "result_summary_json",
    "queue_wait_ms",
    "duration_ms",
    "llm_calls_count",
    "eval_status",
    "benchmark_key",
    "baseline_run_id",
}

# 老库（P5 之前）缺的列，启动时幂等补齐
_RUN_COLUMN_MIGRATIONS: dict[str, str] = {
    "result_summary_json": "TEXT",
    "queue_wait_ms": "INTEGER",
    "duration_ms": "INTEGER",
    "llm_calls_count": "INTEGER",
    "eval_status": "TEXT",
    "benchmark_key": "TEXT",
    "baseline_run_id": "TEXT",
}

LLM_CALL_FIELDS = (
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
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class Store:
    """run / event / artifact / upload 的薄仓储层。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cursor() as conn:
            conn.executescript(SCHEMA)
        self._migrate_run_columns()

    def _migrate_run_columns(self) -> None:
        """老库加列（幂等，只做加法，不改历史数据）。"""

        with self._cursor() as conn:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            for name, sql_type in _RUN_COLUMN_MIGRATIONS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {sql_type}")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    def mark_interrupted(self) -> int:
        """把上次进程退出时未完成的任务标记为 interrupted（不宣称断点恢复）。"""

        with self._cursor() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'interrupted', stage = 'interrupted', finished_at = ? "
                "WHERE status IN ('queued', 'running', 'awaiting_approval')",
                (utcnow(),),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # uploads
    # ------------------------------------------------------------------
    def create_upload(
        self,
        *,
        original_name: str,
        stored_path: str,
        size: int,
        sha256: str,
        content_type: str,
        detected_type: str,
    ) -> dict[str, Any]:
        upload_id = new_id("upl")
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO uploads (id, original_name, stored_path, size, sha256, content_type, "
                "detected_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    upload_id,
                    original_name,
                    stored_path,
                    size,
                    sha256,
                    content_type,
                    detected_type,
                    utcnow(),
                ),
            )
        return self.get_upload(upload_id) or {}

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str,
        task_type: str,
        question: str,
        options: dict[str, Any],
        upload_ids: Iterable[str],
        run_dir: str,
    ) -> dict[str, Any]:
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO runs (id, task_type, question, options_json, status, stage, run_dir, created_at) "
                "VALUES (?, ?, ?, ?, 'queued', 'queued', ?, ?)",
                (run_id, task_type, question, json.dumps(options, ensure_ascii=False), run_dir, utcnow()),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO run_uploads (run_id, upload_id) VALUES (?, ?)",
                [(run_id, upload_id) for upload_id in upload_ids],
            )
        return self.get_run(run_id) or {}

    def list_run_upload_ids(self, run_id: str) -> list[str]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT upload_id FROM run_uploads WHERE run_id = ? ORDER BY rowid ASC",
                (run_id,),
            ).fetchall()
        return [str(row["upload_id"]) for row in rows]

    def count_runs_by_status(self) -> dict[str, int]:
        """按状态统计任务数（供 /api/metrics 使用，纯 SQL，无模型调用）。"""

        with self._cursor() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS total FROM runs GROUP BY status").fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode_run(row) if row else None

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - RUN_UPDATABLE
        if unknown:
            raise ValueError(f"不允许更新的字段: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [fields[key] for key in fields]
        with self._cursor() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*values, run_id))

    def request_cancel(self, run_id: str) -> None:
        with self._cursor() as conn:
            conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))

    def is_cancel_requested(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        return bool(run and run.get("cancel_requested"))

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["options"] = _loads(payload.pop("options_json", "{}"), {})
        payload["approval"] = _loads(payload.pop("approval_json", None), None)
        payload["warnings"] = _loads(payload.pop("warnings_json", "[]"), [])
        payload["error_details"] = _loads(payload.pop("error_details", None), None)
        payload["result_summary"] = _loads(payload.pop("result_summary_json", None), None)
        payload["cancel_requested"] = bool(payload.get("cancel_requested"))
        payload["degraded"] = bool(payload.get("degraded"))
        return payload

    # ------------------------------------------------------------------
    # P5：模型调用指标 / 节点耗时 / 服务端快照
    # ------------------------------------------------------------------
    def add_llm_call(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        call_id = new_id("llm")
        values = {field: record.get(field) for field in LLM_CALL_FIELDS}
        values["streamed"] = 1 if record.get("streamed") else 0
        columns = ["id", "run_id", *LLM_CALL_FIELDS, "created_at"]
        placeholders = ", ".join("?" for _ in columns)
        with self._cursor() as conn:
            conn.execute(
                f"INSERT INTO llm_calls ({', '.join(columns)}) VALUES ({placeholders})",
                (call_id, run_id, *[values[field] for field in LLM_CALL_FIELDS], utcnow()),
            )
        return self.get_llm_call(call_id) or {}

    def get_llm_call(self, call_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
        return self._decode_llm_call(row) if row else None

    def list_llm_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,),
            ).fetchall()
        return [self._decode_llm_call(row) for row in rows]

    def llm_calls_since(self, since: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_calls WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [self._decode_llm_call(row) for row in reversed(rows)]

    @staticmethod
    def _decode_llm_call(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["streamed"] = bool(payload.get("streamed"))
        return payload

    def add_node_timing(self, run_id: str, node: str, status: str, duration_ms: float) -> dict[str, Any]:
        timing_id = new_id("nod")
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO node_timings (id, run_id, node, status, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (timing_id, run_id, node, status, float(duration_ms), utcnow()),
            )
        return {
            "id": timing_id,
            "run_id": run_id,
            "node": node,
            "status": status,
            "duration_ms": round(float(duration_ms), 2),
        }

    def list_node_timings(self, run_id: str) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM node_timings WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def node_timings_since(self, since: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM node_timings WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_metrics_snapshot(self, run_id: str, phase: str, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = new_id("mtr")
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO llm_metrics_snapshots (id, run_id, phase, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, run_id, phase, json.dumps(payload, ensure_ascii=False), utcnow()),
            )
        return {"id": snapshot_id, "run_id": run_id, "phase": phase, "payload": payload}

    def list_metrics_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_metrics_snapshots WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "phase": row["phase"],
                "payload": _loads(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def runs_since(self, since: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, task_type, status, error_code, degraded, created_at, started_at, finished_at, "
                "duration_ms, queue_wait_ms, llm_calls_count, eval_status, baseline_run_id, result_summary_json "
                "FROM runs WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result_summary"] = _loads(item.pop("result_summary_json", None), None)
            results.append(item)
        return results

    def latest_run_by_benchmark(self, benchmark_key: str, *, exclude_run_id: str | None = None) -> dict[str, Any] | None:
        query = (
            "SELECT * FROM runs WHERE benchmark_key = ? AND status = 'succeeded' "
            "AND result_summary_json IS NOT NULL"
        )
        params: list[Any] = [benchmark_key]
        if exclude_run_id:
            query += " AND id <> ?"
            params.append(exclude_run_id)
        query += " ORDER BY finished_at DESC, rowid DESC LIMIT 1"
        with self._cursor() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return self._decode_run(row) if row else None

    def artifact_counts(self, run_ids: list[str]) -> dict[str, int]:
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        with self._cursor() as conn:
            rows = conn.execute(
                f"SELECT run_id, COUNT(*) AS total FROM artifacts WHERE run_id IN ({placeholders}) GROUP BY run_id",
                tuple(run_ids),
            ).fetchall()
        return {str(row["run_id"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        node: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._cursor() as conn:
            cursor = conn.execute(
                "INSERT INTO events (run_id, type, node, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, event_type, node, json.dumps(payload or {}, ensure_ascii=False), utcnow()),
            )
            return int(cursor.lastrowid or 0)

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.get("payload"), {})
            events.append(item)
        return events

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------
    def add_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        filename: str,
        rel_path: str,
        size: int,
        sha256: str,
    ) -> dict[str, Any]:
        artifact_id = new_id("art")
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, run_id, kind, filename, rel_path, size, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, run_id, kind, filename, rel_path, size, sha256, utcnow()),
            )
        return self.get_artifact(run_id, artifact_id) or {}

    def get_artifact(self, run_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ? AND run_id = ?",
                (artifact_id, run_id),
            ).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
