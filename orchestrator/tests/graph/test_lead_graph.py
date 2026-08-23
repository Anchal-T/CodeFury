"""End-to-end Domain Lead graph tests (roadmap Phase 3):

Lead decomposes into manager tasks (Send fan-out over the nested Manager/
Worker subgraph), reviews results, dispatches ONE capped reconciliation
worker per conflicting manager, and always appends a timestamped section to
the domain repo_map.md.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.graph.decompose import StaticDomainDecomposer, StaticDecomposer
from orchestrator.graph.lead_graph import build_lead_graph
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"
GRAPH_TIMEOUT_S = 120.0


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
def domains_dir(tmp_path: Path) -> Path:
    return tmp_path / "domains"


def make_lead(domain: str = "backend") -> Task:
    return Task(
        id="lead-1",
        parent_id=None,
        level=2,
        goal="ship the domain slice",
        deliverable="domain slice integrated",
        dependencies=[],
        status="pending",
        assigned_to=None,
        domain=domain,
    )


def build(store, git_repo, python_bin, decomposer, *, domains_dir=None,
          max_reconcile_attempts=1, max_workers=2):
    return build_lead_graph(
        store=store,
        worktrees=WorktreeManager(git_repo, git_repo / "workspaces"),
        runner=ZCodeRunner(command=[python_bin, str(FAKE_WORKER)]),
        decomposer=decomposer,
        test_command=[python_bin, "-c", "print('tests ok')"],
        max_workers=max_workers,
        max_reconcile_attempts=max_reconcile_attempts,
        knowledge=KnowledgeDocs(),
        domains_dir=domains_dir,
    )


def test_lead_graph_merges_two_managers_end_to_end(
    git_repo: Path, store: StateStore, python_bin: str, domains_dir: Path
) -> None:
    graph = build(store, git_repo, python_bin,
                  StaticDomainDecomposer(["slice one", "slice two"]),
                  domains_dir=domains_dir)

    result = asyncio.run(
        asyncio.wait_for(graph.ainvoke({"lead_task": make_lead().model_dump()}), GRAPH_TIMEOUT_S)
    )

    assert result["outcome"] == "review"
    assert len(list(git_repo.glob("module_*.py"))) == 2

    managers = store.connection().execute(
        "SELECT payload FROM tasks WHERE level = 1"
    ).fetchall()
    assert len(managers) == 2
    assert store.get_task("lead-1").status == "review"

    doc = domains_dir / "backend" / "repo_map.md"
    assert doc.is_file(), "repo_map.md must be created for the domain"
    text = doc.read_text(encoding="utf-8")
    assert "## lead run" in text
    assert "backend" in text


def test_lead_graph_reconciles_cross_manager_conflict(
    git_repo: Path, store: StateStore, python_bin: str, domains_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both workers target shared.txt with different prompts → add/add
    conflict at the second manager merge → Lead dispatches one reconciler."""
    monkeypatch.setenv("FAKE_WORKER_TARGET", "shared.txt")
    graph = build(store, git_repo, python_bin,
                  StaticDomainDecomposer(["slice one", "slice two"]),
                  domains_dir=domains_dir)

    result = asyncio.run(
        asyncio.wait_for(graph.ainvoke({"lead_task": make_lead().model_dump()}), GRAPH_TIMEOUT_S)
    )

    assert result["outcome"] == "review", result.get("blockers")
    content = (git_repo / "shared.txt").read_text(encoding="utf-8")
    assert "reconciler" in content, "reconciler's resolution must win"

    recons = store.connection().execute(
        "SELECT payload FROM tasks WHERE id LIKE '%-reconcile-%'"
    ).fetchall()
    assert len(recons) == 1, "exactly one reconciliation dispatch"
    assert Task.model_validate_json(recons[0]["payload"]).status == "done"

    doc_text = (domains_dir / "backend" / "repo_map.md").read_text(encoding="utf-8")
    assert "reconcil" in doc_text.lower()


def test_lead_graph_escalates_when_reconciliation_disabled(
    git_repo: Path, store: StateStore, python_bin: str, domains_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_WORKER_TARGET", "shared.txt")
    graph = build(store, git_repo, python_bin,
                  StaticDomainDecomposer(["slice one", "slice two"]),
                  domains_dir=domains_dir, max_reconcile_attempts=0)

    result = asyncio.run(
        asyncio.wait_for(graph.ainvoke({"lead_task": make_lead().model_dump()}), GRAPH_TIMEOUT_S)
    )

    assert result["outcome"] == "failed"
    assert any("reconcil" in b.lower() or "conflict" in b.lower() for b in result["blockers"])
    recons = store.connection().execute(
        "SELECT COUNT(*) FROM tasks WHERE id LIKE '%-reconcile-%'"
    ).fetchone()[0]
    assert recons == 0
    assert store.get_task("lead-1").status == "failed"


def test_lead_graph_empty_decomposition_fails_fast(
    git_repo: Path, store: StateStore, python_bin: str, domains_dir: Path
) -> None:
    graph = build(store, git_repo, python_bin, StaticDomainDecomposer([]),
                  domains_dir=domains_dir)

    result = asyncio.run(
        asyncio.wait_for(graph.ainvoke({"lead_task": make_lead().model_dump()}), GRAPH_TIMEOUT_S)
    )

    assert result["outcome"] == "failed"
    assert any("no manager tasks" in b for b in result["blockers"])
