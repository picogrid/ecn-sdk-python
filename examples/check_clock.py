# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Measure the configured ECN's time relative to the local process clock."""

from __future__ import annotations

from datetime import UTC, datetime

from picogrid_ecn_client import (
    ClockEndpoint,
    ClockLeapState,
    ClockReport,
    ECNClient,
    workflows,
)

if __package__:
    from ._common import emit, env_float, env_int, load_config, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_float,
        env_int,
        load_config,
        run_example,
    )


async def main() -> None:
    client = ECNClient(load_config())
    samples = env_int("ECN_CLOCK_SAMPLES", default=3, minimum=1)
    timeout = env_float("ECN_CLOCK_TIMEOUT_SECONDS")
    maximum = env_float("ECN_CLOCK_MAX_OFFSET_SECONDS")
    try:
        result = await workflows.check_clock(
            client,
            samples=samples,
            timeout=timeout,
            max_offset_seconds=maximum,
        )
    except BaseException as primary_error:
        try:
            await client.close()
        except BaseException:
            primary_error.add_note("client cleanup also failed")
        raise
    await client.close()
    emit(result.report)


def _check() -> None:
    report = ClockReport(
        endpoint=ClockEndpoint(host="example.invalid"),
        offset_seconds=0.025,
        round_trip_delay_seconds=0.01,
        local_capture_uncertainty_seconds=0,
        jitter_seconds=0,
        spread_seconds=0,
        samples_requested=1,
        samples_completed=1,
        server_version=4,
        server_stratum=2,
        leap_state=ClockLeapState.NO_WARNING,
        measured_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if report.offset_seconds <= 0:
        raise AssertionError("positive clock offset must mean ECN time is ahead")


if __name__ == "__main__":
    run_example("clock diagnostic", main, _check)
