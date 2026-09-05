"""Phase 12 end-to-end: the `orchestrator eval` CLI replays a seeded source
database through the real CLI against a toy repo, with fake_worker as the
harness — offline, no LLM."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml

from orchestrator.contracts import Report, Task
from orchestrator.memory.store import StateStore

PKG_PARENT = str(Path(__file__).resolve().parents[2])
FAKE_WORKER = Path(PKG_PARENT) / "scripts" / "fake_worker.py"
PROC_TIMEOUT_S = 240.0


def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_toy_repo(tmp_path: Path, python_bin: str, git_init) -> Path:
    root = tmp_path / "toy-repo"
    root.mkdir()
    git_init(root)
    (root / "README.md").write_text("toy repo\n", encoding="utf-8")
    config = {
        "concurrency": {"max_workers": 2},
        "execution": {
            "worker_command": [python_bin, str(FAKE_WORKER)],
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


def seed_source_db(root: Path) -> None:
    """A past run: two level-0 tasks, one passed, one failed."""
    store = StateStore(root / "data" / "source.db")
    store.init_schema()
    for task_id in ("w-1", "w-2"):
        store.save_task(
            Task(
                id=task_id,
                parent_id=None,
                level=0,
                goal=f"goal {task_id}",
                deliverable="d",
                dependencies=[],
                status="done",
                assigned_to=None,
            )
        )
    store.save_report(
        Report(
            task_id="w-1", agent="worker:w-1", summary="old run", tests_passed=True,
            tokens_used=1234,
        )
    )
    store.save_report(
        Report(
            task_id="w-2", agent="worker:w-2", summary="old run", tests_passed=False,
            tokens_used=5678, blockers=["tests failed in worktree"],
        )
    )
    store.close()


def run_cli(root: Path, python_bin: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": PKG_PARENT, "ORCHESTRATOR_SEMANTIC": "0"}
    return subprocess.run(
        [python_bin, "-m", "orchestrator", *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=PROC_TIMEOUT_S,
    )


def test_eval_cli_replays_and_compares(tmp_path: Path, python_bin: str, git_init) -> None:
    root = make_toy_repo(tmp_path, python_bin, git_init)
    seed_source_db(root)
    source_hash = _sha256(root / "data" / "source.db")

    proc = run_cli(
        root, python_bin, "eval", "--from-db", "data/source.db", "--out", "eval_report.json"
    )
    assert proc.returncode == 0, f"{proc.stderr}\n{proc.stdout}"
    assert "w-1" in proc.stdout and "w-2" in proc.stdout
    assert "pass rate: 1/2 → 2/2" in proc.stdout, proc.stdout

    payload = json.loads((root / "eval_report.json").read_text(encoding="utf-8"))
    assert payload["compared"] == 2
    assert payload["new_passes"] == 2, "fake worker passes both replays"
    new_by_id = {c["task_id"]: c for c in payload["comparisons"]}
    assert new_by_id["w-1"]["old_tokens"] == 1234
    assert new_by_id["w-2"]["new_tokens"] > 0

    # The source run is history — byte-identical after the replay.
    assert _sha256(root / "data" / "source.db") == source_hash
    # Replays land in their own database, never the live one.
    assert (root / "data" / "eval.db").is_file()
    assert not (root / "data" / "orchestrator.db").exists()
    # Eval worktrees are cleaned up; eval branches are namespaced.
    assert not (root / "workspaces" / "eval").exists() or not any(
        (root / "workspaces" / "eval").iterdir()
    )


def test_eval_cli_unknown_task_id_fails_with_names(tmp_path: Path, python_bin: str, git_init) -> None:
    root = make_toy_repo(tmp_path, python_bin, git_init)
    seed_source_db(root)
    proc = run_cli(root, python_bin, "eval", "--from-db", "data/source.db", "--task", "nope")
    assert proc.returncode != 0
    assert "nope" in proc.stderr
