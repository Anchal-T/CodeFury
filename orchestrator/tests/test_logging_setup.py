"""Tests for JSONL run logging (orchestrator.logging_setup)."""

import json
import threading
from pathlib import Path

from orchestrator.logging_setup import RunLogger, setup_logging


def read_events(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_setup_logging_creates_timestamped_run_file(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs")
    try:
        assert logger.path.parent == tmp_path / "logs"
        assert logger.path.name.startswith("run_")
        assert logger.path.suffix == ".jsonl"
        assert logger.path.is_file()
    finally:
        logger.close()


def test_event_appends_json_line_with_ts_and_fields(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "run.jsonl")
    try:
        logger.event("task_start", task_id="w-1", level=0)
    finally:
        logger.close()

    events = read_events(logger.path)
    assert len(events) == 1
    assert events[0]["event"] == "task_start"
    assert events[0]["task_id"] == "w-1"
    assert events[0]["level"] == 0
    assert events[0]["ts"].endswith("+00:00")  # timezone-aware UTC


def test_events_append_in_order_across_kinds(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "run.jsonl")
    try:
        logger.event("task_start", task_id="t1")
        logger.event("merge", task_id="t1")
        logger.event("retry", task_id="t2", attempt=2)
        logger.event("report", task_id="t3", tokens_used=250)
        logger.event("task_end", task_id="t3", status="failed")
    finally:
        logger.close()

    kinds = [e["event"] for e in read_events(logger.path)]
    assert kinds == ["task_start", "merge", "retry", "report", "task_end"]


def test_concurrent_writes_produce_whole_lines(tmp_path: Path) -> None:
    """Workers run in threads; interleaved writes must never tear a line."""
    logger = RunLogger(tmp_path / "run.jsonl")
    errors: list[Exception] = []

    def writer(worker: int) -> None:
        try:
            for n in range(25):
                logger.event("task_end", task_id=f"w{worker}-{n}", status="review")
        except Exception as exc:  # noqa: BLE001 — asserted below
            errors.append(exc)

    try:
        threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        logger.close()

    assert errors == []
    events = read_events(logger.path)
    assert len(events) == 100
    assert {e["task_id"] for e in events} == {f"w{w}-{n}" for w in range(4) for n in range(25)}


def test_context_manager_closes_and_is_idempotent(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "run.jsonl") as logger:
        logger.event("task_start", task_id="t1")
    logger.close()  # second close must be a no-op, not a crash
