"""Git worktree isolation for workers (plan §3.4).

Each worker gets ``workspaces/worker_<id>/`` on its own branch; the pipeline
commits the worker's changes there, and a Manager (Phase 2) reviews the diff
and merges (``git merge --no-ff``) or discards. All git calls are list-form
subprocess invocations with shell=False so this works on Linux and Windows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_BRANCH_PREFIX = "orchestrator/worker-"
# Characters that are invalid in Windows paths or problematic in branch names.
_INVALID_CHARS = '<>:"\\|?*'


def sanitize_worker_id(worker_id: str) -> str:
    """Make a worker id safe as a path segment and branch name on both OSes."""
    cleaned = "".join("-" if ch in _INVALID_CHARS else ch for ch in worker_id)
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in cleaned) or "unknown"


def find_repo_root(start: Path) -> Path:
    """Walk up from start to the enclosing git repository root."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"no git repository root found above {start}")


class WorktreeManager:
    """Creates, commits, merges, and cleans up per-worker git worktrees."""

    def __init__(self, repo_root: Path, workspaces_dir: Path) -> None:
        self.repo_root = repo_root
        self.workspaces_dir = workspaces_dir

    def _git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
        )

    def _require(self, proc: subprocess.CompletedProcess[str], action: str) -> str:
        if proc.returncode != 0:
            raise RuntimeError(f"git {action} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def branch_name(self, worker_id: str) -> str:
        return f"{_BRANCH_PREFIX}{sanitize_worker_id(worker_id)}"

    def worktree_path(self, worker_id: str) -> Path:
        return self.workspaces_dir / f"worker_{sanitize_worker_id(worker_id)}"

    def create(self, worker_id: str) -> Path:
        """Add a worktree on its own branch; return its path."""
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        path = self.worktree_path(worker_id)
        proc = self._git(
            ["worktree", "add", str(path), "-b", self.branch_name(worker_id)],
            cwd=self.repo_root,
        )
        self._require(proc, f"worktree add {path}")
        return path

    def commit(self, worker_id: str, message: str) -> bool:
        """Stage and commit all changes in the worker's worktree.

        Returns True when a commit was created, False when there was nothing
        to commit.
        """
        path = self.worktree_path(worker_id)
        self._require(self._git(["add", "-A"], cwd=path), "add")
        proc = self._git(["commit", "-m", message], cwd=path)
        if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
            raise RuntimeError(f"git commit failed: {(proc.stderr or proc.stdout).strip()}")
        return proc.returncode == 0

    def diff_stat(self, worker_id: str) -> str:
        """One-line-per-file diff summary of the worker branch vs its base."""
        return self._require(
            self._git(["diff", "--stat", "HEAD~1..HEAD"], cwd=self.worktree_path(worker_id)),
            "diff --stat",
        ).strip()

    def merge(self, worker_id: str) -> None:
        """Merge the worker's branch into the current branch of repo_root with --no-ff."""
        self._require(
            self._git(["merge", "--no-ff", self.branch_name(worker_id), "-m", f"merge(worker): {worker_id}"], cwd=self.repo_root),
            "merge",
        )

    def discard(self, worker_id: str) -> None:
        """Drop the worker's worktree and branch."""
        self._require(
            self._git(["worktree", "remove", "--force", str(self.worktree_path(worker_id))], cwd=self.repo_root),
            "worktree remove",
        )
        self._require(
            self._git(["branch", "-D", self.branch_name(worker_id)], cwd=self.repo_root),
            "branch -D",
        )

    def cleanup(self) -> None:
        """Prune stale worktree metadata under workspaces_dir."""
        self._require(self._git(["worktree", "prune"], cwd=self.repo_root), "worktree prune")
