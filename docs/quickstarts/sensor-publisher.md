---
title: Build a sensor publisher
sidebar:
  order: 2
---

Use this quickstart to give a sensor one canonical UUID, publish its typed entity
event, and attach a location, including Position Location Information (PLI). In three
steps, you will validate the examples, choose a durable identity, and publish the
sensor and its position.

Before you begin, complete [installation](../getting-started/installation.md) and
have two shells available for the mock broker and publisher.

**Run profile:** offline model check and loopback mock. The publication commands are
staging- and production-oriented only for a sensor integration whose broker ACLs
authorize the exact entity and location publications. Live use changes ECN-visible
state.

Shell labels (**A**, **B**) mark blocks that must run concurrently. Each block
inlines the loopback-mock connection variables; for a live ECN, replace them with
the [authentication](../getting-started/authentication.md) block.

## 1. Validate the examples offline

```bash
python examples/publish_entity.py --check
python examples/publish_location.py --check
```

These checks validate the example inputs without connecting to a broker.

## 2. Choose one durable identity

Generate a UUID once for the sensor and persist it in your own configuration. Reuse
that UUID across JSON or protobuf entity events, ordinary location updates, PLI, and
any task targeting for the same physical or logical sensor.

## 3. Publish the entity and position

**Shell A — mock broker (leave running):**

```bash
picogrid-mock-ecn --mqtt-port 1883
```

Skip Shell A if you are targeting a live ECN.

**Shell B — publisher:**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_ENTITY_CATEGORY=SENSOR
export ECN_ENTITY_TYPE=weather-station
export ECN_ENTITY_NAME=synthetic-sensor
python examples/publish_entity.py

export ECN_LATITUDE=0
export ECN_LONGITUDE=0
python examples/publish_location.py
```

Each command is a mutation and must be authorized by the broker ACL. A publication
receipt reports the MQTT operation; it does not promise persistence or downstream
processing.

Your next step is the [sensor integration guide](../integrations/sensors.md), where
you can choose update cadence, wire format, QoS, and lifecycle behavior.
