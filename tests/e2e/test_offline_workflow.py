# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from picogrid_ecn_client import (
    AuthorizationError,
    ClientState,
    DeliveryPhase,
    ECNClient,
    Entity,
    EntityCategory,
    Location,
    OutcomeUnknownError,
    TaskMode,
    TaskResult,
    TimeoutError,
    WireFormat,
)
from picogrid_ecn_client.testing import NO_ACCESS_TOKEN, MockECN


class DoubleRequest(BaseModel):
    value: int


class DoubleResult(BaseModel):
    doubled: int


def _entity(entity_id: UUID, *, integration: str, category: EntityCategory) -> Entity:
    return Entity(
        id=entity_id,
        category=category,
        integration=integration,
        recorded_at=datetime.now(UTC),
        type="synthetic",
        name="Offline test entity",
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3) -> None:
    reached = asyncio.Event()
    loop = asyncio.get_running_loop()

    def check() -> None:
        if predicate():
            reached.set()
        else:
            loop.call_soon(check)

    loop.call_soon(check)
    await asyncio.wait_for(reached.wait(), timeout=timeout)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_complete_public_workflow_and_cleanup() -> None:
    async with MockECN() as mock:
        client = ECNClient(mock.client_config("offline-integration"))
        await client.start()
        report = await client.preflight()
        assert report.successful and report.ready
        assert client.is_ready

        entity_id = uuid4()
        entity_events = await client.entities.watch(categories={EntityCategory.TRACK})
        location_events = await client.locations.watch(entity_ids={entity_id})
        embedded_location = Location(
            latitude=33.0,
            longitude=-117.0,
            recorded_at=datetime.now(UTC),
            source="synthetic-entity",
        )
        entity = _entity(
            entity_id,
            integration="offline-integration",
            category=EntityCategory.TRACK,
        ).model_copy(
            update={
                "recorded_at": embedded_location.recorded_at,
                "position": embedded_location,
            }
        )
        await client.entities.publish(entity)
        assert (await asyncio.wait_for(anext(entity_events), timeout=1)).entity == entity
        assert client.locations.last_observed(entity_id) == embedded_location
        assert (
            client.locations.last_observed(
                entity_id,
                integration="offline-integration",
            )
            == embedded_location
        )

        location = Location(
            latitude=34.0,
            longitude=-118.0,
            altitude=125.0,
            recorded_at=datetime.now(UTC),
            source="synthetic-test",
        )
        await client.locations.publish(entity_id=entity_id, location=location)
        assert (await asyncio.wait_for(anext(location_events), timeout=1)).location == location
        assert client.locations.last_observed(entity_id) == location

        next_location = location.model_copy(
            update={"latitude": 35.0, "recorded_at": datetime.now(UTC)}
        )
        next_update = asyncio.create_task(
            client.locations.wait_for_update(
                entity_id,
                integration="offline-integration",
                timeout=1,
            )
        )
        exact_location_filter = f"entity_location/offline-integration/{entity_id}"
        await _wait_until(
            lambda: any(
                subscription.topic_filter == exact_location_filter
                for subscription in client._mqtt_transport._subscriptions.values()
            )
        )
        await client.wait_until_ready(timeout=1)
        await client.locations.publish(entity_id=entity_id, location=next_location)
        assert await next_update == next_location
        assert client.locations.last_observed(entity_id) == next_location
        assert exact_location_filter not in {
            subscription.topic_filter
            for subscription in client._mqtt_transport._subscriptions.values()
        }

        async def double(request: DoubleRequest) -> DoubleResult:
            return DoubleResult(doubled=request.value * 2)

        registration = await client.tasks.register(
            entity_id=entity_id,
            command="double",
            request_model=DoubleRequest,
            result_model=DoubleResult,
            handler=double,
        )
        result = await client.tasks.send(
            target_entity_id=entity_id,
            target_integration="offline-integration",
            command="double",
            request=DoubleRequest(value=21),
            result_model=DoubleResult,
            timeout=1,
        )
        assert isinstance(result, TaskResult)
        assert isinstance(result.data, DoubleResult)
        assert result.data.doubled == 42
        await client.tasks.unregister(registration)

        await entity_events.aclose()
        await location_events.aclose()
        await client.close()
        await client.close()
        assert client.status.state == ClientState.CLOSED
        assert not client.is_ready
        assert mock.active_connection_count == 0
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("picogrid-ecn-")
        ]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_start_and_preflight_publish_no_application_messages() -> None:
    async with (
        MockECN() as mock,
        ECNClient(mock.client_config("read-only-startup")) as client,
    ):
        report = await client.preflight()
        assert report.successful and report.ready
        await asyncio.sleep(0)
        assert not mock.events.message_received.is_set()


