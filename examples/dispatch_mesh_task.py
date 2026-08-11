# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Dispatch one typed task through a configured terminal-addressed route."""

from __future__ import annotations

from uuid import UUID

from picogrid_ecn_client import ECNClient, TaskMode
from picogrid_ecn_client.workflows import EchoRequest, dispatch_mesh_task

if __package__:
    from ._common import emit, env_enum, env_float, env_uuid, load_config, required_env, run_example
else:
    from _common import (  # type: ignore[import-not-found,no-redef]
        emit,
        env_enum,
        env_float,
        env_uuid,
        load_config,
        required_env,
        run_example,
    )


async def main() -> None:
    config = load_config()
    env_uuid("ECN_TERMINAL_ID")
    target_entity_id = env_uuid("ECN_TARGET_ENTITY_ID")
    target_integration = required_env("ECN_TARGET_INTEGRATION")
    target_terminal_id = env_uuid("ECN_TARGET_TERMINAL_ID")
    command = required_env("ECN_TASK_COMMAND")
    message = required_env("ECN_TASK_MESSAGE")
    timeout = env_float("ECN_TASK_TIMEOUT")
    mode = env_enum("ECN_TASK_MODE", TaskMode, default=TaskMode.COMPLETE)
    async with ECNClient(config) as client:
        result = await dispatch_mesh_task(
            client,
            target_entity_id=target_entity_id,
            target_integration=target_integration,
            target_terminal_id=target_terminal_id,
            command=command,
            message=message,
            timeout=timeout,
            mode=mode,
        )
    emit(result.result)


def _check() -> None:
    source_terminal_id = UUID("00000000-0000-4000-8000-000000000711")
    target_terminal_id = UUID("00000000-0000-4000-8000-000000000712")
    request = EchoRequest(message="synthetic mesh request")
    if not str(source_terminal_id) or not str(target_terminal_id) or not request.message:
        raise AssertionError("mesh task dispatch model validation failed")


if __name__ == "__main__":
    run_example("dispatch mesh task", main, _check)
