---
title: Picogrid ECN SDK
description: Connect sensors, effectors, and operator tools to authorized ECN data and tasking.
hero:
  tagline: Connect sensors, effectors, and operator tools to authorized data and tasking at the edge.
  actions:
    - text: Install the SDK
      link: /getting-started/installation/
      icon: right-arrow
---

An Expeditionary C2 Node (ECN) is a deployable command-and-control node that connects
sensors, effectors, platforms, operators, and their operational data at the edge. The
Picogrid ECN SDK gives Python applications a typed interface to the entity, location,
and task data available through that node.

Use the SDK to publish sensor observations, operate an effector task handler, stream
tracks and locations into mission tools, or run the included operator view. It is
designed for partner sensor and effector integrators and credentialed Department of
War personnel, including operators and integration developers.

The SDK connects to one ECN through its primary MQTT v5 transport interface.
Broker ACLs determine which local or mesh-routed entity, location, and task data the
credential may access; routing configuration determines what reaches that ECN. The
SDK does not infer access beyond those decisions.

Live use normally requires a reachable authorized ECN endpoint, an assigned
integration name, a verified CA, and either an approved bearer token or mTLS client
certificate and key. Bearer authentication also requires the exact MQTT username you
were provided. A separate credential-free plaintext mode exists only for an
explicitly attested, operator-reviewed private container network.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact.

## Connect read-only first

After [installing the SDK](getting-started/installation.md), configure
[authentication and verified TLS](getting-started/authentication.md), use the explicit
reviewed-container-network boundary documented there, or start with the
[loopback mock](getting-started/mock-setup.md). Then create and validate a named
profile and open one track watcher:

```bash
picogrid-ecn configure --profile NAME
picogrid-ecn doctor --profile NAME
picogrid-ecn preflight --profile NAME
export ECN_OBSERVED_INTEGRATION=authorized-source
export ECN_MAX_EVENTS=10
python examples/watch_tracks.py --profile NAME
```

[Read-only preflight](getting-started/preflight.md) performs zero application
publications. The watcher is also read-only and subscribes only to the requested
track family. The [Observe ECN data quickstart](quickstarts/observe-data.md) walks
through the same path with the offline mock and an authorized live profile.

The client creates no application subscription during startup. Watchers subscribe
only when opened, wait for SUBACK, and release their filters on close.

## Choose your outcome

| I want to… | Start here | Designed for |
|---|---|---|
| Observe tracks, detections, or locations | [Observe ECN data](quickstarts/observe-data.md) | Credentialed operators and developers |
| Connect a sensor | [Build a sensor publisher](quickstarts/sensor-publisher.md) | Sensor integration teams |
| Connect an effector | [Build an effector task handler](quickstarts/effector-handler.md) | Effector integration teams |
| Run an operator display | [Run the operator view](quickstarts/operator-view.md) | Credentialed operators and developers |

Position Location Information (PLI) is the position and location data associated
with a platform or entity. The SDK uses the ordinary `Location` model for PLI.

:::caution[Authorization stays on the ECN]
An SDK method shows what the client can encode; it does not grant permission. Broker
authentication and ACLs decide which connections, subscriptions, and publications
are accepted.
:::

Before live use, review [Compatibility and limitations](compatibility/limitations.md).
The offline mock is a development tool, not evidence of deployed ECN behavior.
