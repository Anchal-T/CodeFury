"""Level 3 — the Architect (plan §9 Phase 4): epic planning with a human
approval gate, epic finalization, and the approved-leads resume driver.

The approval gate is status-driven: planned domain leads sit in
``pending_approval`` until a human flips them (CLI ``approve``); the resume
driver then runs each *approved* lead through its lead graph. No graph
interrupts are needed — checkpoint-based pause/resume arrives in Phase 5.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from orchestrator.contracts import Report, Task
from orchestrator.graph.decompose import EpicDecomposer
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

#: Lead statuses that still need work before an epic can be finalized.
_UNRESOLVED = ("pending", "pending_approval", "in_progress")


def plan_epic(*, epic: Task, decomposer: EpicDecomposer, store: StateStore) -> list[Task]:
    """Move an epic from pending to review by planning its domain leads.

    Children are persisted as ``pending_approval`` — the plan is inert until
    a human approves each domain. An empty decomposition fails the epic
    immediately with an architect report.
    """
    epic.status = "in_progress"
    store.save_task(epic)
    children = decomposer.decompose(epic)
    if not children:
        epic.status = "failed"
        store.save_task(epic)
        store.save_report(
            Report(
                task_id=epic.id,
                agent=f"architect:{epic.id}",
                summary="epic produced no domain tasks",
                diff_ref=None,
                tests_passed=False,
                tokens_used=0,
                blockers=["epic produced no domain tasks"],
            )
        )
        return []
    for child in children:
        store.save_task(child)
    epic.status = "review"
    store.save_task(epic)
    return children


def finalize_epic(
    *,
    epic: Task,
    store: StateStore,
    knowledge: KnowledgeDocs,
    project_state: Path,
) -> Report | None:
    """Finalize the epic once every lead is resolved; None if not ready.

    All leads review/done → epic done; any failed lead → epic failed with
    one blocker per failure. Either way a timestamped PROJECT_STATE.md
    section records the outcome per domain.
    """
    leads = store.tasks_by_parent(epic.id)
    if not leads or any(lead.status in _UNRESOLVED for lead in leads):
        return None

    failed_leads = [lead for lead in leads if lead.status == "failed"]
    ok = not failed_leads
    blockers = [f"lead {lead.id} failed ({lead.domain})" for lead in failed_leads]
    epic.status = "done" if ok else "failed"
    store.save_task(epic)

    tokens = 0
    for lead in leads:
        lead_report = store.latest_report(lead.id)
        if lead_report:
            tokens += lead_report.tokens_used
    report = Report(
        task_id=epic.id,
        agent=f"architect:{epic.id}",
        summary=f"{len(leads) - len(failed_leads)}/{len(leads)} domain(s) shipped",
        diff_ref=None,
        tests_passed=ok,
        tokens_used=tokens,
        blockers=blockers,
    )
    store.save_report(report)

    body_lines = [f"epic: {epic.goal}"]
    for lead in leads:
        body_lines.append(f"- {lead.domain}: {lead.status} ({lead.id})")
    if blockers:
        body_lines.append("blockers: " + "; ".join(blockers))
    knowledge.append_section(project_state, title=f"epic {epic.id}", body="\n".join(body_lines))
    return report


def run_approved_leads(
    *,
    store: StateStore,
    epic: Task,
    lead_graph_factory: Callable[[Task], Any],
) -> list[dict]:
    """Run each approved lead of the epic, sequentially.

    ``pending_approval`` leads are skipped by query — the gate is enforced
    here, not by convention. A lead left ``in_progress`` by an interrupted
    run is reset to ``pending`` and re-run (it must have been approved to
    have started), so a killed session never deadlocks the epic. Returns one
    outcome dict per executed lead.
    """
    runnable: list[Task] = []
    for lead in store.tasks_by_parent(epic.id):
        if lead.status == "pending":
            runnable.append(lead)
        elif lead.status == "in_progress":
            store.set_task_status(lead.id, "pending")
            lead.status = "pending"
            runnable.append(lead)

    async def _run() -> list[dict]:
        outcomes: list[dict] = []
        for lead in runnable:
            graph = lead_graph_factory(lead)
            outcome = await graph.ainvoke({"lead_task": lead.model_dump()})
            outcomes.append(dict(outcome))
        return outcomes

    return asyncio.run(_run())
