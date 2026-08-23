"""Tests for the manage/lead/start/approve CLI commands."""

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from orchestrator.contracts import Task
from orchestrator.graph.architect import plan_epic
from orchestrator.graph.decompose import StaticEpicDecomposer
from orchestrator.main import cli
from orchestrator.memory.store import StateStore

FAKE_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "fake_worker.py"


def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _init_repo(root: Path, git_init) -> None:
    git_init(root)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)


def _write_config(root: Path, python_bin: str) -> None:
    config = {
        "execution": {"zcode_command": [python_bin, str(FAKE_WORKER)], "worker_timeout_s": 60},
        "paths": {"db": "./data/db.sqlite", "workspaces": "./workspaces", "domains": "./domains"},
        "retries": {"max_worker_retries": 1, "max_reconcile_attempts": 1},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _plan_epic_in_db(root: Path, epic_id: str = "epic-t") -> list[Task]:
    """Plan a two-domain epic directly into the CLI's database."""
    epic = Task(
        id=epic_id, parent_id=None, level=3, goal="g", deliverable="d",
        dependencies=[], status="pending", assigned_to=None,
    )
    with StateStore(root / "data" / "db.sqlite") as store:
        store.init_schema()
        return plan_epic(
            epic=epic,
            decomposer=StaticEpicDecomposer([("goal b", "backend"), ("goal i", "infra")]),
            store=store,
        )


def test_approve_flips_pending_approval_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    leads = _plan_epic_in_db(root)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["approve", leads[0].id], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert leads[0].id in result.output
    with StateStore(root / "data" / "db.sqlite") as store:
        assert store.get_task(leads[0].id).status == "pending"
        assert store.get_task(leads[1].id).status == "pending_approval"


def test_approve_rejects_tasks_not_awaiting_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    leads = _plan_epic_in_db(root)
    with StateStore(root / "data" / "db.sqlite") as store:
        store.set_task_status(leads[0].id, "pending")
    monkeypatch.chdir(root)

    already_approved = CliRunner().invoke(cli, ["approve", leads[0].id])
    unknown = CliRunner().invoke(cli, ["approve", "no-such-task"])

    assert already_approved.exit_code != 0
    assert unknown.exit_code != 0


def test_manage_command_runs_graph_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    config = {
        "execution": {"zcode_command": [python_bin, str(FAKE_WORKER)], "worker_timeout_s": 60},
        "paths": {"db": "./data/db.sqlite", "workspaces": "./workspaces"},
        "retries": {"max_worker_retries": 1},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        cli,
        ["manage", "--goal", "ship modules", "--sub-goal", "module one", "--sub-goal", "module two"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "review" in result.output
    assert len(list(root.glob("module_*.py"))) == 2, "worker branches should be merged into the repo"

    import sqlite3

    conn = sqlite3.connect(root / "data" / "db.sqlite")
    manager_reports = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE agent LIKE 'manager:%'"
    ).fetchone()[0]
    worker_reports = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE agent LIKE 'worker:%'"
    ).fetchone()[0]
    conn.close()
    assert manager_reports == 1
    assert worker_reports == 2


def test_lead_command_runs_lead_graph_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    config = {
        "execution": {"zcode_command": [python_bin, str(FAKE_WORKER)], "worker_timeout_s": 60},
        "paths": {"db": "./data/db.sqlite", "workspaces": "./workspaces", "domains": "./domains"},
        "retries": {"max_worker_retries": 1, "max_reconcile_attempts": 1},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        cli,
        [
            "lead",
            "--goal", "ship the backend slice",
            "--manager-goal", "slice one",
            "--manager-goal", "slice two",
            "--domain", "backend",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "review" in result.output
    assert len(list(root.glob("module_*.py"))) == 2

    import sqlite3

    conn = sqlite3.connect(root / "data" / "db.sqlite")
    lead_reports = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE agent LIKE 'lead:%'"
    ).fetchone()[0]
    manager_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE level = 1"
    ).fetchone()[0]
    conn.close()
    assert lead_reports == 1
    assert manager_tasks == 2

    repo_map = root / "domains" / "backend" / "repo_map.md"
    assert repo_map.is_file(), "lead must maintain the domain repo map"
    assert "## lead run" in repo_map.read_text(encoding="utf-8")
