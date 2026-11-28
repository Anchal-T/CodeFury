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


def test_prompt_prepends_knowledge_context_before_task() -> None:
    """Tier-1 memory (plan §3.5): the latest repo_map.md section rides along
    with the task so the worker plans with current project knowledge."""
    prompt = build_worker_prompt(
        make_task(),
        knowledge_context="## lead run lead-1 — 2026-08-24T00:00:00Z\n\nauth module merged",
    )
    context_pos = prompt.index("auth module merged")
    task_pos = prompt.index("Task (JSON)")
    assert "Project knowledge" in prompt
    assert context_pos < task_pos, "knowledge must precede the task payload"


def test_prompt_without_context_omits_the_section() -> None:
    prompt = build_worker_prompt(make_task(), knowledge_context="")
    assert "Project knowledge" not in prompt


def test_prompt_includes_retry_feedback_before_task() -> None:
    """Phase 10: on retries the previous attempt's feedback block rides
    between the knowledge header and the task payload."""
    prompt = build_worker_prompt(
        make_task(),
        knowledge_context="auth module merged last round",
        feedback="## Previous attempt feedback\n\n- tests failed in worktree",
    )
    assert "Previous attempt feedback" in prompt
    assert "tests failed in worktree" in prompt
    knowledge_pos = prompt.index("auth module merged")
    feedback_pos = prompt.index("Previous attempt feedback")
    task_pos = prompt.index("Task (JSON)")
    assert knowledge_pos < feedback_pos < task_pos


def test_prompt_without_feedback_omits_the_block() -> None:
    """First attempts (and every existing caller) see no feedback block."""
    prompt = build_worker_prompt(make_task())
    assert "Previous attempt feedback" not in prompt
