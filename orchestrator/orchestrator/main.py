"""CLI entrypoint: orchestrator run | manage | lead | status | logs (plan §3.7).

``run`` drives the Phase 1 single-worker loop, ``manage`` the Phase 2 Manager
graph, ``lead`` the Phase 3 domain graph. The Phase 4 epic flow (start/approve)
lives in ``orchestrator.cli_epic``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import click

from orchestrator.cli_epic import approve as approve_command
from orchestrator.cli_epic import start as start_command
from orchestrator.cli_status import (
    follow_file,
    newest_run_file,
    print_last_lines,
    render_task_tree,
    wait_for_newest_run_file,
)
from orchestrator.config import load_config
from orchestrator.contracts import Task
from orchestrator.execution.worktree_manager import WorktreeManager, find_repo_root
from orchestrator.execution.runner import build_runner
from orchestrator.governance.budget import BudgetTracker
from orchestrator.governance.retry_policy import RetryPolicy
from orchestrator.graph.build_graph import build_graph
from orchestrator.graph.decompose import (
    SingleManagerDecomposer,
    StaticDecomposer,
    StaticDomainDecomposer,
    StaticEpicDecomposer,
)
from orchestrator.graph.lead_graph import build_lead_graph
from orchestrator.graph.worker import run_worker_task
from orchestrator.logging_setup import setup_logging
from orchestrator.memory.knowledge_docs import KnowledgeDocs, parse_sections
from orchestrator.memory.store import StateStore
from orchestrator.memory.vector import auto_memory, recall_context


@click.group()
def cli() -> None:
    """Hierarchical Multi-Agent Coding Orchestrator."""


cli.add_command(start_command)
cli.add_command(approve_command)


def _budget(store: StateStore, config) -> BudgetTracker:
    """Per-level token caps for this invocation (Phase 6)."""
    return BudgetTracker(store, config.budgets)


#: Poll cadence for `logs --tail`'s follow loop.
_FOLLOW_INTERVAL_S = 0.5


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
    runner = build_runner(config.execution)
    # Relative paths in config.yaml resolve against the config file's
    # directory (base), so runtime artifacts stay under orchestrator/.
    worktrees = WorktreeManager(repo_root, base / config.paths.workspaces)
    runlog = setup_logging(base / config.paths.logs)
    with runlog, StateStore(base / config.paths.db) as store:
        store.init_schema()
        memory = auto_memory(store)
        click.echo(f"[run] task {task.id} → worker loop (repo: {repo_root})")
        report = run_worker_task(
            task,
            store=store,
            worktrees=worktrees,
            runner=runner,
            test_command=config.execution.test_command,
            budget=_budget(store, config),
            runlog=runlog,
            memory=memory,
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
    runlog = setup_logging(base / config.paths.logs)
    with runlog, StateStore(base / config.paths.db) as store:
        store.init_schema()
        memory = auto_memory(store)
        graph = build_graph(
            store=store,
            worktrees=WorktreeManager(repo_root, base / config.paths.workspaces),
            runner=build_runner(config.execution),
            decomposer=StaticDecomposer(list(sub_goals)),
            retry_policy=RetryPolicy.from_config(config),
            test_command=config.execution.test_command,
            max_workers=config.concurrency.max_workers,
            knowledge=KnowledgeDocs(),
            domains_dir=base / config.paths.domains,
            budget=_budget(store, config),
            runlog=runlog,
            memory=memory,
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
    runlog = setup_logging(base / config.paths.logs)
    with runlog, StateStore(base / config.paths.db) as store:
        store.init_schema()
        memory = auto_memory(store)
        graph = build_lead_graph(
            store=store,
            worktrees=WorktreeManager(repo_root, base / config.paths.workspaces),
            runner=build_runner(config.execution),
            decomposer=StaticDomainDecomposer(list(manager_goals)),
            test_command=config.execution.test_command,
            max_workers=config.concurrency.max_workers,
            max_reconcile_attempts=config.retries.max_reconcile_attempts,
            knowledge=KnowledgeDocs(),
            domains_dir=base / config.paths.domains,
            budget=_budget(store, config),
            runlog=runlog,
            memory=memory,
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
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def status(config_path: Path) -> None:
    """Pretty-print the current task tree from SQLite."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    db = base / config.paths.db
    if not db.exists():
        click.echo(f"[status] no database at {db} — nothing has run yet")
        return
    with StateStore(db) as store:
        tasks = store.all_tasks()
        if not tasks:
            click.echo("[status] no tasks recorded yet")
            return
        tree = render_task_tree(tasks, store.latest_tokens_by_task())
    click.echo(tree)


