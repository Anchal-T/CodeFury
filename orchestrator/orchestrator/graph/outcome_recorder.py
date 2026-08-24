"""Shared finalization mechanics for hierarchy levels (deepening step 1).

Every level finishes the same way: flip the Task to a terminal status,
persist one aggregate Report, optionally run the integration gate against
the assembled repo root, and optionally append a knowledge-doc section.
The divergences between levels (status vocabulary, whether the gate and
docs apply) are parameters here — not copies of the same code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.contracts import Report, Task
from orchestrator.graph.worker import run_tests
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

#: Canonical blocker appended when the post-merge integration gate fails.
#: Workers test in isolation, so individually passing branches can still
#: combine into a broken integrated tree.
INTEGRATION_FAILED_BLOCKER = "integration tests failed after merge"

#: Cap on integration output kept in the Report summary.
_SUMMARY_TAIL_CHARS = 500


@dataclass
class IntegrationGate:
    """Post-merge test run configuration (repo root + command)."""

    repo_root: Path
    test_command: list[str]


@dataclass
class RecordRequest:
    """Everything the level decided about its own outcome."""

    ok: bool
    blockers: list[str]
    summary: str
    tokens_used: int = 0
    knowledge_path: Path | None = None
    knowledge_title: str = ""
    knowledge_body: str = ""


class OutcomeRecorder:
    """Persists one level task's terminal outcome behind a single method.

    Owns: the status transition, the aggregate Report row, the integration
    gate (when configured), and the knowledge-doc append (when both the
    docs sink and a knowledge target are provided).
    """

    def __init__(
        self,
        store: StateStore,
        *,
        agent_role: str,
        ok_status: str = "review",
        integration: IntegrationGate | None = None,
        knowledge: KnowledgeDocs | None = None,
    ) -> None:
        self.store = store
        self.agent_role = agent_role
        self.ok_status = ok_status
        self.integration = integration
        self.knowledge = knowledge

    def record(self, task: Task, request: RecordRequest) -> Report:
        ok = request.ok
        blockers = list(request.blockers)
        summary = request.summary
        if ok and self.integration is not None:
            result = run_tests(
                self.integration.test_command, cwd=self.integration.repo_root
            )
            if not result.passed:
                ok = False
                blockers.append(INTEGRATION_FAILED_BLOCKER)
                summary = (
                    f"{summary}; integration test output tail:\n"
                    f"{result.output[-_SUMMARY_TAIL_CHARS:]}"
                )
        task.status = self.ok_status if ok else "failed"
        self.store.save_task(task)
        report = Report(
            task_id=task.id,
            agent=f"{self.agent_role}:{task.id}",
            summary=summary,
            diff_ref=None,
            tests_passed=ok,
            tokens_used=request.tokens_used,
            blockers=blockers,
        )
        self.store.save_report(report)
        if self.knowledge is not None and request.knowledge_path is not None:
            self.knowledge.append_section(
                request.knowledge_path,
                title=request.knowledge_title,
                body=request.knowledge_body,
            )
        return report
