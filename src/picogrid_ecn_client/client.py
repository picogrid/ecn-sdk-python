# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Top-level public ECN client lifecycle and domain access."""

from __future__ import annotations

import asyncio
import builtins
import math
import ssl
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ._entity_locations import EntityLocationService
from ._preflight import PreflightRunner
from ._protocol import (
    build_entity_protobuf_topic,
    build_entity_subscription_filters,
    build_entity_topic,
    build_location_protobuf_topic,
    build_location_topic,
    decode_entity_payload,
    decode_location_payload,
    encode_entity_payload,
    encode_location_payload,
)
from ._services import ClockService, TaskService
from ._transport import MQTTTransport
from ._transport.credentials import build_lifecycle_owned_client_ssl_context
from ._transport.mqtt import _RecoverySnapshot
from .config import ECNConfig
from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConnectionError,
    ECNClientError,
    NotReadyError,
    ProtocolError,
    ResourceLimitError,
    TimeoutError,
    ValidationError,
)
from .interfaces import Clock, Entities, Locations, TaskDispatchResult, Tasks
from .interfaces.tasks import AnyTaskHandler
from .models import (
    ClientState,
    ClockReport,
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    ConnectionStatus,
    DeliveryPolicy,
    Entity,
    EntityCategory,
    EntityEvent,
    Location,
    LocationEvent,
    PreflightReport,
    PublicationReceipt,
    SubscriptionProbe,
    SubscriptionProbeKind,
    TaskMode,
    TaskRegistration,
    WireFormat,
)
from .streams import EventStream

_START_FAILURES_REQUIRING_NEW_CLIENT = frozenset(
    {
        ConnectionFailureCode.CONFIGURATION_INVALID,
        ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED,
    }
)


