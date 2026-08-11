# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from picogrid_ecn_client import (
    AuthenticationError,
    AuthorizationError,
    BearerTokenAuth,
    ClientState,
    ConfigurationError,
    ConnectionError,
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    ECNClient,
    ECNConfig,
    NotReadyError,
    ProtocolError,
    ResourceLimitError,
    TLSConfig,
    ValidationError,
)
from picogrid_ecn_client import TimeoutError as ECNTimeoutError
from picogrid_ecn_client._transport.mqtt import (
    SubscriptionHandle,
    _RecoverySnapshot,
    _Subscription,
)


def _config() -> ECNConfig:
    return ECNConfig(
        host="127.0.0.1",
        mqtt_port=1883,
        integration_name="status-test",
        auth=BearerTokenAuth(token=SecretStr("synthetic")),
        tls=TLSConfig(enabled=False, verify=False),
        allow_insecure=True,
        operation_timeout=0.2,
        shutdown_timeout=0.2,
    )


@pytest.mark.asyncio
async def test_connection_events_are_latest_value_and_readiness_is_strict() -> None:
    client = ECNClient(_config())
    stream = client.connection_events()
    try:
        initial = await anext(stream)
        assert initial.state is ClientState.CREATED
        assert initial.connection_generation == 0

        client._set_state(ClientState.STARTING)
        waiter = asyncio.create_task(client.wait_until_ready(timeout=1.0))
        await asyncio.sleep(0)
        await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.CONNECTING, 1))
        await client._on_mqtt_connection_change(True)
        assert client.status.mqtt_connected
        assert not client.is_ready

        client._mqtt_transport._strict_ready = True
        await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.INACTIVE, 1))
        ready = await waiter
        assert ready.ready
        assert ready.connection_generation == 1
        assert ready.last_connected_at is not None
        assert (await anext(stream)).ready

        client._mqtt_transport._strict_ready = False
        await client._on_mqtt_connection_change(False)
        await client._on_mqtt_recovery_change(
            _RecoverySnapshot(
                ConnectionRetryState.SCHEDULED,
                1,
                next_retry_delay_seconds=0.1,
                failure_code=ConnectionFailureCode.CONNECTION_LOST,
                failure_operation=ConnectionFailureOperation.RECEIVE,
            )
        )
        reconnecting = client.status
        assert reconnecting.state is ClientState.RECONNECTING
        assert reconnecting.last_disconnected_at is not None
        assert reconnecting.next_retry_at is not None
    finally:
        await client.close()
    assert stream.closed


@pytest.mark.asyncio
async def test_status_change_time_tracks_semantics_without_duplicate_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2000, 1, 1, tzinfo=UTC)

    class SteppedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            nonlocal current
            current += timedelta(seconds=1)
            return current

    monkeypatch.setattr("picogrid_ecn_client.client.datetime", SteppedDateTime)
    client = ECNClient(_config())
    stream = client.connection_events()
    try:
        initial = await anext(stream)

        client._set_state(ClientState.CREATED)
        assert client.status == initial

        client._set_state(ClientState.STARTING)
        transitioned = await anext(stream)

        assert transitioned.state is ClientState.STARTING
        assert transitioned.changed_at != initial.changed_at

        await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.CONNECTING, 1))
        connecting = await anext(stream)

        assert connecting.state is ClientState.STARTING
        assert connecting.retry_state is ConnectionRetryState.CONNECTING
        assert connecting.consecutive_attempt_count == 1
        assert connecting.changed_at > transitioned.changed_at

        await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.CONNECTING, 1))
        assert client.status == connecting

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(stream), timeout=0.01)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_retry_status_saturates_a_finite_delay_beyond_datetime_range() -> None:
    client = ECNClient(_config())
    client._set_state(ClientState.RECONNECTING)
    try:
        await client._on_mqtt_recovery_change(
            _RecoverySnapshot(
                ConnectionRetryState.SCHEDULED,
                1,
                next_retry_delay_seconds=1e300,
                failure_code=ConnectionFailureCode.CONNECTION_LOST,
                failure_operation=ConnectionFailureOperation.RECEIVE,
            )
        )

        assert client.status.state is ClientState.RECONNECTING
        assert client.status.retry_state is ConnectionRetryState.SCHEDULED
        assert client.status.next_retry_at == datetime.max.replace(tzinfo=UTC)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [float("inf"), float("-inf"), float("nan")])
