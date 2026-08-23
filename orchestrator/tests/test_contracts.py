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


def test_task_dependencies_default_is_instance_isolated() -> None:
    a = Task(id="a", parent_id=None, level=1, goal="g", deliverable="d", status="pending")
    b = Task(id="b", parent_id=None, level=1, goal="g", deliverable="d", status="pending")
    a.dependencies.append("x")
    assert b.dependencies == [], "default list must not be shared across instances"


def test_report_blockers_default_is_instance_isolated() -> None:
    a = Report(task_id="a", agent="worker:a", summary="s", tests_passed=False, tokens_used=0)
    b = Report(task_id="b", agent="worker:b", summary="s", tests_passed=False, tokens_used=0)
    a.blockers.append("boom")
    assert b.blockers == [], "default list must not be shared across instances"


def test_conflict_blocker_prefix_is_shared_convention() -> None:
    assert isinstance(CONFLICT_BLOCKER_PREFIX, str) and CONFLICT_BLOCKER_PREFIX
