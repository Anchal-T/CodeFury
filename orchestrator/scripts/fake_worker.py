"""Fake worker command for demos and manual testing (dev utility).

Stands in for a real coding-agent CLI: it receives the task prompt as its
last argument (exactly like ZCodeRunner passes it) and makes a small, real
code change in the current working directory.

Usage in the worker config (path relative to the worktree root, which is
the repo root — the runner executes with cwd set to the worktree):
    ZCODE_CMD="python3 orchestrator/scripts/fake_worker.py" python -m orchestrator run --goal "..."

Modes (environment variables):
    FAKE_WORKER_FAIL=1              simulate a failing worker (retry demos)
    FAKE_WORKER_SLEEP_S=<seconds>   sleep before doing the work — lets demos
                                    and kill/resume tests catch a run
                                    mid-flight
    FAKE_WORKER_TARGET=<relpath>    write that file instead of hash modules;
                                    content embeds the prompt hash so two
                                    workers on one file genuinely conflict
    FAKE_WORKER_CONTENT=<text>      extra verbatim line appended to TARGET
    FAKE_WORKER_ROLE=<name>         label written into the output (e.g.
                                    "reconciler" for Domain Lead demos)
    FAKE_WORKER_TOKENS=<n>          token usage reported on stdout as a
                                    'TOKENS_USED: <n>' line (default 1000) —
                                    the orchestrator parses it for budget
                                    accounting (Phase 6)
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

DEFAULT_TOKENS = 1000


def main() -> int:
    prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
    if os.environ.get("FAKE_WORKER_FAIL") == "1":
        print("fake worker: simulated failure (FAKE_WORKER_FAIL=1)", file=sys.stderr)
        return 1

    sleep_s = float(os.environ.get("FAKE_WORKER_SLEEP_S", "0"))
    if sleep_s > 0:
        print(f"fake worker: sleeping {sleep_s}s before working", flush=True)
        time.sleep(sleep_s)

    # Reconciliation dispatches are recognized from the orchestrator's
    # '<task>-reconcile-<n>' id convention embedded in the prompt; an explicit
    # FAKE_WORKER_ROLE overrides the detection.
    role = os.environ.get("FAKE_WORKER_ROLE")
    if not role:
        role = "reconciler" if "-reconcile-" in prompt else "worker"
    tokens_used = int(os.environ.get("FAKE_WORKER_TOKENS", DEFAULT_TOKENS))
    print(f"TOKENS_USED: {tokens_used}")
    target = os.environ.get("FAKE_WORKER_TARGET")
    if target:
        digest = hashlib.sha1(prompt.encode()).hexdigest()[:8]
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"target: {target}",
            f"written by: fake {role}",
            f"prompt sha: {digest}",
        ]
        extra = os.environ.get("FAKE_WORKER_CONTENT")
        if extra:
            lines.append(extra)
        target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"fake {role}: wrote {target}")
        return 0

    # Each task writes its own module (named from the prompt hash) so that
    # parallel workers on different tasks never collide on merge.
    module = f"module_{hashlib.sha1(prompt.encode()).hexdigest()[:8]}"
    Path(f"{module}.py").write_text(
        f'"""{module} written by the fake worker."""\n\n\ndef greet(name: str) -> str:\n    return f"hello from {module}, {{name}}"\n',
        encoding="utf-8",
    )
    Path(f"{module}.txt").write_text(
        f"fake worker executed\nprompt was {len(prompt)} chars\n",
        encoding="utf-8",
    )
    print(f"fake worker: wrote {module}.py and {module}.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
