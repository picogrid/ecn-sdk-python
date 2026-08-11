# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from picogrid_ecn_client import (
    Affiliation,
    ClientState,
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    ConnectionStatus,
    Entity,
    EntityCategory,
    EntityEvent,
    EntityMetadata,
    Location,
    LocationEvent,
)

from operator_app.hub import BrowserHub, BrowserHubClosedError, BrowserLimitError
from operator_app.state import (
    OperatorConnectionState,
    OperatorState,
    operator_connection_state,
)


def _connection_status(
    *,
    state: ClientState,
    ready: bool = False,
    mqtt_connected: bool = False,
    retry_state: ConnectionRetryState = ConnectionRetryState.INACTIVE,
    failure_code: ConnectionFailureCode | None = None,
) -> ConnectionStatus:
    now = datetime.now(UTC)
    return ConnectionStatus(
        state=state,
        ready=ready,
        mqtt_connected=mqtt_connected,
        changed_at=now,
        next_retry_at=(
            now
            if retry_state
            in {
                ConnectionRetryState.SCHEDULED,
                ConnectionRetryState.WAITING_FOR_CREDENTIALS,
            }
            else None
        ),
        last_failure_code=failure_code,
        last_failure_operation=(
            ConnectionFailureOperation.CONNECT if failure_code is not None else None
        ),
        retry_state=retry_state,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            _connection_status(
                state=ClientState.READY,
                ready=True,
                mqtt_connected=True,
            ),
            OperatorConnectionState.READY,
        ),
        (
            _connection_status(
                state=ClientState.RECONNECTING,
                retry_state=ConnectionRetryState.CONNECTING,
                failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
            ),
            OperatorConnectionState.RECONNECTING,
        ),
        (
            _connection_status(
                state=ClientState.RECONNECTING,
                retry_state=ConnectionRetryState.SCHEDULED,
                failure_code=ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
            ),
            OperatorConnectionState.RETRY_SCHEDULED,
        ),
        (
            _connection_status(
                state=ClientState.RECONNECTING,
                retry_state=ConnectionRetryState.WAITING_FOR_CREDENTIALS,
                failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
            ),
            OperatorConnectionState.CREDENTIALS_REJECTED,
        ),
        (
            _connection_status(
                state=ClientState.RECONNECTING,
                retry_state=ConnectionRetryState.SCHEDULED,
                failure_code=ConnectionFailureCode.CONNECTION_LOST,
            ),
            OperatorConnectionState.RETRY_SCHEDULED,
        ),
        (
            _connection_status(
                state=ClientState.FAILED,
                retry_state=ConnectionRetryState.TERMINAL,
                failure_code=ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
            ),
            OperatorConnectionState.CREDENTIALS_UNAVAILABLE,
        ),
        (
            _connection_status(state=ClientState.CLOSED),
            OperatorConnectionState.DISCONNECTED,
        ),
        (
            _connection_status(
                state=ClientState.FAILED,
                retry_state=ConnectionRetryState.TERMINAL,
                failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
            ),
            OperatorConnectionState.CREDENTIALS_REJECTED,
        ),
        (
            _connection_status(
                state=ClientState.FAILED,
                retry_state=ConnectionRetryState.TERMINAL,
                failure_code=ConnectionFailureCode.SUBSCRIPTION_DENIED,
            ),
            OperatorConnectionState.SUBSCRIPTION_DENIED,
        ),
        (
            _connection_status(
                state=ClientState.FAILED,
                retry_state=ConnectionRetryState.TERMINAL,
                failure_code=ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT,
            ),
            OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED,
        ),
        (
            _connection_status(
                state=ClientState.FAILED,
                retry_state=ConnectionRetryState.TERMINAL,
                failure_code=ConnectionFailureCode.PROTOCOL_FAILURE,
            ),
            OperatorConnectionState.TERMINAL,
        ),
    ],
)
def test_connection_status_maps_to_fixed_operator_state(
    status: ConnectionStatus,
    expected: OperatorConnectionState,
) -> None:
    assert operator_connection_state(status) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [
        OperatorConnectionState.SUBSCRIPTION_DENIED,
        OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED,
    ],
)
async def test_essential_watcher_failure_overrides_an_underlying_ready_connection(
    terminal_state: OperatorConnectionState,
    fail_on_unhandled_loop_exception: None,
) -> None:
    state = OperatorState(
        maximum_entities=4,
        stale_after_seconds=30,
        diagnostic_limit=4,
        task_history_limit=4,
    )
    ready = _connection_status(
        state=ClientState.READY,
        ready=True,
        mqtt_connected=True,
    )

    await state.set_connection(ready)
    await state.set_watcher_terminal_state("entity", terminal_state)

    snapshot = await state.snapshot()
    assert snapshot.connection is not None
    assert snapshot.connection.ready
    assert snapshot.connection_summary is terminal_state

    await state.clear()
    cleared = await state.snapshot()
    assert cleared.connection_summary is None


