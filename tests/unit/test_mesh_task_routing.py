# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from picogrid_ecn_client._services.tasks import TaskService
from picogrid_ecn_client.exceptions import (
    ConnectionError,
    DeliveryError,
    OutcomeUnknownError,
    ValidationError,
)
from picogrid_ecn_client.models import (
    DeliveryPhase,
    TaskMode,
    TaskRequestContext,
    TaskResult,
)

ENTITY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SOURCE_TERMINAL_ID = UUID("11111111-1111-4111-8111-111111111111")
TARGET_TERMINAL_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_SOURCE_TERMINAL_ID = UUID("33333333-3333-4333-8333-333333333333")
REQUEST_TOPIC = f"task/receiver/{ENTITY_ID}/echo"
ROUTED_REQUEST_TOPIC = f"{TARGET_TERMINAL_ID}/{REQUEST_TOPIC}"
RESPONSE_TOPIC = f"{REQUEST_TOPIC}/response"
ROUTED_RESPONSE_TOPIC = f"{SOURCE_TERMINAL_ID}/{RESPONSE_TOPIC}"
Callback = Callable[[str, bytes], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RecordingSubscriptionHandle:
    token: int
    connection_generation: int


class Echo(BaseModel):
    message: str


class RecordingTransport:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []
        self.subscriptions: dict[RecordingSubscriptionHandle, tuple[str, Callback]] = {}
        self.restore_on_reconnect: dict[RecordingSubscriptionHandle, bool] = {}
        self._next_token = 0
        self.published_event = asyncio.Event()
        self.fail_next_publication = False
        self.fail_next_publication_after_send_started = False
        self.block_next_publication_after_send_started = False
        self.publication_send_started = asyncio.Event()
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
    ) -> None:
        if not self.connected or (
            expected_connection_generation is not None
            and expected_connection_generation != self.connection_generation
        ):
            raise DeliveryError(
                "synthetic generation is unavailable",
                delivery_phase=DeliveryPhase.NOT_SENT,
                operation="mqtt.publish",
            )
        if self.fail_next_publication:
            self.fail_next_publication = False
            raise RuntimeError("synthetic publication failure")
        if self.fail_next_publication_after_send_started:
            self.fail_next_publication_after_send_started = False
            if on_send_started is not None:
                on_send_started()
            # Model a QoS 1 response that may have reached the broker even though
            # its PUBACK was not observed locally.
            self.published.append((topic, payload, qos))
            self.published_event.set()
            raise OutcomeUnknownError(
                "synthetic publication acknowledgment was not observed",
                delivery_phase=DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
                operation="mqtt.publish",
            )
        if self.block_next_publication_after_send_started:
            self.block_next_publication_after_send_started = False
            if on_send_started is not None:
                on_send_started()
            self.published.append((topic, payload, qos))
            self.published_event.set()
            self.publication_send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling() == 1:
                    current.uncancel()
                raise OutcomeUnknownError(
                    "synthetic publication was interrupted after send started",
                    delivery_phase=DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING,
                    operation="mqtt.publish",
                ) from None
        if on_send_started is not None:
            on_send_started()
        self.published.append((topic, payload, qos))
        self.published_event.set()
        for topic_filter, callback in tuple(self.subscriptions.values()):
            if topic_filter == topic:
                await callback(topic, payload)

    async def subscribe(
        self,
        topic_filter: str,
        callback: Callback,
        *,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> RecordingSubscriptionHandle:
        if not self.connected or (
            expected_connection_generation is not None
            and expected_connection_generation != self.connection_generation
        ):
            raise ConnectionError(
                "synthetic generation is unavailable",
                operation="mqtt.subscribe",
            )
        self._next_token += 1
        handle = RecordingSubscriptionHandle(self._next_token, self.connection_generation)
        self.subscriptions[handle] = (topic_filter, callback)
        self.restore_on_reconnect[handle] = restore_on_reconnect
        return handle

    async def unsubscribe(self, token: object) -> None:
        assert isinstance(token, RecordingSubscriptionHandle)
        self.subscriptions.pop(token, None)
        self.restore_on_reconnect.pop(token, None)

    async def wait_for_connection_loss(self, generation: int) -> None:
        if not self.connected or generation != self.connection_generation:
            return
        await self._connection_lost.wait()

    async def emit(self, topic: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        for topic_filter, callback in tuple(self.subscriptions.values()):
            if topic_filter == topic:
                await callback(topic, encoded)

    def decoded(self, topic: str) -> list[dict[str, Any]]:
        return [
            json.loads(payload)
            for published_topic, payload, _qos in self.published
            if published_topic == topic
        ]


def service(
    transport: RecordingTransport,
    terminal_id: UUID | None,
    *,
    duplicate_cache_size: int = 1024,
) -> TaskService:
    return TaskService(
        transport,
        integration_name="receiver",
        terminal_id=terminal_id,
        duplicate_cache_size=duplicate_cache_size,
        clock=lambda: datetime(2026, 8, 7, tzinfo=UTC),
        task_id_factory=lambda: "mesh-task",
    )


@pytest.mark.asyncio
async def test_terminal_addressed_sender_serializes_source_and_matches_target_response() -> None:
    transport = RecordingTransport()
    tasks = service(transport, SOURCE_TERMINAL_ID)
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            target_terminal_id=TARGET_TERMINAL_ID,
            command="echo",
            request=Echo(message="synthetic"),
            result_model=Echo,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )

    await asyncio.wait_for(transport.published_event.wait(), timeout=1)
    request = transport.decoded(ROUTED_REQUEST_TOPIC)[0]
    assert request["source"] == str(SOURCE_TERMINAL_ID)
    assert {topic for topic, _callback in transport.subscriptions.values()} == {RESPONSE_TOPIC}
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "status": "SUCCESS",
            "source": str(SOURCE_TERMINAL_ID),
            "task_id": request["task_id"],
            "payload": {"message": "wrong responder"},
            "_response_type": "full",
        },
    )
    await asyncio.sleep(0)
    assert not pending.done()
    await transport.emit(
        RESPONSE_TOPIC,
        {
            "status": "SUCCESS",
            "source": str(TARGET_TERMINAL_ID),
            "task_id": request["task_id"],
            "payload": {"message": "synthetic"},
            "_response_type": "full",
        },
    )

    result = await pending
    assert isinstance(result, TaskResult)
    assert result.data == Echo(message="synthetic")
    assert transport.subscriptions == {}
    await tasks.close()


