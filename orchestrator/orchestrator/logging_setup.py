"""JSONL structured logging to logs/run_<timestamp>.jsonl (plan §3.6)."""

from pathlib import Path


def setup_logging(logs_dir: Path) -> Path:
    """Configure JSONL logging and return the run log file path."""
    raise NotImplementedError("Phase 6")
