# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client._services.tasks import TaskService
from picogrid_ecn_client._transport.mqtt import _PublishCompletion
from picogrid_ecn_client.exceptions import (
    AuthorizationError,
    ConnectionError,
    DeliveryError,
    OutcomeUnknownError,
    ProtocolError,
    ResourceLimitError,
    TimeoutError,
    ValidationError,
)
from picogrid_ecn_client.models import (
    DeliveryPhase,
    DispatchReceipt,
    TaskAcknowledgement,
    TaskMode,
    TaskRequestContext,
    TaskResult,
    TaskStatus,
)

ENTITY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FIXED_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
REQUEST_TOPIC = f"task/receiver/{ENTITY_ID}/calculate"
RESPONSE_TOPIC = f"{REQUEST_TOPIC}/response"

MessageCallback = Callable[[str, bytes], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FakeSubscriptionHandle:
    token: int
    connection_generation: int


class Request(BaseModel):
    value: int = Field(ge=0)


class Result(BaseModel):
    doubled: int


class SecretRequest(BaseModel):
    token: str
    values: dict[str, int]

    @field_validator("token")
    @classmethod
    def reject_token(cls, value: str) -> str:
        raise ValueError(f"rejected credential {value}")


class FakeTransport:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []
        self.publish_attempts = 0
        self.subscriptions: dict[FakeSubscriptionHandle, tuple[str, MessageCallback]] = {}
        self.restore_on_reconnect: dict[FakeSubscriptionHandle, bool] = {}
        self.unsubscribed: list[int] = []
        self._next_token = 0
        self._publication_events: dict[str, asyncio.Event] = {}
        self.publish_error: Exception | None = None
        self.publish_completion: _PublishCompletion | None = None
        self.unsubscribe_error: Exception | None = None
        self.connected = True
        self.connection_generation = 1
        self._connection_lost = asyncio.Event()

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None:
        if not self.connected or (
            expected_connection_generation is not None
            and expected_connection_generation != self.connection_generation
        ):
            raise DeliveryError(
                "synthetic generation is unavailable",
                delivery_phase=DeliveryPhase.NOT_SENT,
                operation="mqtt.publish",
            )
        self.publish_attempts += 1
        if self.publish_error is not None:
            raise self.publish_error
        if on_send_started is not None:
            on_send_started()
        self.published.append((topic, payload, qos))
        self._publication_events.setdefault(topic, asyncio.Event()).set()
        callbacks = [
            callback
            for topic_filter, callback in tuple(self.subscriptions.values())
            if topic_filter == topic
        ]
        for callback in callbacks:
            await callback(topic, payload)
        return self.publish_completion

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> FakeSubscriptionHandle:
        if expected_connection_generation is not None and (
            expected_connection_generation != self.connection_generation
        ):
            raise ConnectionError(
                "synthetic generation is unavailable",
                operation="mqtt.subscribe",
            )
        self._next_token += 1
        handle = FakeSubscriptionHandle(self._next_token, self.connection_generation)
        self.subscriptions[handle] = (topic_filter, callback)
        self.restore_on_reconnect[handle] = restore_on_reconnect
        return handle

    async def unsubscribe(self, token: object) -> None:
        assert isinstance(token, FakeSubscriptionHandle)
        self.subscriptions.pop(token, None)
        self.restore_on_reconnect.pop(token, None)
        self.unsubscribed.append(token.token)
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error

    async def wait_for_connection_loss(self, generation: int) -> None:
        if not self.connected or generation != self.connection_generation:
            return
        await self._connection_lost.wait()

    def disconnect(self) -> None:
        self.connected = False
        self._connection_lost.set()
        for handle, restore in tuple(self.restore_on_reconnect.items()):
            if not restore:
                self.subscriptions.pop(handle, None)
                self.restore_on_reconnect.pop(handle, None)

    async def emit(self, topic: str, envelope: dict[str, Any] | bytes) -> None:
        payload = (
            envelope
            if isinstance(envelope, bytes)
            else json.dumps(envelope, separators=(",", ":")).encode()
        )
        callbacks = [
            callback
            for topic_filter, callback in tuple(self.subscriptions.values())
            if topic_filter == topic
        ]
        for callback in callbacks:
            await callback(topic, payload)

    async def wait_for_publish(self, topic: str) -> None:
        if any(published_topic == topic for published_topic, _, _ in self.published):
            return
        await self._publication_events.setdefault(topic, asyncio.Event()).wait()

    async def wait_for_publication_count(self, topic: str, count: int) -> None:
        reached = asyncio.Event()
        loop = asyncio.get_running_loop()

        def check() -> None:
            matches = sum(published_topic == topic for published_topic, _, _ in self.published)
            if matches >= count:
                reached.set()
            else:
                loop.call_soon(check)

        loop.call_soon(check)
        await asyncio.wait_for(reached.wait(), timeout=1)

    def decoded_publications(self, topic: str) -> list[dict[str, Any]]:
        return [
            json.loads(payload)
            for published_topic, payload, _ in self.published
            if published_topic == topic
        ]


class BlockingSubscribeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.subscribe_started = asyncio.Event()
        self.release_subscribe = asyncio.Event()

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> FakeSubscriptionHandle:
        self.subscribe_started.set()
        await self.release_subscribe.wait()
        return await super().subscribe(
            topic_filter,
            callback,
            restore_on_reconnect=restore_on_reconnect,
            expected_connection_generation=expected_connection_generation,
        )


class BlockingResponsePublishTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.response_publish_started = asyncio.Event()
        self.release_response_publish = asyncio.Event()

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        if topic == RESPONSE_TOPIC:
            self.response_publish_started.set()
            await self.release_response_publish.wait()
        await super().publish(
            topic,
            payload,
            qos=qos,
            on_send_started=on_send_started,
            expected_connection_generation=expected_connection_generation,
        )


class BlockingRequestPublishTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.request_publish_started = asyncio.Event()

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        if topic != REQUEST_TOPIC:
            await super().publish(
                topic,
                payload,
                qos=qos,
                on_send_started=on_send_started,
                expected_connection_generation=expected_connection_generation,
            )
            return
        if not self.connected or (
            expected_connection_generation is not None
            and expected_connection_generation != self.connection_generation
        ):
            await super().publish(
                topic,
                payload,
                qos=qos,
                on_send_started=on_send_started,
                expected_connection_generation=expected_connection_generation,
            )
            return
        self.publish_attempts += 1
        if on_send_started is not None:
            on_send_started()
        self.request_publish_started.set()
        await asyncio.Event().wait()


class BlockingUnsubscribeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.unsubscribe_started = asyncio.Event()
        self.release_unsubscribe = asyncio.Event()

    async def unsubscribe(self, token: object) -> None:
        self.unsubscribe_started.set()
        try:
            await asyncio.shield(self.release_unsubscribe.wait())
        except asyncio.CancelledError:
            await self.release_unsubscribe.wait()
            await super().unsubscribe(token)
            raise
        await super().unsubscribe(token)


class BlockingConnectionLossCleanupTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.connection_loss_waiter_started = asyncio.Event()
        self.connection_loss_waiter_cancelled = asyncio.Event()
        self.release_connection_loss_waiter = asyncio.Event()

    async def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None:
        await self.connection_loss_waiter_started.wait()
        return await super().publish(
            topic,
            payload,
            qos=qos,
            on_send_started=on_send_started,
            expected_connection_generation=expected_connection_generation,
        )

    async def wait_for_connection_loss(self, generation: int) -> None:
        self.connection_loss_waiter_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connection_loss_waiter_cancelled.set()
            await self.release_connection_loss_waiter.wait()
            raise


def service(transport: FakeTransport, **overrides: Any) -> TaskService:
    task_ids = iter([f"task-{index}" for index in range(1, 100)])
    return TaskService(
        transport,
        integration_name="receiver",
        clock=lambda: FIXED_NOW,
        task_id_factory=lambda: next(task_ids),
        **overrides,
    )


@pytest.mark.parametrize(
    "value",
    [True, "1", float("nan"), float("inf"), -1, 3601],
)
def test_task_service_rejects_invalid_operation_timeout(value: object) -> None:
    with pytest.raises(ValidationError, match="operation_timeout must be finite"):
        service(FakeTransport(), operation_timeout=value)


@pytest.mark.parametrize(
    "value",
    [True, "1", float("nan"), float("inf"), 0, 61],
)
def test_task_service_rejects_invalid_shutdown_timeout(value: object) -> None:
    with pytest.raises(ValidationError, match="shutdown_timeout must be finite"):
        service(FakeTransport(), shutdown_timeout=value)


def test_task_context_rejects_non_local_source() -> None:
    with pytest.raises(PydanticValidationError):
        TaskRequestContext(
            task_id="task-1",
            target_entity_id=ENTITY_ID,
            command="calculate",
            source="another-source",  # type: ignore[arg-type]
            mode=TaskMode.COMPLETE,
            received_at=FIXED_NOW,
        )


@pytest.mark.asyncio
async def test_default_task_identifier_is_sixteen_hex_characters() -> None:
    transport = FakeTransport()
    tasks = TaskService(transport, integration_name="sender", clock=lambda: FIXED_NOW)

    receipt = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=1),
        result_model=None,
        timeout=None,
        mode=TaskMode.FIRE_AND_FORGET,
    )

    assert isinstance(receipt, DispatchReceipt)
    assert len(receipt.task_id) == 16
    assert int(receipt.task_id, 16) >= 0
    await tasks.close()


