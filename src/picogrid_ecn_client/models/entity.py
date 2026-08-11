# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Public entity and entity-event models."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field, JsonValue

from ._base import AwareDatetime, IntegrationName, PublicModel
from .common import Affiliation, EntityCategory, EntityStatus
from .location import Location


class DisplayMetadata(PublicModel):
    """Provide optional reader-facing display hints for an entity.

    These values describe presentation only and do not change entity identity.
    """

    label: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=256,
                description="Optional display label, from 1 through 256 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional display label, from 1 through 256 characters.",
    )
    symbol: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=128,
                description="Optional display symbol name, from 1 through 128 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional display symbol name, from 1 through 128 characters.",
    )
    color: (
        Annotated[
            str,
            Field(
                pattern=r"^#[0-9A-Fa-f]{6}$",
                description="Optional six-digit hexadecimal display color prefixed by '#'.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional six-digit hexadecimal display color prefixed by '#'.",
    )


class EntityMetadata(PublicModel):
    """Carry optional display hints and explicit JSON-valued extension data.

    Properties are domain metadata supplied by the caller.
    """

    display: DisplayMetadata | None = Field(
        default=None,
        description="Optional reader-facing display hints for this entity.",
    )
    properties: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="JSON-valued domain metadata; defaults to an empty mapping.",
    )


class EntityIdentity(PublicModel):
    """Identify an entity independently of its mutable state.

    The optional fingerprint distinguishes a source-specific identity when supplied.
    """

    id: UUID = Field(description="Canonical UUID identifying the entity.")
    integration: IntegrationName = Field(
        description="Validated integration name that reports the entity."
    )
    fingerprint: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=256,
                description="Optional source-specific fingerprint, from 1 through 256 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional source-specific fingerprint, from 1 through 256 characters.",
    )


class Entity(PublicModel):
    """Represent a timestamped entity and its current public attributes.

    An embedded position, when present, uses the same public ``Location`` model as
    Position Location Information.
    """

    id: UUID = Field(description="Canonical UUID identifying the entity.")
    category: EntityCategory = Field(description="Domain category of the entity.")
    integration: IntegrationName = Field(
        description="Validated integration name that reports the entity."
    )
    recorded_at: AwareDatetime = Field(
        description="Timezone-aware time when the entity state was recorded."
    )
    type: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Source-defined entity type, from 1 through 128 characters.",
        ),
    ]
    fingerprint: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=256,
                description="Optional source-specific fingerprint, from 1 through 256 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional source-specific fingerprint, from 1 through 256 characters.",
    )
    name: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=256,
                description="Optional reader-facing entity name, from 1 through 256 characters.",
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="Optional reader-facing entity name, from 1 through 256 characters.",
    )
    status: EntityStatus = Field(
        default=EntityStatus.UNKNOWN,
        description="Activity state; defaults to UNKNOWN when unspecified.",
    )
    affiliation: Affiliation = Field(
        default=Affiliation.UNKNOWN,
        description="Reported affiliation; defaults to UNKNOWN when unspecified.",
    )
    parent_id: UUID | None = Field(
        default=None,
        description="Optional UUID of the entity that contains or owns this entity.",
    )
    metadata: EntityMetadata = Field(
        default_factory=EntityMetadata,
        description="Display and JSON-valued extension metadata; defaults to empty metadata.",
    )
    position: Location | None = Field(
        default=None,
        description="Optional embedded Position Location Information for the entity.",
    )

    @property
    def identity(self) -> EntityIdentity:
        """Return the entity's stable ID, integration, and optional fingerprint."""
        return EntityIdentity(
            id=self.id,
            integration=self.integration,
            fingerprint=self.fingerprint,
        )


class EntityEvent(PublicModel):
    """Pair an entity update with its event time and decoded location.

    The top-level location is absent when no location was decoded for the event.
    """

    timestamp: AwareDatetime = Field(
        description="Timezone-aware time associated with the entity event."
    )
    entity: Entity = Field(description="Entity state carried by the event.")
    location: Location | None = Field(
        default=None,
        description="Optional location decoded alongside the entity event.",
    )
