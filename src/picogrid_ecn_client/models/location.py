# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Public geodetic, Cartesian, and motion models."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator

from .._geodesy import (
    ecef_to_geodetic,
    ecef_to_ned_velocity,
    geodetic_to_ecef,
    ned_to_ecef_velocity,
)
from ..exceptions import ValidationError
from ._base import AwareDatetime, IntegrationName, PublicModel

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
"""A finite floating-point value that rejects positive and negative infinity and NaN."""


def _strict_number(value: object) -> object:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric fields require an integer or float")
    return value


class Velocity(PublicModel):
    """Represent north/east/down velocity in metres per second.

    Each component is finite and follows the north/east/down coordinate convention.
    """

    north: FiniteFloat = Field(description="Finite northward velocity in metres per second.")
    east: FiniteFloat = Field(description="Finite eastward velocity in metres per second.")
    down: FiniteFloat = Field(description="Finite downward velocity in metres per second.")

    _validate_numbers = field_validator("north", "east", "down", mode="before")(_strict_number)


class AngularVelocity(PublicModel):
    """Represent roll, pitch, and yaw rates in radians per second.

    The pinned wire fields are named as NED angular components; interpreting them as
    roll, pitch, and yaw remains deployed-unverified.
    """

    roll: FiniteFloat = Field(
        description=(
            "Finite roll rate in radians per second; this axis interpretation is "
            "deployed-unverified."
        )
    )
    pitch: FiniteFloat = Field(
        description=(
            "Finite pitch rate in radians per second; this axis interpretation is "
            "deployed-unverified."
        )
    )
    yaw: FiniteFloat = Field(
        description=(
            "Finite yaw rate in radians per second; this axis interpretation is "
            "deployed-unverified."
        )
    )

    _validate_numbers = field_validator("roll", "pitch", "yaw", mode="before")(_strict_number)


class ECEFVelocity(PublicModel):
    """Velocity on EPSG:4978 axes in metres per second."""

    x: FiniteFloat = Field(
        description="Finite velocity along the EPSG:4978 X axis in metres per second."
    )
    y: FiniteFloat = Field(
        description="Finite velocity along the EPSG:4978 Y axis in metres per second."
    )
    z: FiniteFloat = Field(
        description="Finite velocity along the EPSG:4978 Z axis in metres per second."
    )

    _validate_numbers = field_validator("x", "y", "z", mode="before")(_strict_number)


class ECEFPosition(PublicModel):
    """An EPSG:4978 Cartesian position in metres.

    The axes are earth-centered and earth-fixed: X towards the intersection of the
    equator and the prime meridian, Z towards the north pole, and Y completing the
    right-handed set. The ECN carries no ECEF value; this type is local to the client.
    """

    x: FiniteFloat = Field(
        description=(
            "Finite EPSG:4978 X coordinate in metres, towards the intersection of the "
            "equator and the prime meridian."
        )
    )
    y: FiniteFloat = Field(
        description="Finite EPSG:4978 Y coordinate in metres, completing the right-handed set."
    )
    z: FiniteFloat = Field(
        description="Finite EPSG:4978 Z coordinate in metres, towards the north pole."
    )

    _validate_numbers = field_validator("x", "y", "z", mode="before")(_strict_number)

    def to_geodetic(self) -> GeodeticPosition:
        """Convert to latitude, longitude, and height above the WGS-84 ellipsoid.

        Returns:
            The equivalent WGS-84 coordinate triple.

        Raises:
            ValidationError: If the position is near the center of the ellipsoid,
                where geodetic coordinates are not defined, with code
                `degenerate_ecef`.
        """

        latitude, longitude, altitude = ecef_to_geodetic(self.x, self.y, self.z)
        return GeodeticPosition(latitude=latitude, longitude=longitude, altitude=altitude)