async def test_retry_status_bounds_non_finite_internal_delay(delay: float) -> None:
    client = ECNClient(_config())
    client._set_state(ClientState.RECONNECTING)
    try:
        await client._on_mqtt_recovery_change(
            _RecoverySnapshot(
                ConnectionRetryState.SCHEDULED,
                1,
                next_retry_delay_seconds=delay,
                failure_code=ConnectionFailureCode.CONNECTION_LOST,
                failure_operation=ConnectionFailureOperation.RECEIVE,
            )
        )

        assert client.status.next_retry_at == datetime.max.replace(tzinfo=UTC)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_late_transport_callbacks_cannot_resurrect_a_closed_client() -> None:
    client = ECNClient(_config())
    await client.close()
    generation = client.status.connection_generation

    await client._on_mqtt_connection_change(True)
    client._mqtt_transport._strict_ready = True
    await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.INACTIVE, 1))
    await client._on_mqtt_recovery_change(
        _RecoverySnapshot(
            ConnectionRetryState.TERMINAL,
            2,
            failure_code=ConnectionFailureCode.CONNECTION_LOST,
            failure_operation=ConnectionFailureOperation.RECEIVE,
        )
    )

    assert client.status.state is ClientState.CLOSED
    assert not client.status.ready
    assert not client.status.mqtt_connected
    assert client.status.connection_generation == generation


@pytest.mark.asyncio
async def test_close_records_disconnect_and_ignores_late_positive_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    await client._on_mqtt_connection_change(True)
    connected_at = client.status.last_connected_at
    assert connected_at is not None

    async def close_transport() -> None:
        assert client.status.state is ClientState.CLOSING
        await client._on_mqtt_connection_change(True)
        assert client.status.mqtt_connected
        await client._on_mqtt_connection_change(False)

    monkeypatch.setattr(client._mqtt_transport, "close", close_transport)
    await client.close()

    disconnected_at = client.status.last_disconnected_at
    assert client.status.state is ClientState.CLOSED
    assert not client.status.mqtt_connected
    assert disconnected_at is not None
    assert disconnected_at >= connected_at

    await client._on_mqtt_connection_change(True)
    assert client.status.state is ClientState.CLOSED
    assert not client.status.mqtt_connected
    assert client.status.last_connected_at == connected_at
    assert client.status.last_disconnected_at == disconnected_at


@pytest.mark.asyncio
async def test_wait_until_ready_maps_terminal_authentication_and_bounds_input() -> None:
    client = ECNClient(_config())
    try:
        with pytest.raises(NotReadyError):
            await client.wait_until_ready(timeout=1.0)
        with pytest.raises(ValidationError):
            await client.wait_until_ready(timeout=0.0)
        with pytest.raises(ValidationError):
            await client.wait_until_ready(timeout=10**1000)

        client._set_state(ClientState.STARTING)
        waiter = asyncio.create_task(client.wait_until_ready(timeout=1.0))
        await asyncio.sleep(0)
        await client._on_mqtt_recovery_change(
            _RecoverySnapshot(
                ConnectionRetryState.TERMINAL,
                1,
                failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
                failure_operation=ConnectionFailureOperation.CONNECT,
            )
        )
        with pytest.raises(AuthenticationError) as captured:
            await waiter
        assert captured.value.details == {
            "failure_code": ConnectionFailureCode.AUTHENTICATION_REJECTED.value
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_until_ready_uses_the_caller_deadline() -> None:
    client = ECNClient(_config())
    client._set_state(ClientState.STARTING)
    try:
        with pytest.raises(ECNTimeoutError):
            await client.wait_until_ready(timeout=0.01)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_connection_wakeups_delegate_without_creating_public_transport_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    calls: list[str] = []
    monkeypatch.setattr(
        client._mqtt_transport,
        "notify_credentials_changed",
        lambda: calls.append("credentials"),
    )
    monkeypatch.setattr(
        client._mqtt_transport,
        "request_retry",
        lambda: calls.append("retry"),
    )

    client.notify_credentials_changed()
    client.request_retry()

    assert calls == ["credentials", "retry"]
    await client.close()


@pytest.mark.asyncio
async def test_retrying_failed_start_publishes_zero_attempts_before_transport_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    terminal = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        3,
        failure_code=ConnectionFailureCode.AUTHENTICATION_REJECTED,
        failure_operation=ConnectionFailureOperation.CONNECT,
    )
    client._mqtt_transport._recovery_snapshot = terminal
    await client._on_mqtt_recovery_change(terminal)
    stream = client.connection_events()
    assert (await anext(stream)).consecutive_attempt_count == 3
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()

    async def fail_start() -> None:
        transport_started.set()
        await release_transport.wait()
        raise AuthenticationError("synthetic rejection", operation="mqtt.start")

    monkeypatch.setattr(client._mqtt_transport, "start", fail_start)
    starting = asyncio.create_task(client.start())
    await asyncio.wait_for(transport_started.wait(), timeout=1)

    snapshot = await anext(stream)
    assert snapshot.state is ClientState.STARTING
    assert snapshot.consecutive_attempt_count == 0

    release_transport.set()
    with pytest.raises(AuthenticationError):
        await starting
    await client.close()


