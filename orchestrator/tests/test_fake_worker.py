"""Tests for the fake worker dev script (scripts/fake_worker.py)."""

import os
import subprocess
from pathlib import Path

FAKE_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "fake_worker.py"


def test_fake_worker_writes_outputs(tmp_path: Path, python_bin: str) -> None:
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), "the task prompt"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "greeting.py").is_file()
    assert (tmp_path / "hello_from_worker.txt").is_file()
    assert "prompt was" in (tmp_path / "hello_from_worker.txt").read_text(encoding="utf-8")


def test_fake_worker_fail_mode_exits_nonzero_without_changes(
    tmp_path: Path, python_bin: str
) -> None:
    env = {**os.environ, "FAKE_WORKER_FAIL": "1"}
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), "prompt"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "greeting.py").exists()
    assert not (tmp_path / "hello_from_worker.txt").exists()
