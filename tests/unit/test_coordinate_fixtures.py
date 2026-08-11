# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parents[1] / "fixtures" / "wgs84_ecef.json"

SEMI_MAJOR_AXIS_M = Decimal("6378137.0")
INVERSE_FLATTENING = Decimal("298.257223563")
SEMI_MINOR_AXIS_M = SEMI_MAJOR_AXIS_M * (1 - 1 / INVERSE_FLATTENING)

SUPPORTED_ALTITUDE_RANGE_M = (Decimal("-11000"), Decimal("100000"))

# The independent float64 cross-check cannot agree more closely than one unit in
# the last place at Earth radius, which is roughly 1.4e-9 m.
CROSS_CHECK_CEILING_M = Decimal("1e-8")

# Defects computed at 50 decimal digits, so anything above this indicates the
# rotation was not generated at the documented precision.
ARBITRARY_PRECISION_CEILING = Decimal("1e-40")

# Values are stored to 30 significant digits, so identities that hold exactly in
# exact arithmetic must close far tighter than this.
IDENTITY_CEILING = Decimal("1e-20")

# cos(90 degrees) is not exactly zero at finite precision; the equatorial
# components at a pole are around 1e-44.
POLE_EQUATORIAL_CEILING_M = Decimal("1e-30")

REQUIRED_CONTROL_POINTS = frozenset(
    {
        "equator-prime-meridian",
        "equator-prime-meridian-negative-zero-altitude",
        "north-pole",
        "south-pole",
        "near-north-pole",
        "near-south-pole",
        "antimeridian-positive",
        "antimeridian-negative",
        "just-inside-antimeridian-positive",
        "just-inside-antimeridian-negative",
        "dead-sea-shallow-negative-altitude",
        "challenger-deep-negative-altitude",
        "karman-line-high-altitude",
        "min-supported-altitude",
        "max-supported-altitude",
        "asymmetric-swap-detector",
        "transposable-swap-detector",
        "transposable-swap-detector-transposed",
    }
)

REQUIRED_DEGENERATE_INPUTS = frozenset(
    {
        "exact-north-pole-undefined-longitude",
        "exact-south-pole-undefined-longitude",
        "exact-antimeridian-positive-zero",
        "exact-antimeridian-negative-zero",
    }
)

GENERAL_VELOCITY_NED = (Decimal("15.5"), Decimal("-8.3"), Decimal("2.1"))


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def positions(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["name"]: case for case in document["geodetic_to_ecef"]}


def _vector(case: dict[str, Any]) -> list[Decimal]:
    return [Decimal(value) for value in case["velocity_ecef_mps"]]


def _rotation_columns(document: dict[str, Any]) -> dict[tuple[str, str], list[list[Decimal]]]:
    """Group the cardinal velocity cases into the columns of each rotation."""
    columns: dict[tuple[str, str], dict[str, list[Decimal]]] = {}
    for case in document["ned_velocity_to_ecef"]:
        axis = case["name"].rsplit("-lat", 1)[0]
        if axis not in {"unit-north", "unit-east", "unit-down"}:
            continue
        key = (case["latitude_deg"], case["longitude_deg"])
        columns.setdefault(key, {})[axis] = _vector(case)
    return {
        key: [axes["unit-north"], axes["unit-east"], axes["unit-down"]]
        for key, axes in columns.items()
    }


def _determinant(columns: list[list[Decimal]]) -> Decimal:
    north, east, down = columns
    cross = (
        east[1] * down[2] - east[2] * down[1],
        east[2] * down[0] - east[0] * down[2],
        east[0] * down[1] - east[1] * down[0],
    )
    return sum((north[i] * cross[i] for i in range(3)), Decimal(0))


def test_provenance_records_the_oracle_and_ellipsoid(document: dict[str, Any]) -> None:
    provenance = document["provenance"]
    assert provenance["oracle"] == "mpmath"
    assert provenance["working_precision_decimal_digits"] >= 50
    assert "pyerfa" in provenance["independent_cross_check"]
    assert Decimal(provenance["semi_major_axis_m"]) == SEMI_MAJOR_AXIS_M
    assert Decimal(provenance["inverse_flattening"]) == INVERSE_FLATTENING


def test_oracle_was_validated_against_a_published_reference_suite(
    document: dict[str, Any],
) -> None:
    """Records the outcome only; the third-party constants are not redistributed."""
    validation = document["provenance"]["published_vector_validation"]
    assert "SOFA" in validation["suite"]
    assert Decimal(validation["oracle_worst_abs_delta_m"]) < Decimal(
        validation["published_tolerance_m"]
    )


def test_named_control_points_are_present(positions: dict[str, dict[str, Any]]) -> None:
    assert set(positions) >= REQUIRED_CONTROL_POINTS


