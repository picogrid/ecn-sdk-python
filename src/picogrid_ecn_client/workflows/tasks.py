# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable typed task receive and dispatch workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client import (
    ECNClient,
    TaskDispatchResult,
    TaskMode,
    TaskRegistration,
    TaskRequestContext,
    ValidationError,
)
from picogrid_ecn_client.models._base import PublicModel

from ._retention import _EventRetention


class EchoRequest(PublicModel):
    """Bounded request body used by the public task workflow examples."""

    message: str = Field(
        min_length=1,
        max_length=1024,
        description="Echo message to return unchanged.",
    )


class EchoResult(PublicModel):
    """Bounded result body returned by the public task workflow examples."""

    message: str = Field(
        min_length=1,
        max_length=1024,
        description="Echo message returned unchanged.",
    )


def _echo_request(message: str, *, operation: str) -> EchoRequest:
    try:
        return EchoRequest(message=message)
    except PydanticValidationError as exc:
        raise ValidationError(
            "echo request is invalid",
            operation=operation,
            details={
                "errors": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            },
        ) from exc


class ReceiveTaskResult(PublicModel):
    """Registration and retained requests handled before cleanup.

    Attributes:
        registration: Public registration created for the echo handler.
        contexts: All contexts from a positive-limit watch, or the most recent
            ``client.config.watcher_buffer_size`` contexts from an unbounded watch.
        results: Results corresponding to the retained request contexts.
    """

    registration: TaskRegistration = Field(
        description="Public registration created for the echo handler."
    )
    contexts: tuple[TaskRequestContext, ...] = Field(
        description=(
            "All contexts from a positive-limit watch, or the most recently retained "
            "contexts from an unbounded watch."
        )
    )
    results: tuple[EchoResult, ...] = Field(
        description="Results corresponding to the retained request contexts."
    )


class ReceiveMeshTaskResult(PublicModel):
    """Terminal identity plus retained requests handled before cleanup.

    Attributes:
        terminal_id: Configured local terminal identity for the routed handler.
        registration: Public registration created for the echo handler.
        contexts: All contexts from a positive-limit watch, or the most recent
            ``client.config.watcher_buffer_size`` contexts from an unbounded watch.
        results: Results corresponding to the retained request contexts.
    """

    terminal_id: UUID = Field(
        description="Configured local terminal identity for the routed handler."
    )
    registration: TaskRegistration = Field(
        description="Public registration created for the echo handler."
    )
    contexts: tuple[TaskRequestContext, ...] = Field(
        description=(
            "All contexts from a positive-limit watch, or the most recently retained "
            "contexts from an unbounded watch."
        )
    )
    results: tuple[EchoResult, ...] = Field(
        description="Results corresponding to the retained request contexts."
    )


class DispatchTaskResult(PublicModel):
    """Structured result returned by public task dispatch.

    Attributes:
        result: Public dispatch receipt or completed task result.
    """

    result: TaskDispatchResult = Field(
        description="Public dispatch receipt or completed task result."
    )


async def _receive_echo_tasks(
    client: ECNClient,
    *,
    entity_id: UUID,
    command: str,
    task_limit: int,
    on_event: Callable[[TaskRequestContext], None] | None,
    on_registered: Callable[[TaskRegistration], None] | None,
    accept: Callable[[TaskRequestContext], bool] | None = None,
) -> tuple[TaskRegistration, tuple[TaskRequestContext, ...], tuple[EchoResult, ...]]:
    if task_limit < 0:
        raise ValidationError("task_limit must be non-negative")
    completed = asyncio.Event()
    contexts = _EventRetention[TaskRequestContext](
        limit=task_limit, buffer_size=client.config.watcher_buffer_size
    )
    results = _EventRetention[EchoResult](
        limit=task_limit, buffer_size=client.config.watcher_buffer_size
    )
    accepting = True
    final_delivery: asyncio.Task[object] | None = None

    async def echo(context: TaskRequestContext, request: EchoRequest) -> EchoResult:
        nonlocal accepting, final_delivery
        if not accepting:
            raise ValidationError("task handler is no longer accepting requests")
        if accept is not None and not accept(context):
            raise ValidationError("task request source is not served by this handler")
        result = EchoResult(message=request.message)
        if on_event is not None:
            on_event(context)
        contexts.append(context)
        results.append(result)
        if task_limit and len(contexts) >= task_limit:
            final_delivery = asyncio.current_task()
            accepting = False
            completed.set()
        return result

    registration = await client.tasks.register(
        entity_id=entity_id,
        command=command,
        request_model=EchoRequest,
        result_model=EchoResult,
        handler=echo,
    )
    try:
        if on_registered is not None:
            on_registered(registration)
        await completed.wait()
    finally:
        try:
            await client.tasks.unregister(registration)
        finally:
            if final_delivery is not None:
                await final_delivery
    return registration, contexts.snapshot(), results.snapshot()


async def receive_task(
    client: ECNClient,
    *,
    entity_id: UUID,
    command: str,
    task_limit: int = 0,
    on_event: Callable[[TaskRequestContext], None] | None = None,
    on_registered: Callable[[TaskRegistration], None] | None = None,
) -> ReceiveTaskResult:
    """Register an echo handler and clean it up after ``task_limit`` requests.

    An unbounded receiver must consume every context through ``on_event``. Its
    returned tuples retain only the most recent
    ``client.config.watcher_buffer_size`` contexts and corresponding results.

    Args:
        client: Configured SDK client used to register the handler.
        entity_id: Canonical identity that owns the task handler.
        command: Exact command name accepted by the handler.
        task_limit: Requests to handle before cleanup; zero waits until interrupted
            while retaining only the most recent configured watcher buffer.
        on_event: Optional callback invoked for every accepted request context.
        on_registered: Optional callback invoked after registration succeeds.

    Returns:
        Registration metadata plus all positive-limit requests, or the most recently
        retained contexts and results from an unbounded receiver.

    Raises:
        ValidationError: If ``task_limit`` is negative or a request arrives after
            the configured limit has been accepted.
        ECNClientError: If registration, request handling, or cleanup fails.
    """

    registration, contexts, results = await _receive_echo_tasks(
        client,
        entity_id=entity_id,
        command=command,
        task_limit=task_limit,
        on_event=on_event,
        on_registered=on_registered,
    )
    return ReceiveTaskResult(
        registration=registration,
        contexts=contexts,
        results=results,
    )


