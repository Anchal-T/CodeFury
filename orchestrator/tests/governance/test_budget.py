"""Tests for orchestrator.governance.budget (per-level token caps)."""

from pathlib import Path

import pytest

from orchestrator.config import BudgetsConfig
from orchestrator.governance.budget import BUDGET_EXHAUSTED_PREFIX, BudgetTracker
from orchestrator.memory.store import StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_tracker(store: StateStore, **caps) -> BudgetTracker:
    return BudgetTracker(store, BudgetsConfig(**caps))


def test_record_accumulates_only_own_level(store: StateStore) -> None:
    tracker = make_tracker(store)
    tracker.record(0, 100)
    tracker.record(0, 50)
    tracker.record(1, 25)
    assert store.total_tokens_by_level(0) == 150
    assert store.total_tokens_by_level(1) == 25


def test_record_skips_zero_rows(store: StateStore) -> None:
    """Zero-token runs (crashes before any LLM output) add log noise only."""
    tracker = make_tracker(store)
    tracker.record(0, 0)
    assert store.total_tokens_by_level(0) == 0


def test_remaining_subtracts_usage_from_cap(store: StateStore) -> None:
    tracker = make_tracker(store, worker_tokens=100)
    tracker.record(0, 40)
    assert tracker.remaining(0) == 60


def test_remaining_none_when_uncapped(store: StateStore) -> None:
    tracker = make_tracker(store, worker_tokens=None)
    assert tracker.remaining(0) is None


def test_exceeded_true_at_and_past_cap(store: StateStore) -> None:
    """The cap is a hard maximum: hitting it exactly already blocks dispatch."""
    tracker = make_tracker(store, worker_tokens=100)
    assert not tracker.exceeded(0)
    tracker.record(0, 100)
    assert tracker.exceeded(0)
    tracker.record(0, 1)
    assert tracker.exceeded(0)


def test_levels_are_independent(store: StateStore) -> None:
    tracker = make_tracker(store, worker_tokens=10, manager_tokens=None)
    tracker.record(0, 10)
    assert tracker.exceeded(0)
    assert not tracker.exceeded(1)


def test_unknown_level_rejected(store: StateStore) -> None:
    tracker = make_tracker(store)
    with pytest.raises(ValueError, match="level"):
        tracker.exceeded(9)


def test_blocker_message_carries_level_role_used_and_cap(store: StateStore) -> None:
    tracker = make_tracker(store, worker_tokens=100)
    tracker.record(0, 100)
    message = tracker.blocker(0)
    assert message.startswith(BUDGET_EXHAUSTED_PREFIX)
    assert "level 0" in message
    assert "worker" in message
    assert "100/100" in message


def test_blocker_prefix_is_the_review_contract() -> None:
    """review_reports keys no-retry behavior off this exact prefix."""
    assert BUDGET_EXHAUSTED_PREFIX == "token budget exhausted"
