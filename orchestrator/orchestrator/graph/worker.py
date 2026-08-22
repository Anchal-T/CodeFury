"""Level 0 — Worker node: one agent subprocess in an isolated git worktree.

Phase 1 pipeline (plan §9): Task → agent runs in a worktree → tests run →
Report saved to SQLite. The worker command, test command, store and worktree
manager are all injected, so the loop is unit-testable without a real LLM.
"""

from __future__ import annotations

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


def run_tests(test_command: list[str], cwd: Path) -> TestResult:
    """Run the test command in the worktree (list-form, shell=False)."""
    proc = subprocess.run(
        test_command,
        cwd=str(cwd),
        shell=False,
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_S,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode in (0, PYTEST_NO_TESTS_EXIT_CODE)
    return TestResult(passed=passed, output=output.strip())


def _summarize(result: RunnerResult, tests: TestResult) -> str:
    parts: list[str] = []
    if result.timed_out:
        parts.append(f"worker timed out after {result.duration_s:.0f}s")
    parts.append(f"worker exit={result.returncode}, tests {'passed' if tests.passed else 'failed'}")
    tail = (result.stdout or result.stderr).strip()
    if tail:
        parts.append(tail[:SUMMARY_MAX_CHARS])
    return "\n".join(parts)


def _blockers(result: RunnerResult, tests: TestResult, worktree: Path) -> list[str]:
    blockers: list[str] = []
    if result.timed_out:
        blockers.append("worker subprocess timed out")
    elif result.returncode != 0:
        blockers.append(f"worker subprocess exited with code {result.returncode}")
    if not tests.passed:
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
) -> Report:
    """Execute one Task end-to-end and persist the Report.

    The task moves pending → in_progress → review (success) or failed; the
    worker's changes are committed to the worker branch and left there for
    review — merging is a Phase 2 Manager decision.
    """
    task.status = "in_progress"
    store.save_task(task)

    worktree = worktrees.create(task.id)
    result = runner.run(build_worker_prompt(task), cwd=worktree)
    tests = run_tests(test_command, cwd=worktree)
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


def worker_node(state: dict) -> dict:
    """LangGraph node wrapper — wired into the StateGraph in Phase 2."""
    raise NotImplementedError("Phase 2")
