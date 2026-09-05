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

from orchestrator.config import BudgetsConfig
from orchestrator.contracts import Task
from orchestrator.execution.cli_runner import AgentCliRunner
from orchestrator.execution.runner import RunnerResult
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.governance.budget import BUDGET_EXHAUSTED_PREFIX, BudgetTracker
from orchestrator.graph.worker import (
    make_worker_node,
    run_worker_task,
)
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"


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
def pipeline(git_repo: Path, tmp_path: Path, python_bin: str):
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    runner = AgentCliRunner(command=[python_bin, str(FAKE_WORKER)])
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
    modules = list(worktree.glob("module_*.py"))
    assert len(modules) == 1
    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], cwd=worktree)
    assert modules[0].name in committed.splitlines()


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
        slow_runner = AgentCliRunner(
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
        blocked_runner = AgentCliRunner(
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
        runner = AgentCliRunner(command=[python_bin, str(FAKE_WORKER)])
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

        class CrashingRunner(AgentCliRunner):
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
        runner = AgentCliRunner(command=[python_bin, str(FAKE_WORKER)])
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


class CountingRunner(AgentCliRunner):
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
        runner = AgentCliRunner(command=[python_bin, str(FAKE_WORKER)])
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


def test_make_worker_node_rejects_non_positive_max_workers() -> None:
    """A Semaphore(0) would silently deadlock every dispatch — reject it."""
    with pytest.raises(ValueError):
        make_worker_node(
            store=None,  # type: ignore[arg-type] — validation fires before use
            worktrees=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            test_command=[],
            max_workers=0,
        )


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


class RecordingRunner:
    """Captures the prompt while behaving like a successful worker."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, prompt: str, cwd: Path) -> RunnerResult:
        self.prompts.append(prompt)
        return RunnerResult(returncode=0, stdout="ok", stderr="", timed_out=False, duration_s=0.0)


def make_domain_task(domain: str | None) -> Task:
    task = make_task()
    return task.model_copy(update={"id": f"t-{domain or 'x'}", "domain": domain})


def test_pipeline_prepends_knowledge_context_to_prompt(pipeline) -> None:
    """Tier-1 memory reaches the subprocess prompt ahead of the Task JSON."""
    store, worktrees, _, passing_tests, _ = pipeline
    runner = RecordingRunner()

    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,  # type: ignore[arg-type] — same run() interface
        test_command=passing_tests,
        knowledge_context="decision: auth module merged last round",
    )

    assert report.tests_passed
    prompt = runner.prompts[0]
    assert "Project knowledge" in prompt
    assert "auth module merged last round" in prompt
    assert prompt.index("auth module merged") < prompt.index("Task (JSON)")


def test_pipeline_includes_retry_feedback_in_prompt(pipeline) -> None:
    """Phase 10: retry feedback reaches the prompt ahead of the Task JSON."""
    store, worktrees, _, passing_tests, _ = pipeline
    runner = RecordingRunner()

    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,  # type: ignore[arg-type]
        test_command=passing_tests,
        feedback="## Previous attempt feedback\n\n- tests failed in worktree",
    )

    assert report.tests_passed
    prompt = runner.prompts[0]
    assert "Previous attempt feedback" in prompt
    assert prompt.index("Previous attempt feedback") < prompt.index("Task (JSON)")


def test_worker_node_threads_state_feedback(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    """The graph's retry dispatch carries `feedback` in the Send payload; the
    worker node must hand it to the pipeline (Phase 10 wiring)."""
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = RecordingRunner()
        node = make_worker_node(
            store=store,
            worktrees=worktrees,
            runner=runner,  # type: ignore[arg-type]
            test_command=[python_bin, "-c", "print('tests ok')"],
            max_workers=2,
        )
        task = make_task()
        store.save_task(task)

        update = asyncio.run(node({"task": task.model_dump(), "feedback": "fix the flub"}))

        assert update["reports"][0]["tests_passed"] is True
        assert "fix the flub" in runner.prompts[0]
    finally:
        store.close()


def test_worker_node_reads_latest_domain_repo_map(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    """The node resolves the task's domain repo_map.md and hands its latest
    section to the pipeline as knowledge context."""
    from orchestrator.memory.knowledge_docs import KnowledgeDocs

    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        domains_dir = tmp_path / "domains"
        knowledge = KnowledgeDocs()
        knowledge.append_section(
            domains_dir / "backend" / "repo_map.md",
            title="lead run lead-1",
            body="repo map marker XYZ",
        )
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = RecordingRunner()
        node = make_worker_node(
            store=store,
            worktrees=worktrees,
            runner=runner,  # type: ignore[arg-type]
            test_command=[python_bin, "-c", "print('tests ok')"],
            max_workers=2,
            knowledge=knowledge,
            domains_dir=domains_dir,
        )
        task = make_domain_task("backend")
        store.save_task(task)

        update = asyncio.run(node({"task": task.model_dump()}))

        assert update["reports"][0]["tests_passed"] is True
        prompt = runner.prompts[0]
        assert "repo map marker XYZ" in prompt

    finally:
        store.close()


def test_worker_node_without_knowledge_wiring_omits_context(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    """No knowledge deps (or no domain) → clean prompt, never a crash."""
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        runner = RecordingRunner()
        node = make_worker_node(
            store=store,
            worktrees=worktrees,
            runner=runner,  # type: ignore[arg-type]
            test_command=[python_bin, "-c", "print('tests ok')"],
            max_workers=2,
        )
        task = make_domain_task(None)
        store.save_task(task)

        update = asyncio.run(node({"task": task.model_dump()}))

        assert update["reports"][0]["tests_passed"] is True
        assert "Project knowledge" not in runner.prompts[0]
    finally:
        store.close()


# -- Phase 6: token attribution + budget gate -------------------------------


class MemoryRunLogger:
    """Test double capturing events in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, kind: str, **fields: object) -> None:
        self.events.append((kind, dict(fields)))


class FakeVectorMemory:
    """Captures upserts; same interface as VectorMemory.upsert."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def upsert(self, text: str, **metadata: object) -> None:
        self.entries.append((text, dict(metadata)))


def test_worker_indexes_report_summary(pipeline) -> None:
    """Report summaries land in Tier-3 memory for later semantic recall."""
    store, worktrees, runner, passing_tests, _ = pipeline
    memory = FakeVectorMemory()

    run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=passing_tests,
        memory=memory,
    )

    assert len(memory.entries) == 1
    text, meta = memory.entries[0]
    assert "worker exit=0" in text
    assert meta["source"] == "report"
    assert meta["ref"] == "t-42"
    assert meta["title"] == "worker:t-42"


def test_worker_without_memory_skips_indexing(pipeline) -> None:
    store, worktrees, runner, passing_tests, _ = pipeline
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=passing_tests,
    )
    assert report.tests_passed  # no memory passed — nothing to assert beyond no-crash


def test_pipeline_emits_task_start_and_end_events(pipeline) -> None:
    store, worktrees, runner, passing_tests, _ = pipeline
    runlog = MemoryRunLogger()
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=passing_tests,
        runlog=runlog,
    )
    assert [kind for kind, _ in runlog.events] == ["task_start", "task_end"]
    assert runlog.events[0][1] == {"task_id": "t-42", "level": 0}
    assert runlog.events[1][1] == {"task_id": "t-42", "status": "review"}
    del report


def test_budget_gate_emits_start_and_failed_end(pipeline) -> None:
    """Even a skipped dispatch leaves start/end traces in the JSONL log."""
    store, worktrees, _, passing_tests, _ = pipeline
    budget = BudgetTracker(store, BudgetsConfig(worker_tokens=1))
    budget.record(0, 1)
    runlog = MemoryRunLogger()
    run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=NeverRunner(),  # type: ignore[arg-type]
        test_command=passing_tests,
        budget=budget,
        runlog=runlog,
    )
    assert [kind for kind, _ in runlog.events] == ["task_start", "task_end"]
    assert runlog.events[1][1] == {"task_id": "t-42", "status": "failed"}


class NeverRunner:
    """Fails the test the moment the pipeline tries to spawn a worker."""

    def run(self, prompt: str, cwd: Path) -> RunnerResult:
        raise AssertionError("runner must not be invoked once budget is exhausted")


def test_exhausted_budget_short_circuits_before_running_worker(pipeline) -> None:
    """Exhausted budget = immediate blocker report, never a hang and no
    worktree/subprocess spend (mirrors the concurrency lesson)."""
    store, worktrees, _, passing_tests, _ = pipeline
    budget = BudgetTracker(store, BudgetsConfig(worker_tokens=10))
    budget.record(0, 10)

    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=NeverRunner(),  # type: ignore[arg-type]
        test_command=passing_tests,
        budget=budget,
    )

    assert not report.tests_passed
    assert report.blockers[0].startswith(BUDGET_EXHAUSTED_PREFIX)
    assert report.tokens_used == 0
    assert store.get_task("t-42").status == "failed"
    assert store.latest_report("t-42") == report
    assert not worktrees.worktree_path("t-42").exists()


def test_healthy_budget_lets_dispatch_proceed(pipeline) -> None:
    store, worktrees, runner, passing_tests, _ = pipeline
    budget = BudgetTracker(store, BudgetsConfig(worker_tokens=10))
    budget.record(0, 9)
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=passing_tests,
        budget=budget,
    )
    assert report.tests_passed


def test_tokens_parsed_from_output_and_recorded(pipeline) -> None:
    """Report.tokens_used becomes real; the tracker books it at level 0."""
    store, worktrees, _, passing_tests, _ = pipeline

    class TokenRunner:
        def run(self, prompt: str, cwd: Path) -> RunnerResult:
            return RunnerResult(
                returncode=0,
                stdout="worked\nTOKENS_USED: 250",
                stderr="",
                timed_out=False,
                duration_s=0.1,
            )

    budget = BudgetTracker(store, BudgetsConfig(worker_tokens=1000))
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=TokenRunner(),  # type: ignore[arg-type]
        test_command=passing_tests,
        budget=budget,
    )

    assert report.tokens_used == 250
    assert store.total_tokens_by_level(0) == 250
    assert budget.remaining(0) == 750


def test_report_carries_real_tokens_even_without_tracker(pipeline) -> None:
    """Attribution works standalone: parsing does not require a cap."""
    store, worktrees, _, passing_tests, _ = pipeline

    class TokenRunner:
        def run(self, prompt: str, cwd: Path) -> RunnerResult:
            return RunnerResult(
                returncode=0,
                stdout="TOKENS_USED: 77",
                stderr="",
                timed_out=False,
                duration_s=0.1,
            )

    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=TokenRunner(),  # type: ignore[arg-type]
        test_command=passing_tests,
    )
    assert report.tokens_used == 77
    assert store.total_tokens_by_level(0) == 0, "no tracker, no booking"


def test_runner_reported_tokens_win_over_output_marker(pipeline) -> None:
    """A harness with first-class attribution books its own number (Phase 9
    contract); the output convention only serves runners that can't report."""
    store, worktrees, _, passing_tests, _ = pipeline

    class ReportingRunner:
        def run(self, prompt: str, cwd: Path) -> RunnerResult:
            return RunnerResult(
                returncode=0,
                stdout="TOKENS_USED: 100",
                stderr="",
                timed_out=False,
                duration_s=0.1,
                tokens_used=300,
            )

    budget = BudgetTracker(store, BudgetsConfig(worker_tokens=1000))
    report = run_worker_task(
        make_task(),
        store=store,
        worktrees=worktrees,
        runner=ReportingRunner(),  # type: ignore[arg-type]
        test_command=passing_tests,
        budget=budget,
    )

    assert report.tokens_used == 300
    assert store.total_tokens_by_level(0) == 300
    assert budget.remaining(0) == 700


def test_worker_node_threads_budget_gate_to_pipeline(
    git_repo: Path, tmp_path: Path, python_bin: str
) -> None:
    store = StateStore(tmp_path / "data" / "orchestrator.db")
    store.init_schema()
    try:
        worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
        budget = BudgetTracker(store, BudgetsConfig(worker_tokens=1))
        budget.record(0, 1)
        node = make_worker_node(
            store=store,
            worktrees=worktrees,
            runner=NeverRunner(),  # type: ignore[arg-type]
            test_command=[python_bin, "-c", "print('tests ok')"],
            max_workers=2,
            budget=budget,
        )
        task = make_task()
        store.save_task(task)

        update = asyncio.run(node({"task": task.model_dump()}))

        blockers = update["reports"][0]["blockers"]
        assert any(b.startswith(BUDGET_EXHAUSTED_PREFIX) for b in blockers)
        assert store.get_task("t-42").status == "failed"
    finally:
        store.close()
