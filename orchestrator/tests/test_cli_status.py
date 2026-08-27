"""Tests for read-only CLI introspection helpers (orchestrator.cli_status)."""

import io
from pathlib import Path

from orchestrator.cli_status import (
    follow_file,
    newest_run_file,
    print_last_lines,
    render_task_tree,
    wait_for_newest_run_file,
)
from orchestrator.contracts import Task


def make_task(task_id: str, level: int, parent_id: str | None = None, domain: str | None = None) -> Task:
    return Task(
        id=task_id,
        parent_id=parent_id,
        level=level,
        goal=f"goal for {task_id}",
        deliverable="d",
        dependencies=[],
        status="review",
        assigned_to=None,
        domain=domain,
    )


def test_render_task_tree_indents_children_under_parents() -> None:
    tasks = [
        make_task("lead-a", 2, parent_id="epic-1"),
        make_task("epic-1", 3),
        make_task("w-1", 0, parent_id="mgr-a"),
        make_task("mgr-a", 1, parent_id="lead-a"),
    ]
    tree = render_task_tree(tasks)
    lines = tree.splitlines()
    assert lines[0].startswith("epic-1 ")
    assert lines[1].strip().startswith("lead-a ")
    assert lines.index([ln for ln in lines if ln.strip().startswith("lead-a ")][0]) < (
        lines.index([ln for ln in lines if ln.strip().startswith("mgr-a ")][0])
    )
    mgr_line = [ln for ln in lines if ln.strip().startswith("mgr-a ")][0]
    lead_line = [ln for ln in lines if ln.strip().startswith("lead-a ")][0]
    assert len(mgr_line) - len(mgr_line.lstrip()) > len(lead_line) - len(lead_line.lstrip())
    w_line = [ln for ln in lines if ln.strip().startswith("w-1 ")][0]
    assert len(w_line) - len(w_line.lstrip()) > len(mgr_line) - len(mgr_line.lstrip())


def test_render_task_tree_shows_status_domain_and_tokens() -> None:
    epic = make_task("epic-1", 3).model_copy(update={"status": "in_progress"})
    lead = make_task("lead-a", 2, parent_id="epic-1", domain="backend").model_copy(
        update={"status": "failed"}
    )
    tree = render_task_tree([epic, lead], tokens_by_task={"lead-a": 250})
    assert "[in_progress]" in tree
    assert "[failed]" in tree
    assert "backend" in tree
    assert "(250 tok)" in tree


def test_render_task_tree_treats_orphan_as_root() -> None:
    """A child whose parent row vanished still shows up — no silent loss."""
    tree = render_task_tree([make_task("w-orphan", 0, parent_id="ghost")])
    assert tree.startswith("w-orphan ")


def test_render_task_tree_truncates_long_goals() -> None:
    long_goal = "x" * 200
    task = make_task("w-1", 0).model_copy(update={"goal": long_goal})
    tree = render_task_tree([task])
    assert long_goal not in tree
    assert "x..." in tree


def test_newest_run_file_picks_latest_and_handles_missing_dir(tmp_path: Path) -> None:
    assert newest_run_file(tmp_path / "nope") is None
    logs = tmp_path / "logs"
    logs.mkdir()
    assert newest_run_file(logs) is None
    (logs / "run_20260101T000000Z.jsonl").write_text("old\n", encoding="utf-8")
    (logs / "run_20260102T000000Z.jsonl").write_text("new\n", encoding="utf-8")
    assert newest_run_file(logs).name == "run_20260102T000000Z.jsonl"


def test_print_last_lines_outputs_tail_and_returns_position(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = io.StringIO()
    pos = print_last_lines(path, 2, out)
    assert out.getvalue() == "two\nthree\n"
    assert pos == path.stat().st_size

    empty_out = io.StringIO()
    assert print_last_lines(path, 0, empty_out) == path.stat().st_size
    assert empty_out.getvalue() == ""


def test_follow_file_prints_appended_lines_then_stops(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text("first\n", encoding="utf-8")
    out = io.StringIO()
    pos = print_last_lines(path, 1, out)
    assert out.getvalue() == "first\n"

    with path.open("a", encoding="utf-8") as fh:
        fh.write("second\nthird\n")
    end = follow_file(path, pos, out, interval_s=0.01, max_polls=1)

    assert "second\nthird\n" in out.getvalue()
    assert end == path.stat().st_size


def test_follow_file_holds_back_partial_line_until_complete(tmp_path: Path) -> None:
    """A writer mid-line must not leak a torn JSON fragment to the reader."""
    path = tmp_path / "run.jsonl"
    path.write_text("", encoding="utf-8")
    out = io.StringIO()

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "task_start"')
    pos = follow_file(path, 0, out, interval_s=0.01, max_polls=1)
    assert "{" not in out.getvalue(), "partial line must be held back"

    with path.open("a", encoding="utf-8") as fh:
        fh.write(", \"task_id\": \"w-1\"}\nfourth\n")
    pos = follow_file(path, pos, out, interval_s=0.01, max_polls=1)

    assert out.getvalue() == '{"event": "task_start", "task_id": "w-1"}\nfourth\n'


def test_wait_for_newest_run_file_returns_after_max_polls(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    assert wait_for_newest_run_file(logs, interval_s=0.01, max_polls=2) is None
    (logs / "run_x.jsonl").write_text("e\n", encoding="utf-8")
    found = wait_for_newest_run_file(logs, interval_s=0.01, max_polls=2)
    assert found is not None and found.name == "run_x.jsonl"
