"""Tests for the manage/lead/start/approve CLI commands, plus the Phase 6
status/logs introspection commands."""

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


def test_start_rejects_duplicate_epic_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    """Re-planning under an existing --task-id must fail, not overwrite the
    epic while its old children stay keyed to the same parent id."""
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)
    runner = CliRunner()
    args = [
        "start", "--epic", "g", "--domain-goal", "a:backend", "--domain-goal", "b:infra",
        "--task-id", "epic-dup",
    ]

    first = runner.invoke(cli, args, catch_exceptions=False)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, args)
    assert second.exit_code != 0
    assert "epic-dup" in second.output
    assert "WARNING" in second.output, "the rejection must carry an explicit user warning"
    assert "2 child lead(s)" in second.output, "warning states what is at stake"
    assert "orchestrator start" in second.output, "warning tells the user how to proceed"

    with StateStore(root / "data" / "db.sqlite") as store:
        assert len(store.tasks_by_parent("epic-dup")) == 2, "no stale children appended"


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


def _kill_process_tree(pid: int) -> None:
    """Hard-kill a process and all descendants (the manual kill -9 demo).

    Only the DESCENDANTS are reaped here — the caller owns the direct child
    (Popen.wait) and must reap it itself, otherwise psutil's waitpid wins
    the race and subprocess reports a bogus exit status of 0.
    """
    import psutil

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(children, timeout=10)
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass


def test_start_resume_after_kill9_continues_from_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    """The Phase 5 acceptance: kill -9 the orchestrator mid-worker, re-invoke,
    and the epic finishes purely from persisted state — attempts preserved,
    exactly one report per task, one repo_map section (no duplicated work)."""
    import os
    import time

    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    planned = CliRunner().invoke(
        cli,
        ["start", "--epic", "ship it", "--domain-goal", "build the slice:backend",
         "--task-id", "epic-k"],
        catch_exceptions=False,
    )
    assert planned.exit_code == 0, planned.output
    with StateStore(root / "data" / "db.sqlite") as store:
        lead_id = store.tasks_by_parent("epic-k")[0].id
    assert CliRunner().invoke(cli, ["approve", lead_id], catch_exceptions=False).exit_code == 0

    env = {**os.environ, "FAKE_WORKER_SLEEP_S": "4", "PYTHONPATH": str(FAKE_WORKER.parents[1])}
    args = [python_bin, "-m", "orchestrator", "start",
            "--config", str(root / "config.yaml")]

    killed = subprocess.Popen(args, cwd=str(root), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def worker_in_flight() -> bool:
        with StateStore(root / "data" / "db.sqlite") as store:
            rows = store.connection().execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'in_progress' AND level = 0"
            ).fetchone()[0]
        return rows > 0

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not worker_in_flight():
        time.sleep(0.1)
    assert worker_in_flight(), "worker never started; cannot kill mid-flight"
    time.sleep(0.5)  # land inside the sleeping subprocess
    assert killed.poll() is None, (
        f"orchestrator finished before the kill; stdout:\n{killed.stdout.read()}"
    )

    _kill_process_tree(killed.pid)
    killed.wait(timeout=15)
    assert killed.returncode != 0, (
        f"hard kill must be visible in the exit status; "
        f"stdout={killed.stdout.read()!r} stderr={killed.stderr.read()!r}"
    )

    with StateStore(root / "data" / "db.sqlite") as store:
        lead_status = store.get_task(lead_id).status
        assert lead_status in ("in_progress",), "killed mid-run, not after finalize"
        assert store.connection().execute(
            "SELECT COUNT(*) FROM reports WHERE task_id LIKE ? AND agent LIKE 'worker:%'",
            (f"{lead_id}%",),
        ).fetchone()[0] <= 1

    resumed = subprocess.run(args, cwd=str(root), env=env,
                             capture_output=True, text=True, shell=False, timeout=180)
    assert resumed.returncode == 0, f"{resumed.stdout}\n{resumed.stderr}"
    assert "final=done" in resumed.stdout

    with StateStore(root / "data" / "db.sqlite") as store:
        assert store.get_task("epic-k").status == "done"
        duplicates = store.connection().execute(
            "SELECT task_id, COUNT(*) AS c FROM reports GROUP BY task_id HAVING c > 1"
        ).fetchall()
        assert duplicates == [], "resume must not duplicate reports/dispatches"

    repo_map = root / "domains" / "backend" / "repo_map.md"
    assert repo_map.is_file(), "resumed lead still maintains its knowledge doc"
    assert repo_map.read_text(encoding="utf-8").count("## lead run") == 1


# -- Phase 6: status / logs ---------------------------------------------------


def test_status_prints_task_tree_from_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    leads = _plan_epic_in_db(root, epic_id="epic-tree")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "epic-tree" in result.output
    assert "pending_approval" in result.output
    for lead in leads:
        assert lead.id in result.output


def test_status_without_database_reports_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "no database" in result.output


def test_logs_without_run_log_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(cli, ["logs"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "no run logs" in result.output


def test_logs_prints_newest_run_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)
    logs_dir = root / "logs"
    logs_dir.mkdir()
    # Names must sort like real UTC-stamped run files do.
    (logs_dir / "run_20260101T000000Z.jsonl").write_text('{"event": "older"}\n', encoding="utf-8")
    (logs_dir / "run_20260102T000000Z.jsonl").write_text(
        '{"event": "task_start"}\n{"event": "merge"}\n', encoding="utf-8"
    )

    result = CliRunner().invoke(cli, ["logs"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert '"event": "task_start"' in result.output
    assert "older" not in result.output, "only the newest run file is shown"


def test_logs_tail_prints_last_lines_then_follows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    python_bin: str,
    git_init,
) -> None:
    """--tail N shows the last N lines; the follow loop must be patchable so
    the CLI test does not block on a real infinite poll."""
    import orchestrator.main as main_module

    calls: list[dict] = []

    def fake_follow(path, pos, out, **kwargs):
        calls.append({"path": path.name, "pos": pos})

    monkeypatch.setattr(main_module, "follow_file", fake_follow)

    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)
    logs_dir = root / "logs"
    logs_dir.mkdir()
    log = logs_dir / "run_t.jsonl"
    log.write_text("e1\ne2\ne3\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["logs", "--tail", "2"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "e1" not in result.output
    assert "e3" in result.output and "e2" in result.output
    assert len(calls) == 1
    assert calls[0]["path"] == "run_t.jsonl"
    assert calls[0]["pos"] == log.stat().st_size  # follow resumes at EOF


def test_logs_tail_rejects_non_numeric_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_bin: str, git_init
) -> None:
    root = tmp_path / "repo"
    _init_repo(root, git_init)
    _write_config(root, python_bin)
    monkeypatch.chdir(root)
    logs_dir = root / "logs"
    logs_dir.mkdir()
    (logs_dir / "run_x.jsonl").write_text("e\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["logs", "--tail", "lots"])

    assert result.exit_code != 0
    assert "non-negative integer" in result.output
