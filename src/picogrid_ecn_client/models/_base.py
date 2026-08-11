# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Shared validation policy for public models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field


def _reject_reserved_integration(value: str) -> str:
    if value.casefold() == "geolocation":
        raise ValueError("integration name is reserved")
    return value


IntegrationName = Annotated[
    str,
    Field(
        min_length=2,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]$",
    ),
    AfterValidator(_reject_reserved_integration),
]
"""A validated integration name with reserved geolocation naming excluded."""


class PublicModel(BaseModel):
    """Strict, immutable base for stable request and response models."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        validate_default=True,
    )


def utc_datetime(value: datetime) -> datetime:
    """Reject naive timestamps and normalize aware values to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _strict_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime instance")
    return value


AwareDatetime = Annotated[
    datetime,
    BeforeValidator(_strict_datetime),
    AfterValidator(utc_datetime),
]
"""A timezone-aware datetime normalized to Coordinated Universal Time (UTC)."""
