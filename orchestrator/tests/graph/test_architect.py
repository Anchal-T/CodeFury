"""Tests for the Architect (plan §9 Phase 4): epic planning with the human
approval gate, epic finalization, and the approved-leads resume driver."""

import asyncio
from pathlib import Path

import pytest

from orchestrator.contracts import Task
from orchestrator.graph.architect import finalize_epic, plan_epic, run_approved_leads
from orchestrator.graph.decompose import StaticEpicDecomposer
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore


def make_epic() -> Task:
    return Task(
        id="epic-1",
        parent_id=None,
        level=3,
        goal="ship the platform",
        deliverable="all domains merged",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "data" / "orchestrator.db")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture()
def project_state(tmp_path: Path) -> Path:
    return tmp_path / "domains" / "PROJECT_STATE.md"


def test_plan_epic_creates_pending_approval_leads(store: StateStore) -> None:
    epic = make_epic()
    leads = plan_epic(
        epic=epic,
        decomposer=StaticEpicDecomposer([("ship auth api", "backend"), ("wire pipeline", "infra")]),
        store=store,
    )
    assert len(leads) == 2
    for lead in leads:
        assert lead.level == 2
        assert lead.parent_id == "epic-1"
        assert lead.status == "pending_approval"
    assert [lead.domain for lead in leads] == ["backend", "infra"]
    assert store.get_task("epic-1").status == "review"
    assert [t.id for t in store.tasks_by_parent("epic-1")] == [lead.id for lead in leads]


def test_plan_epic_with_no_domains_fails_the_epic(store: StateStore) -> None:
    epic = make_epic()
    leads = plan_epic(epic=epic, decomposer=StaticEpicDecomposer([]), store=store)
    assert leads == []
    assert store.get_task("epic-1").status == "failed"
    report = store.latest_report("epic-1")
    assert report is not None
    assert report.agent == "architect:epic-1"
    assert not report.tests_passed
    assert any("no domain tasks" in b for b in report.blockers)


def _plan_two_leads(store: StateStore) -> tuple[Task, list[Task]]:
    epic = make_epic()
    leads = plan_epic(
        epic=epic,
        decomposer=StaticEpicDecomposer([("ship auth api", "backend"), ("wire pipeline", "infra")]),
        store=store,
    )
    return epic, leads


def test_finalize_epic_done_when_all_leads_resolved(
    store: StateStore, project_state: Path
) -> None:
    epic, leads = _plan_two_leads(store)
    for lead in leads:
        store.set_task_status(lead.id, "review")

    report = finalize_epic(
        epic=store.get_task("epic-1"),
        store=store,
        knowledge=KnowledgeDocs(),
        project_state=project_state,
    )
    assert report is not None
    assert report.tests_passed
    assert store.get_task("epic-1").status == "done"
    assert report.agent == "architect:epic-1"
    text = project_state.read_text(encoding="utf-8")
    assert "epic-1" in text and "backend" in text and "infra" in text


def test_finalize_epic_failed_when_any_lead_failed(
    store: StateStore, project_state: Path
) -> None:
    epic, leads = _plan_two_leads(store)
    store.set_task_status(leads[0].id, "review")
    store.set_task_status(leads[1].id, "failed")

    report = finalize_epic(
        epic=store.get_task("epic-1"),
        store=store,
        knowledge=KnowledgeDocs(),
        project_state=project_state,
    )
    assert report is not None
    assert not report.tests_passed
    assert any(leads[1].id in b for b in report.blockers)
    assert store.get_task("epic-1").status == "failed"


def test_finalize_epic_returns_none_while_leads_unresolved(
    store: StateStore, project_state: Path
) -> None:
    epic, leads = _plan_two_leads(store)
    store.set_task_status(leads[0].id, "review")
    # leads[1] still pending_approval → epic is not finalizable yet

    report = finalize_epic(
        epic=store.get_task("epic-1"),
        store=store,
        knowledge=KnowledgeDocs(),
        project_state=project_state,
    )
    assert report is None
    assert store.get_task("epic-1").status == "review", "epic untouched while awaiting leads"


def test_run_approved_leads_runs_only_approved(store: StateStore) -> None:
    epic, leads = _plan_two_leads(store)
    store.set_task_status(leads[0].id, "pending")  # approved
    # leads[1] stays pending_approval → must NOT run

    received: list[str] = []

    class StubGraph:
        async def ainvoke(self, state: dict) -> dict:
            return {"outcome": "review", "lead_id": state["lead_task"]["id"]}

    def factory(lead: Task) -> StubGraph:
        received.append(lead.id)
        return StubGraph()

    outcomes = run_approved_leads(
        store=store, epic=store.get_task("epic-1"), lead_graph_factory=factory
    )
    assert received == [leads[0].id]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "review"
