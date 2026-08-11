# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from collections.abc import Callable
from uuid import UUID

from pydantic import BaseModel

from picogrid_ecn_client import BearerTokenAuth, ECNClient, TokenProvider


class RequestModel(BaseModel):
    value: str


class OtherRequest(BaseModel):
    count: int


def invalid_provider() -> str:
    return "synchronous-token"


async def check_invalid_arguments(client: ECNClient, entity_id: UUID) -> None:
    client.locations.last_observed("not-a-uuid")  # expect-type-error
    await client.tasks.send(  # expect-type-error
        target_entity_id="not-a-uuid",
        target_integration="consumer",
        command="invalid-target",
        request=RequestModel(value="value"),
    )
    # Commands do not statically expose a peer request model; send still requires a BaseModel.
    await client.tasks.send(  # expect-type-error
        target_entity_id=entity_id,
        target_integration="consumer",
        command="invalid-request",
        request={"not": "a model"},
    )


def check_invalid_auth_provider(provider: Callable[[], str]) -> None:
    typed_provider: TokenProvider = provider  # expect-type-error
    BearerTokenAuth(token_provider=typed_provider)
