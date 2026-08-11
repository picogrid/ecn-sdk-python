# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import math

import pytest

from picogrid_ecn_client._geodesy import (
    INVERSE_FLATTENING,
    SEMI_MAJOR_AXIS_M,
    ecef_to_geodetic,
    ecef_to_ned_velocity,
    geodetic_to_ecef,
    ned_to_ecef_velocity,
)
from picogrid_ecn_client.exceptions import ValidationError

SEMI_MINOR_AXIS_M = SEMI_MAJOR_AXIS_M * (1.0 - 1.0 / INVERSE_FLATTENING)

# The documented conversion tolerances. Position is bounded absolutely because
# the float64 floor at Earth radius is roughly 1.4e-9 m; velocity is a pure
# rotation, so its bound is relative to the speed being rotated.
POSITION_TOLERANCE_M = 1e-6
VELOCITY_RELATIVE_TOLERANCE = 1e-12

SUPPORTED_ALTITUDES_M = (-11000.0, 0.0, 1000.0, 100000.0)


def sweep() -> list[tuple[float, float, float]]:
    """Points spanning the full declared domain, poles and antimeridian included."""

    latitudes = [-90.0 + 5.0 * step for step in range(37)]
    longitudes = [-180.0 + 15.0 * step for step in range(25)]
    return [
        (latitude, longitude, altitude)
        for latitude in latitudes
        for longitude in longitudes
        for altitude in SUPPORTED_ALTITUDES_M
    ]


SWEEP = sweep()


def distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.dist(left, right)


def test_geodetic_round_trip_closes_within_tolerance() -> None:
    """The documented metric: Euclidean distance in ECEF metres after re-converting."""

    worst = 0.0
    for latitude, longitude, altitude in SWEEP:
        position = geodetic_to_ecef(latitude, longitude, altitude)
        restored = geodetic_to_ecef(*ecef_to_geodetic(*position))
        worst = max(worst, distance(position, restored))
    assert worst <= POSITION_TOLERANCE_M


def test_inverse_recovers_the_declared_altitude() -> None:
    worst = 0.0
    for latitude, longitude, altitude in SWEEP:
        _, _, recovered = ecef_to_geodetic(*geodetic_to_ecef(latitude, longitude, altitude))
        worst = max(worst, abs(recovered - altitude))
    assert worst <= POSITION_TOLERANCE_M


def test_inverse_recovers_latitude_and_longitude_away_from_the_poles() -> None:
    for latitude, longitude, altitude in SWEEP:
        if abs(latitude) == 90.0:
            continue
        recovered_latitude, recovered_longitude, _ = ecef_to_geodetic(
            *geodetic_to_ecef(latitude, longitude, altitude)
        )
        assert recovered_latitude == pytest.approx(latitude, abs=1e-12)
        if abs(longitude) == 180.0:
            # Either sign is correct on the antimeridian; the signed-zero
            # convention is asserted separately.
            assert abs(recovered_longitude) == pytest.approx(180.0, abs=1e-12)
        else:
            assert recovered_longitude == pytest.approx(longitude, abs=1e-12)


def test_a_longitude_sign_inversion_is_distinguishable() -> None:
    """Guards the check above: comparing magnitudes would accept a mirrored longitude."""

    for latitude, longitude, altitude in SWEEP:
        if abs(latitude) == 90.0 or longitude == 0.0:
            # A pole has no longitude, and the prime meridian is its own mirror.
            continue
        x, y, _ = geodetic_to_ecef(latitude, longitude, altitude)
        mirrored = math.degrees(math.atan2(-y, x))
        assert mirrored != pytest.approx(longitude, abs=1e-12)
        assert abs(mirrored) == pytest.approx(abs(longitude), abs=1e-12)


def test_surface_points_satisfy_the_ellipsoid_equation() -> None:
    for latitude, longitude, _ in SWEEP:
        x, y, z = geodetic_to_ecef(latitude, longitude, 0.0)
        residual = (x * x + y * y) / SEMI_MAJOR_AXIS_M**2 + (z / SEMI_MINOR_AXIS_M) ** 2
        assert residual == pytest.approx(1.0, abs=1e-12)


