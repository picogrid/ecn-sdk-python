---
title: Observe ECN location
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow to wait for the next ECN location update and inspect the value
stored in this SDK instance's observed-state cache. The
[runnable location example](../../examples/get_ecn_location.py) is not a server query.

## Observe the next update

`terminal-geolocation` is the integration segment of the ECN location broadcast on
the dedicated `entity_location` or `entity_location_pb` family.

```bash
python examples/get_ecn_location.py --check
export ECN_OBSERVATION_TIMEOUT=10
python examples/get_ecn_location.py
```

## Interpret observed state

Set the common connection variables described in
[authentication](../getting-started/authentication.md). The example subscribes only
to `entity_location/terminal-geolocation/+` and
`entity_location_pb/terminal-geolocation/+`. The single-level UUID wildcard is the
fixed-depth segment needed to observe the broadcast without
already knowing its canonical UUID. The returned event supplies that UUID. A timeout
means no matching update was observed; it does not prove that no server-side state
exists. For entity-specific observation, use `wait_for_update(entity_id, ...)` or a
location watcher with the exact UUID.

For the publication workflow, continue with
[Sensor integration](../integrations/sensors.md).
