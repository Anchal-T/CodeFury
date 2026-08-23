"""CLI entrypoint: orchestrator start | status | approve | logs | run | manage (plan §3.7).

``run`` drives the Phase 1 single-worker loop; ``manage`` drives the Phase 2
Manager graph with parallel worker fan-out, merge gating, and retry caps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import click

from orchestrator.config import load_config
from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager, find_repo_root
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.architect import finalize_epic, plan_epic, run_approved_leads
from orchestrator.graph.build_graph import build_graph
from orchestrator.graph.decompose import (
    SingleManagerDecomposer,
    StaticDecomposer,
    StaticDomainDecomposer,
    StaticEpicDecomposer,
)
from orchestrator.graph.lead_graph import build_lead_graph
from orchestrator.graph.worker import run_worker_task
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore


@click.group()
def cli() -> None:
    """Hierarchical Multi-Agent Coding Orchestrator."""


@cli.command()
@click.option("--goal", required=True, help="What the worker should achieve.")
@click.option("--deliverable", default=None, help="Definition of done for the task.")
@click.option("--task-id", default=None, help="Stable task id (default: generated).")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def run(goal: str, deliverable: str | None, task_id: str | None, config_path: Path) -> None:
    """Run one Task through the Phase 1 single-worker loop."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    repo_root = find_repo_root(Path.cwd())
    task = Task(
        id=task_id or f"task-{uuid4().hex[:8]}",
        parent_id=None,
        level=0,
        goal=goal,
        deliverable=deliverable or goal,
        dependencies=[],
        status="pending",
        assigned_to=None,
    )
    runner = ZCodeRunner(
        command=config.execution.zcode_command,
        timeout=config.execution.worker_timeout_s,
    )
    # Relative paths in config.yaml resolve against the config file's
    # directory (base), so runtime artifacts stay under orchestrator/.
    worktrees = WorktreeManager(repo_root, base / config.paths.workspaces)
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        click.echo(f"[run] task {task.id} → worker loop (repo: {repo_root})")
        report = run_worker_task(
            task,
            store=store,
            worktrees=worktrees,
            runner=runner,
            test_command=config.execution.test_command,
        )
    click.echo(
        f"\n[report] task={report.task_id} agent={report.agent}\n"
        f"  tests_passed: {report.tests_passed}\n"
        f"  diff_ref:     {report.diff_ref}\n"
        f"  tokens_used:  {report.tokens_used}\n"
        f"  blockers:     {report.blockers or 'none'}\n"
        f"  summary:      {report.summary.splitlines()[0] if report.summary else ''}"
    )
    if not report.tests_passed:
        raise SystemExit(1)


@cli.command()
@click.option("--goal", required=True, help="What the manager's workers should achieve together.")
@click.option(
    "--sub-goal", "sub_goals", multiple=True, required=True, help="One worker goal per flag."
)
@click.option("--task-id", default=None, help="Stable manager task id (default: generated).")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def manage(goal: str, sub_goals: tuple[str, ...], task_id: str | None, config_path: Path) -> None:
    """Run a Manager task: fan workers out, review, merge or retry (Phase 2)."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    repo_root = find_repo_root(Path.cwd())
    parent = Task(
        id=task_id or f"mgr-{uuid4().hex[:8]}",
        parent_id=None,
        level=1,
        goal=goal,
        deliverable=goal,
        dependencies=[],
        status="pending",
        assigned_to=None,
    )
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        graph = build_graph(
            store=store,
            worktrees=WorktreeManager(repo_root, base / config.paths.workspaces),
            runner=ZCodeRunner(
                command=config.execution.zcode_command,
                timeout=config.execution.worker_timeout_s,
            ),
            decomposer=StaticDecomposer(list(sub_goals)),
            retry_policy=RetryPolicy.from_config(config),
            test_command=config.execution.test_command,
            max_workers=config.concurrency.max_workers,
        )
        click.echo(f"[manage] task {parent.id} → {len(sub_goals)} worker(s), cap {config.concurrency.max_workers}")
        result = asyncio.run(graph.ainvoke({"manager_task": parent.model_dump()}))

    final = result.get("final_status", "unknown")
    attempts = result.get("attempts", {})
    merged = result.get("merged", [])
    click.echo(
        f"\n[manager report] final={final}\n"
        f"  merged:   {merged or 'none'}\n"
        f"  attempts: {attempts or 'none'}\n"
        f"  blockers: {result.get('blockers') or 'none'}"
    )
    if final != "review":
        raise SystemExit(1)


@cli.command()
@click.option("--goal", required=True, help="What the domain should achieve together.")
@click.option(
    "--manager-goal", "manager_goals", multiple=True, required=True,
    help="One manager task goal per flag.",
)
@click.option("--domain", required=True, help="Domain name (owns domains/<domain>/repo_map.md).")
@click.option("--task-id", default=None, help="Stable lead task id (default: generated).")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def lead(
    goal: str,
    manager_goals: tuple[str, ...],
    domain: str,
    task_id: str | None,
    config_path: Path,
) -> None:
    """Run a Domain Lead task: coordinate managers and resolve conflicts (Phase 3)."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    repo_root = find_repo_root(Path.cwd())
    parent = Task(
        id=task_id or f"lead-{uuid4().hex[:8]}",
        parent_id=None,
        level=2,
        goal=goal,
        deliverable=goal,
        dependencies=[],
        status="pending",
        assigned_to=None,
        domain=domain,
    )
    with StateStore(base / config.paths.db) as store:
        store.init_schema()
        graph = build_lead_graph(
            store=store,
            worktrees=WorktreeManager(repo_root, base / config.paths.workspaces),
            runner=ZCodeRunner(
                command=config.execution.zcode_command,
                timeout=config.execution.worker_timeout_s,
            ),
            decomposer=StaticDomainDecomposer(list(manager_goals)),
            test_command=config.execution.test_command,
            max_workers=config.concurrency.max_workers,
            max_reconcile_attempts=config.retries.max_reconcile_attempts,
            knowledge=KnowledgeDocs(),
            domains_dir=base / config.paths.domains,
        )
        click.echo(
            f"[lead] task {parent.id} → {len(manager_goals)} manager(s), "
            f"cap {config.concurrency.max_workers}, reconcile cap {config.retries.max_reconcile_attempts}"
        )
        result = asyncio.run(graph.ainvoke({"lead_task": parent.model_dump()}))

    outcome = result.get("outcome", "unknown")
    click.echo(
        f"\n[lead report] outcome={outcome}\n"
        f"  managers: {result.get('manager_results') or 'none'}\n"
        f"  reconciliations merged: {result.get('merged') or 'none'}\n"
        f"  escalated: {result.get('escalated') or 'none'}\n"
        f"  blockers: {result.get('blockers') or 'none'}"
    )
    if outcome != "review":
        raise SystemExit(1)


@cli.command()
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

        def lead_graph_factory(lead: Task):
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
            )

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
        )
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
        leads = plan_epic(epic=epic, decomposer=StaticEpicDecomposer(pairs), store=store)
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


@cli.command()
def status() -> None:
    """Pretty-print the current task tree from SQLite."""
    raise NotImplementedError("Phase 6")


@cli.command()
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


@cli.command()
@click.option("--tail", is_flag=True, help="Follow the JSONL run log.")
def logs(tail: bool) -> None:
    """Show structured JSONL run logs."""
    raise NotImplementedError("Phase 6")


if __name__ == "__main__":
    cli()
