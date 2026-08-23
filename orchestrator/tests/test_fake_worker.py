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


def test_fake_worker_target_mode_writes_prompt_specific_content(
    tmp_path: Path, python_bin: str
) -> None:
    """Target mode lets demos aim workers at one shared file; the content
    embeds the prompt hash so two different workers genuinely collide."""
    env = {
        **os.environ,
        "FAKE_WORKER_TARGET": "src/shared.txt",
        "FAKE_WORKER_CONTENT": "extra line",
    }
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), "goal one"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    target = tmp_path / "src" / "shared.txt"
    assert target.is_file(), "parent dirs must be created"
    text = target.read_text(encoding="utf-8")
    assert hashlib.sha1(b"goal one").hexdigest()[:8] in text
    assert "extra line" in text


def test_fake_worker_target_mode_differs_per_prompt(
    tmp_path: Path, python_bin: str
) -> None:
    env = {**os.environ, "FAKE_WORKER_TARGET": "shared.txt"}
    first = subprocess.run(
        [python_bin, str(FAKE_WORKER), "goal one"], cwd=tmp_path, env=env,
        capture_output=True, text=True, shell=False, timeout=30,
    )
    text_one = (tmp_path / "shared.txt").read_text(encoding="utf-8")
    second = subprocess.run(
        [python_bin, str(FAKE_WORKER), "goal two"], cwd=tmp_path, env=env,
        capture_output=True, text=True, shell=False, timeout=30,
    )
    text_two = (tmp_path / "shared.txt").read_text(encoding="utf-8")
    assert first.returncode == second.returncode == 0
    assert text_one != text_two, "distinct prompts must produce conflicting content"


def test_fake_worker_reconciler_role_labels_content(
    tmp_path: Path, python_bin: str
) -> None:
    env = {
        **os.environ,
        "FAKE_WORKER_TARGET": "shared.txt",
        "FAKE_WORKER_ROLE": "reconciler",
    }
    proc = subprocess.run(
        [python_bin, str(FAKE_WORKER), "resolve the mess"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    text = (tmp_path / "shared.txt").read_text(encoding="utf-8")
    assert "reconciler" in text
