# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Stable ECN-relative clock diagnostic facade."""

from __future__ import annotations

from typing import Protocol

from ..models.clock import ClockReport


class _ClockClient(Protocol):
    async def _measure_clock(
        self,
        *,
        samples: int,
        timeout: float | None,
    ) -> ClockReport: ...

    async def _require_clock_within(
        self,
        *,
        max_offset_seconds: float,
        samples: int,
        timeout: float | None,
    ) -> ClockReport: ...


class Clock:
    """Measure the configured ECN's time relative to the local process clock."""

    def __init__(self, client: _ClockClient) -> None:
        self.__client = client

    async def measure(
        self,
        *,
        samples: int = 3,
        timeout: float | None = None,
    ) -> ClockReport:
        """Return one bounded report; positive offset means ECN time is ahead.

        Args:
            samples: Number of NTP samples to attempt, from 1 through 10. The
                report describes the valid sample with the lowest round-trip
                delay.
            timeout: Whole-measurement timeout in seconds, greater than 0
                through 60. ``None`` uses the client's configured operation
                timeout.

        Returns:
            One completed measurement of ECN time minus local time.

        Raises:
            NotReadyError: If the client is closed.
            ResourceLimitError: If another measurement is already active.
            TimeoutError: If the measurement exceeds ``timeout``.
            ClockError: If the endpoint cannot be measured.
            ClockProtocolError: If no usable NTP response is obtained.
            ValidationError: If ``samples`` or ``timeout`` is out of range.
        """

        return await self.__client._measure_clock(samples=samples, timeout=timeout)

    async def require_within(
        self,
        *,
        max_offset_seconds: float,
        samples: int = 3,
        timeout: float | None = None,
    ) -> ClockReport:
        """Return a report or raise unless offset plus local uncertainty is within.

        Args:
            max_offset_seconds: Absolute tolerance in seconds, from 0 through
                86,400. The check compares the absolute offset plus the local
                capture uncertainty against this bound.
            samples: Number of NTP samples to attempt, from 1 through 10.
            timeout: Whole-measurement timeout in seconds, greater than 0
                through 60. ``None`` uses the client's configured operation
                timeout.

        Returns:
            The completed measurement, whose ``within_tolerance`` is true.

        Raises:
            NotReadyError: If the client is closed.
            ClockToleranceError: If the measurement exceeds the tolerance; the
                error carries the report.
            ResourceLimitError: If another measurement is already active.
            TimeoutError: If the measurement exceeds ``timeout``.
            ClockError: If the endpoint cannot be measured.
            ClockProtocolError: If no usable NTP response is obtained.
            ValidationError: If an argument is out of range.
        """

        return await self.__client._require_clock_within(
            max_offset_seconds=max_offset_seconds,
            samples=samples,
            timeout=timeout,
        )


__all__ = ["Clock"]
