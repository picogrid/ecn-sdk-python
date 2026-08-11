# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client import (
    ECEFPosition,
    ECEFVelocity,
    GeodeticPosition,
    Location,
    Velocity,
)
from picogrid_ecn_client.exceptions import ValidationError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "wgs84_ecef.json"

# The documented tolerances. Position is absolute because the float64 floor at
# Earth radius is roughly 1.4e-9 m; velocity is a pure rotation, so its bound is
# relative to the speed being rotated.
POSITION_TOLERANCE_M = Decimal("1e-6")
VELOCITY_RELATIVE_TOLERANCE = Decimal("1e-12")

RECORDED_AT = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


def error(actual: float, expected: str) -> Decimal:
    return abs(Decimal(repr(actual)) - Decimal(expected))


def test_forward_conversion_matches_every_golden_position(document: dict[str, Any]) -> None:
    for case in document["geodetic_to_ecef"]:
        position = GeodeticPosition(
            latitude=float(case["latitude_deg"]),
            longitude=float(case["longitude_deg"]),
            altitude=float(case["altitude_m"]),
        ).to_ecef()
        for actual, expected in (
            (position.x, case["x_m"]),
            (position.y, case["y_m"]),
            (position.z, case["z_m"]),
        ):
            assert error(actual, expected) <= POSITION_TOLERANCE_M, case["name"]


def test_inverse_conversion_recovers_every_golden_position(document: dict[str, Any]) -> None:
    for case in document["geodetic_to_ecef"]:
        recovered = ECEFPosition(
            x=float(case["x_m"]), y=float(case["y_m"]), z=float(case["z_m"])
        ).to_geodetic()
        assert error(recovered.altitude, case["altitude_m"]) <= POSITION_TOLERANCE_M, case["name"]
        if abs(float(case["latitude_deg"])) == 90.0:
            continue
        assert recovered.latitude == pytest.approx(float(case["latitude_deg"]), abs=1e-9)


def test_the_round_trip_through_both_types_is_bounded(document: dict[str, Any]) -> None:
    """Criterion 6: the inverse carries the whole error budget; the forward is exact."""

    for case in document["geodetic_to_ecef"]:
        original = ECEFPosition(x=float(case["x_m"]), y=float(case["y_m"]), z=float(case["z_m"]))
        restored = original.to_geodetic().to_ecef()
        for actual, expected in (
            (restored.x, original.x),
            (restored.y, original.y),
            (restored.z, original.z),
        ):
            assert abs(actual - expected) <= float(POSITION_TOLERANCE_M), case["name"]


def test_degenerate_positions_follow_the_documented_convention(document: dict[str, Any]) -> None:
    for case in document["degenerate_ecef_to_geodetic"]:
        recovered = ECEFPosition(
            x=float(case["x_m"]), y=float(case["y_m"]), z=float(case["z_m"])
        ).to_geodetic()
        assert recovered.latitude == float(case["expected_latitude_deg"]), case["name"]
        assert recovered.longitude == float(case["expected_longitude_deg"]), case["name"]


def test_velocity_rotation_matches_every_golden_vector(document: dict[str, Any]) -> None:
    for case in document["ned_velocity_to_ecef"]:
        north, east, down = (float(component) for component in case["velocity_ned_mps"])
        rotated = GeodeticPosition(
            latitude=float(case["latitude_deg"]),
            longitude=float(case["longitude_deg"]),
            altitude=0.0,
        ).to_ecef_velocity(Velocity(north=north, east=east, down=down))
        expected = case["velocity_ecef_mps"]
        speed = max((abs(Decimal(component)) for component in expected), default=Decimal(0))
        ceiling = VELOCITY_RELATIVE_TOLERANCE * speed
        for actual, want in zip((rotated.x, rotated.y, rotated.z), expected, strict=True):
            assert error(actual, want) <= ceiling, case["name"]


def test_velocity_round_trip_returns_the_original_ned_vector(document: dict[str, Any]) -> None:
    for case in document["ned_velocity_to_ecef"]:
        north, east, down = (float(component) for component in case["velocity_ned_mps"])
        position = GeodeticPosition(
            latitude=float(case["latitude_deg"]),
            longitude=float(case["longitude_deg"]),
            altitude=0.0,
        )
        restored = position.to_ned_velocity(
            position.to_ecef_velocity(Velocity(north=north, east=east, down=down))
        )
        speed = max(abs(north), abs(east), abs(down))
        ceiling = float(VELOCITY_RELATIVE_TOLERANCE) * speed
        assert abs(restored.north - north) <= ceiling, case["name"]
        assert abs(restored.east - east) <= ceiling, case["name"]
        assert abs(restored.down - down) <= ceiling, case["name"]


def test_location_converts_its_position_to_ecef() -> None:
    location = Location(latitude=34.05, longitude=-118.24, altitude=100.0, recorded_at=RECORDED_AT)
    assert (
        location.to_ecef()
        == GeodeticPosition(latitude=34.05, longitude=-118.24, altitude=100.0).to_ecef()
    )


