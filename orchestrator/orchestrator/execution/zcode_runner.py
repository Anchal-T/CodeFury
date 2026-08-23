"""Cross-platform worker subprocess wrapper (plan §3.3).

One Worker task = one subprocess.Popen(args_list, shell=False) with the Task
prompt passed to the worker command and cwd set to the worker's git worktree.
The command is injectable (config.yaml ``execution.zcode_command`` or the
``ZCODE_CMD`` environment variable) because the coding agent CLI may differ
per machine. Whole-process-tree termination on timeout via psutil, which is
the only reliable way to reap children on Windows.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import psutil

PROMPT_PLACEHOLDER = "{prompt}"
TREE_KILL_GRACE_S = 5.0


@dataclass
class RunnerResult:
    """Outcome of one worker subprocess run."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class ZCodeRunner:
    """Runs a single worker subprocess per Worker task."""

    def __init__(
        self,
        command: list[str] | None = None,
        timeout: float = 1800.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.command = list(command) if command else ["zcode"]
        self.timeout = timeout
        self.env = dict(env) if env else None

    def build_args(self, prompt: str) -> list[str]:
        """Insert the prompt into the command.

        If any configured argument contains a ``{prompt}`` placeholder —
        including inside larger tokens like ``--prompt={prompt}`` — it is
        substituted in place; otherwise the prompt is appended as the last
        argument.
        """
        if any(PROMPT_PLACEHOLDER in arg for arg in self.command):
            return [arg.replace(PROMPT_PLACEHOLDER, prompt) for arg in self.command]
        return [*self.command, prompt]

    def run(self, prompt: str, cwd: Path) -> RunnerResult:
        """Run the worker command in cwd, killing its process tree on timeout."""
        args = self.build_args(prompt)
        start = time.monotonic()
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=TREE_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                # Uninterruptible survivor: last-resort kill, then reap.
                proc.kill()
                stdout, stderr = proc.communicate()
        return RunnerResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            duration_s=time.monotonic() - start,
        )

    def kill_tree(self, pid: int) -> None:
        """Terminate a process and all its descendants, then kill survivors.

        The root is terminated first, then descendants are re-swept until no
        new pids appear so grandchildren spawned mid-teardown cannot escape.
        psutil.AccessDenied (protected/elevated processes) is swallowed — the
        caller's bounded communicate() is the final backstop.
        """
        killed: set[int] = {pid}
        try:
            root = psutil.Process(pid)
            root.terminate()
        except psutil.NoSuchProcess:
            return
        while True:
            try:
                current = [root, *root.children(recursive=True)]
            except psutil.NoSuchProcess:
                break
            fresh = [proc for proc in current if proc.pid not in killed]
            if not fresh:
                break
            for target in fresh:
                killed.add(target.pid)
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    pass
        targets = []
        for proc_id in killed:
            try:
                targets.append(psutil.Process(proc_id))
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=TREE_KILL_GRACE_S)
        for survivor in alive:
            try:
                survivor.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                pass
