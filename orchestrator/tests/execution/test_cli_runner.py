"""Tests for the AgentCliRunner harness adapter (no real LLM involved)."""

import time
from pathlib import Path

from orchestrator.execution.cli_runner import AgentCliRunner


def test_successful_run(tmp_path: Path, python_bin: str) -> None:
    runner = AgentCliRunner(command=[python_bin, "-c", "print('hi')"])
    result = runner.run("do the thing", cwd=tmp_path)
    assert result.ok
    assert result.stdout.strip() == "hi"
    assert not result.timed_out
    assert result.returncode == 0


def test_prompt_appended_as_last_argument(tmp_path: Path, python_bin: str) -> None:
    runner = AgentCliRunner(command=[python_bin, "-c", "import sys; print(sys.argv[1])"])
    result = runner.run("PROMPT-42", cwd=tmp_path)
    assert result.stdout.strip() == "PROMPT-42"


def test_prompt_placeholder_substituted_in_place(tmp_path: Path, python_bin: str) -> None:
    runner = AgentCliRunner(
        command=[python_bin, "-c", "import sys; print(sys.argv[1], sys.argv[2])", "PRE", "{prompt}"]
    )
    result = runner.run("hello-prompt", cwd=tmp_path)
    assert result.stdout.strip() == "PRE hello-prompt"


def test_prompt_placeholder_inside_larger_token_is_substituted(
    tmp_path: Path, python_bin: str
) -> None:
    """A placeholder embedded in a flag (e.g. --prompt={prompt}) must be
    substituted in place — not silently appended as an extra argument."""
    runner = AgentCliRunner(
        command=[
            python_bin,
            "-c",
            "import sys; print(sys.argv[1], len(sys.argv))",
            "--prompt={prompt}",
        ]
    )
    result = runner.run("hello-prompt", cwd=tmp_path)
    assert result.stdout.strip() == "--prompt=hello-prompt 2"


def test_nonzero_exit_is_not_ok(tmp_path: Path, python_bin: str) -> None:
    runner = AgentCliRunner(command=[python_bin, "-c", "raise SystemExit(3)"])
    result = runner.run("x", cwd=tmp_path)
    assert not result.ok
    assert result.returncode == 3


def test_non_utf8_output_is_decoded_with_replacement_not_crash(
    tmp_path: Path, python_bin: str
) -> None:
    """Child output must never crash the runner with UnicodeDecodeError,
    whatever the host locale decodes it as."""
    runner = AgentCliRunner(
        command=[python_bin, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe')"]
    )
    result = runner.run("x", cwd=tmp_path)
    assert result.ok
    assert result.stdout.startswith("ok ")


def test_timeout_kills_process_tree(tmp_path: Path, python_bin: str) -> None:
    # Parent spawns a child; both sleep far beyond the runner timeout. A
    # working tree-kill must reap both so communicate() can return.
    sleeper = (
        "import subprocess, sys, time\n"
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        "time.sleep(10)\n"
    )
    runner = AgentCliRunner(command=[python_bin, "-c", sleeper], timeout=1.0)
    start = time.monotonic()
    result = runner.run("x", cwd=tmp_path)
    elapsed = time.monotonic() - start
    assert result.timed_out
    assert not result.ok
    assert elapsed < 30, "process tree was not killed promptly"


def test_timeout_reaps_sigterm_ignoring_tree(tmp_path: Path, python_bin: str) -> None:
    """Processes that ignore SIGTERM must still be reaped within the grace
    budget — run() always returns a RunnerResult, never hangs forever."""
    stubborn = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.run([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    runner = AgentCliRunner(command=[python_bin, "-c", stubborn], timeout=1.0)
    start = time.monotonic()
    result = runner.run("x", cwd=tmp_path)
    elapsed = time.monotonic() - start
    assert result.timed_out
    assert not result.ok
    assert elapsed < 30, "SIGTERM-ignoring tree was not reaped promptly"
