---
title: Locations and Position Location Information
sidebar:
  order: 4
---

`Location` represents a timestamped WGS-84 position with optional altitude, bearing,
accuracy, source, velocity, angular velocity, and confidence. Use it to publish or
observe the position and motion of an [entity](entities.md).

## Location and motion model

A `Location` is compliant with EPSG:4326, or with EPSG:4979 where altitude is supplied as
height above the WGS-84 ellipsoid. Latitude and longitude are decimal degrees and altitude
is metres; publishers convert mean-sea-level or geoid-referenced height before
publication. Bearing is degrees clockwise from true north in `[0, 360)`; publishers
normalize a wraparound value such as 360 before publication. An absent altitude means the
height is unknown. It does not mean zero, mean sea level, or a height the client may
infer, and altitude-dependent operations are unavailable without one.

Velocity is ordered north/east/down and measured in metres per second. Angular
velocity is exposed as roll/pitch/yaw radians per second. Review the externally
unverified axis interpretation on the [limitations page](../compatibility/limitations.md).

## Cartesian conversion

`Location.to_ecef` converts a position to EPSG:4978 Cartesian metres, and
`Location.to_ecef_velocity` rotates the velocity onto the same axes.
`ECEFPosition.to_geodetic` inverts the position conversion, returning a
`GeodeticPosition` rather than a `Location`, because a coordinate triple has no
timestamp or source to reconstruct.

```python
from picogrid_ecn_client import Location


def to_cartesian(location: Location) -> None:
    position = location.to_ecef()
    print(position.x, position.y, position.z)
    print(position.to_geodetic().altitude)
```

Altitude is read as height above the ellipsoid. A location without one raises
`ValidationError` with code `missing_altitude`; pass
`assume_zero_ellipsoidal_height=True` to place the position on the ellipsoid instead.
Velocity conversion needs no altitude, because the rotation depends on latitude and
longitude alone. `GeodeticPosition.to_ned_velocity` rotates an `ECEFVelocity` back onto
north/east/down at that position, making the velocity rotation available in both
directions.

The ECN carries geodetic coordinates only. These conversions are local to the client,
they leave the `Location` unchanged, and no ECEF value is published or decoded. Axis
order, tolerances, the supported domain, and the polar and antimeridian conventions are
in the [coordinate reference matrix](../reference/coordinate-reference.md).

## Position uncertainty

`accuracy` is horizontal position uncertainty in metres, expressed as **drms** (distance
root mean square):

```text
drms = sqrt(sigma_north^2 + sigma_east^2)
```

where the sigmas are the one-sigma per-axis standard deviations of the horizontal
position error. drms is exact for any horizontal covariance, but its approximately 63%
containment holds only for a circular error, an isotropic two-dimensional Gaussian with
equal north and east components, and does not apply to an anisotropic or non-Gaussian
source.

### Publishing

Convert to drms before publishing. The wire carries no field naming the convention a
value used.

The covariance and 2drms rows hold for any horizontal error, because 2drms is twice
drms by definition. The CEP50 and R95 rows assume a circular error, meaning an isotropic
two-dimensional Gaussian with equal north and east components. No scalar factor converts
CEP50 or R95 to drms for an anisotropic or non-Gaussian source; publish
`sqrt(sigma_north^2 + sigma_east^2)` from the covariance instead.

| If your source reports | Then `accuracy` is |
|---|---|
| a horizontal covariance or per-axis sigmas | `sqrt(sigma_north^2 + sigma_east^2)` |
| 2drms | `value / 2` |
| CEP50 | `value * 1.200` |
| R95 | `value * 0.578` |
| one-sigma per-axis, isotropic | `value * 1.414` |

### Consuming

The 2drms row is exact by definition. The CEP50 and R95 factors invert the
circular-error rows above and carry the same assumption. They must not be applied to an
anisotropic or non-Gaussian source. For such a source, drms remains a valid magnitude but
does not map to a containment percentage.

| To obtain | Compute |
|---|---|
| CEP50 | `0.833 * accuracy` |
| R95 | `1.731 * accuracy` |
| 2drms | `2 * accuracy` |

### Limits

`accuracy` is a circular approximation. It cannot express an oriented error ellipse, so a
sensor whose along-range and cross-range errors differ substantially loses that structure.
It never covers vertical uncertainty.

`confidence` is a separate dimensionless value between 0 and 1. It carries no unit and no
positional-uncertainty meaning.

## Position Location Information (PLI)

Position Location Information (PLI) associates a location with one canonical entity
UUID. The expansion follows the
[Position Location Information Concept of Operations](https://man.fas.org/dod-101/sys/ship/weaps/docs/plicon.htm).

Publish PLI by pairing one canonical entity UUID with one validated `Location`.
It uses the same supported location topic and payload family as any other location
update; PLI is not a separate Python model, MQTT topic, entity category, discovery
mechanism, or tracking protocol.

Use this location publication when an integration reports the position of its own platform or another
entity it is authorized to update. Keep the UUID stable, use timezone-aware
timestamps, and follow the north/east/down velocity convention above.

```python
receipt = await client.locations.publish(
    entity_id=entity_id, location=location
)
```

Publishing is a broker-authorized mutation. Its receipt reports local MQTT
completion, not persistence or consumer processing. See the runnable
[`publish_location.py`](../../examples/publish_location.py) example.

## Observed location state

Location reads are strictly process-local MQTT observations:

- `last_observed(entity_id, integration=...)` returns a value already decoded by
  this client, or `None`.
- `wait_for_update(...)` lazily subscribes to the narrow matching family and waits
  for the next message.

The cache updates only from successfully decoded dedicated location messages or
entity events carrying an embedded location. With no integration argument,
`last_observed` returns the most recently received matching location across
integrations; this is arrival order, not authoritative event-time history.

Neither method queries authoritative server state. The cache may miss messages sent
before subscription, disappears when the client closes, and must not be described
as ECN-wide history.

## ECN location observation

The ECN location broadcast uses the `terminal-geolocation` integration segment. The
SDK observes it through the dedicated location families. The specialized observation
method uses one fixed-depth UUID segment and returns the UUID from the received event;
it never performs a query.

```python
from picogrid_ecn_client import ECNClient


async def wait_for_one(client: ECNClient) -> None:
    event = await client.locations.wait_for_terminal_geolocation(
        timeout=10,
    )
    assert (
        client.locations.last_observed(
            event.entity_id,
            integration="terminal-geolocation",
        )
        == event.location
    )
```

See [observe a location](../how-to/observe-location.md) and
[publish entities, locations, and PLI](../how-to/pli-entities.md).