@pytest.mark.parametrize(
    "value",
    [True, "1", float("nan"), float("inf"), -1, 3601],
)
@pytest.mark.asyncio
async def test_send_rejects_invalid_timeout_before_any_wire_operation(value: object) -> None:
    transport = FakeTransport()
    tasks = service(transport)

    with pytest.raises(ValidationError, match="timeout must be finite"):
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=cast(Any, value),
            mode=TaskMode.COMPLETE,
        )

    assert transport.published == []
    assert transport.subscriptions == {}
    await tasks.close()


@pytest.mark.asyncio
async def test_complete_dispatch_validates_context_request_and_result() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    contexts: list[TaskRequestContext] = []

    async def handle(context: TaskRequestContext, request: Request) -> Result:
        contexts.append(context)
        return Result(doubled=request.value * 2)

    registration = await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    result = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=7),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )

    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.SUCCESS
    assert isinstance(result.data, Result)
    assert result.data.doubled == 14
    assert result.error_message is None
    assert result.completed_at == FIXED_NOW
    assert contexts == [
        TaskRequestContext(
            task_id="task-1",
            target_entity_id=ENTITY_ID,
            command="calculate",
            source="local",
            mode=TaskMode.COMPLETE,
            received_at=FIXED_NOW,
        )
    ]
    assert registration.request_schema == Request.model_json_schema()
    assert registration.result_schema == Result.model_json_schema()

    request_envelope = transport.decoded_publications(REQUEST_TOPIC)[0]
    assert request_envelope == {
        "source": "local",
        "task_id": "task-1",
        "_response_mode": "complete",
        "payload": {"value": 7},
    }
    response_envelope = transport.decoded_publications(RESPONSE_TOPIC)[0]
    assert response_envelope == {
        "status": "SUCCESS",
        "source": "local",
        "task_id": "task-1",
        "payload": {"doubled": 14},
        "_response_type": "full",
    }
    assert all(qos == 1 for _, _, qos in transport.published)


@pytest.mark.asyncio
async def test_async_request_only_handler_and_idempotent_unregister() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def handle(request: Request) -> Result:
        return Result(doubled=request.value * 2)

    registration = await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    result = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=3),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )
    assert isinstance(result, TaskResult)
    assert isinstance(result.data, Result)
    assert result.data.doubled == 6

    await tasks.unregister(registration)
    await tasks.unregister(registration)
    assert transport.subscriptions == {}


@pytest.mark.asyncio
async def test_failed_unregister_retires_registration_and_allows_reregistration() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def handle(request: Request) -> Result:
        return Result(doubled=request.value * 2)

    registration = await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    transport.unsubscribe_error = AuthorizationError(
        "synthetic negative acknowledgement",
        operation="mqtt.unsubscribe",
    )

    with pytest.raises(AuthorizationError, match="negative acknowledgement"):
        await tasks.unregister(registration)

    transport.unsubscribe_error = None
    replacement = await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    await tasks.unregister(replacement)
    assert transport.subscriptions == {}


