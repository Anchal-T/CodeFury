"""Tests for orchestrator.prompts."""

from orchestrator.contracts import Task
from orchestrator.prompts import build_worker_prompt


def make_task() -> Task:
    return Task(
        id="t-7",
        parent_id=None,
        level=0,
        goal="add greeting module",
        deliverable="greeting.py exists",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


def test_prompt_carries_task_fields_and_rules() -> None:
    prompt = build_worker_prompt(make_task())
    assert "t-7" in prompt
    assert "add greeting module" in prompt
    assert "greeting.py exists" in prompt
    assert "Task (JSON)" in prompt
    assert "Do NOT run git commit" in prompt
