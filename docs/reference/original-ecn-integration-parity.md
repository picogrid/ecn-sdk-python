---
title: Original ECN integration parity matrix
sidebar:
  hidden: true
---

This matrix accounts for every logical page, example, script, top-level reference item,
and supporting source item in the pinned original integration publication. It is an
independently written migration index, not a copy of that publication or of the private
reference SDK.

The public client intentionally covers only the retained MQTT v5 entity, location, and
exact local or terminal-addressed task wire. `staging verified` applies only to the
operation stated in that row. A `staging pending` row may name narrower prerequisites
that completed without treating them as proof of the whole workflow.
`offline-only` means staging is unnecessary for that disposition. The verification
column describes the evidence boundary, not the status of the final release gate.

In this matrix, PLI means Position Location Information. The public `Location` model
uses the retained location wire; there is no separate PLI model, method, topic, or
category.

## Landing page and concepts

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| ROOT-00 | Guide landing page | Public README and this parity index | changed semantics | offline-only | Scope is reduced to the public MQTT v5 thin client. |
| CON-10 | Integration overview | Public client overview | changed semantics | offline-only | Omits platform administration, external platform APIs, and private SDK behavior. |
| CON-11 | Broker connection model | MQTT configuration, TLS, authentication, and broker ACL behavior | changed semantics | staging verified | Target-specific mTLS completed verified TLS and MQTT v5 CONNACK; the client neither inspects nor administers private routing or infrastructure configuration. |
| CON-12 | Topic-shape model | Retained plain entity/location forms plus exact local and terminal-addressed task request/response forms | changed semantics | staging pending | Entity/location and same-ECN local task forms passed, but terminal-addressed task routing remains unverified. Heartbeat, administrative, arbitrary peer-prefixed, and broad-wildcard families are outside the public contract. |
| CON-13 | JSON and protobuf wire formats | Typed JSON/PB watchers and the public protobuf decoder | supported | staging verified | JSON and protobuf TRACK/location round trips passed. Public schemas use an independent namespace and never import private generated modules. |
| CON-14 | Entity model | Typed UUID entity, metadata, and location models | changed semantics | staging pending | One new canonical UUID completed JSON and protobuf TRACK round trips with embedded-location caching, but other categories were not published. Publication remains an event, not an authoritative create, discovery, or fingerprint query. |
| CON-15 | Authentication model | Caller-supplied mTLS or bearer authentication configuration | changed semantics | staging pending | Target-specific mTLS completed MQTT v5 authentication, but bearer authentication was not tested. The package does not issue credentials, register identities, or infer granted scopes. |
| CON-16 | Private routing and infrastructure model | Caller-supplied target-terminal UUID for exact task addressing; no route discovery or administration | changed semantics | staging pending | Entity/location forwarding and actual task routing remain external infrastructure responsibilities. |

## Getting started

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| GET-20 | Prerequisites | Install the wheel and construct validated MQTT configuration | changed semantics | offline-only | No private SDK, cache service, platform API, or on-node package is required. |
| GET-21 | mTLS bootstrap | Configure caller-provided CA, client certificate, and key paths | changed semantics | staging verified | Target-specific mTLS completed verified TLS and MQTT v5 CONNACK. Certificate issuance and server-certificate changes are not client operations. |
| GET-22 | Bearer bootstrap | Configure a caller-owned token provider | changed semantics | offline-only | Identity provisioning, installers, token-file discovery, and platform registration are excluded; deployed broker mapping requires separate external confirmation. |
| GET-23 | Connectivity preflight | [`preflight.py`](../../examples/preflight.py) | changed semantics | staging verified | Configuration, DNS, TCP, verified TLS, authentication-material, and MQTT v5 CONNACK checks passed. It performed no subscription probe, application publish, or HTTP request. |

