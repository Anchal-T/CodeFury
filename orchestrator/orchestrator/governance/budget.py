"""Token/cost tracking with hard caps (plan §3.6).

Counters increment in SQLite per LLM call; caps come from config.yaml
(per-level token budgets). No observability stack — JSONL logs only.
"""


class BudgetTracker:
    """Counts tokens used per level and enforces hard caps."""

    def record(self, level: int, tokens: int) -> None:
        raise NotImplementedError("Phase 6")

    def remaining(self, level: int) -> int:
        raise NotImplementedError("Phase 6")

    def exceeded(self, level: int) -> bool:
        raise NotImplementedError("Phase 6")
