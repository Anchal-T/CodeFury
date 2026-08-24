"""SQLite state store (plan §3.2): one file, WAL mode, no server.

Tables: tasks, reports, agents, token_usage. Each row keeps the queryable
columns as real columns and the full pydantic model as a JSON payload, so
contracts can evolve without schema churn. Graph checkpoints live in the
same file under SqliteSaver's own tables (checkpoints/writes), opened via
``StateStore.open_checkpointer()`` on this store's single connection —
SQLite stays the one source of truth for task state and resumable graph
state alike (plan §9 Phase 5).

Thread-safe: worker pipelines run concurrently in ``asyncio.to_thread``
threads, so every public method serializes on a lock and the connection is
opened with ``check_same_thread=False``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, TypeVar

from langgraph.checkpoint.sqlite import SqliteSaver

from orchestrator.contracts import Report, Task
from orchestrator.memory.checkpointer import SharedConnectionCheckpointer

T = TypeVar("T")


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _with_lock_retry(op: Callable[[], T]) -> T:
    """Run one write operation, riding out SQLite writer contention.

    Task saves and checkpoint writes hit the same file from independent
    connections (worker threads vs the LangGraph checkpointer). Under bursty
    contention SQLite can also fail fast instead of honoring busy_timeout
    (snapshot upgrades), so each attempt gets the full timeout window and a
    failed attempt retries with backoff.
    """
    for attempt in range(_LOCK_ATTEMPTS):
        try:
            return op()
        except sqlite3.OperationalError as exc:
            if attempt == _LOCK_ATTEMPTS - 1 or not _is_lock_error(exc):
                raise
            time.sleep(_LOCK_BACKOFF_S[min(attempt, len(_LOCK_BACKOFF_S) - 1)])
    raise AssertionError("unreachable")

#: Brief write contention between task saves and checkpoint writes is normal
#: under WAL; wait instead of failing fast with SQLITE_BUSY.
_BUSY_TIMEOUT_MS = 3000
#: ...and when contention outlasts one busy-timeout window (or SQLite skips
#: the busy handler entirely on snapshot upgrades), retry the whole op.
_LOCK_ATTEMPTS = 4
_LOCK_BACKOFF_S = (0.05, 0.1, 0.2)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    level       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    assigned_to TEXT,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    agent        TEXT NOT NULL,
    tests_passed INTEGER NOT NULL,
    tokens_used  INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id      TEXT PRIMARY KEY,
    level   INTEGER NOT NULL,
    role    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      INTEGER NOT NULL,
    tokens     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

class StateStore:
    """Owns all SQLite persistence for the orchestrator."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connection(self) -> sqlite3.Connection:
        """Open (lazily) and return the shared connection."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self) -> None:
        """Create all tables, switch to WAL mode, and migrate old databases.

        Pre-Phase-5 databases carry a stub ``checkpoints`` table (thread_id +
        payload) that collides with SqliteSaver's own schema; it is dropped —
        but only in that exact legacy shape, never once the saver owns the
        table.
        """
        with self._lock:
            conn = self.connection()

            def _migrate() -> None:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
                ).fetchone()
                if row:
                    columns = {col[1] for col in conn.execute("PRAGMA table_info(checkpoints)")}
                    if "checkpoint_ns" not in columns:
                        conn.execute("DROP TABLE checkpoints")
                        conn.commit()

            _with_lock_retry(_migrate)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "StateStore":
        with self._lock:
            self.connection()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- tasks -------------------------------------------------------------

    def _save_task_locked(self, task: Task) -> None:
        """Persist one task row; caller must hold self._lock."""
        self.connection().execute(
            "INSERT OR REPLACE INTO tasks (id, parent_id, level, status, assigned_to, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.id, task.parent_id, task.level, task.status, task.assigned_to, task.model_dump_json()),
        )
        self.connection().commit()

    def _get_task_locked(self, task_id: str) -> Task | None:
        """Load one task; caller must hold self._lock."""
        row = self.connection().execute(
            "SELECT payload FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return Task.model_validate_json(row["payload"]) if row else None

    def save_task(self, task: Task) -> None:
        with self._lock:
            _with_lock_retry(lambda: self._save_task_locked(task))

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            return self._get_task_locked(task_id)

    def set_task_status(self, task_id: str, status: str) -> None:
        """Read-modify-write the status atomically.

        The whole update shares ONE lock acquisition AND one retry unit:
        splitting it across get_task/save_task would let a concurrent
        whole-object save interleave and silently drop this status change
        (last-writer-wins on the row).
        """
        with self._lock:

            def _update() -> None:
                task = self._get_task_locked(task_id)
                if task is None:
                    raise KeyError(f"unknown task id: {task_id}")
                task.status = status
                self._save_task_locked(task)

            _with_lock_retry(_update)

    def tasks_by_parent(self, parent_id: str) -> list[Task]:
        """Children of a task, in insertion order (rowid)."""
        with self._lock:
            rows = self.connection().execute(
                "SELECT payload FROM tasks WHERE parent_id = ? ORDER BY rowid", (parent_id,)
            ).fetchall()
        return [Task.model_validate_json(row["payload"]) for row in rows]

    def tasks_by_status(self, status: str) -> list[Task]:
        """All tasks with the given status, in insertion order (rowid)."""
        with self._lock:
            rows = self.connection().execute(
                "SELECT payload FROM tasks WHERE status = ? ORDER BY rowid", (status,)
            ).fetchall()
        return [Task.model_validate_json(row["payload"]) for row in rows]

    # -- reports -----------------------------------------------------------

    def save_report(self, report: Report) -> None:
        with self._lock:

            def _insert() -> None:
                self.connection().execute(
                    "INSERT INTO reports (task_id, agent, tests_passed, tokens_used, payload)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        report.task_id,
                        report.agent,
                        int(report.tests_passed),
                        report.tokens_used,
                        report.model_dump_json(),
                    ),
                )
                self.connection().commit()

            _with_lock_retry(_insert)

    def latest_report(self, task_id: str) -> Report | None:
        with self._lock:
            row = self.connection().execute(
                "SELECT payload FROM reports WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return Report.model_validate_json(row["payload"]) if row else None

    # -- introspection -----------------------------------------------------

    def table_names(self) -> tuple[str, ...]:
        with self._lock:
            rows = self.connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return tuple(row["name"] for row in rows)

    @asynccontextmanager
    async def open_checkpointer(self) -> AsyncIterator[SharedConnectionCheckpointer]:
        """Yield a checkpointer bound to this store's own SQLite connection.

        langgraph 1.x requires an async checkpointer under ``ainvoke``, but a
        second connection writing the same file contends on SQLite's write
        lock and fails with "database is locked". The yielded adapter runs
        the sync SqliteSaver on THIS store's connection, serialized through
        the same mutex as every other operation — one file, one connection,
        one lock (plan §9 Phase 5). Callers enter the context inside their
        ``asyncio.run`` scope; typically that is ``run_approved_leads``.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        yield SharedConnectionCheckpointer(SqliteSaver(self.connection()), self._lock)
