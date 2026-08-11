---
title: Troubleshooting
tableOfContents: false
sidebar:
  hidden: true
---

## Start with preflight

Start with the runnable [preflight example](../../examples/preflight.py):

```bash
python examples/preflight.py --check
python examples/preflight.py
```

## Configuration is rejected

Configuration: validate host-only syntax, port, integration name, TLS, and one
authentication method.

## The broker cannot be reached or TLS fails

DNS/TCP/TLS: fix reachability or trust without disabling verification.

## MQTT authentication or connection fails

MQTT: inspect the typed authentication or connection error; do not print secret
material.

## The subscription probe is rejected

Subscription probe: confirm the exact filter was authorized. Do not broaden it.

## A watcher receives no updates

Watch timeout: remember that observed-state APIs wait for new MQTT traffic and do not
query history.

## TLS passes but MQTT reports `Not authorized`

If verified TLS passes but MQTT returns `Not authorized`, first confirm that the
configured port is the listener issued for the selected authentication profile.
An mTLS connection sends the client certificate and no MQTT username/password; a
bearer connection sends the deployment-issued MQTT username and token without a
client certificate. A server-auth TLS handshake can succeed on the wrong listener
before CONNACK rejects the mismatched profile. Do not add a fallback credential or
disable TLS verification.

## The client is reconnecting

`RECONNECTING` indicates the transport is restoring active subscriptions. Consumers
must tolerate duplicates and gaps.

## A resource limit is reached

`ResourceLimitError` means a configured payload, buffer, or task bound was reached;
change a limit only after bounding the workload. Latest-value TRACK ingress is bounded
separately and never raises at its bound: the client evicts the least recently updated
pending raw observation and records the eviction, so a sustained burst reduces delivered
updates instead of failing the watcher.

## Data is dropped or rejected

Inspect each active stream's `dropped_count` and `decode_error_count` when a consumer
falls behind or wire data is rejected. The counters apply only to that local watcher
and reset with a new stream. `dropped_count` measures decoded events rejected or
replaced in that stream's buffer; it excludes raw TRACK observations coalesced before
decode when all matching watchers request latest-value delivery.

## Requesting help

See [Deployment and support](../deployment-support.md) for the sanitized information
to include when requesting help.