def test_altitude_displaces_along_the_ellipsoid_normal() -> None:
    offset = 750.0
    for latitude, longitude, altitude in SWEEP:
        lower = geodetic_to_ecef(latitude, longitude, altitude)
        upper = geodetic_to_ecef(latitude, longitude, altitude + offset)
        assert distance(lower, upper) == pytest.approx(offset, abs=POSITION_TOLERANCE_M)


def test_longitude_shift_rotates_about_the_polar_axis() -> None:
    shift = 37.0
    for latitude, longitude, altitude in SWEEP:
        if longitude + shift > 180.0:
            continue
        x, y, z = geodetic_to_ecef(latitude, longitude, altitude)
        shifted_x, shifted_y, shifted_z = geodetic_to_ecef(latitude, longitude + shift, altitude)
        angle = math.radians(shift)
        assert shifted_x == pytest.approx(
            x * math.cos(angle) - y * math.sin(angle), abs=POSITION_TOLERANCE_M
        )
        assert shifted_y == pytest.approx(
            x * math.sin(angle) + y * math.cos(angle), abs=POSITION_TOLERANCE_M
        )
        assert shifted_z == pytest.approx(z, abs=POSITION_TOLERANCE_M)


def test_latitude_sign_flip_mirrors_only_the_polar_component() -> None:
    for latitude, longitude, altitude in SWEEP:
        x, y, z = geodetic_to_ecef(latitude, longitude, altitude)
        mirrored_x, mirrored_y, mirrored_z = geodetic_to_ecef(-latitude, longitude, altitude)
        assert mirrored_x == pytest.approx(x, abs=POSITION_TOLERANCE_M)
        assert mirrored_y == pytest.approx(y, abs=POSITION_TOLERANCE_M)
        assert mirrored_z == pytest.approx(-z, abs=POSITION_TOLERANCE_M)


def test_swapping_latitude_and_longitude_moves_the_point() -> None:
    """An asymmetric point; (0, 0) and (45, 45) survive a swapped implementation."""

    upright = geodetic_to_ecef(34.05, 78.9, 100.0)
    swapped = geodetic_to_ecef(78.9, 34.05, 100.0)
    assert distance(upright, swapped) > 1e6


def test_the_equator_lies_on_the_semi_major_axis() -> None:
    x, y, z = geodetic_to_ecef(0.0, 0.0, 0.0)
    assert x == SEMI_MAJOR_AXIS_M
    assert y == 0.0
    assert z == 0.0


def test_the_poles_lie_on_the_semi_minor_axis() -> None:
    for sign in (1.0, -1.0):
        x, y, z = geodetic_to_ecef(sign * 90.0, 0.0, 0.0)
        assert x == 0.0
        assert y == 0.0
        assert z == pytest.approx(sign * SEMI_MINOR_AXIS_M, abs=POSITION_TOLERANCE_M)


def test_longitude_is_reported_as_zero_on_the_polar_axis() -> None:
    for sign in (1.0, -1.0):
        latitude, longitude, altitude = ecef_to_geodetic(0.0, 0.0, sign * SEMI_MINOR_AXIS_M)
        assert latitude == sign * 90.0
        assert longitude == 0.0
        assert altitude == pytest.approx(0.0, abs=POSITION_TOLERANCE_M)
        recovered_latitude, recovered_longitude, _ = ecef_to_geodetic(
            *geodetic_to_ecef(sign * 90.0, 73.0, 0.0)
        )
        assert recovered_latitude == sign * 90.0
        assert recovered_longitude == 0.0


def test_signed_zero_decides_the_antimeridian_sign() -> None:
    """atan2 resolves the ambiguity; the sign of the Y component picks the side."""

    _, positive, _ = ecef_to_geodetic(-SEMI_MAJOR_AXIS_M, 0.0, 0.0)
    _, negative, _ = ecef_to_geodetic(-SEMI_MAJOR_AXIS_M, -0.0, 0.0)
    assert positive == 180.0
    assert negative == -180.0


def test_positions_near_the_center_of_the_ellipsoid_are_rejected() -> None:
    for point in ((0.0, 0.0, 0.0), (1000.0, -2000.0, 500.0)):
        with pytest.raises(ValidationError) as raised:
            ecef_to_geodetic(*point)
        assert raised.value.code == "degenerate_ecef"


def test_the_conversion_holds_beyond_the_degenerate_region() -> None:
    latitude, longitude, altitude = ecef_to_geodetic(1_000_000.0, 0.0, 0.0)
    assert latitude == pytest.approx(0.0, abs=1e-12)
    assert longitude == pytest.approx(0.0, abs=1e-12)
    assert altitude < 0.0


