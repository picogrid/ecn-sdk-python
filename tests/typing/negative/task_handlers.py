# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from collections.abc import Awaitable
from uuid import UUID

from pydantic import BaseModel

from picogrid_ecn_client import ECNClient, RequestTaskHandler, TaskRequestContext


class RequestModel(BaseModel):
    value: str


class OtherRequest(BaseModel):
    count: int


class ResultModel(BaseModel):
    accepted: bool


class OtherResult(BaseModel):
    code: str


async def wrong_request(request: str) -> ResultModel:
    return ResultModel(accepted=bool(request))


async def wrong_result(request: RequestModel) -> int:
    return len(request.value)


async def wrong_model(request: RequestModel) -> OtherResult:
    return OtherResult(code=request.value)


async def wrong_arity(
    context: TaskRequestContext,
    request: RequestModel,
    extra: str,
) -> ResultModel:
    del context, request
    return ResultModel(accepted=bool(extra))


def awaitable_returning(request: RequestModel) -> Awaitable[ResultModel]:
    # Registration requires a coroutine function; inspect.iscoroutinefunction is
    # false for a plain callable that merely returns an awaitable.
    raise NotImplementedError


def synchronous(request: RequestModel) -> ResultModel:
    return ResultModel(accepted=bool(request.value))


async def check_invalid_handlers(client: ECNClient, entity_id: UUID) -> None:
    await client.tasks.register(  # expect-type-error
        entity_id=entity_id,
        command="wrong-request",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=wrong_request,
    )
    await client.tasks.register(  # expect-type-error
        entity_id=entity_id,
        command="wrong-result",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=wrong_result,
    )
    typed_handler: RequestTaskHandler[RequestModel, ResultModel] = wrong_model  # expect-type-error
    await client.tasks.register(
        entity_id=entity_id,
        command="wrong-model",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=typed_handler,
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="wrong-arity",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=wrong_arity,  # expect-type-error
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="synchronous",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=synchronous,  # expect-type-error
    )
    await client.tasks.register(
        entity_id=entity_id,
        command="awaitable-returning",
        request_model=RequestModel,
        result_model=ResultModel,
        handler=awaitable_returning,  # expect-type-error
    )
