"""Hard retry caps enforced in code, not just config (plan §10).

max_worker_retries / max_manager_escalations from config.yaml guard against
uncontrolled retry loops burning tokens.
"""


class RetryPolicy:
    """Decides whether a failed task may be retried or must escalate."""

    def can_retry(self, task_id: str, attempt: int) -> bool:
        raise NotImplementedError("Phase 2")

    def can_escalate(self, manager_id: str, escalations: int) -> bool:
        raise NotImplementedError("Phase 2")
