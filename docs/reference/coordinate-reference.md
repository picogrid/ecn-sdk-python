---
title: Coordinate reference matrix
sidebar:
  hidden: true
---

This matrix accounts for every coordinate-bearing value in the public client: the models,
the JSON and protobuf codecs, the operator application, and the examples and fixtures. It
records what each value means, not how it is transported. `Location.source` is a free-text
producer label with no coordinate, axis, unit, or datum content, so it is outside this
matrix and excluded from every aggregate row.

The `Representation` column separates three kinds of value. `wire` is data carried in a
published or decoded payload. `in-memory` is a client-side copy of a wire value. `display
projection` is a rendering transform applied downstream of the wire and never fed back
into it.

The `Evidence` column follows the same discipline as the rest of this guide.
`source-confirmed` means the behavior is established by the pinned reference
implementation. `public-client policy` means the public client selects a convention the
pinned reference does not fix. `unverified` means the interpretation is neither confirmed
nor contradicted by available evidence. A row covering several fields carries `mixed` and
enumerates the evidence class of each constituent field in a note beneath its table, so
an aggregate never presents a stronger class than the fields it inherits.

Three representations that appear in adjacent systems are absent here, and their absence is
part of the account:

- No orientation or attitude quaternion exists, so no ENU quaternion convention applies.
  Orientation is not a location field of this wire.
- No MGRS or UTM grid reference exists.
- No geoid model or mean-sea-level assumption exists. Altitude is ellipsoidal throughout,
  and no geoid separation is applied or required at runtime.

## Public model

Source: `src/picogrid_ecn_client/models/location.py`.

| Row key | Value                  | CRS or frame                                  | Axis order and units                    | Altitude reference                | Representation | Evidence             |
| ------- | ---------------------- | --------------------------------------------- | --------------------------------------- | --------------------------------- | -------------- | -------------------- |
| LOC-00  | `Location.latitude`    | WGS-84 geographic, EPSG:4326                  | decimal degrees, `[-90, 90]`            | —                                 | wire           | source-confirmed     |
| LOC-01  | `Location.longitude`   | WGS-84 geographic, EPSG:4326                  | decimal degrees, `[-180, 180]`          | —                                 | wire           | source-confirmed     |
| LOC-02  | `Location.altitude`    | WGS-84 3D, EPSG:4979, where supplied          | metres                                  | height above the WGS-84 ellipsoid | wire           | public-client policy |
| LOC-03  | `Location.bearing`     | true north reference                          | degrees clockwise, `[0, 360)`           | —                                 | wire           | source-confirmed     |
| LOC-04  | `Location.accuracy`    | horizontal, position frame                    | metres, drms                            | —                                 | wire           | public-client policy |
| LOC-05  | `Location.recorded_at` | —                                             | timezone-aware, normalized to UTC       | —                                 | wire           | source-confirmed     |
| LOC-06  | `Velocity`             | local tangent plane, NED                      | `[north, east, down]` metres per second | —                                 | wire           | source-confirmed     |
| LOC-07  | `AngularVelocity`      | unresolved; the pinned source is a NED vector | `[roll, pitch, yaw]` radians per second | —                                 | wire           | unverified           |
| LOC-08  | `Location.confidence`  | —                                             | dimensionless, `[0, 1]`                 | —                                 | wire           | unverified           |

`LOC-07` names three rotational axes over a pinned vector whose frame is not established.
The interpretation is recorded on the [limitations page](../compatibility/limitations.md)
and no conversion consumes it.

These rows apply wherever a `Location` appears, whether as a dedicated location message or
embedded in an entity or entity event.

## Local conversion surface

Sources: `src/picogrid_ecn_client/models/location.py`,
`src/picogrid_ecn_client/_geodesy.py`.

| Row key | Value                       | CRS or frame                         | Axis order and units           | Altitude reference                | Representation | Evidence             |
| ------- | --------------------------- | ------------------------------------ | ------------------------------ | --------------------------------- | -------------- | -------------------- |
| CNV-60  | `ECEFPosition`              | earth-centered earth-fixed, EPSG:4978 | `[x, y, z]` metres             | —                                 | in-memory      | public-client policy |
| CNV-61  | `GeodeticPosition`          | WGS-84 3D, EPSG:4979                 | decimal degrees, then metres   | height above the WGS-84 ellipsoid | in-memory      | public-client policy |
| CNV-62  | `ECEFVelocity`              | EPSG:4978 axes                       | `[x, y, z]` metres per second  | —                                 | in-memory      | public-client policy |
| CNV-63  | `Location.to_ecef`          | EPSG:4979 to EPSG:4978               | degrees and metres, to metres  | height above the WGS-84 ellipsoid | in-memory      | public-client policy |
| CNV-64  | `Location.to_ecef_velocity` | NED to EPSG:4978 axes                | metres per second, both frames | not used                          | in-memory      | public-client policy |
| CNV-65  | `GeodeticPosition` rotation | NED and EPSG:4978 axes, both ways    | metres per second, both frames | not used                          | in-memory      | public-client policy |