class GeodeticPosition(PublicModel):
    """A WGS-84 position: degrees of latitude and longitude, metres of height.

    Compliant with EPSG:4979; altitude is height above the WGS-84 ellipsoid. This is
    a coordinate triple rather than a `Location`, so it carries no timestamp, source,
    bearing, accuracy, or motion.
    """

    latitude: Annotated[
        float,
        Field(
            ge=-90,
            le=90,
            allow_inf_nan=False,
            description="Finite WGS-84 latitude in decimal degrees, from -90 through 90.",
        ),
    ]
    longitude: Annotated[
        float,
        Field(
            ge=-180,
            le=180,
            allow_inf_nan=False,
            description="Finite WGS-84 longitude in decimal degrees, from -180 through 180.",
        ),
    ]
    altitude: FiniteFloat = Field(description="Finite height above the WGS-84 ellipsoid in metres.")

    _validate_numbers = field_validator(
        "latitude",
        "longitude",
        "altitude",
        mode="before",
    )(_strict_number)

    def to_ecef(self) -> ECEFPosition:
        """Convert to EPSG:4978 Cartesian metres."""

        x, y, z = geodetic_to_ecef(self.latitude, self.longitude, self.altitude)
        return ECEFPosition(x=x, y=y, z=z)

    def to_ecef_velocity(self, velocity: Velocity) -> ECEFVelocity:
        """Rotate a north/east/down velocity at this position onto EPSG:4978 axes.

        Args:
            velocity: North/east/down velocity in metres per second observed at
                this position.

        Returns:
            The same velocity expressed on EPSG:4978 axes.
        """

        x, y, z = ned_to_ecef_velocity(
            self.latitude, self.longitude, velocity.north, velocity.east, velocity.down
        )
        return ECEFVelocity(x=x, y=y, z=z)

    def to_ned_velocity(self, velocity: ECEFVelocity) -> Velocity:
        """Rotate a velocity on EPSG:4978 axes onto north/east/down at this position.

        Args:
            velocity: Velocity on EPSG:4978 axes in metres per second.

        Returns:
            The same velocity expressed as north/east/down at this position.
        """

        north, east, down = ecef_to_ned_velocity(
            self.latitude, self.longitude, velocity.x, velocity.y, velocity.z
        )
        return Velocity(north=north, east=east, down=down)


