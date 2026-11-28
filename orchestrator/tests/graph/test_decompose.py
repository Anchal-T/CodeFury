"""Tests for orchestrator.graph.decompose."""

from orchestrator.contracts import Task
from orchestrator.graph.decompose import (
    DomainDecomposer,
    SingleManagerDecomposer,
    StaticDecomposer,
    StaticDomainDecomposer,
    StaticEpicDecomposer,
)


def make_parent() -> Task:
    return Task(
        id="m-1",
        parent_id=None,
        level=1,
        goal="build feature X",
        deliverable="feature X works",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


def test_static_decomposer_creates_worker_children() -> None:
    decomposer = StaticDecomposer(sub_goals=["add module a", "add module b"])
    children = decomposer.decompose(make_parent())
    assert len(children) == 2
    for child in children:
        assert child.level == 0
        assert child.parent_id == "m-1"
        assert child.status == "pending"
        assert child.deliverable
    assert [child.goal for child in children] == ["add module a", "add module b"]


def test_child_ids_are_unique_even_for_duplicate_goals() -> None:
    children = StaticDecomposer(sub_goals=["same goal", "same goal"]).decompose(make_parent())
    ids = [child.id for child in children]
    assert len(set(ids)) == 2
    assert all(task_id.startswith("m-1-") for task_id in ids)


def test_domain_decomposer_is_a_static_decomposer() -> None:
    """One shared Static implementation; the Domain variant differs only in
    the child level it emits (1 = manager Tasks for lead input)."""
    assert StaticDomainDecomposer.child_level == 1
    assert StaticDecomposer.child_level == 0


def make_lead() -> Task:
    return Task(
        id="lead-1",
        parent_id=None,
        level=2,
        goal="ship the backend slice",
        deliverable="backend slice merged",
        dependencies=[],
        status="pending",
        assigned_to=None,
        domain="backend",
    )


def test_static_domain_decomposer_creates_manager_children() -> None:
    decomposer = StaticDomainDecomposer(
        manager_goals=["module A slice", "module B slice"]
    )
    children = decomposer.decompose(make_lead())
    assert len(children) == 2
    for child in children:
        assert child.level == 1
        assert child.parent_id == "lead-1"
        assert child.status == "pending"
        assert child.domain == "backend", "children inherit the lead's domain"
        assert child.deliverable
    assert [child.goal for child in children] == ["module A slice", "module B slice"]


def test_domain_decomposer_child_ids_are_unique_and_prefixed() -> None:
    children = StaticDomainDecomposer(manager_goals=["same", "same"]).decompose(make_lead())
    ids = [child.id for child in children]
    assert len(set(ids)) == 2
    assert all(task_id.startswith("lead-1-") for task_id in ids)


def test_domain_decomposer_satisfies_protocol() -> None:
    """StaticDomainDecomposer is structurally a DomainDecomposer."""
    decomposer: DomainDecomposer = StaticDomainDecomposer(manager_goals=[])
    assert callable(decomposer.decompose)


def make_epic() -> Task:
    return Task(
        id="epic-1",
        parent_id=None,
        level=3,
        goal="ship the whole thing",
        deliverable="everything merged",
        dependencies=[],
        status="pending",
        assigned_to=None,
    )


def test_static_epic_decomposer_creates_pending_approval_leads() -> None:
    decomposer = StaticEpicDecomposer(
        domain_goals=[("ship the auth api", "backend"), ("wire the pipeline", "infra")]
    )
    children = decomposer.decompose(make_epic())
    assert len(children) == 2
    for child in children:
        assert child.level == 2
        assert child.parent_id == "epic-1"
        assert child.status == "pending_approval", "leads wait for human approval"
        assert child.deliverable
    assert [child.goal for child in children] == ["ship the auth api", "wire the pipeline"]
    assert [child.domain for child in children] == ["backend", "infra"]


def test_epic_decomposer_child_ids_unique_and_prefixed() -> None:
    children = StaticEpicDecomposer(
        domain_goals=[("same goal", "backend"), ("same goal", "infra")]
    ).decompose(make_epic())
    ids = [child.id for child in children]
    assert len(set(ids)) == 2
    assert all(task_id.startswith("epic-1-") for task_id in ids)


def test_epic_decomposer_requires_domain_named_entries() -> None:
    import pytest

    with pytest.raises(ValueError):
        StaticEpicDecomposer(domain_goals=[("goal without domain", "")])


def test_single_manager_decomposer_yields_one_inheriting_manager() -> None:
    lead = make_lead()
    children = SingleManagerDecomposer().decompose(lead)
    assert len(children) == 1
    child = children[0]
    assert child.level == 1
    assert child.parent_id == "lead-1"
    assert child.goal == lead.goal
    assert child.domain == "backend", "manager inherits the lead's domain"
    assert child.status == "pending"


def test_all_static_decomposers_accept_optional_planning_context() -> None:
    """Phase 5 planning seam: decomposers may receive the latest knowledge
    section; deterministic fakes ignore it but must tolerate the kwarg."""
    context = "latest knowledge section text"
    assert StaticDecomposer(["a"]).decompose(make_parent(), context=context)
    assert StaticDomainDecomposer(["a"]).decompose(make_lead(), context=context)
    assert StaticEpicDecomposer([("g", "d")]).decompose(make_epic(), context=context)

    from orchestrator.graph.decompose import SingleWorkerDecomposer

    worker_children = SingleWorkerDecomposer().decompose(make_parent(), context=context)
    manager_children = SingleManagerDecomposer().decompose(make_lead(), context=context)
    assert len(worker_children) == 1
    assert len(manager_children) == 1
