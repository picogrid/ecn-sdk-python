# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Minimal typed task exchange over the confirmed MQTT task wire."""

from __future__ import annotations

import asyncio
import builtins
import inspect
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .._protocol import (
    build_task_request_topic,
    build_task_response_topic,
    decode_json,
    encode_json,
)
from .._transport.mqtt import _PublishCompletion
from ..exceptions import (
    AuthorizationError,
    DeliveryError,
    ECNClientError,
    NotReadyError,
    OutcomeUnknownError,
    ProtocolError,
    ResourceLimitError,
)
from ..exceptions import ConnectionError as ECNConnectionError
from ..exceptions import TimeoutError as ECNTimeoutError
from ..exceptions import ValidationError as ECNValidationError
from ..interfaces.tasks import AnyTaskHandler, TaskDispatchResult
from ..models import (
    DeliveryPhase,
    DispatchReceipt,
    TaskAcknowledgement,
    TaskMode,
    TaskRegistration,
    TaskRequestContext,
    TaskResult,
    TaskStatus,
)

_Callback = Callable[[str, bytes], Awaitable[None]]
_TaskSource: TypeAlias = Literal["local"] | UUID
_DeliveryKey = tuple[str, UUID, str, str]
_LOCAL: Literal["local"] = "local"
_WIRE_MODES = {
    TaskMode.COMPLETE: "complete",
    TaskMode.ACKNOWLEDGMENT: "ack",
    TaskMode.FIRE_AND_FORGET: "fire_and_forget",
}
_PUBLIC_MODES = {wire: mode for mode, wire in _WIRE_MODES.items()}
_SAFE_VALIDATION_TYPES = frozenset({"greater_than_equal", "int_parsing", "missing", "value_error"})