class Location(PublicModel):
    """Represent timestamped WGS-84 Position Location Information.

    Latitude, longitude, and the timezone-aware recorded time are required. Motion,
    accuracy, source, altitude, bearing, and confidence are optional.
    """

    latitude: Annotated[
        float,
        Field(
            ge=-90,
            le=90,
            allow_inf_nan=False,
            description="Finite WGS-84 latitude in decimal degrees, from -90 through 90.",
        ),
    ]
    longitude: Annotated[
        float,
        Field(
            ge=-180,
            le=180,
            allow_inf_nan=False,
            description="Finite WGS-84 longitude in decimal degrees, from -180 through 180.",
        ),
    ]
    recorded_at: AwareDatetime = Field(
        description="Timezone-aware time when the location was recorded."
    )
    altitude: FiniteFloat | None = Field(
        default=None,
        description="Optional finite altitude in metres.",
    )
    bearing: (
        Annotated[
            float,
            Field(
                ge=0,
                lt=360,
                allow_inf_nan=False,
                description="Optional finite bearing in degrees, from 0 inclusive to 360 exclusive.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional finite bearing in degrees, from 0 inclusive to 360 exclusive.",
    )
    accuracy: (
        Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                description="Optional non-negative finite horizontal accuracy in metres.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional non-negative finite horizontal accuracy in metres.",
    )
    source: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=128,
                description="Optional location source label, from 1 through 128 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional location source label, from 1 through 128 characters.",
    )
    velocity: Velocity | None = Field(
        default=None,
        description="Optional north/east/down velocity in metres per second.",
    )
    angular_velocity: AngularVelocity | None = Field(
        default=None,
        description=(
            "Optional roll/pitch/yaw rates in radians per second; axis interpretation "
            "is deployed-unverified."
        ),
    )
    confidence: (
        Annotated[
            float,
            Field(
                ge=0,
                le=1,
                allow_inf_nan=False,
                description="Optional finite confidence fraction, from 0 through 1.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional finite confidence fraction, from 0 through 1.",
    )

    _validate_numbers = field_validator(
        "latitude",
        "longitude",
        "altitude",
        "bearing",
        "accuracy",
        "confidence",
        mode="before",
    )(_strict_number)

    def _geodetic(
        self, *, assume_zero_ellipsoidal_height: bool, operation: str
    ) -> GeodeticPosition:
        if self.altitude is None and not assume_zero_ellipsoidal_height:
            raise ValidationError(
                "location has no altitude; supply one or pass "
                "assume_zero_ellipsoidal_height to place the position on the ellipsoid",
                code="missing_altitude",
                operation=operation,
            )
        return GeodeticPosition(
            latitude=self.latitude,
            longitude=self.longitude,
            altitude=self.altitude if self.altitude is not None else 0.0,
        )

    def to_geodetic(self, *, assume_zero_ellipsoidal_height: bool = False) -> GeodeticPosition:
        """Return the position of this location as a geodetic coordinate triple.

        Altitude is optional on a location but required for a geodetic position. A
        location without one raises `ValidationError` with code `missing_altitude`
        unless `assume_zero_ellipsoidal_height` places it on the ellipsoid.

        Args:
            assume_zero_ellipsoidal_height: Treat a missing altitude as zero height
                above the WGS-84 ellipsoid instead of raising.

        Returns:
            This location's position as a geodetic coordinate triple.

        Raises:
            ValidationError: If altitude is missing and the caller did not assume
                zero ellipsoidal height.
        """

        return self._geodetic(
            assume_zero_ellipsoidal_height=assume_zero_ellipsoidal_height,
            operation="to_geodetic",
        )

    def to_ecef(self, *, assume_zero_ellipsoidal_height: bool = False) -> ECEFPosition:
        """Convert the position of this location to EPSG:4978 Cartesian metres.

        Altitude is read as height above the WGS-84 ellipsoid. A location without one
        raises `ValidationError` with code `missing_altitude` unless
        `assume_zero_ellipsoidal_height` places it on the ellipsoid.

        Args:
            assume_zero_ellipsoidal_height: Treat a missing altitude as zero height
                above the WGS-84 ellipsoid instead of raising.

        Returns:
            This location's position as an EPSG:4978 Cartesian position.

        Raises:
            ValidationError: If altitude is missing and the caller did not assume
                zero ellipsoidal height.
        """

        return self._geodetic(
            assume_zero_ellipsoidal_height=assume_zero_ellipsoidal_height,
            operation="to_ecef",
        ).to_ecef()

    def to_ecef_velocity(self) -> ECEFVelocity:
        """Rotate the velocity of this location onto EPSG:4978 axes.

        The rotation depends on latitude and longitude alone, so altitude is not
        required.

        Returns:
            This location's velocity expressed on EPSG:4978 axes.

        Raises:
            ValidationError: If the location carries no velocity, with code
                `missing_velocity`.
        """

        if self.velocity is None:
            raise ValidationError(
                "location has no velocity to convert",
                code="missing_velocity",
                operation="to_ecef_velocity",
            )
        x, y, z = ned_to_ecef_velocity(
            self.latitude,
            self.longitude,
            self.velocity.north,
            self.velocity.east,
            self.velocity.down,
        )
        return ECEFVelocity(x=x, y=y, z=z)


class LocationEvent(PublicModel):
    """Pair one entity's location with its integration and event time.

    The event carries the canonical entity UUID and a validated integration name.
    """

    entity_id: UUID = Field(description="Canonical UUID of the located entity.")
    integration: IntegrationName = Field(
        description="Validated integration name that reports the location."
    )
    timestamp: AwareDatetime = Field(
        description="Timezone-aware time associated with the location event."
    )
    location: Location = Field(description="Position Location Information carried by the event.")
