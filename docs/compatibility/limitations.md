---
title: Compatibility and limitations
sidebar:
  order: 1
---

This page centralizes the SDK's supported interoperability profile and known gaps.
Read it before connecting to a live ECN.

## Supported profile

- MQTT v5 only, with verified TLS by default for non-loopback connections; the sole
  credential-free exception requires the explicit reviewed-container-network
  attestation and private-address revalidation on every connection attempt.
- Canonical UUID entity identities.
- The supported JSON and protobuf entity and location families.
- Fixed-depth entity and location watchers that wait for SUBACK.
- Client-observed location state and wait-for-next-update semantics.
- Exact local tasks using literal local or a configured terminal-derived UUID source.
- Exact terminal-addressed task serialization when source and target terminal UUIDs
  are supplied.
- TRACK publication at QoS 0; other supported publications at QoS 1.
- Future-compatible inbound fields and enum fallback with strict identity,
  timestamp, payload-size, and topic agreement.

## Deliberately unsupported

- Authoritative location queries; location reads reflect only state observed by the client.
- Arbitrary topic publish/subscribe, multi-level wildcards, or broker administration.
- Command discovery, advertisement, or registration convergence.
- Mesh topology discovery, route configuration, or arbitrary peer-prefixed topics.
- External platform, provisioning, or persistence APIs.

## Semantics to account for

- Method availability is not authorization; broker ACLs decide each operation.
- Preflight performs zero application publishes and cannot prove publish permission.
- Location caches contain only messages received by that client and are cleared on
  close.
- Publication receipts do not promise persistence or downstream processing.
- QoS 0 has no broker acknowledgement.
- Watchers can observe duplicates and gaps; buffer drops are local diagnostics.
- Task cancellation removes the sender's waiter but sends no cancellation message.
- Task deduplication is bounded and process-local.
- Entity/location mesh replication and task delivery scope depend on deployment
  routing; no SDK setting can enable a route.
- The operator browser API is a trusted local single-user surface, not an ECN API.

## Reconnect contract implemented; artifact and deployment verification pending

The local candidate implements typed full-jitter policy, per-attempt credential
refresh, redacted connection state/events, isolated denied-filter restoration,
explicit clean MQTT v5 sessions, and task delivery uncertainty without mutation
replay. Its exact installed-wheel and hosted gates have not run. Synchronous
standard-library/OpenSSL context parsing remains an immediate hard-deadline shutdown
blocker because cancellation is observed only after the native call returns.
Cancellation-resistant child cleanup can also outlive its bounded drain, so
zero-background-task shutdown remains unproved. This amendment has no staging or
production evidence. Do not infer deployed behavior from the mock, historical staging
observations, or source-level implementation.

TRACK remains QoS 0. Its delivery-phase receipt can prove only local send completion.
If a connection is lost after a QoS 0 send starts but before that local completion is
observed, the API raises `OutcomeUnknownError` with a safe operation identifier rather
than report a retry-safe failure. QoS 1 broker acceptance still does not prove
persistence, handler execution, or downstream processing. A QoS 1 send whose PUBACK
wait expires is also outcome-unknown, as is a task whose PUBACK succeeds but response
is lost. The task error retains its generated task ID. Reconcile downstream state
using the retained identifier; never retry any of these cases automatically.

## Validation status

The installed-wheel suites verify the complete public contract against the offline
mock. Authorized staging sessions have also exercised verified mTLS, bounded
entity/location observation and publication, same-ECN tasks, and receipt of data on a
configured mesh route. Those observations do not establish access on another ECN:
authentication, ACLs, routing, and enabled topic families are deployment-specific.

## Explicitly unverified paths

Cross-terminal task delivery, peer-specific origin attribution, bearer authentication,
terminal geolocation, angular-velocity axis interpretation, the live operator
application, and production compatibility remain unverified. Run read-only preflight
with the credentials issued for each target before opening a watcher. Treat any
authentication or SUBACK rejection as the target's current result; do not infer access
from a prior environment or from the offline mock.
