"""Tests for the Phase 1 worker pipeline (orchestrator.graph.worker).

Uses the committed scripts/fake_worker.py as the worker command and inline
python commands as the test command — no LLM, no network.
"""

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import RunnerResult, ZCodeRunner
from orchestrator.graph.worker import make_worker_node, run_worker_task
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


def test_test_timeout_saves_failed_report(git_repo: Path, tmp_path: Path, python_bin: str) -> None:
    """A hung test run must produce a failed Report, not a stuck task."""
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = ZCodeRunner(command=[python_bin, str(FAKE_WORKER)])
        hanging_tests = [python_bin, "-c", "import time; time.sleep(60)"]
        report = run_worker_task(
            make_task(),
            store=store,
            worktrees=worktrees,
            runner=runner,
            test_command=hanging_tests,
            tests_timeout=1.0,
        )
        assert not report.tests_passed
        assert "tests timed out in worktree" in report.blockers
        assert store.get_task("t-42").status == "failed"
        assert store.latest_report("t-42") == report
    finally:
        store.close()


def test_pipeline_crash_saves_failed_report(git_repo: Path, tmp_path: Path) -> None:
    """An unexpected pipeline exception must still persist a failed Report."""
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")

        class CrashingRunner(ZCodeRunner):
            def run(self, prompt: str, cwd: Path):
                raise RuntimeError("boom")

        report = run_worker_task(
            make_task(),
            store=store,
            worktrees=worktrees,
            runner=CrashingRunner(),
            test_command=["anything"],
        )
        assert not report.tests_passed
        assert any(b.startswith("pipeline error") for b in report.blockers)
        assert "boom" in report.summary
        assert store.get_task("t-42").status == "failed"
        assert store.latest_report("t-42") == report
    finally:
        store.close()


def test_retry_same_task_id_gets_fresh_worktree(git_repo: Path, tmp_path: Path, python_bin: str) -> None:
    """Retrying a failed task under the same id must succeed end-to-end."""
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = ZCodeRunner(command=[python_bin, str(FAKE_WORKER)])
        failing = [python_bin, "-c", "raise SystemExit(1)"]
        passing = [python_bin, "-c", "print('tests ok')"]

        first = run_worker_task(
            make_task(), store=store, worktrees=worktrees, runner=runner, test_command=failing
        )
        assert not first.tests_passed

        second = run_worker_task(
            make_task(), store=store, worktrees=worktrees, runner=runner, test_command=passing
        )
        assert second.tests_passed
        assert store.get_task("t-42").status == "review"
    finally:
        store.close()


class CountingRunner(ZCodeRunner):
    """Records how many run() calls overlap, to observe the semaphore cap."""

    def __init__(self, command: list[str], hold_s: float = 0.4) -> None:
        super().__init__(command=command)
        self.hold_s = hold_s
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, prompt: str, cwd: Path) -> RunnerResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.hold_s)
            return RunnerResult(returncode=0, stdout="ok", stderr="", timed_out=False, duration_s=self.hold_s)
        finally:
            with self._lock:
                self.active -= 1


def test_worker_node_returns_report_and_persists(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = ZCodeRunner(command=[python_bin, str(FAKE_WORKER)])
        node = make_worker_node(
            store=store,
            worktrees=worktrees,
            runner=runner,
            test_command=[python_bin, "-c", "print('tests ok')"],
            max_workers=2,
        )
        task = make_task()
        store.save_task(task)

        update = asyncio.run(node({"task": task.model_dump()}))

        assert [r["task_id"] for r in update["reports"]] == ["t-42"]
        assert update["reports"][0]["tests_passed"] is True
        assert store.get_task("t-42").status == "review"
    finally:
        store.close()


def test_worker_node_semaphore_caps_concurrency(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        tasks = [
            Task(
                id=f"t-{n}", parent_id="m-1", level=0, goal=f"g{n}", deliverable="d",
                dependencies=[], status="pending", assigned_to=None,
            )
            for n in range(3)
        ]
        for task in tasks:
            store.save_task(task)

        async def scenario(max_workers: int) -> CountingRunner:
            runner = CountingRunner(command=[python_bin, "-c", "pass"])
            node = make_worker_node(
                store=store, worktrees=worktrees, runner=runner,
                test_command=[python_bin, "-c", "print('ok')"], max_workers=max_workers,
            )
            await asyncio.gather(*(node({"task": t.model_dump()}) for t in tasks))
            return runner

        capped = asyncio.run(scenario(max_workers=1))
        assert capped.max_active == 1, "cap of 1 must serialize worker execution"

        wide = asyncio.run(scenario(max_workers=3))
        assert wide.max_active >= 2, "cap of 3 must allow actual parallelism"
    finally:
        store.close()
