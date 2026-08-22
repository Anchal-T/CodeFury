"""Tests for orchestrator.config."""

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
    assert config.execution.test_command[:2] == ["python3", "-m"]
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


def test_env_var_overrides_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZCODE_CMD", "python3 scripts/fake_worker.py")
    config = load_config(tmp_path / "missing.yaml")
    assert config.execution.zcode_command == ["python3", "scripts/fake_worker.py"]
