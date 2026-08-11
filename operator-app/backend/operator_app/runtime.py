# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Deterministic ECN/browser lifecycle and safe operator actions."""

from __future__ import annotations

import asyncio
import math
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from picogrid_ecn_client import (
    Affiliation,
    AuthorizationError,
    ClientState,
    ConnectionStatus,
    DeliveryError,
    DeliveryPhase,
    DeliveryPolicy,
    ECNClient,
    ECNClientError,
    ECNConfig,
    Entity,
    EntityCategory,
    EntityEvent,
    EntityMetadata,
    EntityStatus,
    EventStream,
    Location,
    LocationEvent,
    OutcomeUnknownError,
    ResourceLimitError,
    TaskAcknowledgement,
    TaskMode,
    TaskRequestContext,
    TaskStatus,
)
from picogrid_ecn_client import (
    TimeoutError as ECNTimeoutError,
)
from picogrid_ecn_client.testing import MockECN
from pydantic import BaseModel

from .api_models import (
    PreparedTaskResponse,
    PrepareTaskRequest,
    SafeConfigurationView,
    TaskConfirmationResponse,
)
from .commands import CommandCatalog, CommandPolicyError, ValidatedTaskPayload
from .hub import BrowserHub
from .settings import OperatorMode, OperatorSettings
from .state import (
    Freshness,
    OperatorConnectionState,
    OperatorSnapshot,
    OperatorState,
    RuntimeHealthView,
    TaskOutcomeView,
    operator_connection_state,
)


