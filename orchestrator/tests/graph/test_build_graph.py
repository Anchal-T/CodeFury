"""End-to-end tests for the Phase 2 StateGraph (plan §9):

Manager fans workers out via LangGraph Send, reviews reports, merges passing
branches, retries within caps, and provably terminates when the retry cap is
hit — the runaway-loop guard the plan demands.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.build_graph import build_graph
from orchestrator.graph.decompose import StaticDecomposer
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"
GRAPH_TIMEOUT_S = 90.0


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def git_repo(tmp_path: Path, git_init) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git_init(root)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_parent() -> Task:
    return Task(
        id="m-1",
        parent_id=None,
        level=1,
        goal="ship two modules",
        deliverable="both modules exist",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


def test_graph_merges_both_workers_end_to_end(
    git_repo: Path, store: StateStore, python_bin: str
) -> None:
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    graph = build_graph(
        store=store,
        worktrees=worktrees,
        runner=ZCodeRunner(command=[python_bin, str(FAKE_WORKER)]),
        decomposer=StaticDecomposer(["add module one", "add module two"]),
        retry_policy=RetryPolicy(max_worker_retries=2, max_manager_escalations=1),
        test_command=[python_bin, "-c", "print('tests ok')"],
        max_workers=2,
    )
    store.save_task(make_parent())

    result = asyncio.run(
        asyncio.wait_for(
            graph.ainvoke({"manager_task": make_parent().model_dump()}), GRAPH_TIMEOUT_S
        )
    )

    assert result["final_status"] == "review"
    assert len(result["merged"]) == 2
    assert len(list(git_repo.glob("module_*.py"))) == 2
    assert result["attempts"] == {tid: 1 for tid in result["attempts"]}

    workers = [t for t in store.connection().execute("SELECT payload FROM tasks WHERE level = 0")]
    assert len(workers) == 2
    for row in workers:
        task = Task.model_validate_json(row["payload"])
        assert task.parent_id == "m-1"
    worker_reports = store.connection().execute(
        "SELECT COUNT(*) FROM reports WHERE agent LIKE 'worker:%'"
    ).fetchone()[0]
    manager_reports = store.connection().execute(
        "SELECT COUNT(*) FROM reports WHERE agent = 'manager:m-1'"
    ).fetchone()[0]
    assert worker_reports == 2
    assert manager_reports == 1
    assert store.get_task("m-1").status == "review"


def test_graph_emits_manager_result_summary(
    git_repo: Path, store: StateStore, python_bin: str
) -> None:
    """Phase 3 nesting contract: the manager graph reports one structured
    result so a Domain Lead can attribute outcomes per manager."""
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    graph = build_graph(
        store=store,
        worktrees=worktrees,
        runner=ZCodeRunner(command=[python_bin, str(FAKE_WORKER)]),
        decomposer=StaticDecomposer(["add module one"]),
        retry_policy=RetryPolicy(max_worker_retries=2, max_manager_escalations=1),
        test_command=[python_bin, "-c", "print('tests ok')"],
        max_workers=2,
    )
    store.save_task(make_parent())

    result = asyncio.run(
        asyncio.wait_for(
            graph.ainvoke({"manager_task": make_parent().model_dump()}), GRAPH_TIMEOUT_S
        )
    )

    results = result["manager_results"]
    assert len(results) == 1
    entry = results[0]
    assert entry["task_id"] == "m-1"
    assert entry["final_status"] == "review"
    assert len(entry["merged"]) == 1
    assert entry["blockers"] == []
    assert set(entry["attempts"].values()) == {1}


def test_retry_cap_stops_runaway_loops(
    git_repo: Path, store: StateStore, python_bin: str
) -> None:
    """An always-failing worker gets exactly max_worker_retries + 1 attempts,
    then the graph terminates with a failed parent — never an infinite loop."""
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    failing_runner = ZCodeRunner(command=[python_bin, "-c", "raise SystemExit(1)"])
    graph = build_graph(
        store=store,
        worktrees=worktrees,
        runner=failing_runner,
        decomposer=StaticDecomposer(["impossible task"]),
        retry_policy=RetryPolicy(max_worker_retries=2, max_manager_escalations=1),
        test_command=[python_bin, "-c", "print('tests ok')"],
        max_workers=2,
    )
    store.save_task(make_parent())

    result = asyncio.run(
        asyncio.wait_for(
            graph.ainvoke({"manager_task": make_parent().model_dump()}), GRAPH_TIMEOUT_S
        )
    )

    assert result["final_status"] == "failed"
    assert list(result["attempts"].values()) == [3]  # initial + 2 retries, then STOP
    assert result["merged"] == []
    assert any("exhausted" in b for b in result["blockers"])
    assert store.get_task("m-1").status == "failed"
    worker_reports = store.connection().execute(
        "SELECT COUNT(*) FROM reports WHERE agent LIKE 'worker:%'"
    ).fetchone()[0]
    assert worker_reports == 3  # one report per attempt, no more


def test_empty_decomposition_fails_fast(
    git_repo: Path, store: StateStore, python_bin: str
) -> None:
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    graph = build_graph(
        store=store,
        worktrees=worktrees,
        runner=ZCodeRunner(command=[python_bin, "-c", "pass"]),
        decomposer=StaticDecomposer([]),
        retry_policy=RetryPolicy(max_worker_retries=2, max_manager_escalations=1),
        test_command=[python_bin, "-c", "print('tests ok')"],
        max_workers=2,
    )
    store.save_task(make_parent())

    result = asyncio.run(
        asyncio.wait_for(
            graph.ainvoke({"manager_task": make_parent().model_dump()}), GRAPH_TIMEOUT_S
        )
    )

    assert result["final_status"] == "failed"
    assert any("no sub-tasks" in b for b in result["blockers"])
    assert result["merged"] == []
