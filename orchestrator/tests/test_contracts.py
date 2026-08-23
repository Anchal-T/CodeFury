"""Contract tests: Task/Report models and shared cross-level conventions."""

from orchestrator.contracts import CONFLICT_BLOCKER_PREFIX, Report, Task


def test_task_domain_defaults_to_none() -> None:
    task = Task(
        id="t-1",
        parent_id=None,
        level=2,
        goal="g",
        deliverable="d",
        status="pending",
    )
    assert task.domain is None


def test_task_domain_round_trips_through_json() -> None:
    task = Task(
        id="t-1", parent_id=None, level=2, goal="g", deliverable="d",
        status="pending", domain="backend",
    )
    restored = Task.model_validate_json(task.model_dump_json())
    assert restored.domain == "backend"


def test_conflict_blocker_prefix_is_shared_convention() -> None:
    assert isinstance(CONFLICT_BLOCKER_PREFIX, str) and CONFLICT_BLOCKER_PREFIX
