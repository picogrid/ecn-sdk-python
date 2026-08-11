# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Bounded browser WebSocket fanout."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import UUID, uuid4


class BrowserLimitError(RuntimeError):
    """Raised when the configured browser-client bound is reached."""


class BrowserHubClosedError(RuntimeError):
    """Raised when a browser tries to register after deterministic shutdown."""


class BrowserHub:
    def __init__(self, *, maximum_clients: int, queue_size: int) -> None:
        self._maximum_clients = maximum_clients
        self._queue_size = queue_size
        self._clients: dict[UUID, asyncio.Queue[str | None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.dropped_messages = 0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register(self) -> tuple[UUID, asyncio.Queue[str | None]]:
        async with self._lock:
            if self._closed:
                raise BrowserHubClosedError("browser hub is closed")
            if len(self._clients) >= self._maximum_clients:
                raise BrowserLimitError("browser client limit reached")
            identifier = uuid4()
            queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self._queue_size)
            self._clients[identifier] = queue
            return identifier, queue

    async def unregister(self, identifier: UUID) -> None:
        async with self._lock:
            self._clients.pop(identifier, None)

    async def broadcast(self, message: str) -> None:
        async with self._lock:
            if self._closed:
                return
            for queue in self._clients.values():
                if queue.full():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    self.dropped_messages += 1
                queue.put_nowait(message)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            queues = tuple(self._clients.values())
            self._clients.clear()
            for queue in queues:
                while not queue.empty():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(None)


__all__ = ["BrowserHub", "BrowserHubClosedError", "BrowserLimitError"]