@pytest.mark.asyncio
async def test_remote_ack_is_routed_once_and_duplicate_completion_stays_silent() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    contexts: list[TaskRequestContext] = []

    async def handle(context: TaskRequestContext, request: Echo) -> Echo:
        contexts.append(context)
        handler_started.set()
        await release_handler.wait()
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    request = {
        "source": str(SOURCE_TERMINAL_ID),
        "task_id": "remote-ack-task",
        "_response_mode": "ack",
        "payload": {"message": "synthetic"},
    }
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await transport.emit(REQUEST_TOPIC, request)

    responses = transport.decoded(ROUTED_RESPONSE_TOPIC)
    assert responses == [
        {
            "status": "SUCCESS",
            "source": str(TARGET_TERMINAL_ID),
            "task_id": "remote-ack-task",
            "payload": {"ack": True, "message": "Task started"},
            "_response_type": "ack",
        }
    ]
    assert [context.source for context in contexts] == [SOURCE_TERMINAL_ID]

    release_handler.set()
    await asyncio.sleep(0)
    assert transport.decoded(ROUTED_RESPONSE_TOPIC) == responses
    await tasks.close()


@pytest.mark.asyncio
async def test_active_ack_deduplication_survives_cache_pressure() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID, duplicate_cache_size=1)
    release_handlers = asyncio.Event()
    both_started = asyncio.Event()
    handled_task_ids: list[str] = []

    async def handle(context: TaskRequestContext, request: Echo) -> Echo:
        handled_task_ids.append(context.task_id)
        if len(handled_task_ids) == 2:
            both_started.set()
        await release_handlers.wait()
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )

    def request(task_id: str) -> dict[str, Any]:
        return {
            "source": str(SOURCE_TERMINAL_ID),
            "task_id": task_id,
            "_response_mode": "ack",
            "payload": {"message": task_id},
        }

    await transport.emit(REQUEST_TOPIC, request("ack-one"))
    await transport.emit(REQUEST_TOPIC, request("ack-two"))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    await transport.emit(REQUEST_TOPIC, request("ack-one"))
    await asyncio.sleep(0)

    responses = transport.decoded(ROUTED_RESPONSE_TOPIC)
    assert [response["task_id"] for response in responses] == ["ack-one", "ack-two"]
    assert handled_task_ids == ["ack-one", "ack-two"]

    release_handlers.set()
    await asyncio.sleep(0)
    await tasks.close()


@pytest.mark.asyncio
async def test_failed_ack_publication_allows_one_redelivery_effect() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    handled = asyncio.Event()
    handler_calls = 0

    async def handle(request: Echo) -> Echo:
        nonlocal handler_calls
        handler_calls += 1
        handled.set()
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    request = {
        "source": str(SOURCE_TERMINAL_ID),
        "task_id": "redelivered-ack-task",
        "_response_mode": "ack",
        "payload": {"message": "synthetic"},
    }

    transport.fail_next_publication = True
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.sleep(0)
    assert transport.decoded(ROUTED_RESPONSE_TOPIC) == []
    assert handler_calls == 0

    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(handled.wait(), timeout=1)
    responses = transport.decoded(ROUTED_RESPONSE_TOPIC)
    assert [response["task_id"] for response in responses] == ["redelivered-ack-task"]
    assert handler_calls == 1
    await tasks.close()


