"""Tests for the token burn report dev utility (scripts/token_report.py)."""

import subprocess
from pathlib import Path

from orchestrator.memory.store import StateStore

TOKEN_REPORT = Path(__file__).resolve().parents[1] / "scripts" / "token_report.py"


def seed_usage(db: Path) -> None:
    with StateStore(db) as store:
        store.init_schema()
        store.add_token_usage(0, 1000)
        store.add_token_usage(0, 500)
        store.add_token_usage(1, 1500)


def test_report_prints_per_level_burn(tmp_path: Path, python_bin: str) -> None:
    db = tmp_path / "data" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    seed_usage(db)

    proc = subprocess.run(
        [python_bin, str(TOKEN_REPORT), str(db)],
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0].split() == ["level", "role", "entries", "tokens"]
    assert "worker" in lines[2] and "2" in lines[2] and "1500" in lines[2]
    assert "manager" in lines[3] and "1500" in lines[3]


def test_report_handles_empty_and_missing_databases(
    tmp_path: Path, python_bin: str
) -> None:
    db = tmp_path / "empty.db"
    with StateStore(db) as store:
        store.init_schema()

    empty = subprocess.run(
        [python_bin, str(TOKEN_REPORT), str(db)],
        capture_output=True, text=True, shell=False, timeout=30,
    )
    assert empty.returncode == 0
    assert "no token usage" in empty.stdout

    missing = subprocess.run(
        [python_bin, str(TOKEN_REPORT), str(tmp_path / "nope.db")],
        capture_output=True, text=True, shell=False, timeout=30,
    )
    assert missing.returncode == 1
    assert "no database" in missing.stderr
