"""Tests for the Architect (plan §9 Phase 4): epic planning with the human
approval gate, epic finalization, and the approved-leads resume driver."""

import asyncio
import typing
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


class RecordingEpicDecomposer:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def decompose(self, task: Task, context: str = "") -> list[Task]:
        self.contexts.append(context)
        return [
            task.model_copy(
                update={"id": "lead-a", "parent_id": task.id, "level": 2,
                        "domain": "backend", "status": "pending_approval"}
            )
        ]


def test_plan_epic_passes_planning_context_to_decomposer(store: StateStore) -> None:
    """Tier-1 memory feeds planning: the latest PROJECT_STATE section reaches
    the epic decomposer (the future LLM seam)."""
    decomposer = RecordingEpicDecomposer()
    leads = plan_epic(
        epic=make_epic(),
        decomposer=decomposer,  # type: ignore[arg-type]
        store=store,
        planning_context="PROJECT STATE MARKER PS-9",
    )
    assert len(leads) == 1
    assert decomposer.contexts == ["PROJECT STATE MARKER PS-9"]


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


class FakeVectorMemory:
    """Captures upserts; same interface as VectorMemory.upsert."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def upsert(self, text: str, **metadata: object) -> None:
        self.entries.append((text, dict(metadata)))


def test_finalize_epic_indexes_project_state_section(
    store: StateStore, project_state: Path
) -> None:
    """The PROJECT_STATE.md section the epic writes is Tier-3 indexed too."""
    epic, leads = _plan_two_leads(store)
    for lead in leads:
        store.set_task_status(lead.id, "done")
    memory = FakeVectorMemory()

    report = finalize_epic(
        epic=store.get_task("epic-1"),
        store=store,
        knowledge=KnowledgeDocs(),
        project_state=project_state,
        memory=memory,
    )

    assert report is not None and report.tests_passed
    assert len(memory.entries) == 1
    text, meta = memory.entries[0]
    assert "backend: done" in text
    assert meta["source"] == "knowledge"
    assert str(project_state) in str(meta["ref"])
    assert meta["title"] == "epic epic-1"


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
        async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
            return {"outcome": "review", "lead_id": state["lead_task"]["id"]}

    def factory(lead: Task, _checkpointer) -> StubGraph:
        received.append(lead.id)
        return StubGraph()

    outcomes = run_approved_leads(
        store=store, epic=store.get_task("epic-1"), lead_graph_factory=factory
    )
    assert received == [leads[0].id]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "review"


def test_run_approved_leads_recovers_interrupted_leads(store: StateStore) -> None:
    """Without a checkpointer (Phase-4 fallback), a lead left in_progress by a
    killed run is reset to pending and re-run — otherwise the epic could
    never finalize and nothing would report why."""
    epic, leads = _plan_two_leads(store)
    store.set_task_status(leads[0].id, "pending")      # approved, not yet started
    store.set_task_status(leads[1].id, "in_progress")  # interrupted mid-run

    received: list[Task] = []

    class StubGraph:
        async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
            return {"outcome": "review"}

    def factory(lead: Task, _checkpointer) -> StubGraph:
        received.append(lead)
        return StubGraph()

    outcomes = run_approved_leads(
        store=store, epic=store.get_task("epic-1"), lead_graph_factory=factory
    )
    assert [lead.id for lead in received] == [leads[0].id, leads[1].id]
    assert all(lead.status == "pending" for lead in received), (
        "interrupted leads are reset before re-dispatch"
    )
    assert len(outcomes) == 2


class _TrivialState(typing.TypedDict, total=False):
    n: int


class RecordingGraph:
    """Stub lead graph capturing (input, thread_id) per invocation."""

    def __init__(self, sink: list[tuple[dict | None, str]]) -> None:
        self.sink = sink

    async def ainvoke(self, state: dict | None, config: dict | None = None) -> dict:
        self.sink.append((state, config["configurable"]["thread_id"]))
        return {"outcome": "review"}


def _seed_checkpoint(store: StateStore, thread_id: str) -> None:
    """Write one real checkpoint onto a thread via a throwaway graph."""
    from langgraph.constants import END, START
    from langgraph.graph import StateGraph

    async def _seed() -> None:
        builder = StateGraph(_TrivialState)
        builder.add_node("noop", lambda state: {"n": state.get("n", 0)})
        builder.add_edge(START, "noop")
        builder.add_edge("noop", END)
        async with store.open_checkpointer() as saver:
            await builder.compile(checkpointer=saver).ainvoke(
                {"n": 0}, {"configurable": {"thread_id": thread_id}}
            )

    asyncio.run(_seed())


def test_run_approved_leads_resumes_interrupted_lead_from_checkpoint(
    store: StateStore,
) -> None:
    """An in_progress lead whose thread holds a checkpoint is resumed with
    input=None — never restarted, never reset."""
    epic, leads = _plan_two_leads(store)
    lead = leads[0]
    store.set_task_status(lead.id, "in_progress")  # killed mid-run last session
    _seed_checkpoint(store, f"lead:{lead.id}")

    received: list[tuple[dict | None, str]] = []
    outcomes = run_approved_leads(
        store=store,
        epic=store.get_task("epic-1"),
        lead_graph_factory=lambda _lead, _cp: RecordingGraph(received),
    )

    assert len(outcomes) == 1
    state, thread = received[0]
    assert thread == f"lead:{lead.id}", "stable thread id derived from the task id"
    assert state is None, "resume passes None so LangGraph continues the saved thread"
    assert store.get_task(lead.id).status == "review", (
        "driver reconciles stored status from the returned outcome"
    )


def test_run_approved_leads_starts_fresh_lead_with_task_input(
    store: StateStore,
) -> None:
    """A lead with no checkpoint yet gets the standard initial invocation."""
    epic, leads = _plan_two_leads(store)
    store.set_task_status(leads[0].id, "pending")
    expected_input = {"lead_task": store.get_task(leads[0].id).model_dump()}

    received: list[tuple[dict | None, str]] = []
    run_approved_leads(
        store=store,
        epic=store.get_task("epic-1"),
        lead_graph_factory=lambda _lead, _cp: RecordingGraph(received),
    )

    state, thread = received[0]
    assert thread == f"lead:{leads[0].id}"
    assert state == expected_input
