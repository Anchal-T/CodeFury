"""Cross-platform ZCode CLI subprocess wrapper (plan §3.3).

One Worker task = one subprocess.Popen(args_list, shell=False) with the Task
JSON serialized into the prompt and cwd set to the worker's git worktree.
Whole-process-tree termination on timeout via psutil (Windows-safe).
"""

from pathlib import Path


class ZCodeRunner:
    """Runs a single ZCode CLI subprocess per Worker task."""

    def run(self, prompt: str, cwd: Path, timeout: float) -> str:
        """Run ZCode in cwd, kill the process tree on timeout, return output."""
        raise NotImplementedError("Phase 1")

    def kill_tree(self, pid: int) -> None:
        """Terminate a process and all its children (psutil recursive)."""
        raise NotImplementedError("Phase 1")