def test_every_position_is_finite_decimal_within_the_supported_domain(
    document: dict[str, Any],
) -> None:
    for case in document["geodetic_to_ecef"]:
        assert -90 <= Decimal(case["latitude_deg"]) <= 90, case["name"]
        assert -180 <= Decimal(case["longitude_deg"]) <= 180, case["name"]
        altitude = Decimal(case["altitude_m"])
        assert altitude.is_finite(), case["name"]
        assert SUPPORTED_ALTITUDE_RANGE_M[0] <= altitude <= SUPPORTED_ALTITUDE_RANGE_M[1], case[
            "name"
        ]
        for axis in ("x_m", "y_m", "z_m"):
            assert Decimal(case[axis]).is_finite(), f"{case['name']} {axis}"


def test_independent_cross_check_agrees_to_the_float64_floor(document: dict[str, Any]) -> None:
    worst = max(Decimal(case["erfa_max_abs_delta_m"]) for case in document["geodetic_to_ecef"])
    assert worst < CROSS_CHECK_CEILING_M
    assert Decimal(document["provenance"]["cross_check_worst_abs_delta_m"]) == worst


def test_declared_altitude_domain_is_exercised_at_both_bounds(
    positions: dict[str, dict[str, Any]],
) -> None:
    minimum, maximum = SUPPORTED_ALTITUDE_RANGE_M
    assert Decimal(positions["min-supported-altitude"]["altitude_m"]) == minimum
    assert Decimal(positions["max-supported-altitude"]["altitude_m"]) == maximum


def test_altitude_displaces_radially_at_the_equator(positions: dict[str, dict[str, Any]]) -> None:
    """At the equator and prime meridian the X axis is the ellipsoid normal."""
    for altitude in ("-11000", "0", "1000", "100000"):
        case = positions[f"grid-lat0-lon0-alt{altitude}"]
        expected = SEMI_MAJOR_AXIS_M + Decimal(altitude)
        assert abs(Decimal(case["x_m"]) - expected) < IDENTITY_CEILING, altitude
        assert abs(Decimal(case["y_m"])) < IDENTITY_CEILING, altitude
        assert abs(Decimal(case["z_m"])) < IDENTITY_CEILING, altitude


def test_poles_sit_on_the_semi_minor_axis(positions: dict[str, dict[str, Any]]) -> None:
    for name, sign in (("north-pole", 1), ("south-pole", -1)):
        case = positions[name]
        assert abs(Decimal(case["z_m"]) - sign * SEMI_MINOR_AXIS_M) < IDENTITY_CEILING, name
        assert abs(Decimal(case["x_m"])) < POLE_EQUATORIAL_CEILING_M, name
        assert abs(Decimal(case["y_m"])) < POLE_EQUATORIAL_CEILING_M, name


def test_longitude_does_not_move_a_pole(positions: dict[str, dict[str, Any]]) -> None:
    """Longitude is degenerate at a pole, so it must not change the position."""
    origin = positions["north-pole"]
    rotated = positions["north-pole-nonzero-longitude"]
    assert Decimal(rotated["longitude_deg"]) != Decimal(origin["longitude_deg"])
    assert Decimal(rotated["z_m"]) == Decimal(origin["z_m"])
    for axis in ("x_m", "y_m"):
        assert abs(Decimal(rotated[axis])) < POLE_EQUATORIAL_CEILING_M, axis


def test_transposing_latitude_and_longitude_moves_the_position(
    positions: dict[str, dict[str, Any]],
) -> None:
    upright = positions["transposable-swap-detector"]
    swapped = positions["transposable-swap-detector-transposed"]
    assert Decimal(upright["latitude_deg"]) == Decimal(swapped["longitude_deg"])
    assert Decimal(upright["longitude_deg"]) == Decimal(swapped["latitude_deg"])
    separation = sum(
        (Decimal(upright[axis]) - Decimal(swapped[axis])) ** 2 for axis in ("x_m", "y_m", "z_m")
    ).sqrt()
    assert separation > Decimal("1e6")


def test_swap_detector_transposes_into_an_invalid_latitude(
    positions: dict[str, dict[str, Any]],
) -> None:
    """The second detector fails range validation rather than value comparison."""
    case = positions["asymmetric-swap-detector"]
    assert abs(Decimal(case["longitude_deg"])) > 90


def test_velocity_rotation_is_orthonormal_and_norm_preserving(document: dict[str, Any]) -> None:
    for case in document["ned_velocity_to_ecef"]:
        assert Decimal(case["rotation_orthonormality_defect"]) < ARBITRARY_PRECISION_CEILING, case[
            "name"
        ]
        assert Decimal(case["norm_preservation_defect"]) < ARBITRARY_PRECISION_CEILING, case["name"]


