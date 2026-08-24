"""Phase 4 epic commands: plan/resume an epic (`start`) and the approval gate (`approve`).

Registered on the root CLI group in ``orchestrator.main``; kept separate so the
CLI entrypoint stays small and single-purpose.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import click

from orchestrator.config import load_config
from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager, find_repo_root
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.governance.budget import BudgetTracker
from orchestrator.graph.architect import finalize_epic, plan_epic, run_approved_leads
from orchestrator.graph.decompose import SingleManagerDecomposer, StaticEpicDecomposer
from orchestrator.graph.lead_graph import build_lead_graph
from orchestrator.logging_setup import setup_logging
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore


@click.command()
@click.option("--epic", "epic_goal", default=None, help="Plan a new epic (requires --domain-goal pairs).")
@click.option(
    "--domain-goal",
    "domain_goals",
    multiple=True,
    help="One 'goal:domain' pair per planned lead task, e.g. 'ship auth api:backend'.",
)
@click.option("--task-id", default=None, help="Stable epic task id (default: generated).")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def start(
    epic_goal: str | None,
    domain_goals: tuple[str, ...],
    task_id: str | None,
    config_path: Path,
) -> None:
    """Plan an epic for approval, or resume the current epic's approved leads (Phase 4)."""
    config = load_config(config_path)
    base = config_path.resolve().parent

    if epic_goal is not None:
        _start_plan_mode(epic_goal, domain_goals, task_id, config, base)
        return

    repo_root = find_repo_root(Path.cwd())
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        epic = _latest_unfinished_epic(store)
        if epic is None:
            raise click.ClickException(
                "no epic to resume — plan one with: "
                "orchestrator start --epic \"...\" --domain-goal 'goal:domain'"
            )
        worktrees = WorktreeManager(repo_root, base / config.paths.workspaces)
        runner = ZCodeRunner(
            command=config.execution.zcode_command,
            timeout=config.execution.worker_timeout_s,
        )
        knowledge = KnowledgeDocs()
        domains_dir = base / config.paths.domains
        budget = BudgetTracker(store, config.budgets)
        runlog = setup_logging(base / config.paths.logs)

        def lead_graph_factory(lead: Task, checkpointer):
            return build_lead_graph(
                store=store,
                worktrees=worktrees,
                runner=runner,
                decomposer=SingleManagerDecomposer(),
                test_command=config.execution.test_command,
                max_workers=config.concurrency.max_workers,
                max_reconcile_attempts=config.retries.max_reconcile_attempts,
                knowledge=knowledge,
                domains_dir=domains_dir,
                checkpointer=checkpointer,
                budget=budget,
                runlog=runlog,
            )

        try:
            click.echo(f"[start] resuming epic {epic.id} ('{epic.goal}')")
            outcomes = run_approved_leads(
                store=store, epic=store.get_task(epic.id), lead_graph_factory=lead_graph_factory
            )
            for outcome in outcomes:
                click.echo(f"[start] lead finished: outcome={outcome.get('outcome', 'unknown')}")

            report = finalize_epic(
                epic=store.get_task(epic.id),
                store=store,
                knowledge=knowledge,
                project_state=domains_dir / "PROJECT_STATE.md",
                runlog=runlog,
            )
        finally:
            runlog.close()
        if report is None:
            waiting = [t for t in store.tasks_by_parent(epic.id) if t.status == "pending_approval"]
            click.echo(f"[start] epic {epic.id}: {len(waiting)} lead(s) still awaiting approval:")
            for lead in waiting:
                click.echo(f"  [pending_approval] {lead.id} ({lead.domain}): {lead.goal}")
            click.echo("Approve with: orchestrator approve <task_id>, then run `orchestrator start` again.")
            return
        epic = store.get_task(epic.id) or epic  # finalize_epic updated the status
        click.echo(
            f"\n[architect report] epic={epic.id} final={epic.status}\n"
            f"  summary:  {report.summary}\n"
            f"  blockers: {report.blockers or 'none'}"
        )
        if report.blockers:
            for blocker in report.blockers:
                click.echo(f"  - {blocker}")
        if epic.status != "done":
            raise SystemExit(1)


@click.command()
@click.argument("task_id")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def approve(task_id: str, config_path: Path) -> None:
    """Approve a pending_approval task at the human checkpoint."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        task = store.get_task(task_id)
        if task is None:
            raise click.ClickException(f"unknown task id: {task_id}")
        if task.status != "pending_approval":
            raise click.ClickException(
                f"task {task_id} is '{task.status}', not awaiting approval"
            )
        store.set_task_status(task_id, "pending")
    click.echo(f"[approve] {task_id} ('{task.goal}') approved → pending")


def _start_plan_mode(
    epic_goal: str,
    domain_goals: tuple[str, ...],
    task_id: str | None,
    config,
    base: Path,
) -> None:
    """Create the epic and its pending_approval domain leads; run nothing."""
    if not domain_goals:
        raise click.ClickException("--epic requires at least one --domain-goal 'goal:domain'")
    pairs: list[tuple[str, str]] = []
    for entry in domain_goals:
        goal, sep, domain = entry.rpartition(":")
        if not sep or not goal.strip() or not domain.strip():
            raise click.ClickException(
                f"invalid --domain-goal {entry!r}: expected 'goal:domain' (e.g. 'ship auth api:backend')"
            )
        pairs.append((goal.strip(), domain.strip()))

    epic = Task(
        id=task_id or f"epic-{uuid4().hex[:8]}",
        parent_id=None,
        level=3,
        goal=epic_goal,
        deliverable=epic_goal,
        dependencies=[],
        status="pending",
        assigned_to=None,
    )
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        existing = store.get_task(task_id) if task_id is not None else None
        if existing is not None:
            children = store.tasks_by_parent(task_id)
            click.echo(
                f"WARNING: epic task id '{task_id}' already exists "
                f"(status={existing.status}, {len(children)} child lead(s)) — re-planning "
                "would overwrite the epic and mix old and new leads under one parent.",
                err=True,
            )
            click.echo(
                "Hint: pass a different --task-id, omit it to generate one, or resume the "
                "existing epic with `orchestrator start`.",
                err=True,
            )
            raise click.ClickException(f"task id already exists: {task_id}")
        domains_dir = base / config.paths.domains
        knowledge = KnowledgeDocs()
        planning_context = knowledge.read_latest(domains_dir / "PROJECT_STATE.md")
        leads = plan_epic(
            epic=epic,
            decomposer=StaticEpicDecomposer(pairs),
            store=store,
            planning_context=planning_context,
        )
    if not leads:
        raise SystemExit(1)
    click.echo(f"[start] epic {epic.id} planned — {len(leads)} domain lead(s) awaiting approval:")
    for lead in leads:
        click.echo(f"  [pending_approval] {lead.id} ({lead.domain}): {lead.goal}")
    click.echo("Approve with: orchestrator approve <task_id>, then run `orchestrator start` to resume.")


def _latest_unfinished_epic(store: StateStore) -> Task | None:
    """Most recent level-3 task not yet done/failed, or None."""
    for status in ("review", "in_progress"):
        epics = [t for t in store.tasks_by_status(status) if t.level == 3]
        if epics:
            return epics[-1]
    return None
