"""Level 3 — the Architect (plan §9 Phase 4): epic planning with a human
approval gate, epic finalization, and the approved-leads resume driver.

The approval gate is status-driven: planned domain leads sit in
``pending_approval`` until a human flips them (CLI ``approve``); the resume
driver then runs each *approved* lead through its lead graph, resuming
checkpointed threads after a killed session (Phase 5).
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


def plan_epic(
    *, epic: Task, decomposer: EpicDecomposer, store: StateStore, planning_context: str = ""
) -> list[Task]:
    """Move an epic from pending to review by planning its domain leads.

    Children are persisted as ``pending_approval`` — the plan is inert until
    a human approves each domain. An empty decomposition fails the epic
    immediately with an architect report. ``planning_context`` carries the
    latest PROJECT_STATE.md section for LLM-backed decomposers (Phase 5).
    """
    epic.status = "in_progress"
    store.save_task(epic)
    children = decomposer.decompose(epic, context=planning_context)
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
    runlog=None,
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
    if runlog is not None:
        runlog.event(
            "report",
            task_id=epic.id,
            agent=report.agent,
            status=epic.status,
            tokens_used=tokens,
        )

    body_lines = [f"epic: {epic.goal}"]
    for lead in leads:
        body_lines.append(f"- {lead.domain}: {lead.status} ({lead.id})")
    if blockers:
        body_lines.append("blockers: " + "; ".join(blockers))
    knowledge.append_section(project_state, title=f"epic {epic.id}", body="\n".join(body_lines))
    return report


def thread_config(lead: Task) -> dict:
    """Stable per-lead LangGraph thread id.

    Task ids are global primary keys in SQLite, so the same physical lead
    maps to the same checkpointed thread in every process — the seam that
    makes killed-session resume deterministic.
    """
    return {"configurable": {"thread_id": f"lead:{lead.id}"}}


def run_approved_leads(
    *,
    store: StateStore,
    epic: Task,
    lead_graph_factory: Callable[[Task, Any], Any],
) -> list[dict]:
    """Run each approved lead of the epic, sequentially, resuming threads.

    ``pending_approval`` leads are skipped by query — the gate is enforced
    here, not by convention.

    The driver owns the checkpointer lifecycle (AsyncSqliteSaver on the
    store's db file — langgraph 1.x requires async checkpointers and a
    running loop at construction). A lead left ``in_progress`` by a killed
    session whose thread holds a checkpoint resumes it (``ainvoke(None,
    ...)``) instead of restarting; without a checkpoint an interrupted lead
    is reset to ``pending`` and started fresh, so a kill can never deadlock
    the epic. After each run the driver reconciles the lead's stored status
    from the returned outcome, closing the crash window where the graph
    finished but the terminal status never reached SQLite. Returns one
    outcome dict per executed lead.
    """
    children = store.tasks_by_parent(epic.id)
    if not any(lead.status in ("pending", "in_progress") for lead in children):
        return []

    async def _run() -> list[dict]:
        outcomes: list[dict] = []
        async with store.open_checkpointer() as checkpointer:
            plan: list[tuple[Task, bool]] = []  # (lead, resume?)
            for lead in children:
                if lead.status == "pending":
                    plan.append((lead, False))
                elif lead.status == "in_progress":
                    resumable = await checkpointer.aget_tuple(thread_config(lead)) is not None
                    if not resumable:
                        store.set_task_status(lead.id, "pending")
                        lead.status = "pending"
                    plan.append((lead, resumable))
            for lead, resume in plan:
                graph = lead_graph_factory(lead, checkpointer)
                config = thread_config(lead)
                if resume:
                    outcome = dict(await graph.ainvoke(None, config=config))
                else:
                    outcome = dict(
                        await graph.ainvoke({"lead_task": lead.model_dump()}, config=config)
                    )
                _reconcile_status(store, lead, outcome)
                outcomes.append(outcome)
        return outcomes

    return asyncio.run(_run())


def _reconcile_status(store: StateStore, lead: Task, outcome: dict) -> None:
    """Align the stored lead status with the graph's terminal outcome."""
    terminal = outcome.get("outcome")
    if terminal in ("review", "failed"):
        current = store.get_task(lead.id)
        if current is not None and current.status != terminal:
            store.set_task_status(lead.id, terminal)
