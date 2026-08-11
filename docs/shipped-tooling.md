---
title: SDK tools and examples
description: Command-line examples, local mock tooling, and the operator application.
---

The `picogrid-ecn-client` distribution installs two command-line tools:
`picogrid-ecn` for profile configuration and read-only diagnostics, and
`picogrid-mock-ecn` for offline development. The matching source bundle contains the
runnable examples and this guide. The browser operator application is distributed as
the separate `picogrid-ecn-operator-app` wheel so its local-server dependencies do not
enter the thin client runtime.

Position Location Information (PLI) uses the normal location publication wire; the
location example covers that workflow without a duplicate PLI model or method.

## Runnable Python examples

| Workflow | Example |
|---|---|
| Read-only MQTT preflight | [`preflight.py`](../examples/preflight.py) |
| Check ECN-relative time | [`check_clock.py`](../examples/check_clock.py) |
| Observe terminal geolocation | [`get_ecn_location.py`](../examples/get_ecn_location.py) |
| Watch tracks | [`watch_tracks.py`](../examples/watch_tracks.py) |
| Watch detections | [`watch_detections.py`](../examples/watch_detections.py) |
| Publish an entity | [`publish_entity.py`](../examples/publish_entity.py) |
| Publish a location, including PLI | [`publish_location.py`](../examples/publish_location.py) |
| Receive a local task | [`receive_task.py`](../examples/receive_task.py) |
| Dispatch a local task | [`dispatch_task.py`](../examples/dispatch_task.py) |
| Observe mesh-routed data from allowed integrations | [`observe_mesh_data.py`](../examples/observe_mesh_data.py) |
| Receive a terminal-sourced task | [`receive_mesh_task.py`](../examples/receive_mesh_task.py) |
| Dispatch a terminal-addressed task | [`dispatch_mesh_task.py`](../examples/dispatch_mesh_task.py) |
| Decode public protobuf | [`decode_public_protobuf.py`](../examples/decode_public_protobuf.py) |
| Convert a location to ECEF | [`convert_location_to_ecef.py`](../examples/convert_location_to_ecef.py) |

Each included script supports `--check` and imports the installed wheel. Use that
mode before adding credentials or contacting a broker. Live examples accept
`--profile NAME` or `ECN_PROFILE`.

### Example manifest contract

`examples/manifest.json` is the authoritative, committed inventory of runnable
examples. It uses a top-level `schema_version` of `1` and an ordered `examples`
array. Each entry records:

- `id`, `source_path`, `title`, and `summary`;
- a `workflow` object containing the shipped
  `picogrid_ecn_client.workflows.*` module and exported function;
- `required_inputs`, whose entries declare `name`, input `kind`, value `type`,
  whether the input is `required`, any optional `default`, and a `description`;
- `safety_class` (`local`, `read`, `task-receive`, or `write`) and the exact
  supported `modes` (`offline-check`, `mock`, and/or `live`);
- related `documentation`; and
- `notebook_eligible` plus an `exclusion_reason` when notebook generation is
  deliberately disabled.

The `mock` mode is claimed per example, not inferred merely because the SDK ships a
mock broker. Release verification fails closed if the manifest is malformed or
non-deterministically formatted, if an example or documentation path is missing,
extra, unsafe, duplicated, or stale, if a declared workflow is not a callable public
export, or if `notebook_eligible` is not Boolean or is not paired with a non-empty
`exclusion_reason` exactly when false. The committed manifest is the contract
consumed by downstream notebook generation; the SDK release does not generate or
inventory notebooks.

Each example remains a thin input/output wrapper over its declared public workflow.
Consequently, a generated notebook and a direct CLI run exercise the same reviewed
implementation rather than parallel copies of connection, observation,
publication, task, or local-conversion logic.

The installed CLI also provides
`picogrid-ecn clock check --profile NAME --max-offset SECONDS`. It sends only valid
NTPv4 client requests to the configured ECN clock endpoint and does not start MQTT.

## Operator application

The complete application documented under [`operator-app/`](../operator-app/README.md)
consumes entity and location watchers, correlates canonical UUIDs, renders an offline
Leaflet map, and optionally dispatches explicitly allowlisted tasks after operator
confirmation. Install the client and operator wheels from the same inspected release:

<!-- x-release-please-start-version -->
```bash
python -m pip install \
  ./picogrid_ecn_client-0.1.0-py3-none-any.whl \
  ./picogrid_ecn_operator_app-0.1.0-py3-none-any.whl
picogrid-ecn operator --demo
```
<!-- x-release-please-end -->

The operator wheel contains the compiled frontend. Runtime users do not need a
repository copy, Node.js, npm, or a separate frontend process. Its loopback HTTP
server remains outside the SDK runtime package and adds no HTTP dependency to the
client wheel. This is a trusted, single-user, local-host surface that binds to
loopback by default. The approved container recipe binds within the container and
publishes only to host loopback. Host, Origin, and application-policy checks are not
authentication; do not expose or proxy the surface to untrusted or multiple users.

Launch live mode with either complete command:

```bash
picogrid-ecn operator --profile NAME
ECN_PROFILE=NAME picogrid-ecn-operator
```

The operator wheel also installs `picogrid-ecn-operator` as a direct alias.
The profile supplies only the ECN connection; the application's explicit observation
and task allowlists remain required operator settings. Live tasking also remains
disabled until its separate policy flag and allowlists are present, and every task
requires operator confirmation.

The target deployment's authentication and broker ACLs authorize each operation
independently. Successful reads or subscriptions do not grant publication permission.

Before any live launch, obtain written authorization that names the exact ECN target
and each permitted operation. Observation authorization does not authorize task
publication; live tasking additionally requires written authorization for the exact
target UUID and command.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact. Operator
behavior is verified offline; this page makes no staging or production validation
claim.

See [Operator application](operator/application.md) for product workflows. The mock
and synthetic map data verify local behavior only; review
[Compatibility and limitations](compatibility/limitations.md) before live use.
