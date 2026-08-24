"""Tests for orchestrator.graph.outcome_recorder (shared finalization)."""

from pathlib import Path

from orchestrator.contracts import Task
from orchestrator.graph.outcome_recorder import (
    INTEGRATION_FAILED_BLOCKER,
    IntegrationGate,
    OutcomeRecorder,
    RecordRequest,
)
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

import pytest


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


def make_task(task_id: str = "m-1") -> Task:
    return Task(
        id=task_id, parent_id=None, level=1, goal="g", deliverable="d",
        dependencies=[], status="in_progress", assigned_to=None,
    )


def test_record_ok_maps_to_ok_status(store: StateStore) -> None:
    recorder = OutcomeRecorder(store, agent_role="manager")
    report = recorder.record(make_task(), RecordRequest(ok=True, blockers=[], summary="done"))
    assert report.tests_passed
    assert report.agent == "manager:m-1"
    assert store.get_task("m-1").status == "review"


def test_record_failure_maps_to_failed_status(store: StateStore) -> None:
    recorder = OutcomeRecorder(store, agent_role="manager")
    report = recorder.record(
        make_task(),
        RecordRequest(ok=False, blockers=["worker w-2 exhausted retries"], summary="bad"),
    )
    assert not report.tests_passed
    assert report.blockers == ["worker w-2 exhausted retries"]
    assert store.get_task("m-1").status == "failed"


def test_integration_gate_runs_only_when_ok(
    store: StateStore, tmp_path: Path, python_bin: str
) -> None:
    gate = IntegrationGate(repo_root=tmp_path, test_command=[python_bin, "-c", "raise SystemExit(1)"])
    recorder = OutcomeRecorder(store, agent_role="manager", integration=gate)

    failing = recorder.record(make_task(), RecordRequest(ok=False, blockers=[], summary="bad"))
    assert not any(INTEGRATION_FAILED_BLOCKER in b for b in failing.blockers), (
        "gate must not run when the outcome is already failed"
    )

    gated = recorder.record(make_task("m-2"), RecordRequest(ok=True, blockers=[], summary="ok"))
    assert not gated.tests_passed
    assert INTEGRATION_FAILED_BLOCKER in gated.blockers
    assert "integration" in gated.summary


def test_passing_integration_gate_keeps_ok_status(
    store: StateStore, tmp_path: Path, python_bin: str
) -> None:
    gate = IntegrationGate(repo_root=tmp_path, test_command=[python_bin, "-c", "print('ok')"])
    recorder = OutcomeRecorder(store, agent_role="manager", integration=gate)
    report = recorder.record(make_task(), RecordRequest(ok=True, blockers=[], summary="ok"))
    assert report.tests_passed
    assert store.get_task("m-1").status == "review"


def test_record_emits_report_event(store: StateStore) -> None:
    """Every level's finalization lands one report event in the JSONL log."""

    class MemoryRunLogger:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def event(self, kind: str, **fields: object) -> None:
            self.events.append((kind, dict(fields)))

    runlog = MemoryRunLogger()
    recorder = OutcomeRecorder(store, agent_role="lead", runlog=runlog)  # type: ignore[arg-type]
    recorder.record(
        make_task("lead-1"),
        RecordRequest(ok=False, blockers=["boom"], summary="bad", tokens_used=42),
    )

    assert runlog.events == [
        (
            "report",
            {
                "task_id": "lead-1",
                "agent": "lead:lead-1",
                "status": "failed",
                "tokens_used": 42,
            },
        )
    ]


def test_knowledge_section_appended_when_configured(
    store: StateStore, tmp_path: Path
) -> None:
    doc = tmp_path / "domains" / "backend" / "repo_map.md"
    recorder = OutcomeRecorder(store, agent_role="lead", knowledge=KnowledgeDocs())
    recorder.record(
        make_task("lead-1"),
        RecordRequest(
            ok=True, blockers=[], summary="done",
            knowledge_path=doc, knowledge_title="lead run lead-1", knowledge_body="body text",
        ),
    )
    text = doc.read_text(encoding="utf-8")
    assert "## lead run lead-1" in text and "body text" in text

    # No knowledge target → no doc write, everything else unchanged.
    before = doc.read_text(encoding="utf-8")
    plain = OutcomeRecorder(store, agent_role="lead")
    plain.record(make_task("lead-2"), RecordRequest(ok=True, blockers=[], summary="done"))
    assert doc.read_text(encoding="utf-8") == before
