"""Tests for the eval source snapshot (Phase 12): a read-only view of a past
run's level-0 tasks and latest reports. The source database must never be
touched."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from orchestrator.contracts import Report, Task
from orchestrator.eval.source import SourceSnapshot, load_snapshot, select_tasks
from orchestrator.memory.store import StateStore


def make_task(task_id: str = "w-1", level: int = 0) -> Task:
    return Task(
        id=task_id,
        parent_id="m-1" if level == 0 else None,
        level=level,  # type: ignore[arg-type]
        goal=f"goal {task_id}",
        deliverable="d",
        dependencies=[],
        status="done",
        assigned_to=None,
    )


def make_report(task_id: str = "w-1", passed: bool = True) -> Report:
    return Report(
        task_id=task_id,
        agent=f"worker:{task_id}",
        summary="done" if passed else "failed",
        diff_ref=f"orchestrator/worker-{task_id}",
        tests_passed=passed,
        tokens_used=1234,
        blockers=[] if passed else ["tests failed in worktree"],
    )


@pytest.fixture()
def seeded_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "source.db")
    store.init_schema()
    store.save_task(make_task("w-1"))
    store.save_task(make_task("w-2"))
    store.save_task(make_task("mgr-9", level=1))  # not replayable
    store.save_report(make_report("w-1", passed=True))
    store.save_report(make_report("w-2", passed=False))
    return store


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_carries_level0_tasks_and_latest_reports(seeded_store, tmp_path: Path) -> None:
    snapshot = load_snapshot(tmp_path / "source.db")
    assert [t.id for t in snapshot.tasks] == ["w-1", "w-2"]
    assert snapshot.reports["w-1"].tests_passed is True
    assert snapshot.reports["w-2"].tokens_used == 1234


def test_snapshot_missing_db_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no source database"):
        load_snapshot(tmp_path / "missing.db")


def test_snapshot_never_writes_the_source(seeded_store, tmp_path: Path) -> None:
    """The whole point of eval is comparing against history — the source DB
    must stay byte-identical through loading (SQLite mode=ro)."""
    db_path = tmp_path / "source.db"
    before = _sha256(db_path)
    load_snapshot(db_path)
    assert _sha256(db_path) == before


def test_select_tasks_filters_by_ids(seeded_store, tmp_path: Path) -> None:
    snapshot = load_snapshot(tmp_path / "source.db")
    selected = select_tasks(snapshot, ids=["w-2"])
    assert [t.id for t in selected] == ["w-2"]


def test_select_tasks_unknown_id_names_it(seeded_store, tmp_path: Path) -> None:
    snapshot = load_snapshot(tmp_path / "source.db")
    with pytest.raises(ValueError, match="nope"):
        select_tasks(snapshot, ids=["w-1", "nope"])


def test_select_tasks_limit_takes_first_n(seeded_store, tmp_path: Path) -> None:
    snapshot = load_snapshot(tmp_path / "source.db")
    assert [t.id for t in select_tasks(snapshot, limit=1)] == ["w-1"]


def test_source_snapshot_is_a_dataclass_container() -> None:
    snapshot = SourceSnapshot(tasks=[], reports={})
    assert snapshot.tasks == [] and snapshot.reports == {}
