"""Git worktree isolation for workers (plan §3.4).

Each worker gets workspaces/worker_<id>/ on its own branch; the Manager
reviews the diff and merges (git merge --no-ff) or discards. Branch names
must avoid characters invalid in Windows paths.
"""

from pathlib import Path


class WorktreeManager:
    """Creates, merges, and cleans up per-worker git worktrees."""

    def create(self, worker_id: str, workspaces_dir: Path) -> Path:
        """Add a worktree on its own branch; return its path."""
        raise NotImplementedError("Phase 1")

    def merge(self, worker_id: str) -> None:
        """Merge the worker's branch back with --no-ff."""
        raise NotImplementedError("Phase 1")

    def discard(self, worker_id: str) -> None:
        """Drop the worker's branch and remove its worktree."""
        raise NotImplementedError("Phase 1")

    def cleanup(self, workspaces_dir: Path) -> None:
        """Prune all stale worktrees under workspaces_dir."""
        raise NotImplementedError("Phase 1")
