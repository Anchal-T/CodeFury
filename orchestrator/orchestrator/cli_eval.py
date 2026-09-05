"""Phase 12 eval command: replay past level-0 tasks through the current
pipeline and print an old-vs-new comparison.

Registered on the root CLI group in ``orchestrator.main``; kept separate so
the CLI entrypoint stays small and single-purpose.
"""

from __future__ import annotations

from pathlib import Path

import click

from orchestrator.config import load_config
from orchestrator.eval.compare import compare, render, report_to_json
from orchestrator.eval.replay import EvalRunner
from orchestrator.eval.source import load_snapshot, select_tasks
from orchestrator.execution.runner import build_runner
from orchestrator.execution.worktree_manager import WorktreeManager, find_repo_root
from orchestrator.memory.knowledge_docs import KnowledgeDocs
from orchestrator.memory.store import StateStore

EVAL_BRANCH_PREFIX = "orchestrator/eval-"


@click.command(name="eval")
@click.option(
    "--from-db",
    "from_db",
    required=True,
    type=click.Path(path_type=Path),
    help="Source run database whose level-0 tasks will be replayed.",
)
@click.option(
    "--repo",
    type=click.Path(path_type=Path),
    default=None,
    help="Repo root to replay against (default: the current directory's repo).",
)
@click.option("--task", "task_ids", multiple=True, help="Replay only these task ids.")
@click.option("--limit", type=int, default=None, help="Replay only the first N tasks.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Also write the comparison report as JSON to this path.")
@click.option("--keep-worktrees", is_flag=True, help="Keep replay worktrees for inspection.")
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    type=click.Path(path_type=Path),
    help="Path to config.yaml.",
)
def eval_command(
    from_db: Path,
    repo: Path | None,
    task_ids: tuple[str, ...],
    limit: int | None,
    out_path: Path | None,
    keep_worktrees: bool,
    config_path: Path,
) -> None:
    """Replay past tasks against the current pipeline and compare (Phase 12)."""
    if limit is not None and limit < 0:
        raise click.ClickException("--limit expects a non-negative integer")
    config = load_config(config_path)
    base = config_path.resolve().parent
    source_db = from_db if from_db.is_absolute() else Path.cwd() / from_db

    snapshot = load_snapshot(source_db)
    try:
        selected = select_tasks(snapshot, ids=list(task_ids) or None, limit=limit)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if not selected:
        click.echo("[eval] nothing to replay")
        return

    repo_root = find_repo_root((repo or Path.cwd()).resolve())
    eval_workspaces = base / config.paths.workspaces / "eval"
    eval_db = (base / config.paths.db).with_name("eval.db")
    eval_db.parent.mkdir(parents=True, exist_ok=True)

    click.echo(
        f"[eval] replaying {len(selected)} task(s) from {source_db.name} "
        f"against {repo_root} (eval db: {eval_db.name})"
    )
    with StateStore(eval_db) as store:
        store.init_schema()
        runner = EvalRunner(
            store=store,
            worktrees=WorktreeManager(
                repo_root, eval_workspaces, branch_prefix=EVAL_BRANCH_PREFIX
            ),
            runner=build_runner(config.execution),
            test_command=config.execution.test_command,
            knowledge=KnowledgeDocs(),
            domains_dir=base / config.paths.domains,
            critic=config.critic,
            keep_worktrees=keep_worktrees,
        )
        results = runner.run(selected)

    report = compare(snapshot, results)
    click.echo("\n" + render(report))
    if out_path is not None:
        out_path.write_text(report_to_json(report) + "\n", encoding="utf-8")
        click.echo(f"[eval] JSON report written to {out_path}")
