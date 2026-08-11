# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from typing import Never, assert_type
from uuid import UUID

from pydantic import BaseModel, JsonValue

from picogrid_ecn_client import (
    DispatchReceipt,
    ECNClient,
    TaskAcknowledgement,
    TaskMode,
    TaskRequestContext,
    TaskResult,
)


class RequestModel(BaseModel):
    value: str


class ResultModel(BaseModel):
    accepted: bool


async def request_handler(request: RequestModel) -> ResultModel:
    return ResultModel(accepted=bool(request.value))


async def context_handler(
    context: TaskRequestContext,
    request: RequestModel,
) -> TaskResult[ResultModel]:
    del context, request
    raise RuntimeError


async def untyped_request_handler(request: RequestModel) -> dict[str, JsonValue]:
    return {"value": request.value}


async def untyped_context_handler(
    context: TaskRequestContext,
    request: RequestModel,
) -> None:
    del context, request


async def check_task_registration(client: ECNClient, entity_id: UUID) -> None:
    await client.tasks.register(
        entity_id=entity_id,
        command="typed-request",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=request_handler,
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="typed-context",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=context_handler,
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="untyped-request",
        request_model=RequestModel,
        handler=untyped_request_handler,
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="untyped-context",
        request_model=RequestModel,
        handler=untyped_context_handler,
    )


async def check_task_dispatch(client: ECNClient, target_id: UUID, request: RequestModel) -> None:
    typed = await client.tasks.send(
        target_entity_id=target_id,
        target_integration="consumer",
        command="typed",
        request=request,
        result_model=ResultModel,
        expected_connection_generation=1,
    )
    assert_type(typed, TaskResult[ResultModel])

    untyped = await client.tasks.send(
        target_entity_id=target_id,
        target_integration="consumer",
        command="untyped",
        request=request,
    )
    assert_type(untyped, TaskResult[Never])
    assert_type(untyped.data, dict[str, JsonValue] | None)

    acknowledgement = await client.tasks.send(
        target_entity_id=target_id,
        target_integration="consumer",
        command="acknowledge",
        request=request,
        mode=TaskMode.ACKNOWLEDGMENT,
    )
    # A rejected acknowledgement arrives as a FAILED/UNKNOWN TaskResult, so the
    # supported type is the union and callers must narrow before reading fields.
    assert_type(acknowledgement, TaskAcknowledgement | TaskResult[BaseModel])
    if isinstance(acknowledgement, TaskAcknowledgement):
        assert_type(acknowledgement.accepted, bool)

    receipt = await client.tasks.send(
        target_entity_id=target_id,
        target_integration="consumer",
        command="dispatch",
        request=request,
        mode=TaskMode.FIRE_AND_FORGET,
    )
    assert_type(receipt, DispatchReceipt)
