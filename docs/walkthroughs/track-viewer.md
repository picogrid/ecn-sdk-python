---
title: Build a track viewer
tableOfContents: false
sidebar:
  order: 40
---

Use this walkthrough to feed authorized track updates into a map, table, recorder, or
alerting pipeline. The watcher is scoped to the selected source integration.

## Observe tracks

1. Configure one authorized source integration.
2. Open `entities.watch` with `TRACK`, that integration, a bounded buffer, and
   latest-value delivery.
3. Key the local render model by `event.entity.identity`.
4. Apply newer events and tolerate duplicate delivery.
5. Close the stream when the viewer stops.

Design the operator display to mark observations **fresh** or **stale** and to
distinguish **connected**, **disconnected**, and **reconnecting** conditions.

```python
from picogrid_ecn_client import DeliveryPolicy, ECNClient, EntityCategory


async def track_updates(client: ECNClient) -> None:
    stream = await client.entities.watch(
        categories={EntityCategory.TRACK},
        integrations={"authorized-source"},
        buffer_size=64,
        delivery=DeliveryPolicy.LATEST,
    )
    try:
        async for event in stream:
            print(event.entity.identity.model_dump())
    finally:
        await stream.aclose()
```

## Extend the viewer

The runnable [track example](../../examples/watch_tracks.py) exercises the same
watcher. The included [operator application](../../operator-app/README.md) adds
category and affiliation markers, explicit **fresh** and **stale** labels, connection
labels, bounded dropped-event diagnostics, selection state, and deterministic
shutdown. Its mock mode is read-only and reports that tasking is **disabled** unless
the tasking flag and explicit allowlists are supplied. Do not subscribe to every
category merely to populate a viewer.

Continue to [Operator workflows](../operator/workflows.md) for the complete
interaction model.
