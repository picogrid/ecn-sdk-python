---
title: Cleanup and cancellation
tableOfContents: false
sidebar:
  hidden: true
---

## Close every stream

The runnable [track watcher](../../examples/watch_tracks.py) demonstrates the core
stream rule: close every `EventStream` in `finally`.

```python
from picogrid_ecn_client import ECNClient, EntityCategory


async def consume(client: ECNClient) -> None:
    stream = await client.entities.watch(categories={EntityCategory.TRACK})
    try:
        async for event in stream:
            print(event.entity.id)
    finally:
        await stream.aclose()
```

## Close the client after cancellation

Use an async client context so cleanup also runs after cancellation:

```python
from picogrid_ecn_client import ECNClient, ECNConfig


async def run(config: ECNConfig) -> None:
    async with ECNClient(config) as client:
        await consume(client)
```

## End task handlers with their service

Unregister task handlers explicitly when their service lifetime ends. `close()` and
`unregister()` are idempotent. Client close cancels tracked handlers, fails pending
waiters with a typed local error, releases exact subscriptions, and bounds shutdown
by `shutdown_timeout`. Async handlers must cooperate with cancellation; the client
does not create or own worker threads for handler code.

Next, review the [client and task lifecycle](../concepts/lifecycle.md).
