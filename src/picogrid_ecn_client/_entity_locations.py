# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Private lazy MQTT entity/location observation and publication coordination."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from ._protocol import (
    build_entity_subscription_filters,
    build_location_subscription_filters,
    extract_entity_identity,
    parse_entity_topic,
)
from ._transport.mqtt import _PublishCompletion
from .exceptions import (
    AuthorizationError,
    DeliveryError,
    ECNClientError,
    NotReadyError,
    OutcomeUnknownError,
    ResourceLimitError,
    ValidationError,
)
from .models import (
    DeliveryPhase,
    DeliveryPolicy,
    Entity,
    EntityCategory,
    EntityEvent,
    Location,
    LocationEvent,
    PublicationKind,
    PublicationReceipt,
)
from .streams import EventStream

logger = logging.getLogger(__name__)

_DEFAULT_PENDING_TRACK_BYTE_CEILING = 16 * 1024 * 1024

MessageCallback = Callable[[str, bytes], Awaitable[None]]
EntityDecoder = Callable[[str, bytes], EntityEvent | None]
LocationDecoder = Callable[[str, bytes], LocationEvent | None]
EntityEncoder = Callable[[Entity], tuple[str, bytes, int]]
LocationEncoder = Callable[[UUID, str, Location], tuple[str, bytes, int]]
RestoreFailure = AuthorizationError | ResourceLimitError
RestoreFailureCallback = Callable[[RestoreFailure], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class _EntityWatcher:
    stream: EventStream[EntityEvent]
    categories: frozenset[EntityCategory]
    integrations: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PendingTrack:
    topic: str
    payload: bytes
    watchers: tuple[_EntityWatcher, ...]


class MessageTransport(Protocol):
    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int,
    ) -> _PublishCompletion | None: ...

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
    ) -> object: ...

    async def unsubscribe(self, handle: object) -> None: ...


