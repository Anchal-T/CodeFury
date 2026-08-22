"""Graph state schema for the Manager/Worker StateGraph (plan §3.1).

Send fan-out runs worker branches concurrently; each branch returns a
partial update. Append-only lists (operator.add) and a max-merge dict keep
the merged state deterministic no matter how the branches interleave.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


def merge_attempts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Merge per-task attempt counts, keeping the highest seen."""
    merged = dict(left)
    for task_id, count in right.items():
        merged[task_id] = max(merged.get(task_id, 0), count)
    return merged


class OrchestratorState(TypedDict, total=False):
    manager_task: dict                # serialized level-1 Task
    worker_tasks: Annotated[list[dict], operator.add]      # created worker Tasks
    reports: Annotated[list[dict], operator.add]           # serialized worker Reports
    attempts: Annotated[dict[str, int], merge_attempts]    # task_id → attempts made
    merged: Annotated[list[str], operator.add]             # merged worker task ids
    dispatched: list[str]                                 # task ids already sent (dedupe)
    final_status: str                   # set when the manager finishes
    blockers: Annotated[list[str], operator.add]           # aggregate blockers
