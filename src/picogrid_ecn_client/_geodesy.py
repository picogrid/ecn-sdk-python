# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""WGS-84 geodetic and ECEF conversions built on the standard library only."""

from __future__ import annotations

import math

from .exceptions import ValidationError

SEMI_MAJOR_AXIS_M = 6378137.0
INVERSE_FLATTENING = 298.257223563

_FLATTENING = 1.0 / INVERSE_FLATTENING
_ECCENTRICITY_SQUARED = _FLATTENING * (2.0 - _FLATTENING)
_ECCENTRICITY_FOURTH = _ECCENTRICITY_SQUARED * _ECCENTRICITY_SQUARED
_SEMI_MAJOR_AXIS_SQUARED = SEMI_MAJOR_AXIS_M * SEMI_MAJOR_AXIS_M


def geodetic_to_ecef(
    latitude: float, longitude: float, altitude: float
) -> tuple[float, float, float]:
    """Convert EPSG:4979 degrees and ellipsoidal metres to EPSG:4978 metres.

    At latitude 90 or -90, both equatorial components are emitted as exact zeros.
    """

    latitude_rad = math.radians(latitude)
    sin_latitude = math.sin(latitude_rad)
    prime_vertical = SEMI_MAJOR_AXIS_M / math.sqrt(
        1.0 - _ECCENTRICITY_SQUARED * sin_latitude * sin_latitude
    )
    if abs(latitude) == 90.0:
        # cos(pi / 2) is not exactly zero in binary floating point; its residual
        # would reach the inverse, which would report the input longitude instead
        # of zero.
        x = 0.0
        y = 0.0
    else:
        longitude_rad = math.radians(longitude)
        equatorial = (prime_vertical + altitude) * math.cos(latitude_rad)
        x = equatorial * math.cos(longitude_rad)
        y = equatorial * math.sin(longitude_rad)
    return (
        x,
        y,
        (prime_vertical * (1.0 - _ECCENTRICITY_SQUARED) + altitude) * sin_latitude,
    )


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert EPSG:4978 metres to EPSG:4979 degrees and ellipsoidal metres.

    Applies the closed-form solution of Vermeille (2011). Longitude is undefined
    on the polar axis and is reported as zero there. The forward conversion emits
    exact equatorial zeros at the poles, so a pole survives a round trip as
    longitude zero.
    """

    p = (x * x + y * y) / _SEMI_MAJOR_AXIS_SQUARED
    q = (1.0 - _ECCENTRICITY_SQUARED) * z * z / _SEMI_MAJOR_AXIS_SQUARED
    r = (p + q - _ECCENTRICITY_FOURTH) / 6.0
    if r <= 0.0 or 8.0 * r**3 + _ECCENTRICITY_FOURTH * p * q <= 0.0:
        raise ValidationError(
            "position lies inside the region around the center of the ellipsoid "
            "where geodetic coordinates are not defined",
            code="degenerate_ecef",
            operation="to_geodetic",
        )

    s = _ECCENTRICITY_FOURTH * p * q / (4.0 * r**3)
    t = math.cbrt(1.0 + s + math.sqrt(s * (2.0 + s)))
    u = r * (1.0 + t + 1.0 / t)
    v = math.sqrt(u * u + _ECCENTRICITY_FOURTH * q)
    w = _ECCENTRICITY_SQUARED * (u + v - q) / (2.0 * v)
    k = math.sqrt(u + v + w * w) - w

    equatorial = math.hypot(x, y)
    d = k * equatorial / (k + _ECCENTRICITY_SQUARED)
    meridional = math.hypot(d, z)
    altitude = (k + _ECCENTRICITY_SQUARED - 1.0) / k * meridional
    if equatorial == 0.0:
        return math.copysign(90.0, z), 0.0, altitude
    return (
        math.degrees(2.0 * math.atan2(z, d + meridional)),
        math.degrees(math.atan2(y, x)),
        altitude,
    )


def ned_to_ecef_velocity(
    latitude: float, longitude: float, north: float, east: float, down: float
) -> tuple[float, float, float]:
    """Rotate a north/east/down velocity into EPSG:4978 axes."""

    latitude_rad = math.radians(latitude)
    longitude_rad = math.radians(longitude)
    sin_latitude = math.sin(latitude_rad)
    cos_latitude = math.cos(latitude_rad)
    sin_longitude = math.sin(longitude_rad)
    cos_longitude = math.cos(longitude_rad)
    return (
        -sin_latitude * cos_longitude * north
        - sin_longitude * east
        - cos_latitude * cos_longitude * down,
        -sin_latitude * sin_longitude * north
        + cos_longitude * east
        - cos_latitude * sin_longitude * down,
        cos_latitude * north - sin_latitude * down,
    )


def ecef_to_ned_velocity(
    latitude: float, longitude: float, x: float, y: float, z: float
) -> tuple[float, float, float]:
    """Rotate a velocity on EPSG:4978 axes into north/east/down."""

    latitude_rad = math.radians(latitude)
    longitude_rad = math.radians(longitude)
    sin_latitude = math.sin(latitude_rad)
    cos_latitude = math.cos(latitude_rad)
    sin_longitude = math.sin(longitude_rad)
    cos_longitude = math.cos(longitude_rad)
    return (
        -sin_latitude * cos_longitude * x - sin_latitude * sin_longitude * y + cos_latitude * z,
        -sin_longitude * x + cos_longitude * y,
        -cos_latitude * cos_longitude * x - cos_latitude * sin_longitude * y - sin_latitude * z,
    )
