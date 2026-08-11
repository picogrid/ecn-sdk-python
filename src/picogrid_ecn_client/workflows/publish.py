# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable typed publication workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client import (
    DisplayMetadata,
    ECNClient,
    Entity,
    EntityCategory,
    EntityMetadata,
    Location,
    PublicationReceipt,
    ValidationError,
)
from picogrid_ecn_client.models._base import PublicModel


class PublishEntityResult(PublicModel):
    """Published entity and its broker acknowledgement.

    Attributes:
        entity: Typed entity submitted to the ECN.
        receipt: Broker acknowledgement for the publication.
    """

    entity: Entity = Field(description="Typed entity submitted to the ECN.")
    receipt: PublicationReceipt = Field(description="Broker acknowledgement for the publication.")


class PublishLocationResult(PublicModel):
    """Published location and its broker acknowledgement.

    Attributes:
        entity_id: Identity associated with the published location.
        location: Typed location submitted to the ECN.
        receipt: Broker acknowledgement for the publication.
    """

    entity_id: UUID = Field(description="Identity associated with the published location.")
    location: Location = Field(description="Typed location submitted to the ECN.")
    receipt: PublicationReceipt = Field(description="Broker acknowledgement for the publication.")


async def publish_entity(
    client: ECNClient,
    *,
    entity_id: UUID,
    category: EntityCategory,
    entity_type: str,
    name: str | None = None,
    display_name: str | None = None,
    recorded_at: datetime | None = None,
) -> PublishEntityResult:
    """Construct and publish one typed public entity.

    Args:
        client: Configured SDK client used for publication.
        entity_id: Canonical identity of the entity to publish.
        category: Public category assigned to the entity.
        entity_type: Deployment-defined type assigned to the entity.
        name: Optional entity name.
        display_name: Optional operator-facing display label.
        recorded_at: Optional observation time; current UTC time when omitted.

    Returns:
        The published entity and its broker acknowledgement.

    Raises:
        ECNClientError: If validation, connection, or publication fails.
    """

    try:
        entity = Entity(
            id=entity_id,
            category=category,
            integration=client.config.integration_name,
            recorded_at=recorded_at or datetime.now(UTC),
            type=entity_type,
            name=name,
            metadata=EntityMetadata(
                display=DisplayMetadata(label=display_name) if display_name is not None else None
            ),
        )
    except PydanticValidationError as exc:
        raise ValidationError(
            "entity input is invalid",
            operation="workflow.publish_entity",
            details={
                "errors": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            },
        ) from exc
    return PublishEntityResult(entity=entity, receipt=await client.entities.publish(entity))


async def publish_location(
    client: ECNClient,
    *,
    entity_id: UUID,
    location: Location,
) -> PublishLocationResult:
    """Publish one typed public location for an entity identity.

    Args:
        client: Configured SDK client used for publication.
        entity_id: Canonical identity associated with the location.
        location: Validated public location to publish.

    Returns:
        The entity identity, published location, and broker acknowledgement.

    Raises:
        ECNClientError: If validation, connection, or publication fails.
    """

    receipt = await client.locations.publish(entity_id=entity_id, location=location)
    return PublishLocationResult(entity_id=entity_id, location=location, receipt=receipt)


__all__ = [
    "PublishEntityResult",
    "PublishLocationResult",
    "publish_entity",
    "publish_location",
]
