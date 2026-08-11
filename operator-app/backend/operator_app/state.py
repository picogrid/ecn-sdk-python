# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Bounded operator-visible state derived only from observed MQTT events."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from picogrid_ecn_client import (
    Affiliation,
    ClientState,
    ConnectionFailureCode,
    ConnectionRetryState,
    ConnectionStatus,
    Entity,
    EntityEvent,
    EntityStatus,
    Location,
    LocationEvent,
)
from pydantic import BaseModel, ConfigDict


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class OperatorConnectionState(StrEnum):
    """Fixed, secret-safe connection states presented to an operator."""

    READY = "ready"
    RECONNECTING = "reconnecting"
    RETRY_SCHEDULED = "retry scheduled"
    CREDENTIALS_REJECTED = "credentials rejected"
    CREDENTIALS_UNAVAILABLE = "credentials unavailable"
    SUBSCRIPTION_DENIED = "subscription denied"
    SUBSCRIPTION_RESOURCE_LIMITED = "subscription resource-limited"
    TERMINAL = "terminal"
    DISCONNECTED = "disconnected"


def operator_connection_state(status: ConnectionStatus) -> OperatorConnectionState:
    """Map public redacted diagnostics to one fixed operator-facing state."""

    if status.ready:
        return OperatorConnectionState.READY
    if status.retry_state is ConnectionRetryState.SCHEDULED:
        return OperatorConnectionState.RETRY_SCHEDULED
    if status.retry_state is ConnectionRetryState.CONNECTING or (
        status.retry_state is not ConnectionRetryState.WAITING_FOR_CREDENTIALS
        and status.state in {ClientState.STARTING, ClientState.RECONNECTING}
    ):
        return OperatorConnectionState.RECONNECTING
    if status.last_failure_code is ConnectionFailureCode.AUTHENTICATION_REJECTED:
        return OperatorConnectionState.CREDENTIALS_REJECTED
    if status.last_failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE:
        return OperatorConnectionState.CREDENTIALS_UNAVAILABLE
    if status.last_failure_code is ConnectionFailureCode.SUBSCRIPTION_DENIED:
        return OperatorConnectionState.SUBSCRIPTION_DENIED
    if status.last_failure_code is ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT:
        return OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED
    if status.state is ClientState.FAILED or status.retry_state is ConnectionRetryState.TERMINAL:
        return OperatorConnectionState.TERMINAL
    return OperatorConnectionState.DISCONNECTED


class LocationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    latitude: float
    longitude: float
    altitude: float | None
    bearing: float | None
    accuracy: float | None
    source: str | None
    recorded_at: datetime


