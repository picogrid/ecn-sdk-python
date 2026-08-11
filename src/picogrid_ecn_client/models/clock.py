# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Typed ECN-relative clock diagnostic models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .._network import normalize_host
from ._base import PublicModel, utc_datetime


class ClockLeapState(StrEnum):
    """NTP leap indicator reported by the selected server response."""

    NO_WARNING = "no_warning"
    LAST_MINUTE_HAS_61_SECONDS = "last_minute_has_61_seconds"
    LAST_MINUTE_HAS_59_SECONDS = "last_minute_has_59_seconds"
    UNSYNCHRONIZED = "unsynchronized"


class ClockEndpoint(PublicModel):
    """Configured ECN-relative NTP endpoint used for one measurement."""

    host: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description=(
                "NTP host that produced this measurement, from 1 through 1024 "
                "characters. The report always names a resolved host; the fallback "
                "to the configured ECN host is applied by `ECNConfig` when "
                "`ntp_host` is unset, not by this model."
            ),
        ),
    ]
    port: Annotated[
        int,
        Field(
            ge=1,
            le=65_535,
            description="UDP port of the measured NTP endpoint, from 1 through 65,535.",
        ),
    ] = 123

    @field_validator("host", mode="before")
    @classmethod
    def validate_host(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_host(value)


class ClockReport(PublicModel):
    """Summary of one bounded ECN-relative clock measurement.

    ``offset_seconds`` is always ECN time minus local time. The selected sample is
    the valid response with the lowest round-trip delay. This measures offset at one
    point in time; it is not a drift estimate. ``round_trip_delay_seconds`` uses the
    bracketed local monotonic interval minus the server receive-to-transmit interval.
    ``local_capture_uncertainty_seconds`` is a conservative bound on offset error
    attributable to the paired local clock reads and tolerated within-sample
    wall/monotonic divergence, not a bound on network asymmetry or server accuracy.
    """

    endpoint: ClockEndpoint = Field(description="NTP endpoint that produced this measurement.")
    offset_seconds: Annotated[
        float,
        Field(
            allow_inf_nan=False,
            description=(
                "Finite selected offset in seconds, ECN time minus local time; positive "
                "means ECN time is ahead."
            ),
        ),
    ]
    round_trip_delay_seconds: Annotated[
        float,
        Field(
            ge=0,
            allow_inf_nan=False,
            description=(
                "Finite non-negative round-trip delay in seconds for the selected sample, "
                "measured as the bracketed local monotonic interval minus the server "
                "receive-to-transmit interval."
            ),
        ),
    ]
    local_capture_uncertainty_seconds: Annotated[
        float,
        Field(
            ge=0,
            allow_inf_nan=False,
            description=(
                "Finite non-negative bound in seconds on offset error attributable to the "
                "paired local clock reads and tolerated wall/monotonic divergence. It does "
                "not bound network asymmetry or server accuracy."
            ),
        ),
    ]
    jitter_seconds: Annotated[
        float,
        Field(
            ge=0,
            allow_inf_nan=False,
            description=("Finite non-negative variation in seconds across the completed samples."),
        ),
    ]
    spread_seconds: Annotated[
        float,
        Field(
            ge=0,
            allow_inf_nan=False,
            description=(
                "Finite non-negative difference in seconds between the largest and "
                "smallest sampled offsets."
            ),
        ),
    ]
    samples_requested: Annotated[
        int,
        Field(
            ge=1,
            le=10,
            description="Number of samples the caller requested, from 1 through 10.",
        ),
    ]
    samples_completed: Annotated[
        int,
        Field(
            ge=1,
            le=10,
            description=(
                "Number of valid samples used, from 1 through 10; never more than "
                "``samples_requested``."
            ),
        ),
    ]
    server_version: Literal[4] = Field(
        description="NTP version reported by the server; only version 4 is accepted."
    )
    server_stratum: Annotated[
        int,
        Field(
            ge=1,
            le=15,
            description="Server stratum reported in the selected response, from 1 through 15.",
        ),
    ]
    leap_state: ClockLeapState = Field(
        description="Leap indicator from the selected response; unsynchronized is rejected."
    )
    measured_at: datetime = Field(
        description="Timezone-aware UTC time when the measurement completed locally."
    )
    max_offset_seconds: (
        Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                description=(
                    "Finite non-negative tolerance in seconds supplied by a tolerance check; "
                    "present only together with ``within_tolerance``."
                ),
            ),
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Finite non-negative tolerance in seconds supplied by a tolerance check; "
            "present only together with ``within_tolerance``."
        ),
    )
    within_tolerance: bool | None = Field(
        default=None,
        description=(
            "Whether the absolute offset plus local capture uncertainty stayed within "
            "``max_offset_seconds``; present only together with it."
        ),
    )

    _normalize_measured_at = field_validator("measured_at")(utc_datetime)

    @model_validator(mode="after")
    def validate_tolerance_pair(self) -> ClockReport:
        if (self.max_offset_seconds is None) != (self.within_tolerance is None):
            raise ValueError("clock tolerance and result must be present together")
        if self.samples_completed > self.samples_requested:
            raise ValueError("completed clock samples cannot exceed requested samples")
        if self.leap_state is ClockLeapState.UNSYNCHRONIZED:
            raise ValueError("unsynchronized NTP responses cannot produce a report")
        if self.max_offset_seconds is not None:
            expected = (
                abs(self.offset_seconds) + self.local_capture_uncertainty_seconds
                <= self.max_offset_seconds
            )
            if self.within_tolerance is not expected:
                raise ValueError("clock tolerance result does not match offset and uncertainty")
        return self


__all__ = ["ClockEndpoint", "ClockLeapState", "ClockReport"]
