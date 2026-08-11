---
title: Wire formats
sidebar:
  order: 4
---

Use this page when implementing or diagnosing Picogrid ECN SDK MQTT v5 topics,
delivery, JSON, protobuf, or task payloads.

## Topic families

These topic shapes define payload interoperability and support narrow broker ACL
configuration; applications use typed SDK methods rather than constructing topics.

```text
entity/{integration}/{entity_id}/{category}
entity_pb/{integration}/{category}
entity_location/{integration}/{entity_id}
entity_location_pb/{integration}/{entity_id}
task/{integration}/{entity_id}/{command}
{target_terminal_id}/task/{integration}/{entity_id}/{command}
task/{integration}/{entity_id}/{command}/response
{source_terminal_id}/task/{integration}/{entity_id}/{command}/response
```

All connections use MQTT v5. Watchers install only known fixed-depth filters and
never use a multi-level wildcard. Startup installs none.

## QoS and delivery

TRACK entity publication uses QoS 0. Other supported entity publications, location
publications including Position Location Information (PLI), and task
request/response publications use QoS 1. Entity, location, and task subscriptions
request QoS 1. A QoS 1 receipt requires a non-failure PUBACK. The reliability
amendment's exhaustive acknowledgment table accepts SUBACK `0x00`/`0x01`, PUBACK
`0x00`/`0x10`, and UNSUBACK `0x00`/`0x11`; `0x87` is authorization denial, SUBACK or
PUBACK `0x97` is a resource limit, and another recognized well-formed negative value
is a definite typed operation failure. A malformed SUBACK or UNSUBACK is a definite
`ProtocolError`. An unlisted-success, unknown, malformed, or otherwise uninterpretable
PUBACK with a valid matching packet identifier instead gives that mutation caller
`OutcomeUnknownError` with
`BROKER_ACKNOWLEDGMENT_PENDING` and safe correlation while separately invalidating
the connection as `PROTOCOL_FAILURE`. The SDK never replays the mutation. QoS 0 has
no broker acknowledgement and therefore cannot prove broker authorization.

A publication receipt confirms only that the local MQTT publish operation completed.
It does not guarantee broker persistence, ordering across filters, or downstream
acceptance and processing.

## JSON payloads

Entity JSON carries identity, integration, recorded time, type, category, status,
affiliation, optional display/domain metadata, optional fingerprint, and optional
location. The SDK encoder omits absent optional keys, emits required type and default
enum/metadata values, and requires an embedded location timestamp to equal the entity
event timestamp. Inbound decode ignores unknown object fields and accepts fingerprint
as optional.

Location JSON is an object with a `location` object. Latitude, longitude, and a
timezone-aware recorded time are required by the public model. Unknown fields are
ignored, while non-finite or out-of-range values are rejected.
The SDK encoder omits absent optional location keys. If an entity's optional embedded
location lacks a usable timestamp, decode still delivers the entity without that
location. Dedicated location messages remain strict.

For both encodings, coordinates are compliant with EPSG:4326, or with EPSG:4979 where
altitude is supplied as height above the WGS-84 ellipsoid. Altitude is metres, and
publishers convert mean-sea-level or geoid-referenced height before publication. An
omitted altitude key or field means the height is unknown. It does not mean zero, mean
sea level, or a height the client may infer, and altitude-dependent operations are
unavailable without one. Bearing is degrees clockwise from true north in `[0, 360)`;
publishers normalize a wraparound value such as 360 before publication.

For both JSON and protobuf, velocity is a three-value north/east/down vector in metres
per second. Angular velocity uses roll/pitch/yaw radians per second. Its external axis
interpretation is listed under [Compatibility and limitations](../compatibility/limitations.md).

For both encodings, `accuracy` is horizontal position uncertainty in metres expressed as
drms, `sqrt(sigma_north^2 + sigma_east^2)` over the one-sigma per-axis standard deviations
of the horizontal position error. drms is exact for any horizontal covariance, but its
approximately 63% containment holds only for a circular error, an isotropic two-dimensional
Gaussian with equal north and east components, and does not apply to an anisotropic or
non-Gaussian source.
Neither encoding carries a field identifying the convention, so publishers convert to drms
before publication. The value is a circular approximation that cannot express an oriented
error ellipse and never covers vertical uncertainty. Conversions are listed under
[Locations and Position Location Information](../concepts/locations.md).

