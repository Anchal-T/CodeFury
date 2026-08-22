"""CLI entrypoint: orchestrator start | status | approve <task_id> | logs --tail (plan §3.7)."""

import click


@click.group()
def cli() -> None:
    """Hierarchical Multi-Agent Coding Orchestrator."""


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
