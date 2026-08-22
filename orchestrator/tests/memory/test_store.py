"""Tests for orchestrator.memory.store (SQLite WAL persistence)."""

import sqlite3
from pathlib import Path

import pytest

from orchestrator.contracts import Report, Task
from orchestrator.memory.store import StateStore


def make_task(task_id: str = "t1", status: str = "pending") -> Task:
    return Task(
        id=task_id,
        parent_id=None,
        level=0,
        goal="do the thing",
        deliverable="a file",
        dependencies=[],
        status=status,
        assigned_to=None,
    )


def make_report(task_id: str = "t1") -> Report:
    return Report(
        task_id=task_id,
        agent=f"worker:{task_id}",
        summary="done",
        diff_ref="orchestrator/worker-t1",
        tests_passed=True,
        tokens_used=0,
        blockers=[],
    )


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def test_init_schema_creates_all_tables(store: StateStore) -> None:
    assert set(store.table_names()) == {"tasks", "reports", "agents", "checkpoints", "token_usage"}


def test_wal_mode_enabled(store: StateStore) -> None:
    mode = store.connection().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_task_round_trip(store: StateStore) -> None:
    task = make_task("t1")
    store.save_task(task)
    assert store.get_task("t1") == task
    assert store.get_task("missing") is None


def test_set_task_status_updates_row_and_payload(store: StateStore) -> None:
    store.save_task(make_task("t1"))
    store.set_task_status("t1", "review")
    row = store.connection().execute(
        "SELECT status FROM tasks WHERE id = 't1'"
    ).fetchone()
    assert row["status"] == "review"
    assert store.get_task("t1").status == "review"


def test_set_task_status_unknown_id_raises(store: StateStore) -> None:
    with pytest.raises(KeyError):
        store.set_task_status("nope", "review")


def test_latest_report_wins(store: StateStore) -> None:
    first = make_report("t1")
    second = make_report("t1").model_copy(update={"summary": "retry done"})
    store.save_report(first)
    store.save_report(second)
    assert store.latest_report("t1") == second
    assert store.latest_report("missing") is None


def test_report_columns_queryable(store: StateStore) -> None:
    store.save_report(make_report("t1"))
    row = store.connection().execute(
        "SELECT task_id, agent, tests_passed, tokens_used FROM reports"
    ).fetchone()
    assert row["task_id"] == "t1"
    assert row["agent"] == "worker:t1"
    assert row["tests_passed"] == 1
    assert row["tokens_used"] == 0


def test_db_file_created_in_missing_dir(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "nested" / "deep" / "x.db")
    store.init_schema()
    try:
        assert (tmp_path / "nested" / "deep" / "x.db").is_file()
    finally:
        store.close()
