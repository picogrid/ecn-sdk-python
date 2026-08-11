# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Receive one exact typed task with a canonical terminal source."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import ECNClient, TaskRequestContext
from picogrid_ecn_client.workflows import receive_mesh_task

if __package__:
    from ._common import emit, env_int, env_uuid, load_config, required_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_int,
        env_uuid,
        load_config,
        required_env,
        run_example,
    )


async def main() -> None:
    config = load_config()
    terminal_id = env_uuid("ECN_TERMINAL_ID")
    entity_id = env_uuid("ECN_ENTITY_ID")
    command = required_env("ECN_TASK_COMMAND")
    task_limit = env_int("ECN_TASK_LIMIT", default=0, minimum=0)
    async with ECNClient(config) as client:
        await receive_mesh_task(
            client,
            terminal_id=terminal_id,
            entity_id=entity_id,
            command=command,
            task_limit=task_limit,
            on_event=emit,
            on_registered=emit,
        )


def _check() -> None:
    source_terminal_id = UUID("00000000-0000-4000-8000-000000000721")
    context = TaskRequestContext(
        task_id="offline-mesh-example-task",
        target_entity_id=UUID("00000000-0000-4000-8000-000000000722"),
        command="echo",
        source=source_terminal_id,
        mode="complete",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if context.source != source_terminal_id:
        raise AssertionError("mesh task source model validation failed")


if __name__ == "__main__":
    run_example("receive mesh task", main, _check)