def test_the_local_frame_is_an_orthonormal_right_handed_basis() -> None:
    for latitude, longitude, _ in SWEEP:
        rows = [
            ned_to_ecef_velocity(latitude, longitude, *axis)
            for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        ]
        north, east, down = rows
        for axis in rows:
            assert math.hypot(*axis) == pytest.approx(1.0, abs=1e-12)
        for left, right in ((north, east), (north, down), (east, down)):
            assert sum(a * b for a, b in zip(left, right, strict=True)) == pytest.approx(
                0.0, abs=1e-12
            )
        determinant = (
            north[0] * (east[1] * down[2] - east[2] * down[1])
            - north[1] * (east[0] * down[2] - east[2] * down[0])
            + north[2] * (east[0] * down[1] - east[1] * down[0])
        )
        assert determinant == pytest.approx(1.0, abs=1e-12)


def test_east_has_no_polar_component_anywhere() -> None:
    """A cyclic permutation of the rotation stays orthonormal; this catches it."""

    for latitude, longitude, _ in SWEEP:
        assert ned_to_ecef_velocity(latitude, longitude, 0.0, 1.0, 0.0)[2] == 0.0


def test_down_points_towards_the_center_of_the_ellipsoid() -> None:
    for latitude, longitude, _ in SWEEP:
        position = geodetic_to_ecef(latitude, longitude, 0.0)
        descended = geodetic_to_ecef(latitude, longitude, -1000.0)
        motion = ned_to_ecef_velocity(latitude, longitude, 0.0, 0.0, 1000.0)
        for moved, expected in zip(
            motion, (b - a for a, b in zip(position, descended, strict=True)), strict=True
        ):
            assert moved == pytest.approx(expected, abs=POSITION_TOLERANCE_M)


def test_north_points_towards_increasing_latitude() -> None:
    for latitude, longitude, _ in SWEEP:
        if abs(latitude) == 90.0:
            continue
        position = geodetic_to_ecef(latitude, longitude, 0.0)
        advanced = geodetic_to_ecef(latitude + 0.001, longitude, 0.0)
        motion = ned_to_ecef_velocity(latitude, longitude, 1.0, 0.0, 0.0)
        travelled = [b - a for a, b in zip(position, advanced, strict=True)]
        assert sum(a * b for a, b in zip(motion, travelled, strict=True)) > 0.0


def test_velocity_rotation_preserves_speed() -> None:
    velocity = (12.5, -3.25, 7.0)
    speed = math.hypot(*velocity)
    for latitude, longitude, _ in SWEEP:
        rotated = ned_to_ecef_velocity(latitude, longitude, *velocity)
        assert math.hypot(*rotated) == pytest.approx(speed, rel=VELOCITY_RELATIVE_TOLERANCE)


def test_velocity_round_trip_closes_within_tolerance() -> None:
    velocity = (12.5, -3.25, 7.0)
    speed = math.hypot(*velocity)
    for latitude, longitude, _ in SWEEP:
        rotated = ned_to_ecef_velocity(latitude, longitude, *velocity)
        restored = ecef_to_ned_velocity(latitude, longitude, *rotated)
        for component, expected in zip(restored, velocity, strict=True):
            assert abs(component - expected) <= VELOCITY_RELATIVE_TOLERANCE * speed


def test_a_zero_velocity_converts_to_a_zero_vector() -> None:
    """Negative signed zero compares equal to 0.0, so direct equality is sufficient."""

    for latitude, longitude, _ in SWEEP:
        ecef = ned_to_ecef_velocity(latitude, longitude, 0.0, 0.0, 0.0)
        ned = ecef_to_ned_velocity(latitude, longitude, 0.0, 0.0, 0.0)
        assert all(component == 0.0 for component in ecef)
        assert all(component == 0.0 for component in ned)


def test_a_purely_eastward_velocity_is_tangent_to_the_parallel() -> None:
    for latitude, longitude, _ in SWEEP:
        x, y, z = ned_to_ecef_velocity(latitude, longitude, 0.0, 25.0, 0.0)
        position = geodetic_to_ecef(latitude, longitude, 0.0)
        assert z == 0.0
        assert x * position[0] + y * position[1] == pytest.approx(0.0, abs=1e-6)
