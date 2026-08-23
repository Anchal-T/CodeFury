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
from orchestrator.graph.build_graph import build_graph
from orchestrator.graph.decompose import StaticDecomposer, StaticDomainDecomposer
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
    raise NotImplementedError("Phase 4 wiring")


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
