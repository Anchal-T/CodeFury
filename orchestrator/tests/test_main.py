"""Tests for the manage CLI command (Phase 2 graph driver)."""

import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

from orchestrator.main import cli

FAKE_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "fake_worker.py"


def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)


def test_manage_command_runs_graph_end_to_end(
    tmp_path: Path, monkeypatch, python_bin: str
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
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
    assert (root / "greeting.py").is_file(), "worker branches should be merged into the repo"

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