def test_location_without_altitude_refuses_to_convert() -> None:
    location = Location(latitude=34.05, longitude=-118.24, recorded_at=RECORDED_AT)
    with pytest.raises(ValidationError) as raised:
        location.to_ecef()
    assert raised.value.code == "missing_altitude"
    assert raised.value.operation == "to_ecef"


def test_the_reported_operation_names_the_method_the_caller_used() -> None:
    """A delegated conversion must not report the internal step it went through."""

    location = Location(latitude=34.05, longitude=-118.24, recorded_at=RECORDED_AT)
    with pytest.raises(ValidationError) as raised:
        location.to_geodetic()
    assert raised.value.operation == "to_geodetic"


def test_location_without_altitude_converts_when_the_ellipsoid_is_assumed() -> None:
    location = Location(latitude=34.05, longitude=-118.24, recorded_at=RECORDED_AT)
    assumed = location.to_ecef(assume_zero_ellipsoidal_height=True)
    assert assumed == GeodeticPosition(latitude=34.05, longitude=-118.24, altitude=0.0).to_ecef()


def test_the_altitude_assumption_must_be_passed_by_keyword() -> None:
    location = Location(latitude=0.0, longitude=0.0, recorded_at=RECORDED_AT)
    with pytest.raises(TypeError):
        location.to_ecef(True)  # type: ignore[misc]


def test_location_converts_its_velocity_without_needing_an_altitude() -> None:
    location = Location(
        latitude=34.05,
        longitude=-118.24,
        recorded_at=RECORDED_AT,
        velocity=Velocity(north=10.0, east=-5.0, down=1.0),
    )
    assert location.altitude is None
    rotated = location.to_ecef_velocity()
    assert rotated == GeodeticPosition(
        latitude=34.05, longitude=-118.24, altitude=0.0
    ).to_ecef_velocity(Velocity(north=10.0, east=-5.0, down=1.0))


def test_location_without_velocity_refuses_to_convert_one() -> None:
    location = Location(latitude=0.0, longitude=0.0, recorded_at=RECORDED_AT)
    with pytest.raises(ValidationError) as raised:
        location.to_ecef_velocity()
    assert raised.value.code == "missing_velocity"
    assert raised.value.operation == "to_ecef_velocity"


def test_conversion_leaves_the_location_untouched() -> None:
    """Criterion 10: conversions are read-only over the published geodetic values."""

    location = Location(
        latitude=34.05,
        longitude=-118.24,
        altitude=100.0,
        recorded_at=RECORDED_AT,
        velocity=Velocity(north=10.0, east=-5.0, down=1.0),
    )
    before = location.model_dump()
    location.to_ecef()
    location.to_ecef_velocity()
    assert location.model_dump() == before


def test_the_coordinate_types_are_immutable() -> None:
    position = ECEFPosition(x=1.0, y=2.0, z=3.0)
    with pytest.raises(PydanticValidationError):
        position.x = 9.0  # type: ignore[misc]


def test_the_coordinate_types_reject_non_finite_components() -> None:
    for component in ("nan", "inf", "-inf"):
        with pytest.raises(PydanticValidationError):
            ECEFPosition(x=float(component), y=0.0, z=0.0)
        with pytest.raises(PydanticValidationError):
            ECEFVelocity(x=float(component), y=0.0, z=0.0)
        with pytest.raises(PydanticValidationError):
            GeodeticPosition(latitude=0.0, longitude=0.0, altitude=float(component))


def test_geodetic_position_enforces_the_angular_ranges() -> None:
    for latitude, longitude in ((90.1, 0.0), (-90.1, 0.0), (0.0, 180.1), (0.0, -180.1)):
        with pytest.raises(PydanticValidationError):
            GeodeticPosition(latitude=latitude, longitude=longitude, altitude=0.0)


def test_geodetic_position_requires_an_altitude() -> None:
    with pytest.raises(PydanticValidationError):
        GeodeticPosition(latitude=0.0, longitude=0.0)  # type: ignore[call-arg]


def test_the_two_velocity_frames_cannot_be_interchanged() -> None:
    """Distinct axis names make a frame mix-up fail loudly instead of silently."""

    position = GeodeticPosition(latitude=0.0, longitude=0.0, altitude=0.0)
    with pytest.raises(AttributeError):
        position.to_ned_velocity(Velocity(north=1.0, east=0.0, down=0.0))  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        position.to_ecef_velocity(ECEFVelocity(x=1.0, y=0.0, z=0.0))  # type: ignore[arg-type]


def test_positions_near_the_center_of_the_ellipsoid_are_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        ECEFPosition(x=0.0, y=0.0, z=0.0).to_geodetic()
    assert raised.value.code == "degenerate_ecef"