No value in this section reaches the wire. Every ECEF quantity is produced on request from
a decoded or locally constructed geodetic value and is never published, decoded, or cached
as observed state. The conversions read their inputs and return new objects, so a
`Location` is unchanged by converting it.

The ellipsoid is WGS-84 throughout: semi-major axis 6378137.0 m, inverse flattening
298.257223563. The forward conversion is the standard closed form. The inverse applies the
closed-form solution of Vermeille (2011), which is exact and non-iterative, so no
convergence tolerance enters the result. Both use the Python standard library only; the
client takes no geodesy dependency.

`CNV-64` and `CNV-65` rotate a velocity between the local tangent plane and the ECEF axes.
The rotation is fixed by latitude and longitude alone, so no altitude is required and none
is consumed. The east axis has no ECEF Z component at any position.

### Accuracy

Conversions hold to these bounds over the supported domain. The float64 floor at Earth
radius is roughly 1.4e-9 m, so no tighter claim is meaningful.

| Conversion                 | Bound                              |
| -------------------------- | ---------------------------------- |
| Geodetic to ECEF           | 1e-6 m per component               |
| ECEF to geodetic and back  | 1e-6 m per component               |
| Geodetic to ECEF and back  | 1e-6 m of ECEF distance and height |
| Velocity, either direction | 1e-12 relative to the speed        |

Each bound names a metric. A per-component bound is the absolute error of each ECEF
component in metres, against ground truth for the forward conversion and against the
original vector for the ECEF round trip. `ECEF distance` is the Euclidean distance in
ECEF metres between the original vector and the vector obtained by converting the
recovered geodetic triple forward again, and `height` is the absolute difference of
ellipsoidal height in metres.

Recovered latitude and longitude carry no metre bound. A pole has no longitude and either
sign is correct on the antimeridian, so those two cases are governed by the conventions
under Degenerate positions rather than by a tolerance. Away from the poles, latitude and
longitude recover to within 1e-12 degrees, and on the antimeridian that bound holds on
the magnitude.

The supported domain is the full range of latitude and longitude with altitude in
`[-11000, 100000]` m. The forward conversion is defined outside that band and the inverse
remains exact, but the bounds above are stated only within it.

The relative velocity bound applies to a non-zero speed. A zero north/east/down velocity
converts to a zero ECEF vector exactly, and the reverse, at every orientation, because
each rotated component is a sum of products with zero.

Measured against ground truth generated at 50 decimal digits, the worst observed forward
error is 1.7e-9 m and the worst velocity error is 2.3e-16 relative, both at the float64
floor rather than at the stated bound. Over the same sweep, the worst geodetic round trip
is 2.9e-9 m of ECEF distance and 2.3e-9 m of height.

### Degenerate positions

Two inputs have no unique answer and take a documented convention rather than a tolerance.

- **On the polar axis**, where `x` and `y` are both zero, longitude is undefined. It is
  reported as zero and latitude is reported as exactly `90` or `-90`. At latitude `90`
  or `-90`, the forward conversion emits exact zeros for both equatorial components, so
  a pole survives a round trip as longitude zero.
- **On the antimeridian**, the sign of the `y` component decides the result: positive zero
  yields `180` and negative zero yields `-180`.

Positions within roughly 43 km of the center of the ellipsoid have no geodetic solution.
`ECEFPosition.to_geodetic` raises `ValidationError` with code `degenerate_ecef` there
rather than returning a value.

## Protobuf wire

Source: `src/picogrid_ecn_client/schemas/public/common.proto`, `LocationMessage`.

| Row key | Value                       | CRS or frame                         | Axis order and units                                      | Altitude reference                | Representation | Evidence             |
| ------- | --------------------------- | ------------------------------------ | --------------------------------------------------------- | --------------------------------- | -------------- | -------------------- |
| PB-10   | `latitude`, field 1         | WGS-84 geographic, EPSG:4326         | `double`, decimal degrees                                 | —                                 | wire           | source-confirmed     |
| PB-11   | `longitude`, field 2        | WGS-84 geographic, EPSG:4326         | `double`, decimal degrees                                 | —                                 | wire           | source-confirmed     |
| PB-12   | `altitude`, field 3         | WGS-84 3D, EPSG:4979, where supplied | `float`, metres                                           | height above the WGS-84 ellipsoid | wire           | public-client policy |
| PB-13   | `bearing`, field 4          | true north reference                 | `float`, degrees clockwise                                | —                                 | wire           | source-confirmed     |
| PB-14   | `accuracy`, field 5         | horizontal, position frame           | `float`, metres, drms                                     | —                                 | wire           | public-client policy |
| PB-15   | `recorded_at`, field 7      | —                                    | `google.protobuf.Timestamp`, UTC seconds and nanoseconds  | —                                 | wire           | source-confirmed     |
| PB-16   | `velocity`, field 8         | local tangent plane, NED             | repeated `float`, `[north, east, down]` metres per second | —                                 | wire           | source-confirmed     |
| PB-17   | `angular_velocity`, field 9 | unresolved                           | repeated `float`, `[roll, pitch, yaw]` radians per second | —                                 | wire           | unverified           |
| PB-18   | `confidence`, field 10      | —                                    | `float`, dimensionless                                    | —                                 | wire           | unverified           |

