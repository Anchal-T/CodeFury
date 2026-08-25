"""Cross-platform audit (plan §7, §9 Phase 8), enforced by the suite.

The repo's portability rules are conventions until a test makes violating
them impossible: pathlib-only paths, list-form subprocess calls without a
shell, psutil for process-tree kills, and no heavyweight dependencies.
"""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "orchestrator"
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

#: Patterns that break Windows or violate plan §3.3/§7 if they appear in
#: package source (tests may use them to assert absence).
BANNED_SOURCE_PATTERNS = (
    "shell=True",      # list-form + shell=False only (plan §3.3)
    "os.system(",      # never spawn via the shell
    "os.popen(",       # ditto
    "subprocess.check_output(",  # unreviewed convenience wrapper; use run()
    "os.path.join(",   # pathlib.Path only (AGENTS.md)
    "C:\\\\",          # hardcoded Windows paths
)

#: Dependencies AGENTS.md forbids without explicit approval.
BANNED_DEPENDENCIES = ("torch", "docker", "redis")


def package_sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_package_uses_no_shell_or_legacy_path_apis() -> None:
    violations = []
    for source in package_sources():
        text = source.read_text(encoding="utf-8")
        for pattern in BANNED_SOURCE_PATTERNS:
            if pattern in text:
                violations.append(f"{source.name}: {pattern!r}")
    assert not violations, f"portability rules violated:\n" + "\n".join(violations)


def test_process_tree_kills_go_through_psutil() -> None:
    """Only the runner may terminate processes, and only via psutil so
    grandchildren get reaped on Windows too (plan §3.3)."""
    runner = (PACKAGE_ROOT / "execution" / "zcode_runner.py").read_text(encoding="utf-8")
    assert "import psutil" in runner
    assert "terminate()" in runner and "kill()" in runner

    for source in package_sources():
        if source.name == "zcode_runner.py":
            continue
        text = source.read_text(encoding="utf-8")
        assert ".terminate()" not in text, f"{source.name} kills outside the runner"
        assert ".kill()" not in text, f"{source.name} kills outside the runner"


def test_requirements_stay_lightweight() -> None:
    declared = REQUIREMENTS.read_text(encoding="utf-8").lower()
    for dependency in BANNED_DEPENDENCIES:
        assert dependency not in declared, f"{dependency} needs explicit approval (AGENTS.md)"


def test_spawned_commands_are_built_from_lists() -> None:
    """No string-form commands at spawn sites: ``subprocess.run("cmd ...")``
    would need a shell to parse; argv lists never do."""
    violations = []
    for source in package_sources():
        text = source.read_text(encoding="utf-8")
        for call in ("subprocess.run(", "subprocess.Popen(", "subprocess.check_call("):
            start = 0
            while True:
                found = text.find(call, start)
                if found == -1:
                    break
                after = text[found + len(call):].lstrip()
                if after.startswith(('"', "'", "f\"", "f'")):
                    line_no = text.count("\n", 0, found) + 1
                    violations.append(f"{source.name}:{line_no} spawns a string command")
                start = found + 1
    assert not violations, "list-form subprocess only:\n" + "\n".join(violations)
