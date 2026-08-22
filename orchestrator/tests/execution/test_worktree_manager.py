"""Tests for orchestrator.execution.worktree_manager against a temp git repo."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.execution.worktree_manager import (
    WorktreeManager,
    find_repo_root,
    sanitize_worker_id,
)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
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


def test_commit_and_diff_stat(manager: WorktreeManager) -> None:
    manager.create("w1")
    (manager.worktree_path("w1") / "feature.py").write_text("x = 1\n", encoding="utf-8")
    assert manager.commit("w1", "add feature") is True
    stat = manager.diff_stat("w1")
    assert "feature.py" in stat

    assert manager.commit("w1", "nothing new") is False


def test_merge_brings_changes_into_repo(manager: WorktreeManager, git_repo: Path) -> None:
    manager.create("w2")
    (manager.worktree_path("w2") / "merged.txt").write_text("data\n", encoding="utf-8")
    manager.commit("w2", "add merged")
    manager.merge("w2")
    assert (git_repo / "merged.txt").read_text(encoding="utf-8") == "data\n"


def test_discard_removes_worktree_and_branch(manager: WorktreeManager, git_repo: Path) -> None:
    manager.create("w3")
    manager.discard("w3")
    assert not manager.worktree_path("w3").exists()
    assert "orchestrator/worker-w3" not in _git(["branch", "--list"], cwd=git_repo)


def test_cleanup_prunes_stale_worktrees(manager: WorktreeManager, git_repo: Path) -> None:
    path = manager.create("w4")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    manager.cleanup()
    assert "worker_w4" not in _git(["worktree", "list"], cwd=git_repo)


def test_find_repo_root(git_repo: Path) -> None:
    nested = git_repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == git_repo.resolve()
    with pytest.raises(RuntimeError):
        find_repo_root(nested.parents[1] / ".." / "..")
