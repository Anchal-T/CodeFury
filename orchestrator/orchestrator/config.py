"""Typed loading of config.yaml (plan §8) with cross-platform defaults.

The worker harness is injectable three ways, lowest priority first:
built-in ``DEFAULT_WORKER_COMMAND`` < config.yaml ``execution.worker_command``
(the legacy ``zcode_command`` key still works) < the ``WORKER_CMD`` (legacy
``ZCODE_CMD``) environment variable.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_WORKER_COMMAND = ["zcode"]
DEFAULT_HARNESS = "cli"
# The running interpreter is the only spawnable python guaranteed to exist on
# both Linux and Windows ("python3" is not an executable name on Windows).
DEFAULT_TEST_COMMAND = [sys.executable, "-m", "pytest", "-q"]
DEFAULT_WORKER_TIMEOUT_S = 1800.0

#: pytest exit code meaning "no tests collected" — not a failure for us.
PYTEST_NO_TESTS_EXIT_CODE = 5


@dataclass
class ExecutionConfig:
    #: Which registered runner backend executes worker tasks (Phase 9).
    harness: str = DEFAULT_HARNESS
    #: Argv template for the worker harness; a ``{prompt}`` placeholder is
    #: substituted in place, otherwise the prompt is appended as last argv.
    worker_command: list[str] = field(default_factory=lambda: list(DEFAULT_WORKER_COMMAND))
    test_command: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_COMMAND))
    worker_timeout_s: float = DEFAULT_WORKER_TIMEOUT_S


@dataclass
class PathsConfig:
    workspaces: Path = Path("./workspaces")
    domains: Path = Path("./domains")
    db: Path = Path("./data/orchestrator.db")
    logs: Path = Path("./logs")


@dataclass
class ConcurrencyConfig:
    max_workers: int = 3


@dataclass
class BudgetsConfig:
    """Per-level token caps (plan §3.6); ``None`` disables that level's cap.

    A cap counts the tokens a level spends ITSELF (today: level-0 worker
    subprocesses); aggregates are never re-counted at higher levels.
    """

    worker_tokens: int | None = 50000
    manager_tokens: int | None = 150000
    domain_lead_tokens: int | None = 300000
    architect_tokens: int | None = 500000


@dataclass
class RetryConfig:
    max_worker_retries: int = 2
    max_manager_escalations: int = 1
    max_reconcile_attempts: int = 1


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    retries: RetryConfig = field(default_factory=RetryConfig)
    budgets: BudgetsConfig = field(default_factory=BudgetsConfig)


def _as_list(value: object) -> list[str] | None:
    """Parse a command value into argv tokens.

    Strings go through shlex so quoted arguments (spaces inside one token)
    survive. Note: POSIX shlex treats backslashes as escapes — on Windows,
    prefer yaml list-form for paths with backslashes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    return [str(item) for item in value]


def _positive_float(section: dict, key: str, default: float) -> float:
    """Coerce a config value to a positive finite float, naming the key."""
    raw = section.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as err:
        raise ValueError(f"execution.{key} must be a number, got {raw!r}") from err
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"execution.{key} must be a positive finite number, got {raw!r}"
        )
    return value


