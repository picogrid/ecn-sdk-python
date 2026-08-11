# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Register a typed task handler for one entity and command."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from picogrid_ecn_client import ECNClient, TaskRequestContext
from picogrid_ecn_client.workflows import EchoRequest, EchoResult, receive_task

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
    entity_id = env_uuid("ECN_ENTITY_ID")
    command = required_env("ECN_TASK_COMMAND")
    task_limit = env_int("ECN_TASK_LIMIT", default=0, minimum=0)
    async with ECNClient(config) as client:
        await receive_task(
            client,
            entity_id=entity_id,
            command=command,
            task_limit=task_limit,
            on_event=emit,
            on_registered=emit,
        )


def _check() -> None:
    request = EchoRequest(message="synthetic request")
    result = EchoResult(message=request.message)
    context = TaskRequestContext(
        task_id="offline-example-task",
        target_entity_id=UUID("00000000-0000-4000-8000-000000000301"),
        command="echo",
        source="local",
        mode="complete",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    if result.message != request.message or context.command != "echo":
        raise AssertionError("task handler model validation failed")


if __name__ == "__main__":
    run_example("receive task", main, _check)