@pytest.mark.asyncio
async def test_close_interrupts_credential_resolution_within_shutdown_bound() -> None:
    credentials_started = asyncio.Event()
    credentials_cancelled = asyncio.Event()

    async def blocked_credentials() -> tuple[str, str]:
        credentials_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            credentials_cancelled.set()
            raise

    config = _config().model_copy(
        update={
            "auth": BearerTokenAuth(credentials_provider=blocked_credentials),
            "connection_timeout": 2.0,
            "shutdown_timeout": 0.05,
        }
    )
    client = ECNClient(config)
    starting = asyncio.create_task(client.start())
    await asyncio.wait_for(credentials_started.wait(), timeout=1)

    await asyncio.wait_for(client.close(), timeout=5)
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert credentials_cancelled.is_set()
    assert client.status.state is ClientState.CLOSED
    assert client._startup_task is None
    assert not client._startup_completion_tasks
    assert client._mqtt_transport._supervisor is None


@pytest.mark.asyncio
async def test_stream_close_cancellation_preserves_component_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseProbe:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    client = ECNClient(_config())
    stream = client.connection_events()
    task_service = CloseProbe()
    entity_service = CloseProbe()
    clock_service = CloseProbe()
    transport = CloseProbe()

    async def cancel_stream_close() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(stream, "aclose", cancel_stream_close)
    monkeypatch.setattr(client, "_task_service", task_service)
    monkeypatch.setattr(client, "_entity_location_service", entity_service)
    monkeypatch.setattr(client, "_clock_service", clock_service)
    monkeypatch.setattr(client._mqtt_transport, "close", transport.close)

    with pytest.raises(asyncio.CancelledError):
        await client.close()

    cleanup = client._mqtt_cleanup_task
    assert cleanup is not None
    assert cleanup.done()
    assert task_service.close_calls == 1
    assert entity_service.close_calls == 1
    assert clock_service.close_calls == 1
    assert transport.close_calls == 1
    assert client._clock_service is None
    assert not client._connection_streams
    assert client.status.state is ClientState.CLOSED

    await client.close()

    assert cleanup is client._mqtt_cleanup_task
    assert task_service.close_calls == 1
    assert entity_service.close_calls == 1
    assert clock_service.close_calls == 1
    assert transport.close_calls == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("picogrid-ecn-")
    ]


