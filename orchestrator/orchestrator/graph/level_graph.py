"""LevelGraph: one deep module for the hierarchy-level lifecycle.

Every automated level (Manager, Domain Lead) repeats the same skeleton:
plan (decompose once, persist children) → dispatch (LangGraph Send with
attempt counting and dedupe) → review (act on reports/results) → finalize.
This module owns that skeleton; levels differ only through injected parts:

- ``decomposer``: turns the level task into child Tasks.
- ``reviewer``: decision function over the current state returning a
  RoundDecision whose DispatchSpec entries name which registered node to
  Send to; it also owns the terminal transition when done=True.
- ``nodes``: node-name → async callable, registered before wiring.

State schemas stay explicit (see ADR 0001): callers pass their own
TypedDict so subgraph channel drop-list semantics remain visible code.

Dispatch model: the level node never Sends directly. It records
``pending_sends`` in state and a conditional edge turns them into Send
objects — mirroring build_graph's manager→dispatch split so parallel fan-out
keeps LangGraph's concurrent-write rules (workers write only additive
channels: reports/attempts/dispatched).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send

from orchestrator.contracts import Task
from orchestrator.memory.store import StateStore


@dataclass
class DispatchSpec:
    """One Send instruction: target node + serialized Task payload."""

    node: str
    task: dict


@dataclass
class RoundDecision:
    """What a reviewer decided about the current round."""

    dispatches: list[DispatchSpec] = field(default_factory=list)
    done: bool = False
    blockers: list[str] = field(default_factory=list)
    outcome: str | None = None   # terminal status when done ("review"/"failed")


Reviewer = Callable[[dict], RoundDecision]


def build_level_graph(
    *,
    store: StateStore,
    role: str,
    task_key: str,
    state_schema: type,
    decomposer,
    reviewer: Reviewer,
    nodes: dict[str, Callable[[dict], Awaitable[dict]]],
    empty_blocker: str,
):
    """Assemble and compile one level's StateGraph.

    ``task_key`` is the state channel carrying this level's own serialized
    Task (e.g. "manager_task", "lead_task"); ``empty_blocker`` names the
    failure when decomposition yields nothing. The reviewer is called every
    review round with the full state; when it returns done=True the graph
    ends with its outcome.
    """
    # ``role`` names this level's own node; other entries are dispatch targets.

    def _recover_parent(state: dict) -> dict:
        """After Send rounds the level task channel is gone from state; reload
        it via any known child's parent_id (children persist at plan time)."""
        candidates: list[str] = []
        for source in (state.get("child_tasks"), state.get("reports"), state.get("dispatched")):
            for item in source or []:
                if isinstance(item, str):
                    candidates.append(item)
                else:
                    candidates.append(item.get("parent_id") or item.get("id"))
        for candidate in candidates:
            if not candidate:
                continue
            task = store.get_task(candidate)
            if task is None:
                continue
            if task.parent_id is not None:
                parent = store.get_task(task.parent_id)
                if parent is not None:
                    return parent.model_dump()
            return task.model_dump()
        raise KeyError(f"cannot recover level task '{task_key}' from state")

    async def level_node(state: dict) -> dict:
        parent = Task.model_validate(
            state.get(task_key) or _recover_parent(state)
        )

        # Plan mode: decompose once, persist children, request first dispatches.
        if not state.get("child_tasks") and not state.get("dispatched"):
            parent.status = "in_progress"
            store.save_task(parent)
            children = decomposer.decompose(parent)
            if not children:
                parent.status = "failed"
                store.save_task(parent)
                return {
                    "final_status": "failed",
                    "blockers": [empty_blocker],
                    "outcome": "failed",
                }
            for child in children:
                store.save_task(child)
            attempts = state.get("attempts") or {}
            update = {"child_tasks": [child.model_dump() for child in children]}
            decision = reviewer(state)
            update["blockers"] = decision.blockers
            if decision.done:
                update["outcome"] = decision.outcome
                update["final_status"] = decision.outcome
                return update
            update["pending_sends"] = [
                {
                    "node": spec.node,
                    "payload": {
                        "task": spec.task,
                        "attempt": attempts.get(str(spec.task["id"]), 0) + 1,
                    },
                }
                for spec in decision.dispatches
            ]
            return update

        # Review mode: the reviewer decides dispatches or termination.
        decision = reviewer(state)
        update: dict = {"blockers": decision.blockers, "pending_sends": []}
        if decision.done:
            update["outcome"] = decision.outcome
            update["final_status"] = decision.outcome
            return update
        attempts = state.get("attempts") or {}
        already = set(state.get("dispatched") or [])
        reported_ids = {r["task_id"] for r in state.get("reports") or []}
        # A dispatch is fresh (sendable) when never dispatched, or when its
        # report came back — that's a retry, the reviewer's call.
        # Dispatched-but-unreported tasks must NOT be re-sent: doing so would
        # double-run them and loop forever.
        fresh = [
            spec for spec in decision.dispatches
            if str(spec.task["id"]) not in already
            or str(spec.task["id"]) in reported_ids
        ]
        if not fresh:
            # Reviewer wants work that's already dispatched and reported.
            # Re-sending would loop forever (nothing new can re-enter
            # review), so treat this as termination with the reviewer's
            # outcome — the only convergent exit here.
            update["outcome"] = decision.outcome or "review"
            update["final_status"] = update["outcome"]
            return update
        update["pending_sends"] = [
            {
                "node": spec.node,
                "payload": {
                    "task": spec.task,
                    "attempt": attempts.get(str(spec.task["id"]), 0) + 1,
                },
            }
            for spec in fresh
        ]
        return update

    def route(state: dict):
        """Turn pending_sends into Send objects; END once decided."""
        if state.get("outcome"):
            return END
        sends = state.get("pending_sends") or []
        if not sends:
            return END
        return [
            Send(spec["node"], spec["payload"])
            for spec in sends
        ]

    builder = StateGraph(state_schema)
    for name, node_fn in nodes.items():
        if name != role:
            builder.add_node(name, node_fn)
    builder.add_node(role, level_node)
    builder.add_edge(START, role)
    builder.add_conditional_edges(role, route, [*nodes.keys(), END])
    for name in nodes:
        if name != role:
            builder.add_edge(name, role)
    return builder.compile()