class EntityLocationService:
    """Open only caller-bounded subscriptions and retain locally observed locations."""

    def __init__(
        self,
        transport: MessageTransport,
        *,
        integration_name: str,
        default_buffer_size: int,
        maximum_payload_size: int,
        decode_entity: EntityDecoder,
        decode_location: LocationDecoder,
        encode_entity: EntityEncoder,
        encode_location: LocationEncoder,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._integration_name = integration_name
        self._default_buffer_size = default_buffer_size
        self._maximum_payload_size = maximum_payload_size
        self._decode_entity = decode_entity
        self._decode_location = decode_location
        self._encode_entity = encode_entity
        self._encode_location = encode_location
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entity_watchers: list[_EntityWatcher] = []
        self._location_watchers: list[EventStream[LocationEvent]] = []
        self._observed_locations: dict[tuple[UUID, str], LocationEvent] = {}
        self._last_observed_by_entity: dict[UUID, LocationEvent] = {}
        self._pending_tracks: OrderedDict[tuple[str, UUID], _PendingTrack] = OrderedDict()
        self._pending_track_limit = default_buffer_size
        self._pending_track_byte_limit = min(
            default_buffer_size * maximum_payload_size,
            max(_DEFAULT_PENDING_TRACK_BYTE_CEILING, maximum_payload_size),
        )
        self._pending_track_bytes = 0
        self._pending_track_task: asyncio.Task[None] | None = None
        self._latest_track_coalesced_count = 0
        self._latest_track_evicted_count = 0
        self._entity_callback = self._receive_entity
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    async def watch_entities(
        self,
        *,
        categories: frozenset[EntityCategory],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[EntityEvent]:
        """Subscribe lazily using the narrowest fixed-depth entity filters."""

        handles: list[object] = []
        cleanup_lock = asyncio.Lock()
        stream: EventStream[EntityEvent]
        watcher: _EntityWatcher

        async def cleanup(*, suppress_errors: bool) -> None:
            pending_task = await self._detach_entity_watcher(watcher)
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()
                await asyncio.gather(pending_task, return_exceptions=True)
            async with cleanup_lock:
                await self._unsubscribe_all(handles, suppress_errors=suppress_errors)

        async def remove() -> None:
            await cleanup(suppress_errors=False)

        async def fail_restored_subscription(error: RestoreFailure) -> None:
            await stream._fail(error)

        stream = EventStream(
            buffer_size=buffer_size or self._default_buffer_size,
            delivery_policy=delivery_policy,
            on_close=remove,
        )
        watcher = _EntityWatcher(stream, categories, integrations)
        async with self._lifecycle_lock:
            if self._closed:
                raise NotReadyError(
                    "entity/location service is closed",
                    operation="watch",
                )
            # Register before the first SUBACK can activate the shared callback so
            # observations received while later fixed-depth filters are opening are
            # buffered instead of falling through a post-SUBACK ownership gap.
            self._entity_watchers.append(watcher)
        filters = build_entity_subscription_filters(categories, integrations)
        try:
            for topic_filter in filters:
                handle = await self._transport.subscribe(
                    topic_filter,
                    self._entity_callback,
                    on_restore_failure=fail_restored_subscription,
                )
                async with cleanup_lock:
                    async with self._lifecycle_lock:
                        retain_handle = not self._closed and not stream.closed
                    if retain_handle:
                        handles.append(handle)
                if not retain_handle:
                    await self._unsubscribe_all([handle], suppress_errors=True)
                    if stream._terminal_error is not None:
                        raise stream._terminal_error
                    raise NotReadyError(
                        "entity/location service closed while opening a watcher",
                        operation="watch",
                    )
            async with self._lifecycle_lock:
                accepted = (
                    not self._closed
                    and not stream.closed
                    and any(item is watcher for item in self._entity_watchers)
                )
            if not accepted:
                await stream.aclose()
                if stream._terminal_error is not None:
                    raise stream._terminal_error
                raise NotReadyError(
                    "entity/location service closed while opening a watcher",
                    operation="watch",
                )
        except BaseException:
            await cleanup(suppress_errors=True)
            with suppress(ECNClientError):
                await stream.aclose()
            raise
        return stream

    async def watch_locations(
        self,
        *,
        entity_ids: frozenset[UUID],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[LocationEvent]:
        """Subscribe lazily using the narrowest fixed-depth location filters."""

        handles: list[object] = []
        cleanup_lock = asyncio.Lock()
        stream: EventStream[LocationEvent]

        async def cleanup(*, suppress_errors: bool) -> None:
            async with self._lifecycle_lock:
                self._location_watchers[:] = [
                    item for item in self._location_watchers if item is not stream
                ]
            async with cleanup_lock:
                await self._unsubscribe_all(handles, suppress_errors=suppress_errors)

        async def remove() -> None:
            await cleanup(suppress_errors=False)

        async def fail_restored_subscription(error: RestoreFailure) -> None:
            await stream._fail(error)

        async def receive(topic: str, payload: bytes) -> None:
            if self._closed:
                return
            try:
                event = self._decode_location(topic, payload)
            except ECNClientError as error:
                stream._record_decode_error()
                logger.warning("discarded invalid location payload (%s)", error.code)
                return
            if event is None:
                return
            self._record_observed_location(event)
            if entity_ids and event.entity_id not in entity_ids:
                return
            if integrations and event.integration not in integrations:
                return
            stream._put_nowait(event)

        stream = EventStream(
            buffer_size=buffer_size or self._default_buffer_size,
            delivery_policy=delivery_policy,
            on_close=remove,
        )
        await self._ensure_watch_open()
        filters = build_location_subscription_filters(entity_ids, integrations)
        try:
            for topic_filter in filters:
                handle = await self._transport.subscribe(
                    topic_filter,
                    receive,
                    on_restore_failure=fail_restored_subscription,
                )
                async with cleanup_lock:
                    async with self._lifecycle_lock:
                        retain_handle = not self._closed and not stream.closed
                    if retain_handle:
                        handles.append(handle)
                if not retain_handle:
                    await self._unsubscribe_all([handle], suppress_errors=True)
                    if stream._terminal_error is not None:
                        raise stream._terminal_error
                    raise NotReadyError(
                        "entity/location service closed while opening a watcher",
                        operation="watch",
                    )
            async with self._lifecycle_lock:
                if self._closed or stream.closed:
                    accepted = False
                else:
                    self._location_watchers.append(stream)
                    accepted = True
            if not accepted:
                await stream.aclose()
                if stream._terminal_error is not None:
                    raise stream._terminal_error
                raise NotReadyError(
                    "entity/location service closed while opening a watcher",
                    operation="watch",
                )
        except BaseException:
            await cleanup(suppress_errors=True)
            with suppress(ECNClientError):
                await stream.aclose()
            raise
        return stream

    def last_observed_location(
        self,
        entity_id: UUID,
        *,
        integration: str | None,
    ) -> Location | None:
        """Return state learned from this client's own MQTT subscriptions only."""

        if integration is None:
            event = self._last_observed_by_entity.get(entity_id)
        else:
            event = self._observed_locations.get((entity_id, integration))
        return event.location if event is not None else None

    async def publish_entity(self, entity: Entity) -> PublicationReceipt:
        if entity.integration != self._integration_name:
            raise ValidationError(
                "entity integration must match the configured integration_name",
                operation="entities.publish",
            )
        topic, payload, qos = self._encode_entity(entity)
        operation_id = uuid4()
        try:
            await self._transport.publish(topic, payload, qos)
        except DeliveryError as error:
            raise self._enrich_delivery_error(
                error,
                operation="entities.publish",
                operation_id=operation_id,
            ) from None
        return PublicationReceipt(
            operation_id=operation_id,
            kind=PublicationKind.ENTITY,
            entity_id=entity.id,
            qos=qos,
            delivery_phase=(
                DeliveryPhase.LOCAL_SEND_COMPLETED if qos == 0 else DeliveryPhase.BROKER_ACCEPTED
            ),
            accepted_at=self._clock(),
        )

    async def publish_location(
        self,
        entity_id: UUID,
        location: Location,
    ) -> PublicationReceipt:
        topic, payload, qos = self._encode_location(
            entity_id,
            self._integration_name,
            location,
        )
        operation_id = uuid4()
        try:
            await self._transport.publish(topic, payload, qos)
        except DeliveryError as error:
            raise self._enrich_delivery_error(
                error,
                operation="locations.publish",
                operation_id=operation_id,
            ) from None
        return PublicationReceipt(
            operation_id=operation_id,
            kind=PublicationKind.LOCATION,
            entity_id=entity_id,
            qos=qos,
            delivery_phase=(
                DeliveryPhase.LOCAL_SEND_COMPLETED if qos == 0 else DeliveryPhase.BROKER_ACCEPTED
            ),
            accepted_at=self._clock(),
        )

    @staticmethod
    def _enrich_delivery_error(
        error: DeliveryError,
        *,
        operation: str,
        operation_id: UUID,
    ) -> DeliveryError:
        error_type: type[DeliveryError] = (
            OutcomeUnknownError if isinstance(error, OutcomeUnknownError) else DeliveryError
        )
        return error_type(
            "publication delivery did not complete",
            delivery_phase=error.delivery_phase,
            operation=operation,
            operation_id=operation_id,
            code=error.code,
            status_code=error.status_code,
            details=error.details,
        )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            entity_watchers, self._entity_watchers = self._entity_watchers, []
            location_watchers, self._location_watchers = self._location_watchers, []
            pending_task, self._pending_track_task = self._pending_track_task, None
            self._pending_tracks.clear()
            self._pending_track_bytes = 0
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            await asyncio.gather(pending_task, return_exceptions=True)
        try:
            for entity_watcher in entity_watchers:
                try:
                    await entity_watcher.stream.aclose()
                except asyncio.CancelledError:
                    raise
                except ECNClientError:
                    logger.debug("terminal watcher cleanup failed", exc_info=True)
            for location_watcher in location_watchers:
                try:
                    await location_watcher.aclose()
                except asyncio.CancelledError:
                    raise
                except ECNClientError:
                    logger.debug("terminal watcher cleanup failed", exc_info=True)
        finally:
            self._observed_locations.clear()
            self._last_observed_by_entity.clear()

    async def _ensure_watch_open(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise NotReadyError(
                    "entity/location service is closed",
                    operation="watch",
                )

    async def _unsubscribe_all(
        self,
        handles: list[object],
        *,
        suppress_errors: bool = False,
    ) -> None:
        first_error: ECNClientError | None = None
        while handles:
            subscription = handles[-1]
            try:
                await self._transport.unsubscribe(subscription)
            except asyncio.CancelledError:
                raise
            except ECNClientError as error:
                if first_error is None:
                    first_error = error
                logger.debug("lazy subscription cleanup failed", exc_info=True)
            finally:
                handles.pop()
        if first_error is not None and not suppress_errors:
            raise first_error

    def _record_observed_location(self, event: LocationEvent) -> None:
        self._observed_locations[(event.entity_id, event.integration)] = event
        self._last_observed_by_entity[event.entity_id] = event

    async def _receive_entity(self, topic: str, payload: bytes) -> None:
        if self._closed:
            return
        watchers = self._matching_entity_watchers(topic)
        if not watchers:
            return
        try:
            parsed_topic = parse_entity_topic(topic)
        except ECNClientError as error:
            self._record_entity_decode_error(watchers, error)
            return
        try:
            category = EntityCategory(parsed_topic.suffix.upper())
        except ValueError:
            category = EntityCategory.OTHER
        if category is not EntityCategory.TRACK or any(
            watcher.stream.delivery_policy is DeliveryPolicy.FIFO for watcher in watchers
        ):
            self._decode_and_deliver_entity(
                topic,
                payload,
                watchers,
                supersede_pending_track=category is EntityCategory.TRACK,
            )
            return
        try:
            identity = extract_entity_identity(parsed_topic, payload, self._maximum_payload_size)
        except ECNClientError as error:
            self._record_entity_decode_error(watchers, error)
            return
        if self._pop_pending_track(identity) is not None:
            self._latest_track_coalesced_count += 1
        self._pending_tracks[identity] = _PendingTrack(topic, payload, watchers)
        self._pending_track_bytes += len(payload)
        self._pending_tracks.move_to_end(identity)
        while (
            len(self._pending_tracks) > self._pending_track_limit
            or self._pending_track_bytes > self._pending_track_byte_limit
        ):
            _evicted_identity, evicted = self._pending_tracks.popitem(last=False)
            self._pending_track_bytes -= len(evicted.payload)
            self._latest_track_evicted_count += 1
        if self._pending_track_task is None or self._pending_track_task.done():
            self._pending_track_task = asyncio.create_task(
                self._drain_pending_tracks(),
                name="picogrid-ecn-latest-track-ingress",
            )
            self._pending_track_task.add_done_callback(self._pending_track_done)

    def _matching_entity_watchers(self, topic: str) -> tuple[_EntityWatcher, ...]:
        parts = topic.split("/")
        if len(parts) == 4 and parts[0] == "entity":
            integration, suffix = parts[1], parts[3]
        elif len(parts) == 3 and parts[0] == "entity_pb":
            integration, suffix = parts[1], parts[2]
        else:
            return ()
        try:
            category = EntityCategory(suffix.upper())
        except ValueError:
            category = EntityCategory.OTHER
        return tuple(
            watcher
            for watcher in self._entity_watchers
            if (not watcher.categories or category in watcher.categories)
            and (not watcher.integrations or integration in watcher.integrations)
        )

    async def _drain_pending_tracks(self) -> None:
        await asyncio.sleep(0)
        try:
            while self._pending_tracks and not self._closed:
                _identity, pending = self._pending_tracks.popitem(last=False)
                self._pending_track_bytes -= len(pending.payload)
                self._decode_and_deliver_entity(
                    pending.topic,
                    pending.payload,
                    pending.watchers,
                )
                await asyncio.sleep(0)
        finally:
            if self._pending_track_task is asyncio.current_task():
                self._pending_track_task = None

    @staticmethod
    def _pending_track_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("latest TRACK ingress processor failed")

    def _decode_and_deliver_entity(
        self,
        topic: str,
        payload: bytes,
        watchers: tuple[_EntityWatcher, ...],
        *,
        supersede_pending_track: bool = False,
    ) -> None:
        try:
            event = self._decode_entity(topic, payload)
        except ECNClientError as error:
            self._record_entity_decode_error(watchers, error)
            return
        except Exception:
            for watcher in watchers:
                watcher.stream._record_decode_error()
            logger.warning("entity payload decoder failed", exc_info=True)
            return
        if event is None:
            return
        if (
            supersede_pending_track
            and self._pop_pending_track((event.entity.integration, event.entity.id)) is not None
        ):
            # A newly active FIFO watcher moves matching observations onto the
            # direct path. Remove an older queued value for this identity before
            # delivering the newer event so an asynchronous drain cannot regress
            # latest-only consumers or the observed-location cache afterward.
            self._latest_track_coalesced_count += 1
        location = event.location or event.entity.position
        if location is not None:
            self._record_observed_location(
                LocationEvent(
                    entity_id=event.entity.id,
                    integration=event.entity.integration,
                    timestamp=event.timestamp,
                    location=location,
                )
            )
        for watcher in watchers:
            watcher.stream._put_nowait(event)

    async def _detach_entity_watcher(
        self,
        watcher: _EntityWatcher,
    ) -> asyncio.Task[None] | None:
        pending_task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            self._entity_watchers[:] = [
                item for item in self._entity_watchers if item is not watcher
            ]
            for identity, pending in tuple(self._pending_tracks.items()):
                remaining = tuple(item for item in pending.watchers if item is not watcher)
                if remaining:
                    self._pending_tracks[identity] = _PendingTrack(
                        pending.topic,
                        pending.payload,
                        remaining,
                    )
                else:
                    self._pop_pending_track(identity)
            if not self._pending_tracks:
                pending_task, self._pending_track_task = self._pending_track_task, None
        return pending_task

    def _pop_pending_track(self, identity: tuple[str, UUID]) -> _PendingTrack | None:
        pending = self._pending_tracks.pop(identity, None)
        if pending is not None:
            self._pending_track_bytes -= len(pending.payload)
        return pending

    @staticmethod
    def _record_entity_decode_error(
        watchers: tuple[_EntityWatcher, ...],
        error: ECNClientError,
    ) -> None:
        for watcher in watchers:
            watcher.stream._record_decode_error()
        logger.warning("discarded invalid entity payload (%s)", error.code)


__all__: list[str] = []
