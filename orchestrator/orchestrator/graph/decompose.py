"""Task decomposition for the Manager (Level 1) and Domain Lead (Level 2).

The Decomposer protocols are the seams where LLM-backed decomposers plug in
once a headless coding-agent CLI is available; the Static* implementations
are deterministic and used by tests and demos.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from orchestrator.contracts import Task


class Decomposer(Protocol):
    """Turns a manager Task into its level-0 worker Tasks."""

    def decompose(self, task: Task) -> list[Task]: ...


class DomainDecomposer(Protocol):
    """Turns a domain-lead Task into its level-1 manager Tasks."""

    def decompose(self, task: Task) -> list[Task]: ...


def is_decomposer(obj: object) -> bool:
    """Runtime check for either decomposer protocol."""
    return hasattr(obj, "decompose") and callable(obj.decompose)


def _children(task: Task, *, level: int, goals: list[str]) -> list[Task]:
    return [
        Task(
            id=f"{task.id}-{index + 1}-{uuid4().hex[:6]}",
            parent_id=task.id,
            level=level,
            goal=goal,
            deliverable=goal,
            dependencies=[],
            status="pending",
            assigned_to=None,
            domain=task.domain,
        )
        for index, goal in enumerate(goals)
    ]


class StaticDecomposer:
    """Decomposes a task into one worker task per explicit sub-goal."""

    def __init__(self, sub_goals: list[str]) -> None:
        self.sub_goals = list(sub_goals)

    def decompose(self, task: Task) -> list[Task]:
        return _children(task, level=0, goals=self.sub_goals)


class StaticDomainDecomposer:
    """Decomposes a lead task into one manager task per explicit goal."""

    def __init__(self, manager_goals: list[str]) -> None:
        self.manager_goals = list(manager_goals)

    def decompose(self, task: Task) -> list[Task]:
        return _children(task, level=1, goals=self.manager_goals)


class SingleWorkerDecomposer:
    """Default manager-side decomposition inside a lead run: one worker
    carrying the manager's own goal (deterministic; swap in an LLM-backed
    Decomposer per manager when one is available)."""

    def decompose(self, task: Task) -> list[Task]:
        return StaticDecomposer([task.goal]).decompose(task)
