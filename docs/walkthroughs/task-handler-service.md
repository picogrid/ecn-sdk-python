---
title: Run an effector service
tableOfContents: false
sidebar:
  order: 41
---

Use one long-running service to receive and handle the entity and command pairs your
effector supports. The service owns one Picogrid ECN SDK client and one exact
registration for each pair. Registering the handler does not advertise the command
outside MQTT.

## Register the handler

```python
import asyncio
from uuid import UUID

from pydantic import BaseModel

from picogrid_ecn_client import ECNClient, ECNConfig, TaskRequestContext


class Request(BaseModel):
    value: int


class Result(BaseModel):
    doubled: int


async def run(config: ECNConfig) -> None:
    async def handle(context: TaskRequestContext, request: Request) -> Result:
        # Audit the literal-local or canonical source-terminal identity.
        print(f"task source: {context.source}")
        return Result(doubled=request.value * 2)

    async with ECNClient(config) as client:
        registration = await client.tasks.register(
            entity_id=UUID("00000000-0000-4000-8000-000000000001"),
            command="calculate",
            request_model=Request,
            result_model=Result,
            handler=handle,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await client.tasks.unregister(registration)
```

## Operate the service safely

Keep effects idempotent because bounded deduplication state does not survive
restart. Handlers are async-only so cancellation can participate in bounded client
shutdown. Bound handler work and do not return secrets in result payloads or failure
messages. Configure `ECNConfig.terminal_id` before accepting UUID-sourced requests;
without it, the handler accepts only literal `local`. See the runnable
[receive-task example](../../examples/receive_task.py), the
[terminal-addressed receiver example](../../examples/receive_mesh_task.py), and the
[Effector integration guide](../integrations/effectors.md).
