"""Tests for orchestrator.execution.tests_runner (Phase 11 split from
graph.worker): the worktree test gate — a timeout is a failure, never an
exception, so a Report can always be persisted."""

from pathlib import Path

from orchestrator.config import PYTEST_NO_TESTS_EXIT_CODE
from orchestrator.execution import tests_runner


def test_passing_command_returns_passed(python_bin: str, tmp_path: Path) -> None:
    result = tests_runner.run_tests([python_bin, "-c", "print('tests ok')"], cwd=tmp_path)
    assert result.passed
    assert not result.timed_out
    assert "tests ok" in result.output


def test_failing_command_returns_failure_output(python_bin: str, tmp_path: Path) -> None:
    result = tests_runner.run_tests(
        [python_bin, "-c", "import sys; print('boom', file=sys.stderr); raise SystemExit(1)"],
        cwd=tmp_path,
    )
    assert not result.passed
    assert "boom" in result.output


def test_pytest_no_tests_collected_exit_code_passes(python_bin: str, tmp_path: Path) -> None:
    """Exit code 5 means 'no tests collected' — not a failure for us."""
    assert PYTEST_NO_TESTS_EXIT_CODE == 5
    result = tests_runner.run_tests([python_bin, "-c", "raise SystemExit(5)"], cwd=tmp_path)
    assert result.passed


def test_timeout_is_a_failed_result_not_an_exception(python_bin: str, tmp_path: Path) -> None:
    result = tests_runner.run_tests(
        [python_bin, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=1.0,
    )
    assert not result.passed
    assert result.timed_out
    assert "timed out" in result.output


def test_test_result_defaults_not_timed_out() -> None:
    assert tests_runner.TestResult(passed=True, output="ok").timed_out is False
