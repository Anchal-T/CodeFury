"""CLI entrypoint: orchestrator start | status | approve | logs | run (plan §3.7).

``run`` is the Phase 1 single-worker loop driver: it builds a Task from the
goal, executes it through the worker pipeline, and prints the Report.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import click

from orchestrator.config import load_config
from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager, find_repo_root
from orchestrator.execution.zcode_runner import ZCodeRunner
from orchestrator.graph.worker import run_worker_task
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
def start() -> None:
    """Start the orchestrator and begin processing the current epic."""
    raise NotImplementedError("Phase 4")


@cli.command()
def status() -> None:
    """Pretty-print the current task tree from SQLite."""
    raise NotImplementedError("Phase 6")


@cli.command()
@click.argument("task_id")
def approve(task_id: str) -> None:
    """Approve a task at a human approval checkpoint."""
    raise NotImplementedError("Phase 4")


@cli.command()
@click.option("--tail", is_flag=True, help="Follow the JSONL run log.")
def logs(tail: bool) -> None:
    """Show structured JSONL run logs."""
    raise NotImplementedError("Phase 6")


if __name__ == "__main__":
    cli()