@pytest.mark.asyncio
async def test_acknowledgment_is_immediate_and_handler_is_tracked() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def handle(request: Request) -> Result:
        started.set()
        await release.wait()
        completed.set()
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    acknowledgment = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=4),
        result_model=Result,
        timeout=1,
        mode=TaskMode.ACKNOWLEDGMENT,
    )

    assert isinstance(acknowledgment, TaskAcknowledgement)
    assert acknowledgment.accepted is True
    assert acknowledgment.message == "Task started"
    assert acknowledgment.acknowledged_at == FIXED_NOW
    await started.wait()
    assert not completed.is_set()
    release.set()
    await completed.wait()
    await asyncio.sleep(0)
    assert len(transport.decoded_publications(RESPONSE_TOPIC)) == 1
    await tasks.close()


@pytest.mark.asyncio
async def test_fire_and_forget_returns_receipt_without_response_subscription() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    completed = asyncio.Event()

    async def handle(request: Request) -> None:
        assert request.value == 9
        completed.set()

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=None,
        handler=handle,
    )
    receipt = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=9),
        result_model=None,
        timeout=None,
        mode=TaskMode.FIRE_AND_FORGET,
    )

    assert receipt == DispatchReceipt(task_id="task-1", accepted_at=FIXED_NOW)
    await completed.wait()
    assert transport.decoded_publications(RESPONSE_TOPIC) == []
    assert list(transport.subscriptions.values()) == [
        (REQUEST_TOPIC, next(iter(transport.subscriptions.values()))[1])
    ]
    await tasks.close()


@pytest.mark.asyncio
async def test_invalid_request_returns_structured_failure_without_calling_handler() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    called = False

    async def handle(request: Request) -> Result:
        nonlocal called
        called = True
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": "local",
            "task_id": "invalid-request",
            "_response_mode": "complete",
            "payload": {"value": -1},
        },
    )

    assert called is False
    response = transport.decoded_publications(RESPONSE_TOPIC)[0]
    assert response["status"] == "FAILED"
    assert response["error_message"] == "validation_error"
    assert set(response) == {
        "status",
        "source",
        "task_id",
        "payload",
        "_response_type",
        "error_message",
    }
    errors = response["payload"]["errors"]
    assert errors[0]["loc"] == ["value"]
    assert errors[0]["msg"] == "value is invalid"
    assert set(errors[0]) == {"loc", "type", "msg"}


@pytest.mark.asyncio
async def test_non_local_request_is_ignored_before_envelope_validation() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    called = False

    async def handle(request: Request) -> Result:
        nonlocal called
        called = True
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": "another-source",
            "task_id": "non-local",
            "_response_mode": "not-a-mode",
            "payload": "not-an-object",
        },
    )

    await asyncio.sleep(0)
    assert called is False
    assert transport.decoded_publications(RESPONSE_TOPIC) == []
    await tasks.close()


@pytest.mark.asyncio
async def test_validation_failure_payload_does_not_echo_secret_input() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def handle(request: SecretRequest) -> None:
        raise AssertionError("handler must not run")

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=SecretRequest,
        result_model=None,
        handler=handle,
    )
    secret = "do-not-echo-this-credential"
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": "local",
            "task_id": "secret-validation",
            "_response_mode": "complete",
            "payload": {"token": secret, "values": {secret: "not-an-integer"}},
        },
    )

    encoded = next(payload for topic, payload, _ in transport.published if topic == RESPONSE_TOPIC)
    assert secret.encode() not in encoded
    errors = transport.decoded_publications(RESPONSE_TOPIC)[0]["payload"]["errors"]
    assert errors[0] == {
        "loc": ["token"],
        "type": "value_error",
        "msg": "value is invalid",
    }
    assert errors[1] == {
        "loc": ["values", "nested"],
        "type": "int_parsing",
        "msg": "value is invalid",
    }


@pytest.mark.asyncio
async def test_invalid_handler_result_becomes_public_failed_result() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def handle(request: Request) -> dict[str, int]:
        return {"unexpected": request.value}

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    result = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=1),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )

    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.FAILED
    assert isinstance(result.data, dict)
    assert result.data["errors"][0]["loc"] == ["doubled"]
    assert result.error_message == "task result validation failed"


@pytest.mark.asyncio
async def test_handler_may_return_pinned_pending_or_failed_result() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    statuses = iter((TaskStatus.PENDING, TaskStatus.FAILED))

    async def handle(request: Request) -> TaskResult[Result]:
        status = next(statuses)
        if status is TaskStatus.PENDING:
            return TaskResult[Result](
                task_id="handler-result",
                status=status,
                data=Result(doubled=request.value * 2),
                completed_at=FIXED_NOW,
            )
        return TaskResult[Result](
            task_id="handler-result",
            status=status,
            data={"retryable": False},
            error_message="synthetic task failed",
            completed_at=FIXED_NOW,
        )

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    pending = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=2),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )
    failed = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=3),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )

    assert isinstance(pending, TaskResult)
    assert pending.status is TaskStatus.PENDING
    assert pending.data == Result(doubled=4)
    assert isinstance(failed, TaskResult)
    assert failed.status is TaskStatus.FAILED
    assert failed.data == {"retryable": False}
    assert failed.error_message == "synthetic task failed"
    assert transport.decoded_publications(RESPONSE_TOPIC)[-1] == {
        "status": "FAILED",
        "source": "local",
        "task_id": "task-2",
        "payload": {"retryable": False},
        "_response_type": "full",
        "error_message": "synthetic task failed",
    }


