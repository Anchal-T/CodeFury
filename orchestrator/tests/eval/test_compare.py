"""Tests for the eval comparison report (Phase 12): old-vs-new metrics per
task plus aggregates, rendered for the console and exportable as JSON."""

import json

from orchestrator.contracts import Report
from orchestrator.eval.compare import TaskComparison, compare, render, report_to_json
from orchestrator.eval.replay import ReplayResult
from orchestrator.eval.source import SourceSnapshot


def make_report(task_id: str, passed: bool, tokens: int) -> Report:
    return Report(
        task_id=task_id,
        agent=f"worker:{task_id}",
        summary="s",
        tests_passed=passed,
        tokens_used=tokens,
        blockers=[] if passed else ["tests failed"],
    )


def make_snapshot() -> SourceSnapshot:
    return SourceSnapshot(
        tasks=[],
        reports={
            "w-1": make_report("w-1", True, 1000),
            "w-2": make_report("w-2", False, 900),
            "w-3": make_report("w-3", True, 1000),
        },
    )


def make_results() -> list[ReplayResult]:
    return [
        ReplayResult(task_id="w-1", report=make_report("w-1", True, 1000)),
        ReplayResult(task_id="w-2", report=make_report("w-2", True, 1100)),
        ReplayResult(task_id="w-3", report=None, error="pipeline crashed"),
    ]


def test_compare_pairs_old_and_new_metrics() -> None:
    report = compare(make_snapshot(), make_results())
    by_id = {c.task_id: c for c in report.comparisons}
    assert by_id["w-1"].old_passed is True and by_id["w-1"].new_passed is True
    assert by_id["w-2"].old_tokens == 900 and by_id["w-2"].new_tokens == 1100
    assert by_id["w-3"].new_passed is None
    assert "crashed" in by_id["w-3"].note


def test_pass_rates_count_comparable_tasks() -> None:
    """The denominator is tasks with an old report; a crashed replay counts
    as a new failure, never silently disappears (w-3: old pass, new —)."""
    report = compare(make_snapshot(), make_results())
    assert report.old_passes == 2 and report.compared == 3
    assert report.new_passes == 2, "w-1 and w-2 pass; crashed w-3 counts as failure"


def test_render_shows_tasks_and_aggregates() -> None:
    text = render(compare(make_snapshot(), make_results()))
    assert "w-1" in text and "w-2" in text and "w-3" in text
    assert "2/3 → 2/3" in text
    assert "2900" in text and "2100" in text


def test_json_round_trip_carries_the_data() -> None:
    payload = json.loads(report_to_json(compare(make_snapshot(), make_results())))
    assert payload["compared"] == 3
    assert payload["old_passes"] == 2 and payload["new_passes"] == 2
    assert {c["task_id"] for c in payload["comparisons"]} == {"w-1", "w-2", "w-3"}


def test_empty_comparison_renders_without_crash() -> None:
    report = compare(SourceSnapshot(tasks=[], reports={}), [])
    assert report.compared == 0
    text = render(report)
    assert "0/0" in text
    assert json.loads(report_to_json(report))["compared"] == 0


def test_task_comparison_defaults() -> None:
    bare = TaskComparison(task_id="w-9")
    assert bare.old_passed is None and bare.new_tokens == 0 and bare.note == ""
