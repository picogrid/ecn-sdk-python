# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from uuid import UUID

from pydantic import BaseModel

from picogrid_ecn_client import (
    ECNClient,
    EntityEvent,
    EventStream,
    LocationEvent,
    TaskResult,
)


class RequestModel(BaseModel):
    value: str


class ResultModel(BaseModel):
    accepted: bool


class OtherResult(BaseModel):
    code: str


def check_stream_invariance(entity_stream: EventStream[EntityEvent]) -> None:
    location_stream: EventStream[LocationEvent] = entity_stream  # expect-type-error
    del location_stream


def check_result_invariance(result: TaskResult[ResultModel]) -> None:
    other_result: TaskResult[OtherResult] = result  # expect-type-error
    del other_result


async def check_send_result_model(
    client: ECNClient,
    entity_id: UUID,
    request: RequestModel,
) -> None:
    result: TaskResult[OtherResult] = await client.tasks.send(
        target_entity_id=entity_id,
        target_integration="consumer",
        command="typed-result",
        request=request,
        result_model=ResultModel,  # expect-type-error
    )
    del result