@pytest.mark.parametrize(
    "response_update",
    [
        {"status": None},
        {"_response_type": "ack"},
        {"payload": []},
        {"payload": {"wrong": 2}},
    ],
    ids=("invalid-status", "invalid-response-type", "invalid-payload", "invalid-result"),
)
@pytest.mark.asyncio
async def test_sender_preserves_uncertainty_for_correlated_malformed_response(
    response_update: dict[str, object],
) -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    task_id = transport.decoded_publications(REQUEST_TOPIC)[0]["task_id"]
    assert tasks._delivery_phases[task_id] is DeliveryPhase.RESPONSE_PENDING
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": task_id,
            "status": "SUCCESS",
            "payload": {"doubled": 2},
            "_response_type": "full",
        }
        | response_update,
    )

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == task_id
    assert caught.value.operation == "task.send"
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_invalid_task_result_does_not_leak_hostile_payload_through_exception_chain() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    canary = "raw-hostile-payload-canary"
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    task_id = transport.decoded_publications(REQUEST_TOPIC)[0]["task_id"]
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": task_id,
            "status": "SUCCESS",
            "payload": {"doubled": canary},
            "_response_type": "full",
        },
    )

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == task_id
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_sender_accepts_pinned_local_response_without_timestamp() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    request = transport.decoded_publications(REQUEST_TOPIC)[0]
    assert request["source"] == "local"
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": request["task_id"],
            "status": "SUCCESS",
            "payload": {"doubled": 4},
            "_response_type": "full",
        },
    )

    result = await pending
    assert isinstance(result, TaskResult)
    assert result.completed_at == FIXED_NOW
    assert result.data == Result(doubled=4)
    assert transport.subscriptions == {}


@pytest.mark.asyncio
async def test_sender_accepts_pinned_pending_response() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    request = transport.decoded_publications(REQUEST_TOPIC)[0]
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": request["task_id"],
            "status": "PENDING",
            "payload": {"doubled": 4},
            "_response_type": "full",
        },
    )

    result = await pending
    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.PENDING
    assert result.data == Result(doubled=4)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        ("PENDING", {"ack": True, "message": "Task started"}),
        ("SUCCESS", {"ack": "true", "message": "Task started"}),
        ("SUCCESS", {"ack": True, "message": "unexpected"}),
    ],
)
@pytest.mark.asyncio
async def test_ack_dispatch_rejects_noncanonical_response(
    status: str,
    payload: dict[str, object],
) -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=None,
            timeout=1,
            mode=TaskMode.ACKNOWLEDGMENT,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    request = transport.decoded_publications(REQUEST_TOPIC)[0]
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": request["task_id"],
            "status": status,
            "payload": payload,
            "_response_type": "ack",
        },
    )

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == request["task_id"]
    assert transport.subscriptions == {}


@pytest.mark.asyncio
async def test_future_response_status_becomes_typed_unknown() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    request = transport.decoded_publications(REQUEST_TOPIC)[0]
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": request["task_id"],
            "status": "FUTURE_BROKER_STATUS",
            "payload": {"future_shape": True},
            "_response_type": "full",
            "future_field": {"also": "ignored"},
        },
    )

    result = await pending
    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.UNKNOWN
    assert result.data == {"future_shape": True}
    assert result.error_message is None
    assert result.completed_at == FIXED_NOW


@pytest.mark.asyncio
async def test_request_envelope_ignores_unknown_fields() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    handled = asyncio.Event()

    async def handle(request: Request) -> Result:
        handled.set()
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": "local",
            "task_id": "future-envelope",
            "_response_mode": "complete",
            "payload": {"value": 3},
            "future_envelope_field": ["ignored"],
        },
    )

    await asyncio.wait_for(handled.wait(), timeout=1)
    await transport.wait_for_publish(RESPONSE_TOPIC)
    assert transport.decoded_publications(RESPONSE_TOPIC)[0]["status"] == "SUCCESS"
    await tasks.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_uses_bounded_cached_response() -> None:
    transport = FakeTransport()
    tasks = service(transport, duplicate_cache_size=1)
    calls: list[int] = []

    async def handle(request: Request) -> Result:
        calls.append(request.value)
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )

    def request(task_id: str, value: int) -> dict[str, Any]:
        return {
            "source": "local",
            "task_id": task_id,
            "_response_mode": "complete",
            "payload": {"value": value},
        }

    await transport.emit(REQUEST_TOPIC, request("duplicate", 1))
    await transport.emit(REQUEST_TOPIC, request("duplicate", 1))
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 2)
    assert calls == [1]
    first_two = transport.decoded_publications(RESPONSE_TOPIC)
    assert first_two[0] == first_two[1]

    await transport.emit(REQUEST_TOPIC, request("other", 2))
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 3)
    await transport.emit(REQUEST_TOPIC, request("duplicate", 1))
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 4)
    assert calls == [1, 2, 1]
    assert len(tasks._delivery_cache) == 1


@pytest.mark.asyncio
async def test_acknowledgment_mode_suppresses_duplicate_received_before_ack() -> None:
    transport = BlockingResponsePublishTransport()
    tasks = service(transport)
    calls = 0
    handled = asyncio.Event()

    async def handle(_request: Request) -> None:
        nonlocal calls
        calls += 1
        handled.set()

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=None,
        handler=handle,
    )
    request = {
        "source": "local",
        "task_id": "duplicate-ack-before",
        "_response_mode": "ack",
        "payload": {"value": 1},
    }

    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(transport.response_publish_started.wait(), timeout=1)
    await transport.emit(REQUEST_TOPIC, request)
    transport.release_response_publish.set()
    await asyncio.wait_for(handled.wait(), timeout=1)
    await asyncio.sleep(0)

    responses = transport.decoded_publications(RESPONSE_TOPIC)
    assert len(responses) == 1
    assert responses[0]["_response_type"] == "ack"
    assert calls == 1
    await tasks.close()


@pytest.mark.asyncio
async def test_acknowledgment_mode_suppresses_duplicate_received_after_ack() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    calls = 0
    handled = asyncio.Event()

    async def handle(_request: Request) -> None:
        nonlocal calls
        calls += 1
        handled.set()

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=None,
        handler=handle,
    )
    request = {
        "source": "local",
        "task_id": "duplicate-ack-after",
        "_response_mode": "ack",
        "payload": {"value": 1},
    }

    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(handled.wait(), timeout=1)
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 1)
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.sleep(0)

    responses = transport.decoded_publications(RESPONSE_TOPIC)
    assert len(responses) == 1
    assert responses[0]["_response_type"] == "ack"
    assert calls == 1
    await tasks.close()


