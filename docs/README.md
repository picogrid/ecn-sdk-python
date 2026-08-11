---
title: Picogrid ECN SDK guide
sidebar:
  hidden: true
---

This page is the complete documentation map. Most readers should begin at the
[SDK overview](index.md) and follow a role-based quickstart.

## Navigate

- Get started: [installation](getting-started/installation.md),
  [configuration](getting-started/configuration.md),
  [authentication](getting-started/authentication.md),
  [mock setup](getting-started/mock-setup.md), and
  [preflight](getting-started/preflight.md)
- Quickstarts: [observe data](quickstarts/observe-data.md),
  [sensor publisher](quickstarts/sensor-publisher.md),
  [effector handler](quickstarts/effector-handler.md), and
  [operator view](quickstarts/operator-view.md)
- Core concepts: [ECNs](concepts/ecns.md), [entities](concepts/entities.md),
  [locations and Position Location Information (PLI)](concepts/locations.md),
  [tasks](concepts/tasks.md), [mesh routing](concepts/mesh-routing.md),
  [ACLs](concepts/acls.md), [MQTT topics and delivery](concepts/mqtt-wire.md),
  [UUIDs](concepts/uuids.md), [lifecycle](concepts/lifecycle.md), and the
  [security model](concepts/security.md)
- Integrate: [sensors](integrations/sensors.md) and
  [effectors](integrations/effectors.md)
- Operate: [operator workflows](operator/workflows.md) and the
  [operator application](operator/application.md)
- Task guides: [check ECN-relative time](how-to/check-clock.md),
  [observe location](how-to/observe-location.md),
  [watch tracks and detections](how-to/tracks-detections.md),
  [publish entities, locations, and PLI](how-to/pli-entities.md),
  [receive a task](how-to/receive-local-tasks.md),
  [dispatch a task](how-to/dispatch-local-tasks.md),
  [observe mesh-routed data](how-to/observe-mesh-data.md),
  [dispatch a terminal-addressed task](how-to/dispatch-mesh-tasks.md),
  [decode protobuf](how-to/protobuf-decode.md),
  [troubleshoot](how-to/troubleshooting.md), and
  [clean up](how-to/cleanup.md)
- Walkthroughs: [track viewer](walkthroughs/track-viewer.md),
  [effector service](walkthroughs/task-handler-service.md), and
  [tactical live map](walkthroughs/tactical-live-map.md)
- Reference: [API](reference/api.md), [configuration](reference/configuration.md),
  [exceptions](reference/exceptions.md), and [wire formats](reference/wire-formats.md)
- Plan for production: [security and credentials](security/credentials.md),
  [compatibility and limitations](compatibility/limitations.md),
  [SDK tools](shipped-tooling.md), [deployment and support](deployment-support.md),
  and [changelog](changelog.md)

## Runnable how-tos

Every included example imports the installed `picogrid-ecn-client` wheel. Run
`--check` first for network-free model and API validation.

| Outcome | Runnable example | Network behavior |
|---|---|---|
| Read-only preflight | [`preflight.py`](../examples/preflight.py) | Zero application publishes |
| Check ECN-relative time | [`check_clock.py`](../examples/check_clock.py) | Valid NTPv4 client requests only; no MQTT start or publish |
| Observe an ECN location | [`get_ecn_location.py`](../examples/get_ecn_location.py) | Waits for the next MQTT update |
| Watch tracks | [`watch_tracks.py`](../examples/watch_tracks.py) | Bounded category watcher |
| Watch detections | [`watch_detections.py`](../examples/watch_detections.py) | Bounded category watcher |
| Publish an entity | [`publish_entity.py`](../examples/publish_entity.py) | One caller-authorized publication |
| Publish a location | [`publish_location.py`](../examples/publish_location.py) | One caller-authorized publication |
| Publish a location, including Position Location Information (PLI) | [`publish_location.py`](../examples/publish_location.py) | One caller-authorized location publication |
| Receive a local task | [`receive_task.py`](../examples/receive_task.py) | One exact task subscription |
| Dispatch a local task | [`dispatch_task.py`](../examples/dispatch_task.py) | One exact request/response exchange |
| Observe mesh-routed data | [`observe_mesh_data.py`](../examples/observe_mesh_data.py) | Bounded allowlisted watchers; deployment-owned routing |
| Receive a terminal-sourced task | [`receive_mesh_task.py`](../examples/receive_mesh_task.py) | One exact request subscription |
| Dispatch a terminal-addressed task | [`dispatch_mesh_task.py`](../examples/dispatch_mesh_task.py) | One exact routed request/response exchange |
| Decode protobuf | [`decode_public_protobuf.py`](../examples/decode_public_protobuf.py) | Local decode only |
| Convert a location to ECEF | [`convert_location_to_ecef.py`](../examples/convert_location_to_ecef.py) | Local conversion only |

## Compatibility labels

The [compatibility page](compatibility/limitations.md) is the public source of truth
for supported and deferred behavior. Maintainer evidence tiers and complete
historical workflow accounting remain in the non-public
[original ECN-integration parity matrix](reference/original-ecn-integration-parity.md).
