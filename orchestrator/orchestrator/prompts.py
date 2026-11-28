"""Prompt construction for worker subprocesses (plan §3.3, §10).

The prompt serializes the structured Task (never chat transcripts) and pins
the worker to its worktree so agents never touch shared state.
"""

from __future__ import annotations

from orchestrator.contracts import Task

_WORKER_RULES = """\
You are a Worker agent executing one coding task inside a git worktree.

Rules:
- Work ONLY inside the current working directory; never touch files outside it.
- Do NOT run git commit/merge/push — the orchestrator handles version control.
- Achieve the goal and produce the deliverable exactly as specified.
- Keep changes minimal and focused; do not refactor unrelated code.
- If you cannot complete the task, leave a short note in a file named BLOCKED.md.

Task (JSON):
"""


def build_worker_prompt(task: Task, knowledge_context: str = "", feedback: str = "") -> str:
    """Build the prompt handed to the worker command for this task.

    ``knowledge_context`` carries the Tier-1 memory (plan §3.5, §9 Phase 5):
    the latest domain repo_map.md section, prepended ahead of the task so
    workers plan with current project knowledge. Empty context omits the
    block entirely.

    ``feedback`` (Phase 10) carries the previous attempt's feedback block on
    retries, after the knowledge header and before the task payload.
    """
    header = ""
    if knowledge_context.strip():
        header = f"## Project knowledge (latest)\n\n{knowledge_context.strip()}\n\n"
    retry_note = ""
    if feedback.strip():
        retry_note = f"{feedback.strip()}\n\n"
    return header + retry_note + _WORKER_RULES + task.model_dump_json(indent=2)
