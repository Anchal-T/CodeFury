"""Tests for orchestrator.execution.zcode_runner (no real LLM involved)."""

import time
from pathlib import Path

from orchestrator.execution.zcode_runner import ZCodeRunner


def test_successful_run(tmp_path: Path, python_bin: str) -> None:
    runner = ZCodeRunner(command=[python_bin, "-c", "print('hi')"])
    result = runner.run("do the thing", cwd=tmp_path)
    assert result.ok
    assert result.stdout.strip() == "hi"
    assert not result.timed_out
    assert result.returncode == 0


def test_prompt_appended_as_last_argument(tmp_path: Path, python_bin: str) -> None:
    runner = ZCodeRunner(command=[python_bin, "-c", "import sys; print(sys.argv[1])"])
    result = runner.run("PROMPT-42", cwd=tmp_path)
    assert result.stdout.strip() == "PROMPT-42"


def test_prompt_placeholder_substituted_in_place(tmp_path: Path, python_bin: str) -> None:
    runner = ZCodeRunner(
        command=[python_bin, "-c", "import sys; print(sys.argv[1], sys.argv[2])", "PRE", "{prompt}"]
    )
    result = runner.run("hello-prompt", cwd=tmp_path)
    assert result.stdout.strip() == "PRE hello-prompt"


def test_nonzero_exit_is_not_ok(tmp_path: Path, python_bin: str) -> None:
    runner = ZCodeRunner(command=[python_bin, "-c", "raise SystemExit(3)"])
    result = runner.run("x", cwd=tmp_path)
    assert not result.ok
    assert result.returncode == 3


def test_timeout_kills_process_tree(tmp_path: Path, python_bin: str) -> None:
    # Parent spawns a child; both sleep far beyond the runner timeout. A
    # working tree-kill must reap both so communicate() can return.
    sleeper = (
        "import subprocess, sys, time\n"
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    runner = ZCodeRunner(command=[python_bin, "-c", sleeper], timeout=1.0)
    start = time.monotonic()
    result = runner.run("x", cwd=tmp_path)
    elapsed = time.monotonic() - start
    assert result.timed_out
    assert not result.ok
    assert elapsed < 30, "process tree was not killed promptly"
