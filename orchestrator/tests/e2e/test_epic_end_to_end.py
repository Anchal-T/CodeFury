"""Phase 8 end-to-end: the full hierarchy through real CLI invocations.

Architect plans a two-domain epic → human approves both leads → resume runs
Lead → Manager → Worker for each domain → epic finalizes and dogfoods the
memory system (PROJECT_STATE.md + domains/*/repo_map.md updated by its own
output). Everything runs against the committed scripts/fake_worker.py — no
LLM, no network (Tier-3 stays off via ORCHESTRATOR_SEMANTIC=0).
"""

import os
import subprocess
import time
from pathlib import Path

import psutil
import yaml

from orchestrator.memory.store import StateStore

PKG_PARENT = str(Path(__file__).resolve().parents[2])
FAKE_WORKER = Path(PKG_PARENT) / "scripts" / "fake_worker.py"
PROC_TIMEOUT_S = 240.0


def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def make_toy_repo(tmp_path: Path, python_bin: str, git_init, *, max_workers: int = 3) -> Path:
    """A small real git repo with a tuned config pointing at the fake worker."""
    root = tmp_path / "toy-repo"
    root.mkdir()
    git_init(root)
    (root / "README.md").write_text("toy repo\n", encoding="utf-8")
    config = {
        "concurrency": {"max_workers": max_workers},
        "budgets": {
            "worker_tokens": 100000,
            "manager_tokens": None,
            "domain_lead_tokens": None,
            "architect_tokens": None,
        },
        "execution": {
            "zcode_command": [python_bin, str(FAKE_WORKER)],
            "worker_timeout_s": 120,
        },
        "paths": {
            "workspaces": "./workspaces",
            "domains": "./domains",
            "db": "./data/orchestrator.db",
            "logs": "./logs",
        },
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


def orchestrator_env(**overrides: str) -> dict:
    """Subprocess env: importable package, Tier-3 off, fake-worker knobs."""
    env = {
        **os.environ,
        "PYTHONPATH": PKG_PARENT,
        "ORCHESTRATOR_SEMANTIC": "0",
    }
    env.update(overrides)
    return env


def run_cli(root: Path, python_bin: str, *args: str, **env_overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [python_bin, "-m", "orchestrator", *args],
        cwd=str(root),
        env=orchestrator_env(**env_overrides),
        capture_output=True,
        text=True,
        shell=False,
        timeout=PROC_TIMEOUT_S,
    )


def leftover_worker_processes() -> list[str]:
    """Descriptions of any fake_worker processes still alive or defunct.

    A completed orchestration must leave neither: live workers mean a spawn
    escaped cleanup; zombies mean the parent exited without reaping.
    """
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            if not any("fake_worker.py" in part for part in cmdline):
                continue
            found.append(f"pid={proc.pid} status={proc.status()}")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def wait_for_epic_status(db: Path, epic_id: str, want: str, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with StateStore(db) as store:
            task = store.get_task(epic_id)
        if task is not None and task.status == want:
            return want
        time.sleep(0.1)
    with StateStore(db) as store:
        task = store.get_task(epic_id)
    return task.status if task else "<missing>"


def test_full_hierarchy_epic_runs_and_dogfoods_memory(
    tmp_path: Path, python_bin: str, git_init
) -> None:
    root = make_toy_repo(tmp_path, python_bin, git_init)
    db = root / "data" / "orchestrator.db"

    # 1. Architect plans the epic; nothing runs yet.
    planned = run_cli(
        root, python_bin, "start",
        "--epic", "ship demo platform",
        "--domain-goal", "build auth api:backend",
        "--domain-goal", "wire pipeline:infra",
        "--task-id", "epic-e2e",
    )
    assert planned.returncode == 0, planned.stderr or planned.stdout
    assert not ((root / "workspaces").exists() and any((root / "workspaces").iterdir())), (
        "nothing may dispatch before approval"
    )
    lead_ids = [
        line.split()[1]
        for line in planned.stdout.splitlines()
        if "[pending_approval]" in line
    ]
    assert len(lead_ids) == 2

    # 2. Human approves both domains.
    for lead_id in lead_ids:
        approved = run_cli(root, python_bin, "approve", lead_id)
        assert approved.returncode == 0, approved.stderr

    # 3. Resume: leads → managers → workers → merges → epic done.
    resumed = run_cli(root, python_bin, "start")
    assert resumed.returncode == 0, f"{resumed.stderr}\n{resumed.stdout}"
    assert "final=done" in resumed.stdout

    # Hierarchy terminal states, straight from SQLite.
    with StateStore(db) as store:
        epic = store.get_task("epic-e2e")
        assert epic.status == "done"
        leads = store.tasks_by_parent("epic-e2e")
        assert all(lead.status == "review" for lead in leads), (
            [lead.status for lead in leads]
        )
        managers = [
            manager
            for lead in leads
            for manager in store.tasks_by_parent(lead.id)
        ]
        assert len(managers) == 2
        assert all(manager.status == "review" for manager in managers)
        workers = [
            store.get_task(worker_id)
            for manager in managers
            for worker_id in [child.id for child in store.tasks_by_parent(manager.id)]
        ]
        assert workers and all(worker.status == "done" for worker in workers)

        # Phase 6 governance recorded real burn at the level that spent it.
        burn = store.total_tokens_by_level(0)
        assert burn > 0
        reports_tokens = sum(
            report.tokens_used
            for report in (store.latest_report(worker.id) for worker in workers)
            if report
        )
        assert burn == reports_tokens

    # 4. Dogfooding: the run's own output feeds the memory tiers.
    project_state = (root / "domains" / "PROJECT_STATE.md").read_text(encoding="utf-8")
    assert "## epic epic-e2e" in project_state
    assert "epic: ship demo platform" in project_state
    assert "- backend: review" in project_state and "- infra: review" in project_state
    for domain in ("backend", "infra"):
        repo_map = (root / "domains" / domain / "repo_map.md").read_text(encoding="utf-8")
        assert "## lead run" in repo_map, f"{domain} repo_map never appended"

    # 5. psutil tree hygiene: no live or zombie fake workers remain.
    assert leftover_worker_processes() == []
