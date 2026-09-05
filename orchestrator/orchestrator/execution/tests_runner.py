"""Runs a repo's test command inside a worktree (plan §3.3, Phase 11 split).

Extracted from graph.worker so the worker pipeline stays under the ~300-line
cap; the execution layer owns every subprocess concern.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import PYTEST_NO_TESTS_EXIT_CODE

TEST_TIMEOUT_S = 600.0


@dataclass
class TestResult:
    passed: bool
    output: str
    timed_out: bool = False


def run_tests(test_command: list[str], cwd: Path, timeout: float = TEST_TIMEOUT_S) -> TestResult:
    """Run the test command in the worktree (list-form, shell=False).

    A timeout is a test failure, not an exception: the caller must always
    get a TestResult so a Report can be persisted.
    """
    try:
        proc = subprocess.run(
            test_command,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            passed=False,
            output=f"tests timed out after {timeout:.0f}s",
            timed_out=True,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode in (0, PYTEST_NO_TESTS_EXIT_CODE)
    return TestResult(passed=passed, output=output.strip())
