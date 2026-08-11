# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Reusable bounded observation workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection

from pydantic import Field

from picogrid_ecn_client import (
    DeliveryPolicy,
    ECNClient,
    EntityCategory,
    EntityEvent,
    EventStream,
    LocationEvent,
    ValidationError,
)
from picogrid_ecn_client.models._base import PublicModel

from ._retention import _EventRetention


class WatchEntitiesResult(PublicModel):
    """Entity events retained by a watch.

    Attributes:
        events: All events from a positive-limit watch, or the most recent
            ``client.config.watcher_buffer_size`` events from an unbounded watch.
    """

    events: tuple[EntityEvent, ...] = Field(
        description=(
            "All events from a positive-limit watch, or the most recently retained "
            "events from an unbounded watch."
        )
    )


class MeshObservationResult(PublicModel):
    """Entity and location events retained from routed topic families.

    Attributes:
        entity_events: All positive-limit entity events, or the most recently
            retained events from an unbounded entity consumer.
        location_events: All positive-limit location events, or the most recently
            retained events from an unbounded location consumer.
    """

    entity_events: tuple[EntityEvent, ...] = Field(
        description=(
            "All positive-limit entity events, or the most recently retained events "
            "from an unbounded entity consumer."
        )
    )
    location_events: tuple[LocationEvent, ...] = Field(
        description=(
            "All positive-limit location events, or the most recently retained events "
            "from an unbounded location consumer."
        )
    )


class ECNLocationResult(PublicModel):
    """The next terminal-geolocation event observed over MQTT.

    Attributes:
        event: Terminal-geolocation event returned by the public location interface.
    """

    event: LocationEvent = Field(
        description="Terminal-geolocation event returned by the public location interface."
    )


def _validate_limit(limit: int) -> None:
    if limit < 0:
        raise ValidationError("limit must be non-negative")


def _validate_integration(integration: str | None) -> None:
    if integration is not None and not integration.strip():
        raise ValidationError("integration must be non-empty")


def _validate_integrations(integrations: Collection[str]) -> frozenset[str]:
    allowlist = frozenset(integrations)
    if not allowlist:
        raise ValidationError("integrations must be non-empty")
    if any(not integration.strip() for integration in allowlist):
        raise ValidationError("integration names must be non-empty")
    return allowlist


async def _collect_entities(
    stream: EventStream[EntityEvent],
    *,
    limit: int,
    on_event: Callable[[EntityEvent], None] | None,
    buffer_size: int,
) -> tuple[EntityEvent, ...]:
    events = _EventRetention[EntityEvent](limit=limit, buffer_size=buffer_size)
    try:
        async for event in stream:
            events.append(event)
            if on_event is not None:
                on_event(event)
            if limit and len(events) >= limit:
                return events.snapshot()
    finally:
        await stream.aclose()
    return events.snapshot()


async def watch_tracks(
    client: ECNClient,
    *,
    integration: str | None = None,
    limit: int = 0,
    on_event: Callable[[EntityEvent], None] | None = None,
) -> WatchEntitiesResult:
    """Collect track events, with zero meaning an intentionally unbounded watch.

    An unbounded watch must consume every event through ``on_event``. Its returned
    tuple retains only the most recent ``client.config.watcher_buffer_size`` events.

    Args:
        client: Configured SDK client used to open the entity stream.
        integration: Optional exact integration name to observe.
        limit: Maximum events to collect; zero watches until interrupted while
            retaining only the most recent configured watcher buffer.
        on_event: Optional synchronous callback invoked for every observed event.

    Returns:
        All events from a positive-limit watch, or the most recently retained
        events from an unbounded watch.

    Raises:
        ValidationError: If ``limit`` is negative or ``integration`` is empty.
        ECNClientError: If the stream cannot be opened or consumed.
    """
    _validate_limit(limit)
    _validate_integration(integration)

    stream = await client.entities.watch(
        categories={EntityCategory.TRACK},
        integrations={integration} if integration is not None else None,
        delivery=DeliveryPolicy.LATEST,
    )
    return WatchEntitiesResult(
        events=await _collect_entities(
            stream,
            limit=limit,
            on_event=on_event,
            buffer_size=client.config.watcher_buffer_size,
        )
    )


