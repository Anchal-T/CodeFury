"""Tests for orchestrator.config."""

import sys
from pathlib import Path

import pytest

from orchestrator.config import DEFAULT_TEST_COMMAND, load_config

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@pytest.fixture(autouse=True)
def _no_zcode_cmd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient ZCODE_CMD (e.g. set for a demo run) out of file-loading tests."""
    monkeypatch.delenv("ZCODE_CMD", raising=False)


def test_loads_real_repo_config() -> None:
    config = load_config(REPO_CONFIG)
    assert config.execution.zcode_command == ["zcode"]
    assert config.execution.test_command == [sys.executable, "-m", "pytest", "-q"]
    assert config.execution.worker_timeout_s == 1800
    assert config.paths.db == Path("./data/orchestrator.db")
    assert config.paths.workspaces == Path("./workspaces")


def test_missing_file_falls_back_to_defaults() -> None:
    config = load_config(Path("does-not-exist.yaml"))
    assert config.execution.zcode_command == ["zcode"]
    assert config.execution.test_command == DEFAULT_TEST_COMMAND
    assert config.paths.db == Path("./data/orchestrator.db")


def test_custom_yaml_sections(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "execution:\n"
        "  zcode_command: [/usr/bin/python3, agent.py]\n"
        "  worker_timeout_s: 60\n"
        "paths:\n"
        "  db: ./custom.db\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.execution.zcode_command == ["/usr/bin/python3", "agent.py"]
    assert config.execution.worker_timeout_s == 60.0
    assert config.paths.db == Path("./custom.db")


def test_env_var_overrides_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZCODE_CMD", "python3 scripts/fake_worker.py")
    config = load_config(tmp_path / "missing.yaml")
    assert config.execution.zcode_command == ["python3", "scripts/fake_worker.py"]


def test_env_var_overrides_command_keeps_quoted_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoted arguments (spaces inside a single argv token) must survive —
    plain str.split() would silently mangle them into wrong argv entries."""
    monkeypatch.setenv("ZCODE_CMD", '"/usr/local/my agent/bin/zcode" --flag "slow tests"')
    config = load_config(tmp_path / "missing.yaml")
    assert config.execution.zcode_command == [
        "/usr/local/my agent/bin/zcode",
        "--flag",
        "slow tests",
    ]


def test_quoted_yaml_scalar_command_parsed_with_shlex(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        'execution:\n  zcode_command: \'run-agent --name "my agent"\'\n',
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.execution.zcode_command == ["run-agent", "--name", "my agent"]


def test_non_positive_max_workers_rejected(tmp_path: Path) -> None:
    """max_workers: 0 would deadlock every dispatch (Semaphore(0) is never
    acquirable) — invalid configuration must fail fast at load time."""
    import pytest

    config_file = tmp_path / "config.yaml"
    config_file.write_text("concurrency:\n  max_workers: 0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config_file)


def test_default_reconcile_attempts_is_one() -> None:
    config = load_config(Path("does-not-exist.yaml"))
    assert config.retries.max_reconcile_attempts == 1


def test_custom_yaml_overrides_reconcile_attempts(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("retries:\n  max_reconcile_attempts: 2\n", encoding="utf-8")
    config = load_config(config_file)
    assert config.retries.max_reconcile_attempts == 2


def test_negative_reconcile_attempts_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("retries:\n  max_reconcile_attempts: -1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config_file)


@pytest.mark.parametrize("bad", ["0", "-5", ".nan", ".inf"])
def test_non_positive_worker_timeout_rejected(tmp_path: Path, bad: str) -> None:
    """A non-positive timeout would make every worker time out immediately —
    reject it at load time with the offending key named."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"execution:\n  worker_timeout_s: {bad}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="worker_timeout_s"):
        load_config(config_file)


def test_valid_worker_timeout_accepted(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("execution:\n  worker_timeout_s: 90\n", encoding="utf-8")
    assert load_config(config_file).execution.worker_timeout_s == 90.0


def test_non_mapping_top_level_rejected(tmp_path: Path) -> None:
    """A scalar/list yaml root must fail with an actionable message, not an
    AttributeError from deep inside the loader."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("- item\n- another\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(config_file)


@pytest.mark.parametrize("section", ["paths", "execution", "concurrency", "retries"])
def test_non_mapping_section_rejected(tmp_path: Path, section: str) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"{section}: just-a-string\n", encoding="utf-8")
    with pytest.raises(ValueError, match=section):
        load_config(config_file)
