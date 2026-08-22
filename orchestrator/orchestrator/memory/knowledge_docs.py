"""Tier-1 markdown knowledge files (plan §3.5): repo_map.md, decisions.md.

Append-only, timestamped sections under domains/; git-tracked so knowledge
survives even if orchestrator.db is deleted.
"""

from pathlib import Path


class KnowledgeDocs:
    """Reads and appends to markdown knowledge documents."""

    def read_latest(self, doc_path: Path) -> str:
        """Return the current contents of a knowledge doc."""
        raise NotImplementedError("Phase 5")

    def append_section(self, doc_path: Path, title: str, body: str) -> None:
        """Append a timestamped section; never rewrite history."""
        raise NotImplementedError("Phase 5")