@pytest.mark.asyncio
async def test_long_running_handlers_do_not_block_other_task_deliveries() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    release = asyncio.Event()
    both_started = asyncio.Event()
    started: list[int] = []

    async def handle(request: Request) -> Result:
        started.append(request.value)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    for task_id, value in (("concurrent-1", 1), ("concurrent-2", 2)):
        await transport.emit(
            REQUEST_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "_response_mode": "complete",
                "payload": {"value": value},
            },
        )

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started == [1, 2]
    release.set()
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 2)
    await tasks.close()


@pytest.mark.asyncio
async def test_duplicate_flood_does_not_exceed_delivery_task_bound() -> None:
    transport = FakeTransport()
    tasks = service(transport, max_outstanding=3)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(request: Request) -> Result:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Result(doubled=request.value * 2)

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=Result,
        handler=handle,
    )
    repeated = {
        "source": "local",
        "task_id": "same-inflight-task",
        "_response_mode": "complete",
        "payload": {"value": 1},
    }
    for _ in range(100):
        await transport.emit(REQUEST_TOPIC, repeated)

    await asyncio.wait_for(started.wait(), timeout=1)
    assert calls == 1
    assert len(tasks._handler_tasks) <= 3
    release.set()
    await transport.wait_for_publication_count(RESPONSE_TOPIC, 3)
    await tasks.close()


@pytest.mark.asyncio
async def test_repeated_response_is_ignored_after_first_completion() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    task_id = transport.decoded_publications(REQUEST_TOPIC)[0]["task_id"]
    response = {
        "source": "local",
        "task_id": task_id,
        "status": "SUCCESS",
        "payload": {"doubled": 4},
        "_response_type": "full",
    }
    await transport.emit(RESPONSE_TOPIC, response)
    await transport.emit(RESPONSE_TOPIC, response | {"payload": {"doubled": 999}})

    result = await pending
    assert isinstance(result, TaskResult)
    assert isinstance(result.data, Result)
    assert result.data.doubled == 4
    assert tasks._pending == {}


@pytest.mark.asyncio
async def test_response_timeout_is_outcome_unknown_and_ignores_late_response() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    with pytest.raises(OutcomeUnknownError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=0,
            mode=TaskMode.COMPLETE,
        )

    task_id = transport.decoded_publications(REQUEST_TOPIC)[0]["task_id"]
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == task_id
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": task_id,
            "status": "SUCCESS",
            "payload": {"doubled": 4},
            "_response_type": "full",
        },
    )
    assert tasks._pending == {}


