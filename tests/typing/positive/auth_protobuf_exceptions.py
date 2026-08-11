# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from typing import assert_type
from uuid import UUID

from picogrid_ecn_client import (
    BearerTokenAuth,
    ECNClientError,
    EntityCategory,
    EntityEvent,
    LocationEvent,
    decode_entity_event_protobuf,
    decode_entity_location_protobuf,
)


async def async_token() -> str:
    return "token"


async def async_credentials() -> tuple[str, str]:
    return ("user", "token")


def check_authentication_providers() -> None:
    BearerTokenAuth(token_provider=async_token)
    BearerTokenAuth(credentials_provider=async_credentials)


def check_protobuf_helpers(payload: bytes, entity_id: UUID) -> None:
    entity_event = decode_entity_event_protobuf(
        payload,
        integration="consumer",
        category=EntityCategory.SENSOR,
    )
    assert_type(entity_event, EntityEvent)
    location_event = decode_entity_location_protobuf(
        payload,
        integration="consumer",
        entity_id=entity_id,
    )
    assert_type(location_event, LocationEvent)


def check_exception_attributes(error: ECNClientError) -> None:
    assert_type(error.code, str)
    assert_type(error.status_code, int | None)
    assert_type(error.details, dict[str, str])