@pytest.mark.asyncio
async def test_close_joins_failed_start_cleanup_without_losing_detached_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0
            self.close_completions = 0

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            self.close_completions += 1

    client = ECNClient(_config())
    task_service = BlockingService()
    entity_service = BlockingService()
    transport_close_calls = 0

    async def fail_start() -> None:
        raise AuthenticationError("synthetic rejection", operation="mqtt.start")

    async def close_transport() -> None:
        nonlocal transport_close_calls
        transport_close_calls += 1

    monkeypatch.setattr(client, "_task_service", task_service)
    monkeypatch.setattr(client, "_entity_location_service", entity_service)
    monkeypatch.setattr(client._mqtt_transport, "start", fail_start)
    monkeypatch.setattr(client._mqtt_transport, "close", close_transport)

    starting = asyncio.create_task(client.start())
    await asyncio.wait_for(
        asyncio.gather(
            task_service.close_started.wait(),
            entity_service.close_started.wait(),
        ),
        timeout=1,
    )

    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not closing.done()
    task_service.release_close.set()
    entity_service.release_close.set()

    await asyncio.wait_for(closing, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert task_service.close_calls == 1
    assert task_service.close_completions == 1
    assert entity_service.close_calls == 1
    assert entity_service.close_completions == 1
    assert transport_close_calls == 1
    assert client.status.state is ClientState.CLOSED
    assert client._startup_task is None
    assert not client._startup_completion_tasks
    assert client._mqtt_cleanup_task is not None
    assert client._mqtt_cleanup_task.done()
    assert client._clock_service is None


@pytest.mark.asyncio
async def test_closed_close_joins_surviving_cleanup_without_reclosing_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_completed = asyncio.Event()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            self.close_completed.set()

    config = _config().model_copy(update={"shutdown_timeout": 0.01})
    client = ECNClient(config)
    task_service = BlockingService()
    entity_service = BlockingService()
    transport_close_calls = 0

    async def close_transport() -> None:
        nonlocal transport_close_calls
        transport_close_calls += 1

    monkeypatch.setattr(client, "_task_service", task_service)
    monkeypatch.setattr(client, "_entity_location_service", entity_service)
    monkeypatch.setattr(client._mqtt_transport, "close", close_transport)

    with pytest.raises(ECNTimeoutError, match="shutdown"):
        await client.close()

    assert client.status.state is ClientState.CLOSED
    await asyncio.wait_for(task_service.close_started.wait(), timeout=1)
    await asyncio.wait_for(entity_service.close_started.wait(), timeout=1)
    cleanup = client._mqtt_cleanup_task
    assert cleanup is not None
    assert not cleanup.done()

    task_service.release_close.set()
    entity_service.release_close.set()
    await client.close()

    assert task_service.close_calls == 1
    assert entity_service.close_calls == 1
    assert task_service.close_completed.is_set()
    assert entity_service.close_completed.is_set()
    assert transport_close_calls == 1
    assert cleanup is client._mqtt_cleanup_task
    assert cleanup.done()


@pytest.mark.asyncio
async def test_join_existing_mqtt_cleanup_treats_cancellation_as_incomplete() -> None:
    client = ECNClient(_config())
    cleanup = asyncio.create_task(asyncio.sleep(0))
    cleanup.cancel()
    await asyncio.gather(cleanup, return_exceptions=True)
    client._mqtt_cleanup_task = cleanup

    assert not await client._join_existing_mqtt_cleanup()

    client._mqtt_cleanup_task = None
    await client.close()


@pytest.mark.asyncio
async def test_component_cleanup_propagates_child_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    task_service = client._task_service
    assert task_service is not None
    original_close = task_service.close

    async def cancel_close() -> None:
        raise asyncio.CancelledError

    async def close_transport() -> None:
        return None

    monkeypatch.setattr(task_service, "close", cancel_close)
    monkeypatch.setattr(client._mqtt_transport, "close", close_transport)

    with pytest.raises(asyncio.CancelledError):
        await client._close_services_and_transport((task_service,))

    monkeypatch.setattr(task_service, "close", original_close)
    await client.close()


@pytest.mark.asyncio
async def test_restart_bounds_previous_mqtt_cleanup_before_rebuilding_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config().model_copy(update={"shutdown_timeout": 0.01}))
    release_cleanup = asyncio.Event()
    cleanup = asyncio.create_task(release_cleanup.wait())
    client._mqtt_cleanup_task = cleanup
    client._entity_location_service = None
    build_calls = 0

    def build_services() -> None:
        nonlocal build_calls
        build_calls += 1

    async def skip_redundant_cleanup() -> None:
        return None

    monkeypatch.setattr(client, "_build_services", build_services)
    monkeypatch.setattr(client, "_cleanup_failed_start", skip_redundant_cleanup)

    with pytest.raises(ConnectionError, match="previous MQTT cleanup did not finish"):
        await client._start_once()

    assert build_calls == 0
    cleanup.cancel()
    await asyncio.gather(cleanup, return_exceptions=True)
    client._mqtt_cleanup_task = None
    await client.close()


