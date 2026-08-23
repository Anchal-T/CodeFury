"""Level 2 — Domain Lead (plan §2, §9 Phase 3): reviews Manager results for
one domain and resolves cross-module merge conflicts through a single,
retry-capped reconciliation dispatch per conflicting manager. The LangGraph
node wrapper lives in build_graph; this review logic stays pure/injectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.contracts import CONFLICT_BLOCKER_PREFIX, Report, Task
from orchestrator.execution.worktree_manager import MergeConflictError, WorktreeManager
from orchestrator.memory.store import StateStore


def conflict_files_from_blocker(blocker: str) -> list[str]:
    """Extract the 'files: a, b' paths from a manager conflict blocker."""
    _, sep, tail = blocker.rpartition("files:")
    if not sep:
        return []
    return [part.strip() for part in tail.split(",") if part.strip()]


@dataclass
class LeadDecision:
    """Outcome of reviewing all manager results for one lead round."""

    done_managers: list[str] = field(default_factory=list)   # managers finished clean
    escalated: list[str] = field(default_factory=list)       # ids failed for good
    reconcile: list[dict] = field(default_factory=list)      # serialized recon Tasks to Send
    merged_recons: list[str] = field(default_factory=list)   # recon ids merged this round
    blockers: list[str] = field(default_factory=list)        # aggregate blockers
    all_done: bool = False


def _is_conflict_only(blockers: list[str]) -> bool:
    return bool(blockers) and all(b.startswith(CONFLICT_BLOCKER_PREFIX) for b in blockers)


def _fail_if_known(store: StateStore, task_id: str) -> None:
    if store.get_task(task_id) is not None:
        store.set_task_status(task_id, "failed")


def lead_review(
    *,
    lead: Task,
    manager_results: list[dict],
    reports: list[Report],
    reconcile_tasks: list[Task | dict],
    reconcile_attempts: dict[str, int],
    max_reconcile_attempts: int,
    worktrees: WorktreeManager,
    store: StateStore,
) -> LeadDecision:
    """Review the latest state of every manager/reconciliation task.

    - Clean manager (final_status "review", no blockers) → done.
    - Conflict-only blockers → ONE reconciliation worker per manager whose
      goal names the conflicted files; tracked via attempts, past the cap a
      second conflict escalates as a blocker instead of re-dispatching.
    - Any non-conflict blocker → escalate immediately, no reconciler.
    """
    decision = LeadDecision()
    latest_by_task = {report.task_id: report for report in reports}
    unresolved_managers = 0
    open_recons = 0

    for item in reconcile_tasks:
        recon = Task.model_validate(item)
        report = latest_by_task.get(recon.id)
        if report is None:
            open_recons += 1
            continue
        if report.tests_passed and not report.blockers:
            try:
                worktrees.merge(recon.id)
            except MergeConflictError as exc:
                decision.escalated.append(recon.id)
                files = ", ".join(exc.conflicted_files) or "unknown files"
                decision.blockers.append(
                    f"{CONFLICT_BLOCKER_PREFIX} for {recon.id}: reconciliation merge "
                    f"conflicted again: files: {files}"
                )
                _fail_if_known(store, recon.id)
            except RuntimeError as exc:
                decision.escalated.append(recon.id)
                decision.blockers.append(f"reconciliation merge failed for {recon.id}: {exc}")
                _fail_if_known(store, recon.id)
            else:
                decision.merged_recons.append(recon.id)
                if store.get_task(recon.id) is not None:
                    store.set_task_status(recon.id, "done")
        else:
            decision.escalated.append(recon.id)
            joined = "; ".join(report.blockers) or "no report details"
            decision.blockers.append(f"reconciliation for {recon.id} failed: {joined}")
            _fail_if_known(store, recon.id)

    for result in manager_results:
        manager_id = str(result.get("task_id"))
        status = result.get("final_status")
        blockers = [str(b) for b in result.get("blockers") or []]
        if status is None:
            unresolved_managers += 1
            continue
        if status == "review" and not blockers:
            decision.done_managers.append(manager_id)
            continue
        if _is_conflict_only(blockers):
            used = int(reconcile_attempts.get(manager_id, 0))
            if used < max_reconcile_attempts:
                files: list[str] = []
                for blocker in blockers:
                    files.extend(conflict_files_from_blocker(blocker))
                unique = ", ".join(dict.fromkeys(files)) or "unspecified files"
                recon = Task(
                    id=f"{manager_id}-reconcile-{used + 1}",
                    parent_id=lead.id,
                    level=0,
                    goal=(
                        f"Resolve cross-module merge conflicts reported by {manager_id}; "
                        f"conflicted files: {unique}. Integrate both intended changes "
                        "so the integrated tests pass."
                    ),
                    deliverable=f"conflict-free integration of {unique}",
                    dependencies=[],
                    status="pending",
                    assigned_to=None,
                    domain=lead.domain,
                )
                store.save_task(recon)
                decision.reconcile.append(recon.model_dump())
            else:
                decision.escalated.append(manager_id)
                decision.blockers.append(
                    f"{CONFLICT_BLOCKER_PREFIX} for {manager_id}: reconciliation exhausted "
                    f"after {used} attempt(s); conflict remains"
                )
                _fail_if_known(store, manager_id)
            continue
        decision.escalated.append(manager_id)
        decision.blockers.extend(blockers or [f"manager {manager_id} failed"])
        _fail_if_known(store, manager_id)

    decision.all_done = (
        unresolved_managers == 0 and open_recons == 0 and not decision.reconcile
    )
    return decision
