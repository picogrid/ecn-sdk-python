# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import aiomqtt
import pytest

import picogrid_ecn_client._entity_locations as entity_locations_module
from picogrid_ecn_client import (
    AuthorizationError,
    DeliveryPolicy,
    Entity,
    EntityCategory,
    EntityEvent,
    Location,
    LocationEvent,
    NotReadyError,
    PublicationKind,
    ResourceLimitError,
    ValidationError,
    WireFormat,
)
from picogrid_ecn_client._entity_locations import EntityLocationService
from picogrid_ecn_client._protocol import (
    build_entity_protobuf_topic,
    build_entity_topic,
    decode_entity_payload,
    encode_entity_payload,
)

MessageCallback = Callable[[str, bytes], Awaitable[None]]
RestoreFailure = AuthorizationError | ResourceLimitError
RestoreFailureCallback = Callable[[RestoreFailure], Awaitable[None] | None]
TRACK_ENTITY_ID = UUID(int=1)


class FakeTransport:
    def __init__(self) -> None:
        self.callbacks: dict[object, tuple[str, MessageCallback]] = {}
        self.restore_failure_callbacks: dict[object, RestoreFailureCallback] = {}
        self.published: list[tuple[str, bytes, int]] = []
        self.unsubscribe_attempts: list[object] = []

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object:
        token = object()
        self.callbacks[token] = (topic_filter, callback)
        if on_restore_failure is not None:
            self.restore_failure_callbacks[token] = on_restore_failure
        return token

    async def unsubscribe(self, subscription: object) -> None:
        self.unsubscribe_attempts.append(subscription)
        self.callbacks.pop(subscription, None)
        self.restore_failure_callbacks.pop(subscription, None)

    async def publish(self, topic: str, payload: bytes, qos: int) -> None:
        self.published.append((topic, payload, qos))

    async def reject_restored_filter(
        self,
        topic_filter: str,
        error: RestoreFailure,
    ) -> None:
        await self.reject_restored_filters({topic_filter}, error)

    async def reject_restored_filters(
        self,
        topic_filters: set[str],
        error: RestoreFailure,
    ) -> None:
        rejected: list[RestoreFailureCallback] = []
        for token, (pattern, _callback) in tuple(self.callbacks.items()):
            if pattern not in topic_filters:
                continue
            self.callbacks.pop(token)
            callback = self.restore_failure_callbacks.pop(token)
            rejected.append(callback)
        for callback in rejected:
            result = callback(error)
            if result is not None:
                await result

    async def deliver(self, topic_filter: str, topic: str, payload: bytes) -> None:
        del topic_filter
        seen_callbacks: set[int] = set()
        for pattern, callback in tuple(self.callbacks.values()):
            callback_id = id(callback)
            if callback_id not in seen_callbacks and aiomqtt.Topic(topic).matches(pattern):
                seen_callbacks.add(callback_id)
                await callback(topic, payload)


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
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object:
        self.subscribe_started.set()
        await self.release_subscribe.wait()
        return await super().subscribe(
            topic_filter,
            callback,
            on_restore_failure=on_restore_failure,
        )


class DeliverBetweenEntitySubscriptionsTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.subscribe_calls = 0

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object:
        self.subscribe_calls += 1
        if self.subscribe_calls == 2:
            await self.deliver(
                "unused",
                f"entity/demo/{TRACK_ENTITY_ID}/track",
                b"{}",
            )
        return await super().subscribe(
            topic_filter,
            callback,
            on_restore_failure=on_restore_failure,
        )


class RejectSecondEntitySubscriptionTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.subscribe_calls = 0

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object:
        self.subscribe_calls += 1
        if self.subscribe_calls == 2:
            raise AuthorizationError(
                "synthetic broker rejection",
                operation="mqtt.subscribe",
            )
        return await super().subscribe(
            topic_filter,
            callback,
            on_restore_failure=on_restore_failure,
        )


class BlockingUnsubscribeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.unsubscribe_started = asyncio.Event()
        self.release_unsubscribe = asyncio.Event()
        self.unsubscribe_calls = 0

    async def unsubscribe(self, subscription: object) -> None:
        self.unsubscribe_calls += 1
        if self.unsubscribe_calls == 1:
            self.unsubscribe_started.set()
            await self.release_unsubscribe.wait()
        await super().unsubscribe(subscription)


class FailingUnsubscribeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.unsubscribe_calls = 0

    async def unsubscribe(self, subscription: object) -> None:
        del subscription
        self.unsubscribe_calls += 1
        raise AuthorizationError(
            "synthetic broker rejection",
            operation="mqtt.unsubscribe",
        )


def make_service(transport: FakeTransport) -> EntityLocationService:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entity_id = UUID(int=1)

    def decode_entity(topic: str, payload: bytes) -> EntityEvent:
        del topic, payload
        entity = Entity(
            id=entity_id,
            category=EntityCategory.TRACK,
            integration="demo",
            recorded_at=now,
            type="synthetic",
        )
        return EntityEvent(timestamp=now, entity=entity)

    def decode_location(topic: str, payload: bytes) -> LocationEvent:
        del topic, payload
        location = Location(latitude=1, longitude=2, recorded_at=now)
        return LocationEvent(
            entity_id=entity_id,
            integration="demo",
            timestamp=now,
            location=location,
        )

    return EntityLocationService(
        transport,
        integration_name="demo",
        default_buffer_size=2,
        maximum_payload_size=1024 * 1024,
        decode_entity=decode_entity,
        decode_location=decode_location,
        encode_entity=lambda entity: (f"entity/demo/{entity.id}/track", b"{}", 0),
        encode_location=lambda entity_id, integration, location: (
            f"entity_location/{integration}/{entity_id}",
            b"{}",
            1,
        ),
        clock=lambda: now,
    )


def make_track_service(
    transport: FakeTransport,
    *,
    pending_limit: int = 256,
) -> tuple[EntityLocationService, list[int]]:
    decode_calls = [0]

    def decode_entity(topic: str, payload: bytes) -> EntityEvent:
        decode_calls[0] += 1
        return decode_entity_payload(topic, payload, 1024 * 1024)

    return (
        EntityLocationService(
            transport,
            integration_name="demo",
            default_buffer_size=pending_limit,
            maximum_payload_size=1024 * 1024,
            decode_entity=decode_entity,
            decode_location=lambda _topic, _payload: None,
            encode_entity=lambda entity: (
                build_entity_topic("demo", entity.id, entity.category),
                b"{}",
                0,
            ),
            encode_location=lambda entity_id, integration, location: (
                f"entity_location/{integration}/{entity_id}",
                b"{}",
                1,
            ),
        ),
        decode_calls,
    )


def track_message(
    wire_format: WireFormat,
    sequence: int,
    *,
    entity_id: UUID = TRACK_ENTITY_ID,
) -> tuple[str, bytes]:
    entity = Entity(
        id=entity_id,
        category=EntityCategory.TRACK,
        integration="demo",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=sequence),
        type=f"synthetic-{sequence}",
    )
    if wire_format is WireFormat.PROTOBUF:
        topic = build_entity_protobuf_topic(entity.integration, entity.category)
    else:
        topic = build_entity_topic(entity.integration, entity.id, entity.category)
    return topic, encode_entity_payload(entity, wire_format, 1024 * 1024)


@pytest.mark.asyncio
async def test_filters_and_delivers_typed_events() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    assert not transport.callbacks
    matching = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=0,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    excluded = await service.watch_entities(
        categories=frozenset({EntityCategory.DETECTION}),
        integrations=frozenset(),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )

    await transport.deliver("unused", f"entity/demo/{UUID(int=1)}/track", b"{}")

    assert (await anext(matching)).entity.category is EntityCategory.TRACK
    assert excluded._queue.empty()
    await service.close()
    assert matching.closed and excluded.closed
    assert not transport.callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entity", "location"])
