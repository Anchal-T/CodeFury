"""SQLite state store (plan §3.2): one file, WAL mode, no server.

Tables: tasks, reports, agents, checkpoints, token_usage. Each row keeps the
queryable columns as real columns and the full pydantic model as a JSON
payload, so contracts can evolve without schema churn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from orchestrator.contracts import Report, Task

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
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT PRIMARY KEY,
    payload   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      INTEGER NOT NULL,
    tokens     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_TABLES = ("tasks", "reports", "agents", "checkpoints", "token_usage")


class StateStore:
    """Owns all SQLite persistence for the orchestrator."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connection(self) -> sqlite3.Connection:
        """Open (lazily) and return the shared connection."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self) -> None:
        """Create all tables and switch the database to WAL mode."""
        conn = self.connection()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "StateStore":
        self.connection()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- tasks -------------------------------------------------------------

    def save_task(self, task: Task) -> None:
        self.connection().execute(
            "INSERT OR REPLACE INTO tasks (id, parent_id, level, status, assigned_to, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.id, task.parent_id, task.level, task.status, task.assigned_to, task.model_dump_json()),
        )
        self.connection().commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self.connection().execute(
            "SELECT payload FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return Task.model_validate_json(row["payload"]) if row else None

    def set_task_status(self, task_id: str, status: str) -> None:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task id: {task_id}")
        task.status = status
        self.save_task(task)

    # -- reports -----------------------------------------------------------

    def save_report(self, report: Report) -> None:
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

    def latest_report(self, task_id: str) -> Report | None:
        row = self.connection().execute(
            "SELECT payload FROM reports WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return Report.model_validate_json(row["payload"]) if row else None

    # -- introspection -----------------------------------------------------

    def table_names(self) -> tuple[str, ...]:
        rows = self.connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return tuple(row["name"] for row in rows)

    def checkpointer(self):
        """Return a LangGraph SqliteSaver bound to the same db file."""
        raise NotImplementedError("Phase 5")
