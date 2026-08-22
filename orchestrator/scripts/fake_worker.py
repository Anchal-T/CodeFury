"""Fake worker command for demos and manual testing (dev utility).

Stands in for a real coding-agent CLI: it receives the task prompt as its
last argument (exactly like ZCodeRunner passes it) and makes a small, real
code change in the current working directory.

Usage in the worker config (path relative to the worktree root, which is
the repo root — the runner executes with cwd set to the worktree):
    ZCODE_CMD="python3 orchestrator/scripts/fake_worker.py" python -m orchestrator run --goal "..."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
    if os.environ.get("FAKE_WORKER_FAIL") == "1":
        print("fake worker: simulated failure (FAKE_WORKER_FAIL=1)", file=sys.stderr)
        return 1
    Path("greeting.py").write_text(
        '"""Greeting module written by the fake worker."""\n\n\ndef greet(name: str) -> str:\n    return f"hello from worker, {name}"\n',
        encoding="utf-8",
    )
    Path("hello_from_worker.txt").write_text(
        f"fake worker executed\nprompt was {len(prompt)} chars\n",
        encoding="utf-8",
    )
    print("fake worker: wrote greeting.py and hello_from_worker.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