@pytest.mark.asyncio
async def test_uncertain_ack_publication_invokes_once_without_response_replay() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_finished = asyncio.Event()
    handler_calls = 0

    async def handle(request: Echo) -> Echo:
        nonlocal handler_calls
        handler_calls += 1
        handler_started.set()
        await release_handler.wait()
        handler_finished.set()
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    request = {
        "source": str(SOURCE_TERMINAL_ID),
        "task_id": "uncertain-ack-task",
        "_response_mode": "ack",
        "payload": {"message": "synthetic"},
    }

    transport.fail_next_publication_after_send_started = True
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    # A duplicate while the effect is active must neither republish the uncertain
    # ACK nor invoke the handler again.
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.sleep(0)
    assert handler_calls == 1
    assert len(transport.decoded(ROUTED_RESPONSE_TOPIC)) == 1

    release_handler.set()
    await asyncio.wait_for(handler_finished.wait(), timeout=1)
    await asyncio.sleep(0)

    # The completed tombstone also suppresses later redeliveries. There is no
    # unsolicited final response and no retry of the uncertain ACK.
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.sleep(0)
    responses = transport.decoded(ROUTED_RESPONSE_TOPIC)
    assert len(responses) == 1
    assert responses[0]["_response_type"] == "ack"
    assert responses[0]["task_id"] == "uncertain-ack-task"
    assert handler_calls == 1
    assert len(tasks._delivery_cache) == 1

    await tasks.close()
    assert transport.subscriptions == {}
    assert tasks._handler_tasks == set()


@pytest.mark.asyncio
async def test_close_during_uncertain_ack_send_does_not_start_handler() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    handler_started = asyncio.Event()

    async def handle(request: Echo) -> Echo:
        handler_started.set()
        await asyncio.Event().wait()
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    request = {
        "source": str(SOURCE_TERMINAL_ID),
        "task_id": "closing-ack-task",
        "_response_mode": "ack",
        "payload": {"message": "synthetic"},
    }

    transport.block_next_publication_after_send_started = True
    await transport.emit(REQUEST_TOPIC, request)
    await asyncio.wait_for(transport.publication_send_started.wait(), timeout=1)

    await tasks.close()
    await asyncio.sleep(0)

    responses = transport.decoded(ROUTED_RESPONSE_TOPIC)
    assert len(responses) == 1
    assert responses[0]["_response_type"] == "ack"
    assert responses[0]["task_id"] == "closing-ack-task"
    assert not handler_started.is_set()
    assert transport.subscriptions == {}
    assert tasks._handler_tasks == set()
    assert tasks._deliveries == {}
    assert tasks._delivery_cache == {}


@pytest.mark.asyncio
async def test_configured_terminal_identity_preserves_unprefixed_implicit_same_ecn_exchange() -> (
    None
):
    transport = RecordingTransport()
    tasks = service(transport, SOURCE_TERMINAL_ID)

    async def echo(request: Echo) -> Echo:
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=echo,
    )
    pending = asyncio.create_task(
        tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            command="echo",
            request=Echo(message="same ECN"),
            result_model=Echo,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )
    )
    for _ in range(20):
        if transport.decoded(RESPONSE_TOPIC):
            break
        await asyncio.sleep(0)

    result = await pending
    assert isinstance(result, TaskResult)
    assert transport.decoded(REQUEST_TOPIC)[0]["source"] == str(SOURCE_TERMINAL_ID)
    assert transport.decoded(RESPONSE_TOPIC)[0]["source"] == str(SOURCE_TERMINAL_ID)
    await tasks.close()


@pytest.mark.asyncio
async def test_explicit_self_target_is_rejected_before_wire_activity() -> None:
    transport = RecordingTransport()
    tasks = service(transport, SOURCE_TERMINAL_ID)

    with pytest.raises(ValidationError, match="different terminal"):
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            target_terminal_id=SOURCE_TERMINAL_ID,
            command="echo",
            request=Echo(message="explicit self target"),
            result_model=Echo,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert transport.published == []
    assert transport.subscriptions == {}
    await tasks.close()