## How-to pages and example assets

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| HOW-30 | Observe terminal-geolocation state | Client-observed location state and [`get_ecn_location.py`](../../examples/get_ecn_location.py) | changed semantics | staging pending | Uses fixed-depth location filters with a single-level UUID segment and returns the observed canonical UUID; it is not an authoritative server query or general discovery operation. |
| EX-H30 | Terminal-geolocation Python example | [`get_ecn_location.py`](../../examples/get_ecn_location.py) | changed semantics | staging pending | Waits on the pinned integration's narrow dedicated location filters without requiring a pre-known UUID. |
| HOW-31 | Observe live tracks | Category-filtered entity watcher | supported | staging verified | A read-only fixed-depth retry received 100 TRACK events representing 72 canonical UUID identities, all with embedded locations, plus 74 dedicated locations, with zero decode errors, drops, publishes, or cleanup leaks. Operator timing corroborated target-side mesh RX; the prefix-stripped event did not authenticate its peer origin. |
| EX-H31 | Track watcher Python example | [`watch_tracks.py`](../../examples/watch_tracks.py) | supported | staging pending | The same installed-wheel watcher path received live TRACK events, but the standalone example command was not separately run against staging. |
| HOW-32 | Observe detections | Category-filtered entity watcher | supported | staging pending | The narrow detection subscription was accepted and closed cleanly, but no detection event was received; event observation remains pending. |
| EX-H32 | Detection watcher Python example | [`watch_detections.py`](../../examples/watch_detections.py) | supported | staging pending | The installed-wheel watcher established the bounded subscription, but no detection payload was available to validate live decoding. |
| HOW-33 | Publish PLI | Typed [`publish_location.py`](../../examples/publish_location.py) publication | changed semantics | staging verified | One synthetic JSON PLI completed a QoS 1 round trip on the location wire; the public API uses its single location model and does not invent a separate PLI surface. |
| EX-H33 | PLI Python example | [`publish_location.py`](../../examples/publish_location.py) | changed semantics | staging pending | The ordinary installed location path is the public PLI path; no duplicate example is shipped. |
| HOW-34 | Create or discover an entity | Typed entity-event publication | changed semantics | staging pending | JSON and protobuf TRACK events for one new canonical UUID completed round trips, but other categories were not published. The client does not promise authoritative creation, discovery, upsert, or fingerprint idempotence. |
| EX-H34 | Entity creation Python example | [`publish_entity.py`](../../examples/publish_entity.py) | changed semantics | staging pending | The underlying installed-wheel entity publication path passed for TRACK, but the example's category-generic contract was not exhaustively exercised. |
| HOW-35 | Receive a task | Exact-topic local and terminal-sourced task registration | changed semantics | staging pending | One exact same-ECN task with literal `local` completed and the registration closed; terminal-sourced routing, advertisement, and discovery remain unverified. |
| EX-H35 | Task handler Python examples | [`receive_task.py`](../../examples/receive_task.py) and [`receive_mesh_task.py`](../../examples/receive_mesh_task.py) | changed semantics | staging pending | The local exact-topic handler path completed; the routed example remains staging-unverified. |
| HOW-36 | Dispatch a task | Exact local or terminal-addressed MQTT request/response exchange | changed semantics | staging pending | One exact same-ECN QoS 1 request/result exchange passed with source `local`. Terminal-addressed routing remains external and unverified. |
| EX-H36 | Task dispatch Python examples | [`dispatch_task.py`](../../examples/dispatch_task.py) and [`dispatch_mesh_task.py`](../../examples/dispatch_mesh_task.py) | changed semantics | staging pending | The local bounded response path completed and released its resources; the routed example remains staging-unverified. |
| HOW-37 | Render observed events | Separately installed public [`operator-app`](../../operator-app/README.md) over typed watchers | changed semantics | offline-only | The independently authored map uses narrow public watchers and remains outside the SDK runtime wheel. |
| EX-H37-SERVER | Renderer server source | Local server packaged in the operator wheel | changed semantics | offline-only | One installed entry point serves only the local browser application and uses the installed client for MQTT traffic. |
| EX-H37-UI | Renderer static UI | Compiled Leaflet map packaged in the operator wheel | changed semantics | offline-only | The UI is independently authored, defaults to read-only, contains no proprietary assets, and requires no runtime Node.js installation. |
| HOW-38 | Decode protobuf payloads | Public decoder and independently generated public schema | supported | staging verified | Protobuf TRACK and location round trips passed with the public decoder; it never imports a private SDK namespace. |
| EX-H38 | Protobuf decoder Python example | [`decode_public_protobuf.py`](../../examples/decode_public_protobuf.py) | supported | offline-only | The retained example decodes a caller-provided file and includes a synthetic offline self-check. |
| HOW-39 | Search server-wide entities | No SDK equivalent | explicitly deferred | offline-only | The original workflow has no confirmed MQTT wire equivalent and is not included in the public SDK. |
| EX-H39 | Remote-search Python example | No SDK equivalent | explicitly deferred | offline-only | No replacement example is shipped because the corresponding operation is not part of the public SDK. |