@pytest.mark.asyncio
async def test_successful_restart_replaces_completed_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    transport_start_calls = 0
    transport_close_calls = 0

    async def start_transport() -> None:
        nonlocal transport_start_calls
        transport_start_calls += 1
        if transport_start_calls == 1:
            raise AuthenticationError("synthetic rejection", operation="mqtt.start")
        client._mqtt_transport._strict_ready = True
        await client._on_mqtt_connection_change(True)

    async def close_transport() -> None:
        nonlocal transport_close_calls
        transport_close_calls += 1
        client._mqtt_transport._strict_ready = False
        await client._on_mqtt_connection_change(False)

    monkeypatch.setattr(client._mqtt_transport, "start", start_transport)
    monkeypatch.setattr(client._mqtt_transport, "close", close_transport)

    with pytest.raises(AuthenticationError):
        await client.start()
    first_cleanup = client._mqtt_cleanup_task
    assert first_cleanup is not None
    assert first_cleanup.done()
    assert transport_close_calls == 1

    await client.start()
    assert client.is_ready
    assert client._mqtt_cleanup_task is None
    task_service = client._task_service
    entity_service = client._entity_location_service
    assert task_service is not None
    assert entity_service is not None
    task_close_calls = 0
    entity_close_calls = 0
    original_task_close = task_service.close
    original_entity_close = entity_service.close

    async def close_task_service() -> None:
        nonlocal task_close_calls
        task_close_calls += 1
        await original_task_close()

    async def close_entity_service() -> None:
        nonlocal entity_close_calls
        entity_close_calls += 1
        await original_entity_close()

    monkeypatch.setattr(task_service, "close", close_task_service)
    monkeypatch.setattr(entity_service, "close", close_entity_service)

    await client.close()

    assert task_close_calls == 1
    assert entity_close_calls == 1
    assert transport_close_calls == 2
    assert client.status.state is ClientState.CLOSED
    assert client._mqtt_cleanup_task is not first_cleanup
    assert client._mqtt_cleanup_task is not None
    assert client._mqtt_cleanup_task.done()


@pytest.mark.asyncio
async def test_concurrent_start_calls_share_one_startup_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()
    start_calls = 0

    async def ready_start() -> None:
        nonlocal start_calls
        start_calls += 1
        transport_started.set()
        await release_transport.wait()
        client._mqtt_transport._strict_ready = True
        await client._on_mqtt_connection_change(True)

    monkeypatch.setattr(client._mqtt_transport, "start", ready_start)
    first = asyncio.create_task(client.start())
    await asyncio.wait_for(transport_started.wait(), timeout=1)
    second = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    release_transport.set()

    await asyncio.gather(first, second)
    assert start_calls == 1
    assert client.is_ready
    assert client._startup_task is None
    await client.close()


@pytest.mark.asyncio
async def test_repeated_start_during_post_ready_recovery_is_a_passive_barrier() -> None:
    client = ECNClient(_config().model_copy(update={"connection_timeout": 0.01}))
    recovery_supervisor = asyncio.create_task(
        asyncio.Event().wait(),
        name="synthetic-post-ready-recovery",
    )
    handle = SubscriptionHandle(uuid4(), 1)
    subscription = _Subscription("entity/status-test/+/track", lambda _topic, _payload: None)
    client._mqtt_transport._supervisor = recovery_supervisor
    client._mqtt_transport._subscriptions[handle] = subscription
    client._connection_generation = 1
    client._set_state(ClientState.RECONNECTING)
    snapshot = _RecoverySnapshot(
        ConnectionRetryState.SCHEDULED,
        2,
        next_retry_delay_seconds=30.0,
        failure_code=ConnectionFailureCode.CONNECTION_LOST,
        failure_operation=ConnectionFailureOperation.RECEIVE,
    )
    client._mqtt_transport._recovery_snapshot = snapshot
    await client._on_mqtt_recovery_change(snapshot)

    try:
        with pytest.raises(ConnectionError, match="recovery did not become ready"):
            await client.start()

        assert client.status.state is ClientState.RECONNECTING
        assert client._mqtt_transport._supervisor is recovery_supervisor
        assert not recovery_supervisor.done()
        assert client._mqtt_transport._subscriptions == {handle: subscription}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancelling_first_start_waiter_does_not_cancel_shared_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()

    async def ready_start() -> None:
        transport_started.set()
        await release_transport.wait()
        client._mqtt_transport._strict_ready = True
        await client._on_mqtt_connection_change(True)

    monkeypatch.setattr(client._mqtt_transport, "start", ready_start)
    first = asyncio.create_task(client.start())
    await asyncio.wait_for(transport_started.wait(), timeout=1)
    second = asyncio.create_task(client.start())
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert client._startup_task is not None
    assert not client._startup_task.done()
    assert client._startup_task.cancelling() == 0

    release_transport.set()
    await second
    assert client.is_ready
    await client.close()


