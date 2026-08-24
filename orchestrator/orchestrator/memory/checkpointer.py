"""Async LangGraph checkpointer bound to StateStore's shared SQLite connection.

langgraph 1.x requires async checkpoint savers under ``ainvoke``, but giving
the saver its own SQLite connection makes two writers fight over the write
lock ("database is locked"). ``SharedConnectionCheckpointer`` wraps the sync
``SqliteSaver`` running on StateStore's one connection and funnels every
checkpoint operation through StateStore's mutex, so all database access is
serialized in-process; blocking calls run in worker threads to keep the
event loop responsive.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver


class SharedConnectionCheckpointer(BaseCheckpointSaver):
    """Delegate async checkpointer API to a sync SqliteSaver under one lock."""

    def __init__(self, saver: SqliteSaver, guard: threading.Lock) -> None:
        super().__init__()
        self._saver = saver
        self._guard = guard

    def _call(self, op: Any) -> Any:
        with self._guard:
            return op()

    # -- sync API (delegates directly; usable from threads) -----------------

    def get_tuple(self, config: dict) -> Any:
        return self._call(lambda: self._saver.get_tuple(config))

    def put(self, *args: Any) -> Any:
        return self._call(lambda: self._saver.put(*args))

    def put_writes(self, *args: Any) -> Any:
        return self._call(lambda: self._saver.put_writes(*args))

    def delete_thread(self, thread_id: str) -> None:
        self._call(lambda: self._saver.delete_thread(thread_id))

    # -- async API required by langgraph 1.x ``ainvoke`` --------------------

    async def aget_tuple(self, config: dict) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, *args: Any) -> Any:
        return await asyncio.to_thread(self.put, *args)

    async def aput_writes(self, *args: Any) -> Any:
        return await asyncio.to_thread(self.put_writes, *args)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
