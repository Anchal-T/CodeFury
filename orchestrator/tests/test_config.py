"""Tests for orchestrator.config."""

from pathlib import Path

import pytest

from orchestrator.config import DEFAULT_TEST_COMMAND, load_config

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@pytest.fixture(autouse=True)
def _no_zcode_cmd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient ZCODE_CMD (e.g. set for a demo run) out of file-loading tests."""
    monkeypatch.delenv("ZCODE_CMD", raising=False)


def test_committed_config_parses() -> None:
    """Smoke-check only: the committed config.yaml is a tuned deployment
    artifact — its operational values must not be pinned here."""
    assert load_config(REPO_CONFIG).raw


def test_explicit_values_load_from_fixture(tmp_path: Path) -> None:
    """Loader behavior verified against a dedicated fixture, decoupled from
    the repo's tunable config.yaml."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "execution:\n"
        "  zcode_command: [/usr/bin/python3, agent.py]\n"
        "  worker_timeout_s: 60\n"
        "paths:\n"
        "  db: ./custom.db\n"
        "  workspaces: ./ws\n"
        "retries:\n"
        "  max_worker_retries: 1\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.execution.zcode_command == ["/usr/bin/python3", "agent.py"]
    assert config.execution.worker_timeout_s == 60.0
    assert config.paths.db == Path("./custom.db")
    assert config.paths.workspaces == Path("./ws")
    assert config.retries.max_worker_retries == 1


def test_missing_file_falls_back_to_defaults() -> None:
    config = load_config(Path("does-not-exist.yaml"))
    assert config.execution.zcode_command == ["zcode"]
    assert config.execution.test_command == DEFAULT_TEST_COMMAND
    assert config.paths.db == Path("./data/orchestrator.db")
    assert config.paths.logs == Path("./logs")
    assert config.budgets.worker_tokens == 50000
    assert config.budgets.manager_tokens == 150000
    assert config.budgets.domain_lead_tokens == 300000
    assert config.budgets.architect_tokens == 500000


def test_budgets_load_from_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "budgets:\n"
        "  worker_tokens: 100\n"
        "  manager_tokens: 200\n"
        "  domain_lead_tokens: 300\n"
        "  architect_tokens: 400\n",
        encoding="utf-8",
    )
    budgets = load_config(config_file).budgets
    assert budgets.worker_tokens == 100
    assert budgets.manager_tokens == 200
    assert budgets.domain_lead_tokens == 300
    assert budgets.architect_tokens == 400


def test_null_budget_disables_cap(tmp_path: Path) -> None:
    """An explicit null means 'no cap' — distinct from a missing key, which
    falls back to the tuned default (a surprise cap on fresh checkouts)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("budgets:\n  worker_tokens: null\n", encoding="utf-8")
    budgets = load_config(config_file).budgets
    assert budgets.worker_tokens is None
    assert budgets.manager_tokens == 150000


def test_negative_budget_rejected(tmp_path: Path) -> None:
    """A negative cap would block every dispatch instantly while looking like
    a tuned value — reject it at load time with the key named."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("budgets:\n  manager_tokens: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manager_tokens"):
        load_config(config_file)


def test_non_numeric_budget_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("budgets:\n  worker_tokens: lots\n", encoding="utf-8")
    with pytest.raises(ValueError, match="worker_tokens"):
        load_config(config_file)


def test_logs_path_override(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("paths:\n  logs: ./var/runlog\n", encoding="utf-8")
    assert load_config(config_file).paths.logs == Path("./var/runlog")


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


def test_existing_but_unusable_config_path_warns(tmp_path: Path, caplog) -> None:
    """A directory (or unreadable file) at an explicit path is a likely
    operator mistake — surface a warning instead of silently defaulting."""
    import logging

    directory = tmp_path / "config.yaml"
    directory.mkdir()
    with caplog.at_level(logging.WARNING, logger="orchestrator.config"):
        config = load_config(directory)
    assert config.execution.zcode_command  # defaults still apply
    assert any("not a readable file" in record.message for record in caplog.records)


def test_missing_config_path_stays_silent(tmp_path: Path, caplog) -> None:
    """Missing-file fallback is intentional (fresh checkouts); no warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="orchestrator.config"):
        load_config(tmp_path / "does-not-exist.yaml")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_non_positive_max_workers_rejected(tmp_path: Path) -> None:
    """max_workers: 0 would deadlock every dispatch (Semaphore(0) is never
    acquirable) — invalid configuration must fail fast at load time."""
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
