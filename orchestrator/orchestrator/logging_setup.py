"""JSONL structured logging to logs/run_<timestamp>.jsonl (plan §3.6).

One append-only file per invocation; every event is a single JSON line
(timestamp, event kind, arbitrary fields) flushed immediately so a
concurrent ``orchestrator logs --tail`` sees events as they happen.
Injected into pipelines/graphs as an optional collaborator — ``None``
means silent, keeping unit tests and non-logging callers unchanged.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunLogger:
    """Thread-safe JSONL event writer; one line per event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def event(self, kind: str, **fields: object) -> None:
        """Append one event record and flush it to disk."""
        record = {"ts": _utc_now().isoformat(), "event": kind, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def setup_logging(logs_dir: Path) -> RunLogger:
    """Open this run's log file: <logs_dir>/run_<UTC timestamp>.jsonl."""
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    unique_id = uuid4().hex[:8]
    return RunLogger(logs_dir / f"run_{stamp}_{unique_id}.jsonl")
