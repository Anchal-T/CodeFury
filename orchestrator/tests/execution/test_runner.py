"""Tests for the harness contract layer (Phase 9): Runner protocol,
RunnerResult token reporting, the agent-CLI token convention, and the
config → runner factory. No subprocess spawning here except where noted.
"""

from pathlib import Path

import pytest

from orchestrator.config import DEFAULT_WORKER_TIMEOUT_S, ExecutionConfig
from orchestrator.execution.cli_runner import AgentCliRunner
from orchestrator.execution.runner import (
    RUNNER_BUILDERS,
    Runner,
    RunnerResult,
    build_runner,
    tokens_from_output,
)


# -- token attribution convention (moved from graph.worker) ------------------


def test_tokens_last_marker_wins() -> None:
    assert tokens_from_output("TOKENS_USED: 100\nall done", "") == 100
    assert tokens_from_output("TOKENS_USED: 5\nretry", "warning\nTOKENS_USED: 9") == 9


def test_tokens_missing_or_malformed_is_zero() -> None:
    """A worker that never reported (crash, old agent CLI) costs nothing;
    mid-line mentions are not markers — the line must stand alone."""
    assert tokens_from_output("", "") == 0
    assert tokens_from_output("plain output only", "no marker") == 0
    assert tokens_from_output("TOKENS_USED: lots", "TOKENS_USED:") == 0
    assert tokens_from_output("log says TOKENS_USED: 12 inline", "") == 0


# -- RunnerResult contract ---------------------------------------------------


def test_runner_result_tokens_used_defaults_to_none() -> None:
    """None means 'this runner cannot report tokens'; callers fall back to
    the output convention. A reported zero stays a zero."""
    result = RunnerResult(returncode=0, stdout="", stderr="", timed_out=False, duration_s=0.0)
    assert result.tokens_used is None
    assert result.ok


def test_effective_tokens_prefers_runner_reported_value() -> None:
    """A backend with first-class attribution wins over the output marker."""
    result = RunnerResult(
        returncode=0, stdout="TOKENS_USED: 100", stderr="", timed_out=False,
        duration_s=0.0, tokens_used=300,
    )
    assert result.effective_tokens() == 300


def test_effective_tokens_falls_back_to_output_convention() -> None:
    result = RunnerResult(
        returncode=0, stdout="TOKENS_USED: 250", stderr="", timed_out=False, duration_s=0.0,
    )
    assert result.effective_tokens() == 250


def test_effective_tokens_reported_zero_stays_zero() -> None:
    """An explicit 0 is attribution ('this run cost nothing'), never a
    trigger to re-parse output."""
    result = RunnerResult(
        returncode=0, stdout="TOKENS_USED: 999", stderr="", timed_out=False,
        duration_s=0.0, tokens_used=0,
    )
    assert result.effective_tokens() == 0


def test_effective_tokens_no_report_no_marker_is_zero() -> None:
    result = RunnerResult(returncode=0, stdout="plain output", stderr="", timed_out=False, duration_s=0.0)
    assert result.effective_tokens() == 0


def test_cli_runner_is_a_runner() -> None:
    """The protocol is structural; the built-in CLI adapter satisfies it."""
    assert isinstance(AgentCliRunner(command=["agent"]), Runner)


# -- factory -----------------------------------------------------------------


def test_build_runner_defaults_to_cli_harness() -> None:
    runner = build_runner(ExecutionConfig())
    assert isinstance(runner, AgentCliRunner)
    assert runner.command == ["zcode"]
    assert runner.timeout == DEFAULT_WORKER_TIMEOUT_S


def test_build_runner_wires_command_and_timeout() -> None:
    execution = ExecutionConfig(
        harness="cli",
        worker_command=["agent", "--prompt={prompt}"],
        worker_timeout_s=61.5,
    )
    runner = build_runner(execution)
    assert isinstance(runner, AgentCliRunner)
    assert runner.command == ["agent", "--prompt={prompt}"]
    assert runner.timeout == 61.5


def test_build_runner_unknown_harness_names_it() -> None:
    """An unknown harness key must fail fast with the value named, not
    silently fall back to a different backend."""
    with pytest.raises(ValueError, match="nope"):
        build_runner(ExecutionConfig(harness="nope"))


def test_registry_is_extensible(monkeypatch: pytest.MonkeyPatch) -> None:
    """New harnesses plug in without touching the factory (open/closed).
    The registration is monkeypatched away so the suite stays pristine."""

    class MyRunner:
        def run(self, prompt: str, cwd: Path) -> RunnerResult:
            raise NotImplementedError

    monkeypatch.setitem(RUNNER_BUILDERS, "mine", lambda execution: MyRunner())
    runner = build_runner(ExecutionConfig(harness="mine"))
    assert isinstance(runner, MyRunner)
    assert isinstance(runner, Runner)
