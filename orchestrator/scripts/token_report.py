"""Per-level token burn report from a run database (dev utility, plan §9 Phase 8).

Usage:
    python3 scripts/token_report.py [path/to/orchestrator.db]

Prints one row per hierarchy level: recorded token_usage sums plus how many
usage entries back them. Read-only; safe to run while an orchestration is
live (WAL readers never block writers).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.memory.store import StateStore  # noqa: E402

LEVEL_ROLES = {0: "worker", 1: "manager", 2: "domain_lead", 3: "architect"}


def collect_burn(store: StateStore) -> list[tuple[int, str, int, int]]:
    """(level, role, tokens, entries) per level that has recorded spend."""
    rows = store.connection().execute(
        "SELECT level, COUNT(*) AS entries, SUM(tokens) AS tokens"
        " FROM token_usage GROUP BY level ORDER BY level"
    ).fetchall()
    return [
        (int(row["level"]), LEVEL_ROLES.get(int(row["level"]), "?"),
         int(row["tokens"]), int(row["entries"]))
        for row in rows
    ]


def format_report(burn: list[tuple[int, str, int, int]]) -> str:
    header = f"{'level':<6}{'role':<12}{'entries':>8}{'tokens':>10}"
    lines = [header, "-" * len(header)]
    for level, role, tokens, entries in burn:
        lines.append(f"{level:<6}{role:<12}{entries:>8}{tokens:>10}")
    return "\n".join(lines)


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/orchestrator.db")
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1
    with StateStore(db_path) as store:
        burn = collect_burn(store)
    if not burn:
        print(f"no token usage recorded yet in {db_path}")
        return 0
    print(format_report(burn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
