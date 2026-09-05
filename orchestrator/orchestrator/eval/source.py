"""Read-only snapshot of a past run (Phase 12): the level-0 tasks and their
latest reports, loaded with SQLite ``mode=ro`` so eval can never mutate the
history it measures against."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.contracts import Report, Task


@dataclass
class SourceSnapshot:
    """A past run's replayable tasks and their latest reports."""

    tasks: list[Task] = field(default_factory=list)
    reports: dict[str, Report] = field(default_factory=dict)


def load_snapshot(db_path: Path) -> SourceSnapshot:
    """Load level-0 tasks + latest report per task from a run database.

    Opens the file read-only (``mode=ro``): any accidental write attempt
    fails in SQLite instead of corrupting the source run. Raises
    FileNotFoundError when the path does not exist.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"no source database at {db_path}")
    # as_uri() percent-encodes and normalizes separators, so Windows
    # backslash paths survive the URI form (repo portability rule).
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tasks = [
            Task.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT payload FROM tasks WHERE level = 0 ORDER BY rowid"
            ).fetchall()
        ]
        reports: dict[str, Report] = {}
        for row in connection.execute("SELECT payload FROM reports ORDER BY id"):
            report = Report.model_validate_json(row[0])
            reports[report.task_id] = report  # later rows win = latest
    finally:
        connection.close()
    return SourceSnapshot(tasks=tasks, reports=reports)


def select_tasks(
    snapshot: SourceSnapshot,
    *,
    ids: list[str] | None = None,
    limit: int | None = None,
) -> list[Task]:
    """Pick the tasks to replay: optional id filter (unknown ids are named,
    not skipped) then an optional first-N limit in stored order."""
    tasks = list(snapshot.tasks)
    if ids:
        wanted = set(ids)
        known = {task.id for task in tasks}
        missing = sorted(wanted - known)
        if missing:
            raise ValueError(f"unknown task id(s) in source database: {missing}")
        tasks = [task for task in tasks if task.id in wanted]
    if limit is not None:
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        tasks = tasks[:limit]
    return tasks