Latitude and longitude are `double`; every other numeric field is single-precision
`float`. Altitude therefore carries less precision on the protobuf path than in the model.
Latitude and longitude are also ordinary proto3 scalars, so a decoded zero cannot be
distinguished from an omitted field.

`PB-15` is a nested message rather than a scalar, so its presence is tracked. The producer
always emits it. A dedicated location message that omits it fails decode; a location
embedded in an entity event inherits the envelope timestamp instead. A decoded timestamp is
always UTC-aware, and one outside the representable datetime range is rejected.

## JSON wire

Source: `src/picogrid_ecn_client/_protocol/codec.py`.

| Row key | Value                    | CRS or frame                   | Axis order and units                    | Altitude reference                | Representation | Evidence         |
| ------- | ------------------------ | ------------------------------ | --------------------------------------- | --------------------------------- | -------------- | ---------------- |
| JS-20   | Scalar location keys     | as the public model rows above | as the public model rows above          | height above the WGS-84 ellipsoid | wire           | mixed            |
| JS-21   | `velocity` array         | local tangent plane, NED       | `[north, east, down]` metres per second | —                                 | wire           | source-confirmed |
| JS-22   | `angular_velocity` array | unresolved                     | `[roll, pitch, yaw]` radians per second | —                                 | wire           | unverified       |

`JS-20` inherits `LOC-00` through `LOC-05` and `LOC-08`: latitude, longitude, bearing, and
recorded time are `source-confirmed`; altitude and accuracy are `public-client policy`;
confidence is `unverified`.

`JS-21` and `JS-22` are positional arrays. Their order is the only thing that carries axis
identity, so a reordering is indistinguishable from a value change.

## Display projection

Source: `operator-app/frontend/src/main.ts`.

| Row key | Value                  | CRS or frame            | Axis order and units                 | Altitude reference | Representation     | Evidence         |
| ------- | ---------------------- | ----------------------- | ------------------------------------ | ------------------ | ------------------ | ---------------- |
| DSP-30  | Operator map rendering | Web Mercator, EPSG:3857 | projected metres, then screen pixels | not used           | display projection | source-confirmed |

The map is constructed without a `crs` option, so Leaflet applies its default EPSG:3857
projection. This holds whether a configured basemap is present or the offline graticule is
drawn. The projection consumes latitude and longitude only, ignores altitude, and is
one-way: no projected value is written back into observed or published state. EPSG:3857 is
not an ECN wire format.

Web Mercator is undefined at the poles, so the projection covers latitudes of roughly
`[-85.06, 85.06]` and the offline graticule is drawn to that bound. A position outside
that band is carried on the wire and held in memory unchanged; only its rendering is
constrained.

## Operator application

Sources: `operator-app/backend/operator_app/state.py`,
`operator-app/frontend/src/types.ts`.

| Row key | Value                                                                          | CRS or frame                   | Axis order and units           | Altitude reference                | Representation | Evidence |
| ------- | ------------------------------------------------------------------------------ | ------------------------------ | ------------------------------ | --------------------------------- | -------------- | -------- |
| OP-40   | `LocationView` latitude, longitude, altitude, bearing, accuracy, recorded time | as the public model rows above | as the public model rows above | height above the WGS-84 ellipsoid | in-memory      | mixed    |

`OP-40` inherits `LOC-00` through `LOC-05`: latitude, longitude, bearing, and recorded
time are `source-confirmed`; altitude and accuracy are `public-client policy`. It carries
no velocity, angular velocity, or confidence value.

`LocationView` is populated only from decoded inbound events and carries no conversion.

## Examples and fixtures

| Row key | Value                                                                                            | CRS or frame                   | Axis order and units           | Altitude reference                | Representation | Evidence |
| ------- | ------------------------------------------------------------------------------------------------ | ------------------------------ | ------------------------------ | --------------------------------- | -------------- | -------- |
| EX-50   | `ECN_LATITUDE`, `ECN_LONGITUDE`, `ECN_ALTITUDE`, `ECN_BEARING`, `ECN_ACCURACY`, `ECN_CONFIDENCE` | as the public model rows above | as the public model rows above | height above the WGS-84 ellipsoid | in-memory      | mixed    |
| EX-51   | `tests/fixtures/protocol/location_update.json`                                                   | as the public model rows above | as the public model rows above | height above the WGS-84 ellipsoid | wire           | mixed    |

`EX-50` inherits `LOC-00` through `LOC-04` and `LOC-08`: latitude, longitude, and bearing
are `source-confirmed`; altitude and accuracy are `public-client policy`; confidence is
`unverified`.

`EX-51` inherits `LOC-00` through `LOC-03`, `LOC-05`, `LOC-07`, and `LOC-08`: latitude,
longitude, bearing, and recorded time are `source-confirmed`; altitude is
`public-client policy`; angular velocity and confidence are `unverified`. It carries no
accuracy value.
