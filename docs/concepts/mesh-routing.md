---
title: Mesh routing boundary
tableOfContents: false
sidebar:
  order: 7
---

Picogrid ECN SDK exchanges MQTT messages with one connected ECN. Mesh forwarding is
a deployment function: ECN infrastructure may copy authorized peer-origin messages
into the same entity, location, and task topic families that local integrations use.
The SDK neither discovers the topology nor configures those routes.

## Deployment context controls visibility

Mesh behavior is not universal. Common deployment paths include:

- **Zero mesh:** peer forwarding is disabled or no peers are configured. Entity,
  location, and task traffic remains at the connected ECN.
- **Baseline mesh profile:** configured peers can exchange the baseline entity JSON,
  entity protobuf, dedicated protobuf location, and task families. Embedded
  locations travel with entity events. Dedicated JSON location forwarding depends
  on the deployed route profile and must not be assumed.
- **Deployment-specific mesh:** operators may enable, disable, or narrow supported
  families and peers. Broker ACLs still determine what this client may observe or
  publish.

These are deployment profiles, not SDK protocol modes. No `mesh=True` setting can
create a route, and mock behavior is not evidence that a particular ECN enabled one.

## Entity and location observations

Entity and location publications have no caller-selected destination ECN. The SDK
publishes their normal unprefixed topics to the connected broker; configured
infrastructure decides whether to replicate them. Routed observations arrive under
the same unprefixed topic shape, so existing bounded watchers receive them without a
mesh-specific subscription.

The confirmed entity and location payloads do not carry a general origin-ECN field.
The topic integration identifies the producer integration, `Entity.parent_id`
describes entity parentage, and `Location.source` describes measurement provenance;
none is a reliable mesh-hop marker. Applications can correlate data by canonical
entity UUID and integration, but must not infer topology from arrival.

## Terminal-addressed tasks

Use the ordinary same-ECN task path unless the deployment has supplied canonical
terminal identities and an authorized cross-terminal route.

Tasks have an additional confirmed route. Set `ECNConfig.terminal_id` to the
canonical UUID of the connected ECN terminal. A normal task request serializes that
UUID in `source`. Passing a different `target_terminal_id` to `client.tasks.send()`
publishes one exact terminal-prefixed request. Explicit self-targeting is rejected;
omit the target to use the ordinary unprefixed same-ECN request. On a compatible
route, ECN infrastructure delivers the request to the target as the ordinary
unprefixed task topic and routes the exact response back to the source terminal.

Handlers continue to subscribe to only:

```text
task/{integration}/{entity_id}/{command}
```

They accept literal `source="local"` for the established compatibility path. They
also accept a canonical UUID source when their own `terminal_id` is configured,
expose that UUID in `TaskRequestContext.source`, and publish the response through the
source-terminal route. A malformed source or a UUID source without local terminal
configuration is ignored without invoking the handler or publishing.

An unprefixed request is not intrinsically confined to one ECN: a deployment may
fan out the task family. Use an exact `target_terminal_id` whenever the deployment
requires one intended remote terminal, and rely on deployment routing plus broker
ACLs to constrain execution. The SDK performs no command discovery and never retries
a task automatically.

Start with the runnable [mesh observation](../how-to/observe-mesh-data.md) and
[terminal-addressed task](../how-to/dispatch-mesh-tasks.md) examples. Their offline
tests prove serialization and cleanup. Authorized staging observation has confirmed
that a configured route can deliver prefix-stripped TRACK and dedicated location
events through these watchers. The events themselves do not authenticate the peer of
origin, and terminal-addressed task routing remains externally unverified.