## Walkthrough pages and assets

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| WALK-40 | Track-viewer walkthrough | Public track watcher and runnable [`operator-app`](../../operator-app/README.md) | changed semantics | offline-only | The installed operator artifact is exercised with synthetic data; live operator-map behavior is not staging or production verified. |
| EX-W40-MAIN | Track-viewer main program | [`watch_tracks.py`](../../examples/watch_tracks.py) and `picogrid-ecn operator` | changed semantics | offline-only | The installed operator entry point is exercised offline; historical live watcher evidence does not validate the complete operator application. |
| WALK-41 | Task-handler walkthrough | [`receive_task.py`](../../examples/receive_task.py) and [`receive_mesh_task.py`](../../examples/receive_mesh_task.py) | changed semantics | staging pending | The literal-local exact-topic path passed with deterministic cleanup; source-terminal routing remains unverified and deployment infrastructure owns forwarding. |
| EX-W41-MAIN | Task-handler main program | [`receive_task.py`](../../examples/receive_task.py) and [`receive_mesh_task.py`](../../examples/receive_mesh_task.py) | changed semantics | staging pending | The typed local request/result/context path passed; the routed example remains unverified. |
| EX-W41-CONTAINER | Task-handler container recipe | N/A | explicitly deferred | offline-only | Deployment packaging is consumer-owned and not included in the wheel. |
| EX-W41-COMPOSE | Task-handler composition recipe | N/A | explicitly deferred | offline-only | Service orchestration is outside the client contract. |
| WALK-42 | Tactical-view walkthrough | Runnable public operator map over typed watchers | changed semantics | offline-only | The public application omits broad capture, routing diagnostics, proprietary UI assets, and infrastructure control. |
| EX-W42-SERVER | Tactical-view server source | Local server packaged in the operator wheel | changed semantics | offline-only | The server remains an external application boundary outside the SDK wheel and is launched by the installed entry point. |
| EX-W42-UI | Tactical-view static UI | Compiled Leaflet map and diagnostics panel packaged in the operator wheel | changed semantics | offline-only | The UI is independently authored, consumes only sanitized public event snapshots, and has no runtime Node.js dependency. |
| EX-W42-CONTAINER | Tactical-view container recipe | Public operator-app Dockerfile | changed semantics | offline-only | The image accepts the exact public client and operator wheels at build time and contains no credentials or endpoint. |
| EX-W42-COMPOSE | Tactical-view composition recipe | Public operator-app Compose recipe | changed semantics | offline-only | Configuration uses placeholders, loopback publication, read-only defaults, and caller-mounted credentials. |

## Reference-tier pages

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| DOC-50 | Shipped-script catalog | Installed public examples and separate operator application wheel | changed semantics | offline-only | Client and operator wheel contents are distinguished explicitly and installed together for the operator workflow. |
| DOC-60 | Combined protocol reference | Public API/protocol references and this migration index | changed semantics | staging pending | Entity/location and literal-local task shapes passed, but terminal-addressed routing remains source-pinned and staging-unverified. Non-public transport details, private methods, heartbeat, route administration, and administrative namespaces remain omitted. |
| DOC-70 | Troubleshooting guide | Secret-safe MQTT/TLS diagnostics | changed semantics | offline-only | Errors and examples do not print secret values or credential-bearing paths. |

