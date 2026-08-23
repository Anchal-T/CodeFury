"""Typed loading of config.yaml (plan §8) with cross-platform defaults.

The worker command is injectable three ways, lowest priority first:
config.yaml ``execution.zcode_command`` < ``ZCODE_CMD`` environment variable.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_ZCODE_COMMAND = ["zcode"]
# The running interpreter is the only spawnable python guaranteed to exist on
# both Linux and Windows ("python3" is not an executable name on Windows).
DEFAULT_TEST_COMMAND = [sys.executable, "-m", "pytest", "-q"]
DEFAULT_WORKER_TIMEOUT_S = 1800.0

#: pytest exit code meaning "no tests collected" — not a failure for us.
PYTEST_NO_TESTS_EXIT_CODE = 5


@dataclass
class ExecutionConfig:
    zcode_command: list[str] = field(default_factory=lambda: list(DEFAULT_ZCODE_COMMAND))
    test_command: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_COMMAND))
    worker_timeout_s: float = DEFAULT_WORKER_TIMEOUT_S


@dataclass
class PathsConfig:
    workspaces: Path = Path("./workspaces")
    domains: Path = Path("./domains")
    db: Path = Path("./data/orchestrator.db")


@dataclass
class ConcurrencyConfig:
    max_workers: int = 3
    max_managers: int = 2


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
    raw: dict = field(default_factory=dict)


def _as_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.split()
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


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml; missing file or sections fall back to defaults."""
    data: dict = {}
    if path is not None and path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(
                f"{path}: top-level config must be a mapping, got {type(loaded).__name__}"
            )
        data = loaded or {}

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

    config = Config(
        paths=PathsConfig(
            workspaces=Path(paths_raw.get("workspaces", PathsConfig.workspaces)),
            domains=Path(paths_raw.get("domains", PathsConfig.domains)),
            db=Path(paths_raw.get("db", PathsConfig.db)),
        ),
        execution=ExecutionConfig(
            zcode_command=_as_list(exec_raw.get("zcode_command")) or list(DEFAULT_ZCODE_COMMAND),
            test_command=_as_list(exec_raw.get("test_command")) or list(DEFAULT_TEST_COMMAND),
            worker_timeout_s=_positive_float(exec_raw, "worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S),
        ),
        concurrency=ConcurrencyConfig(
            max_workers=int(conc_raw.get("max_workers", ConcurrencyConfig.max_workers)),
            max_managers=int(conc_raw.get("max_managers", ConcurrencyConfig.max_managers)),
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
        raw=data,
    )
    if config.concurrency.max_workers < 1:
        raise ValueError(
            f"concurrency.max_workers must be >= 1, got {config.concurrency.max_workers}"
            " (0 would deadlock every worker dispatch)"
        )
    if config.concurrency.max_managers < 1:
        raise ValueError(
            f"concurrency.max_managers must be >= 1, got {config.concurrency.max_managers}"
        )
    if config.retries.max_reconcile_attempts < 0:
        raise ValueError(
            "retries.max_reconcile_attempts must be >= 0, got "
            f"{config.retries.max_reconcile_attempts}"
        )
    env_cmd = os.environ.get("ZCODE_CMD")
    if env_cmd:
        config.execution.zcode_command = env_cmd.split()
    return config
