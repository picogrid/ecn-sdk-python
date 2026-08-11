# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Dispatch one typed task and print its structured public result."""

from __future__ import annotations

from picogrid_ecn_client import ECNClient, TaskMode
from picogrid_ecn_client.workflows import EchoRequest, EchoResult, dispatch_task

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
    target_entity_id = env_uuid("ECN_TARGET_ENTITY_ID")
    target_integration = required_env("ECN_TARGET_INTEGRATION")
    command = required_env("ECN_TASK_COMMAND")
    message = required_env("ECN_TASK_MESSAGE")
    timeout = env_float("ECN_TASK_TIMEOUT")
    mode = env_enum("ECN_TASK_MODE", TaskMode, default=TaskMode.COMPLETE)
    async with ECNClient(config) as client:
        result = await dispatch_task(
            client,
            target_entity_id=target_entity_id,
            target_integration=target_integration,
            command=command,
            message=message,
            timeout=timeout,
            mode=mode,
        )
    emit(result.result)


def _check() -> None:
    request = EchoRequest(message="synthetic request")
    if EchoResult(message=request.message).message != request.message:
        raise AssertionError("task dispatch model validation failed")


if __name__ == "__main__":
    run_example("dispatch task", main, _check)
