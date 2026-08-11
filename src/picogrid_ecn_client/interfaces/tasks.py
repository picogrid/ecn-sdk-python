# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Stable typed task registration and dispatch API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Literal, Never, Protocol, TypeAlias, TypeVar, overload
from uuid import UUID

from pydantic import BaseModel, JsonValue

from ..models import (
    DispatchReceipt,
    TaskAcknowledgement,
    TaskMode,
    TaskRegistration,
    TaskRequestContext,
    TaskResult,
)

RequestT = TypeVar("RequestT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)
RequestT_contra = TypeVar("RequestT_contra", bound=BaseModel, contravariant=True)
ResultT_co = TypeVar("ResultT_co", bound=BaseModel, covariant=True)


class RequestTaskHandler(Protocol[RequestT_contra, ResultT_co]):
    """Handle a validated task request without dispatch context.

    The client invokes the async handler with one request model. Closing the
    client cancels an in-flight invocation and bounds shutdown waiting by
    ``shutdown_timeout``. Return values are encoded only for task modes that
    require a result; handler failures or invalid results become failed task
    responses when the mode requires a response.

    Registration inspects the signature and accepts only a handler whose
    parameters bind to exactly one positional argument. An extra defaulted
    positional parameter, such as ``async def handler(request, retry=False)``,
    binds to both one and two arguments, so registration rejects it as
    ambiguous even though callable subtyping accepts it here.

    Args:
        request: Request model validated before invocation.

    Returns:
        A coroutine resolving to a result model, task result, JSON mapping, or
        ``None``.
    """

    def __call__(
        self, request: RequestT_contra, /
    ) -> Coroutine[Any, Any, ResultT_co | TaskResult[ResultT_co] | Mapping[str, JsonValue] | None]:
        """Invoke the handler with a validated request.

        Args:
            request: Request model validated before invocation.

        Returns:
            A coroutine resolving to a result model, task result, JSON mapping,
            or ``None``. Registration rejects non-async callables, so the
            protocol requires a coroutine rather than any awaitable.
        """
        ...


class ContextTaskHandler(Protocol[RequestT_contra, ResultT_co]):
    """Handle a validated task request together with its dispatch context.

    The client invokes the async handler with context followed by the request
    model. Closing the client cancels an in-flight invocation and bounds
    shutdown waiting by ``shutdown_timeout``. Return values are encoded only
    for task modes that require a result; handler failures or invalid results
    become failed task responses when the mode requires a response.

    Registration inspects the signature and accepts only a handler whose
    parameters bind to exactly two positional arguments. A defaulted extra
    positional parameter makes the arity ambiguous and is rejected at
    registration even though callable subtyping accepts it here.

    Args:
        context: Source, mode, and timing context for the request.
        request: Request model validated before invocation.

    Returns:
        A coroutine resolving to a result model, task result, JSON mapping, or
        ``None``.
    """

    def __call__(
        self, context: TaskRequestContext, request: RequestT_contra, /
    ) -> Coroutine[Any, Any, ResultT_co | TaskResult[ResultT_co] | Mapping[str, JsonValue] | None]:
        """Invoke the handler with context and a validated request.

        Args:
            context: Source, mode, and timing context for the request.
            request: Request model validated before invocation.

        Returns:
            A coroutine resolving to a result model, task result, JSON mapping,
            or ``None``. Registration rejects non-async callables, so the
            protocol requires a coroutine rather than any awaitable.
        """
        ...


TaskHandler: TypeAlias = (
    RequestTaskHandler[RequestT, ResultT] | ContextTaskHandler[RequestT, ResultT]
)
"""An async request-only or context-and-request task handler."""
TaskDispatchResult: TypeAlias = TaskResult[BaseModel] | TaskAcknowledgement | DispatchReceipt
"""A task result, acknowledgment, or dispatch receipt selected by task mode."""
AnyTaskHandler: TypeAlias = Callable[..., Awaitable[object]]


class _TaskClient(Protocol):
    def _ensure_ready(self) -> None: ...

    async def _register_task(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[BaseModel],
        result_model: type[BaseModel] | None,
        handler: AnyTaskHandler,
    ) -> TaskRegistration: ...

    async def _unregister_task(self, registration: TaskRegistration) -> None: ...

    async def _send_task(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None,
        timeout: float | None,
        mode: TaskMode,
        expected_connection_generation: int | None,
    ) -> TaskDispatchResult: ...


class Tasks:
    """Register typed handlers and dispatch typed requests."""

    def __init__(self, client: _TaskClient) -> None:
        self.__client = client

    @overload
    async def register(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[RequestT],
        handler: RequestTaskHandler[RequestT, ResultT] | ContextTaskHandler[RequestT, ResultT],
        result_model: type[ResultT],
    ) -> TaskRegistration: ...

    @overload
    async def register(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[RequestT],
        handler: RequestTaskHandler[RequestT, BaseModel] | ContextTaskHandler[RequestT, BaseModel],
        result_model: None = None,
    ) -> TaskRegistration: ...

    async def register(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[BaseModel],
        handler: AnyTaskHandler,
        result_model: type[BaseModel] | None = None,
    ) -> TaskRegistration:
        """Register one async handler for an exact task request topic.

        The handler must accept either the validated request or a
        :class:`TaskRequestContext` followed by the request. Its return is
        validated against ``result_model`` when provided. Closing the client
        cancels active handlers and bounds shutdown waiting by
        ``shutdown_timeout``.

        Args:
            entity_id: Entity that receives the command.
            command: Command identifying the exact task request topic.
            request_model: Model used to validate request payloads.
            handler: Async request-only or context-and-request callable.
            result_model: Optional model used to validate handler results.

        Returns:
            The local registration and its request and result schemas.

        Raises:
            ValidationError: If a model or handler signature is invalid, or a
                handler is already registered for this entity and command.
            NotReadyError: If the client cannot register a handler.
            AuthorizationError: If the broker rejects the request-topic
                subscription.
            ConnectionError: If the transport fails while subscribing.
        """

        self.__client._ensure_ready()
        return await self.__client._register_task(
            entity_id=entity_id,
            command=command,
            request_model=request_model,
            result_model=result_model,
            handler=handler,
        )

    async def unregister(self, registration: TaskRegistration) -> None:
        """Remove a local task handler and release its exact subscription.

        Repeated calls for the same registration are safe.

        Args:
            registration: Registration returned by `register`.

        Raises:
            NotReadyError: If the client is not ready.
            AuthorizationError: If the broker rejects the unsubscribe.
            ProtocolError: If the broker returns a malformed UNSUBACK.
            ConnectionError: If the transport fails while unsubscribing.
        """

        self.__client._ensure_ready()
        await self.__client._unregister_task(registration)

    @overload
    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[ResultT],
        timeout: float | None = None,
        mode: Literal[TaskMode.COMPLETE] = TaskMode.COMPLETE,
        expected_connection_generation: int | None = None,
    ) -> TaskResult[ResultT]: ...

    @overload
    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: None = None,
        timeout: float | None = None,
        mode: Literal[TaskMode.COMPLETE] = TaskMode.COMPLETE,
        expected_connection_generation: int | None = None,
    ) -> TaskResult[Never]: ...

    @overload
    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None = None,
        timeout: float | None = None,
        mode: Literal[TaskMode.ACKNOWLEDGMENT],
        expected_connection_generation: int | None = None,
    ) -> TaskAcknowledgement | TaskResult[BaseModel]: ...

    @overload
    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None = None,
        timeout: float | None = None,
        mode: Literal[TaskMode.FIRE_AND_FORGET],
        expected_connection_generation: int | None = None,
    ) -> DispatchReceipt: ...

    @overload
    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None = None,
        timeout: float | None = None,
        mode: TaskMode,
        expected_connection_generation: int | None = None,
    ) -> TaskDispatchResult: ...

    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None = None,
        timeout: float | None = None,
        mode: TaskMode = TaskMode.COMPLETE,
        expected_connection_generation: int | None = None,
    ) -> object:
        """Dispatch one task request using the selected response mode.

        ``COMPLETE`` returns a :class:`TaskResult`. ``ACKNOWLEDGMENT`` returns a
        :class:`TaskAcknowledgement` when the receiver accepts the request, and a
        :class:`TaskResult` carrying ``FAILED`` or ``UNKNOWN`` status when the
        receiver rejects it, so callers must narrow before reading
        acknowledgment fields. ``FIRE_AND_FORGET`` returns a
        :class:`DispatchReceipt` after broker PUBACK without waiting for a
        response. A response deadline after PUBACK has an unknown outcome;
        the client raises :class:`OutcomeUnknownError` with
        ``RESPONSE_PENDING`` and the generated task ID without retrying.
        Cancellation stops local waiting without publishing a cancellation
        request.

        Args:
            target_entity_id: Entity that receives the command.
            target_integration: Integration owning the target entity.
            target_terminal_id: Optional exact terminal route for the target.
            command: Command to dispatch.
            request: Pydantic request payload.
            result_model: Optional model used to validate a complete response.
            timeout: Optional response timeout in seconds.
            mode: Response behavior for the dispatch. ``TaskMode.COMPLETE``
                waits for a result, ``TaskMode.ACKNOWLEDGMENT`` waits only for
                the receiver to accept the request, and
                ``TaskMode.FIRE_AND_FORGET`` publishes without waiting.
            expected_connection_generation: Optional ready-connection generation
                that must still be active at the publication boundary. When supplied,
                it must be a non-boolean integer of one or greater. A mismatch fails
                before publication.

        Returns:
            The acknowledgment, result, or dispatch receipt selected by ``mode``
            and the responding status.

        Raises:
            NotReadyError: If the client is not ready.
            OutcomeUnknownError: If request send begins without a valid PUBACK,
                or a valid PUBACK is followed by a missing, disconnected,
                timed-out, malformed, or invalid correlated response. The error
                retains the safe delivery phase and generated task ID. The client
                does not retry automatically.
            ValidationError: If a dispatch argument or the request payload is
                invalid, including an explicit self-target.
            ResourceLimitError: If outstanding task operations are exhausted or
                a payload exceeds the configured maximum.
            AuthorizationError: If the broker rejects the response subscription
                or the request publication.
            ProtocolError: If the response SUBACK or a definite PUBACK result
                violates the supported acknowledgment contract.
            DeliveryError: If response-subscription or publication setup fails
                before request send begins. A mismatch in
                ``expected_connection_generation`` raises this error with phase
                ``NOT_SENT`` before publication. The error carries the generated
                task ID.
        """

        self.__client._ensure_ready()
        return await self.__client._send_task(
            target_entity_id=target_entity_id,
            target_integration=target_integration,
            target_terminal_id=target_terminal_id,
            command=command,
            request=request,
            result_model=result_model,
            timeout=timeout,
            mode=mode,
            expected_connection_generation=expected_connection_generation,
        )


__all__ = [
    "ContextTaskHandler",
    "RequestTaskHandler",
    "TaskDispatchResult",
    "TaskHandler",
    "Tasks",
]