@pytest.mark.asyncio
async def test_disconnect_between_response_suback_and_request_publish_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    tasks = service(transport)
    original_subscribe = transport.subscribe

    async def subscribe_then_advance_generation(
        topic_filter: str,
        callback: MessageCallback,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> FakeSubscriptionHandle:
        handle = await original_subscribe(
            topic_filter,
            callback,
            restore_on_reconnect=restore_on_reconnect,
            expected_connection_generation=expected_connection_generation,
        )
        transport.disconnect()
        transport.connected = True
        transport.connection_generation += 1
        transport._connection_lost = asyncio.Event()
        return handle

    monkeypatch.setattr(transport, "subscribe", subscribe_then_advance_generation)

    with pytest.raises(DeliveryError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert type(caught.value) is DeliveryError
    assert caught.value.delivery_phase is DeliveryPhase.NOT_SENT
    assert caught.value.task_id == "task-1"
    assert transport.publish_attempts == 0
    assert transport.published == []
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_completed_response_before_puback_failure_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def respond_before_puback_failure(
        _topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        assert qos == 1
        assert expected_connection_generation == transport.connection_generation
        transport.publish_attempts += 1
        if on_send_started is not None:
            on_send_started()
        task_id = json.loads(payload)["task_id"]
        await transport.emit(
            RESPONSE_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "status": "SUCCESS",
                "payload": {"doubled": 4},
                "_response_type": "full",
            },
        )
        raise OutcomeUnknownError(
            "synthetic PUBACK uncertainty",
            delivery_phase=DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
            operation="mqtt.publish",
        )

    monkeypatch.setattr(transport, "publish", respond_before_puback_failure)

    result = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=2),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )

    assert isinstance(result, TaskResult)
    assert isinstance(result.data, Result)
    assert result.data.doubled == 4
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_response_before_send_does_not_override_not_sent_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def respond_before_not_sent_failure(
        _topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        assert qos == 1
        assert on_send_started is not None
        assert expected_connection_generation == transport.connection_generation
        transport.publish_attempts += 1
        task_id = json.loads(payload)["task_id"]
        await transport.emit(
            RESPONSE_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "status": "SUCCESS",
                "payload": {"doubled": 4},
                "_response_type": "full",
            },
        )
        raise DeliveryError(
            "synthetic pre-send failure",
            delivery_phase=DeliveryPhase.NOT_SENT,
            operation="mqtt.publish",
        )

    monkeypatch.setattr(transport, "publish", respond_before_not_sent_failure)

    with pytest.raises(DeliveryError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert type(caught.value) is DeliveryError
    assert caught.value.delivery_phase is DeliveryPhase.NOT_SENT
    assert caught.value.task_id == "task-1"
    assert transport.publish_attempts == 1
    assert transport.published == []
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_malformed_response_before_puback_failure_preserves_delivery_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def respond_malformed_before_puback_failure(
        _topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        assert qos == 1
        assert expected_connection_generation == transport.connection_generation
        transport.publish_attempts += 1
        if on_send_started is not None:
            on_send_started()
        task_id = json.loads(payload)["task_id"]
        await transport.emit(
            RESPONSE_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "status": "SUCCESS",
                "payload": {"doubled": 4},
                "_response_type": "ack",
            },
        )
        raise OutcomeUnknownError(
            "synthetic PUBACK uncertainty",
            delivery_phase=DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
            operation="mqtt.publish",
        )

    monkeypatch.setattr(transport, "publish", respond_malformed_before_puback_failure)

    with pytest.raises(OutcomeUnknownError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert caught.value.delivery_phase is DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
    assert caught.value.task_id == "task-1"
    assert caught.value.operation == "task.send"
    assert caught.value.__cause__ is None
    assert transport.publish_attempts == 1
    assert transport.published == []
    assert transport.subscriptions == {}
    assert transport.unsubscribed == [1]
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_connection_generation_loss_after_puback_is_response_pending_without_replay() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    task_id = transport.decoded_publications(REQUEST_TOPIC)[0]["task_id"]

    assert list(transport.restore_on_reconnect.values()) == [False]
    transport.disconnect()

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == task_id
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_connection_loss_waiter_failure_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    tasks = service(transport)
    loop = asyncio.get_running_loop()
    reported_contexts: list[dict[str, Any]] = []
    prior_handler = loop.get_exception_handler()

    async def fail_connection_loss_waiter(_generation: int) -> None:
        raise RuntimeError("synthetic connection-loss waiter failure")

    original_publish = transport.publish

    async def publish_after_loss_waiter_runs(
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> None:
        await original_publish(
            topic,
            payload,
            qos=qos,
            on_send_started=on_send_started,
            expected_connection_generation=expected_connection_generation,
        )
        await asyncio.sleep(0)

    monkeypatch.setattr(transport, "wait_for_connection_loss", fail_connection_loss_waiter)
    monkeypatch.setattr(transport, "publish", publish_after_loss_waiter_runs)
    loop.set_exception_handler(lambda _loop, context: reported_contexts.append(context))
    try:
        with pytest.raises(OutcomeUnknownError) as caught:
            await tasks.send(
                target_entity_id=ENTITY_ID,
                target_integration="receiver",
                command="calculate",
                request=Request(value=2),
                result_model=Result,
                timeout=1,
                mode=TaskMode.COMPLETE,
            )
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)

    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"
    assert not any(
        context.get("message") == "Task exception was never retrieved"
        for context in reported_contexts
    )
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_cancellation_after_puback_reports_response_pending_and_cleans_up() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    pending.cancel()
    with pytest.raises(OutcomeUnknownError) as caught:
        await pending

    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"
    assert pending.cancelling() == 0
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    assert tasks._reserved_task_ids == set()


@pytest.mark.asyncio
async def test_task_group_cancellation_after_puback_clears_child_cancellation() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    async def fail_after_publish() -> None:
        await transport.wait_for_publish(REQUEST_TOPIC)
        raise RuntimeError("synthetic sibling failure")

    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(
                tasks.send(
                    target_entity_id=ENTITY_ID,
                    target_integration="receiver",
                    command="calculate",
                    request=Request(value=2),
                    result_model=Result,
                    timeout=100,
                    mode=TaskMode.COMPLETE,
                )
            )
            group.create_task(fail_after_publish())

    assert any(isinstance(error, OutcomeUnknownError) for error in caught.value.exceptions)
    assert any(isinstance(error, RuntimeError) for error in caught.value.exceptions)
    assert pending.cancelling() == 0
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_asyncio_timeout_after_puback_reports_delivery_uncertainty() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    timeout = asyncio.timeout(None)

    async def expire_after_publish() -> None:
        await transport.wait_for_publish(REQUEST_TOPIC)
        timeout.reschedule(asyncio.get_running_loop().time())

    expiration = asyncio.create_task(expire_after_publish())
    current = asyncio.current_task()
    assert current is not None
    cancellation_count = current.cancelling()
    with pytest.raises(OutcomeUnknownError) as caught:
        async with timeout:
            await tasks.send(
                target_entity_id=ENTITY_ID,
                target_integration="receiver",
                command="calculate",
                request=Request(value=2),
                result_model=Result,
                timeout=100,
                mode=TaskMode.COMPLETE,
            )
    await expiration

    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert current.cancelling() == cancellation_count
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_asyncio_timeout_after_puback_preserves_existing_cancellation_count() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    current = asyncio.current_task()
    assert current is not None
    expiration: asyncio.Task[None] | None = None

    current.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        cancellation_baseline = current.cancelling()
        assert cancellation_baseline == 1

        timeout = asyncio.timeout(None)

        async def expire_after_publish() -> None:
            await transport.wait_for_publish(REQUEST_TOPIC)
            timeout.reschedule(asyncio.get_running_loop().time())

        expiration = asyncio.create_task(expire_after_publish())
        with pytest.raises(OutcomeUnknownError) as caught:
            async with timeout:
                await tasks.send(
                    target_entity_id=ENTITY_ID,
                    target_integration="receiver",
                    command="calculate",
                    request=Request(value=2),
                    result_model=Result,
                    timeout=100,
                    mode=TaskMode.COMPLETE,
                )
        await expiration

        assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
        assert current.cancelling() == cancellation_baseline
        assert transport.publish_attempts == 1
        assert transport.subscriptions == {}
        assert tasks._active_operations == 0
    finally:
        current.uncancel()
        if expiration is not None and not expiration.done():
            expiration.cancel()
            await asyncio.gather(expiration, return_exceptions=True)
        await tasks.close()


@pytest.mark.asyncio
async def test_completed_response_wins_cancellation_during_cleanup() -> None:
    transport = BlockingUnsubscribeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "source": "local",
            "task_id": "task-1",
            "status": "SUCCESS",
            "payload": {"doubled": 4},
            "_response_type": "full",
        },
    )
    await asyncio.wait_for(transport.unsubscribe_started.wait(), timeout=1)

    pending.cancel()
    transport.release_unsubscribe.set()
    result = await pending

    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.SUCCESS
    assert result.task_id == "task-1"
    assert pending.cancelling() == 0
    assert transport.subscriptions == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AuthorizationError, ResourceLimitError, ProtocolError])
async def test_negative_puback_wins_cancellation_during_cleanup(
    error_type: type[AuthorizationError | ResourceLimitError | ProtocolError],
) -> None:
    transport = BlockingUnsubscribeTransport()
    failure = error_type("synthetic broker rejection", operation="mqtt.publish")
    transport.publish_error = failure
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await asyncio.wait_for(transport.unsubscribe_started.wait(), timeout=1)

    pending.cancel()
    transport.release_unsubscribe.set()
    with pytest.raises(error_type) as caught:
        await pending

    assert caught.value is failure
    assert pending.cancelling() == 0
    assert transport.subscriptions == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AuthorizationError, ResourceLimitError, ProtocolError])
