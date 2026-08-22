"""Hard retry caps enforced in code, not just config (plan §10).

max_worker_retries / max_manager_escalations guard against uncontrolled
retry loops burning tokens: a task gets at most max_worker_retries + 1
attempts in total, then it fails for good.
"""

from __future__ import annotations

from orchestrator.config import Config

DEFAULT_MAX_WORKER_RETRIES = 2
DEFAULT_MAX_MANAGER_ESCALATIONS = 1


class RetryPolicy:
    """Decides whether a failed task may be retried or must escalate."""

    def __init__(
        self,
        max_worker_retries: int = DEFAULT_MAX_WORKER_RETRIES,
        max_manager_escalations: int = DEFAULT_MAX_MANAGER_ESCALATIONS,
    ) -> None:
        self.max_worker_retries = max_worker_retries
        self.max_manager_escalations = max_manager_escalations

    @classmethod
    def from_config(cls, config: Config) -> "RetryPolicy":
        return cls(
            max_worker_retries=config.retries.max_worker_retries,
            max_manager_escalations=config.retries.max_manager_escalations,
        )

    def can_retry(self, task_id: str, attempt: int) -> bool:
        """attempt is the number of attempts already made (0 = first try)."""
        return attempt < self.max_worker_retries

    def can_escalate(self, manager_id: str, escalations: int) -> bool:
        return escalations < self.max_manager_escalations
