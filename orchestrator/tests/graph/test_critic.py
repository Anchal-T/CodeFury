"""Tests for the deterministic report critic (Phase 11): quality heuristics
over a worker Report and its worktree diff, complementing tests_passed."""

from orchestrator.config import CriticConfig
from orchestrator.contracts import Report
from orchestrator.graph.critic import criticize


def make_report(**overrides: object) -> Report:
    base = Report(
        task_id="w-1",
        agent="worker:w-1",
        summary="done",
        diff_ref="orchestrator/worker-w-1",
        tests_passed=True,
        tokens_used=10,
        blockers=[],
    )
    return base.model_copy(update=overrides) if overrides else base


DEFAULTS = CriticConfig()


def test_clean_success_has_no_warnings() -> None:
    critique = criticize(
        make_report(), changed_files=["src/app.py"], blocked=False, config=DEFAULTS
    )
    assert critique.warnings == []


def test_every_warning_carries_the_critic_prefix() -> None:
    critique = criticize(
        make_report(), changed_files=["tests/test_app.py"], blocked=False, config=DEFAULTS
    )
    assert critique.warnings
    assert all(w.startswith("critic:") for w in critique.warnings)


def test_modified_test_files_warn() -> None:
    critique = criticize(
        make_report(),
        changed_files=["src/app.py", "tests/test_app.py"],
        blocked=False,
        config=DEFAULTS,
    )
    assert any("test files" in w and "tests/test_app.py" in w for w in critique.warnings)


def test_nested_test_dirs_count_as_test_files() -> None:
    critique = criticize(
        make_report(), changed_files=["pkg/tests/test_deep.py"], blocked=False, config=DEFAULTS
    )
    assert any("test files" in w for w in critique.warnings)


def test_forbidden_paths_warn() -> None:
    config = CriticConfig(forbidden_globs=[".env*"])
    critique = criticize(
        make_report(), changed_files=["src/app.py", ".env.local"], blocked=False, config=config
    )
    assert any("forbidden" in w and ".env.local" in w for w in critique.warnings)


def test_excessive_churn_warns() -> None:
    config = CriticConfig(max_changed_files=2)
    critique = criticize(
        make_report(),
        changed_files=[f"f{i}.py" for i in range(3)],
        blocked=False,
        config=config,
    )
    assert any("3 files" in w and "cap 2" in w for w in critique.warnings)


def test_success_with_empty_diff_warns() -> None:
    critique = criticize(make_report(), changed_files=[], blocked=False, config=DEFAULTS)
    assert any("empty" in w for w in critique.warnings)


def test_empty_diff_with_failure_is_not_flagged() -> None:
    """A failed run with no diff is sad but consistent — no critic noise."""
    report = make_report(
        tests_passed=False, blockers=["tests failed in worktree"], summary="failed"
    )
    critique = criticize(report, changed_files=[], blocked=False, config=DEFAULTS)
    assert critique.warnings == []


def test_passing_report_with_empty_summary_warns() -> None:
    report = make_report(summary="   ")
    critique = criticize(
        report, changed_files=["src/app.py"], blocked=False, config=DEFAULTS
    )
    assert any("empty summary" in w for w in critique.warnings)


def test_blocked_marker_despite_passing_report_warns() -> None:
    critique = criticize(
        make_report(), changed_files=["src/app.py"], blocked=True, config=DEFAULTS
    )
    assert any("BLOCKED.md" in w for w in critique.warnings)


def test_blocked_marker_reflected_in_blockers_not_double_flagged() -> None:
    """The worker pipeline already turns BLOCKED.md into a blocker; the
    inconsistency heuristic must stay quiet when it is reported properly."""
    report = make_report(
        tests_passed=False, blockers=["worker wrote BLOCKED.md"], summary="failed"
    )
    critique = criticize(
        report, changed_files=["src/app.py"], blocked=True, config=DEFAULTS
    )
    assert not any("despite" in w for w in critique.warnings)
