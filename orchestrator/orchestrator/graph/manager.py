"""Level 1 — Manager (plan §2, §9 Phase 2): reviews worker Reports, gates
merges, and applies retry caps. The LangGraph node wrapper that drives this
lives in build_graph; the review logic is kept pure and injectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.contracts import Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.memory.store import StateStore


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
) -> ReviewDecision:
    """Review the latest report per worker task and act on it.

    pass + no blockers → merge the worker branch (--no-ff) and mark done;
    a merge that conflicts fails the task without retry (conflict resolution
    is Domain Lead territory, Phase 3); anything failed within budget goes
    back to pending for another dispatch; past the cap it fails for good.
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
            except RuntimeError as exc:
                decision.failed.append(task.id)
                decision.blockers.append(f"merge conflict for {task.id}: {exc}")
                store.set_task_status(task.id, "failed")
                continue
            decision.merged.append(task.id)
            store.set_task_status(task.id, "done")
            continue

        if retry_policy.can_retry(task.id, attempts.get(task.id, 0)):
            task.status = "pending"
            store.save_task(task)
            decision.retry.append(task.model_dump())
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
) -> Report:
    """Persist the parent task's final status and its aggregate Report."""
    ok = decision.all_done and not decision.failed and not decision.blockers
    parent.status = "review" if ok else "failed"
    store.save_task(parent)
    report = Report(
        task_id=parent.id,
        agent=f"manager:{parent.id}",
        summary=(
            f"{len(decision.merged)} merged, {len(decision.failed)} failed, "
            f"{len(decision.retry)} retrying"
        ),
        diff_ref=None,
        tests_passed=ok,
        tokens_used=sum(r.tokens_used for r in reports),
        blockers=list(decision.blockers),
    )
    store.save_report(report)
    return report
