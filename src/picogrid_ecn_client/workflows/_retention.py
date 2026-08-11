# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Private retention helpers shared by workflow implementations."""

from __future__ import annotations

from collections import deque
from typing import Generic, TypeVar

_EventT = TypeVar("_EventT")


class _EventRetention(Generic[_EventT]):
    """Retain all bounded events or a rolling unlimited-event window."""

    __slots__ = ("_events",)

    def __init__(self, *, limit: int, buffer_size: int) -> None:
        self._events: list[_EventT] | deque[_EventT]
        if limit:
            self._events = []
        else:
            self._events = deque(maxlen=buffer_size)

    def append(self, event: _EventT) -> None:
        self._events.append(event)

    def __len__(self) -> int:
        return len(self._events)

    def snapshot(self) -> tuple[_EventT, ...]:
        return tuple(self._events)
