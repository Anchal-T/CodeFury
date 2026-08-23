"""Tests for orchestrator.graph.decompose."""

from orchestrator.contracts import Task
from orchestrator.graph.decompose import (
    DomainDecomposer,
    StaticDecomposer,
    StaticDomainDecomposer,
    is_decomposer,
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


def test_is_decomposer_protocol() -> None:
    assert is_decomposer(StaticDecomposer(sub_goals=[]))
    assert not is_decomposer(object())


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
    decomposer: DomainDecomposer = StaticDomainDecomposer(manager_goals=[])
    assert is_decomposer(decomposer)
