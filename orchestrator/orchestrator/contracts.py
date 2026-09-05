"""Data contracts (plan §6): the only objects that cross hierarchy levels.

Never pass raw chat transcripts between levels — Task/Report objects keep
token cost predictable as the agent tree grows.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: Blocker-string convention marking a merge conflict; the Domain Lead keys
#: reconciliation off this prefix (manager writes it, lead matches it).
CONFLICT_BLOCKER_PREFIX = "merge conflict"


class Task(BaseModel):
    id: str
    parent_id: str | None
    level: Literal[0, 1, 2, 3]
    goal: str
    deliverable: str
    dependencies: list[str] = Field(default_factory=list)
    status: Literal["pending", "pending_approval", "in_progress", "review", "done", "failed"]
    assigned_to: str | None = None
    domain: str | None = None


class Report(BaseModel):
    task_id: str
    agent: str
    summary: str
    diff_ref: str | None = None
    tests_passed: bool
    tokens_used: int
    blockers: list[str] = Field(default_factory=list)
    #: Non-gating quality notes from the deterministic critic (Phase 11);
    #: strict manager mode may promote them to blockers. Old rows lack the
    #: key — the default keeps them readable.
    warnings: list[str] = Field(default_factory=list)
