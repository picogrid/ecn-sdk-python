# Changelog

All notable changes to the Picogrid ECN SDK are recorded here under
[Semantic Versioning](VERSIONING.md). Each heading names a release and its release
date.

## [0.2.0] - 2026-10-09

### Breaking wire changes

- Protobuf location topics change from
  `entity_location_pb/{integration}/{entity_id}` to
  `entity_location_pb/{integration}`. Entity identity now travels in the
  `EntityLocationMessage.entity_id` payload field as exactly 16 raw UUID bytes, and
  the subscription family changes from `entity_location_pb/+/+` to
  `entity_location_pb/+`. Consumers that filtered protobuf locations by topic entity
  segment must subscribe by integration and filter the decoded `LocationEvent` ID.
- `decode_entity_location_protobuf(...)` no longer accepts an `entity_id` keyword;
  remove that argument because the decoder reads identity from the payload.
- `response` is now a reserved integration name, so client task topics stay
  disjoint from the `task/response/...` namespace used for responses addressed to
  Legion. Terminal-to-terminal task responses keep the `0.1.0` shape
  `{source_terminal_id}/task/{integration}/{entity_id}/{command}/response`.
- The protobuf snapshot converges to edge-sdk's category, affiliation, entity, and
  location schema breadth. `EntityCategory` adds `WEATHER`, `ZONE`, `VEHICLE`, and
  `UXV` (`GEOMETRIC` remains supported), while `Affiliation` adds `PENDING`,
  `ASSUMED_FRIEND`, the five `EXERCISE_*` values, `JOKER`, `FAKER`, and
  `NONE_SPECIFIED`. Callers with exhaustive enum handling must add these members.
  Entity encode now duplicates the envelope timestamp into
  `EntityMessage.recorded_at`; decode rejects a present embedded timestamp whose
  seconds or nanoseconds differ from the envelope. Decode tolerates the extended
  location geometry/motion fields, converts `CRS_ECEF` positions to geodetic
  coordinates, and leaves those extended fields unexposed on public models.

### Reliability and delivery

- Closing a client, or leaving its `async with` block, no longer raises
  `asyncio.CancelledError` when a task handler is registered. Transport close used to
  cancel the handler's in-flight unsubscribe cleanup, and that cancellation reached
  the caller even though the caller was never cancelled. Close already retires every
  local subscription on a non-persistent session, so the removal now completes
  quietly. Caller cancellation still propagates.

### Testing

- The versioned `ecn-wire-conformance` corpus (version 0.2) pins every fixture by
  SHA-256, pins canonical JSON encodings where applicable, and fails CI on
  unhashed corpus changes.

## [0.1.0] - 2026-08-12

### SDK and API

- An MQTT-v5-only typed client for publishing and watching entities and locations,
  sending and handling tasks, reading the ECN clock, running diagnostics and
  preflight checks, consuming bounded event streams, and encoding or decoding the
  supported protobuf wire forms.
- Entity and location models cover canonical UUID identities, detections, tracks,
  Position Location Information, observed location state, and explicit delivery
  policies.
- Task APIs support exact local request and response forms plus caller-supplied
  terminal-addressed routes. Typed `RequestTaskHandler` and `ContextTaskHandler`
  contracts and mode-aware `Tasks.send` overloads preserve request and result types
  through dispatch and registration.
- Cross-terminal task delivery, authentication, ACL behavior, and routing
  compatibility remain deployment-specific.
- `load_config`, `ECNConfig`, and typed authentication models support named profiles,
  environment configuration, bearer credentials, mTLS credentials, and explicit
  transport settings.
- Credential-free plaintext MQTT is available only through the mutually required
  `NoAuth` and `ReviewedContainerNetwork` models. This profile requires a reviewed
  private container network, rejects credential material, and revalidates private
  addresses on each connection attempt.
- `TransportBoundaryError` reports when a reviewed-container-network endpoint cannot
  remain inside its required private-address boundary.