class OperatorActionError(RuntimeError):
    """A safe policy or operation failure suitable for an API response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        outcome_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_status = outcome_status


class OperatorCleanupError(RuntimeError):
    """Report static resource classes that did not close after full cleanup."""

    def __init__(self, components: tuple[str, ...]) -> None:
        self.components = components
        super().__init__(f"operator cleanup failed for: {', '.join(components)}")


class _MockEchoResult(BaseModel):
    accepted: bool
    summary: str


_BACKGROUND_JOIN_TIMEOUT_SECONDS = 2.0
_TASK_CONFIRMATION_DEADLINE_SECONDS = 15.0
_INTERNAL_VIEW_ID = UUID(int=0)
_INTERNAL_VIEW_GENERATION = UUID(int=0)


@dataclass(slots=True)
class _PreparedTask:
    token: str
    view_id: UUID
    view_generation: UUID
    target_key: str
    target_entity_id: UUID
    target_integration: str
    target_label: str
    command: str
    payload: dict[str, object]
    request: ValidatedTaskPayload
    mode: TaskMode
    connection_generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _RetiredPreparationProof:
    view_id: UUID
    view_generation: UUID
    expires_at: datetime


@dataclass(slots=True)
class _BrowserViewLease:
    generation: UUID
    connected: bool = True
    active_mutations: int = 0


class OperatorRuntime:
    """Own every client, stream, task, and browser queue for one app lifespan."""

    def __init__(self, settings: OperatorSettings) -> None:
        self.settings = settings
        self.commands = CommandCatalog.load(settings.commands_file)
        self.state = OperatorState(
            maximum_entities=settings.maximum_entities,
            stale_after_seconds=settings.stale_after_seconds,
            diagnostic_limit=settings.diagnostic_limit,
            task_history_limit=settings.task_history_limit,
        )
        self.hub = BrowserHub(
            maximum_clients=settings.maximum_browser_clients,
            queue_size=settings.browser_queue_size,
        )
        self.client: ECNClient | None = None
        self._entity_stream: EventStream[EntityEvent] | None = None
        self._location_stream: EventStream[LocationEvent] | None = None
        self._connection_stream: EventStream[ConnectionStatus] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._notify = asyncio.Event()
        self._mock_stop = asyncio.Event()
        self._running = False
        self._prepared: OrderedDict[str, _PreparedTask] = OrderedDict()
        self._retired_preparations: OrderedDict[str, _RetiredPreparationProof] = OrderedDict()
        self._retired_views: OrderedDict[tuple[UUID, UUID], None] = OrderedDict()
        self._prepared_lock = asyncio.Lock()
        self._active_views: dict[UUID, _BrowserViewLease] = {}
        self._mock: MockECN | None = None
        self._mock_clients: dict[str, ECNClient] = {}
        self._mock_registrations: list[Any] = []
        self._mock_ids: dict[str, UUID] = {}

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._mock_stop.clear()
            configuration = await self._configuration()
            self.client = ECNClient(configuration)
            await self.client.start()
            await self.state.set_connection(self.client.status)
            self._connection_stream = self.client.connection_events()

            try:
                self._entity_stream = await self.client.entities.watch(
                    categories=set(self.settings.categories),
                    integrations=set(self.settings.integrations),
                    buffer_size=self.settings.event_buffer_size,
                    delivery=DeliveryPolicy.LATEST,
                )
            except (AuthorizationError, ResourceLimitError) as error:
                await self._record_watcher_failure("entity", error)
            # This intentionally watches each allowed integration with only the UUID
            # level as '+', so location/PLI-only UUIDs remain visible.
            try:
                self._location_stream = await self.client.locations.watch(
                    integrations=set(self.settings.integrations),
                    buffer_size=self.settings.event_buffer_size,
                    delivery=DeliveryPolicy.LATEST,
                )
            except (AuthorizationError, ResourceLimitError) as error:
                await self._record_watcher_failure("location", error)

            self._running = True
            self._tasks = [
                *(
                    [asyncio.create_task(self._entity_loop(), name="operator-entity-watch")]
                    if self._entity_stream is not None
                    else []
                ),
                *(
                    [asyncio.create_task(self._location_loop(), name="operator-location-watch")]
                    if self._location_stream is not None
                    else []
                ),
                asyncio.create_task(self._connection_loop(), name="operator-connection-state"),
                asyncio.create_task(self._health_loop(), name="operator-health-state"),
                asyncio.create_task(self._broadcast_loop(), name="operator-browser-fanout"),
            ]
            if self.settings.mode is OperatorMode.MOCK:
                self._tasks.append(
                    asyncio.create_task(self._mock_publish_loop(), name="operator-mock-synthetic")
                )
            await self.state.diagnostic(
                "info",
                "runtime_started",
                "operator runtime started with bounded MQTT watchers",
            )
            self.request_broadcast()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._running = False
        cleanup_failures: list[str] = []
        pending = tuple(self._tasks)
        self._tasks.clear()
        self._mock_stop.set()
        watcher_names = {
            "operator-entity-watch",
            "operator-location-watch",
            "operator-connection-state",
        }
        watcher_tasks = tuple(task for task in pending if task.get_name() in watcher_names)
        worker_tasks = tuple(task for task in pending if task.get_name() not in watcher_names)
        for task in worker_tasks:
            if task.get_name() != "operator-mock-synthetic":
                task.cancel()
        unfinished = await self._join_background_tasks(worker_tasks, cleanup_failures)

        async with self._prepared_lock:
            self._prepared.clear()
            self._retired_preparations.clear()
            self._retired_views.clear()
            self._active_views.clear()

        if self._mock_registrations and self._mock_clients:
            target = self._mock_clients.get(self._target_integration())
            if target is not None:
                for registration in tuple(self._mock_registrations):
                    try:
                        await target.tasks.unregister(registration)
                    except Exception:
                        cleanup_failures.append("mock task registration")
            self._mock_registrations.clear()

        streams = (
            ("entity watcher", self._entity_stream),
            ("location watcher", self._location_stream),
            ("connection observer", self._connection_stream),
        )
        for label, stream in streams:
            if stream is None:
                continue
            try:
                async with asyncio.timeout(_BACKGROUND_JOIN_TIMEOUT_SECONDS):
                    await stream.aclose()
            except Exception:
                cleanup_failures.append(label)
        for task in watcher_tasks:
            if not task.done():
                task.cancel()
        unfinished.update(await self._join_background_tasks(watcher_tasks, cleanup_failures))

        if self.client is not None:
            client = self.client
            self.client = None
            try:
                await client.close()
            except Exception:
                cleanup_failures.append("public MQTT client")

        for client in tuple(self._mock_clients.values()):
            try:
                await client.close()
            except Exception:
                cleanup_failures.append("mock MQTT client")
        self._mock_clients.clear()

        if self._mock is not None:
            mock = self._mock
            self._mock = None
            try:
                await mock.close()
            except Exception:
                cleanup_failures.append("mock broker")

        unfinished = await self._join_background_tasks(tuple(unfinished), cleanup_failures)
        for task in unfinished:
            task.cancel()
            task.add_done_callback(self._consume_background_result)
            cleanup_failures.append(f"background task {task.get_name()}")
        self._entity_stream = None
        self._location_stream = None
        self._connection_stream = None
        self._notify.clear()
        try:
            await self.state.clear()
        except Exception:
            cleanup_failures.append("observed state")
        try:
            await self.hub.close()
        except Exception:
            cleanup_failures.append("browser hub")
        if cleanup_failures:
            raise OperatorCleanupError(tuple(dict.fromkeys(cleanup_failures)))

    @staticmethod
    async def _join_background_tasks(
        tasks: tuple[asyncio.Task[None], ...],
        cleanup_failures: list[str],
    ) -> set[asyncio.Task[None]]:
        if not tasks:
            return set()
        finished, unfinished = await asyncio.wait(
            tasks,
            timeout=_BACKGROUND_JOIN_TIMEOUT_SECONDS,
        )
        for task in finished:
            if not task.cancelled() and task.exception() is not None:
                cleanup_failures.append(f"background task {task.get_name()}")
        return unfinished

    @staticmethod
    def _consume_background_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _configuration(self) -> ECNConfig:
        if self.settings.mode is OperatorMode.LIVE:
            return self.settings.live_client_config()

        self._mock = MockECN()
        await self._mock.start()
        for integration in self.settings.integrations:
            client = ECNClient(
                self._mock.client_config(integration).model_copy(
                    update={"wire_format": self.settings.wire_format}
                )
            )
            await client.start()
            self._mock_clients[integration] = client

        self._mock_ids = {
            "track": uuid4(),
            "detection": uuid4(),
            "target": (
                min(self.settings.task_entity_allowlist, key=lambda entity_id: entity_id.int)
                if self.settings.tasking_enabled
                else uuid4()
            ),
            "location_only": uuid4(),
        }
        target_client = self._mock_clients[self._target_integration()]

        async def echo_handler(
            context: TaskRequestContext, request: ValidatedTaskPayload
        ) -> _MockEchoResult:
            if context.source != "local":
                raise RuntimeError("mock received a non-local task")
            await asyncio.sleep(0.05)
            size = len(request.model_dump_json().encode())
            return _MockEchoResult(accepted=True, summary=f"accepted {size} bytes")

        if self.settings.tasking_enabled:
            for command in self.commands.registration_names(self._target_integration()):
                self._mock_registrations.append(
                    await target_client.tasks.register(
                        entity_id=self._mock_ids["target"],
                        command=command,
                        request_model=ValidatedTaskPayload,
                        result_model=_MockEchoResult,
                        handler=echo_handler,
                    )
                )
        return self._mock.client_config(self.settings.client_integration).model_copy(
            update={"wire_format": self.settings.wire_format}
        )

    def _target_integration(self) -> str:
        return self.settings.integrations[-1]

    async def _entity_loop(self) -> None:
        stream = self._entity_stream
        assert stream is not None
        try:
            async for event in stream:
                # A target that crossed the freshness boundary must not regain
                # eligibility while an older preparation survives.
                await self._invalidate_prepared_target_if_ineligible(
                    integration=event.entity.integration,
                    entity_id=event.entity.id,
                )
                await self.state.observe_entity(event)
                self.request_broadcast()
        except asyncio.CancelledError:
            raise
        except ECNClientError as error:
            await self._record_watcher_failure("entity", error)
        finally:
            # During runtime shutdown the public client's bounded close owns
            # watcher teardown. Avoid racing a separate UNSUBSCRIBE against it.
            if self._running:
                await stream.aclose()
            await self.discard_prepared_task()

    async def _location_loop(self) -> None:
        stream = self._location_stream
        assert stream is not None
        try:
            async for event in stream:
                await self.state.observe_location(event)
                self.request_broadcast()
        except asyncio.CancelledError:
            raise
        except ECNClientError as error:
            await self._record_watcher_failure("location", error)
        finally:
            if self._running:
                await stream.aclose()
            await self.discard_prepared_task()

    async def _record_watcher_failure(
        self,
        watcher: Literal["entity", "location"],
        error: ECNClientError,
    ) -> None:
        """Persist only fixed watcher failure classes suitable for operators."""

        if isinstance(error, AuthorizationError):
            terminal_state = OperatorConnectionState.SUBSCRIPTION_DENIED
            code = "subscription_denied"
            message = f"{watcher} watcher subscription was denied"
        elif isinstance(error, ResourceLimitError):
            terminal_state = OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED
            code = "subscription_resource_limited"
            message = f"{watcher} watcher subscription was resource-limited"
        else:
            terminal_state = None
            code = error.code
            message = f"{watcher} watcher stopped"
        if terminal_state is not None:
            await self.state.set_watcher_terminal_state(watcher, terminal_state)
        await self.state.diagnostic("error", code, message)
        self.request_broadcast()

    async def _connection_loop(self) -> None:
        stream = self._connection_stream
        assert stream is not None
        previous_ready: bool | None = None
        previous_generation: int | None = None
        previous_summary: str | None = None
        try:
            async for status in stream:
                await self._invalidate_ineligible_prepared()
                await self._apply_connection_status(
                    status,
                    previous_ready=previous_ready,
                    previous_generation=previous_generation,
                    previous_summary=previous_summary,
                )
                previous_ready = status.ready
                previous_generation = status.connection_generation
                previous_summary = operator_connection_state(status).value
        except asyncio.CancelledError:
            raise
        except ECNClientError:
            await self._record_connection_observer_stopped()
        else:
            await self._record_connection_observer_stopped()
        finally:
            if self._running:
                await stream.aclose()
            await self.discard_prepared_task()

    async def _record_connection_observer_stopped(self) -> None:
        """Surface unexpected observer exhaustion without leaking broker detail."""

        client = self.client
        if not self._running or client is None:
            return
        current = client.status
        disconnected = current.model_copy(
            update={
                "state": ClientState.RECONNECTING,
                "ready": False,
                "mqtt_connected": False,
                "changed_at": datetime.now(UTC),
            }
        )
        await self.state.set_connection(disconnected)
        await self.state.diagnostic(
            "error",
            "connection_observer_stopped",
            "ECN connection observer stopped",
        )
        self.request_broadcast()

    async def _apply_connection_status(
        self,
        status: ConnectionStatus,
        *,
        previous_ready: bool | None,
        previous_generation: int | None,
        previous_summary: str | None,
    ) -> None:
        """Persist one redacted event and enforce reconnect task safety."""

        changed = await self.state.set_connection(status)
        readiness_was_lost = bool(previous_ready and not status.ready)
        connection_was_replaced = bool(
            previous_generation is not None and status.connection_generation != previous_generation
        )
        if readiness_was_lost or connection_was_replaced:
            async with self._prepared_lock:
                invalidated = len(self._prepared)
                now = datetime.now(UTC)
                for token in tuple(self._prepared):
                    self._retire_preparation_locked(token, now=now)
            if invalidated:
                await self.state.diagnostic(
                    "warning",
                    "task_preparations_invalidated",
                    "prepared tasks were discarded when ECN connection readiness changed",
                )
        summary = operator_connection_state(status).value
        if summary != previous_summary:
            await self.state.diagnostic(
                "info" if status.ready else "warning",
                f"connection_{summary.replace(' ', '_')}",
                f"ECN connection is {summary}",
            )
        if changed:
            self.request_broadcast()

    async def _health_loop(self) -> None:
        previous_drops = (0, 0, 0)
        previous_decode_errors = (0, 0)
        while True:
            await asyncio.sleep(0.5)
            if not self._running:
                return
            await self._invalidate_ineligible_prepared()
            health = self._health()
            drops = (
                health.entity_dropped_events,
                health.location_dropped_events,
                health.browser_dropped_updates,
            )
            if drops != previous_drops:
                await self.state.diagnostic(
                    "warning",
                    "bounded_update_drop",
                    "one or more bounded observer/browser queues dropped older updates",
                )
                previous_drops = drops
                self.request_broadcast()
            decode_errors = (health.entity_decode_errors, health.location_decode_errors)
            if decode_errors != previous_decode_errors:
                await self.state.diagnostic(
                    "warning",
                    "payload_decode_error",
                    "one or more bounded watchers rejected an invalid MQTT payload",
                )
                previous_decode_errors = decode_errors
                self.request_broadcast()

    def request_broadcast(self) -> None:
        self._notify.set()

    async def _broadcast_loop(self) -> None:
        while True:
            await self._notify.wait()
            self._notify.clear()
            await asyncio.sleep(0.1)
            snapshot = await self.snapshot()
            await self.hub.broadcast(snapshot.model_dump_json(serialize_as_any=True))

    async def snapshot(self) -> OperatorSnapshot:
        snapshot = await self.state.snapshot()
        return snapshot.model_copy(update={"health": self._health()})

    def _health(self) -> RuntimeHealthView:
        return RuntimeHealthView(
            entity_watcher_active=(
                self._entity_stream is not None and not self._entity_stream.closed
            ),
            location_watcher_active=(
                self._location_stream is not None and not self._location_stream.closed
            ),
            entity_scope_pairs=len(self.settings.categories) * len(self.settings.integrations),
            location_scope_filters=2 * len(self.settings.integrations),
            entity_dropped_events=(
                self._entity_stream.dropped_count if self._entity_stream is not None else 0
            ),
            location_dropped_events=(
                self._location_stream.dropped_count if self._location_stream is not None else 0
            ),
            entity_decode_errors=(
                self._entity_stream.decode_error_count if self._entity_stream is not None else 0
            ),
            location_decode_errors=(
                self._location_stream.decode_error_count if self._location_stream is not None else 0
            ),
            browser_clients=self.hub.client_count,
            browser_dropped_updates=self.hub.dropped_messages,
        )

    def safe_configuration(self) -> SafeConfigurationView:
        return SafeConfigurationView(
            mode=self.settings.mode.value,
            read_only=not self.settings.tasking_enabled,
            tasking_enabled=self.settings.tasking_enabled,
            integrations=self.settings.integrations,
            categories=tuple(category.value for category in self.settings.categories),
            stale_after_seconds=self.settings.stale_after_seconds,
            maximum_entities=self.settings.maximum_entities,
            commands=tuple(self.commands.public_inventory()),
            basemap_url_template=self.settings.basemap_url_template,
            basemap_attribution=self.settings.basemap_attribution,
        )

    async def activate_browser_view(self, view_id: UUID, view_generation: UUID) -> bool:
        """Ensure a view identity backs at most one live state connection."""

        async with self._prepared_lock:
            if (
                not self._running
                or view_id == _INTERNAL_VIEW_ID
                or view_generation == _INTERNAL_VIEW_GENERATION
                or view_id in self._active_views
            ):
                return False
            # A duplicate connection could otherwise preserve prepared tokens after
            # the owning socket closes, defeating the browser's fail-closed dismissal.
            self._active_views[view_id] = _BrowserViewLease(generation=view_generation)
            self._retired_views.pop((view_id, view_generation), None)
            return True

    def _retire_view_preparations_locked(
        self,
        view_id: UUID,
        view_generation: UUID,
    ) -> int:
        tokens = tuple(
            token
            for token, prepared in self._prepared.items()
            if prepared.view_id == view_id and prepared.view_generation == view_generation
        )
        now = datetime.now(UTC)
        for token in tokens:
            self._retire_preparation_locked(token, now=now)
        return len(tokens)

    def _remember_retired_view_locked(self, view_id: UUID, view_generation: UUID) -> None:
        proof = (view_id, view_generation)
        self._retired_views[proof] = None
        self._retired_views.move_to_end(proof)
        while len(self._retired_views) > self.settings.maximum_browser_clients:
            self._retired_views.popitem(last=False)

    async def deactivate_browser_view(self, view_id: UUID, view_generation: UUID) -> int:
        """Release a browser view and invalidate all of its prepared tasks."""

        async with self._prepared_lock:
            lease = self._active_views.get(view_id)
            if lease is None or lease.generation != view_generation or not lease.connected:
                discarded = 0
            else:
                lease.connected = False
                discarded = self._retire_view_preparations_locked(view_id, view_generation)
                self._remember_retired_view_locked(view_id, view_generation)
                if lease.active_mutations == 0:
                    self._active_views.pop(view_id)
        await self._record_preparation_discard(discarded)
        return discarded

    async def retire_browser_view(self, view_id: UUID, view_generation: UUID) -> int:
        """Acknowledge exact browser-view retirement without touching ECN transport."""

        async with self._prepared_lock:
            lease = self._active_views.get(view_id)
            if lease is None:
                if (view_id, view_generation) not in self._retired_views:
                    raise OperatorActionError(
                        "operator browser view retirement is not proven",
                        status_code=409,
                    )
                self._retired_views.move_to_end((view_id, view_generation))
                discarded = 0
            elif lease.generation != view_generation:
                raise OperatorActionError(
                    "operator browser view generation does not match the active view",
                    status_code=409,
                )
            elif lease.active_mutations != 0:
                raise OperatorActionError(
                    "operator browser view has an active task mutation",
                    status_code=409,
                )
            else:
                lease.connected = False
                discarded = self._retire_view_preparations_locked(view_id, view_generation)
                self._remember_retired_view_locked(view_id, view_generation)
                self._active_views.pop(view_id)
        await self._record_preparation_discard(discarded)
        return discarded

    def _browser_view_is_active_locked(
        self,
        view_id: UUID | None,
        view_generation: UUID | None,
    ) -> bool:
        if view_id is None:
            return view_generation is None
        lease = self._active_views.get(view_id)
        return bool(
            view_generation is not None
            and lease is not None
            and lease.connected
            and lease.generation == view_generation
        )

    @asynccontextmanager
    async def _browser_mutation(
        self,
        view_id: UUID | None,
        view_generation: UUID | None,
    ) -> AsyncIterator[None]:
        if view_id is None:
            if view_generation is not None:
                raise OperatorActionError(
                    "operator browser view identity is incomplete",
                    status_code=409,
                )
            yield
            return
        if view_generation is None:
            raise OperatorActionError(
                "operator browser view identity is incomplete",
                status_code=409,
            )
        async with self._prepared_lock:
            lease = self._active_views.get(view_id)
            if lease is None or not lease.connected or lease.generation != view_generation:
                raise OperatorActionError(
                    "operator browser view is disconnected; nothing was published",
                    status_code=409,
                )
            lease.active_mutations += 1
        try:
            yield
        finally:
            async with self._prepared_lock:
                current = self._active_views.get(view_id)
                if current is lease:
                    current.active_mutations -= 1
                    if current.active_mutations == 0 and not current.connected:
                        self._active_views.pop(view_id)

    async def prepare_task(
        self,
        request: PrepareTaskRequest,
        *,
        view_id: UUID | None = None,
        view_generation: UUID | None = None,
    ) -> PreparedTaskResponse:
        if not self.settings.tasking_enabled:
            raise OperatorActionError("tasking is disabled by deployment policy", status_code=403)
        async with self._browser_mutation(view_id, view_generation):
            return await self._prepare_task(
                request,
                view_id=view_id,
                view_generation=view_generation,
            )

    async def _prepare_task(
        self,
        request: PrepareTaskRequest,
        *,
        view_id: UUID | None,
        view_generation: UUID | None,
    ) -> PreparedTaskResponse:
        if not self.settings.tasking_enabled:
            raise OperatorActionError("tasking is disabled by deployment policy", status_code=403)
        client = self.client
        health = self._health()
        if (
            client is None
            or not client.is_ready
            or not health.entity_watcher_active
            or not health.location_watcher_active
        ):
            raise OperatorActionError(
                "MQTT observers are not ready",
                status_code=503,
                outcome_status="RECONNECT",
            )
        connection_generation = client.status.connection_generation
        if request.integration not in self.settings.integrations:
            raise OperatorActionError("target integration is not allowlisted", status_code=403)
        target = await self.state.task_target(
            integration=request.integration,
            entity_id=request.entity_id,
        )
        if target is None or target.location_only:
            raise OperatorActionError("target entity has not been observed", status_code=404)
        if target.entity_freshness is not Freshness.FRESH:
            raise OperatorActionError("target entity observation is stale", status_code=409)
        if target.entity_id not in self.settings.task_entity_allowlist:
            raise OperatorActionError("target entity UUID is not allowlisted", status_code=403)
        try:
            validated = self.commands.validate(
                command_name=request.command,
                integration=request.integration,
                payload=request.payload,
            )
        except CommandPolicyError as error:
            raise OperatorActionError(str(error), status_code=422) from None
        mode = self.commands.mode_for(request.command)

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.prepare_ttl_seconds)
        label = target.name or target.type or str(target.entity_id)
        prepared = _PreparedTask(
            token=token,
            view_id=view_id if view_id is not None else _INTERNAL_VIEW_ID,
            view_generation=(
                view_generation if view_generation is not None else _INTERNAL_VIEW_GENERATION
            ),
            target_key=target.key,
            target_entity_id=target.entity_id,
            target_integration=target.integration,
            target_label=label,
            command=request.command,
            payload=request.payload,
            request=validated,
            mode=mode,
            connection_generation=connection_generation,
            expires_at=expires_at,
        )
        async with self._prepared_lock:
            current_health = self._health()
            if (
                self.client is not client
                or not client.is_ready
                or client.status.connection_generation != connection_generation
                or not current_health.entity_watcher_active
                or not current_health.location_watcher_active
            ):
                raise OperatorActionError(
                    "MQTT connection changed during preparation; prepare again",
                    status_code=409,
                )
            if not self._browser_view_is_active_locked(view_id, view_generation):
                raise OperatorActionError(
                    "operator browser view is disconnected; nothing was published",
                    status_code=409,
                )
            self._expire_prepared(datetime.now(UTC))
            self._prepared[token] = prepared
            while len(self._prepared) > self.settings.prepared_task_limit:
                oldest = next(iter(self._prepared))
                self._retire_preparation_locked(oldest, now=datetime.now(UTC))
        current = await self.state.task_target(
            integration=target.integration,
            entity_id=target.entity_id,
        )
        eligible = (
            current is not None
            and current.key == target.key
            and not current.location_only
            and current.entity_freshness is Freshness.FRESH
        )
        async with self._prepared_lock:
            still_present = self._prepared.get(token) is prepared
            current_health = self._health()
            connection_unchanged = (
                self.client is client
                and client.is_ready
                and client.status.connection_generation == connection_generation
                and current_health.entity_watcher_active
                and current_health.location_watcher_active
            )
            view_unchanged = self._browser_view_is_active_locked(
                view_id,
                view_generation,
            )
            if not eligible or not still_present or not connection_unchanged or not view_unchanged:
                self._retire_preparation_locked(token, now=datetime.now(UTC))
        if not eligible or not still_present or not connection_unchanged or not view_unchanged:
            await self._record_preparation_discard(int(still_present))
            raise OperatorActionError(
                "target, MQTT connection, or browser view changed during preparation; prepare again",
                status_code=409,
            )
        await self.state.diagnostic(
            "info", "task_prepared", "a task awaits explicit operator confirmation"
        )
        self.request_broadcast()
        return PreparedTaskResponse(
            preparation_token=token,
            expires_at=expires_at,
            target_key=target.key,
            target_label=label,
            command=request.command,
            mode=mode.value,
            payload=request.payload,
            warning=(
                "Confirming performs one MQTT task dispatch and waits for exactly one "
                "acknowledgment. Handler completion is intentionally not reported, and the "
                "dispatch is not retried automatically."
                if mode is TaskMode.ACKNOWLEDGMENT
                else "Confirming performs one MQTT task dispatch and waits for its final "
                "response. The dispatch is not retried automatically."
            ),
        )

    async def confirm_task(
        self,
        token: str,
        *,
        view_id: UUID | None = None,
        view_generation: UUID | None = None,
    ) -> TaskConfirmationResponse:
        if not self.settings.tasking_enabled:
            raise OperatorActionError("tasking is disabled by deployment policy", status_code=403)
        async with self._browser_mutation(view_id, view_generation):
            return await self._confirm_task(
                token,
                view_id=view_id,
                view_generation=view_generation,
            )

    async def _confirm_task(
        self,
        token: str,
        *,
        view_id: UUID | None,
        view_generation: UUID | None,
    ) -> TaskConfirmationResponse:
        if not self.settings.tasking_enabled:
            raise OperatorActionError("tasking is disabled by deployment policy", status_code=403)
        async with self._prepared_lock:
            now = datetime.now(UTC)
            self._expire_prepared(now)
            candidate = self._prepared.get(token)
            expected_view = view_id if view_id is not None else _INTERNAL_VIEW_ID
            expected_view_generation = (
                view_generation if view_generation is not None else _INTERNAL_VIEW_GENERATION
            )
            matches_view = bool(
                candidate is not None
                and candidate.view_id == expected_view
                and candidate.view_generation == expected_view_generation
            )
            inactive_browser_view = not self._browser_view_is_active_locked(
                view_id,
                view_generation,
            )
            current_generation = (
                self.client.status.connection_generation if self.client is not None else None
            )
            generation_changed = bool(
                matches_view
                and candidate is not None
                and current_generation != candidate.connection_generation
            )
            if inactive_browser_view:
                if matches_view:
                    self._retire_preparation_locked(token, now=now)
                prepared = None
            elif generation_changed:
                assert candidate is not None
                self._retire_preparation_locked(token, now=now)
                prepared = None
            else:
                prepared = self._prepared.pop(token) if matches_view else None
        if inactive_browser_view:
            raise OperatorActionError(
                "operator browser view is disconnected; nothing was published",
                status_code=409,
            )
        if generation_changed:
            assert candidate is not None
            await self._record_task_generation_change(candidate)
            raise OperatorActionError(
                "MQTT connection changed; nothing was published and a fresh prepare is required",
                status_code=409,
                outcome_status="RECONNECT",
            )
        if prepared is None:
            raise OperatorActionError("preparation token is invalid or expired", status_code=409)

        target = await self.state.task_target(
            integration=prepared.target_integration,
            entity_id=prepared.target_entity_id,
        )
        if target is None or target.location_only or target.entity_freshness is not Freshness.FRESH:
            raise OperatorActionError("target is no longer eligible for tasking", status_code=409)
        if target.entity_id not in self.settings.task_entity_allowlist:
            raise OperatorActionError(
                "target entity UUID is no longer allowlisted", status_code=403
            )
        health = self._health()
        client = self.client
        if (
            not self._running
            or client is None
            or not client.is_ready
            or not health.entity_watcher_active
            or not health.location_watcher_active
        ):
            await self.state.add_task_outcome(
                TaskOutcomeView(
                    task_id=None,
                    target_key=prepared.target_key,
                    command=prepared.command,
                    mode=prepared.mode.value,
                    status="RECONNECT",
                    detail="MQTT was not ready; nothing was published and a fresh prepare is required",
                    completed_at=datetime.now(UTC),
                )
            )
            await self.state.diagnostic(
                "warning", "task_reconnect_required", "task dispatch requires MQTT reconnection"
            )
            self.request_broadcast()
            raise OperatorActionError(
                "MQTT client is not ready",
                status_code=503,
                outcome_status="RECONNECT",
            )

        try:
            async with asyncio.timeout(_TASK_CONFIRMATION_DEADLINE_SECONDS):
                result = await client.tasks.send(
                    target_entity_id=prepared.target_entity_id,
                    target_integration=prepared.target_integration,
                    command=prepared.command,
                    request=prepared.request,
                    timeout=_TASK_CONFIRMATION_DEADLINE_SECONDS,
                    mode=prepared.mode,
                    expected_connection_generation=prepared.connection_generation,
                )
        except TimeoutError:
            detail = (
                "task exchange timed out before publication was proven; nothing was published "
                "and the task was not retried"
            )
            await self.state.add_task_outcome(
                TaskOutcomeView(
                    task_id=None,
                    target_key=prepared.target_key,
                    command=prepared.command,
                    mode=prepared.mode.value,
                    status="TIMEOUT",
                    detail=detail,
                    completed_at=datetime.now(UTC),
                )
            )
            await self.state.diagnostic("error", "timeout", "task dispatch timed out")
            self.request_broadcast()
            raise OperatorActionError(
                "task dispatch timed out",
                status_code=504,
                outcome_status="TIMEOUT",
            ) from None
        except asyncio.CancelledError:
            await self.state.add_task_outcome(
                TaskOutcomeView(
                    task_id=None,
                    target_key=prepared.target_key,
                    command=prepared.command,
                    mode=prepared.mode.value,
                    status="CANCELLED",
                    detail="task exchange was cancelled and was not retried",
                    completed_at=datetime.now(UTC),
                )
            )
            await self.state.diagnostic("warning", "task_cancelled", "task dispatch was cancelled")
            self.request_broadcast()
            raise
        except OutcomeUnknownError as error:
            phase = error.delivery_phase.value
            correlation = (
                f"task ID {error.task_id}" if error.task_id is not None else "task ID unavailable"
            )
            detail = (
                f"task outcome is unknown at {phase}; {correlation}; do not retry automatically"
            )
            await self.state.add_task_outcome(
                TaskOutcomeView(
                    task_id=error.task_id,
                    target_key=prepared.target_key,
                    command=prepared.command,
                    mode=prepared.mode.value,
                    status="OUTCOME_UNKNOWN",
                    detail=detail,
                    completed_at=datetime.now(UTC),
                )
            )
            await self.state.diagnostic(
                "error",
                error.code,
                "task outcome is unknown; inspect the retained task correlation before retrying",
            )
            self.request_broadcast()
            raise OperatorActionError(
                detail,
                status_code=409,
                outcome_status="OUTCOME_UNKNOWN",
            ) from None
        except ECNClientError as error:
            if (
                isinstance(error, DeliveryError)
                and error.delivery_phase is DeliveryPhase.NOT_SENT
                and (
                    not client.is_ready
                    or client.status.connection_generation != prepared.connection_generation
                )
            ):
                await self._record_task_generation_change(prepared)
                raise OperatorActionError(
                    "MQTT connection changed; nothing was published and a fresh prepare is "
                    "required",
                    status_code=409,
                    outcome_status="RECONNECT",
                ) from None
            timed_out = isinstance(error, ECNTimeoutError)
            await self.state.add_task_outcome(
                TaskOutcomeView(
                    task_id=None,
                    target_key=prepared.target_key,
                    command=prepared.command,
                    mode=prepared.mode.value,
                    status="TIMEOUT" if timed_out else "FAILED",
                    detail=(
                        "task exchange timed out and was not retried"
                        if timed_out
                        else "task exchange failed and was not retried"
                    ),
                    completed_at=datetime.now(UTC),
                )
            )
            await self.state.diagnostic("error", error.code, "task dispatch failed")
            self.request_broadcast()
            raise OperatorActionError(
                "task dispatch timed out" if timed_out else "task dispatch failed",
                status_code=504 if timed_out else 502,
                outcome_status="TIMEOUT" if timed_out else "FAILED",
            ) from None

        task_id = result.task_id
        if isinstance(result, TaskAcknowledgement):
            status = "ACK"
            detail = (
                "exactly one acknowledgment was received; handler completion is intentionally "
                "not reported"
            )
        else:
            raw_status = getattr(result, "status", None)
            status = raw_status.value if isinstance(raw_status, TaskStatus) else "UNKNOWN"
            detail = "task exchange completed without automatic retry"
        completed_at = datetime.now(UTC)
        outcome = TaskOutcomeView(
            task_id=task_id,
            target_key=prepared.target_key,
            command=prepared.command,
            mode=prepared.mode.value,
            status=status,
            detail=detail,
            completed_at=completed_at,
        )
        await self.state.add_task_outcome(outcome)
        await self.state.diagnostic("info", "task_completed", "confirmed task exchange completed")
        self.request_broadcast()
        return TaskConfirmationResponse(**outcome.model_dump())

    async def _record_task_generation_change(self, prepared: _PreparedTask) -> None:
        await self.state.add_task_outcome(
            TaskOutcomeView(
                task_id=None,
                target_key=prepared.target_key,
                command=prepared.command,
                mode=prepared.mode.value,
                status="RECONNECT",
                detail=(
                    "MQTT connection changed after preparation; nothing was published and "
                    "a fresh prepare is required"
                ),
                completed_at=datetime.now(UTC),
            )
        )
        await self.state.diagnostic(
            "warning",
            "task_generation_changed",
            "task dispatch requires a fresh preparation after MQTT reconnection",
        )
        self.request_broadcast()

    async def _invalidate_prepared_target_if_ineligible(
        self,
        *,
        integration: str,
        entity_id: UUID,
    ) -> int:
        """Permanently remove preparations after a target loses eligibility."""

        target = await self.state.task_target(integration=integration, entity_id=entity_id)
        if (
            target is not None
            and not target.location_only
            and target.entity_freshness is Freshness.FRESH
        ):
            return 0
        async with self._prepared_lock:
            now = datetime.now(UTC)
            tokens = tuple(
                token
                for token, prepared in self._prepared.items()
                if prepared.target_integration == integration
                and prepared.target_entity_id == entity_id
            )
            for token in tokens:
                self._retire_preparation_locked(token, now=now)
        await self._record_preparation_discard(len(tokens))
        return len(tokens)

    async def _invalidate_ineligible_prepared(self) -> int:
        async with self._prepared_lock:
            candidates = tuple(self._prepared.items())
        invalid: list[tuple[str, _PreparedTask]] = []
        for token, prepared in candidates:
            target = await self.state.task_target(
                integration=prepared.target_integration,
                entity_id=prepared.target_entity_id,
            )
            if (
                target is None
                or target.location_only
                or target.entity_freshness is not Freshness.FRESH
            ):
                invalid.append((token, prepared))
        discarded = 0
        async with self._prepared_lock:
            now = datetime.now(UTC)
            for token, prepared in invalid:
                if self._prepared.get(token) is prepared:
                    self._retire_preparation_locked(token, now=now)
                    discarded += 1
        await self._record_preparation_discard(discarded)
        return discarded

    async def _record_preparation_discard(self, discarded: int) -> None:
        if discarded:
            await self.state.diagnostic(
                "info",
                "task_preparation_discarded",
                "one or more prepared tasks were invalidated before publication",
            )
            self.request_broadcast()

    async def discard_prepared_task(
        self,
        token: str | None = None,
        *,
        view_id: UUID | None = None,
        view_generation: UUID | None = None,
    ) -> int:
        return await self._discard_prepared_task(
            token,
            view_id=view_id,
            view_generation=view_generation,
        )

    async def _discard_prepared_task(
        self,
        token: str | None,
        *,
        view_id: UUID | None,
        view_generation: UUID | None,
    ) -> int:
        """Invalidate one view's preparation, or all after MQTT-state loss."""

        newly_discarded = 0
        async with self._prepared_lock:
            now = datetime.now(UTC)
            self._expire_retired_preparations(now)
            if token is None and view_id is None:
                discarded = len(self._prepared)
                for candidate in tuple(self._prepared):
                    self._retire_preparation_locked(candidate, now=now)
                newly_discarded = discarded
            elif token is None:
                tokens = tuple(
                    candidate
                    for candidate, prepared in self._prepared.items()
                    if prepared.view_id == view_id
                    and (view_generation is None or prepared.view_generation == view_generation)
                )
                for candidate in tokens:
                    self._retire_preparation_locked(candidate, now=now)
                discarded = len(tokens)
                newly_discarded = discarded
            else:
                prepared = self._prepared.get(token)
                matches_view = prepared is not None and (
                    view_id is None
                    or (prepared.view_id == view_id and prepared.view_generation == view_generation)
                )
                if matches_view:
                    self._retire_preparation_locked(token, now=now)
                    newly_discarded = 1
                retired = self._retired_preparations.get(token)
                confirmed_retired = retired is not None and (
                    view_id is None
                    or (retired.view_id == view_id and retired.view_generation == view_generation)
                )
                discarded = int(matches_view or confirmed_retired)
        await self._record_preparation_discard(newly_discarded)
        return discarded

    def _expire_prepared(self, now: datetime) -> None:
        self._expire_retired_preparations(now)
        expired = [token for token, task in self._prepared.items() if task.expires_at <= now]
        for token in expired:
            self._retire_preparation_locked(token, now=now)

    def _retire_preparation_locked(self, token: str, *, now: datetime) -> bool:
        prepared = self._prepared.pop(token, None)
        if prepared is None:
            return False
        self._retired_preparations[token] = _RetiredPreparationProof(
            view_id=prepared.view_id,
            view_generation=prepared.view_generation,
            expires_at=now + timedelta(seconds=self.settings.prepare_ttl_seconds),
        )
        self._retired_preparations.move_to_end(token)
        while len(self._retired_preparations) > self.settings.prepared_task_limit:
            self._retired_preparations.popitem(last=False)
        return True

    def _expire_retired_preparations(self, now: datetime) -> None:
        expired = [
            token for token, proof in self._retired_preparations.items() if proof.expires_at <= now
        ]
        for token in expired:
            self._retired_preparations.pop(token, None)

    async def _mock_publish_loop(self) -> None:
        tick = 0
        while not self._mock_stop.is_set():
            try:
                await self._publish_mock_frame(tick)
            except asyncio.CancelledError:
                raise
            except ECNClientError as error:
                await self.state.diagnostic("error", error.code, "mock synthetic feed stopped")
                self.request_broadcast()
                return
            tick += 1
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._mock_stop.wait(),
                    timeout=self.settings.synthetic_period_seconds,
                )

    async def _publish_mock_frame(self, tick: int) -> None:
        sensor_integration = self.settings.integrations[0]
        target_integration = self._target_integration()
        sensor = self._mock_clients[sensor_integration]
        target = self._mock_clients[target_integration]
        now = datetime.now(UTC)
        phase = tick / 12
        track_location = Location(
            latitude=34.05 + math.sin(phase) * 0.025,
            longitude=-118.24 + math.cos(phase) * 0.035,
            altitude=250.0,
            bearing=(tick * 7.0) % 360,
            recorded_at=now,
            source="synthetic-mock",
        )
        detection_location = Location(
            latitude=34.075 + math.sin(phase * 0.7) * 0.012,
            longitude=-118.20 + math.cos(phase * 0.7) * 0.012,
            recorded_at=now,
            source="synthetic-mock",
        )
        target_location = Location(
            latitude=34.025,
            longitude=-118.275,
            recorded_at=now,
            source="synthetic-mock",
        )
        location_only = Location(
            latitude=34.0 + math.sin(phase * 0.4) * 0.008,
            longitude=-118.31 + math.cos(phase * 0.4) * 0.008,
            recorded_at=now,
            source="synthetic-pli-only",
        )
        await sensor.entities.publish(
            Entity(
                id=self._mock_ids["track"],
                category=EntityCategory.TRACK,
                integration=sensor_integration,
                recorded_at=now,
                type="synthetic-track",
                name="Synthetic moving track",
                status=EntityStatus.ACTIVE,
                affiliation=Affiliation.FRIEND,
                metadata=EntityMetadata(properties={"synthetic": True, "sequence": tick}),
                position=track_location,
            )
        )
        await sensor.entities.publish(
            Entity(
                id=self._mock_ids["detection"],
                category=EntityCategory.DETECTION,
                integration=sensor_integration,
                recorded_at=now,
                type="synthetic-detection",
                name="Synthetic <strong data-xss-canary>markup</strong> detection",
                status=EntityStatus.ACTIVE,
                affiliation=Affiliation.SUSPECT,
                metadata=EntityMetadata(properties={"confidence": 0.82, "synthetic": True}),
                position=detection_location,
            )
        )
        await target.entities.publish(
            Entity(
                id=self._mock_ids["target"],
                category=EntityCategory.DEVICE,
                integration=target_integration,
                recorded_at=now,
                type="synthetic-task-target",
                name="Synthetic task target",
                status=EntityStatus.ACTIVE,
                affiliation=Affiliation.FRIEND,
                metadata=EntityMetadata(properties={"task_capable": True, "synthetic": True}),
                position=target_location,
            )
        )
        await sensor.locations.publish(entity_id=self._mock_ids["track"], location=track_location)
        await sensor.locations.publish(
            entity_id=self._mock_ids["location_only"], location=location_only
        )


__all__ = ["OperatorActionError", "OperatorCleanupError", "OperatorRuntime"]
