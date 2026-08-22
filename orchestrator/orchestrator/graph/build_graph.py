"""Wires Manager ↔ Worker into one StateGraph (plan §3.1, §9 Phase 2).

The Manager decomposes its task, fans workers out via LangGraph ``Send``
(parallel branches sharing one semaphore-capped worker node), reviews the
reports, merges passing branches, retries failures within hard caps, and
terminates when everything is merged or failed.
"""

from __future__ import annotations

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send

from orchestrator.contracts import Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.decompose import Decomposer
from orchestrator.graph.manager import ReviewDecision, finalize_parent, review_reports
from orchestrator.graph.state import OrchestratorState
from orchestrator.graph.worker import TEST_TIMEOUT_S, make_worker_node
from orchestrator.memory.store import StateStore


def build_graph(
    *,
    store: StateStore,
    worktrees: WorktreeManager,
    runner: ZCodeRunner,
    decomposer: Decomposer,
    retry_policy: RetryPolicy,
    test_command: list[str],
    max_workers: int = 3,
    tests_timeout: float = TEST_TIMEOUT_S,
):
    """Assemble and compile the Manager/Worker StateGraph with injected deps."""
    worker = make_worker_node(
        store=store,
        worktrees=worktrees,
        runner=runner,
        test_command=test_command,
        max_workers=max_workers,
        tests_timeout=tests_timeout,
    )

    async def manager(state: OrchestratorState) -> dict:
        parent = Task.model_validate(state["manager_task"])

        # Plan mode: decompose once, persist, and hand routing to dispatch().
        if not state.get("worker_tasks"):
            parent.status = "in_progress"
            store.save_task(parent)
            children = decomposer.decompose(parent)
            if not children:
                decision = ReviewDecision(
                    blockers=["manager produced no sub-tasks"], all_done=True
                )
                finalize_parent(parent=parent, decision=decision, reports=[], store=store)
                return {
                    "final_status": parent.status,
                    "blockers": decision.blockers,
                    "merged": [],
                    "retrying": [],
                }
            for child in children:
                store.save_task(child)
            return {"worker_tasks": [child.model_dump() for child in children], "retrying": []}

        # Review mode: act on the latest report per worker task.
        reports = [Report.model_validate(r) for r in state.get("reports", [])]
        tasks = [Task.model_validate(t) for t in state["worker_tasks"]]
        decision = review_reports(
            reports=reports,
            worker_tasks=tasks,
            attempts=dict(state.get("attempts", {})),
            worktrees=worktrees,
            retry_policy=retry_policy,
            store=store,
        )
        update: dict = {
            "merged": decision.merged,
            "blockers": decision.blockers,
            "retrying": decision.retry,
        }
        if decision.all_done:
            finalize_parent(parent=parent, decision=decision, reports=reports, store=store)
            update["final_status"] = parent.status
        return update

    def dispatch(state: OrchestratorState):
        """Route manager output: Send retries/new tasks to workers, else END."""
        if state.get("final_status"):
            return END
        retrying = state.get("retrying") or []
        if retrying:
            batch = retrying
        else:
            already_dispatched = set(state.get("dispatched", []))
            batch = [t for t in state.get("worker_tasks", []) if t["id"] not in already_dispatched]
        if not batch:
            return END
        attempts = state.get("attempts", {})
        return [
            Send("worker", {"task": task, "attempt": attempts.get(task["id"], 0) + 1})
            for task in batch
        ]

    builder = StateGraph(OrchestratorState)
    builder.add_node("manager", manager)
    builder.add_node("worker", worker)
    builder.add_edge(START, "manager")
    builder.add_conditional_edges("manager", dispatch, ["worker", END])
    builder.add_edge("worker", "manager")
    return builder.compile()