- A local WGS-84-to-ECEF conversion surface provides `GeodeticPosition`,
  `ECEFPosition`, `ECEFVelocity`, `Location.to_ecef`, and
  `Location.to_ecef_velocity` without adding a runtime dependency or changing the
  wire formats.
- Python 3.14 support widens package metadata to `>=3.11,<3.15`, adds the 3.14
  classifier, locks `cp314` dependency wheels, and moves the operator container to
  the `python:3.14.0-slim-bookworm` base image. The complete installed suite and
  symmetric supported-minor CI gates now cover Python 3.14 alongside 3.11-3.13;
  the named delayed-publication shutdown regression remains a Python 3.13 gate.

### Reusable workflows

- The `picogrid_ecn_client.workflows` subpackage with 28 typed reusable workflow and result exports provides shared building blocks for observing data, publishing sensor output, handling effector tasks, and operator-oriented flows.
- Runnable examples use the same workflow surfaces and are inventoried in a
  schema-versioned `examples/manifest.json` with their inputs, safety class,
  supported modes, documentation, and notebook eligibility.

### Reliability and delivery

- Opening a watcher waits for broker subscription acceptance, while closing its last
  consumer releases the subscription. Startup creates no entity, location, or task
  subscription.
- Event streams, payloads, task work, and shutdown paths have explicit bounds.
  TRACK observations can use latest-value delivery coalesced by integration and
  entity UUID, while any matching FIFO watcher preserves ordered delivery.
- JSON payload nesting is now bounded at 64 container levels in both directions,
  matching the protobuf group-depth bound. Deeper values are rejected as unsupported
  on encode and malformed on decode on every supported Python version; previously,
  Python 3.11-3.13 rejected only near interpreter recursion limits and Python 3.14
  did not reject at those depths.
- Task request and response serialization uses canonical source UUIDs, exact target
  routing, source-aware duplicate handling, and explicit terminal result forms.
- Unsupported or malformed MQTT topics produce `ProtocolError` details with a fixed
  topic-family classification rather than exposing raw topic text.
- Preflight checks connection and authentication readiness without application
  publications; broker authentication and ACLs remain authoritative for access.

### Testing and mocks

- `MockECN` provides a compact offline MQTT surface for deterministic entity,
  location, task, authentication, and workflow development against loopback.
- Development and CI can explicitly opt into an external bind or unauthenticated
  connection through API settings or the `--allow-external-bind` and
  `--allow-unauthenticated` command-line flags.
- Every included example has a network-free `--check` mode, and installed-wheel
  fixtures exercise the distributed package rather than repository-only imports.
- Mock results validate SDK behavior but do not establish compatibility with a
  deployed ECN.

### Documentation

- A published documentation site provides installation, authentication, preflight,
  concepts, quickstarts, sensor and effector integration guides, operator workflows,
  security guidance, compatibility limits, and deployment support.
- A generated Python API reference covers supported exports, approved client and
  domain members, workflow helpers, and the offline testing surface.
- Reader-facing docstrings and model-field descriptions accompany the supported
  surface, with strict Pyright and mypy consumer checks for the installed wheel.
- Runnable examples and generated notebooks provide bounded paths for observing
  data, publishing sensor output, receiving tasks, and issuing approved tasking.

### Packaging and supply chain

- A separately installable operator wheel includes its compiled map frontend, lazy
  `picogrid-ecn operator` launcher, read-only demo mode, and profile-based live mode.
- The repository, wheel, and source distribution are licensed under the unmodified
  Mozilla Public License 2.0 (MPL-2.0), with required license and notice material
  included in release artifacts.
- Reproducible wheel and source-distribution builds use fixed inventories and are
  verified with dependency auditing, software bills of materials, checksums, and
  provenance records.
- The release candidate treats the operator wheel as an exact allowlisted,
  reproducible, checksummed, attested, and signed artifact while keeping it out of
  the client-only PyPI publication path pending separate approval.
- Package metadata includes project, documentation, source, support, security, and
  changelog URLs, and distribution metadata keeps README links usable.
- Release automation covers verification, documentation, security checks,
  dependency updates, artifact assembly, and draft release preparation.
- Release automation includes the exact-wheel operator container.
