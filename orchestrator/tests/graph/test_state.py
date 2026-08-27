"""Tests for orchestrator.graph.state (fan-out safe reducers)."""

import operator
from typing import get_args, get_type_hints

from orchestrator.graph.state import OrchestratorState, merge_attempts


def test_merge_attempts_takes_max_per_key() -> None:
    assert merge_attempts({"a": 1, "b": 2}, {"a": 3, "c": 1}) == {"a": 3, "b": 2, "c": 1}
    assert merge_attempts({"a": 5}, {"a": 2}) == {"a": 5}


def test_merge_attempts_handles_empty_sides() -> None:
    assert merge_attempts({}, {"a": 1}) == {"a": 1}
    assert merge_attempts({"a": 1}, {}) == {"a": 1}


def test_state_fields_use_safe_reducers() -> None:
    hints = get_type_hints(OrchestratorState, include_extras=True)
    # Parallel Send branches each return partial updates; append-only lists
    # and a max-merge dict keep the merged state deterministic.
    assert get_args(hints["reports"])[1] is operator.add
    assert get_args(hints["worker_tasks"])[1] is operator.add
    assert get_args(hints["merged"])[1] is operator.add
    assert get_args(hints["attempts"])[1] is merge_attempts


def test_state_has_scalar_fields() -> None:
    hints = get_type_hints(OrchestratorState)
    for field in ("manager_task", "final_status", "dispatched"):
        assert field in hints
