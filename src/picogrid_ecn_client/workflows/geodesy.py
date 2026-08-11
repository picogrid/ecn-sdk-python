# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable local geodesy workflows."""

from __future__ import annotations

from pydantic import Field

from picogrid_ecn_client.models import ECEFPosition, ECEFVelocity, Location
from picogrid_ecn_client.models._base import PublicModel


class ConvertLocationToECEFResult(PublicModel):
    """ECEF position and optional rotated velocity for a geodetic location.

    Attributes:
        position: Earth-centered, earth-fixed Cartesian position.
        velocity: Rotated ECEF velocity, or ``None`` when the location has no velocity.
    """

    position: ECEFPosition = Field(description="Earth-centered, earth-fixed Cartesian position.")
    velocity: ECEFVelocity | None = Field(
        description="Rotated ECEF velocity, or None when the location has no velocity."
    )


def convert_location_to_ecef(location: Location) -> ConvertLocationToECEFResult:
    """Convert a public WGS-84 location and optional velocity to ECEF coordinates.

    Args:
        location: Geodetic location to convert locally.

    Returns:
        The ECEF position and, when present on ``location``, its rotated velocity.
    """

    return ConvertLocationToECEFResult(
        position=location.to_ecef(),
        velocity=location.to_ecef_velocity() if location.velocity is not None else None,
    )


__all__ = ["ConvertLocationToECEFResult", "convert_location_to_ecef"]
