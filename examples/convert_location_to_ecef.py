# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Convert a WGS-84 location to ECEF locally, without a network connection."""

from __future__ import annotations

from datetime import UTC, datetime
from math import hypot

from picogrid_ecn_client import ECEFPosition, Location, Velocity, workflows

if __package__:
    from ._common import emit, location_from_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        location_from_env,
        run_example,
    )


async def main() -> None:
    result = workflows.convert_location_to_ecef(
        location_from_env(default_source="convert-location-to-ecef")
    )
    emit(result.position)
    if result.velocity is not None:
        emit(result.velocity)


def _check() -> None:
    location = Location(
        latitude=34.05,
        longitude=-118.24,
        altitude=100.0,
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        velocity=Velocity(north=12.0, east=-3.0, down=0.5),
    )

    # The ECN carries geodetic coordinates only. This position is held locally and
    # is never published; publishing still uses the unchanged Location.
    position = location.to_ecef()
    if not isinstance(position, ECEFPosition):
        raise AssertionError("conversion did not produce an ECEF position")

    restored = position.to_geodetic()
    if abs(restored.altitude - 100.0) > 1e-6:
        raise AssertionError("round trip lost the ellipsoidal height")

    velocity = location.velocity
    assert velocity is not None
    rotated = location.to_ecef_velocity()
    ground_speed = hypot(velocity.north, velocity.east, velocity.down)
    if abs(hypot(rotated.x, rotated.y, rotated.z) - ground_speed) > 1e-9:
        raise AssertionError("velocity rotation did not preserve speed")


if __name__ == "__main__":
    run_example("convert location to ecef", main, _check)
