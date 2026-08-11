# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Stable typed entity API."""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from ..exceptions import ValidationError
from ..models import DeliveryPolicy, Entity, EntityCategory, EntityEvent, PublicationReceipt
from ..streams import EventStream


class _EntityClient(Protocol):
    def _ensure_ready(self) -> None: ...

    async def _watch_entities(
        self,
        *,
        categories: frozenset[EntityCategory],
        integrations: frozenset[str],
        buffer_size: int,
        delivery_policy: DeliveryPolicy,
    ) -> EventStream[EntityEvent]: ...

    async def _publish_entity(self, entity: Entity) -> PublicationReceipt: ...


class Entities:
    """Observe and publish typed entities through supported MQTT v5 families."""

    def __init__(self, client: _EntityClient) -> None:
        self.__client = client

    async def watch(
        self,
        *,
        categories: Collection[EntityCategory] | None = None,
        integrations: Collection[str] | None = None,
        buffer_size: int | None = None,
        delivery: DeliveryPolicy = DeliveryPolicy.FIFO,
    ) -> EventStream[EntityEvent]:
        """Open a lazy, bounded entity event stream after SUBACK.

        The subscription uses the narrowest fixed-depth JSON and protobuf
        filters represented by the category and integration selections. Filters
        contain no multi-level wildcard, are reference counted, and are released
        when their last stream closes.

        When every matching TRACK watcher selects ``LATEST``, raw observations can
        replace stale observations for the same identity before full payload decode.

        Args:
            categories: Entity categories to observe, or all supported categories.
            integrations: Integration names to observe, or all integrations.
            buffer_size: Positive local queue size, or the configured default.
            delivery: Local behavior when the stream queue is full.

        Returns:
            A bounded stream of matching typed entity events.

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
                operation="entity.watch",
            )
        self.__client._ensure_ready()
        size = 0 if buffer_size is None else buffer_size
        return await self.__client._watch_entities(
            categories=frozenset(categories or ()),
            integrations=frozenset(integrations or ()),
            buffer_size=size,
            delivery_policy=delivery,
        )

    async def publish(self, entity: Entity) -> PublicationReceipt:
        """Publish one typed entity in the configured wire format.

        The receipt confirms completion of the local MQTT operation only, not
        persistence or downstream processing.

        Args:
            entity: Entity to publish on its exact supported topic.

        Returns:
            The local MQTT publication receipt.

        Raises:
            AuthorizationError: If the broker rejects the publication.
            ConnectionError: If the MQTT publication cannot be completed.
            NotReadyError: If the client has not reached readiness.
            ProtocolError: If the broker returns a malformed or failed PUBACK.
            ResourceLimitError: If the encoded payload exceeds the byte limit.
            ValidationError: If the entity cannot be encoded for publication.
        """

        self.__client._ensure_ready()
        return await self.__client._publish_entity(entity)


__all__ = ["Entities"]
