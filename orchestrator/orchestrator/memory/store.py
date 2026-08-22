"""SQLite state store (plan §3.2): one file, WAL mode, no server.

Tables: tasks, reports, agents, checkpoints, token_usage. Also provides the
LangGraph SqliteSaver checkpointer for pause/resume.
"""

from pathlib import Path

from orchestrator.contracts import Report, Task


class StateStore:
    """Owns all SQLite persistence for the orchestrator."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def init_schema(self) -> None:
        """Create tables and switch the database to WAL mode."""
        raise NotImplementedError("Phase 1")

    def save_task(self, task: Task) -> None:
        raise NotImplementedError("Phase 1")

    def save_report(self, report: Report) -> None:
        raise NotImplementedError("Phase 1")

    def checkpointer(self):
        """Return a LangGraph SqliteSaver bound to the same db file."""
        raise NotImplementedError("Phase 5")
