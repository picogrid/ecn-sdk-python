---
title: Publish entities, locations, and PLI
sidebar:
  hidden: true
---

Use this workflow to publish an entity and its location under one canonical UUID.
Position Location Information (PLI) is a location associated with that UUID. Publish
it with the ordinary `Location` model and location wire; there is no separate PLI
model, method, topic, or entity category.

## Validate offline

These operations mutate the authorized MQTT target. Validate model construction
offline first:

```bash
python examples/publish_entity.py --check
python examples/publish_location.py --check
```

The runnable scripts are [publish an entity](../../examples/publish_entity.py) and
[publish a location](../../examples/publish_location.py).

## Publish an entity

For a broker-authorized publication, first set the common connection variables from
[authentication](../getting-started/authentication.md). Then publish one synthetic
entity event for a caller-owned UUID:

```bash
export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_ENTITY_CATEGORY=detection
export ECN_ENTITY_TYPE=synthetic-detection
export ECN_ENTITY_NAME='Synthetic public-client entity'
export ECN_DISPLAY_NAME='Synthetic display label'
python examples/publish_entity.py
```

## Publish a location

Publish one location for that same UUID with bounded synthetic coordinates:

```bash
export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_LATITUDE=0
export ECN_LONGITUDE=0
export ECN_ALTITUDE=100
export ECN_BEARING=0
export ECN_ACCURACY=1
export ECN_CONFIDENCE=1
export ECN_LOCATION_SOURCE=synthetic-public-client
python examples/publish_location.py
```

## Publish a location as PLI

When the position is operationally described as PLI, use the same location command
and authorize it as the same mutation:

```bash
export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_LATITUDE=0
export ECN_LONGITUDE=0
export ECN_ALTITUDE=100
export ECN_LOCATION_SOURCE=synthetic-public-client
python examples/publish_location.py
```

The entity's integration is always the configured integration name. Set
`ECN_WIRE_FORMAT=json` or `ECN_WIRE_FORMAT=protobuf` only when that supported wire
format is authorized. PLI carries location fields on the supported location family;
it does not invent a separate API or wire path.

## Interpret publication receipts

For these QoS 1 operations, a `PublicationReceipt` means the broker returned a
non-failure PUBACK. A negative PUBACK raises a typed public error. The receipt still
does not assert that downstream consumers persisted, rendered, or accepted the
message.

TRACK entity publications use QoS 0 and receive no broker acknowledgement. Local send
completion therefore does not establish broker receipt, persistence, or downstream
processing. Never use these scripts in a read-only validation session. For an
end-to-end design, continue with [Sensor integration](../integrations/sensors.md).
