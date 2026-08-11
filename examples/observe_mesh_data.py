# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Observe entity and location data routed into authorized MQTT topic families."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import (
    ECNClient,
    Entity,
    EntityCategory,
    EntityEvent,
    Location,
    LocationEvent,
)
from picogrid_ecn_client.workflows import observe_mesh_data

if __package__:
    from ._common import (
        ExampleConfigurationError,
        emit,
        env_int,
        load_config,
        required_env,
        run_example,
    )
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        ExampleConfigurationError,
        emit,
        env_int,
        load_config,
        required_env,
        run_example,
    )


def _integration_allowlist() -> frozenset[str]:
    integrations = frozenset(
        item.strip()
        for item in required_env("ECN_OBSERVED_INTEGRATIONS").split(",")
        if item.strip()
    )
    if not integrations:
        raise ExampleConfigurationError(
            "ECN_OBSERVED_INTEGRATIONS must contain at least one integration"
        )
    if any("/" in item or "+" in item or "#" in item for item in integrations):
        raise ExampleConfigurationError(
            "ECN_OBSERVED_INTEGRATIONS must contain exact integration names"
        )
    return integrations


async def main() -> None:
    config = load_config()
    integrations = _integration_allowlist()
    limit = env_int("ECN_MAX_EVENTS", default=0, minimum=0)
    async with ECNClient(config) as client:
        await observe_mesh_data(
            client,
            integrations=integrations,
            limit=limit,
            on_event=emit,
        )


def _check() -> None:
    entity_id = UUID("00000000-0000-4000-8000-000000000701")
    recorded_at = datetime(2026, 1, 1, tzinfo=UTC)
    entity_event = EntityEvent(
        timestamp=recorded_at,
        entity=Entity(
            id=entity_id,
            category=EntityCategory.TRACK,
            integration="synthetic-mesh-source",
            recorded_at=recorded_at,
            type="synthetic-track",
        ),
    )
    location_event = LocationEvent(
        entity_id=entity_id,
        integration="synthetic-mesh-source",
        timestamp=recorded_at,
        location=Location(
            latitude=0,
            longitude=0,
            recorded_at=recorded_at,
            source="synthetic-sensor",
        ),
    )
    if entity_event.entity.id != location_event.entity_id:
        raise AssertionError("mesh observation correlation model validation failed")


if __name__ == "__main__":
    run_example("observe mesh-routed data", main, _check)
