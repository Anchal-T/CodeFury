"""Tests for the Manager review logic (plan §9 Phase 2): merge gating,
retry decisions, conflict handling, and parent finalization."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.manager import review_reports, finalize_parent
from orchestrator.memory.store import StateStore


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
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_worker_task(task_id: str = "w-1", status: str = "review") -> Task:
    return Task(
        id=task_id,
        parent_id="m-1",
        level=0,
        goal=f"goal for {task_id}",
        deliverable="a file",
        dependencies=[],
        status=status,
        assigned_to=None,
    )


def make_report(task_id: str = "w-1", passed: bool = True) -> Report:
    return Report(
        task_id=task_id,
        agent=f"worker:{task_id}",
        summary="done" if passed else "failed",
        diff_ref=f"orchestrator/worker-{task_id}",
        tests_passed=passed,
        tokens_used=10,
        blockers=[] if passed else ["tests failed in worktree"],
    )


@pytest.fixture()
def deps(git_repo: Path):
    worktrees = WorktreeManager(git_repo, git_repo / "workspaces")
    policy = RetryPolicy(max_worker_retries=2, max_manager_escalations=1)
    return worktrees, policy


def test_passed_report_merges_branch(deps, store: StateStore, git_repo: Path) -> None:
    worktrees, policy = deps
    task = make_worker_task("w-1")
    store.save_task(task)
    worktree = worktrees.create("w-1")
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    worktrees.commit("w-1", "worker change")

    decision = review_reports(
        reports=[make_report("w-1", passed=True)],
        worker_tasks=[task],
        attempts={"w-1": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.merged == ["w-1"]
    assert decision.retry == []
    assert decision.failed == []
    assert decision.all_done
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
    assert store.get_task("w-1").status == "done"


def test_failed_report_within_budget_is_retried(deps, store: StateStore) -> None:
    worktrees, policy = deps
    task = make_worker_task("w-1", status="failed")
    store.save_task(task)

    decision = review_reports(
        reports=[make_report("w-1", passed=False)],
        worker_tasks=[task],
        attempts={"w-1": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.merged == []
    assert [t["id"] for t in decision.retry] == ["w-1"]
    assert decision.failed == []
    assert not decision.all_done
    assert store.get_task("w-1").status == "pending"


def test_failed_report_past_cap_fails_for_good(deps, store: StateStore) -> None:
    worktrees, policy = deps
    task = make_worker_task("w-1", status="failed")
    store.save_task(task)

    decision = review_reports(
        reports=[make_report("w-1", passed=False)],
        worker_tasks=[task],
        attempts={"w-1": 2},  # cap of 2 reached: no third attempt
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.retry == []
    assert decision.failed == ["w-1"]
    assert decision.all_done
    assert any("exhausted" in b for b in decision.blockers)
    assert store.get_task("w-1").status == "failed"


def test_merge_conflict_fails_without_retry(deps, store: StateStore, git_repo: Path) -> None:
    worktrees, policy = deps
    task = make_worker_task("w-1")
    store.save_task(task)
    worktree = worktrees.create("w-1")
    (worktree / "conflict.txt").write_text("worker version\n", encoding="utf-8")
    worktrees.commit("w-1", "worker change")

    # Base branch moves the same file elsewhere → merge will conflict.
    (git_repo / "conflict.txt").write_text("base version\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-m", "base change"], cwd=git_repo)

    decision = review_reports(
        reports=[make_report("w-1", passed=True)],
        worker_tasks=[task],
        attempts={"w-1": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.merged == []
    assert decision.retry == []
    assert decision.failed == ["w-1"]
    assert any("merge conflict" in b for b in decision.blockers)
    assert store.get_task("w-1").status == "failed"


def test_missing_report_means_not_done(deps, store: StateStore) -> None:
    worktrees, policy = deps
    task_a = make_worker_task("w-1")
    task_b = make_worker_task("w-2")
    store.save_task(task_a)
    store.save_task(task_b)
    worktree = worktrees.create("w-1")
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    worktrees.commit("w-1", "worker change")

    decision = review_reports(
        reports=[make_report("w-1", passed=True)],
        worker_tasks=[task_a, task_b],
        attempts={"w-1": 1, "w-2": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.merged == ["w-1"]
    assert not decision.all_done


def test_finalize_parent_success_and_failure(store: StateStore) -> None:
    parent = Task(
        id="m-1", parent_id=None, level=1, goal="g", deliverable="d",
        dependencies=[], status="in_progress", assigned_to=None,
    )
    store.save_task(parent)
    reports = [make_report("w-1", passed=True), make_report("w-2", passed=True)]

    from orchestrator.graph.manager import ReviewDecision

    ok_decision = ReviewDecision(
        merged=["w-1", "w-2"], retry=[], failed=[], blockers=[], all_done=True,
    )
    report = finalize_parent(parent=parent, decision=ok_decision, reports=reports, store=store)
    assert report.tests_passed
    assert store.get_task("m-1").status == "review"
    assert store.latest_report("m-1").agent == "manager:m-1"

    bad_decision = ReviewDecision(
        merged=["w-1"], retry=[], failed=["w-2"],
        blockers=["worker w-2 exhausted retries"], all_done=True,
    )
    report = finalize_parent(parent=parent, decision=bad_decision, reports=reports, store=store)
    assert not report.tests_passed
    assert report.blockers == ["worker w-2 exhausted retries"]
    assert store.get_task("m-1").status == "failed"
