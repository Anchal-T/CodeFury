"""Harness contract layer (Phase 9): everything above this module depends
on the :class:`Runner` protocol, never on a concrete worker backend.

A *harness* is whatever executes one Worker task — today an agent-CLI
subprocess (:class:`AgentCliRunner`), tomorrow an SDK/API backend. The
module also owns the agent-CLI token-attribution convention and the
config → runner factory so adding a backend means registering a builder,
not editing call sites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from orchestrator.config import ExecutionConfig

#: Worker output convention for token attribution: a standalone
#: 'TOKENS_USED: <n>' line; the last marker wins when retries append output.
TOKENS_USED_PATTERN = re.compile(r"^TOKENS_USED:\s*(\d+)\s*$", re.MULTILINE)


def tokens_from_output(*outputs: str) -> int:
    """Extract reported token usage from worker output; absent/malformed → 0."""
    matches = TOKENS_USED_PATTERN.findall("\n".join(outputs))
    return int(matches[-1]) if matches else 0


@dataclass
class RunnerResult:
    """Outcome of one worker run.

    ``tokens_used`` is what the runner itself could report; ``None`` means
    the backend has no first-class attribution, in which case
    :meth:`effective_tokens` falls back to the agent-CLI output convention.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    tokens_used: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def effective_tokens(self) -> int:
        """Token spend for this run: the runner's own number if it reported
        one (an explicit 0 is attribution, not absence), else the output
        convention, else 0."""
        if self.tokens_used is not None:
            return self.tokens_used
        return tokens_from_output(self.stdout, self.stderr)


@runtime_checkable
class Runner(Protocol):
    """Anything that can execute one worker prompt inside a directory."""

    def run(self, prompt: str, cwd: Path) -> RunnerResult: ...


Builder = Callable[["ExecutionConfig"], Runner]

#: Harness name (config key ``execution.harness``) → builder.
RUNNER_BUILDERS: dict[str, Builder] = {}


def register_runner(name: str, builder: Builder) -> None:
    """Register (or replace) the builder for a harness kind."""
    RUNNER_BUILDERS[name] = builder


def build_runner(execution: ExecutionConfig) -> Runner:
    """Instantiate the configured harness; unknown names fail fast."""
    if "cli" not in RUNNER_BUILDERS:
        # Built-in registration is lazy to keep import direction one-way:
        # cli_runner imports this module's contract types.
        from orchestrator.execution.cli_runner import AgentCliRunner

        register_runner(
            "cli",
            lambda exec_cfg: AgentCliRunner(
                command=exec_cfg.worker_command,
                timeout=exec_cfg.worker_timeout_s,
            ),
        )
    builder = RUNNER_BUILDERS.get(execution.harness)
    if builder is None:
        known = ", ".join(sorted(RUNNER_BUILDERS))
        raise ValueError(
            f"unknown execution.harness {execution.harness!r} (known: {known})"
        )
    return builder(execution)
