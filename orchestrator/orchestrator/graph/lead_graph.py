"""Level 2 — Domain Lead graph (plan §3.1, §9 Phase 3).

The lead decomposes its task into level-1 manager tasks, fans them out via
LangGraph ``Send`` over the NESTED Manager/Worker StateGraph (added as a
native subgraph node), reviews the aggregated results, and resolves
cross-module merge conflicts by dispatching ONE reconciliation worker per
conflicting manager (capped). Regardless of outcome it appends a timestamped
section to domains/<domain>/repo_map.md via KnowledgeDocs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send

from orchestrator.contracts import Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.build_graph import TEST_TIMEOUT_S, build_graph
from orchestrator.graph.decompose import DomainDecomposer, SingleWorkerDecomposer
from orchestrator.graph.domain_lead import lead_review
from orchestrator.graph.outcome_recorder import (
    INTEGRATION_FAILED_BLOCKER,
    IntegrationGate,
    OutcomeRecorder,
    RecordRequest,
)
from orchestrator.graph.state import LeadState
from orchestrator.graph.worker import make_worker_node, run_tests
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore
from orchestrator.memory.vector import recall_context


def _attempts_delta(new_recons: list[dict]) -> dict[str, int]:
    """reconcile_attempts updates for freshly created recon tasks.

    Recon ids are '<manager_id>-reconcile-<n>' — manager ids never contain
    the '-reconcile-' marker, so the split is unambiguous.
    """
    delta: dict[str, int] = {}
    for item in new_recons:
        recon_id = str(item["id"])
        manager_id, sep, ordinal = recon_id.rpartition("-reconcile-")
        if sep:
            delta[manager_id] = int(ordinal)
    return delta


def build_lead_graph(
    *,
    store: StateStore,
    worktrees: WorktreeManager,
    runner: ZCodeRunner,
    decomposer: DomainDecomposer,
    test_command: list[str],
    max_workers: int = 3,
    max_reconcile_attempts: int = 1,
    tests_timeout: float = TEST_TIMEOUT_S,
    knowledge: KnowledgeDocs | None = None,
    domains_dir: Path | None = None,
    worker_decomposer=None,
    retry_policy: RetryPolicy | None = None,
    checkpointer=None,
    budget=None,
    runlog=None,
    memory=None,
):
    """Assemble and compile Architect-less lead → managers → workers graph.

    ``checkpointer`` (a LangGraph BaseCheckpointSaver, typically
    ``StateStore.checkpointer()``) makes the whole run — including the nested
    manager/worker subgraphs — resumable under a stable
    ``thread_id = lead:<task id>``; None keeps runs non-persistent.
    ``budget`` + ``runlog`` thread Phase 6 governance through every level.
    """
    knowledge = knowledge or KnowledgeDocs()
    retry_policy = retry_policy or RetryPolicy()
    semaphore = asyncio.Semaphore(max_workers)

    # The nested Manager/Worker graph shares this process's single worker cap.
    # Tier-1 memory is wired into every worker via the shared KnowledgeDocs.
    manager_graph = build_graph(
        store=store,
        worktrees=worktrees,
        runner=runner,
        decomposer=worker_decomposer or SingleWorkerDecomposer(),
        retry_policy=retry_policy,
        test_command=test_command,
        max_workers=max_workers,
        tests_timeout=tests_timeout,
        worker_semaphore=semaphore,
        knowledge=knowledge,
        domains_dir=domains_dir,
        budget=budget,
        runlog=runlog,
        memory=memory,
    )
    reconcile_worker = make_worker_node(
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=test_command,
        max_workers=max_workers,
        tests_timeout=tests_timeout,
        semaphore=semaphore,
        knowledge=knowledge,
        domains_dir=domains_dir,
        budget=budget,
        runlog=runlog,
        memory=memory,
    )

    recorder = OutcomeRecorder(
        store,
        agent_role="lead",
        integration=IntegrationGate(
            repo_root=worktrees.repo_root, test_command=test_command
        ),
        knowledge=knowledge,
        runlog=runlog,
        memory=memory,
    )

    def _finalize(
        lead: Task,
        *,
        ok: bool,
        reports: list[Report],
        blockers: list[str],
        clean_managers: int,
        total_managers: int,
        escalated_manager_ids: list[str],
        merged_recons: list[str],
    ) -> None:
        summary = (
            f"{clean_managers}/{total_managers} manager(s) clean, "
            f"{len(merged_recons)} reconciliation(s) merged, "
            f"{len(escalated_manager_ids)} escalation(s)"
        )
        body = "\n".join(
            [
                f"domain: {lead.domain}",
                f"managers clean: {clean_managers}/{total_managers}",
                f"reconciliations merged: {', '.join(merged_recons) or 'none'}",
                f"escalations: {', '.join(escalated_manager_ids) or 'none'}",
                f"blockers: {'; '.join(blockers) or 'none'}",
            ]
        )
        recorder.record(
            lead,
            RecordRequest(
                ok=ok,
                blockers=blockers,
                summary=summary,
                tokens_used=sum(report.tokens_used for report in reports),
                knowledge_path=(
                    domains_dir / lead.domain / "repo_map.md"
                    if domains_dir is not None and lead.domain
                    else None
                ),
                knowledge_title=f"lead run {lead.id}",
                knowledge_body=body,
            ),
        )

    async def lead(state: LeadState) -> dict:
        lead_task = Task.model_validate(state["lead_task"])

        # Plan mode: decompose once into level-1 manager tasks.
        if not state.get("manager_tasks"):
            lead_task.status = "in_progress"
            store.save_task(lead_task)
            planning_context = ""
            if domains_dir is not None and lead_task.domain:
                planning_context = knowledge.read_latest(
                    domains_dir / lead_task.domain / "repo_map.md"
                )
            # Tier-3 seam (Phase 7): top-k relevant history supplements the
            # markdown context — markdown stays primary, vectors add recall.
            history = recall_context(memory, lead_task.goal)
            if history:
                planning_context = (
                    f"{planning_context}\n\n{history}" if planning_context else history
                )
            children = decomposer.decompose(lead_task, context=planning_context)
            if not children:
                blockers = ["lead produced no manager tasks"]
                _finalize(
                    lead_task,
                    ok=False,
                    reports=[],
                    blockers=blockers,
                    clean_managers=0,
                    total_managers=0,
                    escalated_manager_ids=[],
                    merged_recons=[],
                )
                return {"outcome": "failed", "blockers": blockers}
            for child in children:
                store.save_task(child)
            return {"manager_tasks": [child.model_dump() for child in children]}

        # Review mode: act on aggregated manager results + reconcile reports.
        # Managers already handled (clean, escalated, or covered by a
        # reconciliation dispatch) are excluded — manager_results persist in
        # state across rounds and must not be re-adjudicated.
        resolved = set(state.get("resolved_managers") or [])
        in_reconciliation = set((state.get("reconcile_attempts") or {}).keys())
        pending_results = [
            r for r in (state.get("manager_results") or [])
            if r.get("task_id") not in resolved and r.get("task_id") not in in_reconciliation
        ]
        reports = [Report.model_validate(r) for r in state.get("reports", [])]
        decision = lead_review(
            lead=lead_task,
            manager_results=pending_results,
            reports=reports,
            reconcile_tasks=list(state.get("reconcile_tasks") or []),
            reconcile_attempts=dict(state.get("reconcile_attempts") or {}),
            max_reconcile_attempts=max_reconcile_attempts,
            worktrees=worktrees,
            store=store,
        )
        # A manager whose reconciliation merged cleanly counts as resolved.
        reconciled_managers = [
            str(merged_id).rpartition("-reconcile-")[0]
            for merged_id in decision.merged_recons
        ]
        newly_resolved = [
            *decision.done_managers,
            *decision.escalated,
            *[m for m in reconciled_managers if m],
        ]
        update: dict = {
            "blockers": decision.blockers,
            "escalated": decision.escalated,
            "merged": decision.merged_recons,
            "resolved_managers": newly_resolved,
            "reconcile_tasks": decision.reconcile,
            "reconcile_attempts": _attempts_delta(decision.reconcile),
        }
        if not decision.all_done:
            return update

        total_ids = [t["id"] for t in (state.get("manager_tasks") or [])]
        all_resolved = set(total_ids) <= (resolved | set(newly_resolved))
        # A reconciled conflict still leaves its original blocker in history;
        # terminal ESCALATIONS are what make a domain run fail.
        ever_escalated = set(state.get("escalated") or []) | set(decision.escalated)
        ever_blockers = list(state.get("blockers") or []) + list(decision.blockers)
        ok = all_resolved and not ever_escalated
        if ok:
            integration = run_tests(test_command, cwd=worktrees.repo_root)
            if not integration.passed:
                ok = False
                decision.blockers.append(INTEGRATION_FAILED_BLOCKER)
                ever_blockers.append(INTEGRATION_FAILED_BLOCKER)
        escalated_manager_ids = sorted(
            (set(state.get("escalated") or []) | set(decision.escalated)) & set(total_ids)
        )
        _finalize(
            lead_task,
            ok=ok,
            reports=reports,
            blockers=ever_blockers,
            clean_managers=len(total_ids) - len(escalated_manager_ids),
            total_managers=len(total_ids),
            escalated_manager_ids=escalated_manager_ids,
            merged_recons=decision.merged_recons,
        )
        update["outcome"] = "review" if ok else "failed"
        update["blockers"] = decision.blockers
        return update

    def route(state: LeadState):
        """Send undispached reconciliations/managers; END once decided."""
        if state.get("outcome"):
            return END
        dispatched = set(state.get("dispatched") or [])
        recons = [
            t for t in (state.get("reconcile_tasks") or []) if t["id"] not in dispatched
        ]
        if recons:
            attempts = state.get("attempts") or {}
            return [
                Send("worker", {"task": t, "attempt": attempts.get(t["id"], 0) + 1})
                for t in recons
            ]
        managers = state.get("manager_tasks") or []
        if managers and not state.get("manager_results"):
            return [
                Send("manager", {"manager_task": t}) for t in managers
            ]
        return END

    builder = StateGraph(LeadState)
    builder.add_node("lead", lead)
    builder.add_node("manager", manager_graph)   # native subgraph node
    builder.add_node("worker", reconcile_worker)
    builder.add_edge(START, "lead")
    builder.add_conditional_edges("lead", route, ["manager", "worker", END])
    builder.add_edge("manager", "lead")
    builder.add_edge("worker", "lead")
    return builder.compile(checkpointer=checkpointer)