@pytest.mark.asyncio
async def test_startup_completion_is_consumed_after_all_waiters_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()

    async def fail_start() -> None:
        transport_started.set()
        await release_transport.wait()
        raise AuthenticationError("synthetic rejection", operation="mqtt.start")

    monkeypatch.setattr(client._mqtt_transport, "start", fail_start)
    first = asyncio.create_task(client.start())
    await asyncio.wait_for(transport_started.wait(), timeout=1)
    second = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    startup = client._startup_task
    assert startup is not None

    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    assert not startup.done()
    assert startup.cancelling() == 0

    release_transport.set()
    with pytest.raises(AuthenticationError):
        await startup
    if client._startup_completion_tasks:
        await asyncio.gather(*tuple(client._startup_completion_tasks))
    assert startup.done()
    assert client._startup_task is None
    assert not client._startup_completion_tasks
    assert client.status.state is ClientState.FAILED
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "failure_operation", "expected_error"),
    [
        (
            ConnectionFailureCode.CONFIGURATION_INVALID,
            ConnectionFailureOperation.CONFIGURE,
            ConfigurationError,
        ),
        (
            ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED,
            ConnectionFailureOperation.TLS,
            ConnectionError,
        ),
    ],
)
async def test_start_requires_new_client_after_fixed_initial_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_code: ConnectionFailureCode,
    failure_operation: ConnectionFailureOperation,
    expected_error: type[Exception],
) -> None:
    client = ECNClient(_config())
    snapshot = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        1,
        failure_code=failure_code,
        failure_operation=failure_operation,
    )
    start_calls = 0

    async def fail_start() -> None:
        nonlocal start_calls
        start_calls += 1
        client._mqtt_transport._recovery_snapshot = snapshot
        await client._on_mqtt_recovery_change(snapshot)
        raise client._readiness_terminal_error(operation="mqtt.start")

    monkeypatch.setattr(client._mqtt_transport, "start", fail_start)

    with pytest.raises(expected_error):
        await client.start()

    with pytest.raises(expected_error) as caught:
        await client.start()

    assert getattr(caught.value, "operation", None) == "client.start"
    assert client.status.connection_generation == 0
    assert start_calls == 1
    assert client.status.state is ClientState.FAILED
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "expected_error"),
    [
        (ConnectionFailureCode.CREDENTIALS_UNAVAILABLE, AuthenticationError),
        (ConnectionFailureCode.CONFIGURATION_INVALID, ConfigurationError),
        (ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED, AuthorizationError),
        (ConnectionFailureCode.CONNECTION_RESOURCE_LIMIT, ResourceLimitError),
        (ConnectionFailureCode.PROTOCOL_FAILURE, ProtocolError),
        (ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED, ConnectionError),
        (ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW, ConnectionError),
        (ConnectionFailureCode.RETRY_EXHAUSTED, ConnectionError),
    ],
)
async def test_start_cannot_revive_post_ready_terminal_states(
    monkeypatch: pytest.MonkeyPatch,
    failure_code: ConnectionFailureCode,
    expected_error: type[Exception],
) -> None:
    client = ECNClient(_config())
    client._connection_generation = 1
    snapshot = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        2,
        failure_code=failure_code,
        failure_operation=ConnectionFailureOperation.CONNECT,
    )
    client._mqtt_transport._recovery_snapshot = snapshot
    await client._on_mqtt_recovery_change(snapshot)
    start_calls = 0

    async def unexpected_start() -> None:
        nonlocal start_calls
        start_calls += 1

    monkeypatch.setattr(client._mqtt_transport, "start", unexpected_start)

    with pytest.raises(expected_error) as caught:
        await client.start()

    assert getattr(caught.value, "operation", None) == "client.start"
    assert start_calls == 0
    assert client.status.state is ClientState.FAILED
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "failure_code"),
    [
        ("notify_credentials_changed", ConnectionFailureCode.CREDENTIALS_UNAVAILABLE),
        ("request_retry", ConnectionFailureCode.RETRY_EXHAUSTED),
    ],
)
async def test_wait_until_ready_immediately_after_terminal_revival(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    failure_code: ConnectionFailureCode,
) -> None:
    client = ECNClient(_config())
    client._connection_generation = 1
    snapshot = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        2,
        failure_code=failure_code,
        failure_operation=ConnectionFailureOperation.CONNECT,
    )
    client._mqtt_transport._recovery_snapshot = snapshot
    await client._on_mqtt_recovery_change(snapshot)
    monkeypatch.setattr(client._mqtt_transport, method_name, lambda: None)

    getattr(client, method_name)()
    assert client.status.state is ClientState.RECONNECTING
    assert client.status.retry_state is ConnectionRetryState.CONNECTING
    assert client.status.consecutive_attempt_count == 0
    waiter = asyncio.create_task(client.wait_until_ready(timeout=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    await client._on_mqtt_connection_change(True)
    client._mqtt_transport._strict_ready = True
    await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.INACTIVE, 1))

    ready = await waiter
    assert ready.ready
    assert ready.connection_generation == 2
    await client.close()