DEFAULT_TAIL_LINES = 20


@cli.command()
@click.option(
    "--tail",
    "tail",
    default=None,
    is_flag=False,
    flag_value=str(DEFAULT_TAIL_LINES),
    help="Show only the last N lines (default 20), then follow the log live.",
)
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def logs(tail: str | None, config_path: Path) -> None:
    """Print the JSONL run log; with --tail N, follow it live."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    logs_dir = base / config.paths.logs
    latest = newest_run_file(logs_dir)
    out = sys.stdout

    try:
        if latest is None:
            if tail is None:
                click.echo(f"[logs] no run logs in {logs_dir}")
                return
            click.echo(f"[logs] waiting for the first run log in {logs_dir} — Ctrl+C to stop", err=True)
            latest = wait_for_newest_run_file(logs_dir, interval_s=_FOLLOW_INTERVAL_S)

        if tail is None:
            out.write(latest.read_text(encoding="utf-8", errors="replace"))
            out.flush()
            return

        try:
            n = int(tail)
            if n < 0:
                raise ValueError
        except ValueError:
            raise click.ClickException("--tail expects a non-negative integer") from None
        pos = print_last_lines(latest, n, out)
        click.echo(f"[logs] following {latest.name} — Ctrl+C to stop", err=True)
        follow_file(latest, pos, out, interval_s=_FOLLOW_INTERVAL_S)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.argument("query")
@click.option("--top-k", default=5, show_default=True, help="Maximum hits to show.")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def recall(query: str, top_k: int, config_path: Path) -> None:
    """Search Tier-3 semantic memory with a natural-language query."""
    config = load_config(config_path)
    base = config_path.resolve().parent
    db = base / config.paths.db
    if not db.exists():
        raise click.ClickException(
            f"no database at {db} — run something first, or seed with `orchestrator reindex`"
        )
    with StateStore(db) as store:
        store.init_schema()
        memory = auto_memory(store)
        if memory is None:
            raise click.ClickException(
                "Tier-3 unavailable — install fastembed (pip install fastembed)"
                " and make sure ORCHESTRATOR_SEMANTIC is not set to 0"
            )
        hits = memory.search(query, top_k=top_k)
    if not hits:
        click.echo(f"[recall] no entries match {query!r} — seed with `orchestrator reindex`")
        return
    for hit in hits:
        label = hit["title"] or hit["ref"]
        click.echo(f"{hit['score']:.3f}  [{hit['source']}] {label}")
        snippet = " ".join(str(hit["body"]).split())
        if snippet:
            click.echo(f"      {snippet}")


@cli.command()
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def reindex(config_path: Path) -> None:
    """(Re)index knowledge docs and report summaries into Tier-3 memory.

    Idempotent: entries are keyed by (source, ref), so re-running replaces
    instead of duplicating.
    """
    config = load_config(config_path)
    base = config_path.resolve().parent
    db = base / config.paths.db
    domains_dir = base / config.paths.domains
    if not db.exists():
        raise click.ClickException(f"no database at {db}")
    indexed = 0
    with StateStore(db) as store:
        store.init_schema()
        memory = auto_memory(store)
        if memory is None:
            raise click.ClickException(
                "Tier-3 unavailable — install fastembed (pip install fastembed)"
                " and make sure ORCHESTRATOR_SEMANTIC is not set to 0"
            )
        knowledge = KnowledgeDocs()
        for doc in sorted(domains_dir.rglob("*.md")) if domains_dir.is_dir() else []:
            text = knowledge.read_latest(doc)
            for title, body in parse_sections(text):
                if not body.strip():
                    continue
                memory.upsert(body, source="knowledge", ref=f"{doc}#{title}", title=title)
                indexed += 1
        for report in store.latest_reports():
            summary = report.summary.strip()
            if not summary:
                continue
            memory.upsert(
                summary,
                source="report",
                ref=report.task_id,
                title=report.agent,
            )
            indexed += 1
    click.echo(f"[reindex] {indexed} entr{'y' if indexed == 1 else 'ies'} indexed into Tier-3 memory")


if __name__ == "__main__":
    cli()
