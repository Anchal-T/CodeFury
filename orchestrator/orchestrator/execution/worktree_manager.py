"""Git worktree isolation for workers (plan §3.4).

Each worker gets ``workspaces/worker_<id>/`` on its own branch; the pipeline
commits the worker's changes there, and a Manager (Phase 2) reviews the diff
and merges (``git merge --no-ff``) or leaves the worktree for the next
attempt. All git calls are list-form
subprocess invocations with shell=False so this works on Linux and Windows.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BRANCH_PREFIX = "orchestrator/worker-"
# Characters that are invalid in Windows paths or problematic in branch names.
_INVALID_CHARS = '<>:"\\|?*'
# git stderr lines like: CONFLICT (content): Merge conflict in src/app.py
_CONFLICT_LINE_RE = re.compile(r"^CONFLICT \([^)]+\): (.+)$")
#: A hung git call must not pin a worker-concurrency slot forever.
GIT_TIMEOUT_S = 300.0


class MergeConflictError(RuntimeError):
    """A merge hit conflicts; carries the conflicted file paths for the Lead."""

    def __init__(self, message: str, conflicted_files: list[str]) -> None:
        super().__init__(message)
        self.conflicted_files = conflicted_files


def parse_conflicted_files(stderr: str) -> list[str]:
    """Extract conflicted paths from git merge stderr (deduplicated, sorted)."""
    files: set[str] = set()
    for line in stderr.splitlines():
        match = _CONFLICT_LINE_RE.match(line.strip())
        if not match:
            continue
        detail = match.group(1)
        _, sep, path = detail.rpartition(" in ")
        files.add(path if sep else detail)
    return sorted(files)


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
    """Creates, commits, and merges per-worker git worktrees.

    ``branch_prefix`` namespaces the per-task branches; eval replays (Phase
    12) pass ``orchestrator/eval-`` so replaying a task id never clobbers a
    real run's worker branch.
    """

    def __init__(
        self,
        repo_root: Path,
        workspaces_dir: Path,
        *,
        branch_prefix: str = _BRANCH_PREFIX,
    ) -> None:
        self.repo_root = repo_root
        self.workspaces_dir = workspaces_dir
        self.branch_prefix = branch_prefix

    def _git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                shell=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(
                f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S:.0f}s"
            ) from err

    def _require(self, proc: subprocess.CompletedProcess[str], action: str) -> str:
        if proc.returncode != 0:
            raise RuntimeError(f"git {action} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def branch_name(self, worker_id: str) -> str:
        return f"{self.branch_prefix}{sanitize_worker_id(worker_id)}"

    def changed_files(self, worker_id: str) -> list[str]:
        """Files the worker branch changed relative to where it was cut.

        Anchored at the merge-base, so merges by other workers after this
        branch was cut never pollute the diff. Empty when the branch has no
        commits of its own (nothing was committed).
        """
        branch = self.branch_name(worker_id)
        base = self._require(
            self._git(["merge-base", "HEAD", branch], cwd=self.repo_root),
            "merge-base",
        ).strip()
        if not base:
            return []
        output = self._require(
            self._git(["diff", "--name-only", f"{base}..{branch}"], cwd=self.repo_root),
            "diff --name-only",
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def worktree_path(self, worker_id: str) -> Path:
        return self.workspaces_dir / f"worker_{sanitize_worker_id(worker_id)}"

    def create(self, worker_id: str) -> Path:
        """Add a worktree on its own branch; return its path.

        Idempotent: leftovers from a previous attempt of the same task
        (worktree directory and branch) are cleared first, so a retry
        always starts fresh.
        """
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        path = self.worktree_path(worker_id)
        branch = self.branch_name(worker_id)
        if path.is_dir():
            remove = self._git(["worktree", "remove", "--force", str(path)], cwd=self.repo_root)
            if remove.returncode != 0:
                shutil.rmtree(path, ignore_errors=True)
                if path.exists():
                    logger.warning(
                        "could not clear stale worktree %s: git=%r; manual cleanup may be needed",
                        path,
                        remove.stderr.strip(),
                    )
            self._git(["worktree", "prune"], cwd=self.repo_root)
        listing = self._git(["branch", "--list", branch], cwd=self.repo_root)
        if listing.stdout.strip():
            self._require(self._git(["branch", "-D", branch], cwd=self.repo_root), "branch -D")
        proc = self._git(["worktree", "add", str(path), "-b", branch], cwd=self.repo_root)
        self._require(proc, f"worktree add {path}")
        return path

    def remove(self, worker_id: str) -> None:
        """Best-effort removal of a worktree and its branch (eval cleanup).

        Tolerates an already-gone worktree or branch so replay cleanup never
        turns into its own failure mode.
        """
        path = self.worktree_path(worker_id)
        if self._git(["worktree", "remove", "--force", str(path)], cwd=self.repo_root).returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
        self._git(["worktree", "prune"], cwd=self.repo_root)
        branch = self.branch_name(worker_id)
        if self._git(["branch", "--list", branch], cwd=self.repo_root).stdout.strip():
            self._git(["branch", "-D", branch], cwd=self.repo_root)

    def commit(self, worker_id: str, message: str) -> bool:
        """Stage and commit all changes in the worker's worktree.

        Returns True when a commit was created, False when there was nothing
        to commit. The empty check runs `git status --porcelain`, which is
        locale-independent (unlike matching localized "nothing to commit").
        """
        path = self.worktree_path(worker_id)
        status = self._git(["status", "--porcelain"], cwd=path)
        if not status.stdout.strip():
            return False
        self._require(self._git(["add", "-A"], cwd=path), "add")
        self._require(self._git(["commit", "-m", message], cwd=path), "commit")
        return True

    def merge(self, worker_id: str) -> None:
        """Merge the worker's branch into the current branch of repo_root with --no-ff.

        On failure the merge is aborted so the repo is left clean for the
        next worker's merge; conflicts raise MergeConflictError naming the
        conflicted files (the Domain Lead puts them in the reconciler goal).
        """
        proc = self._git(
            ["merge", "--no-ff", self.branch_name(worker_id), "-m", f"merge(worker): {worker_id}"],
            cwd=self.repo_root,
        )
        if proc.returncode != 0:
            self._git(["merge", "--abort"], cwd=self.repo_root)
            # git writes CONFLICT lines to stdout, fatal errors to stderr.
            output = f"{proc.stdout}\n{proc.stderr}"
            files = parse_conflicted_files(output)
            detail = output.strip()
            if files:
                raise MergeConflictError(
                    f"git merge failed ({proc.returncode}): {detail}", files
                )
            raise RuntimeError(f"git merge failed ({proc.returncode}): {detail}")
