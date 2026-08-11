---
title: Observe ECN data
sidebar:
  order: 1
---

Use this quickstart to connect to an Expeditionary C2 Node (ECN), receive validated
tracks and detections from an integration you are authorized to observe, and inspect
terminal location updates. In three steps, you will validate the client, watch one
data category, and observe location state.

Before you begin, complete the [installation](../getting-started/installation.md)
workflow and set aside three shells for the concurrent commands.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact.

**Run profile:** offline model check and loopback mock. Preflight validates the configured
connection profile and does not require an observation integration filter. Read-only
observation commands are staging- and production-oriented when run with credentials
issued for that ECN and an explicitly authorized integration filter. Deployment
compatibility remains target-specific.

Shell labels (**A**, **B**, **C**) mark blocks that must run concurrently. Shell A
(the mock broker) stays open through the whole walkthrough. Each block inlines the
loopback-mock connection variables; for a live ECN, replace the entire mock block with
the applicable [authentication](../getting-started/authentication.md) block, including
its `unset ECN_ALLOW_INSECURE` safeguard.

## 1. Install and validate

Start the [loopback mock](../getting-started/mock-setup.md#run-the-mock-broker), then
run preflight from a second shell.

**Shell A — mock broker (leave running):**

```bash
picogrid-mock-ecn --mqtt-port 1883
```

**Shell B — preflight:**

```bash
python examples/preflight.py --check

export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1
python examples/preflight.py
```

Preflight connects and reports MQTT v5 CONNACK. It performs no application publish
and creates no subscription unless you explicitly request one bounded probe.

## 2. Watch one category

Start the watcher first and leave it running, then publish a synthetic TRACK from
another shell to unblock it. The watcher exits after one event because
`ECN_MAX_EVENTS=1`.

**Shell B — track watcher (leave running):**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_OBSERVED_INTEGRATION=example-integration  # authorized-source in live use
export ECN_MAX_EVENTS=1                              # 10+ against a live ECN
python examples/watch_tracks.py
```

For detections, run [`watch_detections.py`](../../examples/watch_detections.py).
Both examples open the watcher after connection, wait for SUBACK, use a bounded
buffer, and close the stream when the requested event count is reached.

**Shell C — track publisher (fires once):**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_ENTITY_CATEGORY=TRACK
export ECN_ENTITY_TYPE=synthetic-track
python examples/publish_entity.py
```

Shell B prints one entity event and exits.

## 3. Observe location state

[`get_ecn_location.py`](../../examples/get_ecn_location.py) waits for the next
`terminal-geolocation` location update without requiring its UUID in advance. Start
the listener, then within the 15-second observation window fire the publisher from
another shell.

**Shell B — location listener (leave running):**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_OBSERVATION_TIMEOUT=15
python examples/get_ecn_location.py
```

**Shell C — terminal-geolocation publisher:**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=terminal-geolocation
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000009
export ECN_LATITUDE=37.7749
export ECN_LONGITUDE=-122.4194
python examples/publish_location.py
```

The returned UUID and location are observations received by this client. They are
not an authoritative ECN lookup or historical query.

Your next step is to learn how [entities](../concepts/entities.md),
[locations](../concepts/locations.md), and [watcher lifecycle](../concepts/lifecycle.md)
fit together in an integration.
