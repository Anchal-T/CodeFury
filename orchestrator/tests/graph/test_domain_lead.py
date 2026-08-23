"""Domain Lead review tests (roadmap Phase 3): reconciliation dispatch,
retry-capped-once conflict handling, and immediate escalation of
non-conflict failures. Uses temp git repos like the manager tests."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import CONFLICT_BLOCKER_PREFIX, Report, Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.graph.domain_lead import lead_review
from orchestrator.memory.store import StateStore


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
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture()
def worktrees(git_repo: Path) -> WorktreeManager:
    return WorktreeManager(git_repo, git_repo / "workspaces")


def make_lead() -> Task:
    return Task(
        id="lead-1",
        parent_id=None,
        level=2,
        goal="ship backend slice",
        deliverable="backend slice merged",
        dependencies=[],
        status="in_progress",
        assigned_to=None,
        domain="backend",
    )


def make_manager_result(
    task_id: str = "mgr-1",
    final_status: str | None = "failed",
    blockers: list[str] | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "final_status": final_status,
        "merged": [],
        "blockers": blockers or [],
        "attempts": {},
    }


def review(store, worktrees, *, lead=None, results=None, reports=None,
           recons=None, attempts=None, max_attempts=1):
    return lead_review(
        lead=lead or make_lead(),
        manager_results=results or [],
        reports=reports or [],
        reconcile_tasks=recons or [],
        reconcile_attempts=attempts or {},
        max_reconcile_attempts=max_attempts,
        worktrees=worktrees,
        store=store,
    )


def test_conflicting_manager_gets_one_reconcile_task(store, worktrees) -> None:
    decision = review(
        store,
        worktrees,
        results=[
            make_manager_result(
                blockers=[f"{CONFLICT_BLOCKER_PREFIX} for w-9: files: shared.txt, other.txt"]
            )
        ],
    )
    assert decision.escalated == []
    assert len(decision.reconcile) == 1
    recon = Task.model_validate(decision.reconcile[0])
    assert recon.level == 0
    assert recon.parent_id == "lead-1"
    assert recon.domain == "backend"
    assert recon.status == "pending"
    assert "shared.txt" in recon.goal and "other.txt" in recon.goal
    assert not decision.all_done
    assert store.get_task(recon.id) is not None


def test_second_conflict_escalates_instead_of_reconciling_again(store, worktrees) -> None:
    manager = Task(
        id="mgr-1", parent_id="lead-1", level=1, goal="g", deliverable="d",
        dependencies=[], status="failed",
    )
    store.save_task(manager)
    decision = review(
        store,
        worktrees,
        results=[
            make_manager_result(blockers=[f"{CONFLICT_BLOCKER_PREFIX} for w-9: files: shared.txt"])
        ],
        attempts={"mgr-1": 1},
    )
    assert decision.reconcile == []
    assert decision.escalated == ["mgr-1"]
    assert any("reconciliation" in b for b in decision.blockers)
    assert store.get_task("mgr-1").status == "failed"


def test_non_conflict_failure_escalates_immediately_without_reconciler(store, worktrees) -> None:
    decision = review(
        store,
        worktrees,
        results=[make_manager_result(blockers=["worker w-2 exhausted retries"])],
    )
    assert decision.reconcile == []
    assert decision.escalated == ["mgr-1"]
    assert any("exhausted retries" in b for b in decision.blockers)


def test_clean_manager_counts_as_done(store, worktrees) -> None:
    decision = review(
        store,
        worktrees,
        results=[make_manager_result(final_status="review", blockers=[])],
    )
    assert decision.done_managers == ["mgr-1"]
    assert decision.all_done


def test_running_manager_keeps_lead_unfinished(store, worktrees) -> None:
    decision = review(store, worktrees, results=[make_manager_result(final_status=None)])
    assert decision.done_managers == []
    assert decision.escalated == []
    assert not decision.all_done


def test_passing_reconcile_report_is_merged_and_done(store, worktrees, git_repo) -> None:
    recon = Task(
        id="mgr-1-reconcile-1", parent_id="lead-1", level=0,
        goal="resolve shared.txt", deliverable="integrated", dependencies=[],
        status="pending", domain="backend",
    )
    store.save_task(recon)
    worktree = worktrees.create(recon.id)
    (worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")
    worktrees.commit(recon.id, "reconcile change")
    report = Report(
        task_id=recon.id, agent=f"worker:{recon.id}", summary="resolved",
        diff_ref=worktrees.branch_name(recon.id), tests_passed=True,
        tokens_used=0, blockers=[],
    )

    decision = review(
        store,
        worktrees,
        results=[make_manager_result(final_status="review")],
        reports=[report],
        recons=[recon.model_dump()],
        attempts={"mgr-1": 1},
    )
    assert decision.merged_recons == [recon.id]
    assert (git_repo / "shared.txt").read_text(encoding="utf-8") == "resolved\n"
    assert store.get_task(recon.id).status == "done"
    assert decision.all_done


def test_failing_reconcile_report_escalates(store, worktrees) -> None:
    recon = Task(
        id="mgr-1-reconcile-1", parent_id="lead-1", level=0,
        goal="resolve shared.txt", deliverable="integrated", dependencies=[],
        status="failed", domain="backend",
    )
    store.save_task(recon)
    report = Report(
        task_id=recon.id, agent=f"worker:{recon.id}", summary="still broken",
        diff_ref=None, tests_passed=False, tokens_used=0,
        blockers=["tests failed in worktree"],
    )
    decision = review(
        store,
        worktrees,
        results=[make_manager_result(final_status="review")],
        reports=[report],
        recons=[recon.model_dump()],
        attempts={"mgr-1": 1},
    )
    assert decision.merged_recons == []
    assert decision.escalated == [recon.id]
    assert any("reconciliation" in b for b in decision.blockers)
    assert store.get_task(recon.id).status == "failed"


def test_reconcile_merge_conflicting_again_escalates(store, worktrees, git_repo) -> None:
    """Second conflict: the reconciler's own branch collides → escalate."""
    recon = Task(
        id="mgr-1-reconcile-1", parent_id="lead-1", level=0,
        goal="resolve shared.txt", deliverable="integrated", dependencies=[],
        status="review", domain="backend",
    )
    store.save_task(recon)
    worktree = worktrees.create(recon.id)
    (worktree / "shared.txt").write_text("reconciler version\n", encoding="utf-8")
    worktrees.commit(recon.id, "reconcile change")
    # Base moves the same file after the recon branched → merge conflicts.
    (git_repo / "shared.txt").write_text("base version\n", encoding="utf-8")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-m", "base change"], cwd=git_repo)
    report = Report(
        task_id=recon.id, agent=f"worker:{recon.id}", summary="wrote resolution",
        diff_ref=worktrees.branch_name(recon.id), tests_passed=True,
        tokens_used=0, blockers=[],
    )

    decision = review(
        store,
        worktrees,
        results=[make_manager_result(final_status="review")],
        reports=[report],
        recons=[recon.model_dump()],
        attempts={"mgr-1": 1},
    )
    assert decision.merged_recons == []
    assert decision.escalated == [recon.id]
    assert any(CONFLICT_BLOCKER_PREFIX in b for b in decision.blockers)
    assert store.get_task(recon.id).status == "failed"
