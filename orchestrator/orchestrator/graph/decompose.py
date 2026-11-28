"""Task decomposition for the Manager (Level 1) and Domain Lead (Level 2).

The Decomposer protocol is the seam where LLM-backed decomposers plug in
once a headless coding-agent CLI is available; the Static* implementations
are deterministic and used by tests and demos. Level-specific protocols
(DomainDecomposer, EpicDecomposer) keep call-site signatures self-documenting
while sharing the single decompose shape.

``context`` (Phase 5 planning seam): callers pass the latest knowledge
section — domain repo_map.md for leads, PROJECT_STATE.md for epics — so
LLM-backed decomposers can plan with current project knowledge. The
deterministic fakes accept and ignore it.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from orchestrator.contracts import Task


class Decomposer(Protocol):
    """Turns a Task into its child Tasks, one level down."""

    def decompose(self, task: Task, context: str = "") -> list[Task]: ...


class DomainDecomposer(Decomposer, Protocol):
    """Turns a domain-lead Task into its level-1 manager Tasks."""


def _children(
    task: Task,
    *,
    level: int,
    goals: list[str],
    status: str = "pending",
    domains: list[str] | None = None,
) -> list[Task]:
    domains = domains or [task.domain] * len(goals)
    return [
        Task(
            id=f"{task.id}-{index + 1}-{uuid4().hex[:6]}",
            parent_id=task.id,
            level=level,
            goal=goal,
            deliverable=goal,
            dependencies=[],
            status=status,  # type: ignore[arg-type]
            assigned_to=None,
            domain=domain,
        )
        for index, (goal, domain) in enumerate(zip(goals, domains))
    ]


class StaticDecomposer:
    """Decomposes a task into one child task per explicit sub-goal.

    ``child_level`` pins the level of the produced children: 0 = workers
    (manager input), 1 = managers (domain-lead input). The level-specific
    names below stay distinct so call sites, tests, and fakes read as their
    own level; they share this one implementation.
    """

    child_level = 0

    def __init__(self, sub_goals: list[str]) -> None:
        self.sub_goals = list(sub_goals)

    def decompose(self, task: Task, context: str = "") -> list[Task]:
        return _children(task, level=self.child_level, goals=self.sub_goals)


class StaticDomainDecomposer(StaticDecomposer):
    """Level-1 StaticDecomposer: children are manager Tasks (lead input)."""

    child_level = 1

    def __init__(self, manager_goals: list[str]) -> None:
        super().__init__(sub_goals=list(manager_goals))


class SingleWorkerDecomposer:
    """Default manager-side decomposition inside a lead run: one worker
    carrying the manager's own goal (deterministic; swap in an LLM-backed
    Decomposer per manager when one is available)."""

    def decompose(self, task: Task, context: str = "") -> list[Task]:
        return StaticDecomposer([task.goal]).decompose(task, context=context)


class SingleManagerDecomposer:
    """Default lead-side decomposition inside an architect run: one manager
    carrying the lead's own goal (deterministic; swap in an LLM-backed
    DomainDecomposer per lead when one is available)."""

    def decompose(self, task: Task, context: str = "") -> list[Task]:
        return StaticDomainDecomposer([task.goal]).decompose(task, context=context)


class EpicDecomposer(Decomposer, Protocol):
    """Turns an epic Task into its level-2 domain-lead Tasks."""


class StaticEpicDecomposer:
    """Decomposes an epic into one lead task per (goal, domain) pair.

    Children are created in ``pending_approval`` — the Architect's plan is
    inert until a human approves each domain via the CLI (plan §9 Phase 4).
    """

    def __init__(self, domain_goals: list[tuple[str, str]]) -> None:
        for goal, domain in domain_goals:
            if not domain:
                raise ValueError(f"epic entry {goal!r} is missing a domain")
        self.domain_goals = list(domain_goals)

    def decompose(self, task: Task, context: str = "") -> list[Task]:
        goals = [goal for goal, _domain in self.domain_goals]
        domains = [domain for _goal, domain in self.domain_goals]
        return _children(task, level=2, goals=goals, status="pending_approval", domains=domains)
