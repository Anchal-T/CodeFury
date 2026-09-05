"""Tests for the eval replayer (Phase 12): level-0 tasks from a past run are
re-dispatched through the current pipeline into an isolated eval workspace.
No merging, no writes to the source DB, worktrees cleaned up by default."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import Report, Task
from orchestrator.execution.cli_runner import AgentCliRunner
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.eval.replay import EvalRunner, ReplayResult
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"


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
def eval_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "data" / "eval.db")
    store.init_schema()
    yield store
    store.close()


def make_task(task_id: str = "w-1") -> Task:
    return Task(
        id=task_id,
        parent_id=None,
        level=0,
        goal=f"goal {task_id}",
        deliverable="d",
        dependencies=[],
        status="done",
        assigned_to=None,
    )


def make_runner(python_bin: str) -> AgentCliRunner:
    return AgentCliRunner(command=[python_bin, str(FAKE_WORKER)])


def make_eval_runner(
    git_repo: Path,
    eval_store: StateStore,
    python_bin: str,
    tmp_path: Path,
    **kwargs: object,
) -> EvalRunner:
    return EvalRunner(
        store=eval_store,
        worktrees=WorktreeManager(
            git_repo, tmp_path / "eval-workspaces", branch_prefix="orchestrator/eval-"
        ),
        runner=make_runner(python_bin),
        test_command=[python_bin, "-c", "print('tests ok')"],
        **kwargs,  # type: ignore[arg-type]
    )


def test_replay_reruns_task_and_persists_report(
    git_repo: Path, eval_store: StateStore, python_bin: str, tmp_path: Path
) -> None:
    runner = make_eval_runner(git_repo, eval_store, python_bin, tmp_path)
    results = runner.run([make_task("w-1")])

    assert len(results) == 1
    result = results[0]
    assert result.error is None
    assert result.report is not None
    assert result.report.tests_passed
    assert result.report.tokens_used > 0
    # The replayed report landed in the eval store (durability for inspection).
    stored = eval_store.latest_report("w-1")
    assert stored is not None and stored.tests_passed
    # No merging: the toy repo root stays untouched by the replay.
    assert not (git_repo / "module_").exists()


def test_replay_cleans_worktrees_by_default(
    git_repo: Path, eval_store: StateStore, python_bin: str, tmp_path: Path
) -> None:
    runner = make_eval_runner(git_repo, eval_store, python_bin, tmp_path)
    runner.run([make_task("w-1")])
    assert not (tmp_path / "eval-workspaces" / "worker_w-1").exists()
    assert "orchestrator/eval-w-1" not in _git(["branch", "--list"], cwd=git_repo)


def test_replay_keep_worktrees_flag_retains_them(
    git_repo: Path, eval_store: StateStore, python_bin: str, tmp_path: Path
) -> None:
    runner = make_eval_runner(
        git_repo, eval_store, python_bin, tmp_path, keep_worktrees=True
    )
    runner.run([make_task("w-keep")])
    assert (tmp_path / "eval-workspaces" / "worker_w-keep").is_dir()
    assert "orchestrator/eval-w-keep" in _git(["branch", "--list"], cwd=git_repo)


def test_replay_uses_eval_branch_prefix_not_worker_prefix(
    git_repo: Path, eval_store: StateStore, python_bin: str, tmp_path: Path
) -> None:
    """A replay of task 'w-1' must never clobber a real run's
    orchestrator/worker-w-1 branch — eval branches are namespaced."""
    runner = make_eval_runner(git_repo, eval_store, python_bin, tmp_path)
    runner.run([make_task("w-1")])
    assert "orchestrator/worker-w-1" not in _git(["branch", "--list"], cwd=git_repo)


def test_replay_failure_is_captured_not_raised(
    git_repo: Path, eval_store: StateStore, python_bin: str, tmp_path: Path
) -> None:
    runner = make_eval_runner(git_repo, eval_store, python_bin, tmp_path)
    failing = EvalRunner(
        store=eval_store,
        worktrees=runner.worktrees,
        runner=AgentCliRunner(command=[python_bin, "-c", "raise SystemExit(7)"]),
        test_command=[python_bin, "-c", "print('tests ok')"],
    )
    results = failing.run([make_task("w-bad")])
    assert results[0].error is None  # pipeline still returns a Report
    assert results[0].report is not None
    assert not results[0].report.tests_passed
    assert any("exited with code 7" in b for b in results[0].report.blockers)


def test_replay_result_holds_task_id_and_report() -> None:
    result = ReplayResult(task_id="w-1", report=None, error="boom")
    assert result.task_id == "w-1" and result.error == "boom"
