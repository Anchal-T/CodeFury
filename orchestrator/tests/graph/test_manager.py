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


def test_retry_entry_carries_feedback_from_failed_report(deps, store: StateStore) -> None:
    """Phase 10: a retried task rides with its previous attempt's feedback so
    the rebuilt worker prompt says what went wrong (the identical-prompt retry
    used to repeat the same mistakes)."""
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
    feedback = decision.retry[0]["feedback"]
    assert "Previous attempt feedback" in feedback
    assert "tests failed in worktree" in feedback


def test_failed_report_past_cap_fails_for_good(deps, store: StateStore) -> None:
    worktrees, policy = deps
    task = make_worker_task("w-1", status="failed")
    store.save_task(task)

    decision = review_reports(
        reports=[make_report("w-1", passed=False)],
        worker_tasks=[task],
        attempts={"w-1": 3},  # initial try + 2 retries exhausted
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


def test_budget_blocker_fails_without_retry(deps, store: StateStore) -> None:
    """A budget-gated report means the level is out of tokens — re-dispatching
    would just bounce off the gate again; fail immediately instead."""
    from orchestrator.governance.budget import BUDGET_EXHAUSTED_PREFIX

    worktrees, policy = deps
    task = make_worker_task("w-1", status="failed")
    store.save_task(task)
    report = make_report("w-1", passed=False).model_copy(
        update={
            "summary": "skipped: level token budget exhausted",
            "blockers": [f"{BUDGET_EXHAUSTED_PREFIX} for level 0 (worker): 10/10 tokens used"],
        }
    )

    decision = review_reports(
        reports=[report],
        worker_tasks=[task],
        attempts={"w-1": 1},  # retries are NOT exhausted — budget decides
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
    )
    assert decision.retry == []
    assert decision.failed == ["w-1"]
    assert decision.all_done
    assert any(b.startswith(BUDGET_EXHAUSTED_PREFIX) for b in decision.blockers)
    assert store.get_task("w-1").status == "failed"


def test_review_emits_merge_and_retry_events(deps, store: StateStore) -> None:
    """The JSONL run log receives merge/retry events from the review."""
    from orchestrator.logging_setup import RunLogger  # noqa: F401 — typing parity

    class MemoryRunLogger:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def event(self, kind: str, **fields: object) -> None:
            self.events.append((kind, dict(fields)))

    worktrees, policy = deps
    merged_task = make_worker_task("w-1")
    retried_task = make_worker_task("w-2", status="failed")
    store.save_task(merged_task)
    store.save_task(retried_task)
    worktree = worktrees.create("w-1")
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    worktrees.commit("w-1", "worker change")

    runlog = MemoryRunLogger()
    review_reports(
        reports=[make_report("w-1", passed=True), make_report("w-2", passed=False)],
        worker_tasks=[merged_task, retried_task],
        attempts={"w-1": 1, "w-2": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
        runlog=runlog,  # type: ignore[arg-type] — same event() interface
    )

    assert ("merge", {"task_id": "w-1"}) in runlog.events
    assert ("retry", {"task_id": "w-2", "attempt": 2, "with_feedback": True}) in runlog.events


def test_strict_critic_promotes_warnings_to_blockers(deps, store: StateStore) -> None:
    """Phase 11 strict mode: critic warnings become blockers, so the report
    routes into the Phase 10 retry-with-feedback path instead of merging."""
    from orchestrator.config import CriticConfig

    worktrees, policy = deps
    task = make_worker_task("w-1", status="review")
    store.save_task(task)
    report = make_report("w-1", passed=True).model_copy(
        update={"warnings": ["critic: worker modified test files: tests/test_x.py"]}
    )

    decision = review_reports(
        reports=[report],
        worker_tasks=[task],
        attempts={"w-1": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
        critic=CriticConfig(strict=True),
    )
    assert decision.merged == []
    assert [t["id"] for t in decision.retry] == ["w-1"]
    assert store.get_task("w-1").status == "pending"


def test_non_strict_critic_leaves_passing_reports_alone(
    deps, store: StateStore, git_repo: Path
) -> None:
    """Default (advisory) mode: warnings never change the merge decision."""
    from orchestrator.config import CriticConfig

    worktrees, policy = deps
    task = make_worker_task("w-1")
    store.save_task(task)
    worktree = worktrees.create("w-1")
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    worktrees.commit("w-1", "worker change")
    report = make_report("w-1", passed=True).model_copy(
        update={"warnings": ["critic: worker modified test files: tests/test_x.py"]}
    )

    decision = review_reports(
        reports=[report],
        worker_tasks=[task],
        attempts={"w-1": 1},
        worktrees=worktrees,
        retry_policy=policy,
        store=store,
        critic=CriticConfig(),
    )
    assert decision.merged == ["w-1"]
    assert decision.retry == []


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


def test_finalize_parent_runs_integration_tests_after_merge(
    git_repo: Path, store: StateStore, python_bin: str
) -> None:
    """Individually passing workers can still assemble a broken tree: the
    manager must test the merged repo root before declaring success."""
    parent = Task(
        id="m-1", parent_id=None, level=1, goal="g", deliverable="d",
        dependencies=[], status="in_progress", assigned_to=None,
    )
    store.save_task(parent)
    from orchestrator.graph.manager import ReviewDecision

    decision = ReviewDecision(merged=["w-1"], retry=[], failed=[], blockers=[], all_done=True)

    passing = [python_bin, "-c", "print('integration ok')"]
    report = finalize_parent(
        parent=parent, decision=decision, reports=[], store=store,
        repo_root=git_repo, test_command=passing,
    )
    assert report.tests_passed
    assert store.get_task("m-1").status == "review"

    failing = [python_bin, "-c", "raise SystemExit(1)"]
    report = finalize_parent(
        parent=parent, decision=decision, reports=[], store=store,
        repo_root=git_repo, test_command=failing,
    )
    assert not report.tests_passed
    assert any("integration" in b for b in report.blockers)
    assert store.get_task("m-1").status == "failed"
