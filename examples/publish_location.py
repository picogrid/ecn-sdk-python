# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Publish a typed location for one public entity identity."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import ECNClient, Location
from picogrid_ecn_client.workflows import publish_location

if __package__:
    from ._common import emit, env_uuid, load_config, location_from_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_uuid,
        load_config,
        location_from_env,
        run_example,
    )


async def main() -> None:
    config = load_config()
    entity_id = env_uuid("ECN_ENTITY_ID")
    location = location_from_env(default_source=config.integration_name)
    async with ECNClient(config) as client:
        result = await publish_location(
            client,
            entity_id=entity_id,
            location=location,
        )
    emit(result.receipt)


def _check() -> None:
    Location(
        latitude=0,
        longitude=0,
        altitude=100,
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        source=str(UUID("00000000-0000-4000-8000-000000000203")),
    )


if __name__ == "__main__":
    run_example("publish location", main, _check)