## Script inventory

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| SCR-00 | Root script package marker | N/A | explicitly deferred | offline-only | Contains no behavior and is not needed by the wheel. |
| SCR-01 | Static-site build script | Reproducible Astro documentation build outside the runtime | changed semantics | offline-only | Site bytes are verified separately and never become a client runtime dependency. |
| SCR-02 | On-node credential issuer | N/A | explicitly deferred | offline-only | Credential issuance and node mutation are prohibited client responsibilities. |
| SCR-03 | Remote-script package marker | N/A | explicitly deferred | offline-only | Contains no behavior and is not needed by the wheel. |
| SCR-04 | Bounded traffic capture and render tool | Typed watcher APIs feeding consumer-owned capture/rendering | changed semantics | offline-only | Narrow watcher consumption is supported; capture retention and rendering remain downstream, and no broad observation tool is shipped. |
| SCR-05 | Remote helper-library package marker | N/A | explicitly deferred | offline-only | Contains no behavior and is not needed by the wheel. |
| SCR-06 | Raw location and routing helper | Public location watcher | changed semantics | staging verified | Controlled JSON/protobuf locations and 74 live dedicated locations plus embedded TRACK locations were observed through bounded public watchers; private access, broad filters, peer attribution, and outbound-routing diagnostics are removed. |
| SCR-07 | Map-style helper | Independently authored public operator-map styling | changed semantics | offline-only | Styling is compiled into the operator wheel and is not copied proprietary presentation material. |
| SCR-08 | Inline web UI asset | Independently authored public Leaflet UI | changed semantics | offline-only | Compiled browser assets are packaged in the operator wheel; browser dependencies remain outside the MQTT client runtime package. |
| SCR-09 | Live-map server | Installed local operator application server | changed semantics | offline-only | The server consumes narrow watchers, exposes only its local operator APIs, and launches with `picogrid-ecn operator`. |
| SCR-10 | Multi-system connectivity preflight | [`preflight.py`](../../examples/preflight.py) | changed semantics | staging verified | The zero-publish installed-wheel preflight passed through MQTT v5 CONNACK without subscription probes; cache, external platform, HTTP, and token-file checks are removed. |
| SCR-11 | Remote integration bootstrap script | N/A | explicitly deferred | offline-only | Provisioning, installation, and registration mutation are outside scope. |

## Top-level reference inventory

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| REF-01 | MQTT subscriber reference document | Retained MQTT topic and wire reference | changed semantics | staging pending | Bounded entity/location forms and the exact literal-local task exchange passed, but terminal-addressed task routing remains unverified. |
| REF-02 | Terminal provisioning reference document | N/A | explicitly deferred | offline-only | Operator provisioning and host changes are outside scope. |
| REF-03 | SDK integration reference document | Source-grounded public compatibility notes | changed semantics | offline-only | Used only as read-only evidence; no private API is imported or copied. |
| REF-04 | Private reference SDK wheel | N/A | explicitly deferred | offline-only | Excluded from source, dependencies, tests, build inputs, and release artifacts. |
| REF-05 | Private wheel signature bundle | Release process provenance concepts | explicitly deferred | offline-only | The private bundle itself is not distributed with the public client. |
| REF-06 | Extracted private SDK source snapshot tree | Independently implemented public models, codecs, and tests | changed semantics | offline-only | This aggregate row covers every descendant in the private docs/examples/schemas/source/tests/generated-client corpus; none is copied, imported, or packaged. |

