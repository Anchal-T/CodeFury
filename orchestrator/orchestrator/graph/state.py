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
    dispatched: Annotated[list[str], operator.add]         # task ids ever sent (dedupe)
    final_status: str                   # set when the manager finishes
    retrying: list                      # current retry batch (overwritten per round;
                                        # entries carry 'feedback' for the retry prompt)
    blockers: Annotated[list[str], operator.add]           # aggregate blockers
    manager_results: Annotated[list[dict], operator.add]   # one summary per finished manager


class LeadState(TypedDict, total=False):
    """Domain Lead graph state.

    Channels shared with the nested manager subgraphs (reports, attempts,
    merged, dispatched, blockers, manager_results) carry reducers so parallel
    subgraph outputs aggregate; keys the lead does not declare (final_status,
    worker_tasks, retrying) are dropped from subgraph outputs. The terminal
    key is `outcome` — written only by the lead, never by subgraphs.
    """

    lead_task: dict                    # serialized level-2 Task
    manager_tasks: list                # created level-1 tasks (written once)
    manager_results: Annotated[list[dict], operator.add]
    reports: Annotated[list[dict], operator.add]
    attempts: Annotated[dict[str, int], merge_attempts]
    merged: Annotated[list[str], operator.add]             # ids merged by the lead
    dispatched: Annotated[list[str], operator.add]
    blockers: Annotated[list[str], operator.add]
    escalated: Annotated[list[str], operator.add]          # ids failed for good
    resolved_managers: Annotated[list[str], operator.add]  # manager ids fully handled
    reconcile_tasks: list              # current recon batch (overwritten per round)
    reconcile_attempts: Annotated[dict[str, int], merge_attempts]  # manager_id → dispatches
    outcome: str                       # terminal status: "review" | "failed"
