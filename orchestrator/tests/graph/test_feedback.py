"""Tests for retry feedback construction (Phase 10): a failed Report becomes
a capped prompt block so the next attempt starts from what went wrong."""

from orchestrator.contracts import Report
from orchestrator.graph.feedback import DEFAULT_MAX_CHARS, FEEDBACK_HEADER, build_feedback


def make_failed_report(**overrides: object) -> Report:
    base = Report(
        task_id="w-1",
        agent="worker:w-1",
        summary="worker exit=0, tests failed\ntest output tail:\nAssertionError: boom",
        diff_ref="orchestrator/worker-w-1",
        tests_passed=False,
        tokens_used=1200,
        blockers=["tests failed in worktree", "worker wrote BLOCKED.md"],
    )
    return base.model_copy(update=overrides) if overrides else base


def test_feedback_contains_header_guidance_blockers_and_summary() -> None:
    block = build_feedback(make_failed_report())
    assert block.startswith(FEEDBACK_HEADER)
    assert "previous attempt" in block.lower()
    assert "- tests failed in worktree" in block
    assert "- worker wrote BLOCKED.md" in block
    assert "AssertionError: boom" in block


def test_feedback_empty_report_yields_empty_string() -> None:
    """Nothing actionable (no blockers, no summary) → no block at all, so
    the retry prompt stays clean."""
    empty = make_failed_report(summary="", blockers=[])
    assert build_feedback(empty) == ""


def test_feedback_summary_only_still_renders() -> None:
    block = build_feedback(make_failed_report(blockers=[]))
    assert FEEDBACK_HEADER in block
    assert "Previous attempt summary:" in block


def test_feedback_is_capped_and_keeps_the_tail() -> None:
    """A pathological summary must not bloat every retry prompt: the block
    is hard-capped, and the cap eats the summary's head (its tail — where
    the failing-test output lives — survives, marked with an ellipsis)."""
    report = make_failed_report(summary="head " + "x" * (DEFAULT_MAX_CHARS * 4) + " TAIL")
    block = build_feedback(report)
    assert len(block) <= DEFAULT_MAX_CHARS
    assert "…" in block
    assert block.endswith(" TAIL")


def test_feedback_header_and_blockers_survive_the_cap() -> None:
    """The cap eats the summary, never the actionable header/blockers."""
    report = make_failed_report(
        summary="y" * (DEFAULT_MAX_CHARS * 4),
        blockers=["tests failed in worktree"],
    )
    block = build_feedback(report)
    assert block.startswith(FEEDBACK_HEADER)
    assert "- tests failed in worktree" in block
    assert len(block) <= DEFAULT_MAX_CHARS


def test_feedback_custom_cap_honored() -> None:
    report = make_failed_report(summary="z" * 500)
    block = build_feedback(report, max_chars=300)
    assert len(block) <= 300
    assert "…" in block
    assert block.endswith("z" * 10)


def test_feedback_degenerate_cap_still_respected() -> None:
    """A cap smaller than the header+guidance prefix cannot preserve them —
    the hard length cap still wins, marker or not."""
    report = make_failed_report(summary="z" * 500)
    block = build_feedback(report, max_chars=100)
    assert len(block) <= 100
