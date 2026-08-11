# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Publish or update one typed public entity event."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import (
    DisplayMetadata,
    ECNClient,
    Entity,
    EntityCategory,
    EntityMetadata,
)
from picogrid_ecn_client.workflows import publish_entity

if __package__:
    from ._common import (
        emit,
        env_enum,
        env_uuid,
        load_config,
        optional_env,
        required_env,
        run_example,
        utc_now,
    )
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_enum,
        env_uuid,
        load_config,
        optional_env,
        required_env,
        run_example,
        utc_now,
    )


async def main() -> None:
    config = load_config()
    entity_id = env_uuid("ECN_ENTITY_ID")
    category = env_enum("ECN_ENTITY_CATEGORY", EntityCategory)
    entity_type = required_env("ECN_ENTITY_TYPE")
    name = optional_env("ECN_ENTITY_NAME")
    display_name = optional_env("ECN_DISPLAY_NAME")
    recorded_at = utc_now()
    async with ECNClient(config) as client:
        result = await publish_entity(
            client,
            entity_id=entity_id,
            category=category,
            entity_type=entity_type,
            name=name,
            display_name=display_name,
            recorded_at=recorded_at,
        )
    emit(result.receipt)


def _check() -> None:
    Entity(
        id=UUID("00000000-0000-4000-8000-000000000202"),
        category=EntityCategory.DETECTION,
        integration="offline-example",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        type="synthetic-detection",
        metadata=EntityMetadata(display=DisplayMetadata(label="Synthetic detection")),
    )


if __name__ == "__main__":
    run_example("publish entity", main, _check)