class _Transport(Protocol):
    @property
    def connection_generation(self) -> int: ...

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None: ...

    async def subscribe(
        self,
        topic_filter: str,
        callback: _Callback,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> _SubscriptionHandle: ...

    async def unsubscribe(self, handle: object) -> None: ...

    async def wait_for_connection_loss(self, generation: int) -> None: ...


class _SubscriptionHandle(Protocol):
    @property
    def connection_generation(self) -> int: ...


@dataclass(slots=True)
class _Registration:
    public: TaskRegistration
    request_model: type[BaseModel]
    result_model: type[BaseModel] | None
    handler: AnyTaskHandler
    arity: int
    topic: str
    subscription: object | None = None


@dataclass(frozen=True, slots=True)
class _Envelope:
    route: _ResponseRoute
    task_id: str
    mode: TaskMode
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ResponseRoute:
    request_source: str
    context_source: _TaskSource
    topic_terminal_id: UUID | None
    payload_source: str


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: TaskStatus
    payload: dict[str, Any]
    error_message: str | None = None


class TaskService:
    """Exact-topic task handlers and bounded correlated dispatch."""

    def __init__(
        self,
        transport: _Transport,
        *,
        integration_name: str,
        terminal_id: UUID | None = None,
        operation_timeout: float = 10.0,
        max_outstanding: int = 128,
        duplicate_cache_size: int = 1024,
        max_payload_size: int = 1_048_576,
        shutdown_timeout: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        connection_generation: Callable[[], int] | None = None,
        ready_transport_generation: Callable[[], int | None] | None = None,
        task_id_factory: Callable[[], str] | None = None,
        registration_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if not isinstance(integration_name, str) or not integration_name.strip():
            raise ECNValidationError(
                "integration_name must be a non-empty string",
                operation="task_service.configure",
            )
        if (
            isinstance(operation_timeout, bool)
            or not isinstance(operation_timeout, (int, float))
            or not math.isfinite(operation_timeout)
            or not 0 <= operation_timeout <= 3600
        ):
            raise ECNValidationError(
                "operation_timeout must be finite and between zero and 3600 seconds",
                operation="task_service.configure",
            )
        if (
            isinstance(shutdown_timeout, bool)
            or not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(shutdown_timeout)
            or not 0 < shutdown_timeout <= 60
        ):
            raise ECNValidationError(
                "shutdown_timeout must be finite, greater than zero, and at most 60 seconds",
                operation="task_service.configure",
            )
        for name, value in (
            ("max_outstanding", max_outstanding),
            ("duplicate_cache_size", duplicate_cache_size),
            ("max_payload_size", max_payload_size),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ECNValidationError(
                    f"{name} must be a positive integer",
                    operation="task_service.configure",
                )
        self._transport = transport
        self._integration = integration_name.strip()
        self._terminal_id = terminal_id
        self._timeout = float(operation_timeout)
        self._max_outstanding = max_outstanding
        self._cache_size = duplicate_cache_size
        self._max_payload = max_payload_size
        self._shutdown_timeout = float(shutdown_timeout)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection_generation = connection_generation or (
            lambda: self._transport.connection_generation
        )
        self._ready_transport_generation = ready_transport_generation or (
            lambda: self._transport.connection_generation
        )
        self._task_id_factory = task_id_factory or (lambda: uuid4().hex[:16])
        self._registration_id_factory = registration_id_factory or uuid4

        self._closed = False
        self._registration_lock = asyncio.Lock()
        self._registrations: dict[tuple[UUID, str], _Registration] = {}
        self._active_operations = 0
        self._reserved_task_ids: set[str] = set()
        self._pending: dict[str, asyncio.Future[TaskDispatchResult]] = {}
        self._delivery_phases: dict[str, DeliveryPhase] = {}
        self._response_subscriptions: dict[str, _SubscriptionHandle] = {}
        self._deliveries: dict[_DeliveryKey, int] = {}
        self._delivery_cache: OrderedDict[_DeliveryKey, bytes | None] = OrderedDict()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._setup_tasks: set[asyncio.Task[Any]] = set()

    async def register(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[BaseModel],
        result_model: type[BaseModel] | None,
        handler: AnyTaskHandler,
    ) -> TaskRegistration:
        """Subscribe one handler to one exact request topic."""

        self._ensure_open()
        if not isinstance(entity_id, UUID):
            raise ECNValidationError(
                "entity_id must be a canonical UUID",
                operation="task.register",
            )
        self._validate_model(request_model, "request_model")
        if result_model is not None:
            self._validate_model(result_model, "result_model")
        topic = build_task_request_topic(self._integration, entity_id, command)
        public = TaskRegistration(
            registration_id=self._registration_id_factory(),
            entity_id=entity_id,
            command=command,
            request_schema=request_model.model_json_schema(),
            result_schema=result_model.model_json_schema() if result_model else None,
            registered_at=self._now(),
        )
        registration = _Registration(
            public, request_model, result_model, handler, self._handler_arity(handler), topic
        )

        async def receive(received_topic: str, payload: bytes) -> None:
            if not self._closed and received_topic == topic:
                await self._receive(registration, payload)

        key = (entity_id, command)
        setup_task = asyncio.current_task()
        assert setup_task is not None
        self._setup_tasks.add(setup_task)
        try:
            async with self._registration_lock:
                self._ensure_open()
                if key in self._registrations:
                    raise ECNValidationError(
                        "a handler is already registered for this entity and command",
                        operation="task.register",
                    )
                subscription = await self._subscribe(topic, receive, "task.register")
                if self._closed:
                    await self._unsubscribe(subscription, "task.register", strict=False)
                    raise NotReadyError(
                        "task service closed while registering a handler",
                        operation="task.register",
                    )
                registration.subscription = subscription
                self._registrations[key] = registration
        except asyncio.CancelledError:
            if self._closed:
                raise ECNConnectionError(
                    "task service closed while registering a handler",
                    operation="task.register",
                ) from None
            raise
        finally:
            self._setup_tasks.discard(setup_task)
        return public

    async def unregister(self, registration: TaskRegistration) -> None:
        """Remove a returned registration; repeated calls are harmless."""

        async with self._registration_lock:
            key = next(
                (
                    key
                    for key, stored in self._registrations.items()
                    if stored.public.registration_id == registration.registration_id
                ),
                None,
            )
            if key is None:
                return
            stored = self._registrations[key]
            try:
                if stored.subscription is not None:
                    await self._unsubscribe(stored.subscription, "task.unregister", strict=True)
            finally:
                # MQTTTransport retires the local handle before waiting for
                # UNSUBACK.  Retire the matching service record even when the
                # broker rejects that unsubscribe so re-registration cannot
                # mistake an inactive handler for a live one.
                self._registrations.pop(key, None)

    async def send(
        self,
        *,
        target_entity_id: UUID,
        target_integration: str,
        target_terminal_id: UUID | None = None,
        command: str,
        request: BaseModel,
        result_model: type[BaseModel] | None,
        timeout: float | None,
        mode: TaskMode,
        expected_connection_generation: int | None = None,
    ) -> TaskDispatchResult:
        """Publish one request and await its exact response when requested."""

        self._ensure_open()
        if not isinstance(target_entity_id, UUID):
            raise ECNValidationError(
                "target_entity_id must be a canonical UUID",
                operation="task.send",
            )
        if not isinstance(request, BaseModel):
            raise ECNValidationError(
                "request must be a Pydantic model instance", operation="task.send"
            )
        if result_model is not None:
            self._validate_model(result_model, "result_model")
        try:
            wire_mode = _WIRE_MODES[mode]
        except (KeyError, TypeError):
            raise ECNValidationError(
                "mode must be a supported TaskMode", operation="task.send"
            ) from None
        if timeout is None:
            deadline = self._timeout
        elif (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 3600
        ):
            raise ECNValidationError(
                "timeout must be finite and between zero and 3600 seconds",
                operation="task.send",
            )
        else:
            deadline = float(timeout)

        if target_terminal_id is not None and not isinstance(target_terminal_id, UUID):
            raise ECNValidationError(
                "target_terminal_id must be a canonical UUID",
                operation="task.send",
            )
        if expected_connection_generation is not None and (
            isinstance(expected_connection_generation, bool)
            or not isinstance(expected_connection_generation, int)
            or expected_connection_generation < 1
        ):
            raise ECNValidationError(
                "expected_connection_generation must be a positive integer",
                operation="task.send",
            )
        expected_transport_generation: int | None = None
        generation_matches = expected_connection_generation is None
        if expected_connection_generation is not None and (
            self._connection_generation() == expected_connection_generation
        ):
            expected_transport_generation = self._ready_transport_generation()
            generation_matches = expected_transport_generation is not None
        if target_terminal_id is not None and self._terminal_id is None:
            raise ECNValidationError(
                "terminal-addressed task dispatch requires ECNConfig.terminal_id",
                operation="task.send",
            )
        if target_terminal_id is not None and target_terminal_id == self._terminal_id:
            raise ECNValidationError(
                "target_terminal_id must identify a different terminal; omit it for "
                "same-ECN dispatch",
                operation="task.send",
            )
        request_topic = build_task_request_topic(
            target_integration,
            target_entity_id,
            command,
            target_terminal_id=target_terminal_id,
        )
        response_topic = build_task_response_topic(target_integration, target_entity_id, command)
        request_source = str(self._terminal_id) if self._terminal_id is not None else _LOCAL
        if target_terminal_id is not None:
            expected_response_source = str(target_terminal_id)
        elif self._terminal_id is not None:
            expected_response_source = str(self._terminal_id)
        else:
            expected_response_source = _LOCAL
        setup_task = asyncio.current_task()
        assert setup_task is not None
        cancellation_baseline = setup_task.cancelling()
        task_id = self._begin_operation()
        self._setup_tasks.add(setup_task)
        loss_waiter: asyncio.Task[None] | None = None
        response_future: asyncio.Future[TaskDispatchResult] | None = None
        definite_publish_rejection: ECNClientError | None = None
        try:
            if not generation_matches:
                raise self._delivery_failure(task_id)
            try:
                request_data = request.model_dump(mode="json")
            except (PydanticValidationError, TypeError, ValueError):
                raise ECNValidationError(
                    "request could not be serialized",
                    code="task_request_validation_error",
                    operation="task.send",
                ) from None
            encoded = encode_json(
                {
                    "source": request_source,
                    "task_id": task_id,
                    "_response_mode": wire_mode,
                    "payload": request_data,
                },
                self._max_payload,
            )
            if mode is TaskMode.FIRE_AND_FORGET:
                await self._publish(
                    request_topic,
                    encoded,
                    "task.send",
                    task_id=task_id,
                    expected_connection_generation=expected_transport_generation,
                )
                return DispatchReceipt(task_id=task_id, accepted_at=self._now())

            future: asyncio.Future[TaskDispatchResult] = asyncio.get_running_loop().create_future()
            response_future = future
            self._pending[task_id] = future

            async def receive(received_topic: str, payload: bytes) -> None:
                if not self._closed and not future.done() and received_topic == response_topic:
                    self._accept_response(
                        payload,
                        task_id,
                        mode,
                        result_model,
                        expected_response_source,
                        future,
                    )

            try:
                token = await self._subscribe(
                    response_topic,
                    receive,
                    "task.send",
                    restore_on_reconnect=False,
                    expected_connection_generation=expected_transport_generation,
                )
                self._response_subscriptions[task_id] = token
                if self._closed:
                    raise self._delivery_failure(task_id)
                if (
                    expected_transport_generation is not None
                    and token.connection_generation != expected_transport_generation
                ):
                    raise self._delivery_failure(task_id)
                loss_waiter = asyncio.create_task(
                    self._transport.wait_for_connection_loss(token.connection_generation),
                    name="picogrid-ecn-task-connection-loss",
                )
                try:
                    completion = await self._publish(
                        request_topic,
                        encoded,
                        "task.send",
                        task_id=task_id,
                        expected_connection_generation=(
                            expected_transport_generation
                            if expected_transport_generation is not None
                            else token.connection_generation
                        ),
                    )
                except (AuthorizationError, ResourceLimitError, ProtocolError) as error:
                    definite_publish_rejection = error
                    raise
                except DeliveryError:
                    if (
                        self._delivery_phases.get(task_id, DeliveryPhase.NOT_SENT)
                        is not DeliveryPhase.NOT_SENT
                        and future.done()
                        and not future.cancelled()
                    ):
                        return self._response_result(future, task_id)
                    raise
                if future.done() and not future.cancelled():
                    return self._response_result(future, task_id)
                if completion is _PublishCompletion.COMPLETED_AFTER_CANCELLATION:
                    self._delivery_phases[task_id] = DeliveryPhase.RESPONSE_PENDING
                    raise self._delivery_failure(task_id)
                # No await may separate observed PUBACK from RESPONSE_PENDING. A
                # disconnect after this assignment can no longer be mistaken for
                # a retry-safe failure.
                self._delivery_phases[task_id] = DeliveryPhase.RESPONSE_PENDING
                if future.done():
                    return self._response_result(future, task_id)
                if loss_waiter.done():
                    raise self._delivery_failure(task_id)
                try:
                    async with asyncio.timeout(deadline):
                        completed, _ = await asyncio.wait(
                            (future, loss_waiter),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                except builtins.TimeoutError:
                    if future.done():
                        return self._response_result(future, task_id)
                    raise self._delivery_failure(task_id) from None
                if future in completed or future.done():
                    return self._response_result(future, task_id)
                raise self._delivery_failure(task_id)
            finally:
                if loss_waiter is not None:
                    if not loss_waiter.done():
                        loss_waiter.cancel()
                    try:
                        await asyncio.gather(loss_waiter, return_exceptions=True)
                    except asyncio.CancelledError:
                        if definite_publish_rejection is None:
                            raise
                        self._consume_current_cancellation(cancellation_baseline)
                        await asyncio.gather(loss_waiter, return_exceptions=True)
                self._pending.pop(task_id, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()
                try:
                    await self._drop_response_subscription(task_id)
                except asyncio.CancelledError:
                    if definite_publish_rejection is None:
                        raise
                    self._consume_current_cancellation(cancellation_baseline)
                    raise definite_publish_rejection from None
        except asyncio.CancelledError:
            if (
                response_future is not None
                and response_future.done()
                and not response_future.cancelled()
            ):
                self._consume_current_cancellation(cancellation_baseline)
                return self._response_result(response_future, task_id)
            phase = self._delivery_phases.get(task_id, DeliveryPhase.NOT_SENT)
            if phase is not DeliveryPhase.NOT_SENT:
                self._consume_current_cancellation(cancellation_baseline)
                raise self._delivery_failure(task_id) from None
            raise
        except (ECNConnectionError, NotReadyError):
            raise self._delivery_failure(task_id) from None
        finally:
            self._setup_tasks.discard(setup_task)
            self._end_operation(task_id)

    async def close(self) -> None:
        """Cancel handlers, fail waiters, and remove exact subscriptions."""

        if self._closed:
            return
        self._closed = True
        for task_id, future in tuple(self._pending.items()):
            if not future.done():
                if self._delivery_phases.get(task_id) is DeliveryPhase.NOT_SENT:
                    future.cancel()
                else:
                    future.set_exception(self._delivery_failure(task_id))
        self._pending.clear()
        tasks = (*tuple(self._handler_tasks), *tuple(self._setup_tasks))
        for task in tasks:
            task.cancel()
        handlers_timed_out = False
        try:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
                handlers_timed_out = bool(pending)
        finally:
            self._deliveries.clear()
            async with self._registration_lock:
                registrations = tuple(self._registrations.values())
                self._registrations.clear()
            for registration in registrations:
                if registration.subscription is not None:
                    await self._unsubscribe(registration.subscription, "task.close", strict=False)
            for task_id in tuple(self._response_subscriptions):
                await self._drop_response_subscription(task_id)
            self._setup_tasks.clear()
            self._delivery_cache.clear()
        if handlers_timed_out:
            raise ECNTimeoutError(
                "task handler shutdown exceeded shutdown_timeout",
                operation="task.close",
            )

    async def _receive(self, registration: _Registration, encoded: bytes) -> None:
        try:
            raw = decode_json(encoded, self._max_payload)
        except ECNClientError:
            return
        route = self._parse_response_route(raw.get("source"))
        if route is None:
            return
        envelope = self._parse_envelope(raw, route)
        if envelope is None:
            await self._reject_invalid(registration, raw, route)
            return
        key = (
            route.request_source,
            registration.public.entity_id,
            registration.public.command,
            envelope.task_id,
        )
        if key in self._delivery_cache:
            response = self._delivery_cache[key]
            self._delivery_cache.move_to_end(key)
            if response is not None:
                await self._publish_response(registration, route, response)
            return
        if key in self._deliveries:
            self._deliveries[key] = min(self._deliveries[key] + 1, self._max_outstanding - 1)
            return
        try:
            request = registration.request_model.model_validate(envelope.payload)
        except PydanticValidationError as exc:
            response = None
            if envelope.mode is not TaskMode.FIRE_AND_FORGET:
                response = self._failure(
                    route,
                    envelope.task_id,
                    "validation_error",
                    self._pydantic_details(exc, registration.request_model),
                    self._response_type(envelope.mode),
                )
            self._cache(key, response)
            if response is not None:
                await self._publish_response(registration, route, response)
            return
        if len(self._handler_tasks) >= self._max_outstanding:
            if envelope.mode is not TaskMode.FIRE_AND_FORGET:
                await self._publish_response(
                    registration,
                    route,
                    self._failure(
                        route,
                        envelope.task_id,
                        "task handler capacity is exhausted",
                        response_type=self._response_type(envelope.mode),
                    ),
                )
            return

        context = TaskRequestContext(
            task_id=envelope.task_id,
            target_entity_id=registration.public.entity_id,
            command=registration.public.command,
            source=route.context_source,
            mode=envelope.mode,
            received_at=self._now(),
        )
        self._deliveries[key] = 0
        task = asyncio.create_task(
            self._run_delivery(registration, envelope, context, request, key),
            name="picogrid-ecn-task-handler",
        )
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_done)
        await asyncio.sleep(0)

    async def _run_delivery(
        self,
        registration: _Registration,
        envelope: _Envelope,
        context: TaskRequestContext,
        request: BaseModel,
        key: _DeliveryKey,
    ) -> None:
        try:
            if envelope.mode is TaskMode.ACKNOWLEDGMENT:
                # Keep the delivery active until the handler exits so bounded-cache
                # pressure cannot turn a redelivery into a second ACK or invocation.
                try:
                    await self._publish_response(
                        registration,
                        envelope.route,
                        self._response(
                            envelope.route,
                            envelope.task_id,
                            TaskStatus.SUCCESS,
                            {"ack": True, "message": "Task started"},
                            "ack",
                        ),
                    )
                except OutcomeUnknownError:
                    # The acknowledgment may already have reached the requester.
                    # Honor its "Task started" contract by invoking the handler
                    # exactly once, while retaining the active delivery key so a
                    # redelivery cannot publish another ACK or duplicate the effect.
                    # The completed tombstone installed below suppresses later
                    # redeliveries without retrying this uncertain publication.
                    if self._closed:
                        # Service close owns cancellation and must not start new
                        # user code after closing begins. The request subscription
                        # is being retired, so discard the active delivery without
                        # replaying the uncertain ACK or emitting a final response.
                        self._deliveries.pop(key, None)
                        return
                except Exception:
                    # A definite failure means no ACK was accepted and no effect
                    # began. Do not install a tombstone that would suppress a broker
                    # redelivery.
                    self._deliveries.pop(key, None)
                    raise
                await self._invoke(registration, context, request)
                await self._finish(registration, key, envelope.route, None)
                return
            if envelope.mode is TaskMode.FIRE_AND_FORGET:
                await self._invoke(registration, context, request)
                await self._finish(registration, key, envelope.route, None)
                return
            outcome = await self._run_handler(registration, context, request)
            await self._finish(
                registration,
                key,
                envelope.route,
                self._response(
                    envelope.route,
                    envelope.task_id,
                    outcome.status,
                    outcome.payload,
                    "full",
                    outcome.error_message,
                ),
            )
        except asyncio.CancelledError:
            self._deliveries.pop(key, None)
            raise
        except Exception:
            if key in self._deliveries:
                response = (
                    self._failure(envelope.route, envelope.task_id, "task processing failed")
                    if envelope.mode is TaskMode.COMPLETE
                    else None
                )
                await self._finish(registration, key, envelope.route, response)

    async def _finish(
        self,
        registration: _Registration,
        key: _DeliveryKey,
        route: _ResponseRoute,
        response: bytes | None,
    ) -> None:
        duplicates = self._deliveries.pop(key, 0)
        if self._closed:
            return
        self._cache(key, response)
        if response is not None:
            publication_count = duplicates + 1
            for _ in range(publication_count):
                await self._publish_response(registration, route, response)

    def _cache(self, key: _DeliveryKey, response: bytes | None) -> None:
        self._delivery_cache[key] = response
        self._delivery_cache.move_to_end(key)
        while len(self._delivery_cache) > self._cache_size:
            self._delivery_cache.popitem(last=False)

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handler_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _run_handler(
        self, registration: _Registration, context: TaskRequestContext, request: BaseModel
    ) -> _Outcome:
        try:
            returned = await self._invoke(registration, context, request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _Outcome(TaskStatus.FAILED, {}, "task handler execution failed")
        status = TaskStatus.SUCCESS
        if isinstance(returned, TaskResult):
            status = returned.status
            if status not in {TaskStatus.SUCCESS, TaskStatus.PENDING}:
                if status is TaskStatus.FAILED:
                    payload = self._normalize_result(returned.data, None)
                    return _Outcome(
                        status,
                        payload,
                        returned.error_message or "task processing failed",
                    )
                return _Outcome(
                    TaskStatus.FAILED, {}, "task handler returned an invalid result status"
                )
            returned = returned.data
        try:
            payload = self._normalize_result(returned, registration.result_model)
            encode_json(payload, self._max_payload)
            return _Outcome(status, payload)
        except PydanticValidationError as exc:
            return _Outcome(
                TaskStatus.FAILED,
                self._pydantic_details(exc, registration.result_model),
                "task result validation failed",
            )
        except ECNClientError:
            return _Outcome(TaskStatus.FAILED, {}, "task result validation failed")
        except (TypeError, ValueError):
            return _Outcome(TaskStatus.FAILED, {}, "task handler returned an invalid result")

    async def _invoke(
        self, registration: _Registration, context: TaskRequestContext, request: BaseModel
    ) -> object:
        args = (request,) if registration.arity == 1 else (context, request)
        returned = registration.handler(*args)
        if not inspect.isawaitable(returned):
            raise TypeError("task handler did not return an awaitable")
        return await returned

    @staticmethod
    def _normalize_result(returned: object, result_model: type[BaseModel] | None) -> dict[str, Any]:
        if result_model is not None:
            value = (
                returned.model_dump(mode="json") if isinstance(returned, BaseModel) else returned
            )
            return result_model.model_validate(value).model_dump(mode="json")
        if returned is None:
            return {}
        if isinstance(returned, BaseModel):
            return returned.model_dump(mode="json")
        if isinstance(returned, Mapping):
            return dict(returned)
        raise TypeError("unsupported handler result")

    def _accept_response(
        self,
        encoded: bytes,
        task_id: str,
        mode: TaskMode,
        result_model: type[BaseModel] | None,
        expected_source: str,
        future: asyncio.Future[TaskDispatchResult],
    ) -> None:
        try:
            raw = decode_json(encoded, self._max_payload)
        except ECNClientError:
            return
        if raw.get("source") != expected_source or raw.get("task_id") != task_id:
            return
        try:
            result = self._decode_response(raw, mode, result_model)
        except ECNClientError as exc:
            if not future.done():
                future.set_exception(exc)
        else:
            if not future.done():
                future.set_result(result)

    def _decode_response(
        self,
        raw: dict[str, Any],
        mode: TaskMode,
        result_model: type[BaseModel] | None,
    ) -> TaskDispatchResult:
        task_id = self._bounded_string(raw.get("task_id"), "task_id")
        status_value = raw.get("status")
        payload = raw.get("payload")
        if not isinstance(status_value, str) or not status_value or len(status_value) > 128:
            raise ProtocolError("task response status is invalid", operation="task.send")
        if not isinstance(payload, dict):
            raise ProtocolError("task response payload must be an object", operation="task.send")
        if raw.get("_response_type") != self._response_type(mode):
            raise ProtocolError("task response type is invalid", operation="task.send")
        try:
            status = TaskStatus(status_value)
        except ValueError:
            status = TaskStatus.UNKNOWN
        now = self._now()
        error_message = raw.get("error_message")
        if error_message is not None and (
            not isinstance(error_message, str)
            or not error_message.strip()
            or len(error_message) > 1024
        ):
            raise ProtocolError("task failure response is invalid", operation="task.send")
        if status in {TaskStatus.UNKNOWN, TaskStatus.FAILED}:
            return TaskResult[BaseModel](
                task_id=task_id,
                status=status,
                data=payload,
                error_message=error_message,
                completed_at=now,
            )
        if mode is TaskMode.ACKNOWLEDGMENT:
            if status is not TaskStatus.SUCCESS:
                raise ProtocolError(
                    "task acknowledgment status is invalid",
                    operation="task.send",
                )
            if payload.get("ack") is not True or payload.get("message") != "Task started":
                raise ProtocolError(
                    "task acknowledgment payload is invalid",
                    operation="task.send",
                )
            try:
                return TaskAcknowledgement(
                    task_id=task_id,
                    accepted=True,
                    message="Task started",
                    acknowledged_at=now,
                )
            except PydanticValidationError:
                raise ProtocolError(
                    "task acknowledgment payload is invalid", operation="task.send"
                ) from None
        try:
            data = payload if result_model is None else result_model.model_validate(payload)
        except PydanticValidationError:
            raise ProtocolError(
                "task response does not match the requested result model",
                operation="task.send",
            ) from None
        return TaskResult[BaseModel](
            task_id=task_id,
            status=status,
            data=data,
            error_message=error_message,
            completed_at=now,
        )

    def _parse_response_route(self, value: object) -> _ResponseRoute | None:
        if value == _LOCAL:
            return _ResponseRoute(_LOCAL, _LOCAL, None, _LOCAL)
        if not isinstance(value, str) or self._terminal_id is None:
            return None
        try:
            source = UUID(value)
        except ValueError:
            return None
        if str(source) != value:
            return None
        if source == self._terminal_id:
            return _ResponseRoute(value, source, None, str(self._terminal_id))
        return _ResponseRoute(value, source, source, str(self._terminal_id))

    def _parse_envelope(
        self,
        raw: dict[str, Any],
        route: _ResponseRoute,
    ) -> _Envelope | None:
        try:
            task_id = self._bounded_string(raw.get("task_id"), "task_id")
        except ProtocolError:
            return None
        wire_mode, payload = raw.get("_response_mode"), raw.get("payload")
        if not isinstance(wire_mode, str) or wire_mode not in _PUBLIC_MODES:
            return None
        if not isinstance(payload, dict):
            return None
        return _Envelope(route, task_id, _PUBLIC_MODES[wire_mode], payload)

    async def _reject_invalid(
        self,
        registration: _Registration,
        raw: dict[str, Any],
        route: _ResponseRoute,
    ) -> None:
        task_id, wire_mode = raw.get("task_id"), raw.get("_response_mode")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or len(task_id.strip()) > 128
            or wire_mode not in {"complete", "ack"}
        ):
            return
        await self._publish_response(
            registration,
            route,
            self._failure(
                route,
                task_id.strip(),
                "task request envelope is invalid",
                response_type="ack" if wire_mode == "ack" else "full",
            ),
        )

    def _response(
        self,
        route: _ResponseRoute,
        task_id: str,
        status: TaskStatus,
        payload: dict[str, Any],
        response_type: str,
        error_message: str | None = None,
    ) -> bytes:
        value = {
            "status": status.value,
            "source": route.payload_source,
            "task_id": task_id,
            "payload": payload,
            "_response_type": response_type,
        }
        if error_message is not None:
            value["error_message"] = error_message
        return encode_json(value, self._max_payload)

    def _failure(
        self,
        route: _ResponseRoute,
        task_id: str,
        message: str,
        payload: dict[str, Any] | None = None,
        response_type: str = "full",
    ) -> bytes:
        return self._response(
            route,
            task_id,
            TaskStatus.FAILED,
            payload or {},
            response_type,
            message,
        )

    async def _publish_response(
        self,
        registration: _Registration,
        route: _ResponseRoute,
        payload: bytes,
    ) -> None:
        if self._closed:
            return
        topic = build_task_response_topic(
            self._integration,
            registration.public.entity_id,
            registration.public.command,
            route_terminal_id=route.topic_terminal_id,
        )
        await self._publish(topic, payload, "task.respond")

    def _begin_operation(self) -> str:
        self._ensure_open()
        if self._active_operations >= self._max_outstanding:
            raise ResourceLimitError(
                "maximum outstanding task operations reached", operation="task.send"
            )
        task_id = self._allocate_task_id()
        self._reserved_task_ids.add(task_id)
        self._delivery_phases[task_id] = DeliveryPhase.NOT_SENT
        self._active_operations += 1
        return task_id

    def _end_operation(self, task_id: str) -> None:
        self._reserved_task_ids.discard(task_id)
        self._delivery_phases.pop(task_id, None)
        self._active_operations = max(0, self._active_operations - 1)

    def _allocate_task_id(self) -> str:
        for _ in range(16):
            candidate = self._task_id_factory()
            if not isinstance(candidate, str):
                break
            candidate = candidate.strip()
            if candidate and len(candidate) <= 128 and candidate not in self._reserved_task_ids:
                return candidate
        raise ResourceLimitError(
            "could not allocate a unique task identifier", operation="task.send"
        )

    async def _drop_response_subscription(self, task_id: str) -> None:
        token = self._response_subscriptions.pop(task_id, None)
        if token is not None:
            await self._unsubscribe(token, "task.send", strict=False)

    async def _publish(
        self,
        topic: str,
        payload: bytes,
        operation: str,
        *,
        task_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None:
        def send_started() -> None:
            if task_id is not None:
                self._delivery_phases[task_id] = DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING

        try:
            if expected_connection_generation is None:
                completion = await self._transport.publish(
                    topic,
                    payload,
                    qos=1,
                    on_send_started=send_started if task_id is not None else None,
                )
            else:
                completion = await self._transport.publish(
                    topic,
                    payload,
                    qos=1,
                    on_send_started=send_started if task_id is not None else None,
                    expected_connection_generation=expected_connection_generation,
                )
        except DeliveryError as error:
            if task_id is None:
                raise
            raise self._enrich_delivery_error(error, task_id) from None
        except ECNClientError:
            raise
        except Exception:
            if task_id is not None:
                raise self._delivery_failure(task_id) from None
            raise ECNConnectionError("task transport publish failed", operation=operation) from None
        if task_id is not None:
            self._delivery_phases[task_id] = DeliveryPhase.BROKER_ACCEPTED
        return completion

    async def _subscribe(
        self,
        topic: str,
        callback: _Callback,
        operation: str,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> _SubscriptionHandle:
        try:
            return await self._transport.subscribe(
                topic,
                callback,
                restore_on_reconnect=restore_on_reconnect,
                expected_connection_generation=expected_connection_generation,
            )
        except ECNClientError:
            raise
        except Exception:
            raise ECNConnectionError(
                "task transport subscription failed", operation=operation
            ) from None

    def _delivery_failure(self, task_id: str) -> DeliveryError:
        phase = self._delivery_phases.get(task_id, DeliveryPhase.NOT_SENT)
        if phase is DeliveryPhase.BROKER_ACCEPTED:
            phase = DeliveryPhase.RESPONSE_PENDING
        if phase in {
            DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
            DeliveryPhase.RESPONSE_PENDING,
        }:
            return OutcomeUnknownError(
                "task delivery outcome is unknown",
                delivery_phase=phase,
                operation="task.send",
                task_id=task_id,
            )
        return DeliveryError(
            "task request was not sent",
            delivery_phase=DeliveryPhase.NOT_SENT,
            operation="task.send",
            task_id=task_id,
        )

    def _response_result(
        self,
        future: asyncio.Future[TaskDispatchResult],
        task_id: str,
    ) -> TaskDispatchResult:
        try:
            return future.result()
        except ProtocolError:
            phase = self._delivery_phases.get(task_id, DeliveryPhase.NOT_SENT)
            if phase in {
                DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
                DeliveryPhase.BROKER_ACCEPTED,
                DeliveryPhase.RESPONSE_PENDING,
            }:
                raise self._delivery_failure(task_id) from None
            raise

    @staticmethod
    def _consume_current_cancellation(cancellation_baseline: int) -> None:
        task = asyncio.current_task()
        # asyncio.timeout() owns its cancellation request and removes it in
        # __aexit__.  If this operation began with existing cancellation debt,
        # consuming here as well would erase a pre-existing request.  Exactly
        # one request over a zero baseline is the only unambiguous state and
        # covers ordinary direct and TaskGroup cancellation.
        if task is not None and cancellation_baseline == 0 and task.cancelling() == 1:
            task.uncancel()

    @staticmethod
    def _enrich_delivery_error(error: DeliveryError, task_id: str) -> DeliveryError:
        error_type: type[DeliveryError] = (
            OutcomeUnknownError if isinstance(error, OutcomeUnknownError) else DeliveryError
        )
        return error_type(
            "task delivery did not complete",
            delivery_phase=error.delivery_phase,
            operation="task.send",
            task_id=task_id,
            code=error.code,
            status_code=error.status_code,
            details=error.details,
        )

    async def _unsubscribe(self, token: object, operation: str, *, strict: bool) -> None:
        try:
            await self._transport.unsubscribe(token)
        except ECNClientError:
            if strict:
                raise
        except Exception:
            if strict:
                raise ECNConnectionError(
                    "task transport unsubscribe failed", operation=operation
                ) from None

    def _ensure_open(self) -> None:
        if self._closed:
            raise NotReadyError("task service is closed", operation="task")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ECNValidationError(
                "task service clock must return a timezone-aware datetime",
                operation="task_service.clock",
            )
        return value.astimezone(UTC)

    @staticmethod
    def _validate_model(model: object, field: str) -> None:
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise ECNValidationError(
                f"{field} must be a Pydantic model class", operation="task.register"
            )

    @staticmethod
    def _handler_arity(handler: AnyTaskHandler) -> int:
        if not callable(handler):
            raise ECNValidationError("handler must be callable", operation="task.register")
        handler_call = inspect.getattr_static(type(handler), "__call__", None)
        asynchronous = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
            handler_call
        )
        if not asynchronous:
            raise ECNValidationError(
                "handler must be an async callable",
                operation="task.register",
            )
        try:
            signature = inspect.signature(handler)
            accepts_one = TaskService._signature_accepts(signature, 1)
            accepts_two = TaskService._signature_accepts(signature, 2)
        except (TypeError, ValueError):
            raise ECNValidationError(
                "handler signature could not be inspected", operation="task.register"
            ) from None
        if accepts_one == accepts_two:
            raise ECNValidationError(
                "handler must accept exactly request or context and request",
                operation="task.register",
            )
        return 1 if accepts_one else 2

    @staticmethod
    def _signature_accepts(signature: inspect.Signature, count: int) -> bool:
        try:
            signature.bind(*(object(),) * count)
        except TypeError:
            return False
        return True

    @staticmethod
    def _bounded_string(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
            raise ProtocolError(f"task {field} is invalid", operation="task")
        return value.strip()

    @staticmethod
    def _response_type(mode: TaskMode) -> str:
        return "ack" if mode is TaskMode.ACKNOWLEDGMENT else "full"

    @staticmethod
    def _pydantic_details(
        exc: PydanticValidationError,
        model: type[BaseModel] | None,
    ) -> dict[str, Any]:
        errors: list[dict[str, object]] = []
        root_fields = model.model_fields if model is not None else {}
        for item in exc.errors(include_url=False, include_context=False, include_input=False):
            location: list[str | int] = []
            for index, part in enumerate(item.get("loc", ())[:16]):
                if isinstance(part, int) or (
                    index == 0 and isinstance(part, str) and part in root_fields
                ):
                    location.append(part)
                else:
                    location.append("nested")
            candidate = str(item.get("type", "validation_error"))
            error_type = candidate if candidate in _SAFE_VALIDATION_TYPES else "validation_error"
            errors.append(
                {
                    "loc": location,
                    "type": error_type,
                    "msg": "value is invalid",
                }
            )
        return {"errors": errors[:32]}


__all__ = ["TaskService"]
