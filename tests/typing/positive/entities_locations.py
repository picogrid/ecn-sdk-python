# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from typing import assert_type
from uuid import UUID

from picogrid_ecn_client import (
    ECNClient,
    Entity,
    EntityEvent,
    EventStream,
    Location,
    LocationEvent,
    PublicationReceipt,
)


async def check_entities(client: ECNClient, entity: Entity) -> None:
    stream = await client.entities.watch()
    assert_type(stream, EventStream[EntityEvent])
    async for event in stream:
        assert_type(event, EntityEvent)
    assert_type(await client.entities.publish(entity), PublicationReceipt)


async def check_locations(
    client: ECNClient,
    entity_id: UUID,
    location: Location,
) -> None:
    stream = await client.locations.watch(entity_ids=[entity_id])
    assert_type(stream, EventStream[LocationEvent])
    async for event in stream:
        assert_type(event, LocationEvent)

    observed = client.locations.last_observed(entity_id)
    assert_type(observed, Location | None)
    if observed is not None:
        assert_type(observed, Location)

    assert_type(await client.locations.wait_for_update(entity_id), Location)
    terminal = await client.locations.wait_for_terminal_geolocation()
    assert_type(terminal, LocationEvent)
    receipt = await client.locations.publish(entity_id=entity_id, location=location)
    assert_type(receipt, PublicationReceipt)
