"""Deterministic report critic (Phase 11): quality heuristics over a worker
Report and its worktree diff, complementing ``tests_passed``.

Pure and LLM-free: the caller (worker pipeline) supplies the changed-file
list and the BLOCKED.md flag; the manager applies policy (strict mode
promotes warnings to blockers, which routes into retry-with-feedback).
Every warning carries the ``critic:`` prefix so consumers can attribute it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath

from orchestrator.config import CriticConfig
from orchestrator.contracts import Report

#: Changed paths that count as test files, matched with fnmatch against
#: posix-style repo-relative paths. Tampering with tests is the classic way
#: to pass a gate without doing the work.
TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "tests/*",
    "*/tests/*",
    "spec/*",
    "*/spec/*",
)

#: Warnings listing files show at most this many paths before ellipsizing.
MAX_LISTED_FILES = 5


@dataclass
class Critique:
    """Advisory findings about one worker Report."""

    warnings: list[str] = field(default_factory=list)


def _posix(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/")))


def _matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def _list(paths: list[str]) -> str:
    listed = ", ".join(paths[:MAX_LISTED_FILES])
    return listed + ("…" if len(paths) > MAX_LISTED_FILES else "")


def criticize(
    report: Report,
    *,
    changed_files: list[str],
    blocked: bool,
    config: CriticConfig,
) -> Critique:
    """Heuristically critique one Report against its worktree evidence.

    ``changed_files`` is the worker branch's diff (merge-base anchored);
    ``blocked`` says whether BLOCKED.md sits in the worktree. The heuristics
    are advisory — they never raise and never mutate the Report.
    """
    warnings: list[str] = []
    files = [_posix(f) for f in changed_files if f.strip()]
    if files:
        if len(files) > config.max_changed_files:
            warnings.append(
                f"critic: worker changed {len(files)} files "
                f"(cap {config.max_changed_files}) — sanity-check the scope"
            )
        test_hits = [f for f in files if _matches(f, TEST_FILE_PATTERNS)]
        if test_hits:
            warnings.append(f"critic: worker modified test files: {_list(test_hits)}")
        forbidden = [f for f in files if _matches(f, config.forbidden_globs)]
        if forbidden:
            warnings.append(f"critic: worker touched forbidden path(s): {_list(forbidden)}")
    elif report.tests_passed and not report.blockers:
        warnings.append("critic: report claims success but the worktree diff is empty")
    if report.tests_passed and not report.summary.strip():
        warnings.append("critic: passing report carries an empty summary")
    if blocked and report.tests_passed and not any("BLOCKED.md" in b for b in report.blockers):
        warnings.append("critic: BLOCKED.md present despite a passing report")
    return Critique(warnings)
