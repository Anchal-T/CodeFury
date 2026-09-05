"""Tests for orchestrator.execution.worktree_manager against a temp git repo."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import CONFLICT_BLOCKER_PREFIX
from orchestrator.execution.worktree_manager import (
    MergeConflictError,
    WorktreeManager,
    find_repo_root,
    sanitize_worker_id,
)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def git_repo(tmp_path: Path, git_init) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git_init(root)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


@pytest.fixture()
def manager(git_repo: Path) -> WorktreeManager:
    return WorktreeManager(git_repo, git_repo / "workspaces")


def test_sanitize_worker_id_strips_invalid_chars() -> None:
    assert ":" not in sanitize_worker_id("a:b*c?")
    assert sanitize_worker_id("simple-id_1") == "simple-id_1"
    assert sanitize_worker_id("") == "unknown"


def test_create_makes_worktree_and_branch(manager: WorktreeManager, git_repo: Path) -> None:
    path = manager.create("w1")
    assert path.is_dir()
    assert "orchestrator/worker-w1" in _git(["branch", "--list"], cwd=git_repo)
    assert str(path) in _git(["worktree", "list"], cwd=git_repo)


def test_changed_files_lists_worker_changes(
    manager: WorktreeManager, git_repo: Path
) -> None:
    """Phase 11 critic input: the files the worker branch changed relative to
    where it was cut (Phase 11)."""
    path = manager.create("w1")
    (path / "feature.py").write_text("x\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("y\n", encoding="utf-8")
    manager.commit("w1", "worker change")
    assert manager.changed_files("w1") == ["feature.py", "src/app.py"]


def test_changed_files_empty_for_untouched_worktree(manager: WorktreeManager) -> None:
    manager.create("w1")
    assert manager.commit("w1", "nothing to commit") is False
    assert manager.changed_files("w1") == []


def test_changed_files_survives_base_movement(
    manager: WorktreeManager, git_repo: Path
) -> None:
    """Other workers merging while this one runs must not pollute the diff —
    the comparison anchors at the merge-base, not at current HEAD."""
    path = manager.create("w1")
    (path / "worker.txt").write_text("worker\n", encoding="utf-8")
    manager.commit("w1", "worker change")
    (git_repo / "base.txt").write_text("moved on\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-m", "base moves"], cwd=git_repo)
    assert manager.changed_files("w1") == ["worker.txt"]


def test_create_is_idempotent_for_retries(manager: WorktreeManager) -> None:
    """A retried task id must get a fresh worktree, not a collision error."""
    first = manager.create("w5")
    (first / "leftover.txt").write_text("stale attempt\n", encoding="utf-8")
    manager.commit("w5", "first attempt")

    second = manager.create("w5")
    assert second == first
    assert second.is_dir()
    assert not (second / "leftover.txt").exists(), "retry must start from a clean worktree"
    (second / "retry.txt").write_text("fresh\n", encoding="utf-8")
    assert manager.commit("w5", "retry attempt") is True


def test_merge_brings_changes_into_repo(manager: WorktreeManager, git_repo: Path) -> None:
    manager.create("w2")
    (manager.worktree_path("w2") / "merged.txt").write_text("data\n", encoding="utf-8")
    manager.commit("w2", "add merged")
    manager.merge("w2")
    assert (git_repo / "merged.txt").read_text(encoding="utf-8") == "data\n"


def test_failed_merge_aborts_and_leaves_repo_clean(manager: WorktreeManager, git_repo: Path) -> None:
    """A conflicted merge must not leave MERGE_HEAD dangling: the repo stays
    clean so subsequent merges (other workers) still work."""
    manager.create("w6")
    (manager.worktree_path("w6") / "conflict.txt").write_text("worker version\n", encoding="utf-8")
    manager.commit("w6", "worker change")
    (git_repo / "conflict.txt").write_text("base version\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-m", "base change"], cwd=git_repo)

    with pytest.raises(RuntimeError):
        manager.merge("w6")

    status = _git(["status", "--porcelain"], cwd=git_repo)
    assert "UU" not in status, "no unmerged paths may remain"
    assert not (git_repo / ".git" / "MERGE_HEAD").exists()

    # A later worker must still be able to merge cleanly.
    manager.create("w7")
    (manager.worktree_path("w7") / "later.txt").write_text("later\n", encoding="utf-8")
    manager.commit("w7", "later change")
    manager.merge("w7")
    assert (git_repo / "later.txt").read_text(encoding="utf-8") == "later\n"


def test_merge_conflict_raises_typed_error_naming_files(
    manager: WorktreeManager, git_repo: Path
) -> None:
    """The Domain Lead needs the conflicted paths to write the reconciler goal."""
    manager.create("w8")
    (manager.worktree_path("w8") / "conflict.txt").write_text("worker version\n", encoding="utf-8")
    manager.commit("w8", "worker change")
    (git_repo / "conflict.txt").write_text("base version\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-m", "base change"], cwd=git_repo)

    with pytest.raises(MergeConflictError) as excinfo:
        manager.merge("w8")
    assert excinfo.value.conflicted_files == ["conflict.txt"]


def test_conflict_blocker_prefix_is_shared_convention() -> None:
    assert CONFLICT_BLOCKER_PREFIX == "merge conflict"


def test_find_repo_root(git_repo: Path) -> None:
    nested = git_repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == git_repo.resolve()
    outside = (nested.parents[1] / ".." / "..").resolve()
    if any((candidate / ".git").exists() for candidate in (outside, *outside.parents)):
        pytest.skip("temporary directory is nested inside another git repository")
    with pytest.raises(RuntimeError):
        find_repo_root(outside)