def _token_cap(section: dict, key: str, default: int | None) -> int | None:
    """Coerce a budget cap to a non-negative int; explicit null = no cap.

    A missing key falls back to the tuned default; an explicit ``null``
    deliberately disables the cap — the two must stay distinguishable.
    """
    if key not in section:
        return default
    raw = section[key]
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as err:
        raise ValueError(f"budgets.{key} must be an integer, got {raw!r}") from err
    if value < 0:
        raise ValueError(f"budgets.{key} must be >= 0 (or null for no cap), got {value}")
    return value


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml; missing file or sections fall back to defaults."""
    data: dict = {}
    if path is not None:
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(
                    f"{path}: top-level config must be a mapping, got {type(loaded).__name__}"
                )
            data = loaded or {}
        elif path.exists():
            # Exists but is unusable (a directory, permissions...) — a likely
            # operator mistake; the intentional missing-file fallback stays
            # silent, this one must not be.
            logger.warning("config %s is not a readable file; using defaults", path)

    def _section(name: str) -> dict:
        raw = data.get(name)
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"{path}: config section '{name}' must be a mapping, got {type(raw).__name__}"
            )
        return raw or {}

    paths_raw = _section("paths")
    exec_raw = _section("execution")
    conc_raw = _section("concurrency")
    retries_raw = _section("retries")
    budgets_raw = _section("budgets")

    config = Config(
        paths=PathsConfig(
            workspaces=Path(paths_raw.get("workspaces", PathsConfig.workspaces)),
            domains=Path(paths_raw.get("domains", PathsConfig.domains)),
            db=Path(paths_raw.get("db", PathsConfig.db)),
            logs=Path(paths_raw.get("logs", PathsConfig.logs)),
        ),
        execution=ExecutionConfig(
            harness=str(exec_raw.get("harness", DEFAULT_HARNESS)),
            worker_command=_worker_command(exec_raw),
            test_command=_as_list(exec_raw.get("test_command")) or list(DEFAULT_TEST_COMMAND),
            worker_timeout_s=_positive_float(exec_raw, "worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S),
        ),
        concurrency=ConcurrencyConfig(
            max_workers=int(conc_raw.get("max_workers", ConcurrencyConfig.max_workers)),
        ),
        retries=RetryConfig(
            max_worker_retries=int(retries_raw.get("max_worker_retries", RetryConfig.max_worker_retries)),
            max_manager_escalations=int(
                retries_raw.get("max_manager_escalations", RetryConfig.max_manager_escalations)
            ),
            max_reconcile_attempts=int(
                retries_raw.get("max_reconcile_attempts", RetryConfig.max_reconcile_attempts)
            ),
        ),
        budgets=BudgetsConfig(
            worker_tokens=_token_cap(budgets_raw, "worker_tokens", BudgetsConfig.worker_tokens),
            manager_tokens=_token_cap(budgets_raw, "manager_tokens", BudgetsConfig.manager_tokens),
            domain_lead_tokens=_token_cap(
                budgets_raw, "domain_lead_tokens", BudgetsConfig.domain_lead_tokens
            ),
            architect_tokens=_token_cap(
                budgets_raw, "architect_tokens", BudgetsConfig.architect_tokens
            ),
        ),
    )
    if config.concurrency.max_workers < 1:
        raise ValueError(
            f"concurrency.max_workers must be >= 1, got {config.concurrency.max_workers}"
            " (0 would deadlock every worker dispatch)"
        )
    if config.retries.max_reconcile_attempts < 0:
        raise ValueError(
            "retries.max_reconcile_attempts must be >= 0, got "
            f"{config.retries.max_reconcile_attempts}"
        )
    env_cmd = _new_or_legacy(
        os.environ.get("WORKER_CMD"),
        os.environ.get("ZCODE_CMD"),
        "ZCODE_CMD is deprecated; rename it to WORKER_CMD",
    )
    if env_cmd:
        config.execution.worker_command = shlex.split(str(env_cmd))
    return config


def _new_or_legacy(new: object, old: object, deprecation: str) -> object:
    """Resolve a renamed setting: a non-empty new value wins; otherwise a
    non-empty legacy value is used with a deprecation warning. Empty new
    values carry no opinion, so the legacy setting still applies."""
    if new:
        return new
    if old:
        logger.warning(deprecation)
    return old


def _worker_command(exec_raw: dict) -> list[str]:
    """Resolve the worker command from the execution section (new
    ``worker_command`` key, legacy ``zcode_command`` key, or the default)."""
    raw = _new_or_legacy(
        exec_raw.get("worker_command"),
        exec_raw.get("zcode_command"),
        "execution.zcode_command is deprecated; rename it to worker_command",
    )
    return _as_list(raw) or list(DEFAULT_WORKER_COMMAND)