## Scaffolding and presentation assets

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| SCAF-01 | Site-publishing workflow | Verify-once Pages workflow for exact reviewed site bytes | changed semantics | offline-only | The permission-bounded workflow has deployed a private Pages site from an exact verified artifact; this is documentation delivery, not package publication or deployed ECN evidence. |
| SCAF-02A | Repository ignore configuration | Public repository ignore policy | changed semantics | offline-only | Public policy is tailored to this repository and secret-safe release outputs. |
| SCAF-03A | Container environment example | Placeholder-only public operator environment example | changed semantics | offline-only | It contains no credential value or operational endpoint and defaults to read-only mock operation. |
| SCAF-03B | Container image recipe | Public operator-app Dockerfile | changed semantics | offline-only | The independently authored recipe installs the exact supplied client wheel and includes no secret. |
| SCAF-03C | Container composition recipe | Public operator-app Compose recipe | changed semantics | offline-only | It binds the browser service to loopback and mounts caller-owned credentials only when explicitly configured. |
| SCAF-04A | Documentation wordmark image | N/A | explicitly deferred | offline-only | Original branding asset is not required for protocol parity. |
| SCAF-04B | Documentation dark-theme image | N/A | explicitly deferred | offline-only | Original presentation asset is not copied. |
| SCAF-04C | Documentation light-theme image | N/A | explicitly deferred | offline-only | Original presentation asset is not copied. |
| SCAF-04D | Documentation stylesheet | Public Starlight theme with authorized static Picogrid-derived tokens | changed semantics | offline-only | Only minimal light/dark color and radius tokens are adapted from the exact source recorded in `NOTICE.md`; no private UI component or npm package is used. |
| SCAF-04E | Documentation site template | Independently authored Astro/Starlight site | changed semantics | offline-only | The guide is built from committed public Markdown and exact locked dependencies. |
| SCAF-04F | Generated-site wordmark image | N/A | explicitly deferred | offline-only | Generated presentation asset is not copied. |
| SCAF-04G | Generated-site dark-theme image | N/A | explicitly deferred | offline-only | Generated presentation asset is not copied. |
| SCAF-04H | Generated-site light-theme image | N/A | explicitly deferred | offline-only | Generated presentation asset is not copied. |
| SCAF-04I | Generated-site stylesheet | Reproducibly generated public site CSS | changed semantics | offline-only | Output includes the authorized provenance-bound static tokens and independently authored Starlight overrides. |
| SCAF-04J | Generated site page | Reproducibly generated navigable public guide | changed semantics | offline-only | Documentation is maintained as source and does not embed the original generated site. |
| SCAF-05 | Original guide package metadata | Public package metadata | changed semantics | offline-only | Runtime dependencies and package contents are independently defined and release-audited. |

## Original test-source inventory

| Row key | Original workflow or item | Public equivalent | Disposition | Verification | Safety or migration difference |
|---|---|---|---|---|---|
| TEST-00 | Root test package marker | N/A | explicitly deferred | offline-only | Contains no behavior. |
| TEST-01 | Integration-test package marker | N/A | explicitly deferred | offline-only | Contains no behavior. |
| TEST-02 | Live integration test module | Public offline contract/E2E suites; bounded staging plan | changed semantics | staging pending | A bounded installed-wheel session passed synthetic entity/location/PLI and literal-local task workflows, but cross-terminal, terminal-geolocation, bearer, detection-event, and operator workflows remain pending; no raw output or operational details are retained. |
| TEST-03 | Unit-test package marker | N/A | explicitly deferred | offline-only | Contains no behavior. |
| TEST-04 | Location-helper unit tests | Public location codec, watcher, observation-cache, and installed-wheel tests | changed semantics | offline-only | Tests target the public API and do not import private helpers. |
| TEST-05 | Preflight unit tests | Public MQTT-only preflight and zero-publish tests | changed semantics | offline-only | HTTP, cache, and platform checks are absent. |
| TEST-06 | Map-style unit tests | Operator backend, frontend, and headless-browser map tests | changed semantics | offline-only | Tests exercise independently authored public behavior from installed artifacts without private fixtures. |

## Evidence boundary

Offline mock and installed-wheel evidence demonstrates the public contract, not
deployed ECN behavior. The bounded staging session separately verified target-specific
mTLS MQTT v5 preflight; JSON and protobuf TRACK/location paths; JSON Position Location
Information (PLI); literal-local task exchange; and clean resource shutdown. A
read-only retry immediately after an operator-controlled peer restart separately
observed 100 TRACK events representing 72 canonical UUID identities, all with
embedded locations, plus 74 dedicated locations. That timing is
operator-corroborated target-side mesh RX evidence, but the prefix-stripped public
wire did not authenticate peer origin or expose inbound wire format or delivered QoS.
Cross-terminal tasks, peer-specific attribution, terminal geolocation, bearer
authentication, live operator use, and production remain unverified. Every
staging label is operation-specific and must never include endpoint, credential,
host-access, raw capture, or operational-entity details.