def test_every_rotation_is_proper_not_a_reflection(document: dict[str, Any]) -> None:
    """A determinant of -1 is a reflection, which norm preservation would accept."""
    rotations = _rotation_columns(document)
    assert len(rotations) >= 15
    for key, columns in rotations.items():
        assert abs(_determinant(columns) - 1) < IDENTITY_CEILING, key


def test_every_rotation_has_orthonormal_columns(document: dict[str, Any]) -> None:
    for key, columns in _rotation_columns(document).items():
        for i in range(3):
            for j in range(3):
                dot = sum((columns[i][k] * columns[j][k] for k in range(3)), Decimal(0))
                expected = Decimal(1) if i == j else Decimal(0)
                assert abs(dot - expected) < IDENTITY_CEILING, (key, i, j)


def test_general_velocity_is_the_cardinal_combination(document: dict[str, Any]) -> None:
    """Catches an axis transposition or sign flip at every sampled position."""
    rotations = _rotation_columns(document)
    checked = 0
    for case in document["ned_velocity_to_ecef"]:
        if not case["name"].startswith("general-"):
            continue
        columns = rotations[(case["latitude_deg"], case["longitude_deg"])]
        for axis in range(3):
            expected = sum(
                (
                    GENERAL_VELOCITY_NED[component] * columns[component][axis]
                    for component in range(3)
                ),
                Decimal(0),
            )
            assert abs(_vector(case)[axis] - expected) < IDENTITY_CEILING, (case["name"], axis)
        checked += 1
    assert checked >= 15


def test_cardinal_velocity_directions_at_the_origin(document: dict[str, Any]) -> None:
    """North, east, and down at the equator and prime meridian are the ECEF axes."""
    columns = _rotation_columns(document)[("0", "0")]
    assert columns[0] == [Decimal(0), Decimal(0), Decimal(1)]
    assert columns[1] == [Decimal(0), Decimal(1), Decimal(0)]
    assert columns[2] == [Decimal(-1), Decimal(0), Decimal(0)]


def test_east_is_always_horizontal_in_ecef(document: dict[str, Any]) -> None:
    """East has no ECEF Z component at any position, whatever the latitude.

    Orthonormality and a determinant of one are preserved by permuting the
    axes, so this absolute property is what distinguishes the true rotation
    from a rotated copy of it.
    """
    for key, columns in _rotation_columns(document).items():
        assert abs(columns[1][2]) < IDENTITY_CEILING, key


def test_cardinal_velocity_directions_on_the_ninety_degree_meridian(
    document: dict[str, Any],
) -> None:
    """At the equator and ninety degrees east the frame is a known permutation."""
    columns = _rotation_columns(document)[("0", "90")]
    expected = (
        [Decimal(0), Decimal(0), Decimal(1)],
        [Decimal(-1), Decimal(0), Decimal(0)],
        [Decimal(0), Decimal(-1), Decimal(0)],
    )
    # cos(90 degrees) is not exactly zero at finite precision, so the
    # off-axis components are around 1e-51 rather than absent.
    for axis, (actual, target) in enumerate(zip(columns, expected, strict=True)):
        for component, (got, want) in enumerate(zip(actual, target, strict=True)):
            assert abs(got - want) < IDENTITY_CEILING, (axis, component)


def test_degenerate_inputs_pin_the_undecidable_conventions(document: dict[str, Any]) -> None:
    """The forward transform cannot emit these bit patterns, so they are stated."""
    cases = {case["name"]: case for case in document["degenerate_ecef_to_geodetic"]}
    assert set(cases) >= REQUIRED_DEGENERATE_INPUTS

    for name in ("exact-north-pole-undefined-longitude", "exact-south-pole-undefined-longitude"):
        case = cases[name]
        assert Decimal(case["x_m"]) == 0, name
        assert Decimal(case["y_m"]) == 0, name
        assert Decimal(case["expected_longitude_deg"]) == 0, name
        assert abs(Decimal(case["expected_latitude_deg"])) == 90, name


def test_signed_zero_selects_the_antimeridian_sign(document: dict[str, Any]) -> None:
    cases = {case["name"]: case for case in document["degenerate_ecef_to_geodetic"]}
    positive = cases["exact-antimeridian-positive-zero"]
    negative = cases["exact-antimeridian-negative-zero"]

    # The two rows differ only in the sign of a zero, which Decimal compares equal.
    assert Decimal(positive["y_m"]) == Decimal(negative["y_m"]) == 0
    assert positive["y_m"] == "0.0"
    assert negative["y_m"] == "-0.0"
    assert Decimal(positive["expected_longitude_deg"]) == 180
    assert Decimal(negative["expected_longitude_deg"]) == -180
