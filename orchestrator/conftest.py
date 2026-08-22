"""Root conftest: puts the project dir on sys.path and provides a reliable
python interpreter for subprocess-based tests.

The interpreter probe exists because in some environments ``sys.executable``
is an application bundle that may boot a GUI instead of executing ``-c``
snippets; tests must use an interpreter that verifiably works.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def python_bin() -> str:
    """Return a python executable that runs ``-c`` snippets reliably."""
    marker = "orchestrator-python-probe"
    candidates = [
        sys.executable,
        shutil.which("python3") or "",
        "/usr/bin/python3",
        "/usr/bin/python3.12",
    ]
    for candidate in dict.fromkeys(c for c in candidates if c):
        try:
            proc = subprocess.run(
                [candidate, "-c", f"print('{marker}')"],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.stdout.strip() == marker:
            return candidate
    pytest.fail("no usable python interpreter found for subprocess tests")
