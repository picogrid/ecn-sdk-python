# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from picogrid_ecn_client import (
    Entity,
    EntityCategory,
    Location,
    ValidationError,
    decode_entity_event_protobuf,
    decode_entity_location_protobuf,
)
from picogrid_ecn_client._protocol import (
    encode_entity_protobuf,
    encode_location_protobuf,
)

ENTITY_ID = UUID("20000000-0000-4000-8000-000000000001")
RECORDED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_public_entity_protobuf_decoder_returns_typed_event() -> None:
    entity = Entity(
        id=ENTITY_ID,
        integration="synthetic-vendor",
        category=EntityCategory.TRACK,
        recorded_at=RECORDED_AT,
        type="synthetic-track",
    )
    event = decode_entity_event_protobuf(
        encode_entity_protobuf(entity, 64 * 1024),
        integration=entity.integration,
        category=entity.category,
    )
    assert event.entity == entity


def test_public_location_protobuf_decoder_returns_typed_event() -> None:
    location = Location(
        latitude=34,
        longitude=-118,
        recorded_at=RECORDED_AT,
    )
    event = decode_entity_location_protobuf(
        encode_location_protobuf(location, 64 * 1024),
        integration="synthetic-vendor",
        entity_id=ENTITY_ID,
    )
    assert event.entity_id == ENTITY_ID
    assert event.location == location


@pytest.mark.parametrize(
    "arguments",
    [
        {"payload": bytearray(), "integration": "synthetic", "category": EntityCategory.TRACK},
        {"payload": b"", "integration": "synthetic", "category": "TRACK"},
        {
            "payload": b"",
            "integration": "synthetic",
            "category": EntityCategory.TRACK,
            "maximum_payload_size": 1,
        },
    ],
)
def test_public_protobuf_decoder_rejects_invalid_inputs(arguments: object) -> None:
    with pytest.raises(ValidationError):
        decode_entity_event_protobuf(**arguments)  # type: ignore[arg-type]


def test_public_location_decoder_requires_uuid() -> None:
    with pytest.raises(ValidationError, match="UUID"):
        decode_entity_location_protobuf(
            b"",
            integration="synthetic",
            entity_id="not-a-uuid",  # type: ignore[arg-type]
        )
