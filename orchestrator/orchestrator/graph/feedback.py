"""Retry feedback (Phase 10): renders a failed attempt's Report into a
prompt block so the next attempt starts from what went wrong.

Retries used to re-dispatch the identical prompt, so a worker repeated the
same mistakes. This closes the loop deterministically — no LLM involved.
"""

from __future__ import annotations

from orchestrator.contracts import Report

#: Header marking the feedback block in a worker prompt. scripts/fake_worker.py
#: keys its FAKE_WORKER_FAIL_UNTIL_FEEDBACK mode off this text (keep in sync).
FEEDBACK_HEADER = "## Previous attempt feedback"

#: Hard cap on the block: retries share the level's token budget, so a
#: pathological summary must not bloat every retry prompt.
DEFAULT_MAX_CHARS = 1500

_GUIDANCE = (
    "The previous attempt failed. Address the issues below; "
    "do not repeat its approach."
)


def build_feedback(report: Report, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Render the feedback block for a failed attempt's Report.

    Empty string when there is nothing actionable (no blockers and no
    summary). The whole block is capped at ``max_chars``; the cap eats the
    summary's head, never the header, blockers, or the summary's tail
    (where worker summaries put the failing-test output).
    """
    blockers = [b.strip() for b in report.blockers if b.strip()]
    summary = report.summary.strip()
    if not blockers and not summary:
        return ""
    lines = [FEEDBACK_HEADER, "", _GUIDANCE]
    if blockers:
        lines += ["", "Blockers:", *[f"- {blocker}" for blocker in blockers]]
    if summary:
        lines += ["", "Previous attempt summary:", summary]
    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    prefix_len = len(block) - len(summary)
    keep = max_chars - prefix_len - 1  # reserve one char for the ellipsis
    if keep <= 0:
        return block[:max_chars]
    # Keep the summary's tail, not its head: worker summaries append the
    # failing-test output last, which is the actionable part (Phase 10).
    return block[:prefix_len] + "…" + summary[-keep:]
