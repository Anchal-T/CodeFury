"""Old-vs-new comparison for eval replays (Phase 12): per-task metrics plus
aggregate pass rates and token burn, rendered for the console and exportable
as JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from orchestrator.eval.replay import ReplayResult
from orchestrator.eval.source import SourceSnapshot


@dataclass
class TaskComparison:
    """One source task's old metrics against its replay."""

    task_id: str
    old_passed: bool | None = None
    new_passed: bool | None = None
    old_tokens: int = 0
    new_tokens: int = 0
    note: str = ""


@dataclass
class EvalReport:
    """Aggregate comparison over all replayed tasks."""

    comparisons: list[TaskComparison] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return len(self.comparisons)

    @property
    def old_passes(self) -> int:
        return sum(1 for c in self.comparisons if c.old_passed)

    @property
    def new_passes(self) -> int:
        """A crashed replay counts as a new failure — it never disappears."""
        return sum(1 for c in self.comparisons if c.new_passed)

    @property
    def old_tokens(self) -> int:
        return sum(c.old_tokens for c in self.comparisons)

    @property
    def new_tokens(self) -> int:
        return sum(c.new_tokens for c in self.comparisons)


def compare(snapshot: SourceSnapshot, results: list[ReplayResult]) -> EvalReport:
    """Pair every replay result with its source report (if any)."""
    comparisons: list[TaskComparison] = []
    for result in results:
        old = snapshot.reports.get(result.task_id)
        new = result.report
        comparisons.append(
            TaskComparison(
                task_id=result.task_id,
                old_passed=old.tests_passed if old else None,
                new_passed=new.tests_passed if new else None,
                old_tokens=old.tokens_used if old else 0,
                new_tokens=new.tokens_used if new else 0,
                note=result.error or "",
            )
        )
    return EvalReport(comparisons=comparisons)


def _mark(passed: bool | None) -> str:
    return {True: "pass", False: "FAIL", None: "—"}.get(passed, "—")  # type: ignore[return-value]


def render(report: EvalReport) -> str:
    """Console table: one row per task, aggregates at the end."""
    header = f"{'task':<20} {'old':<5} {'new':<5} {'tokens old→new':<18} note"
    lines = [header, "-" * len(header)]
    for c in report.comparisons:
        tokens = f"{c.old_tokens} → {c.new_tokens}"
        lines.append(f"{c.task_id:<20} {_mark(c.old_passed):<5} {_mark(c.new_passed):<5} {tokens:<18} {c.note}")
    lines.append("-" * len(header))
    lines.append(
        f"pass rate: {report.old_passes}/{report.compared} → {report.new_passes}/{report.compared}"
        f" | tokens: {report.old_tokens} → {report.new_tokens}"
    )
    return "\n".join(lines)


def report_to_json(report: EvalReport) -> str:
    """Stable JSON export for --out files and CI consumption."""
    return json.dumps(
        {
            "compared": report.compared,
            "old_passes": report.old_passes,
            "new_passes": report.new_passes,
            "old_tokens": report.old_tokens,
            "new_tokens": report.new_tokens,
            "comparisons": [
                {
                    "task_id": c.task_id,
                    "old_passed": c.old_passed,
                    "new_passed": c.new_passed,
                    "old_tokens": c.old_tokens,
                    "new_tokens": c.new_tokens,
                    "note": c.note,
                }
                for c in report.comparisons
            ],
        },
        indent=2,
    )
