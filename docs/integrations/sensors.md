---
title: Sensor integration
sidebar:
  order: 1
---

A sensor integration uses the Picogrid ECN SDK to publish measurements or detections
to an Expeditionary C2 Node (ECN) as entities and location updates. The supported
pattern gives every entity one canonical UUID and uses that UUID to correlate its
entity and location events.

Start with the [sensor publisher quickstart](../quickstarts/sensor-publisher.md) to
validate the basic workflow, then use the publication and observation tasks below for
your integration.

## Model the sensor

1. Assign one durable [canonical UUID](../concepts/uuids.md).
2. Choose the narrowest entity category: commonly `SENSOR`, `DETECTION`, or `TRACK`.
3. Choose a stable, application-specific `type`.
4. Use display metadata for operator-facing labels, not identity.
5. Reuse the configured integration name in every publication.

## Publish updates

Publish an entity when identity or metadata becomes available and when its meaningful
state changes. Publish dedicated locations or
[Position Location Information (PLI)](../concepts/locations.md#position-location-information-pli)
when position changes. An entity may also carry an embedded location with the same
event timestamp.

The JSON and protobuf formats share the same typed model. Select the publication
format with `ECN_WIRE_FORMAT`; do not change formats to bypass validation.

TRACK entity publication uses QoS 0. Other entity categories and location
publications, including PLI, use QoS 1. Design your source to tolerate duplicate
delivery and to avoid unbounded retry loops. The SDK does not automatically
republish after a failure.

## Validate and observe

Use the network-free checks first:

```bash
python examples/publish_entity.py --check
python examples/publish_location.py --check
```

Against the loopback mock, open an exact observer before publishing to exercise the
complete round trip. Against a live ECN, publish only synthetic or operational data
the broker ACL explicitly authorizes.

Continue with these tasks:

- [Publish entities, locations, and PLI](../how-to/pli-entities.md)
- [Watch tracks and detections](../how-to/tracks-detections.md)
- [Observe ECN location](../how-to/observe-location.md)
- [Observe mesh-routed data](../how-to/observe-mesh-data.md)

See [Wire formats](../reference/wire-formats.md) and
[Compatibility and limitations](../compatibility/limitations.md).
