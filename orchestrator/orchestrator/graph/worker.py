"""Level 0 — Worker node: one agent subprocess in an isolated git worktree.

Phase 1 pipeline (plan §9): Task → agent runs in a worktree → tests run →
Report saved to SQLite. The worker command, test command, store and worktree
manager are all injected, so the loop is unit-testable without a real LLM.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import PYTEST_NO_TESTS_EXIT_CODE
from orchestrator.contracts import Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import RunnerResult, ZCodeRunner
from orchestrator.memory.store import StateStore
from orchestrator.prompts import build_worker_prompt

TEST_TIMEOUT_S = 600.0
SUMMARY_MAX_CHARS = 500


@dataclass
class TestResult:
    passed: bool
    output: str
    timed_out: bool = False


def run_tests(test_command: list[str], cwd: Path, timeout: float = TEST_TIMEOUT_S) -> TestResult:
    """Run the test command in the worktree (list-form, shell=False).

    A timeout is a test failure, not an exception: the caller must always
    get a TestResult so a Report can be persisted.
    """
    try:
        proc = subprocess.run(
            test_command,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            passed=False,
            output=f"tests timed out after {timeout:.0f}s",
            timed_out=True,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode in (0, PYTEST_NO_TESTS_EXIT_CODE)
    return TestResult(passed=passed, output=output.strip())


def _summarize(result: RunnerResult, tests: TestResult) -> str:
    parts: list[str] = []
    if result.timed_out:
        parts.append(f"worker timed out after {result.duration_s:.0f}s")
    parts.append(f"worker exit={result.returncode}, tests {'passed' if tests.passed else 'failed'}")
    worker_tail = (result.stdout or result.stderr).strip()
    if worker_tail:
        parts.append(worker_tail[:SUMMARY_MAX_CHARS])
    if not tests.passed:
        parts.append("test output tail:\n" + tests.output[-SUMMARY_MAX_CHARS:])
    return "\n".join(parts)


def _blockers(result: RunnerResult, tests: TestResult, worktree: Path) -> list[str]:
    blockers: list[str] = []
    if result.timed_out:
        blockers.append("worker subprocess timed out")
    elif result.returncode != 0:
        blockers.append(f"worker subprocess exited with code {result.returncode}")
    if tests.timed_out:
        blockers.append("tests timed out in worktree")
    elif not tests.passed:
        blockers.append("tests failed in worktree")
    if (worktree / "BLOCKED.md").is_file():
        blockers.append("worker wrote BLOCKED.md")
    return blockers


def run_worker_task(
    task: Task,
    *,
    store: StateStore,
    worktrees: WorktreeManager,
    runner: ZCodeRunner,
    test_command: list[str],
    tests_timeout: float = TEST_TIMEOUT_S,
) -> Report:
    """Execute one Task end-to-end and persist the Report.

    The task moves pending → in_progress → review (success) or failed, and a
    Report is ALWAYS saved — even when the pipeline itself crashes — so a
    task is never left in_progress with no trace of what happened.
    """
    task.status = "in_progress"
    store.save_task(task)
    try:
        return _run_pipeline(
            task,
            store=store,
            worktrees=worktrees,
            runner=runner,
            test_command=test_command,
            tests_timeout=tests_timeout,
        )
    except Exception as exc:
        report = Report(
            task_id=task.id,
            agent=f"worker:{task.id}",
            summary=f"pipeline error: {exc!r}",
            diff_ref=None,
            tests_passed=False,
            tokens_used=0,
            blockers=[f"pipeline error: {exc}"],
        )
        store.save_report(report)
        task.status = "failed"
        store.save_task(task)
        return report


def _run_pipeline(
    task: Task,
    *,
    store: StateStore,
    worktrees: WorktreeManager,
    runner: ZCodeRunner,
    test_command: list[str],
    tests_timeout: float,
) -> Report:
    worktree = worktrees.create(task.id)
    result = runner.run(build_worker_prompt(task), cwd=worktree)
    tests = run_tests(test_command, cwd=worktree, timeout=tests_timeout)
    worktrees.commit(task.id, f"worker({task.id}): {task.goal}")

    blockers = _blockers(result, tests, worktree)
    report = Report(
        task_id=task.id,
        agent=f"worker:{task.id}",
        summary=_summarize(result, tests),
        diff_ref=worktrees.branch_name(task.id),
        tests_passed=tests.passed and not blockers,
        # Phase 6 parses real token usage from the worker output; until then
        # usage is attributed via governance counters at the calling level.
        tokens_used=0,
        blockers=blockers,
    )
    store.save_report(report)
    task.status = "failed" if blockers else "review"
    store.save_task(task)
    return report


def make_worker_node(
    *,
    store: StateStore,
    worktrees: WorktreeManager,
    runner: ZCodeRunner,
    test_command: list[str],
    max_workers: int = 3,
    tests_timeout: float = TEST_TIMEOUT_S,
):
    """Build the async LangGraph worker node with a concurrency cap.

    The semaphore enforces config concurrency.max_workers (plan §4): never
    more worker subprocesses than the budget allows, real parallelism below
    it. The blocking pipeline runs in a thread so one implementation serves
    both the CLI and the graph.
    """
    semaphore = asyncio.Semaphore(max_workers)

    async def node(state: dict) -> dict:
        task = Task.model_validate(state["task"])
        attempt = int(state.get("attempt", 1))
        async with semaphore:
            report = await asyncio.to_thread(
                run_worker_task,
                task,
                store=store,
                worktrees=worktrees,
                runner=runner,
                test_command=test_command,
                tests_timeout=tests_timeout,
            )
        return {
            "reports": [report.model_dump()],
            "attempts": {task.id: attempt},
            "dispatched": [task.id],
        }

    return node
