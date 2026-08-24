"""Read-only CLI introspection: task tree (status) and run-log tailing (logs).

Pure helpers with injectable output streams so ``logs --tail``'s follow loop
is testable without sleeping through a real terminal session.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TextIO

from orchestrator.contracts import Task

_GOAL_MAX_CHARS = 60


# -- status: the SQLite task tree -------------------------------------------


def render_task_tree(tasks: list[Task], tokens_by_task: dict[str, int] | None = None) -> str:
    """Format every persisted task as an indented parent→child tree.

    Insertion order is preserved within each level; tasks whose parent row is
    absent (or NULL) render as roots, so orphans stay visible.
    """
    tokens_by_task = tokens_by_task or {}
    ids = {task.id for task in tasks}
    children_by_parent: dict[str, list[Task]] = {}
    roots: list[Task] = []
    for task in tasks:
        if task.parent_id and task.parent_id in ids:
            children_by_parent.setdefault(task.parent_id, []).append(task)
        else:
            roots.append(task)

    lines: list[str] = []

    def emit(task: Task, depth: int) -> None:
        lines.append(_format_line(task, depth, tokens_by_task.get(task.id, 0)))
        for child in children_by_parent.get(task.id, []):
            emit(child, depth + 1)

    for root in roots:
        emit(root, 0)
    return "\n".join(lines)


def _format_line(task: Task, depth: int, tokens: int) -> str:
    indent = "  " * depth
    domain = f" {task.domain}" if task.domain else ""
    goal = task.goal if len(task.goal) <= _GOAL_MAX_CHARS else task.goal[:_GOAL_MAX_CHARS - 3] + "..."
    suffix = f" ({tokens} tok)" if tokens else ""
    return f"{indent}{task.id} [{task.status}] L{task.level}{domain} {goal}{suffix}"


# -- logs: JSONL tailing ------------------------------------------------------


def newest_run_file(logs_dir: Path) -> Path | None:
    """The lexicographically latest run_<ts>.jsonl (timestamps sort)."""
    if not logs_dir.is_dir():
        return None
    files = sorted(logs_dir.glob("run_*.jsonl"))
    return files[-1] if files else None


def wait_for_newest_run_file(
    logs_dir: Path, *, interval_s: float, max_polls: int | None = None
) -> Path | None:
    """Poll until a run log exists (None = give up after max_polls)."""
    polls = 0
    while True:
        found = newest_run_file(logs_dir)
        if found is not None:
            return found
        if max_polls is not None and polls >= max_polls:
            return None
        time.sleep(interval_s)
        polls += 1


def print_last_lines(path: Path, n: int, out: TextIO) -> int:
    """Write the last n lines; returns the file position to resume from."""
    data = path.read_bytes()
    if n > 0:
        tail = b"".join(data.splitlines(keepends=True)[-n:])
        out.write(tail.decode("utf-8", errors="replace"))
        out.flush()
    return len(data)


def follow_file(
    path: Path,
    pos: int,
    out: TextIO,
    *,
    interval_s: float = 0.5,
    max_polls: int | None = None,
) -> int:
    """Print complete lines appended after pos; returns the position reached.

    ``max_polls=None`` follows forever — the CLI relies on KeyboardInterrupt.
    Partial trailing lines are held back until their newline arrives so a
    concurrent writer can never leak a torn JSON fragment.
    """
    polls = 0
    with path.open("rb") as fh:
        fh.seek(pos)
        while max_polls is None or polls < max_polls:
            chunk = fh.read()
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                if not text.endswith("\n"):
                    keep = text.rfind("\n") + 1
                    back = len(text[keep:].encode("utf-8"))
                    if back:
                        fh.seek(-back, os.SEEK_CUR)
                    text = text[:keep]
                if text:
                    out.write(text)
                    out.flush()
            time.sleep(interval_s)
            polls += 1
        return fh.tell()
