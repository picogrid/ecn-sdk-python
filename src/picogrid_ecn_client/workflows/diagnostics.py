# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable diagnostic workflows."""

from __future__ import annotations

from pydantic import Field

from picogrid_ecn_client.client import ECNClient
from picogrid_ecn_client.models import ClockReport, PreflightReport
from picogrid_ecn_client.models._base import PublicModel


class PreflightResult(PublicModel):
    """Structured result of the public connectivity preflight.

    Attributes:
        report: Public report containing every completed preflight check.
    """

    report: PreflightReport = Field(
        description="Public report containing every completed preflight check."
    )


class CheckClockResult(PublicModel):
    """Structured result of an ECN-relative clock measurement.

    Attributes:
        report: Public clock report selected from the completed samples.
    """

    report: ClockReport = Field(
        description="Public clock report selected from the completed samples."
    )


async def preflight(client: ECNClient) -> PreflightResult:
    """Run the public, read-only ECN preflight checks.

    Args:
        client: Configured SDK client to diagnose.

    Returns:
        The typed preflight report result.

    Raises:
        ECNClientError: If a connectivity or authorization check cannot complete.
    """

    return PreflightResult(report=await client.preflight())


async def check_clock(
    client: ECNClient,
    *,
    samples: int = 3,
    timeout: float | None = None,
    max_offset_seconds: float | None = None,
) -> CheckClockResult:
    """Measure ECN-relative clock offset and optionally enforce a maximum offset.

    Args:
        client: Configured SDK client whose clock endpoint will be queried.
        samples: Number of valid NTP samples to collect.
        timeout: Optional total measurement timeout in seconds.
        max_offset_seconds: Optional maximum permitted absolute offset in seconds.

    Returns:
        The typed clock measurement result.

    Raises:
        ClockError: If the measurement fails.
        ClockToleranceError: If ``max_offset_seconds`` is exceeded.
        ValidationError: If a numeric argument is outside its supported range.
    """

    if max_offset_seconds is None:
        report = await client.clock.measure(samples=samples, timeout=timeout)
    else:
        report = await client.clock.require_within(
            max_offset_seconds=max_offset_seconds,
            samples=samples,
            timeout=timeout,
        )
    return CheckClockResult(report=report)


__all__ = ["CheckClockResult", "PreflightResult", "check_clock", "preflight"]
