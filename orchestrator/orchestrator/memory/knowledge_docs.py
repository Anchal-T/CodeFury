"""Tier-1 markdown knowledge files (plan §3.5): repo_map.md, decisions.md.

Append-only, timestamped sections under domains/; git-tracked so knowledge
survives even if orchestrator.db is deleted. Existing bytes are never
rewritten — new sections are appended after a blank-line separator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class KnowledgeDocs:
    """Reads and appends to markdown knowledge documents."""

    def read_latest(self, doc_path: Path) -> str:
        """Return the current contents of a knowledge doc ('' if missing)."""
        if not doc_path.is_file():
            return ""
        return doc_path.read_text(encoding="utf-8")

    def append_section(self, doc_path: Path, title: str, body: str) -> None:
        """Append one timestamped section; never rewrite existing history."""
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
        existing = self.read_latest(doc_path)
        with doc_path.open("a", encoding="utf-8") as handle:
            if existing:
                if not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("\n")
            handle.write(f"## {title} — {timestamp}\n\n{body.strip()}\n")