class EntityView(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    entity_id: UUID
    integration: str
    category: str | None
    affiliation: Affiliation
    status: EntityStatus
    type: str | None
    name: str | None
    fingerprint: str | None
    metadata: dict[str, object]
    location: LocationView | None
    location_only: bool
    entity_recorded_at: datetime | None
    last_observed_at: datetime
    age_seconds: float
    freshness: Freshness
    entity_age_seconds: float | None
    entity_freshness: Freshness | None
    location_age_seconds: float | None
    location_freshness: Freshness | None


class DiagnosticView(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    level: str
    code: str
    message: str


class TaskOutcomeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str | None
    target_key: str
    command: str
    mode: str
    status: str
    detail: str
    completed_at: datetime


class RuntimeHealthView(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_watcher_active: bool
    location_watcher_active: bool
    entity_scope_pairs: int
    location_scope_filters: int
    entity_dropped_events: int
    location_dropped_events: int
    entity_decode_errors: int
    location_decode_errors: int
    browser_clients: int
    browser_dropped_updates: int


class OperatorSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    connection: ConnectionStatus | None
    connection_summary: OperatorConnectionState | None
    entities: tuple[EntityView, ...]
    diagnostics: tuple[DiagnosticView, ...]
    task_outcomes: tuple[TaskOutcomeView, ...]
    health: RuntimeHealthView | None = None


@dataclass(slots=True)
class _ObservedRecord:
    entity_id: UUID
    integration: str
    entity: Entity | None
    location: Location | None
    entity_observed_at: datetime | None
    location_observed_at: datetime | None
    last_observed_at: datetime


def entity_key(integration: str, entity_id: UUID) -> str:
    """Canonical browser/backend correlation key."""

    return f"{integration}/{entity_id}"


def _bounded_value(value: object, *, depth: int = 0) -> object:
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, list):
        return [_bounded_value(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
        }
    return str(value)[:256]


class OperatorState:
    """LRU entity state, diagnostic ring, and task outcome ring."""

    def __init__(
        self,
        *,
        maximum_entities: int,
        stale_after_seconds: float,
        diagnostic_limit: int,
        task_history_limit: int,
    ) -> None:
        self._maximum_entities = maximum_entities
        self._stale_after_seconds = stale_after_seconds
        self._records: OrderedDict[str, _ObservedRecord] = OrderedDict()
        self._diagnostics: deque[DiagnosticView] = deque(maxlen=diagnostic_limit)
        self._task_outcomes: deque[TaskOutcomeView] = deque(maxlen=task_history_limit)
        self._connection: ConnectionStatus | None = None
        self._watcher_terminal_states: dict[
            Literal["entity", "location"], OperatorConnectionState
        ] = {}
        self._lock = asyncio.Lock()

    async def observe_entity(self, event: EntityEvent) -> None:
        observed_at = datetime.now(UTC)
        entity = event.entity
        location = event.location or entity.position
        key = entity_key(entity.integration, entity.id)
        async with self._lock:
            previous = self._records.get(key)
            record = _ObservedRecord(
                entity_id=entity.id,
                integration=entity.integration,
                entity=entity,
                location=(
                    location if location is not None else (previous.location if previous else None)
                ),
                entity_observed_at=observed_at,
                location_observed_at=(
                    observed_at
                    if location is not None
                    else (previous.location_observed_at if previous else None)
                ),
                last_observed_at=observed_at,
            )
            self._put(key, record)

    async def observe_location(self, event: LocationEvent) -> None:
        observed_at = datetime.now(UTC)
        key = entity_key(event.integration, event.entity_id)
        async with self._lock:
            previous = self._records.get(key)
            record = _ObservedRecord(
                entity_id=event.entity_id,
                integration=event.integration,
                entity=previous.entity if previous else None,
                location=event.location,
                entity_observed_at=previous.entity_observed_at if previous else None,
                location_observed_at=observed_at,
                last_observed_at=observed_at,
            )
            self._put(key, record)

    def _put(self, key: str, record: _ObservedRecord) -> None:
        self._records[key] = record
        self._records.move_to_end(key)
        while len(self._records) > self._maximum_entities:
            self._records.popitem(last=False)

    async def set_connection(self, status: ConnectionStatus) -> bool:
        async with self._lock:
            changed = self._connection != status
            self._connection = status
            return changed

    async def set_watcher_terminal_state(
        self,
        watcher: Literal["entity", "location"],
        state: OperatorConnectionState,
    ) -> None:
        """Record one fixed, secret-safe essential watcher terminal reason."""

        if state not in {
            OperatorConnectionState.SUBSCRIPTION_DENIED,
            OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED,
        }:
            raise ValueError("watcher terminal state must describe a subscription failure")
        async with self._lock:
            self._watcher_terminal_states[watcher] = state

    async def diagnostic(self, level: str, code: str, message: str) -> None:
        safe_level = level if level in {"info", "warning", "error"} else "error"
        async with self._lock:
            self._diagnostics.append(
                DiagnosticView(
                    timestamp=datetime.now(UTC),
                    level=safe_level,
                    code=code[:64],
                    message=message[:300],
                )
            )

    async def add_task_outcome(self, outcome: TaskOutcomeView) -> None:
        async with self._lock:
            self._task_outcomes.append(outcome)

    async def task_target(self, *, integration: str, entity_id: UUID) -> EntityView | None:
        key = entity_key(integration, entity_id)
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            return self._view(record, datetime.now(UTC))

    async def snapshot(self) -> OperatorSnapshot:
        now = datetime.now(UTC)
        async with self._lock:
            entities = tuple(self._view(record, now) for record in reversed(self._records.values()))
            connection_summary = (
                operator_connection_state(self._connection)
                if self._connection is not None
                else None
            )
            watcher_states = frozenset(self._watcher_terminal_states.values())
            if OperatorConnectionState.SUBSCRIPTION_DENIED in watcher_states:
                connection_summary = OperatorConnectionState.SUBSCRIPTION_DENIED
            elif OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED in watcher_states:
                connection_summary = OperatorConnectionState.SUBSCRIPTION_RESOURCE_LIMITED
            return OperatorSnapshot(
                generated_at=now,
                connection=self._connection,
                connection_summary=connection_summary,
                entities=entities,
                diagnostics=tuple(reversed(self._diagnostics)),
                task_outcomes=tuple(reversed(self._task_outcomes)),
            )

    async def clear(self) -> None:
        """Discard all client-observed state and operator history on shutdown."""

        async with self._lock:
            self._records.clear()
            self._diagnostics.clear()
            self._task_outcomes.clear()
            self._connection = None
            self._watcher_terminal_states.clear()

    def _view(self, record: _ObservedRecord, now: datetime) -> EntityView:
        age = max(0.0, (now - record.last_observed_at).total_seconds())
        entity_age = (
            max(0.0, (now - record.entity_observed_at).total_seconds())
            if record.entity_observed_at is not None
            else None
        )
        location_age = (
            max(0.0, (now - record.location_observed_at).total_seconds())
            if record.location_observed_at is not None
            else None
        )
        entity = record.entity
        metadata: dict[str, object] = {}
        if entity is not None:
            raw = entity.metadata.model_dump(mode="json")
            bounded = _bounded_value(raw)
            if isinstance(bounded, dict):
                metadata = bounded
        location = record.location
        return EntityView(
            key=entity_key(record.integration, record.entity_id),
            entity_id=record.entity_id,
            integration=record.integration,
            category=entity.category.value if entity is not None else None,
            affiliation=entity.affiliation if entity is not None else Affiliation.UNKNOWN,
            status=entity.status if entity is not None else EntityStatus.UNKNOWN,
            type=entity.type if entity is not None else None,
            name=entity.name if entity is not None else None,
            fingerprint=entity.fingerprint if entity is not None else None,
            metadata=metadata,
            location=(
                LocationView(
                    latitude=location.latitude,
                    longitude=location.longitude,
                    altitude=location.altitude,
                    bearing=location.bearing,
                    accuracy=location.accuracy,
                    source=location.source,
                    recorded_at=location.recorded_at,
                )
                if location is not None
                else None
            ),
            location_only=entity is None,
            entity_recorded_at=entity.recorded_at if entity is not None else None,
            last_observed_at=record.last_observed_at,
            age_seconds=age,
            freshness=(Freshness.STALE if age > self._stale_after_seconds else Freshness.FRESH),
            entity_age_seconds=entity_age,
            entity_freshness=(
                None
                if entity_age is None
                else (
                    Freshness.STALE if entity_age > self._stale_after_seconds else Freshness.FRESH
                )
            ),
            location_age_seconds=location_age,
            location_freshness=(
                None
                if location_age is None
                else (
                    Freshness.STALE if location_age > self._stale_after_seconds else Freshness.FRESH
                )
            ),
        )


__all__ = [
    "DiagnosticView",
    "EntityView",
    "Freshness",
    "OperatorConnectionState",
    "OperatorSnapshot",
    "OperatorState",
    "RuntimeHealthView",
    "TaskOutcomeView",
    "entity_key",
    "operator_connection_state",
]
