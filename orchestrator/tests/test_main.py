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


def test_start_epic_plans_and_awaits_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    """Plan mode: creates the epic + pending_approval leads, runs NOTHING."""
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        cli,
        [
            "start", "--epic", "ship the platform",
            "--domain-goal", "ship auth api:backend",
            "--domain-goal", "wire pipeline:infra",
            "--task-id", "epic-42",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "epic-42" in result.output
    assert result.output.count("pending_approval") >= 2
    assert not list(root.glob("module_*.py")), "nothing may run before approval"
    workspaces = root / "workspaces"
    assert not workspaces.exists() or not any(workspaces.iterdir())

    with StateStore(root / "data" / "db.sqlite") as store:
        assert store.get_task("epic-42").status == "review"
        leads = store.tasks_by_parent("epic-42")
        assert [lead.domain for lead in leads] == ["backend", "infra"]
        assert all(lead.status == "pending_approval" for lead in leads)


def test_start_requires_domain_goals_with_epic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["start", "--epic", "goalless epic"])

    assert result.exit_code != 0
    bad = CliRunner().invoke(cli, ["start", "--epic", "g", "--domain-goal", "no-colon"])
    assert bad.exit_code != 0


def test_start_resume_runs_only_approved_leads_and_finishes_epic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    """The full Phase 4 story: plan → approve one → partial run → approve the
    rest → resume → epic done with PROJECT_STATE.md updated."""
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)
    runner = CliRunner()

    planned = runner.invoke(
        cli,
        [
            "start", "--epic", "ship the platform",
            "--domain-goal", "ship auth api:backend",
            "--domain-goal", "wire pipeline:infra",
            "--task-id", "epic-42",
        ],
        catch_exceptions=False,
    )
    assert planned.exit_code == 0, planned.output

    with StateStore(root / "data" / "db.sqlite") as store:
        leads = store.tasks_by_parent("epic-42")
        backend_lead, infra_lead = leads[0], leads[1]

    # Approve only backend and resume: backend runs, infra stays untouched.
    assert runner.invoke(cli, ["approve", backend_lead.id], catch_exceptions=False).exit_code == 0
    partial = runner.invoke(cli, ["start"], catch_exceptions=False)
    assert partial.exit_code == 0, partial.output

    assert len(list(root.glob("module_*.py"))) == 1, "only the approved domain runs"
    with StateStore(root / "data" / "db.sqlite") as store:
        assert store.get_task(infra_lead.id).status == "pending_approval"
        assert store.get_task("epic-42").status == "review", "epic waits for infra"
    assert not (root / "domains" / "PROJECT_STATE.md").exists(), "no epic section until done"

    # Approve infra and resume: everything runs, epic finalizes.
    assert runner.invoke(cli, ["approve", infra_lead.id], catch_exceptions=False).exit_code == 0
    final = runner.invoke(cli, ["start"], catch_exceptions=False)
    assert final.exit_code == 0, final.output

    assert len(list(root.glob("module_*.py"))) == 2
    with StateStore(root / "data" / "db.sqlite") as store:
        assert store.get_task("epic-42").status == "done"
        report = store.latest_report("epic-42")
        assert report is not None and report.agent == "architect:epic-42"
        assert report.tests_passed
    project_state = root / "domains" / "PROJECT_STATE.md"
    state_text = project_state.read_text(encoding="utf-8")
    assert "epic-42" in state_text and "backend" in state_text and "infra" in state_text
    assert (root / "domains" / "backend" / "repo_map.md").is_file()


def test_start_resume_without_any_epic_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code != 0
    assert "epic" in result.output.lower()


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
