"""Level 1 — Manager (plan §2, §9 Phase 2): reviews worker Reports, gates
merges, and applies retry caps. The LangGraph node wrapper that drives this
lives in build_graph; the review logic is kept pure and injectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.contracts import CONFLICT_BLOCKER_PREFIX, Report, Task
from orchestrator.execution.worktree_manager import MergeConflictError, WorktreeManager
from orchestrator.governance.budget import BUDGET_EXHAUSTED_PREFIX
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.outcome_recorder import (
    IntegrationGate,
    OutcomeRecorder,
    RecordRequest,
)
from orchestrator.memory.store import StateStore

if TYPE_CHECKING:
    from orchestrator.logging_setup import RunLogger
    from orchestrator.memory.vector import VectorMemory


@dataclass
class ReviewDecision:
    """Outcome of reviewing all worker reports for one manager round."""

    merged: list[str] = field(default_factory=list)     # worker task ids merged
    retry: list[dict] = field(default_factory=list)      # worker task dicts to re-dispatch
    failed: list[str] = field(default_factory=list)      # worker task ids failed for good
    blockers: list[str] = field(default_factory=list)    # aggregate blockers
    all_done: bool = False                               # every task merged or failed


def review_reports(
    *,
    reports: list[Report],
    worker_tasks: list[Task],
    attempts: dict[str, int],
    worktrees: WorktreeManager,
    retry_policy: RetryPolicy,
    store: StateStore,
    runlog: "RunLogger | None" = None,
) -> ReviewDecision:
    """Review the latest report per worker task and act on it.

    pass + no blockers → merge the worker branch (--no-ff) and mark done;
    a merge that conflicts fails the task without retry (conflict resolution
    is Domain Lead territory, Phase 3); anything failed within budget goes
    back to pending for another dispatch; past the cap it fails for good.
    ``runlog`` receives merge/retry events (Phase 6).
    """
    decision = ReviewDecision()
    latest_by_task = {report.task_id: report for report in reports}
    for task in worker_tasks:
        report = latest_by_task.get(task.id)
        if report is None:
            continue  # still running, nothing to review yet

        if report.tests_passed and not report.blockers:
            try:
                worktrees.merge(task.id)
            except MergeConflictError as exc:
                decision.failed.append(task.id)
                files = ", ".join(exc.conflicted_files) or "unknown files"
                decision.blockers.append(
                    f"{CONFLICT_BLOCKER_PREFIX} for {task.id}: files: {files}"
                )
                store.set_task_status(task.id, "failed")
                continue
            decision.merged.append(task.id)
            store.set_task_status(task.id, "done")
            if runlog is not None:
                runlog.event("merge", task_id=task.id)
            continue

        if any(b.startswith(BUDGET_EXHAUSTED_PREFIX) for b in report.blockers):
            # The level is out of tokens: a retry would bounce off the gate
            # instantly. Fail for good and surface the budget blocker.
            decision.failed.append(task.id)
            decision.blockers.extend(report.blockers)
            store.set_task_status(task.id, "failed")
            continue

        if retry_policy.can_retry(task.id, attempts.get(task.id, 0)):
            task.status = "pending"
            store.save_task(task)
            decision.retry.append(task.model_dump())
            if runlog is not None:
                runlog.event("retry", task_id=task.id, attempt=attempts.get(task.id, 0) + 1)
        else:
            decision.failed.append(task.id)
            decision.blockers.append(f"worker {task.id} exhausted retries")
            store.set_task_status(task.id, "failed")

    resolved = set(decision.merged) | set(decision.failed) | {t["id"] for t in decision.retry}
    decision.all_done = resolved == {task.id for task in worker_tasks} and not decision.retry
    return decision


def finalize_parent(
    *,
    parent: Task,
    decision: ReviewDecision,
    reports: list[Report],
    store: StateStore,
    repo_root: Path | None = None,
    test_command: list[str] | None = None,
    runlog: "RunLogger | None" = None,
    memory: "VectorMemory | None" = None,
) -> Report:
    """Persist the parent task's final status and its aggregate Report.

    When repo_root and test_command are provided and the merged outcome is
    otherwise OK, the integration gate runs once against the assembled repo
    root (see OutcomeRecorder).
    """
    recorder = OutcomeRecorder(
        store,
        agent_role="manager",
        integration=(
            IntegrationGate(repo_root=repo_root, test_command=test_command)
            if repo_root is not None and test_command is not None
            else None
        ),
        runlog=runlog,
        memory=memory,
    )
    return recorder.record(
        parent,
        RecordRequest(
            ok=decision.all_done and not decision.failed and not decision.blockers,
            blockers=list(decision.blockers),
            summary=(
                f"{len(decision.merged)} merged, {len(decision.failed)} failed, "
                f"{len(decision.retry)} retrying"
            ),
            tokens_used=sum(r.tokens_used for r in reports),
        ),
    )