JSON over MQTT v5 is the SDK and mock contract. External verification status is
tracked on the [limitations page](../compatibility/limitations.md).

## Protobuf

The installed schemas define the supported numeric wire fields for entity, entity
event, location, and entity-location wrapper messages under the public package
namespace.

The protobuf entity ID lives in its payload, while the protobuf location entity ID
lives in its topic. The SDK producer omits the optional entity category field and
derives category from the topic. Inbound
decode accepts a known matching category field; a future enum value becomes `OTHER`.
Strict topic agreement means a future payload category on a known topic is rejected,
and `OTHER` passes only when the topic category is also unknown. `OTHER` is
decode-only. GEOMETRIC is supported without a numeric enum because the confirmed
producer omits that optional field and derives category from the topic suffix.
When a future numeric category field and a future textual suffix are both unknown,
the SDK can establish only coarse unknown/`OTHER` compatibility.

Unknown protobuf fields are preserved by the runtime and ignored by public decode.
Generated message classes are not public API. The SDK emits required/default entity
values. Protobuf latitude and longitude are ordinary proto3 scalar fields: a decoded
zero cannot prove whether the sender explicitly supplied zero or omitted the field.
Timestamp presence remains strict.

For a runnable workflow, see [Decode protobuf](../how-to/protobuf-decode.md).

## Task JSON

Request:

```json
{
  "source": "00000000-0000-4000-8000-000000000011",
  "task_id": "0123456789abcdef",
  "_response_mode": "complete",
  "payload": {"request_field": "value"}
}
```

The compatibility fallback is the same object with `"source": "local"`.

Complete or pending response:

```json
{
  "status": "SUCCESS",
  "source": "00000000-0000-4000-8000-000000000012",
  "task_id": "0123456789abcdef",
  "payload": {"result_field": "value"},
  "_response_type": "full"
}
```

Failure adds only the optional flat `error_message`; its payload remains an
arbitrary JSON object. There is no wire timestamp or nested error structure.

ACK response:

```json
{
  "status": "SUCCESS",
  "source": "00000000-0000-4000-8000-000000000012",
  "task_id": "0123456789abcdef",
  "payload": {"ack": true, "message": "Task started"},
  "_response_type": "ack"
}
```

ACK sends no final response. Fire-and-forget sends no response. A literal-local
request uses `source="local"` in its response. A configured same-terminal exchange
uses that terminal UUID in both payloads while retaining the unprefixed response
topic. A canonical remote source is accepted only when the handler
has `ECNConfig.terminal_id`; the response is published through that exact source
terminal prefix and carries the responding terminal UUID in its payload `source`.
The originating dispatcher receives the ordinary unprefixed response and verifies
that source against its requested target. The SDK publishes the ACK before invoking
the handler so exactly one acknowledgement is observable before handler work begins.
Legacy hostname-valued source aliases are deliberately excluded: public sources are
only literal `local` or canonical terminal UUIDs. Remote dispatch therefore requires
an exact `target_terminal_id`; an unprefixed configured-terminal request accepts only
the same-terminal response source.
Byte identity is not claimed; the contract is the documented JSON fields, values,
topic route, QoS, and single-response lifecycle.

## Coordinate values

Location bytes are geodetic on both wire formats: decimal degrees of latitude and
longitude, and metres of height above the WGS-84 ellipsoid where altitude is supplied.
Velocity is a positional `[north, east, down]` array in metres per second.

The client converts to and from EPSG:4978 Cartesian coordinates in memory, and the ECN
neither accepts nor emits an ECEF value. There is no ECEF field on either wire format, no
topic carries one, and a converted position is never written back into published or
observed state. See the [coordinate reference matrix](coordinate-reference.md).

## Compatibility boundary

Broker ACLs, deployed identifier grammar beyond the SDK's conservative validation,
deployed terminal routes, authentication profiles, and command availability are not
established by this wire reference. Confirm them with read-only preflight and the
narrowest authorized watcher on each target. See
[Compatibility and limitations](../compatibility/limitations.md).
