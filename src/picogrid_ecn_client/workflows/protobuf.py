# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable local public-protobuf workflows."""

from __future__ import annotations

from pydantic import Field

from picogrid_ecn_client import (
    EntityCategory,
    EntityEvent,
    decode_entity_event_protobuf,
)
from picogrid_ecn_client.models._base import PublicModel


class DecodePublicProtobufResult(PublicModel):
    """Validated entity event decoded from public protobuf bytes.

    Attributes:
        event: Validated public entity event reconstructed from the payload.
    """

    event: EntityEvent = Field(
        description="Validated public entity event reconstructed from the payload."
    )


def decode_public_protobuf(
    payload: bytes,
    *,
    integration: str,
    category: EntityCategory,
    maximum_payload_size: int = 1024 * 1024,
) -> DecodePublicProtobufResult:
    """Decode one public entity-event protobuf payload without a connection.

    Args:
        payload: Serialized public entity-event protobuf bytes.
        integration: Integration name carried by the source topic.
        category: Entity category carried by the source topic.
        maximum_payload_size: Largest payload accepted, in bytes.

    Returns:
        The validated public entity event.

    Raises:
        ProtocolError: If the payload is malformed or disagrees with its category.
        ResourceLimitError: If ``payload`` exceeds ``maximum_payload_size``.
        ValidationError: If an input or decoded public field is invalid.
    """

    event = decode_entity_event_protobuf(
        payload,
        integration=integration,
        category=category,
        maximum_payload_size=maximum_payload_size,
    )
    return DecodePublicProtobufResult(event=event)


__all__ = ["DecodePublicProtobufResult", "decode_public_protobuf"]
