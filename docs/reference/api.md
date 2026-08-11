---
title: Picogrid ECN SDK API
tableOfContents: false
sidebar:
  order: 1
---

Use this page to navigate the Picogrid ECN SDK's supported public Python surface.
All supported imports are available from `picogrid_ecn_client` unless noted. The
distribution name remains `picogrid-ecn-client`.

| Surface | Operations | Contract |
| --- | --- | --- |
| `ECNClient` | `start`, `wait_until_ready`, `connection_events`, `notify_credentials_changed`, `request_retry`, `preflight`, `close`, async context | MQTT v5 lifecycle; preflight is read-only. See the generated [client signatures and failure modes](python/client/ecn-client.md). |
| `client.clock` | `measure`, `require_within` | Optional configured-ECN NTP diagnostic; works before or after a failed MQTT startup, closes with the client, and never changes a clock. `require_within` raises `ClockToleranceError` outside tolerance; its `report` attribute preserves the measured `ClockReport`. |
| `client.entities` | `watch`, `publish` | Lazy typed watchers and caller-authorized publication. |
| `client.locations` | `watch`, `last_observed`, `wait_for_update`, `wait_for_terminal_geolocation`, `publish` | Reads are client-observed MQTT state only; ordinary location publication also represents PLI. |
| `client.tasks` | `register`, `unregister`, `send` | Exact local and canonical terminal-addressed tasks. |
| `EventStream[T]` | async iteration, `dropped_count`, `decode_error_count`, `aclose` | Bounded decoded-event queue and per-watcher diagnostics; explicit cleanup. |
| Public protobuf | `decode_entity_event_protobuf`, `decode_entity_location_protobuf` | Bounded typed decoders; no generated class API. |

`wait_until_ready` waits for strict transport and active-subscription readiness.
`connection_events` returns a capacity-one latest-value stream of redacted connection
status. The credential-change and retry notifications wake eligible recovery work;
they do not queue or replay entity, location, task, or FMV mutations.

Configuration is explicit in `ECNConfig`: host, MQTT and NTP endpoints, integration name,
optional connected-terminal UUID,
authentication, TLS, JSON or protobuf format, connection/operation/task/shutdown
timeouts, reconnect policy, watcher buffer, maximum payload, and maximum outstanding
operations. MQTT v5 is fixed and is not configurable.

`ClockReport` defines offset as ECN time minus local time. It includes the selected
lowest-delay sample, round-trip delay, conservative local timing uncertainty, jitter
and spread, sample counts, NTP stratum and leap state, measurement time, configured
endpoint, and optional tolerance result. The uncertainty bounds local offset error
attributable to paired-read capture timing and tolerated within-sample wall/monotonic
divergence; it does not bound network asymmetry or server accuracy. `require_within`
passes only when `abs(offset_seconds) + local_capture_uncertainty_seconds` is within
the requested tolerance. Transport-level timeouts and endpoint errors retry
sequentially while the ten-attempt budget and overall deadline remain. If the attempt
cap is reached after a valid response, the report preserves that usable subset and
records requested versus completed samples; zero valid responses, the overall
deadline, and protocol-invalid responses remain errors. `round_trip_delay_seconds` is
the bracketed local monotonic interval minus the server receive-to-transmit interval,
so tolerated local wall-clock movement does not alter sample ordering. The request
timestamp is captured after UDP
endpoint setup, immediately at the send boundary. Receive clocks are captured
immediately after the user-space socket receive returns and before cleanup, so setup
and post-receive cleanup or return latency do not bias the reported offset or delay.
This is not a kernel packet-arrival timestamp. One report is an offset measurement,
not a drift estimate.
If bracketed wall and monotonic readings detect a local clock step beyond their
measurement uncertainty, the operation raises `ClockProtocolError` with code
`clock_local_step_detected` and returns no report.
If a local paired-clock capture itself takes too long to support an accurate offset,
the same exception type uses code `clock_local_read_uncertain` and returns no report.

See the separate [configuration](configuration.md) and
[exceptions](exceptions.md) references for field defaults and failure meanings. See
the [wire-format reference](wire-formats.md) for topics, delivery, and payloads.

Task results expose `task_id`, `status`, typed or JSON-object `data`, optional
`error_message`, and the local receive/completion time. They do not expose invented
nested error codes. Future inbound status strings map to `TaskStatus.UNKNOWN`.

For complete signatures, parameters, defaults, model fields, enum values, and
exceptions, use the generated [Python API reference](python/index.md). It covers
every supported symbol, including the separately documented offline
[testing surface](python/testing/index.md), and links each page back to the
workflow and security guides that govern operational meaning. See also the
[runnable Python examples](../shipped-tooling.md#runnable-python-examples).
