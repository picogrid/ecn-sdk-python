# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from typing import assert_type
from uuid import UUID

from pydantic import BaseModel

from picogrid_ecn_client import ECNClient, TaskResult


class RequestModel(BaseModel):
    value: str


async def check_await_and_optional(client: ECNClient, entity_id: UUID) -> None:
    assert_type(client.start(), None)  # expect-type-error
    forgotten_send = client.tasks.send(
        target_entity_id=entity_id,
        target_integration="consumer",
        command="forgotten-await",
        request=RequestModel(value="value"),
    )
    assert_type(forgotten_send, TaskResult[RequestModel])  # expect-type-error

    observed = client.locations.last_observed(entity_id)
    assert_type(observed.latitude, float)  # expect-type-error
