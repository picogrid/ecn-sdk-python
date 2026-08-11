---
title: Entities
tableOfContents: false
sidebar:
  order: 3
---

An **entity** is the SDK's representation of an operational object reported by an
[integration](ecns.md). The validated `Entity` model carries a UUID identity,
integration, category, timestamp, type, status, affiliation, optional display
metadata, and optional location. `EntityEvent` adds the received event timestamp and
decoded location.

## Categories and observation

Supported publication categories are DEVICE, DETECTION, TRACK, SYSTEM, SENSOR,
ALERT, and GEOMETRIC. `OTHER` is a decode-only safe value for an unknown future
category and is rejected on publication.

Open a watcher only when needed and pass every known filter:

```python
from picogrid_ecn_client import DeliveryPolicy, ECNClient, EntityCategory


async def next_track(client: ECNClient) -> None:
    stream = await client.entities.watch(
        categories={EntityCategory.TRACK},
        integrations={"authorized-source"},
        buffer_size=32,
        delivery=DeliveryPolicy.LATEST,
    )
    try:
        event = await anext(stream)
        print(event.model_dump())
    finally:
        await stream.aclose()
```

## Publication receipts

TRACK publication uses QoS 0; other supported entity publications use QoS 1. A
QoS 1 receipt requires a non-failure PUBACK, while a TRACK receipt can confirm only
the local QoS 0 send because MQTT provides no broker acknowledgement. Neither is
downstream persistence or consumer acceptance.

When every watcher matching a TRACK message uses `LATEST`, the client keeps only the
freshest raw payload per integration and canonical entity UUID before full model
decoding. Both pending identity count and aggregate raw bytes are bounded. A matching
`FIFO` watcher disables this ingress coalescing so every message is decoded and
offered in order. Other entity categories always use the ordinary decode path.

Learn [UUID identity](uuids.md), then use the runnable
[watch tracks](../../examples/watch_tracks.py),
[watch detections](../../examples/watch_detections.py), and
[publish an entity](../../examples/publish_entity.py). For a complete producer
design, see [Sensor integration](../integrations/sensors.md).
