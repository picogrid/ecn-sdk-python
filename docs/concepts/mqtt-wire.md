---
title: MQTT topics and delivery
sidebar:
  order: 2
---

The Picogrid ECN SDK uses MQTT v5 to exchange supported operational messages with one
[ECN](ecns.md). Applications work through typed SDK services rather than constructing
arbitrary topics.

## Connection and subscription lifecycle

Picogrid ECN SDK connects to an ECN through its MQTT v5 interface. Startup performs
CONNECT/CONNACK and does not install entity, location, or task subscriptions.
Watchers and task registrations subscribe lazily, wait for SUBACK, and unsubscribe
when their last consumer closes.

## Confirmed topic families

These families are listed so broker operators can configure narrow ACLs; applications
use the typed SDK services rather than constructing topics:

```text
entity/{integration}/{entity_id}/{category}
entity_pb/{integration}/{category}
entity_location/{integration}/{entity_id}
entity_location_pb/{integration}/{entity_id}
task/{integration}/{entity_id}/{command}
task/{integration}/{entity_id}/{command}/response
{target_terminal_id}/task/{integration}/{entity_id}/{command}
{source_terminal_id}/task/{integration}/{entity_id}/{command}/response
```

## Bounded topic access

Builders restrict UUIDs and topic segments to a conservative safe grammar. Watchers
derive exact or fixed-depth filters from typed arguments. The SDK exposes neither
arbitrary topics nor a multi-level wildcard. Terminal-prefixed task topics are exact
publication routes only. ECN infrastructure removes the terminal prefix before
delivery, so the SDK subscribes only to the exact unprefixed request or response
topic.

See [wire formats](../reference/wire-formats.md) for payloads and
[Compatibility and limitations](../compatibility/limitations.md) for external
verification status.
