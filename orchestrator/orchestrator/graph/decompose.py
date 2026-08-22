"""Task decomposition for the Manager (plan §2).

The Decomposer protocol is the seam where an LLM-backed decomposer plugs in
once a headless coding-agent CLI is available; StaticDecomposer is the
deterministic implementation used by tests and demos.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from orchestrator.contracts import Task


class Decomposer(Protocol):
    """Turns a manager Task into its level-0 worker Tasks."""

    def decompose(self, task: Task) -> list[Task]: ...


def is_decomposer(obj: object) -> bool:
    """Runtime check for the Decomposer protocol."""
    return hasattr(obj, "decompose") and callable(obj.decompose)


class StaticDecomposer:
    """Decomposes a task into one worker task per explicit sub-goal."""

    def __init__(self, sub_goals: list[str]) -> None:
        self.sub_goals = list(sub_goals)

    def decompose(self, task: Task) -> list[Task]:
        children: list[Task] = []
        for index, goal in enumerate(self.sub_goals):
            children.append(
                Task(
                    id=f"{task.id}-{index + 1}-{uuid4().hex[:6]}",
                    parent_id=task.id,
                    level=0,
                    goal=goal,
                    deliverable=goal,
                    dependencies=[],
                    status="pending",
                    assigned_to=None,
                )
            )
        return children
