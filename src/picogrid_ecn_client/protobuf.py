# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Typed offline decoders for the deliberately narrow public protobuf subset."""

from __future__ import annotations

from uuid import UUID

from ._protocol import (
    build_location_protobuf_topic,
    decode_entity_protobuf,
    decode_location_protobuf,
)
from ._protocol.topics import build_entity_protobuf_decode_topic
from .exceptions import ValidationError
from .models import EntityCategory, EntityEvent, LocationEvent

_DEFAULT_MAXIMUM_PAYLOAD_SIZE = 1024 * 1024
_LARGEST_MAXIMUM_PAYLOAD_SIZE = 16 * 1024 * 1024


def _validate_inputs(
    payload: bytes,
    maximum_payload_size: int,
    category: EntityCategory | None = None,
) -> None:
    if not isinstance(payload, bytes):
        raise ValidationError(
            "protobuf payload must be bytes",
            operation="protobuf.decode",
        )
    if (
        isinstance(maximum_payload_size, bool)
        or not isinstance(maximum_payload_size, int)
        or not 1024 <= maximum_payload_size <= _LARGEST_MAXIMUM_PAYLOAD_SIZE
    ):
        raise ValidationError(
            "maximum_payload_size must be between 1024 and 16777216 bytes",
            operation="protobuf.decode",
        )
    if category is not None and not isinstance(category, EntityCategory):
        raise ValidationError(
            "category must be an EntityCategory",
            operation="protobuf.decode",
        )


def decode_entity_event_protobuf(
    payload: bytes,
    *,
    integration: str,
    category: EntityCategory,
    maximum_payload_size: int = _DEFAULT_MAXIMUM_PAYLOAD_SIZE,
) -> EntityEvent:
    """Decode one entity event from the supported protobuf subset.

    Typed integration and category context replaces a raw MQTT topic. Unknown
    protobuf fields are ignored. Future category numbers map to ``OTHER`` and
    future status or affiliation numbers map to ``UNKNOWN`` when strict category
    agreement still holds.

    Args:
        payload: Serialized protobuf payload.
        integration: Topic-derived integration name.
        category: Topic-derived entity category.
        maximum_payload_size: Inclusive payload limit in bytes, from 1024 through
            16777216.

    Returns:
        The decoded typed entity event.

    Raises:
        ProtocolError: If the payload is malformed, lacks required fields, or
            disagrees with the typed category context.
        ResourceLimitError: If the payload exceeds ``maximum_payload_size`` bytes.
        ValidationError: If an input or decoded public field is invalid.
    """

    _validate_inputs(payload, maximum_payload_size, category)
    topic = build_entity_protobuf_decode_topic(integration, category)
    return decode_entity_protobuf(topic, payload, maximum_payload_size)


def decode_entity_location_protobuf(
    payload: bytes,
    *,
    integration: str,
    entity_id: UUID,
    maximum_payload_size: int = _DEFAULT_MAXIMUM_PAYLOAD_SIZE,
) -> LocationEvent:
    """Decode one entity-location update from the supported protobuf subset.

    Typed integration and entity UUID context replaces a raw MQTT topic. Unknown
    protobuf fields are ignored.

    Args:
        payload: Serialized protobuf payload.
        integration: Topic-derived integration name.
        entity_id: Topic-derived canonical entity UUID.
        maximum_payload_size: Inclusive payload limit in bytes, from 1024 through
            16777216.

    Returns:
        The decoded typed location event.

    Raises:
        ProtocolError: If the payload is malformed or lacks required fields.
        ResourceLimitError: If the payload exceeds ``maximum_payload_size`` bytes.
        ValidationError: If an input or decoded public field is invalid.
    """

    _validate_inputs(payload, maximum_payload_size)
    if not isinstance(entity_id, UUID):
        raise ValidationError(
            "entity_id must be a UUID",
            operation="protobuf.decode",
        )
    topic = build_location_protobuf_topic(integration, entity_id)
    return decode_location_protobuf(topic, payload, maximum_payload_size)


__all__ = ["decode_entity_event_protobuf", "decode_entity_location_protobuf"]
