# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Watch authorized track events as validated public models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import ECNClient, Entity, EntityCategory, EntityEvent
from picogrid_ecn_client.workflows import watch_tracks

if __package__:
    from ._common import emit, env_int, load_config, optional_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_int,
        load_config,
        optional_env,
        run_example,
    )


async def main() -> None:
    config = load_config()
    integration = optional_env("ECN_OBSERVED_INTEGRATION")
    limit = env_int("ECN_MAX_EVENTS", default=0, minimum=0)
    async with ECNClient(config) as client:
        await watch_tracks(
            client,
            integration=integration,
            limit=limit,
            on_event=emit,
        )


def _check() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    event = EntityEvent(
        timestamp=timestamp,
        entity=Entity(
            id=UUID("00000000-0000-4000-8000-000000000101"),
            category=EntityCategory.TRACK,
            integration="offline-example",
            recorded_at=timestamp,
            type="synthetic-track",
        ),
    )
    if event.entity.category is not EntityCategory.TRACK:
        raise AssertionError("track model validation failed")


if __name__ == "__main__":
    run_example("watch tracks", main, _check)