@pytest.mark.asyncio
async def test_initial_retry_exhaustion_revival_waits_for_connecting_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    terminal = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        2,
        failure_code=ConnectionFailureCode.RETRY_EXHAUSTED,
        failure_operation=ConnectionFailureOperation.CONNECT,
    )
    client._mqtt_transport._recovery_snapshot = terminal
    await client._on_mqtt_recovery_change(terminal)
    stream = client.connection_events()
    failed = await anext(stream)
    assert failed.state is ClientState.FAILED
    monkeypatch.setattr(client._mqtt_transport, "request_retry", lambda: None)

    client.request_retry()
    reconnecting = await asyncio.wait_for(anext(stream), timeout=0.1)
    assert reconnecting.state is ClientState.RECONNECTING
    assert reconnecting.retry_state is ConnectionRetryState.CONNECTING
    assert reconnecting.connection_generation == 0
    assert reconnecting.consecutive_attempt_count == 0

    waiter = asyncio.create_task(client.wait_until_ready(timeout=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    await client._on_mqtt_connection_change(True)
    client._mqtt_transport._strict_ready = True
    await client._on_mqtt_recovery_change(_RecoverySnapshot(ConnectionRetryState.INACTIVE, 1))

    ready = await waiter
    assert ready.ready
    assert ready.connection_generation == 1
    await stream.aclose()
    await client.close()


@pytest.mark.asyncio
async def test_initial_retry_exhaustion_preserves_services_for_explicit_revival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(_config())
    terminal = _RecoverySnapshot(
        ConnectionRetryState.TERMINAL,
        1,
        failure_code=ConnectionFailureCode.RETRY_EXHAUSTED,
        failure_operation=ConnectionFailureOperation.CONNECT,
    )

    async def exhaust_startup() -> None:
        client._mqtt_transport._recovery_snapshot = terminal
        await client._on_mqtt_recovery_change(terminal)
        raise ConnectionError(
            "synthetic retry exhaustion",
            operation="mqtt.start",
            details={"failure_code": ConnectionFailureCode.RETRY_EXHAUSTED.value},
        )

    close_calls = 0
    original_close = client._mqtt_transport.close

    async def record_close() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(client._mqtt_transport, "start", exhaust_startup)
    monkeypatch.setattr(client._mqtt_transport, "close", record_close)

    with pytest.raises(ConnectionError, match="synthetic retry exhaustion"):
        await client.start()

    assert close_calls == 0
    assert client.status.state is ClientState.FAILED
    assert client._task_service is not None
    assert client._entity_location_service is not None

    retry_calls = 0

    def record_retry() -> None:
        nonlocal retry_calls
        retry_calls += 1

    monkeypatch.setattr(client._mqtt_transport, "request_retry", record_retry)
    client.request_retry()

    assert retry_calls == 1
    monkeypatch.setattr(client._mqtt_transport, "close", original_close)
    await client.close()