def _bounded_retry_timestamp(observed_at: datetime, delay_seconds: float) -> datetime:
    """Represent an internal finite retry delay without overflowing datetime."""

    if not math.isfinite(delay_seconds):
        return datetime.max.replace(tzinfo=UTC)
    try:
        return observed_at + timedelta(seconds=delay_seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


class ECNClient:
    """Manage one explicitly configured MQTT v5 ECN connection.

    The domain interfaces are stable for the lifetime of the client. Entity and
    location subscriptions are installed lazily by their watch methods, not by
    :meth:`start`.
    """

    def __init__(self, config: ECNConfig) -> None:
        """Create a client without connecting or installing subscriptions.

        Args:
            config: Frozen connection and operation configuration.
        """
        self._config = config
        self._state = ClientState.CREATED
        self._state_changed_at = datetime.now(UTC)
        self._mqtt_connected = False
        self._lifecycle_lock = asyncio.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._startup_completion_tasks: set[asyncio.Task[None]] = set()
        self._mqtt_cleanup_task: asyncio.Task[None] | None = None

        self._connection_generation = 0
        self._ready_transport_generation: int | None = None
        self._consecutive_attempt_count = 0
        self._last_connected_at: datetime | None = None
        self._last_disconnected_at: datetime | None = None
        self._next_retry_at: datetime | None = None
        self._last_failure_code: ConnectionFailureCode | None = None
        self._last_failure_operation: ConnectionFailureOperation | None = None
        self._retry_state = ConnectionRetryState.INACTIVE
        self._ready_waiters: set[asyncio.Future[ConnectionStatus]] = set()
        self._connection_streams: set[EventStream[ConnectionStatus]] = set()

        self._mqtt_transport = MQTTTransport(
            config,
            self._on_mqtt_connection_change,
            on_recovery_change=self._on_mqtt_recovery_change,
        )
        self._clock_service: ClockService | None = ClockService(config)
        self._entity_location_service: EntityLocationService | None = None
        self._task_service: TaskService | None = None

        self._clock_interface = Clock(self)
        self._entities_interface = Entities(self)
        self._locations_interface = Locations(self)
        self._tasks_interface = Tasks(self)
        self._published_status_signature = self.status.model_dump(
            mode="python",
            exclude={"changed_at"},
        )
        self._build_services()

    @property
    def config(self) -> ECNConfig:
        """Return the frozen configuration supplied at construction."""
        return self._config

    @property
    def clock(self) -> Clock:
        """Return the ECN-relative clock diagnostic interface.

        The interface is always present, including before `start()` and after
        `close()`; individual measurements require a configured NTP endpoint.
        """
        return self._clock_interface

    @property
    def entities(self) -> Entities:
        """Return the stable typed entity interface."""
        return self._entities_interface

    @property
    def locations(self) -> Locations:
        """Return the stable typed location interface."""
        return self._locations_interface

    @property
    def tasks(self) -> Tasks:
        """Return the stable typed task interface."""
        return self._tasks_interface

    @property
    def is_ready(self) -> bool:
        """Return whether MQTT and every still-required subscription are ready."""
        return (
            self._state is ClientState.READY and self._mqtt_connected and self._mqtt_transport.ready
        )

    @property
    def status(self) -> ConnectionStatus:
        """Return a secret-safe snapshot of the current connection state."""
        return ConnectionStatus(
            state=self._state,
            ready=self.is_ready,
            mqtt_connected=self._mqtt_connected,
            changed_at=self._state_changed_at,
            connection_generation=self._connection_generation,
            consecutive_attempt_count=self._consecutive_attempt_count,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
            next_retry_at=self._next_retry_at,
            last_failure_code=self._last_failure_code,
            last_failure_operation=self._last_failure_operation,
            retry_state=self._retry_state,
        )

    async def wait_until_ready(self, *, timeout: float) -> ConnectionStatus:
        """Wait for strict readiness within one caller-supplied deadline.

        Args:
            timeout: Positive finite maximum wait in seconds.

        Returns:
            The first strictly ready connection snapshot.

        Raises:
            AuthenticationError: If credentials are unavailable or rejected.
            AuthorizationError: If connection or required-subscription access is denied.
            ConfigurationError: If unchanged local configuration is invalid.
            ConnectionError: If recovery becomes terminal for another connection reason.
            NotReadyError: If the client has not started or is closing/closed.
            ProtocolError: If recovery terminates on a protocol violation.
            ResourceLimitError: If the broker rejects a required resource.
            TimeoutError: If the caller's deadline expires first.
            ValidationError: If ``timeout`` is not positive and finite.
        """

        timeout_seconds: float | None = None
        if not isinstance(timeout, bool) and isinstance(timeout, (int, float)):
            with suppress(OverflowError):
                timeout_seconds = float(timeout)
        if timeout_seconds is None or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValidationError(
                "timeout must be positive and finite",
                operation="client.wait_until_ready",
            )
        if self.is_ready:
            return self.status
        self._raise_if_readiness_wait_cannot_continue()
        waiter: asyncio.Future[ConnectionStatus] = asyncio.get_running_loop().create_future()
        self._ready_waiters.add(waiter)
        self._complete_ready_waiters()
        try:
            async with asyncio.timeout(timeout_seconds):
                return await waiter
        except builtins.TimeoutError:
            raise TimeoutError(
                "client did not become ready before the caller deadline",
                operation="client.wait_until_ready",
            ) from None
        finally:
            self._ready_waiters.discard(waiter)

    def connection_events(self) -> EventStream[ConnectionStatus]:
        """Return a capacity-one latest-value stream of redacted connection status.

        The current snapshot is queued before this method returns. Existing streams
        close deterministically with their owning client.

        Raises:
            NotReadyError: If client closing has begun or the client is closed.
        """

        if self._state in {ClientState.CLOSING, ClientState.CLOSED}:
            raise NotReadyError(
                "connection events are unavailable after close begins",
                operation="client.connection_events",
            )
        stream: EventStream[ConnectionStatus]

        def release() -> None:
            self._connection_streams.discard(stream)

        stream = EventStream(
            buffer_size=1,
            delivery_policy=DeliveryPolicy.LATEST,
            on_close=release,
        )
        self._connection_streams.add(stream)
        stream._put_nowait(self.status)
        return stream

    def notify_credentials_changed(self) -> None:
        """Wake credential-blocked recovery after an atomic credential repair."""

        snapshot = self._mqtt_transport.recovery_snapshot
        self._mqtt_transport.notify_credentials_changed()
        self._mark_authorized_revival(
            snapshot,
            frozenset(
                {
                    ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
                    ConnectionFailureCode.AUTHENTICATION_REJECTED,
                }
            ),
        )

    def request_retry(self) -> None:
        """Wake scheduled transient recovery or revive transient retry exhaustion."""

        snapshot = self._mqtt_transport.recovery_snapshot
        self._mqtt_transport.request_retry()
        self._mark_authorized_revival(
            snapshot,
            frozenset({ConnectionFailureCode.RETRY_EXHAUSTED}),
        )

    async def start(self) -> None:
        """Connect to MQTT v5 and wait for readiness.

        This idempotent readiness barrier completes after CONNECT and CONNACK.
        It installs no entity or location subscriptions. A closed client cannot
        be restarted.

        Raises:
            AuthenticationError: If authentication material or broker
                authentication is rejected.
            AuthorizationError: If the broker rejects connection authorization.
            ConfigurationError: If local MQTT or TLS verification configuration is invalid.
            ConnectionError: If MQTT readiness fails or times out.
            NotReadyError: If the client is closing or already closed.
            ProtocolError: If MQTT readiness terminates on a protocol violation.
            ResourceLimitError: If the broker rejects a required connection resource.
        """

        wait_for_recovery = False
        async with self._lifecycle_lock:
            if self.is_ready:
                return
            if self._state in {ClientState.CLOSING, ClientState.CLOSED}:
                raise NotReadyError(
                    "a closed client cannot be started again",
                    operation="client.start",
                )
            if self._state is ClientState.FAILED and (
                self._connection_generation > 0
                or self._last_failure_code in _START_FAILURES_REQUIRING_NEW_CLIENT
            ):
                raise self._readiness_terminal_error(operation="client.start")
            if self._connection_generation > 0:
                # A previously ready client already owns one long-running recovery
                # supervisor. A repeated start is only a passive readiness barrier;
                # it must not impose a new startup lifecycle on that supervisor.
                wait_for_recovery = True
                startup = None
            else:
                startup = self._startup_task
                if startup is None or startup.done():
                    self._set_state(ClientState.STARTING)
                    startup = asyncio.create_task(
                        self._start_once(),
                        name="picogrid-ecn-client-start",
                    )
                    self._startup_task = startup
                    startup.add_done_callback(self._schedule_startup_completion)

        if wait_for_recovery:
            try:
                await self.wait_until_ready(timeout=self._config.connection_timeout)
            except TimeoutError:
                raise ConnectionError(
                    "client recovery did not become ready before connection_timeout",
                    operation="client.start",
                ) from None
            return

        assert startup is not None
        await asyncio.shield(startup)

    def _schedule_startup_completion(self, startup: asyncio.Task[None]) -> None:
        if not startup.cancelled():
            startup.exception()
        completion = asyncio.create_task(
            self._clear_completed_startup(startup),
            name="picogrid-ecn-client-start-completion",
        )
        self._startup_completion_tasks.add(completion)
        completion.add_done_callback(self._startup_completion_done)

    async def _clear_completed_startup(self, startup: asyncio.Task[None]) -> None:
        async with self._lifecycle_lock:
            if self._startup_task is startup:
                self._startup_task = None

    def _startup_completion_done(self, completion: asyncio.Task[None]) -> None:
        self._startup_completion_tasks.discard(completion)
        if not completion.cancelled():
            completion.exception()

    async def _start_once(self) -> None:
        try:
            if (
                self._clock_service is None
                or self._entity_location_service is None
                or self._task_service is None
            ):
                previous_cleanup = self._mqtt_cleanup_task
                if previous_cleanup is not None:
                    try:
                        async with asyncio.timeout(self._config.shutdown_timeout):
                            await asyncio.shield(previous_cleanup)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if previous_cleanup.cancelled() and (
                            current is None or not current.cancelling()
                        ):
                            raise ConnectionError(
                                "previous MQTT cleanup was cancelled",
                                operation="client.start",
                            ) from None
                        raise
                    except builtins.TimeoutError:
                        raise ConnectionError(
                            "previous MQTT cleanup did not finish",
                            operation="client.start",
                        ) from None
                    if self._mqtt_cleanup_task is previous_cleanup:
                        self._mqtt_cleanup_task = None
                self._build_services()
            await self._mqtt_transport.start()
        except asyncio.CancelledError:
            if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                await self._cleanup_failed_start()
                if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                    self._set_state(ClientState.FAILED)
            raise
        except ECNClientError:
            if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                snapshot = self._mqtt_transport.recovery_snapshot
                if not (
                    self._connection_generation == 0
                    and snapshot.state is ConnectionRetryState.TERMINAL
                    and snapshot.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
                ):
                    await self._cleanup_failed_start()
                if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                    self._set_state(ClientState.FAILED)
            raise
        except Exception:
            if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                await self._cleanup_failed_start()
                if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                    self._set_state(ClientState.FAILED)
            raise ConnectionError(
                "client startup failed",
                operation="client.start",
            ) from None
        if self._state in {ClientState.CLOSING, ClientState.CLOSED}:
            raise NotReadyError(
                "client closing interrupted startup",
                operation="client.start",
            )
        if not self._mqtt_transport.ready:
            await self._cleanup_failed_start()
            if self._state not in {ClientState.CLOSING, ClientState.CLOSED}:
                self._set_state(ClientState.FAILED)
            raise ConnectionError(
                "client startup completed without strict MQTT readiness",
                operation="client.start",
            )
        self._set_state(ClientState.READY)

    async def preflight(
        self,
        *,
        subscription_probes: Collection[SubscriptionProbe] = (),
    ) -> PreflightReport:
        """Run read-only checks and caller-requested subscription probes.

        The report covers configuration, DNS, TCP, TLS, MQTT v5 CONNACK, and
        only the supplied bounded subscription probes. The method performs no
        application publish. Its ``publish_authorization`` check is always
        ``UNKNOWN`` because preflight does not establish write permission.

        Args:
            subscription_probes: Exact bounded subscription probes to request.

        Returns:
            A secret-safe report of the completed checks.

        Raises:
            TimeoutError: If closing the probe transport exceeds the configured
                shutdown timeout. Check failures are reported in the returned
                report rather than raised.
        """

        probe_transport = MQTTTransport(self._config)

        async def mqtt_probe() -> None:
            if probe_transport.connected:
                return
            await probe_transport.start()

        def make_subscription_probe(probe: SubscriptionProbe) -> Callable[[], Awaitable[None]]:
            topic_filter = self._subscription_probe_topic(probe)

            async def run() -> None:
                if not probe_transport.connected:
                    raise ConnectionError(
                        "MQTT transport is not connected",
                        operation="preflight.subscription",
                    )

                async def discard(_topic: str, _payload: bytes) -> None:
                    return None

                handle = await probe_transport.subscribe(topic_filter, discard)
                await probe_transport.unsubscribe(handle)

            return run

        async def ssl_context_factory() -> ssl.SSLContext:
            context = await build_lifecycle_owned_client_ssl_context(
                self._config.tls,
                self._config.auth,
            )
            if context is None:
                raise RuntimeError("TLS context requested while TLS is disabled")
            return context

        try:
            return await PreflightRunner(
                self._config,
                mqtt_probe=mqtt_probe,
                subscription_probes=tuple(
                    make_subscription_probe(probe) for probe in subscription_probes
                ),
                ssl_context_factory=ssl_context_factory,
            ).run()
        finally:
            await probe_transport.close()

    async def close(self) -> None:
        """Close the client and remove owned resources.

        Cleanup is idempotent and bounded by the configured shutdown timeout.
        It closes streams and handlers, stops MQTT work and the connection, and
        removes temporary credential files.

        Raises:
            ConnectionError: If a component fails during cleanup.
            TimeoutError: If cleanup exceeds the shutdown timeout in seconds.
        """

        try:
            async with self._lifecycle_lock:
                if self._state is ClientState.CLOSED:
                    if not await self._join_existing_mqtt_cleanup():
                        raise TimeoutError(
                            "client shutdown exceeded shutdown_timeout",
                            operation="client.close",
                        )
                    return
                self._set_state(ClientState.CLOSING)
                try:
                    if not await self._run_component_cleanup("picogrid-ecn-client-close"):
                        raise TimeoutError(
                            "client shutdown exceeded shutdown_timeout",
                            operation="client.close",
                        )
                finally:
                    self._mqtt_connected = False
                    if self._startup_task is not None and self._startup_task.done():
                        self._startup_task = None
                    self._set_state(ClientState.CLOSED)
        finally:
            await self._drain_startup_completion_tasks()

    async def __aenter__(self) -> ECNClient:
        """Start the client and return it after readiness.

        Returns:
            This client, ready for entity, location, and task work.

        Raises:
            AuthenticationError: If authentication material or broker
                authentication is rejected.
            AuthorizationError: If the broker rejects connection authorization.
            ConfigurationError: If local MQTT or TLS verification configuration is invalid.
            ConnectionError: If MQTT readiness fails or times out.
            NotReadyError: If the client is closing or already closed.
            ProtocolError: If MQTT readiness terminates on a protocol violation.
            ResourceLimitError: If the broker rejects a required connection resource.
        """
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when leaving the asynchronous context.

        The client closes regardless of whether an exception propagated from
        the context. Cleanup is idempotent and bounded by the configured
        shutdown timeout.

        Args:
            exc_type: Type of the propagated exception, or ``None``.
            exc_value: Propagated exception instance, or ``None``.
            traceback: Traceback for the propagated exception, or ``None``.

        Raises:
            ConnectionError: If a component fails during cleanup.
            TimeoutError: If cleanup exceeds the shutdown timeout in seconds.
        """
        await self.close()

    def _set_state(self, state: ClientState) -> None:
        if state is ClientState.STARTING:
            self._consecutive_attempt_count = 0
            self._retry_state = ConnectionRetryState.INACTIVE
            self._next_retry_at = None
        elif state is ClientState.FAILED:
            self._retry_state = ConnectionRetryState.TERMINAL
            self._next_retry_at = None
        elif state in {ClientState.CLOSING, ClientState.CLOSED}:
            self._retry_state = ConnectionRetryState.INACTIVE
            self._next_retry_at = None
        self._transition_state(state)
        self._publish_connection_status()

    def _transition_state(self, state: ClientState) -> None:
        """Apply one lifecycle-state transition without publishing it."""

        if self._state is state:
            return
        self._state = state

    def _mark_authorized_revival(
        self,
        previous: _RecoverySnapshot,
        allowed_codes: frozenset[ConnectionFailureCode],
    ) -> None:
        """Make one transport-authorized terminal revival immediately observable."""

        if not (
            self._state is ClientState.FAILED
            and previous.state is ConnectionRetryState.TERMINAL
            and previous.failure_code in allowed_codes
            and (
                self._connection_generation > 0
                or previous.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
            )
        ):
            return
        self._consecutive_attempt_count = 0
        self._retry_state = ConnectionRetryState.CONNECTING
        self._next_retry_at = None
        self._transition_state(ClientState.RECONNECTING)
        self._publish_connection_status()

    async def _on_mqtt_connection_change(self, connected: bool) -> None:
        if connected and self._state in {ClientState.CLOSING, ClientState.CLOSED}:
            return
        if self._mqtt_connected == connected:
            return
        observed_at = datetime.now(UTC)
        self._mqtt_connected = connected
        if connected:
            self._last_connected_at = observed_at
        else:
            self._last_disconnected_at = observed_at
            if self._state is ClientState.READY:
                self._transition_state(ClientState.RECONNECTING)
        self._publish_connection_status()

    async def _on_mqtt_recovery_change(self, snapshot: _RecoverySnapshot) -> None:
        if self._state in {ClientState.CLOSING, ClientState.CLOSED}:
            return
        observed_at = datetime.now(UTC)
        self._consecutive_attempt_count = snapshot.consecutive_attempt_count
        self._retry_state = snapshot.state
        self._next_retry_at = (
            _bounded_retry_timestamp(observed_at, snapshot.next_retry_delay_seconds)
            if snapshot.state
            in {
                ConnectionRetryState.SCHEDULED,
                ConnectionRetryState.WAITING_FOR_CREDENTIALS,
            }
            and snapshot.next_retry_delay_seconds is not None
            else None
        )
        if snapshot.failure_code is not None:
            self._last_failure_code = snapshot.failure_code
        if snapshot.failure_operation is not None:
            self._last_failure_operation = snapshot.failure_operation

        if snapshot.state is ConnectionRetryState.TERMINAL:
            self._transition_state(ClientState.FAILED)
        elif snapshot.state is ConnectionRetryState.CONNECTING:
            if (
                self._connection_generation > 0
                or self._state is ClientState.RECONNECTING
                or (
                    self._state is ClientState.FAILED
                    and self._last_failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
                )
            ):
                self._transition_state(ClientState.RECONNECTING)
        elif (
            snapshot.state is ConnectionRetryState.INACTIVE
            and self._mqtt_connected
            and self._mqtt_transport.ready
        ):
            if self._state is not ClientState.READY:
                self._connection_generation += 1
            self._ready_transport_generation = self._mqtt_transport.connection_generation
            self._transition_state(ClientState.READY)
        self._publish_connection_status()

    def _publish_connection_status(self) -> None:
        snapshot = self.status
        signature = snapshot.model_dump(mode="python", exclude={"changed_at"})
        if signature != self._published_status_signature:
            self._state_changed_at = datetime.now(UTC)
            self._published_status_signature = signature
            snapshot = self.status
            for stream in tuple(self._connection_streams):
                stream._put_nowait(snapshot)
        self._complete_ready_waiters(snapshot)

    def _complete_ready_waiters(self, snapshot: ConnectionStatus | None = None) -> None:
        if not self._ready_waiters:
            return
        current = snapshot or self.status
        if current.ready:
            for waiter in tuple(self._ready_waiters):
                if not waiter.done():
                    waiter.set_result(current)
            return
        if current.state in {ClientState.CLOSING, ClientState.CLOSED}:
            for waiter in tuple(self._ready_waiters):
                if not waiter.done():
                    waiter.set_exception(
                        NotReadyError(
                            "client closed before becoming ready",
                            operation="client.wait_until_ready",
                        )
                    )
            return
        if current.state is not ClientState.FAILED:
            return
        for waiter in tuple(self._ready_waiters):
            if not waiter.done():
                waiter.set_exception(self._readiness_terminal_error())

    def _raise_if_readiness_wait_cannot_continue(self) -> None:
        if self._state in {ClientState.CREATED, ClientState.CLOSING, ClientState.CLOSED}:
            raise NotReadyError(
                "client is not in a state that can become ready",
                operation="client.wait_until_ready",
            )
        if self._state is ClientState.FAILED:
            raise self._readiness_terminal_error()

    def _readiness_terminal_error(
        self,
        *,
        operation: str = "client.wait_until_ready",
    ) -> ECNClientError:
        code = self._last_failure_code
        details = {"failure_code": code.value} if code is not None else None
        if code is ConnectionFailureCode.CONFIGURATION_INVALID:
            return ConfigurationError(
                "client configuration cannot become ready",
                operation=operation,
                details=details,
            )
        if code in {
            ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
            ConnectionFailureCode.AUTHENTICATION_REJECTED,
        }:
            return AuthenticationError(
                "client credentials cannot become ready",
                operation=operation,
                details=details,
            )
        if code in {
            ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED,
            ConnectionFailureCode.SUBSCRIPTION_DENIED,
        }:
            return AuthorizationError(
                "client authorization cannot become ready",
                operation=operation,
                details=details,
            )
        if code in {
            ConnectionFailureCode.CONNECTION_RESOURCE_LIMIT,
            ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT,
        }:
            return ResourceLimitError(
                "client resource allocation cannot become ready",
                operation=operation,
                details=details,
            )
        if code is ConnectionFailureCode.PROTOCOL_FAILURE:
            return ProtocolError(
                "client protocol recovery cannot become ready",
                operation=operation,
                details=details,
            )
        return ConnectionError(
            "client connection recovery cannot become ready",
            operation=operation,
            details=details,
        )

    @staticmethod
    def _subscription_probe_topic(probe: SubscriptionProbe) -> str:
        if probe.kind is SubscriptionProbeKind.ENTITY:
            assert probe.category is not None
            if probe.wire_format is WireFormat.PROTOBUF:
                return build_entity_protobuf_topic(probe.integration, probe.category)
            if probe.entity_id is not None:
                return build_entity_topic(probe.integration, probe.entity_id, probe.category)
            filters = build_entity_subscription_filters(
                frozenset({probe.category}),
                frozenset({probe.integration}),
            )
            return next(value for value in filters if value.startswith("entity/"))
        assert probe.entity_id is not None
        if probe.wire_format is WireFormat.PROTOBUF:
            return build_location_protobuf_topic(probe.integration, probe.entity_id)
        return build_location_topic(probe.integration, probe.entity_id)

    def _build_services(self) -> None:
        if self._clock_service is None:
            self._clock_service = ClockService(self._config)

        def encode_entity(entity: Entity) -> tuple[str, bytes, int]:
            if self._config.wire_format is WireFormat.PROTOBUF:
                topic = build_entity_protobuf_topic(entity.integration, entity.category)
            else:
                topic = build_entity_topic(entity.integration, entity.id, entity.category)
            payload = encode_entity_payload(
                entity,
                self._config.wire_format,
                self._config.maximum_payload_size,
            )
            qos = 0 if entity.category is EntityCategory.TRACK else 1
            return topic, payload, qos

        def encode_location(
            entity_id: UUID,
            integration: str,
            location: Location,
        ) -> tuple[str, bytes, int]:
            if self._config.wire_format is WireFormat.PROTOBUF:
                topic = build_location_protobuf_topic(integration, entity_id)
            else:
                topic = build_location_topic(integration, entity_id)
            payload = encode_location_payload(
                location,
                self._config.wire_format,
                self._config.maximum_payload_size,
            )
            return topic, payload, 1

        self._entity_location_service = EntityLocationService(
            self._mqtt_transport,
            integration_name=self._config.integration_name,
            default_buffer_size=self._config.watcher_buffer_size,
            maximum_payload_size=self._config.maximum_payload_size,
            decode_entity=lambda topic, payload: decode_entity_payload(
                topic,
                payload,
                self._config.maximum_payload_size,
            ),
            decode_location=lambda topic, payload: decode_location_payload(
                topic,
                payload,
                self._config.maximum_payload_size,
            ),
            encode_entity=encode_entity,
            encode_location=encode_location,
        )
        self._task_service = TaskService(
            self._mqtt_transport,
            integration_name=self._config.integration_name,
            terminal_id=self._config.terminal_id,
            operation_timeout=self._config.task_timeout,
            max_outstanding=self._config.maximum_outstanding_operations,
            max_payload_size=self._config.maximum_payload_size,
            shutdown_timeout=self._config.shutdown_timeout,
            connection_generation=lambda: self._connection_generation,
            ready_transport_generation=lambda: self._ready_transport_generation,
        )

    async def _cleanup_failed_start(self) -> None:
        try:
            await self._run_component_cleanup(
                "picogrid-ecn-client-start-cleanup",
                close_clock=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Startup's original typed failure remains the actionable error.
            pass
        self._mqtt_connected = False

    async def _run_component_cleanup(
        self,
        task_name: str,
        *,
        close_clock: bool = True,
    ) -> bool:
        """Run component cleanup within the configured bound.

        A dependency or caller handler can suppress cancellation. In that case the
        detached task retains a result consumer, while the public lifecycle call
        returns at the configured boundary instead of awaiting it forever.
        """

        cleanup_operation = self._close_components if close_clock else self._close_mqtt_components
        cleanup = asyncio.create_task(cleanup_operation(), name=task_name)
        try:
            async with asyncio.timeout(self._config.shutdown_timeout):
                await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cleanup.cancel()
            cleanup.add_done_callback(self._consume_cleanup_result)
            await asyncio.sleep(0)
            raise
        except builtins.TimeoutError:
            cleanup.cancel()
            cleanup.add_done_callback(self._consume_cleanup_result)
            await asyncio.sleep(0)
            return False
        return True

    @staticmethod
    def _consume_cleanup_result(future: asyncio.Future[Any]) -> None:
        if not future.cancelled():
            future.exception()

    async def _drain_startup_completion_tasks(self) -> None:
        while self._startup_completion_tasks:
            await asyncio.gather(
                *tuple(self._startup_completion_tasks),
                return_exceptions=True,
            )

    def _detach_mqtt_services(
        self,
    ) -> tuple[TaskService | None, EntityLocationService | None]:
        services = (self._task_service, self._entity_location_service)
        self._task_service = None
        self._entity_location_service = None
        return services

    async def _close_mqtt_components(self) -> None:
        cleanup = self._mqtt_cleanup_task
        services_are_owned = (
            self._task_service is not None or self._entity_location_service is not None
        )
        if cleanup is None or (cleanup.done() and services_are_owned):
            cleanup = asyncio.create_task(
                self._close_services_and_transport(self._detach_mqtt_services()),
                name="picogrid-ecn-client-mqtt-component-close",
            )
            self._mqtt_cleanup_task = cleanup
            cleanup.add_done_callback(self._consume_cleanup_result)
        await asyncio.shield(cleanup)

    async def _join_existing_mqtt_cleanup(self) -> bool:
        cleanup = self._mqtt_cleanup_task
        if cleanup is None:
            return True
        if cleanup.done():
            if cleanup.cancelled():
                return False
            cleanup.exception()
            return True
        try:
            async with asyncio.timeout(self._config.shutdown_timeout):
                await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            if cleanup.cancelled():
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return False
            raise
        except builtins.TimeoutError:
            return False
        except Exception:
            # The first close already established CLOSED and reported its terminal
            # result. An idempotent close consumes that generation without reporting
            # a later cleanup exception a second time.
            return True
        return True

    async def _close_components(self) -> None:
        errors: list[Exception] = []
        stream_cancellation: asyncio.CancelledError | None = None
        startup = self._startup_task
        current = asyncio.current_task()
        if startup is not None and startup is not current and not startup.done():
            startup.cancel()
        try:
            await self._close_connection_streams()
        except asyncio.CancelledError as error:
            stream_cancellation = error
        except Exception as error:
            errors.append(error)
        clock_service = self._clock_service
        self._clock_service = None
        mqtt_cleanup = asyncio.create_task(
            self._close_mqtt_components(),
            name="picogrid-ecn-client-mqtt-close-join",
        )
        close_tasks: list[asyncio.Task[Any]] = [mqtt_cleanup]
        if clock_service is not None:
            close_tasks.append(
                asyncio.create_task(
                    clock_service.close(),
                    name="picogrid-ecn-clockservice-close",
                )
            )
        if startup is not None and startup is not current and not startup.done():
            close_tasks.append(startup)
        close_group = asyncio.gather(*close_tasks, return_exceptions=True)
        try:
            results = await asyncio.shield(close_group)
        except asyncio.CancelledError:
            for task in close_tasks:
                if not task.done():
                    task.cancel()
                task.add_done_callback(self._consume_cleanup_result)
            close_group.add_done_callback(self._consume_cleanup_result)
            raise
        for result in results[:2] if clock_service is not None else results[:1]:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                errors.append(result)
        if stream_cancellation is not None:
            raise stream_cancellation
        self._raise_cleanup_errors(errors)

    async def _close_connection_streams(self) -> None:
        streams = tuple(self._connection_streams)
        results: list[BaseException | None] = []
        try:
            if streams:
                results = list(
                    await asyncio.gather(
                        *(stream.aclose() for stream in streams),
                        return_exceptions=True,
                    )
                )
        finally:
            self._connection_streams.clear()
        cancelled = next(
            (result for result in results if isinstance(result, asyncio.CancelledError)),
            None,
        )
        if cancelled is not None:
            raise cancelled
        self._raise_cleanup_errors([result for result in results if isinstance(result, Exception)])

    async def _close_services_and_transport(
        self,
        services: tuple[
            TaskService | EntityLocationService | ClockService | None,
            ...,
        ],
    ) -> None:
        close_tasks = [
            asyncio.create_task(
                service.close(),
                name=f"picogrid-ecn-{type(service).__name__.lower()}-close",
            )
            for service in services
            if service is not None
        ]
        close_tasks.append(
            asyncio.create_task(
                self._mqtt_transport.close(),
                name="picogrid-ecn-mqtt-transport-close",
            )
        )
        close_group = asyncio.gather(*close_tasks, return_exceptions=True)
        try:
            results = await asyncio.shield(close_group)
        except asyncio.CancelledError:
            for task in close_tasks:
                if not task.done():
                    task.cancel()
                task.add_done_callback(self._consume_cleanup_result)
            close_group.add_done_callback(self._consume_cleanup_result)
            raise
        cancelled = next(
            (result for result in results if isinstance(result, asyncio.CancelledError)),
            None,
        )
        if cancelled is not None:
            raise cancelled
        self._raise_cleanup_errors([result for result in results if isinstance(result, Exception)])

    @staticmethod
    def _raise_cleanup_errors(errors: Collection[Exception]) -> None:
        if not errors:
            return
        first = next(iter(errors))
        if isinstance(first, ECNClientError):
            raise first from None
        raise ConnectionError(
            "client cleanup failed",
            operation="client.close",
        ) from None

    def _ensure_ready(self) -> None:
        if not self.is_ready:
            raise NotReadyError(
                f"client is not ready (state={self._state.value})",
                operation="readiness_check",
            )

    def _event_service(self) -> EntityLocationService:
        service = self._entity_location_service
        if service is None:
            raise NotReadyError("entity service is not ready", operation="readiness_check")
        return service

    def _tasks_service(self) -> TaskService:
        service = self._task_service
        if service is None:
            raise NotReadyError("task service is not ready", operation="readiness_check")
        return service

    def _clock_service_or_raise(self) -> ClockService:
        service = self._clock_service
        if service is None:
            raise NotReadyError("clock diagnostic is closed", operation="clock.measure")
        return service

    async def _measure_clock(
        self,
        *,
        samples: int,
        timeout: float | None,
    ) -> ClockReport:
        return await self._clock_service_or_raise().measure(samples=samples, timeout=timeout)

    async def _require_clock_within(
        self,
        *,
        max_offset_seconds: float,
        samples: int,
        timeout: float | None,
    ) -> ClockReport:
        return await self._clock_service_or_raise().require_within(
            max_offset_seconds=max_offset_seconds,
            samples=samples,
            timeout=timeout,
        )

    def _validate_watcher_size(self, buffer_size: int) -> None:
        if buffer_size < 0:
            raise ResourceLimitError(
                "watcher buffer size must be positive",
                operation="watch",
            )
        if buffer_size > self._config.watcher_buffer_size:
            raise ResourceLimitError(
                "watcher buffer exceeds configured watcher_buffer_size",
                operation="watch",
                details={"maximum": self._config.watcher_buffer_size},
            )

    async def _watch_entities(
        self,
        *,
        categories: frozenset[EntityCategory],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[EntityEvent]:
        self._validate_watcher_size(buffer_size)
        return await self._event_service().watch_entities(
            categories=categories,
            integrations=integrations,
            buffer_size=buffer_size,
            delivery_policy=delivery_policy,
        )

    async def _publish_entity(self, entity: Entity) -> PublicationReceipt:
        return await self._event_service().publish_entity(entity)

    async def _watch_locations(
        self,
        *,
        entity_ids: frozenset[UUID],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[LocationEvent]:
        self._validate_watcher_size(buffer_size)
        return await self._event_service().watch_locations(
            entity_ids=entity_ids,
            integrations=integrations,
            buffer_size=buffer_size,
            delivery_policy=delivery_policy,
        )

    def _last_observed_location(
        self,
        entity_id: UUID,
        *,
        integration: str | None,
    ) -> Location | None:
        return self._event_service().last_observed_location(
            entity_id,
            integration=integration,
        )

    async def _wait_for_location_update(
        self,
        entity_id: UUID,
        *,
        integration: str | None,
        timeout: float | None,
    ) -> Location:
        event = await self._wait_for_observed_location_event(
            entity_ids=frozenset({entity_id}),
            integrations=frozenset({integration}) if integration is not None else frozenset(),
            timeout=timeout,
            operation="location.wait_for_update",
        )
        return event.location

    async def _wait_for_terminal_geolocation(
        self,
        *,
        timeout: float | None,
    ) -> LocationEvent:
        return await self._wait_for_observed_location_event(
            entity_ids=frozenset(),
            integrations=frozenset({"terminal-geolocation"}),
            timeout=timeout,
            operation="location.wait_for_terminal_geolocation",
        )

    async def _wait_for_observed_location_event(
        self,
        *,
        entity_ids: frozenset[UUID],
        integrations: frozenset[str],
        timeout: float | None,
        operation: str,
    ) -> LocationEvent:
        stream = await self._event_service().watch_locations(
            entity_ids=entity_ids,
            integrations=integrations,
            buffer_size=1,
            delivery_policy=DeliveryPolicy.LATEST,
        )
        try:
            try:
                wait_timeout = timeout if timeout is not None else self._config.operation_timeout
                async with asyncio.timeout(wait_timeout):
                    return await anext(stream)
            except builtins.TimeoutError:
                raise TimeoutError(
                    "no matching MQTT location update arrived before the timeout",
                    operation=operation,
                ) from None
        finally:
            await stream.aclose()

    async def _publish_location(
        self,
        entity_id: UUID,
        location: Location,
    ) -> PublicationReceipt:
        return await self._event_service().publish_location(entity_id, location)

    async def _register_task(
        self,
        *,
        entity_id: UUID,
        command: str,
        request_model: type[BaseModel],
        result_model: type[BaseModel] | None,
        handler: AnyTaskHandler,
    ) -> TaskRegistration:
        return await self._tasks_service().register(
            entity_id=entity_id,
            command=command,
            request_model=request_model,
            result_model=result_model,
            handler=handler,
        )

    async def _unregister_task(self, registration: TaskRegistration) -> None:
        await self._tasks_service().unregister(registration)

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
    ) -> TaskDispatchResult:
        return await self._tasks_service().send(
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


__all__ = ["ECNClient"]
