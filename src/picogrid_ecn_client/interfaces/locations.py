# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Stable typed location API."""

from __future__ import annotations

import math
from collections.abc import Collection
from typing import Protocol
from uuid import UUID

from ..exceptions import ValidationError
from ..models import DeliveryPolicy, Location, LocationEvent, PublicationReceipt
from ..streams import EventStream


def _validated_timeout(timeout: float | None, *, operation: str) -> float | None:
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValidationError(
            "timeout must be a finite positive number of at most 300 seconds",
            operation=operation,
        )
    return float(timeout) if timeout is not None else None


class _LocationClient(Protocol):
    def _ensure_ready(self) -> None: ...

    async def _watch_locations(
        self,
        *,
        entity_ids: frozenset[UUID],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[LocationEvent]: ...

    def _last_observed_location(
        self,
        entity_id: UUID,
        *,
        integration: str | None,
    ) -> Location | None: ...

    async def _wait_for_location_update(
        self,
        entity_id: UUID,
        *,
        integration: str | None,
        timeout: float | None,
    ) -> Location: ...

    async def _wait_for_terminal_geolocation(
        self,
        *,
        timeout: float | None,
    ) -> LocationEvent: ...

    async def _publish_location(
        self,
        entity_id: UUID,
        location: Location,
    ) -> PublicationReceipt: ...


class Locations:
    """Observe and publish typed locations through supported MQTT v5 families."""

    def __init__(self, client: _LocationClient) -> None:
        self.__client = client

    async def watch(
        self,
        *,
        entity_ids: Collection[UUID] | None = None,
        integrations: Collection[str] | None = None,
        buffer_size: int | None = None,
        delivery: DeliveryPolicy = DeliveryPolicy.LATEST,
    ) -> EventStream[LocationEvent]:
        """Open a lazy, bounded location event stream after SUBACK.

        Entity and integration selections become fixed-depth JSON and protobuf
        filters. Filters are reference counted and released when their last
        stream closes.

        Args:
            entity_ids: Entity UUIDs to observe, or all entities.
            integrations: Integration names to observe, or all integrations.
            buffer_size: Positive local queue size, or the configured default.
            delivery: Local behavior when the stream queue is full.

        Returns:
            A bounded stream of matching typed location events.

        Raises:
            AuthorizationError: If the broker rejects a required subscription.
            ConnectionError: If the MQTT subscription cannot be completed.
            NotReadyError: If the client has not reached readiness.
            ResourceLimitError: If the requested queue exceeds the configured limit.
            ValidationError: If an argument cannot form a supported watcher.
        """
        if buffer_size is not None and (
            isinstance(buffer_size, bool) or not isinstance(buffer_size, int) or buffer_size < 1
        ):
            raise ValidationError(
                "buffer_size must be a positive integer when provided",
                operation="location.watch",
            )
        self.__client._ensure_ready()
        size = 0 if buffer_size is None else buffer_size
        return await self.__client._watch_locations(
            entity_ids=frozenset(entity_ids or ()),
            integrations=frozenset(integrations or ()),
            buffer_size=size,
            delivery_policy=delivery,
        )

    def last_observed(
        self,
        entity_id: UUID,
        *,
        integration: str | None = None,
    ) -> Location | None:
        """Return the latest matching location observed by this client.

        This in-memory lookup performs no network operation. With no integration,
        it returns the most recently received matching entity location across
        integrations. Per-instance observed state is discarded when the client
        closes.

        Args:
            entity_id: Canonical entity UUID to look up.
            integration: Exact integration to match, or any integration.

        Returns:
            The latest matching decoded location, or ``None`` if none was observed.

        Raises:
            NotReadyError: If the client has not reached readiness.
        """

        self.__client._ensure_ready()
        return self.__client._last_observed_location(
            entity_id,
            integration=integration,
        )

    async def wait_for_update(
        self,
        entity_id: UUID,
        *,
        integration: str | None = None,
        timeout: float | None = None,
    ) -> Location:
        """Wait for a future matching MQTT location update.

        The method uses a temporary lazy subscription and does not return an
        update cached before the call.

        Args:
            entity_id: Canonical entity UUID to observe.
            integration: Exact integration to match, or any integration.
            timeout: Deadline in seconds, or the configured operation timeout
                when ``None``.

        Returns:
            The location from the next matching update.

        Raises:
            AuthorizationError: If the broker rejects the temporary subscription
                or the unsubscribe that releases it.
            ConnectionError: If the MQTT subscription or its release cannot be
                completed.
            NotReadyError: If the client has not reached readiness.
            ProtocolError: If the broker returns a malformed UNSUBACK while the
                temporary subscription is released.
            TimeoutError: If no matching future update arrives before the deadline.
            ValidationError: If ``timeout`` is not finite and positive.
        """

        validated_timeout = _validated_timeout(
            timeout,
            operation="location.wait_for_update",
        )
        self.__client._ensure_ready()
        return await self.__client._wait_for_location_update(
            entity_id,
            integration=integration,
            timeout=validated_timeout,
        )

    async def wait_for_terminal_geolocation(
        self,
        *,
        timeout: float | None = None,
    ) -> LocationEvent:
        """Wait for a future terminal-geolocation MQTT observation.

        The fixed-depth JSON and protobuf filters contain one UUID segment. The
        returned event supplies that canonical UUID. This observes a future
        message; it is not discovery or an authoritative location query.

        Args:
            timeout: Deadline in seconds, or the configured operation timeout
                when ``None``.

        Returns:
            The next terminal-geolocation event.

        Raises:
            AuthorizationError: If the broker rejects the temporary subscription
                or the unsubscribe that releases it.
            ConnectionError: If the MQTT subscription or its release cannot be
                completed.
            NotReadyError: If the client has not reached readiness.
            ProtocolError: If the broker returns a malformed UNSUBACK while the
                temporary subscription is released.
            TimeoutError: If no matching future update arrives before the deadline.
            ValidationError: If ``timeout`` is not finite and positive.
        """

        validated_timeout = _validated_timeout(
            timeout,
            operation="location.wait_for_terminal_geolocation",
        )
        self.__client._ensure_ready()
        return await self.__client._wait_for_terminal_geolocation(
            timeout=validated_timeout,
        )

    async def publish(self, *, entity_id: UUID, location: Location) -> PublicationReceipt:
        """Publish one typed location for a canonical entity UUID.

        The configured integration supplies the topic segment. The receipt
        confirms completion of the local MQTT operation only, not persistence or
        downstream processing.

        Args:
            entity_id: Canonical UUID of the located entity.
            location: Position and location information to publish.

        Returns:
            The local MQTT publication receipt.

        Raises:
            AuthorizationError: If the broker rejects the publication.
            ConnectionError: If the MQTT publication cannot be completed.
            NotReadyError: If the client has not reached readiness.
            ProtocolError: If the broker returns a malformed or failed PUBACK.
            ResourceLimitError: If the encoded payload exceeds the byte limit.
            ValidationError: If the location cannot be encoded for publication.
        """
        self.__client._ensure_ready()
        return await self.__client._publish_location(entity_id, location)


__all__ = ["Locations"]