@pytest.mark.asyncio
async def test_remote_handler_routes_response_to_source_and_exposes_uuid_context() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    contexts: list[TaskRequestContext] = []

    async def handle(context: TaskRequestContext, request: Echo) -> Echo:
        contexts.append(context)
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": str(SOURCE_TERMINAL_ID),
            "task_id": "remote-task",
            "_response_mode": "complete",
            "payload": {"message": "synthetic"},
        },
    )
    for _ in range(20):
        if transport.decoded(ROUTED_RESPONSE_TOPIC):
            break
        await asyncio.sleep(0)

    assert contexts[0].source == SOURCE_TERMINAL_ID
    assert transport.decoded(ROUTED_RESPONSE_TOPIC) == [
        {
            "status": "SUCCESS",
            "source": str(TARGET_TERMINAL_ID),
            "task_id": "remote-task",
            "payload": {"message": "synthetic"},
            "_response_type": "full",
        }
    ]
    await tasks.close()


@pytest.mark.asyncio
async def test_remote_request_without_configured_terminal_identity_is_ignored() -> None:
    transport = RecordingTransport()
    tasks = service(transport, None)
    called = False

    async def handle(request: Echo) -> Echo:
        nonlocal called
        called = True
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": str(SOURCE_TERMINAL_ID),
            "task_id": "unsupported-remote-task",
            "_response_mode": "complete",
            "payload": {"message": "synthetic"},
        },
    )
    await asyncio.sleep(0)

    assert called is False
    assert transport.published == []
    await tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "not-a-terminal",
        str(ENTITY_ID).upper(),
        f" {SOURCE_TERMINAL_ID}",
    ],
)
async def test_configured_handler_ignores_noncanonical_terminal_source(source: str) -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    called = False

    async def handle(request: Echo) -> Echo:
        nonlocal called
        called = True
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    await transport.emit(
        REQUEST_TOPIC,
        {
            "source": source,
            "task_id": "invalid-source",
            "_response_mode": "complete",
            "payload": {"message": "synthetic"},
        },
    )
    await asyncio.sleep(0)

    assert called is False
    assert transport.published == []
    await tasks.close()


@pytest.mark.asyncio
async def test_duplicate_key_keeps_identical_task_ids_independent_by_source() -> None:
    transport = RecordingTransport()
    tasks = service(transport, TARGET_TERMINAL_ID)
    sources: list[UUID | str] = []

    async def handle(context: TaskRequestContext, request: Echo) -> Echo:
        sources.append(context.source)
        return request

    await tasks.register(
        entity_id=ENTITY_ID,
        command="echo",
        request_model=Echo,
        result_model=Echo,
        handler=handle,
    )
    for source in (SOURCE_TERMINAL_ID, OTHER_SOURCE_TERMINAL_ID):
        await transport.emit(
            REQUEST_TOPIC,
            {
                "source": str(source),
                "task_id": "shared-task-id",
                "_response_mode": "complete",
                "payload": {"message": str(source)},
            },
        )
        for _ in range(20):
            if transport.decoded(f"{source}/{RESPONSE_TOPIC}"):
                break
            await asyncio.sleep(0)

    assert sources == [SOURCE_TERMINAL_ID, OTHER_SOURCE_TERMINAL_ID]
    assert len(transport.decoded(f"{SOURCE_TERMINAL_ID}/{RESPONSE_TOPIC}")) == 1
    assert len(transport.decoded(f"{OTHER_SOURCE_TERMINAL_ID}/{RESPONSE_TOPIC}")) == 1
    await tasks.close()


@pytest.mark.asyncio
async def test_remote_dispatch_requires_source_terminal_identity_before_wire_activity() -> None:
    transport = RecordingTransport()
    tasks = service(transport, None)

    with pytest.raises(ValidationError, match=r"ECNConfig\.terminal_id"):
        await tasks.send(
            target_entity_id=ENTITY_ID,
            target_integration="receiver",
            target_terminal_id=TARGET_TERMINAL_ID,
            command="echo",
            request=Echo(message="synthetic"),
            result_model=Echo,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert transport.published == []
    assert transport.subscriptions == {}
    await tasks.close()


@pytest.mark.asyncio
async def test_task_entity_ids_require_uuid_objects_before_wire_activity() -> None:
    transport = RecordingTransport()
    tasks = service(transport, None)
    invalid_entity_id: Any = str(ENTITY_ID)

    async def handle(request: Echo) -> Echo:
        return request

    with pytest.raises(ValidationError, match="entity_id"):
        await tasks.register(
            entity_id=invalid_entity_id,
            command="echo",
            request_model=Echo,
            result_model=Echo,
            handler=handle,
        )
    with pytest.raises(ValidationError, match="target_entity_id"):
        await tasks.send(
            target_entity_id=invalid_entity_id,
            target_integration="receiver",
            command="echo",
            request=Echo(message="synthetic"),
            result_model=Echo,
            timeout=1,
            mode=TaskMode.COMPLETE,
        )

    assert transport.published == []
    assert transport.subscriptions == {}
    await tasks.close()
