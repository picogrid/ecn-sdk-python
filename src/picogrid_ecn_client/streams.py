# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Bounded public async event stream."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar, cast

from .exceptions import ECNClientError
from .models import DeliveryPolicy

EventT = TypeVar("EventT")
_CLOSED = object()
CloseCallback: TypeAlias = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: ECNClientError


class EventStream(AsyncIterator[EventT], Generic[EventT]):
    """Iterate a bounded local queue of typed MQTT events.

    ``FIFO`` drops a newly arriving event when the queue is full. ``LATEST``
    instead evicts the oldest queued event and enqueues the new event. These
    policies do not change broker delivery semantics.
    """

    def __init__(
        self,
        *,
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
        on_close: CloseCallback | None = None,
    ) -> None:
        """Create a bounded stream for an owning watcher.

        Args:
            buffer_size: Fixed positive local queue size.
            delivery_policy: Local full-queue delivery policy.
            on_close: Optional callback that releases watcher ownership.
        """
        if buffer_size < 1:
            raise ValueError("buffer_size must be at least 1")
        self._queue: asyncio.Queue[EventT | _StreamFailure | object] = asyncio.Queue(
            maxsize=buffer_size
        )
        self._delivery_policy = delivery_policy
        self._on_close = on_close
        self._closed = False
        self._dropped_count = 0
        self._decode_error_count = 0
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._terminal_error: ECNClientError | None = None

    @property
    def buffer_size(self) -> int:
        """Return the fixed positive local queue size."""
        return self._queue.maxsize

    @property
    def delivery_policy(self) -> DeliveryPolicy:
        """Return the local full-queue delivery policy."""
        return self._delivery_policy

    @property
    def dropped_count(self) -> int:
        """Return decoded events dropped from this stream's bounded buffer."""

        return self._dropped_count

    @property
    def decode_error_count(self) -> int:
        """Return the number of matching payloads rejected by this decoder."""

        return self._decode_error_count

    @property
    def closed(self) -> bool:
        """Return whether close has begun and further delivery is disabled."""
        return self._closed

    def __aiter__(self) -> EventStream[EventT]:
        """Return this stream as its asynchronous iterator."""
        return self

    async def __anext__(self) -> EventT:
        """Return the next queued event, waiting until one arrives."""
        if self._closed and self._queue.empty():
            if self._terminal_error is not None:
                raise self._terminal_error
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        if isinstance(item, _StreamFailure):
            raise item.error
        return cast("EventT", item)

    def _put_nowait(self, item: EventT) -> bool:
        """Enqueue from the internal dispatcher without blocking it."""

        if self._closed:
            return False
        if not self._queue.full():
            self._queue.put_nowait(item)
            return True
        self._dropped_count += 1
        if self._delivery_policy is DeliveryPolicy.FIFO:
            return False
        self._queue.get_nowait()
        self._queue.put_nowait(item)
        return True

    def _record_decode_error(self) -> None:
        """Record one rejected inbound payload for the owning watcher."""

        if not self._closed:
            self._decode_error_count += 1

    async def _fail(self, error: ECNClientError) -> None:
        """Terminate this stream with one typed internal delivery failure.

        The first terminal action wins. Queued events are discarded so a consumer
        waiting on this stream observes the failure promptly, while the one existing
        close task releases watcher ownership exactly once.
        """

        async with self._close_lock:
            close_task = self._close_task
            if close_task is None:
                self._terminal_error = error
                self._closed = True
                self._replace_queue(_StreamFailure(error))
                close_task = asyncio.create_task(
                    self._finish_close(),
                    name="picogrid-ecn-event-stream-fail",
                )
                close_task.add_done_callback(self._consume_close_result)
                self._close_task = close_task
        await asyncio.shield(close_task)

    async def aclose(self) -> None:
        """Close the stream idempotently.

        Closing releases watcher ownership and its subscriptions when no other
        local consumer needs them. It also unblocks iteration waiting for an
        event.

        Raises:
            AuthorizationError: If the broker rejects the unsubscribe issued
                when this was the last local consumer of a filter.
            ProtocolError: If the broker returns a malformed UNSUBACK.
            ConnectionError: If the transport fails while unsubscribing.
        """

        async with self._close_lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                close_task = asyncio.create_task(
                    self._finish_close(),
                    name="picogrid-ecn-event-stream-close",
                )
                close_task.add_done_callback(self._consume_close_result)
                self._close_task = close_task
        await asyncio.shield(close_task)

    @staticmethod
    def _consume_close_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _finish_close(self) -> None:
        try:
            if self._on_close is not None:
                result = self._on_close()
                if inspect.isawaitable(result):
                    await result
        finally:
            marker: _StreamFailure | object = (
                _CLOSED if self._terminal_error is None else _StreamFailure(self._terminal_error)
            )
            self._replace_queue(marker)

    def _replace_queue(self, marker: _StreamFailure | object) -> None:
        """Replace buffered values with one terminal marker without blocking."""

        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(marker)


__all__ = ["EventStream"]