async def test_watcher_counts_rejected_inbound_payloads(kind: str) -> None:
    transport = FakeTransport()
    service = make_service(transport)
    entity_id = UUID(int=1)

    def reject(_topic: str, _payload: bytes) -> EntityEvent | LocationEvent:
        raise ValidationError("synthetic invalid payload", operation="decode")

    if kind == "entity":
        service._decode_entity = reject  # type: ignore[assignment]
        stream = await service.watch_entities(
            categories=frozenset({EntityCategory.TRACK}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )
        topic = f"entity/demo/{entity_id}/track"
    else:
        service._decode_location = reject  # type: ignore[assignment]
        stream = await service.watch_locations(
            entity_ids=frozenset({entity_id}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )
        topic = f"entity_location/demo/{entity_id}"

    await transport.deliver("unused", topic, b"invalid")

    assert stream.decode_error_count == 1
    await stream.aclose()


@pytest.mark.asyncio
async def test_location_watch_is_exact_and_updates_only_local_observed_state() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    entity_id = UUID(int=1)
    assert service.last_observed_location(entity_id, integration=None) is None

    stream = await service.watch_locations(
        entity_ids=frozenset({entity_id}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    filters = {topic_filter for topic_filter, _callback in transport.callbacks.values()}
    assert filters == {
        f"entity_location/demo/{entity_id}",
        f"entity_location_pb/demo/{entity_id}",
    }

    await transport.deliver("unused", f"entity_location/demo/{entity_id}", b"{}")
    event = await anext(stream)
    assert service.last_observed_location(entity_id, integration=None) == event.location
    assert service.last_observed_location(entity_id, integration="demo") == event.location
    assert service.last_observed_location(entity_id, integration="other") is None
    await stream.aclose()
    assert not transport.callbacks


@pytest.mark.asyncio
async def test_entity_positions_update_integration_specific_and_most_recent_cache() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    entity_id = UUID(int=1)
    recorded_at = datetime(2026, 1, 1, tzinfo=UTC)
    first = Location(latitude=1, longitude=2, recorded_at=recorded_at)
    second = Location(latitude=3, longitude=4, recorded_at=recorded_at)
    third = Location(latitude=5, longitude=6, recorded_at=recorded_at)
    decoded = iter(
        (
            EntityEvent(
                timestamp=recorded_at,
                entity=Entity(
                    id=entity_id,
                    category=EntityCategory.TRACK,
                    integration="alpha",
                    recorded_at=recorded_at,
                    type="synthetic",
                    position=first,
                ),
            ),
            EntityEvent(
                timestamp=recorded_at,
                entity=Entity(
                    id=entity_id,
                    category=EntityCategory.TRACK,
                    integration="bravo",
                    recorded_at=recorded_at,
                    type="synthetic",
                ),
                location=second,
            ),
            EntityEvent(
                timestamp=recorded_at,
                entity=Entity(
                    id=entity_id,
                    category=EntityCategory.TRACK,
                    integration="alpha",
                    recorded_at=recorded_at,
                    type="synthetic",
                    position=third,
                ),
            ),
        )
    )
    service._decode_entity = lambda _topic, _payload: next(decoded)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset(),
        buffer_size=3,
        delivery_policy=DeliveryPolicy.FIFO,
    )

    await transport.deliver("unused", f"entity/alpha/{entity_id}/track", b"first")
    assert service.last_observed_location(entity_id, integration="alpha") == first
    assert service.last_observed_location(entity_id, integration=None) == first
    await transport.deliver("unused", f"entity/bravo/{entity_id}/track", b"second")
    assert service.last_observed_location(entity_id, integration="bravo") == second
    assert service.last_observed_location(entity_id, integration=None) == second
    await transport.deliver("unused", f"entity/alpha/{entity_id}/track", b"third")
    assert service.last_observed_location(entity_id, integration="alpha") == third
    assert service.last_observed_location(entity_id, integration="bravo") == second
    assert service.last_observed_location(entity_id, integration=None) == third

    await service.close()
    assert stream.closed
    assert service.last_observed_location(entity_id, integration=None) is None
    assert service.last_observed_location(entity_id, integration="alpha") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entity", "location"])
async def test_close_during_suback_cleans_pending_watcher(kind: str) -> None:
    transport = BlockingSubscribeTransport()
    service = make_service(transport)
    entity_id = UUID(int=1)
    if kind == "entity":
        opening = asyncio.create_task(
            service.watch_entities(
                categories=frozenset({EntityCategory.TRACK}),
                integrations=frozenset({"demo"}),
                buffer_size=1,
                delivery_policy=DeliveryPolicy.FIFO,
            )
        )
    else:
        opening = asyncio.create_task(
            service.watch_locations(
                entity_ids=frozenset({entity_id}),
                integrations=frozenset({"demo"}),
                buffer_size=1,
                delivery_policy=DeliveryPolicy.FIFO,
            )
        )

    await asyncio.wait_for(transport.subscribe_started.wait(), timeout=1)
    await service.close()
    transport.release_subscribe.set()

    with pytest.raises(NotReadyError, match="closed while opening"):
        await asyncio.wait_for(opening, timeout=1)
    assert transport.callbacks == {}
    assert service._entity_watchers == []
    assert service._location_watchers == []


@pytest.mark.asyncio
async def test_entity_watch_buffers_delivery_between_fixed_depth_subacks() -> None:
    transport = DeliverBetweenEntitySubscriptionsTransport()
    service = make_service(transport)

    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )

    assert transport.subscribe_calls == 2
    assert (await anext(stream)).entity.id == TRACK_ENTITY_ID
    await service.close()


@pytest.mark.asyncio
async def test_failed_later_entity_suback_releases_early_registration() -> None:
    transport = RejectSecondEntitySubscriptionTransport()
    service = make_service(transport)

    with pytest.raises(AuthorizationError, match="synthetic broker rejection"):
        await service.watch_entities(
            categories=frozenset({EntityCategory.TRACK}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )

    assert service._entity_watchers == []
    assert transport.callbacks == {}
    assert service._pending_tracks == {}
    assert service._pending_track_bytes == 0
    await service.close()


@pytest.mark.asyncio
async def test_failed_later_entity_suback_closes_provisional_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscribe_failure = AuthorizationError(
        "synthetic broker rejection",
        operation="mqtt.subscribe",
    )

    class RejectLaterTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.subscribe_calls = 0
            self.opened_handles: list[object] = []

        async def subscribe(
            self,
            topic_filter: str,
            callback: MessageCallback,
            *,
            on_restore_failure: RestoreFailureCallback | None = None,
        ) -> object:
            self.subscribe_calls += 1
            if self.subscribe_calls == 2:
                raise subscribe_failure
            handle = await super().subscribe(
                topic_filter,
                callback,
                on_restore_failure=on_restore_failure,
            )
            self.opened_handles.append(handle)
            return handle

    streams: list[entity_locations_module.EventStream[EntityEvent]] = []
    event_stream_type = entity_locations_module.EventStream

    def recording_event_stream(
        *,
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
        on_close: Callable[[], Awaitable[None] | None] | None,
    ) -> entity_locations_module.EventStream[EntityEvent]:
        stream = event_stream_type(
            buffer_size=buffer_size,
            delivery_policy=delivery_policy,
            on_close=on_close,
        )
        streams.append(stream)
        return stream

    monkeypatch.setattr(entity_locations_module, "EventStream", recording_event_stream)
    transport = RejectLaterTransport()
    service = make_service(transport)

    with pytest.raises(AuthorizationError) as raised:
        await service.watch_entities(
            categories=frozenset({EntityCategory.TRACK}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )

    assert raised.value is subscribe_failure
    assert streams[0].closed is True
    assert transport.subscribe_calls == 2
    assert len(transport.opened_handles) == 1
    assert transport.opened_handles[0] not in transport.callbacks
    assert transport.callbacks == {}
    assert service._entity_watchers == []
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_stream_close_finishes_all_unsubscriptions() -> None:
    transport = BlockingUnsubscribeTransport()
    service = make_service(transport)
    stream = await service.watch_locations(
        entity_ids=frozenset({UUID(int=1)}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    assert len(transport.callbacks) == 2

    closing = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(transport.unsubscribe_started.wait(), timeout=1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    transport.release_unsubscribe.set()
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert transport.callbacks == {}
    assert transport.unsubscribe_calls == 2


@pytest.mark.asyncio
async def test_explicit_stream_close_surfaces_first_error_after_all_unsubscriptions() -> None:
    transport = FailingUnsubscribeTransport()
    service = make_service(transport)
    stream = await service.watch_locations(
        entity_ids=frozenset({UUID(int=1)}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )

    with pytest.raises(AuthorizationError, match="synthetic broker rejection"):
        await stream.aclose()

    assert stream.closed
    assert transport.unsubscribe_calls == 2


@pytest.mark.asyncio
async def test_terminal_service_close_is_best_effort_after_unsubscribe_rejection() -> None:
    transport = FailingUnsubscribeTransport()
    service = make_service(transport)
    stream = await service.watch_locations(
        entity_ids=frozenset({UUID(int=1)}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )

    await service.close()

    assert stream.closed
    assert transport.unsubscribe_calls == 2


@pytest.mark.asyncio
async def test_snapshotted_callback_cannot_repopulate_location_state_after_close() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    entity_id = UUID(int=1)
    stream = await service.watch_locations(
        entity_ids=frozenset({entity_id}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    callback = next(iter(transport.callbacks.values()))[1]

    await service.close()
    await callback(f"entity_location/demo/{entity_id}", b"{}")

    assert stream.closed
    assert service.last_observed_location(entity_id, integration=None) is None


@pytest.mark.asyncio
async def test_publish_enforces_configured_integration_and_receipts() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entity = Entity(
        id=uuid4(),
        category=EntityCategory.TRACK,
        integration="demo",
        recorded_at=now,
        type="synthetic",
    )
    receipt = await service.publish_entity(entity)
    assert receipt.kind is PublicationKind.ENTITY
    assert transport.published[-1][2] == 0

    invalid = entity.model_copy(update={"integration": "other"})
    with pytest.raises(ValidationError, match="configured integration_name"):
        await service.publish_entity(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entity", "location"])
@pytest.mark.parametrize("error_type", [AuthorizationError, ResourceLimitError])
async def test_restored_subscription_failure_closes_every_owner_and_preserves_other_watchers(
    kind: str,
    error_type: type[RestoreFailure],
) -> None:
    transport = FakeTransport()
    service = make_service(transport)
    if kind == "entity":
        denied = [
            await service.watch_entities(
                categories=frozenset({EntityCategory.TRACK}),
                integrations=frozenset({"demo"}),
                buffer_size=1,
                delivery_policy=DeliveryPolicy.FIFO,
            )
            for _ in range(2)
        ]
        unaffected = await service.watch_entities(
            categories=frozenset({EntityCategory.DETECTION}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )
        rejected_filter = "entity/demo/+/track"
    else:
        denied_entity_id = UUID(int=1)
        denied = [
            await service.watch_locations(
                entity_ids=frozenset({denied_entity_id}),
                integrations=frozenset({"demo"}),
                buffer_size=1,
                delivery_policy=DeliveryPolicy.FIFO,
            )
            for _ in range(2)
        ]
        unaffected = await service.watch_locations(
            entity_ids=frozenset({UUID(int=2)}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )
        rejected_filter = f"entity_location/demo/{denied_entity_id}"
    denied_handles = set(transport.callbacks)
    error = error_type(
        "synthetic restored subscription rejection",
        operation="mqtt.restore_subscription",
    )

    await transport.reject_restored_filter(rejected_filter, error)

    for stream in denied:
        with pytest.raises(error_type) as caught:
            await anext(stream)
        assert caught.value is error
        assert stream.closed
    assert not unaffected.closed
    assert len(transport.callbacks) == 2
    denied_cleanup_calls = [
        handle for handle in transport.unsubscribe_attempts if handle in denied_handles
    ]
    assert len(denied_cleanup_calls) == 4
    assert len(set(denied_cleanup_calls)) == 4
    await service.close()
    assert transport.callbacks == {}


@pytest.mark.asyncio
async def test_multiple_restore_denials_release_one_stream_ownership_once() -> None:
    transport = FakeTransport()
    service = make_service(transport)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    handles = set(transport.callbacks)
    error = AuthorizationError(
        "synthetic restored subscription rejection",
        operation="mqtt.restore_subscription",
    )

    await transport.reject_restored_filters(
        {"entity/demo/+/track", "entity_pb/demo/track"},
        error,
    )

    with pytest.raises(AuthorizationError):
        await anext(stream)
    assert len(transport.unsubscribe_attempts) == 2
    assert set(transport.unsubscribe_attempts) == handles
    assert transport.callbacks == {}
    await service.close()


@pytest.mark.asyncio
async def test_restore_failure_during_subscription_open_cannot_resurrect_a_handle() -> None:
    error = ResourceLimitError(
        "synthetic restored subscription rejection",
        operation="mqtt.restore_subscription",
    )

    class FailDuringSubscribeTransport(FakeTransport):
        async def subscribe(
            self,
            topic_filter: str,
            callback: MessageCallback,
            *,
            on_restore_failure: RestoreFailureCallback | None = None,
        ) -> object:
            handle = await super().subscribe(
                topic_filter,
                callback,
                on_restore_failure=on_restore_failure,
            )
            assert on_restore_failure is not None
            result = on_restore_failure(error)
            if result is not None:
                await result
            return handle

    transport = FailDuringSubscribeTransport()
    service = make_service(transport)

    with pytest.raises(ResourceLimitError) as caught:
        await service.watch_entities(
            categories=frozenset({EntityCategory.TRACK}),
            integrations=frozenset({"demo"}),
            buffer_size=1,
            delivery_policy=DeliveryPolicy.FIFO,
        )

    assert caught.value is error
    assert transport.callbacks == {}
    assert len(transport.unsubscribe_attempts) == 1
    assert service._entity_watchers == []
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_format", [WireFormat.JSON, WireFormat.PROTOBUF])
async def test_latest_track_burst_decodes_only_freshest_payload(
    wire_format: WireFormat,
) -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport)
    first = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=4,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    second = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=4,
        delivery_policy=DeliveryPolicy.LATEST,
    )

    for sequence in range(200):
        topic, payload = track_message(wire_format, sequence)
        await transport.deliver("unused", topic, payload)
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert decode_calls == [1]
    assert (await anext(first)).entity.type == "synthetic-199"
    assert (await anext(second)).entity.type == "synthetic-199"
    assert first.dropped_count == second.dropped_count == 0
    assert service._latest_track_coalesced_count == 199
    assert service._latest_track_evicted_count == 0
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_format", [WireFormat.JSON, WireFormat.PROTOBUF])
async def test_fifo_watcher_disables_track_ingress_coalescing(
    wire_format: WireFormat,
) -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport)
    fifo = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=5,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    latest = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=5,
        delivery_policy=DeliveryPolicy.LATEST,
    )

    for sequence in range(5):
        topic, payload = track_message(wire_format, sequence)
        await transport.deliver("unused", topic, payload)

    assert decode_calls == [5]
    assert service._pending_track_task is None
    assert [(await anext(fifo)).entity.type for _ in range(5)] == [
        f"synthetic-{sequence}" for sequence in range(5)
    ]
    assert [(await anext(latest)).entity.type for _ in range(5)] == [
        f"synthetic-{sequence}" for sequence in range(5)
    ]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_format", [WireFormat.JSON, WireFormat.PROTOBUF])
async def test_newer_fifo_delivery_supersedes_older_pending_latest_track(
    wire_format: WireFormat,
) -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport)
    latest = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=2,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    first_topic, first_payload = track_message(wire_format, 1)
    await transport.deliver("unused", first_topic, first_payload)

    fifo = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=2,
        delivery_policy=DeliveryPolicy.FIFO,
    )
    second_topic, second_payload = track_message(wire_format, 2)
    await transport.deliver("unused", second_topic, second_payload)
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert (await anext(latest)).entity.type == "synthetic-2"
    assert latest._queue.empty()
    assert (await anext(fifo)).entity.type == "synthetic-2"
    assert decode_calls == [1]
    assert service._latest_track_coalesced_count == 1
    assert service._pending_track_bytes == 0
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_pending_limit_evicts_least_recently_updated_identity() -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport, pending_limit=2)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=2,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    messages = (
        track_message(WireFormat.JSON, 0, entity_id=UUID(int=1)),
        track_message(WireFormat.JSON, 0, entity_id=UUID(int=2)),
        track_message(WireFormat.JSON, 1, entity_id=UUID(int=1)),
        track_message(WireFormat.JSON, 0, entity_id=UUID(int=3)),
    )
    for topic, payload in messages:
        await transport.deliver("unused", topic, payload)
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert decode_calls == [2]
    assert [(await anext(stream)).entity.id for _ in range(2)] == [UUID(int=1), UUID(int=3)]
    assert service._latest_track_coalesced_count == 1
    assert service._latest_track_evicted_count == 1
    assert stream.dropped_count == 0
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_pending_bytes_account_for_replacement_and_drain() -> None:
    transport = FakeTransport()
    service, _decode_calls = make_track_service(transport, pending_limit=8)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=3,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    first_topic, first_payload = track_message(WireFormat.JSON, 1, entity_id=UUID(int=1))
    replacement_payload = first_payload + (b" " * 32)
    second_topic, second_payload = track_message(WireFormat.JSON, 1, entity_id=UUID(int=2))
    service._pending_track_byte_limit = len(replacement_payload) + len(second_payload)

    await transport.deliver("unused", first_topic, first_payload)
    assert service._pending_track_bytes == len(first_payload)
    await transport.deliver("unused", first_topic, replacement_payload)
    assert service._pending_track_bytes == len(replacement_payload)
    await transport.deliver("unused", second_topic, second_payload)
    assert service._pending_track_bytes == len(replacement_payload) + len(second_payload)
    assert service._latest_track_coalesced_count == 1
    assert service._latest_track_evicted_count == 0

    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task
    assert service._pending_track_bytes == 0
    await stream.aclose()
    await service.close()


@pytest.mark.asyncio
async def test_default_pending_track_bytes_are_bounded_below_the_count_product() -> None:
    service, _decode_calls = make_track_service(FakeTransport(), pending_limit=256)

    assert service._pending_track_byte_limit >= service._maximum_payload_size
    assert (
        service._pending_track_byte_limit
        < service._pending_track_limit * service._maximum_payload_size
    )
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_pending_byte_limit_evicts_least_recent_identity() -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport, pending_limit=8)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=3,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    messages = tuple(
        track_message(WireFormat.JSON, 1, entity_id=UUID(int=value)) for value in (1, 2, 3)
    )
    service._pending_track_byte_limit = len(messages[0][1]) + len(messages[1][1])

    for topic, payload in messages:
        await transport.deliver("unused", topic, payload)
    assert service._pending_track_bytes <= service._pending_track_byte_limit
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert decode_calls == [2]
    assert [(await anext(stream)).entity.id for _ in range(2)] == [UUID(int=2), UUID(int=3)]
    assert service._latest_track_evicted_count == 1
    assert service._pending_track_bytes == 0
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_high_cardinality_burst_stays_within_both_bounds() -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport, pending_limit=512)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=8,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    sample_topic, sample_payload = track_message(
        WireFormat.JSON,
        1,
        entity_id=UUID(int=1),
    )
    del sample_topic
    service._pending_track_byte_limit = len(sample_payload) * 8

    for value in range(1, 257):
        topic, payload = track_message(WireFormat.JSON, 1, entity_id=UUID(int=value))
        await transport.deliver("unused", topic, payload)

    assert len(service._pending_tracks) <= service._pending_track_limit
    assert service._pending_track_bytes <= service._pending_track_byte_limit
    assert service._latest_track_evicted_count >= 248
    await stream.aclose()
    assert decode_calls == [0]
    assert service._pending_tracks == {}
    assert service._pending_track_bytes == 0
    assert service._pending_track_task is None
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_large_burst_bounds_decode_work_and_delivers_freshest() -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport, pending_limit=512)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=8,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    identity_count = 256
    sequence_count = 10
    delivered_count = identity_count * sequence_count
    # Size the byte budget from a burst-shaped payload: sequence 0 carries no
    # fractional-seconds component and serialises shorter than every later one,
    # so sampling it would make the surviving count depend on payload width.
    _, sample_payload = track_message(
        WireFormat.JSON,
        sequence_count - 1,
        entity_id=UUID(int=1),
    )
    budget = 8
    service._pending_track_byte_limit = len(sample_payload) * budget

    for sequence in range(sequence_count):
        for value in range(1, identity_count + 1):
            topic, payload = track_message(
                WireFormat.JSON,
                sequence,
                entity_id=UUID(int=value),
            )
            await transport.deliver("unused", topic, payload)

    # Bounded memory: the byte budget binds well before the identity count, and
    # no payload reached the full decoder while the burst was being coalesced.
    surviving_count = len(service._pending_tracks)
    assert surviving_count == budget
    assert surviving_count < service._pending_track_limit
    assert service._pending_track_bytes <= service._pending_track_byte_limit
    assert decode_calls[0] == 0
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    # Materially reduced full-decode work: one decode per surviving identity.
    delivered = [(await anext(stream)).entity for _ in range(surviving_count)]
    assert decode_calls[0] == surviving_count
    assert decode_calls[0] * 100 <= delivered_count
    assert {entity.id for entity in delivered} == {
        UUID(int=value) for value in range(identity_count - surviving_count + 1, identity_count + 1)
    }
    assert {entity.type for entity in delivered} == {f"synthetic-{sequence_count - 1}"}
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_format", [WireFormat.JSON, WireFormat.PROTOBUF])
async def test_valid_track_after_malformed_payload_is_delivered(
    wire_format: WireFormat,
) -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    topic, payload = track_message(wire_format, 1)

    await transport.deliver("unused", topic, b"\x12")
    await transport.deliver("unused", topic, payload)
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert (await anext(stream)).entity.type == "synthetic-1"
    assert decode_calls == [1]
    # Malformed JSON is deferred because identity is in its topic; protobuf is counted
    # immediately because finding its identity requires inspecting the payload.
    assert stream.decode_error_count == (0 if wire_format is WireFormat.JSON else 1)
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_drain_continues_after_unexpected_decode_error() -> None:
    transport = FakeTransport()
    service, _decode_calls = make_track_service(transport)
    decode_entity = service._decode_entity

    def fail_first(topic: str, payload: bytes) -> EntityEvent | None:
        if str(UUID(int=1)) in topic:
            raise RuntimeError("synthetic decoder failure")
        return decode_entity(topic, payload)

    service._decode_entity = fail_first
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=2,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    for entity_id in (UUID(int=1), UUID(int=2)):
        topic, payload = track_message(WireFormat.JSON, 1, entity_id=entity_id)
        await transport.deliver("unused", topic, payload)
    pending_task = service._pending_track_task
    assert pending_task is not None
    await pending_task

    assert (await anext(stream)).entity.id == UUID(int=2)
    assert service._pending_tracks == {}
    assert stream.decode_error_count == 1
    await service.close()


@pytest.mark.asyncio
async def test_latest_track_malformed_burst_teardown_leaves_no_residue() -> None:
    transport = FakeTransport()
    service, _decode_calls = make_track_service(transport)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=4,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    messages = [
        track_message(WireFormat.JSON, 1, entity_id=UUID(int=1)),
        (track_message(WireFormat.JSON, 1, entity_id=UUID(int=2))[0], b"{"),
        track_message(WireFormat.JSON, 1, entity_id=UUID(int=3)),
        (track_message(WireFormat.JSON, 1, entity_id=UUID(int=4))[0], b"not-json"),
        track_message(WireFormat.JSON, 2, entity_id=UUID(int=2)),
    ]
    for topic, payload in messages:
        await transport.deliver("unused", topic, payload)

    assert service._pending_tracks
    assert service._pending_track_bytes > 0
    await stream.aclose()

    assert service._pending_tracks == {}
    assert service._pending_track_bytes == 0
    assert service._pending_track_task is None
    assert transport.callbacks == {}
    await service.close()


@pytest.mark.asyncio
async def test_completed_track_drain_does_not_clear_successor_task() -> None:
    service, _decode_calls = make_track_service(FakeTransport())
    drain = asyncio.create_task(service._drain_pending_tracks())
    service._pending_track_task = drain
    await asyncio.sleep(0)

    release_successor = asyncio.Event()

    async def wait_for_release() -> None:
        await release_successor.wait()

    successor = asyncio.create_task(wait_for_release())
    service._pending_track_task = successor
    await drain

    assert service._pending_track_task is successor
    release_successor.set()
    await successor
    service._pending_track_task = None
    await service.close()


@pytest.mark.asyncio
async def test_last_track_watcher_releases_pending_ingress_state() -> None:
    transport = FakeTransport()
    service, decode_calls = make_track_service(transport)
    stream = await service.watch_entities(
        categories=frozenset({EntityCategory.TRACK}),
        integrations=frozenset({"demo"}),
        buffer_size=1,
        delivery_policy=DeliveryPolicy.LATEST,
    )
    topic, payload = track_message(WireFormat.JSON, 1)
    await transport.deliver("unused", topic, payload)

    await stream.aclose()

    assert decode_calls == [0]
    assert service._pending_tracks == {}
    assert service._pending_track_bytes == 0
    assert service._pending_track_task is None
    assert transport.callbacks == {}
    await service.close()