async def watch_detections(
    client: ECNClient,
    *,
    integration: str | None = None,
    limit: int = 0,
    on_event: Callable[[EntityEvent], None] | None = None,
) -> WatchEntitiesResult:
    """Collect detection events, with zero meaning an intentionally unbounded watch.

    An unbounded watch must consume every event through ``on_event``. Its returned
    tuple retains only the most recent ``client.config.watcher_buffer_size`` events.

    Args:
        client: Configured SDK client used to open the entity stream.
        integration: Optional exact integration name to observe.
        limit: Maximum events to collect; zero watches until interrupted while
            retaining only the most recent configured watcher buffer.
        on_event: Optional synchronous callback invoked for every observed event.

    Returns:
        All events from a positive-limit watch, or the most recently retained
        events from an unbounded watch.

    Raises:
        ValidationError: If ``limit`` is negative or ``integration`` is empty.
        ECNClientError: If the stream cannot be opened or consumed.
    """
    _validate_limit(limit)
    _validate_integration(integration)

    stream = await client.entities.watch(
        categories={EntityCategory.DETECTION},
        integrations={integration} if integration is not None else None,
    )
    return WatchEntitiesResult(
        events=await _collect_entities(
            stream,
            limit=limit,
            on_event=on_event,
            buffer_size=client.config.watcher_buffer_size,
        )
    )


async def observe_mesh_data(
    client: ECNClient,
    *,
    integrations: Collection[str],
    limit: int = 0,
    on_event: Callable[[EntityEvent | LocationEvent], None] | None = None,
) -> MeshObservationResult:
    """Observe routed entity and location families until either consumer completes.

    An unbounded watch must consume every event through ``on_event``. Each returned
    tuple retains only the most recent ``client.config.watcher_buffer_size`` events.

    Args:
        client: Configured SDK client used to open both public streams.
        integrations: Exact integration names allowed on both streams.
        limit: Maximum events per consumer; zero watches until interrupted while
            retaining only the most recent configured watcher buffer per consumer.
        on_event: Optional synchronous callback invoked for every observed event.

    Returns:
        All events from positive-limit consumers, or the most recently retained
        events from unbounded consumers, before either consumer completed.

    Raises:
        ValidationError: If ``limit`` is negative, ``integrations`` is empty, or
            an integration name is empty.
        ECNClientError: If either stream cannot be opened or consumed.
    """
    _validate_limit(limit)
    allowlist = _validate_integrations(integrations)
    entity_stream = await client.entities.watch(
        integrations=allowlist,
        delivery=DeliveryPolicy.LATEST,
    )
    try:
        location_stream = await client.locations.watch(
            integrations=allowlist,
            delivery=DeliveryPolicy.LATEST,
        )
    except BaseException:
        await entity_stream.aclose()
        raise

    entity_events = _EventRetention[EntityEvent](
        limit=limit, buffer_size=client.config.watcher_buffer_size
    )
    location_events = _EventRetention[LocationEvent](
        limit=limit, buffer_size=client.config.watcher_buffer_size
    )

    async def consume_entities() -> None:
        async for event in entity_stream:
            entity_events.append(event)
            if on_event is not None:
                on_event(event)
            if limit and len(entity_events) >= limit:
                return

    async def consume_locations() -> None:
        async for event in location_stream:
            location_events.append(event)
            if on_event is not None:
                on_event(event)
            if limit and len(location_events) >= limit:
                return

    consumers = {
        asyncio.create_task(consume_entities()),
        asyncio.create_task(consume_locations()),
    }
    try:
        done, pending = await asyncio.wait(consumers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        # Nested so a rejected unsubscribe on the first stream cannot strand the
        # second watcher and its subscription on a client the caller keeps alive.
        try:
            await location_stream.aclose()
        finally:
            await entity_stream.aclose()
    return MeshObservationResult(
        entity_events=entity_events.snapshot(),
        location_events=location_events.snapshot(),
    )


async def get_ecn_location(
    client: ECNClient,
    *,
    timeout: float | None = None,
) -> ECNLocationResult:
    """Wait for the next terminal-geolocation MQTT event.

    Args:
        client: Configured SDK client used to observe terminal geolocation.
        timeout: Optional maximum wait in seconds.

    Returns:
        The next observed terminal-geolocation event.

    Raises:
        ECNClientError: If the stream cannot be opened or consumed.
        TimeoutError: If no event arrives before ``timeout``.
    """

    event = await client.locations.wait_for_terminal_geolocation(timeout=timeout)
    return ECNLocationResult(event=event)


__all__ = [
    "ECNLocationResult",
    "MeshObservationResult",
    "WatchEntitiesResult",
    "get_ecn_location",
    "observe_mesh_data",
    "watch_detections",
    "watch_tracks",
]
