"""Tests for the Phase 1 worker pipeline (orchestrator.graph.worker).

Uses the committed scripts/fake_worker.py as the worker command and inline
python commands as the test command — no LLM, no network.
"""

import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.graph.worker import run_worker_task
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


@pytest.fixture()
def pipeline(git_repo: Path, tmp_path: Path, python_bin: str):
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    runner = ZCodeRunner(command=[python_bin, str(FAKE_WORKER)])
    passing_tests = [python_bin, "-c", "print('tests ok')"]
    failing_tests = [python_bin, "-c", "raise SystemExit(1)"]
    yield store, worktrees, runner, passing_tests, failing_tests
    store.close()


def make_task() -> Task:
    return Task(
        id="t-42",
        parent_id=None,
        level=0,
        goal="add greeting module",
        deliverable="greeting.py exists",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


def test_happy_path_produces_review_report(pipeline) -> None:
    store, worktrees, runner, passing_tests, _ = pipeline
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=passing_tests,
    )
    assert report.tests_passed
    assert report.blockers == []
    assert report.diff_ref == "orchestrator/worker-t-42"
    assert store.get_task("t-42").status == "review"
    assert store.latest_report("t-42") == report

    worktree = worktrees.worktree_path("t-42")
    assert (worktree / "greeting.py").is_file()
    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], cwd=worktree)
    assert "greeting.py" in committed.splitlines()


def test_failing_tests_fail_the_task(pipeline) -> None:
    store, worktrees, runner, _, failing_tests = pipeline
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=failing_tests,
    )
    assert not report.tests_passed
    assert "tests failed in worktree" in report.blockers
    assert store.get_task("t-42").status == "failed"


def test_worker_timeout_blocks_and_fails(tmp_path: Path, git_repo: Path, python_bin: str) -> None:
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        slow_runner = ZCodeRunner(
            command=[python_bin, "-c", "import time; time.sleep(60)"],
            timeout=1.0,
        )
        report = run_worker_task(
            make_task(),
            store=store,
            worktrees=worktrees,
            runner=slow_runner,
            test_command=[python_bin, "-c", "print('tests ok')"],
        )
        assert "worker subprocess timed out" in report.blockers
        assert not report.tests_passed
        assert store.get_task("t-42").status == "failed"
    finally:
        store.close()


def test_blocked_marker_becomes_blocker(git_repo: Path, tmp_path: Path, python_bin: str) -> None:
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        blocked_runner = ZCodeRunner(
            command=[
                python_bin,
                "-c",
                "from pathlib import Path; Path('BLOCKED.md').write_text('stuck')",
            ]
        )
        report = run_worker_task(
            make_task(),
            store=store,
            worktrees=worktrees,
            runner=blocked_runner,
            test_command=[python_bin, "-c", "print('tests ok')"],
        )
        assert "worker wrote BLOCKED.md" in report.blockers
        assert store.get_task("t-42").status == "failed"
    finally:
        store.close()
