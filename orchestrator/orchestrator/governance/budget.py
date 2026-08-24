"""Token/cost tracking with hard caps (plan §3.6).

Counters increment in SQLite per LLM call; caps come from config.yaml
(per-level token budgets). No observability stack — JSONL logs only.

The tracker is pure policy over StateStore queries: rows land in the
existing ``token_usage`` table at the level that actually spends (today
level-0 workers), and a cap compares the stored per-level sum. Exhaustion
surfaces as a blocker string — never a hang — mirroring the concurrency
lesson from Phase 2.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from orchestrator.config import BudgetsConfig
from orchestrator.memory.store import StateStore

#: Blocker-string convention marking an exhausted budget; review logic keys
#: no-retry behavior off this prefix (mirrors CONFLICT_BLOCKER_PREFIX).
BUDGET_EXHAUSTED_PREFIX = "token budget exhausted"

#: Task level → BudgetsConfig field holding that level's cap.
_LEVEL_BUDGET_KEYS = {
    0: "worker_tokens",
    1: "manager_tokens",
    2: "domain_lead_tokens",
    3: "architect_tokens",
}


class BudgetTracker:
    """Counts tokens used per level and enforces hard caps."""

    def __init__(self, store: StateStore, budgets: BudgetsConfig) -> None:
        self.store = store
        self.budgets = budgets
        self._dispatch_lock = RLock()

    def _cap(self, level: int) -> int | None:
        try:
            key = _LEVEL_BUDGET_KEYS[level]
        except KeyError:
            raise ValueError(f"unknown task level: {level}") from None
        return getattr(self.budgets, key)

    def record(self, level: int, tokens: int) -> None:
        """Attribute real spend to a level; zero-token runs add no row."""
        with self._dispatch_lock:
            if tokens:
                self.store.add_token_usage(level, tokens)

    @contextmanager
    def dispatch(self, level: int) -> Iterator[bool]:
        """Serialize capped dispatches around their usage accounting.

        Token usage is reported only after a worker exits, so the gate and the
        subsequent record must not be separated across concurrent workers.
        Uncapped levels retain their normal parallel behavior.
        """
        if self._cap(level) is None:
            yield True
            return
        with self._dispatch_lock:
            yield not self.exceeded(level)

    def remaining(self, level: int) -> int | None:
        """Tokens left before the cap; ``None`` when the level is uncapped."""
        cap = self._cap(level)
        if cap is None:
            return None
        return cap - self.store.total_tokens_by_level(level)

    def exceeded(self, level: int) -> bool:
        remaining = self.remaining(level)
        return remaining is not None and remaining <= 0

    def blocker(self, level: int) -> str:
        """The blocker string a gated dispatch puts into its Report."""
        cap = self._cap(level)
        used = self.store.total_tokens_by_level(level)
        role = _LEVEL_BUDGET_KEYS[level].removesuffix("_tokens")
        return (
            f"{BUDGET_EXHAUSTED_PREFIX} for level {level} ({role}): "
            f"{used}/{cap} tokens used"
        )