@pytest.mark.asyncio
@pytest.mark.e2e
@pytest.mark.parametrize("wire_format", [WireFormat.JSON, WireFormat.PROTOBUF])
async def test_terminal_geolocation_wait_is_uuid_free_fixed_depth_and_cleans_up(
    wire_format: WireFormat,
) -> None:
    terminal_entity_id = uuid4()
    location = Location(
        latitude=34.25,
        longitude=-117.75,
        recorded_at=datetime.now(UTC),
        source="terminal-geolocation",
    )
    async with (
        MockECN() as mock,
        ECNClient(mock.client_config("observer")) as observer,
        ECNClient(
            mock.client_config("terminal-geolocation").model_copy(
                update={"wire_format": wire_format}
            )
        ) as publisher,
    ):
        waiting = asyncio.create_task(observer.locations.wait_for_terminal_geolocation(timeout=1))
        expected_filters = {
            "entity_location/terminal-geolocation/+",
            "entity_location_pb/terminal-geolocation/+",
        }
        await _wait_until(
            lambda: (
                expected_filters
                <= {
                    subscription.topic_filter
                    for subscription in observer._mqtt_transport._subscriptions.values()
                }
            )
        )

        await publisher.locations.publish(entity_id=terminal_entity_id, location=location)

        event = await waiting
        assert event.entity_id == terminal_entity_id
        assert event.integration == "terminal-geolocation"
        assert event.location == location
        assert (
            observer.locations.last_observed(
                terminal_entity_id,
                integration="terminal-geolocation",
            )
            == location
        )
        assert expected_filters.isdisjoint(
            {
                subscription.topic_filter
                for subscription in observer._mqtt_transport._subscriptions.values()
            }
        )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_terminal_geolocation_timeout_is_typed_and_releases_filters() -> None:
    async with (
        MockECN() as mock,
        ECNClient(mock.client_config("observer")) as observer,
    ):
        with pytest.raises(TimeoutError) as raised:
            await observer.locations.wait_for_terminal_geolocation(timeout=0.01)

        assert raised.value.operation == "location.wait_for_terminal_geolocation"
        assert not observer._mqtt_transport._subscriptions


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_client_close_cancels_delayed_qos1_publication() -> None:
    mock = MockECN()
    await mock.start()
    client = ECNClient(mock.client_config("delayed-publication"))
    await client.start()
    try:
        mock.set_delay(30, operation="mqtt.publish")
        location = Location(
            latitude=34.0,
            longitude=-118.0,
            recorded_at=datetime.now(UTC),
            source="synthetic-shutdown-test",
        )
        publishing = asyncio.create_task(
            client.locations.publish(entity_id=uuid4(), location=location)
        )
        await asyncio.wait_for(mock.events.message_received.wait(), timeout=1)

        await asyncio.wait_for(client.close(), timeout=1)

        with pytest.raises(OutcomeUnknownError) as caught:
            await publishing
        assert caught.value.delivery_phase is DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
        assert caught.value.operation == "locations.publish"
        assert caught.value.operation_id is not None
        await asyncio.wait_for(mock.close(), timeout=1)
        assert mock.active_connection_count == 0
        assert mock.active_task_count == 0
    finally:
        await client.close()
        await mock.close()


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_client_close_disconnects_before_resistant_handler_timeout() -> None:
    mock = MockECN()
    await mock.start()
    config = mock.client_config("resistant-handler").model_copy(update={"shutdown_timeout": 0.05})
    client = ECNClient(config)
    await client.start()
    release = asyncio.Event()
    started = asyncio.Event()
    resisted = asyncio.Event()
    completed = asyncio.Event()

    async def handle(_request: DoubleRequest) -> None:
        started.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    resisted.set()
        finally:
            completed.set()

    try:
        await client.tasks.register(
            entity_id=UUID("00000000-0000-4000-8000-000000000701"),
            command="resist-close",
            request_model=DoubleRequest,
            result_model=None,
            handler=handle,
        )
        await client.tasks.send(
            target_entity_id=UUID("00000000-0000-4000-8000-000000000701"),
            target_integration="resistant-handler",
            command="resist-close",
            request=DoubleRequest(value=1),
            result_model=None,
            timeout=1,
            mode=TaskMode.FIRE_AND_FORGET,
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        with pytest.raises(TimeoutError, match="shutdown"):
            await asyncio.wait_for(client.close(), timeout=1)

        await asyncio.wait_for(resisted.wait(), timeout=1)
        assert not client._mqtt_transport.connected
        assert client._mqtt_transport._supervisor is None
        await _wait_until(lambda: mock.active_connection_count == 0)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name() == "picogrid-ecn-client-close"
        ]
    finally:
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        await client.close()
        await mock.close()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("picogrid-ecn-")
    ]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_broker_acl_rejects_explicit_subscription() -> None:
    async with MockECN() as mock:
        client = ECNClient(mock.client_config("unprivileged", token=NO_ACCESS_TOKEN))
        await client.start()
        with pytest.raises(AuthorizationError, match="subscription") as raised:
            await client.entities.watch(
                categories={EntityCategory.DETECTION},
                integrations={"unprivileged"},
            )
        assert raised.value.operation == "mqtt.subscribe"
        assert mock.events.authorization_denied.is_set()

        with pytest.raises(AuthorizationError, match="publication") as publish_rejection:
            await client.locations.publish(
                entity_id=UUID(int=1),
                location=Location(
                    latitude=0,
                    longitude=0,
                    recorded_at=datetime.now(UTC),
                    source="synthetic",
                ),
            )
        assert publish_rejection.value.operation == "mqtt.publish"
        assert client.is_ready
        await client.close()


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_watcher_close_surfaces_negative_unsuback_and_client_still_cleans_up() -> None:
    async with MockECN() as mock:
        client = ECNClient(mock.client_config("bounded-observer"))
        await client.start()
        stream = await client.entities.watch(
            categories={EntityCategory.TRACK},
            integrations={"bounded-observer"},
        )
        mock.events.mqtt_connected.clear()
        mock.set_authorization_failure("entity.read")

        with pytest.raises(AuthorizationError, match="unsubscription") as raised:
            await stream.aclose()

        assert raised.value.operation == "mqtt.unsubscribe"
        assert stream.closed
        assert client._mqtt_transport._subscriptions == {}
        await _wait_until(
            lambda: mock.events.mqtt_connected.is_set() and client.is_ready,
        )
        await client.close()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_task_unregister_negative_unsuback_allows_clean_reregistration() -> None:
    async with MockECN() as mock:
        client = ECNClient(mock.client_config("task-unregister"))
        await client.start()
        entity_id = uuid4()

        async def double(request: DoubleRequest) -> DoubleResult:
            return DoubleResult(doubled=request.value * 2)

        registration = await client.tasks.register(
            entity_id=entity_id,
            command="double",
            request_model=DoubleRequest,
            result_model=DoubleResult,
            handler=double,
        )
        mock.events.mqtt_connected.clear()
        mock.set_authorization_failure("task.receive")

        with pytest.raises(AuthorizationError, match="unsubscription"):
            await client.tasks.unregister(registration)

        assert not client.is_ready
        assert client.status.state is ClientState.RECONNECTING
        assert client._mqtt_transport._subscriptions == {}
        mock.set_authorization_failure("task.receive", enabled=False)
        await _wait_until(
            lambda: mock.events.mqtt_connected.is_set() and client.is_ready,
        )

        replacement = await client.tasks.register(
            entity_id=entity_id,
            command="double",
            request_model=DoubleRequest,
            result_model=DoubleResult,
            handler=double,
        )
        await client.tasks.unregister(replacement)
        await client.close()

    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_disconnect_recovers_and_restores_watchers() -> None:
    async with MockECN() as mock:
        client = ECNClient(
            mock.client_config("reconnecting").model_copy(update={"reconnect_delay": 0.05})
        )
        await client.start()
        stream = await client.entities.watch(categories={EntityCategory.TRACK})
        mock.events.mqtt_connected.clear()
        mock.events.mqtt_disconnected.clear()

        await mock.disconnect_clients()
        await asyncio.wait_for(mock.events.mqtt_disconnected.wait(), timeout=1)
        await _wait_until(lambda: client.status.state is ClientState.RECONNECTING)
        await asyncio.wait_for(mock.events.mqtt_connected.wait(), timeout=1)
        await _wait_until(lambda: client.is_ready)

        entity = _entity(
            uuid4(),
            integration="reconnecting",
            category=EntityCategory.TRACK,
        )
        await client.entities.publish(entity)
        assert (await asyncio.wait_for(anext(stream), timeout=1)).entity.id == entity.id
        await stream.aclose()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_public_protobuf_end_to_end() -> None:
    async with MockECN() as mock:
        config = mock.client_config("protobuf-client").model_copy(
            update={"wire_format": WireFormat.PROTOBUF}
        )
        async with ECNClient(config) as client:
            entity_id = uuid4()
            recorded_at = datetime.now(UTC)
            embedded_location = Location(
                latitude=33.0,
                longitude=-117.0,
                recorded_at=recorded_at,
                source="synthetic-protobuf-entity",
            )
            entity = Entity(
                id=entity_id,
                integration="protobuf-client",
                category=EntityCategory.DETECTION,
                recorded_at=recorded_at,
                type="synthetic",
                name="Offline protobuf entity",
                position=embedded_location,
            )
            entity_stream = await client.entities.watch(categories={EntityCategory.DETECTION})
            location_stream = await client.locations.watch(entity_ids={entity_id})
            await client.entities.publish(entity)
            received = await asyncio.wait_for(anext(entity_stream), timeout=1)
            assert received.entity.id == entity.id
            assert received.entity.category is EntityCategory.DETECTION
            assert client.locations.last_observed(entity_id) == embedded_location

            location = Location(
                latitude=34.0,
                longitude=-118.0,
                altitude=100.0,
                recorded_at=datetime.now(UTC),
                source="synthetic-protobuf-location",
            )
            await client.locations.publish(entity_id=entity_id, location=location)
            received_location = await asyncio.wait_for(anext(location_stream), timeout=1)
            assert received_location.location == location
            assert (
                client.locations.last_observed(
                    entity_id,
                    integration="protobuf-client",
                )
                == location
            )

            next_location = location.model_copy(
                update={"latitude": 35.0, "recorded_at": datetime.now(UTC)}
            )
            waiting = asyncio.create_task(
                client.locations.wait_for_update(
                    entity_id,
                    integration="protobuf-client",
                    timeout=1,
                )
            )
            exact_filter = f"entity_location_pb/protobuf-client/{entity_id}"
            await _wait_until(
                lambda: any(
                    subscription.topic_filter == exact_filter
                    for subscription in client._mqtt_transport._subscriptions.values()
                )
            )
            await client.locations.publish(entity_id=entity_id, location=next_location)
            assert await waiting == next_location
            assert (
                await asyncio.wait_for(anext(location_stream), timeout=1)
            ).location == next_location
            assert exact_filter not in {
                subscription.topic_filter
                for subscription in client._mqtt_transport._subscriptions.values()
            }

            await entity_stream.aclose()
            await location_stream.aclose()

        await _wait_until(lambda: mock.active_connection_count == 0)
