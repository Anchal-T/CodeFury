"""Tests for the fake worker dev script (scripts/fake_worker.py)."""

import hashlib
import os
import subprocess
from pathlib import Path

FAKE_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "fake_worker.py"


def module_name_for(prompt: str) -> str:
    return f"module_{hashlib.sha1(prompt.encode()).hexdigest()[:8]}"


def test_fake_worker_writes_task_specific_outputs(tmp_path: Path, python_bin: str) -> None:
    prompt = "the task prompt"
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), prompt],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    module = module_name_for(prompt)
    assert (tmp_path / f"{module}.py").is_file(), "worker output must be task-specific"
    assert (tmp_path / f"{module}.txt").is_file()
    assert "prompt was" in (tmp_path / f"{module}.txt").read_text(encoding="utf-8")


def test_fake_worker_fail_mode_exits_nonzero_without_changes(
    tmp_path: Path, python_bin: str
) -> None:
    prompt = "doomed prompt"
    env = {**os.environ, "FAKE_WORKER_FAIL": "1"}
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), prompt],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode != 0
    module = module_name_for(prompt)
    assert not (tmp_path / f"{module}.py").exists()
    assert not (tmp_path / f"{module}.txt").exists()