@pytest.mark.asyncio
async def test_state_correlates_embedded_and_dedicated_locations_by_uuid(
    fail_on_unhandled_loop_exception: None,
) -> None:
    state = OperatorState(
        maximum_entities=16,
        stale_after_seconds=30,
        diagnostic_limit=16,
        task_history_limit=8,
    )
    entity_id = uuid4()
    location_only_id = uuid4()
    now = datetime.now(UTC)
    embedded = Location(latitude=1.0, longitude=2.0, recorded_at=now)
    await state.observe_entity(
        EntityEvent(
            timestamp=now,
            entity=Entity(
                id=entity_id,
                category=EntityCategory.TRACK,
                integration="mock-sensor",
                recorded_at=now,
                type="synthetic-track",
                affiliation=Affiliation.FRIEND,
                metadata=EntityMetadata(properties={"nested": {"safe": True}}),
                position=embedded,
            ),
            location=embedded,
        )
    )
    await state.observe_location(
        LocationEvent(
            entity_id=location_only_id,
            integration="mock-sensor",
            timestamp=now,
            location=Location(latitude=3.0, longitude=4.0, recorded_at=now),
        )
    )

    snapshot = await state.snapshot()

    by_id = {item.entity_id: item for item in snapshot.entities}
    assert by_id[entity_id].location is not None
    assert by_id[entity_id].location_only is False
    assert by_id[entity_id].entity_freshness == "fresh"
    assert by_id[entity_id].location_freshness == "fresh"
    assert by_id[location_only_id].location_only is True
    assert by_id[location_only_id].category is None
    assert by_id[location_only_id].entity_freshness is None
    assert by_id[location_only_id].location_freshness == "fresh"

    await state.clear()
    cleared = await state.snapshot()
    assert cleared.entities == ()
    assert cleared.connection is None


@pytest.mark.asyncio
async def test_browser_hub_bounds_clients_and_drops_oldest_update(
    fail_on_unhandled_loop_exception: None,
) -> None:
    hub = BrowserHub(maximum_clients=1, queue_size=2)
    identifier, queue = await hub.register()
    with pytest.raises(BrowserLimitError):
        await hub.register()

    await hub.broadcast("one")
    await hub.broadcast("two")
    await hub.broadcast("three")

    assert hub.dropped_messages == 1
    assert await queue.get() == "two"
    assert await queue.get() == "three"
    await hub.unregister(identifier)
    assert hub.client_count == 0


@pytest.mark.asyncio
async def test_browser_hub_close_unblocks_consumers_and_rejects_new_registration(
    fail_on_unhandled_loop_exception: None,
) -> None:
    hub = BrowserHub(maximum_clients=2, queue_size=3)
    _identifier, queue = await hub.register()
    await hub.broadcast("queued update")
    await hub.broadcast("second queued update")

    await hub.close()

    assert await asyncio.wait_for(queue.get(), timeout=1) is None
    assert queue.empty()
    assert hub.client_count == 0
    with pytest.raises(BrowserHubClosedError, match="closed"):
        await hub.register()
