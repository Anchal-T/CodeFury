"""Typed loading of config.yaml (plan §8) with cross-platform defaults.

The worker command is injectable three ways, lowest priority first:
config.yaml ``execution.zcode_command`` < ``ZCODE_CMD`` environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_ZCODE_COMMAND = ["zcode"]
DEFAULT_TEST_COMMAND = ["python3", "-m", "pytest", "-q"]
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
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    raw: dict = field(default_factory=dict)


def _as_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value]


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml; missing file or sections fall back to defaults."""
    data: dict = {}
    if path is not None and path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    paths_raw = data.get("paths") or {}
    exec_raw = data.get("execution") or {}

    config = Config(
        paths=PathsConfig(
            workspaces=Path(paths_raw.get("workspaces", PathsConfig.workspaces)),
            domains=Path(paths_raw.get("domains", PathsConfig.domains)),
            db=Path(paths_raw.get("db", PathsConfig.db)),
        ),
        execution=ExecutionConfig(
            zcode_command=_as_list(exec_raw.get("zcode_command")) or list(DEFAULT_ZCODE_COMMAND),
            test_command=_as_list(exec_raw.get("test_command")) or list(DEFAULT_TEST_COMMAND),
            worker_timeout_s=float(exec_raw.get("worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S)),
        ),
        raw=data,
    )
    env_cmd = os.environ.get("ZCODE_CMD")
    if env_cmd:
        config.execution.zcode_command = env_cmd.split()
    return config