async def receive_mesh_task(
    client: ECNClient,
    *,
    terminal_id: UUID,
    entity_id: UUID,
    command: str,
    task_limit: int = 0,
    on_event: Callable[[TaskRequestContext], None] | None = None,
    on_registered: Callable[[TaskRegistration], None] | None = None,
) -> ReceiveMeshTaskResult:
    """Register an echo handler for a terminal-addressed deployment route.

    An unbounded receiver must consume every context through ``on_event``. Its
    returned tuples retain only the most recent
    ``client.config.watcher_buffer_size`` contexts and corresponding results.

    Args:
        client: Configured SDK client used to register the handler.
        terminal_id: Local terminal identity that must match the client configuration.
        entity_id: Canonical identity that owns the task handler.
        command: Exact command name accepted by the handler.
        task_limit: Requests to handle before cleanup; zero waits until interrupted
            while retaining only the most recent configured watcher buffer.
        on_event: Optional callback invoked for every accepted request context.
        on_registered: Optional callback invoked after registration succeeds.

    Returns:
        Terminal identity and registration metadata plus all positive-limit requests,
        or the most recently retained contexts and results from an unbounded receiver.

    Raises:
        ValidationError: If ``terminal_id`` mismatches, ``task_limit`` is negative,
            a request arrives from a source other than a canonical source terminal, or
            a request arrives after the configured limit has been accepted.
        ECNClientError: If registration, request handling, or cleanup fails.
    """
    if terminal_id != client.config.terminal_id:
        raise ValidationError("terminal_id must match the configured client terminal_id")

    registration, contexts, results = await _receive_echo_tasks(
        client,
        entity_id=entity_id,
        command=command,
        task_limit=task_limit,
        on_event=on_event,
        on_registered=on_registered,
        # A terminal-addressed route serves canonical source terminals only. The literal
        # "local" compatibility source reaches the same entity and command topic, so
        # without this filter it would be echoed and counted against task_limit.
        accept=lambda context: isinstance(context.source, UUID),
    )
    return ReceiveMeshTaskResult(
        terminal_id=terminal_id,
        registration=registration,
        contexts=contexts,
        results=results,
    )


async def dispatch_task(
    client: ECNClient,
    *,
    target_entity_id: UUID,
    target_integration: str,
    command: str,
    message: str,
    timeout: float | None = None,
    mode: TaskMode = TaskMode.COMPLETE,
) -> DispatchTaskResult:
    """Dispatch one typed echo request on the direct integration route.

    Args:
        client: Configured SDK client used to dispatch the request.
        target_entity_id: Canonical identity of the task target.
        target_integration: Exact integration name that owns the target.
        command: Exact command name to invoke.
        message: Echo message sent in the typed request body.
        timeout: Optional maximum wait in seconds.
        mode: Requested task completion mode.

    Returns:
        The public dispatch receipt or completed task result.

    Raises:
        ECNClientError: If validation, dispatch, or response handling fails.
    """

    result = await client.tasks.send(
        target_entity_id=target_entity_id,
        target_integration=target_integration,
        command=command,
        request=_echo_request(message, operation="workflow.dispatch_task"),
        result_model=EchoResult if mode is TaskMode.COMPLETE else None,
        timeout=timeout,
        mode=mode,
    )
    return DispatchTaskResult(result=result)


async def dispatch_mesh_task(
    client: ECNClient,
    *,
    target_entity_id: UUID,
    target_integration: str,
    target_terminal_id: UUID,
    command: str,
    message: str,
    timeout: float | None = None,
    mode: TaskMode = TaskMode.COMPLETE,
) -> DispatchTaskResult:
    """Dispatch one typed echo request on an explicit terminal-addressed route.

    Args:
        client: Configured SDK client used to dispatch the request.
        target_entity_id: Canonical identity of the task target.
        target_integration: Exact integration name that owns the target.
        target_terminal_id: Canonical terminal identity that routes to the target.
        command: Exact command name to invoke.
        message: Echo message sent in the typed request body.
        timeout: Optional maximum wait in seconds.
        mode: Requested task completion mode.

    Returns:
        The public dispatch receipt or completed task result.

    Raises:
        ECNClientError: If validation, dispatch, or response handling fails.
    """

    result = await client.tasks.send(
        target_entity_id=target_entity_id,
        target_integration=target_integration,
        target_terminal_id=target_terminal_id,
        command=command,
        request=_echo_request(message, operation="workflow.dispatch_mesh_task"),
        result_model=EchoResult if mode is TaskMode.COMPLETE else None,
        timeout=timeout,
        mode=mode,
    )
    return DispatchTaskResult(result=result)


__all__ = [
    "DispatchTaskResult",
    "EchoRequest",
    "EchoResult",
    "ReceiveMeshTaskResult",
    "ReceiveTaskResult",
    "dispatch_mesh_task",
    "dispatch_task",
    "receive_mesh_task",
    "receive_task",
]