async def test_negative_puback_wins_cancellation_while_draining_loss_waiter(
    error_type: type[AuthorizationError | ResourceLimitError | ProtocolError],
) -> None:
    transport = BlockingConnectionLossCleanupTransport()
    failure = error_type("synthetic broker rejection", operation="mqtt.publish")
    transport.publish_error = failure
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )

    try:
        await asyncio.wait_for(
            transport.connection_loss_waiter_cancelled.wait(),
            timeout=1,
        )
        pending.cancel()
        transport.release_connection_loss_waiter.set()

        with pytest.raises(error_type) as caught:
            await asyncio.wait_for(asyncio.shield(pending), timeout=1)

        assert caught.value is failure
        assert pending.cancelling() == 0
        assert transport.subscriptions == {}
        assert tasks._active_operations == 0
    finally:
        transport.release_connection_loss_waiter.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await tasks.close()


@pytest.mark.asyncio
async def test_close_after_puback_reports_response_pending_and_leaves_no_task_resources() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)

    await tasks.close()

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._pending == {}
    assert tasks._setup_tasks == set()
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_close_during_qos1_send_reports_ack_pending_without_replay() -> None:
    transport = BlockingRequestPublishTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await asyncio.wait_for(transport.request_publish_started.wait(), timeout=1)

    await tasks.close()

    with pytest.raises(OutcomeUnknownError) as caught:
        await pending
    assert caught.value.delivery_phase is DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
    assert caught.value.task_id == "task-1"
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._setup_tasks == set()
    assert tasks._active_operations == 0


@pytest.mark.asyncio
async def test_cancellation_before_send_remains_ordinary_cancellation() -> None:
    transport = BlockingSubscribeTransport()
    tasks = service(transport)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await asyncio.wait_for(transport.subscribe_started.wait(), timeout=1)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert pending.cancelled()
    assert transport.publish_attempts == 0
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_outstanding_operations_are_bounded_without_queueing() -> None:
    transport = FakeTransport()
    tasks = service(transport, max_outstanding=1)
    first = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )
    )
    await transport.wait_for_publish(REQUEST_TOPIC)

    with pytest.raises(ResourceLimitError, match="maximum outstanding"):
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=100,
            mode=TaskMode.COMPLETE,
        )

    first.cancel()
    with pytest.raises(OutcomeUnknownError) as caught:
        await first
    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"


@pytest.mark.asyncio
async def test_close_cancels_background_handler_and_unsubscribes() -> None:
    transport = FakeTransport()
    tasks = service(transport)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request: Request) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=None,
        handler=handle,
    )
    acknowledgment = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=1),
        result_model=None,
        timeout=1,
        mode=TaskMode.ACKNOWLEDGMENT,
    )
    assert isinstance(acknowledgment, TaskAcknowledgement)
    await started.wait()

    await tasks.close()
    await tasks.close()
    assert cancelled.is_set()
    assert transport.subscriptions == {}
    assert tasks._handler_tasks == set()


@pytest.mark.asyncio
async def test_close_cancels_dispatch_blocked_before_suback_without_publishing() -> None:
    transport = BlockingSubscribeTransport()
    tasks = service(transport)
    sending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    await asyncio.wait_for(transport.subscribe_started.wait(), timeout=1)

    await tasks.close()

    with pytest.raises(asyncio.CancelledError):
        await sending
    assert transport.published == []
    assert transport.subscriptions == {}
    assert tasks._setup_tasks == set()


@pytest.mark.asyncio
async def test_close_cancels_registration_blocked_before_suback() -> None:
    transport = BlockingSubscribeTransport()
    tasks = service(transport)

    async def handle(request: Request) -> None:
        del request

    registering = asyncio.create_task(
        tasks.register(
            entity_id=ENTITY_ID,
            command="calculate",
            request_model=Request,
            result_model=None,
            handler=handle,
        )
    )
    await asyncio.wait_for(transport.subscribe_started.wait(), timeout=1)

    await tasks.close()

    with pytest.raises(ConnectionError, match="closed while registering"):
        await registering
    assert transport.subscriptions == {}
    assert tasks._registrations == {}
    assert tasks._setup_tasks == set()


@pytest.mark.asyncio
async def test_close_bounds_cancellation_resistant_handler_and_suppresses_final_response() -> None:
    transport = FakeTransport()
    tasks = service(transport, shutdown_timeout=0.01)
    started = asyncio.Event()
    resisted = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def handle(request: Request) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resisted.set()
            await release.wait()
        finally:
            completed.set()

    await tasks.register(
        entity_id=ENTITY_ID,
        command="calculate",
        request_model=Request,
        result_model=None,
        handler=handle,
    )
    acknowledgment = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=1),
        result_model=None,
        timeout=1,
        mode=TaskMode.ACKNOWLEDGMENT,
    )
    assert isinstance(acknowledgment, TaskAcknowledgement)
    await started.wait()
    publication_count = len(transport.published)

    with pytest.raises(TimeoutError, match="handler shutdown"):
        await tasks.close()
    await asyncio.wait_for(resisted.wait(), timeout=1)
    assert transport.subscriptions == {}

    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(transport.published) == publication_count
    assert tasks._handler_tasks == set()


@pytest.mark.asyncio
async def test_handler_signature_and_topic_segments_are_rejected() -> None:
    transport = FakeTransport()
    tasks = service(transport)

    def synchronous_handler(request: Request) -> None:
        del request

    with pytest.raises(ValidationError, match="async callable"):
        await tasks.register(
            entity_id=ENTITY_ID,
            command="calculate",
            request_model=Request,
            result_model=None,
            handler=synchronous_handler,  # type: ignore[arg-type]
        )

    async def invalid_handler() -> None:
        return None

    with pytest.raises(ValidationError, match="handler must accept"):
        await tasks.register(
            entity_id=ENTITY_ID,
            command="calculate",
            request_model=Request,
            result_model=None,
            handler=invalid_handler,
        )

    async def valid_handler(request: Request) -> None:
        return None

    with pytest.raises(ValidationError):
        await tasks.register(
            entity_id=ENTITY_ID,
            command="bad/#",
            request_model=Request,
            result_model=None,
            handler=valid_handler,
        )
    assert "topic" not in inspect.signature(tasks.send).parameters


