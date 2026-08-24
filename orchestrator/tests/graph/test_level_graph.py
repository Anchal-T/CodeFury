"""Parametrized lifecycle tests at the LevelGraph interface.

One suite over the shared skeleton (empty decomposition, retry-cap
termination, dispatch dedupe, single terminal outcome) instead of
near-copies per level. Uses stub reviewer/decomposer/nodes — no subprocess,
no git — so these run in milliseconds; the fake_worker E2E suites remain
the true-wiring regression harness.
"""

import asyncio
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.graph.level_graph import (
    DispatchSpec,
    RoundDecision,
    build_level_graph,
)
from orchestrator.memory.store import StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_parent(task_id: str = "lvl-1") -> Task:
    return Task(
        id=task_id, parent_id=None, level=1, goal="g", deliverable="d",
        dependencies=[], status="pending", assigned_to=None,
    )


def child(parent_id: str, n: int) -> Task:
    return Task(
        id=f"{parent_id}-c{n}", parent_id=parent_id, level=0,
        goal=f"goal {n}", deliverable="d", dependencies=[],
        status="pending", assigned_to=None,
    )


class StubWorker:
    """Fake 'worker' node: reports success for every dispatched task.

    Writes only additive channels — mirrors make_worker_node, whose outputs
    never collide across concurrent Sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    async def __call__(self, state: dict) -> dict:
        task_id = str(state["task"]["id"])
        self.sent.append((task_id, int(state.get("attempt", 1))))
        report = {
            "task_id": task_id, "agent": f"worker:{task_id}", "summary": "ok",
            "tests_passed": True, "tokens_used": 1, "blockers": [],
        }
        return {
            "reports": [report],
            "attempts": {task_id: int(state.get("attempt", 1))},
            "dispatched": [task_id],
        }


def make_level(store, decomposer, reviewer, worker=None):
    worker = worker or StubWorker()
    graph = build_level_graph(
        store=store,
        role="manager",
        task_key="manager_task",
        state_schema=dict,
        decomposer=decomposer,
        reviewer=reviewer,
        nodes={"worker": worker},
        empty_blocker="manager produced no sub-tasks",
    )
    return graph, worker


def run(graph, parent: Task) -> dict:
    return asyncio.run(
        asyncio.wait_for(
            graph.ainvoke(
                {"manager_task": parent.model_dump()},
                config={"recursion_limit": 50},
            ),
            timeout=30.0,
        )
    )


def test_empty_decomposition_fails_fast_without_dispatch(store) -> None:
    reviewer_calls: list[dict] = []

    class NoChildren:
        def decompose(self, task):
            return []

    def reviewer(state):
        reviewer_calls.append(state)
        return RoundDecision(done=True, outcome="failed")

    graph, worker = make_level(store, NoChildren(), reviewer)
    result = run(graph, make_parent())

    assert result["outcome"] == "failed"
    assert result["final_status"] == "failed"
    assert any("no sub-tasks" in b for b in result["blockers"])
    assert worker.sent == [], "nothing may be dispatched for an empty plan"
    assert reviewer_calls == [], "reviewer must not run when planning already failed"
    assert store.get_task("lvl-1").status == "failed"


def test_retry_cap_terminates_with_failed_parent(store) -> None:
    tasks = [child("lvl-1", 1)]

    class OneChild:
        def decompose(self, task):
            return [child("lvl-1", 1)]

    MAX_ATTEMPTS = 3

    def reviewer(state):
        attempts = state.get("attempts") or {}
        tid = tasks[0].id
        if attempts.get(tid, 0) >= MAX_ATTEMPTS:
            return RoundDecision(done=True, outcome="failed",
                                 blockers=[f"worker {tid} exhausted retries"])
        return RoundDecision(
            dispatches=[DispatchSpec(node="worker", task=tasks[0].model_dump())]
        )

    graph, worker = make_level(store, OneChild(), reviewer)
    result = run(graph, make_parent())

    assert result["outcome"] == "failed"
    assert [a for _, a in worker.sent] == [1, 2, 3], "exactly max attempts, then STOP"
    assert any("exhausted" in b for b in result["blockers"])
    assert store.get_task("lvl-1").status == "in_progress"  # parent stays non-terminal here; recorder owns the flip


def test_dispatch_dedupe_never_sends_same_child_twice_in_one_round(store) -> None:
    """With a single-dispatch-per-round schema (plain dict, no additive
    reducers), the module must still converge: each child dispatched once,
    then the reviewer terminates."""
    class TwoChildren:
        def __init__(self) -> None:
            self.planned = False

        def decompose(self, task):
            if self.planned:
                return []
            self.planned = True
            return [child("lvl-1", 1), child("lvl-1", 2)]

    dispatched: set[str] = set()

    def reviewer(state):
        nonlocal dispatched
        reports = state.get("reports") or []
        reported = {r["task_id"] for r in reports}
        if len(reported) >= 2:
            return RoundDecision(done=True, outcome="review")
        # Dispatch the first child this reviewer hasn't sent yet (dict
        # schema: one Send per round). Track sends locally — a reported
        # child must not be re-picked, or the run never converges.
        for k in (1, 2):
            tid = f"lvl-1-c{k}"
            if tid not in dispatched:
                dispatched.add(tid)
                return RoundDecision(
                    dispatches=[DispatchSpec(node="worker", task=child("lvl-1", k).model_dump())]
                )
        # All dispatched; waiting on the remaining report. done=True ends the
        # run here — with a plain-dict schema there is no way to "wait" for a
        # specific report without re-entering review on it.
        return RoundDecision(done=True, outcome="review")

    graph, worker = make_level(store, TwoChildren(), reviewer)
    result = run(graph, make_parent())

    assert result["outcome"] == "review"
    ids = [tid for tid, _ in worker.sent]
    assert sorted(set(ids)) == ["lvl-1-c1", "lvl-1-c2"], (
        "each child dispatched exactly once — no re-dispatch loop"
    )
    assert len(ids) <= 4, f"no runaway loop (sent {ids})"


def test_attempt_counting_is_owned_by_the_module(store) -> None:
    """The Send payload's attempt counter increments per round without the
    reviewer tracking it — LevelGraph owns that bookkeeping."""
    seen_attempts: list[int] = []

    class OneChild:
        def decompose(self, task):
            return [child("lvl-1", 1)]

    def reviewer(state):
        attempts = state.get("attempts") or {}
        seen_attempts.append(attempts.get("lvl-1-c1", 0))
        if attempts.get("lvl-1-c1", 0) >= 2:
            return RoundDecision(done=True, outcome="review")
        return RoundDecision(
            dispatches=[DispatchSpec(node="worker", task=child("lvl-1", 1).model_dump())]
        )

    graph, _worker = make_level(store, OneChild(), reviewer)
    run(graph, make_parent())

    assert seen_attempts == [0, 1, 2], "attempts must grow across review rounds"
