"""The eval replayer (Phase 12): re-dispatches level-0 tasks from a past run
through the *current* pipeline — current prompts, harness, knowledge, critic —
into an isolated eval workspace.

Replays never merge branches and never touch the source run's databases or
worker branches: eval worktrees live in their own directory and their git
branches are namespaced (``orchestrator/eval-<task id>``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.contracts import Report, Task
from orchestrator.execution.runner import Runner
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.graph.worker import run_worker_task
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

if TYPE_CHECKING:
    from orchestrator.config import CriticConfig


@dataclass
class ReplayResult:
    """Outcome of replaying one source task."""

    task_id: str
    report: Report | None = None
    error: str | None = None


class EvalRunner:
    """Replays selected tasks through the current pipeline."""

    def __init__(
        self,
        *,
        store: StateStore,
        worktrees: WorktreeManager,
        runner: Runner,
        test_command: list[str],
        knowledge: KnowledgeDocs | None = None,
        domains_dir: Path | None = None,
        critic: "CriticConfig | None" = None,
        keep_worktrees: bool = False,
    ) -> None:
        self.store = store
        self.worktrees = worktrees
        self.runner = runner
        self.test_command = test_command
        self.knowledge = knowledge
        self.domains_dir = domains_dir
        self.critic = critic
        self.keep_worktrees = keep_worktrees

    def run(self, tasks: list[Task]) -> list[ReplayResult]:
        """Replay each task sequentially; one bad replay never stops the rest."""
        return [self._replay_one(task) for task in tasks]

    def _replay_one(self, source_task: Task) -> ReplayResult:
        task = source_task.model_copy(update={"status": "pending"})
        try:
            report = run_worker_task(
                task,
                store=self.store,
                worktrees=self.worktrees,
                runner=self.runner,
                test_command=self.test_command,
                knowledge_context=self._context_for(task),
                critic=self.critic,
            )
        except Exception as exc:  # noqa: BLE001 — a replay crash is data, not a stop
            return ReplayResult(task_id=source_task.id, error=f"pipeline crashed: {exc!r}")
        finally:
            if not self.keep_worktrees:
                self.worktrees.remove(task.id)
        return ReplayResult(task_id=source_task.id, report=report)

    def _context_for(self, task: Task) -> str:
        if self.knowledge is None or self.domains_dir is None or not task.domain:
            return ""
        return self.knowledge.read_latest(self.domains_dir / task.domain / "repo_map.md")
