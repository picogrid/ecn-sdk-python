# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Observe the next terminal-geolocation MQTT update; this is not a server query."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import ECNClient, Location, LocationEvent, Locations
from picogrid_ecn_client.workflows import get_ecn_location

if __package__:
    from ._common import emit, env_float, load_config, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_float,
        load_config,
        run_example,
    )


async def main() -> None:
    config = load_config()
    timeout = env_float("ECN_OBSERVATION_TIMEOUT")
    async with ECNClient(config) as client:
        result = await get_ecn_location(client, timeout=timeout)
    emit(result.event)


def _check() -> None:
    if not callable(Locations.wait_for_terminal_geolocation):
        raise AssertionError("terminal-geolocation observation API is unavailable")
    recorded_at = datetime(2026, 1, 1, tzinfo=UTC)
    LocationEvent(
        entity_id=UUID("00000000-0000-4000-8000-000000000206"),
        integration="terminal-geolocation",
        timestamp=recorded_at,
        location=Location(
            latitude=0,
            longitude=0,
            recorded_at=recorded_at,
            source="terminal-geolocation",
        ),
    )


if __name__ == "__main__":
    run_example("get MQTT-observed ECN location", main, _check)
