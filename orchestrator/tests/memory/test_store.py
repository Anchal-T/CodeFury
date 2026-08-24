"""Tests for orchestrator.memory.store (SQLite WAL persistence)."""

import sqlite3
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.constants import END, START
from langgraph.graph import StateGraph

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
        diff_ref=f"orchestrator/worker-{task_id}",
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
    # The legacy stub `checkpoints` table is gone in Phase 5; SqliteSaver owns
    # its own checkpoints/writes tables, created lazily on first use.
    assert set(store.table_names()) == {"tasks", "reports", "agents", "token_usage"}


def test_init_schema_drops_legacy_stub_checkpoints_table(tmp_path: Path) -> None:
    """Pre-Phase-5 databases carry a stub checkpoints table whose schema
    collides with SqliteSaver's; init_schema must clear exactly that shape."""
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "CREATE TABLE checkpoints (thread_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        raw.commit()
    finally:
        raw.close()

    store = StateStore(db)
    store.init_schema()
    try:
        assert "checkpoints" not in set(store.table_names())
    finally:
        store.close()


def test_init_schema_keeps_saver_checkpoints_table(store: StateStore) -> None:
    """Once the checkpointer has created its own tables, re-running
    init_schema must never drop them."""
    store.checkpointer().get_tuple({"configurable": {"thread_id": "t"}})
    store.init_schema()
    assert "checkpoints" in set(store.table_names())
    assert "writes" in set(store.table_names())


class _TrivialState(TypedDict, total=False):
    n: int


def _trivial_graph(checkpointer):
    builder = StateGraph(_TrivialState)
    builder.add_node("add", lambda state: {"n": state.get("n", 0) + 1})
    builder.add_edge(START, "add")
    builder.add_edge("add", END)
    return builder.compile(checkpointer=checkpointer)


def test_checkpointer_returns_cached_sqlite_saver(store: StateStore) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = store.checkpointer()
    assert isinstance(saver, SqliteSaver)
    assert store.checkpointer() is saver


def test_checkpointer_round_trip_survives_reopen(tmp_path: Path) -> None:
    """The whole point of Phase 5: a checkpoint written by one process is
    readable by a fresh StateStore on the same db file."""
    db = tmp_path / "data" / "cp.db"
    config = {"configurable": {"thread_id": "lead:t1"}}
    with StateStore(db) as store:
        store.init_schema()
        graph = _trivial_graph(store.checkpointer())
        graph.invoke({"n": 1}, config)
        assert "checkpoints" in set(store.table_names())

    with StateStore(db) as reopened:
        reopened.init_schema()
        graph = _trivial_graph(reopened.checkpointer())
        snapshot = graph.get_state(config)
        # The first process's completed run left n=2 (1 in, +1 in the node).
        assert snapshot.values.get("n") == 2


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


def test_concurrent_writes_from_threads(store: StateStore) -> None:
    """Workers will run in threads (asyncio.to_thread); the store must cope."""
    import threading

    errors: list[Exception] = []

    def writer(prefix: str) -> None:
        try:
            for i in range(20):
                store.save_task(make_task(f"{prefix}-{i}"))
                store.save_report(make_report(f"{prefix}-{i}"))
        except Exception as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    tasks_count = store.connection().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    reports_count = store.connection().execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    assert tasks_count == 80
    assert reports_count == 80


def test_set_task_status_atomic_under_mixed_concurrency(store: StateStore) -> None:
    """set_task_status is a read-modify-write; racing it against whole-object
    save_task calls must never corrupt the row or lose the task entirely."""
    import threading

    store.save_task(make_task("race-1"))
    errors: list[Exception] = []
    statuses = ("in_progress", "review", "failed", "done")

    def status_flipper() -> None:
        try:
            for n in range(60):
                store.set_task_status("race-1", statuses[n % len(statuses)])
        except Exception as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)

    def object_saver() -> None:
        try:
            for _ in range(60):
                store.save_task(make_task("race-1", status="pending"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=status_flipper), threading.Thread(target=object_saver)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = store.get_task("race-1")
    assert final is not None, "the raced task must survive"
    assert final.status in {"pending", *statuses}, (
        "row must always reflect one coherent write, never an interleaved one"
    )


def test_tasks_by_parent_returns_children_in_save_order(store: StateStore) -> None:
    from pathlib import Path as _P  # noqa: F401 — keep imports local to this test

    parent = Task(
        id="epic-1", parent_id=None, level=3, goal="g", deliverable="d",
        dependencies=[], status="review", assigned_to=None,
    )
    child_a = parent.model_copy(update={"id": "lead-a", "parent_id": "epic-1", "level": 2, "domain": "backend"})
    child_b = parent.model_copy(update={"id": "lead-b", "parent_id": "epic-1", "level": 2, "domain": "infra"})
    store.save_task(parent)
    store.save_task(child_a)
    store.save_task(child_b)

    children = store.tasks_by_parent("epic-1")
    assert [t.id for t in children] == ["lead-a", "lead-b"]
    assert all(t.parent_id == "epic-1" for t in children)
    assert store.tasks_by_parent("nobody") == []


def test_tasks_by_status_filters_across_parents(store: StateStore) -> None:
    lead_a = make_task("lead-a", status="pending_approval").model_copy(
        update={"parent_id": "epic-1", "level": 2, "domain": "backend"}
    )
    lead_b = make_task("lead-b", status="pending").model_copy(
        update={"parent_id": "epic-1", "level": 2, "domain": "infra"}
    )
    worker = make_task("w-1", status="pending")
    store.save_task(lead_a)
    store.save_task(lead_b)
    store.save_task(worker)

    approved = store.tasks_by_status("pending")
    assert [t.id for t in approved] == ["lead-b", "w-1"]
    waiting = store.tasks_by_status("pending_approval")
    assert [t.id for t in waiting] == ["lead-a"]
    assert store.tasks_by_status("done") == []
