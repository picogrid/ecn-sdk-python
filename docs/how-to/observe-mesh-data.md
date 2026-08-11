---
title: Observe mesh-routed data
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow to observe entity and location events that an ECN deployment routes
from other ECNs to your connected broker. What you can see depends on the deployment
and your credentials: broker ACLs determine which routed integrations you can
observe, and no SDK setting enables a route. The SDK does not configure, discover, or
perform mesh routing.

## Run the mesh observer

The [runnable mesh observation example](../../examples/observe_mesh_data.py) opens
the existing entity and location watchers for an explicit integration allowlist:

```bash
python examples/observe_mesh_data.py --check
export ECN_OBSERVED_INTEGRATIONS=authorized-source-one,authorized-source-two
export ECN_MAX_EVENTS=10
python examples/observe_mesh_data.py
```

## Interpret mesh visibility

Each integration produces only exact or fixed-depth entity and location filters.
The script never subscribes to a mesh namespace, a broker-wide wildcard, or an
administrative topic. It closes both streams when either the configured event bound
is reached or the process is cancelled.

Correlate entity and location events with their canonical entity UUID and integration
name. `Location.source` describes the location measurement or sensor source; it is
not the originating ECN and must not be used as mesh provenance. Whether an event
was routed across ECNs, which integrations are visible, and which route policy was
used are deployment properties outside this public wire contract.

This workflow is verified against the offline public contract only. Its behavior
against an external mesh deployment remains unverified.

For the supported publication pattern, continue with
[Sensor integration](../integrations/sensors.md).