@pytest.mark.asyncio
async def test_transport_failure_is_translated_without_raw_error_text() -> None:
    transport = FakeTransport()
    transport.publish_error = RuntimeError("secret transport detail")
    tasks = service(transport)

    with pytest.raises(Exception) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=None,
            timeout=None,
            mode=TaskMode.FIRE_AND_FORGET,
        )
    assert type(caught.value) is DeliveryError
    assert caught.value.delivery_phase is DeliveryPhase.NOT_SENT
    assert caught.value.task_id == "task-1"
    assert "secret transport detail" not in str(caught.value)
    assert "secret transport detail" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert tasks._active_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [TaskMode.FIRE_AND_FORGET, TaskMode.COMPLETE])
async def test_puback_wait_failure_preserves_task_id_and_never_replays(mode: TaskMode) -> None:
    transport = FakeTransport()
    transport.publish_error = OutcomeUnknownError(
        "synthetic PUBACK wait expiry",
        delivery_phase=DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
        operation="mqtt.publish",
    )
    tasks = service(transport)

    with pytest.raises(OutcomeUnknownError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result if mode is TaskMode.COMPLETE else None,
            timeout=1,
            mode=mode,
        )

    assert caught.value.delivery_phase is DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
    assert caught.value.task_id == "task-1"
    assert caught.value.operation == "task.send"
    assert transport.publish_attempts == 1
    assert transport.subscriptions == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_response_wait_cancellation_after_puback_is_response_pending() -> None:
    transport = FakeTransport()
    transport.publish_completion = _PublishCompletion.COMPLETED_AFTER_CANCELLATION
    tasks = service(transport)

    with pytest.raises(OutcomeUnknownError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"
    assert caught.value.operation == "task.send"
    assert transport.publish_attempts == 1
    assert len(transport.published) == 1
    assert transport.subscriptions == {}
    assert transport.unsubscribed == [1]
    assert tasks._pending == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_completed_response_wins_simultaneous_puback_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.publish_completion = _PublishCompletion.COMPLETED_AFTER_CANCELLATION
    tasks = service(transport)
    original_publish = transport.publish

    async def respond_during_publish(
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None:
        completion = await original_publish(
            topic,
            payload,
            qos=qos,
            on_send_started=on_send_started,
            expected_connection_generation=expected_connection_generation,
        )
        task_id = json.loads(payload)["task_id"]
        await transport.emit(
            RESPONSE_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "status": "SUCCESS",
                "payload": {"doubled": 4},
                "_response_type": "full",
            },
        )
        return completion

    monkeypatch.setattr(transport, "publish", respond_during_publish)

    result = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=2),
        result_model=Result,
        timeout=1,
        mode=TaskMode.COMPLETE,
    )

    assert isinstance(result, TaskResult)
    assert result.status is TaskStatus.SUCCESS
    assert result.task_id == "task-1"
    assert isinstance(result.data, Result)
    assert result.data.doubled == 4
    assert transport.publish_attempts == 1
    assert len(transport.published) == 1
    assert transport.subscriptions == {}
    assert transport.unsubscribed == [1]
    assert tasks._pending == {}
    assert tasks._reserved_task_ids == set()
    assert tasks._delivery_phases == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_malformed_response_during_puback_cancellation_remains_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    transport.publish_completion = _PublishCompletion.COMPLETED_AFTER_CANCELLATION
    tasks = service(transport)
    original_publish = transport.publish

    async def respond_during_publish(
        topic: str,
        payload: bytes,
        *,
        qos: int,
        on_send_started: Callable[[], None] | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion | None:
        completion = await original_publish(
            topic,
            payload,
            qos=qos,
            on_send_started=on_send_started,
            expected_connection_generation=expected_connection_generation,
        )
        task_id = json.loads(payload)["task_id"]
        await transport.emit(
            RESPONSE_TOPIC,
            {
                "source": "local",
                "task_id": task_id,
                "status": "SUCCESS",
                "payload": {"doubled": 4},
                "_response_type": "ack",
            },
        )
        return completion

    monkeypatch.setattr(transport, "publish", respond_during_publish)

    with pytest.raises(OutcomeUnknownError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=2),
            result_model=Result,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert caught.value.delivery_phase is DeliveryPhase.RESPONSE_PENDING
    assert caught.value.task_id == "task-1"
    assert transport.publish_attempts == 1
    assert len(transport.published) == 1
    assert transport.subscriptions == {}
    assert transport.unsubscribed == [1]
    assert tasks._pending == {}
    assert tasks._reserved_task_ids == set()
    assert tasks._delivery_phases == {}
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_fire_and_forget_puback_wins_simultaneous_cancellation() -> None:
    transport = FakeTransport()
    transport.publish_completion = _PublishCompletion.COMPLETED_AFTER_CANCELLATION
    tasks = service(transport)

    receipt = await tasks.send(
        target_entity_id=ENTITY_ID,
        target_integration="receiver",
        command="calculate",
        request=Request(value=1),
        result_model=None,
        timeout=None,
        mode=TaskMode.FIRE_AND_FORGET,
    )

    assert receipt == DispatchReceipt(task_id="task-1", accepted_at=FIXED_NOW)
    assert transport.publish_attempts == 1
    assert tasks._active_operations == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_negative_puback_remains_definite_typed_failure_without_replay() -> None:
    transport = FakeTransport()
    denial = AuthorizationError(
        "synthetic negative PUBACK",
        operation="mqtt.publish",
    )
    transport.publish_error = denial
    tasks = service(transport)

    with pytest.raises(AuthorizationError) as caught:
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="calculate",
            request=Request(value=1),
            result_model=None,
            timeout=1,
            mode=TaskMode.FIRE_AND_FORGET,
        )

    assert caught.value is denial
    assert transport.publish_attempts == 1
    assert tasks._active_operations == 0
    await tasks.close()
