"""Tests for orchestrator.governance.retry_policy (hard caps, plan §10)."""

from pathlib import Path

from orchestrator.config import load_config
from orchestrator.governance.retry_policy import RetryPolicy


def make_policy(max_worker_retries: int = 2) -> RetryPolicy:
    return RetryPolicy(max_worker_retries=max_worker_retries, max_manager_escalations=1)


def test_can_retry_within_cap() -> None:
    policy = make_policy(max_worker_retries=2)
    assert policy.can_retry("t1", attempt=0) is True
    assert policy.can_retry("t1", attempt=1) is True


def test_can_retry_denied_at_cap() -> None:
    policy = make_policy(max_worker_retries=2)
    assert policy.can_retry("t1", attempt=2) is False
    assert policy.can_retry("t1", attempt=99) is False


def test_zero_retries_allows_single_attempt_only() -> None:
    policy = make_policy(max_worker_retries=0)
    assert policy.can_retry("t1", attempt=0) is False


def test_can_escalate_caps_escalations() -> None:
    policy = make_policy()
    assert policy.can_escalate("m1", escalations=0) is True
    assert policy.can_escalate("m1", escalations=1) is False


def test_from_config_reads_retries_section(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "retries:\n  max_worker_retries: 3\n  max_manager_escalations: 2\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    policy = RetryPolicy.from_config(config)
    assert policy.max_worker_retries == 3
    assert policy.max_manager_escalations == 2
    assert policy.can_retry("t1", attempt=2) is True
    assert policy.can_retry("t1", attempt=3) is False
