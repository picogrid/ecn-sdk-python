---
title: Lifecycle, reconnect, and streams
tableOfContents: false
sidebar:
  order: 6
---

Use `async with ECNClient(config)` whenever possible. Entry waits for MQTT v5
CONNACK; exit closes streams, task handlers, correlation subscriptions, and the
transport within `shutdown_timeout`.

## Streams and cleanup

Watchers are lazy and reference-counted. `EventStream.aclose()` releases its filter;
the MQTT unsubscribe occurs when the final consumer of that filter closes. A
negative UNSUBACK is surfaced as a typed error rather than reported as successful
broker cleanup. The transport then invalidates the clean-session connection and
reconnects only filters that still have local consumers. Streams use bounded buffers
with FIFO or latest-value delivery. Overflow does not create an unbounded queue.
Each stream exposes `dropped_count` for local buffer loss and `decode_error_count`
for matching inbound payloads rejected by that watcher's decoder. Both counters are
local diagnostics, not broker-wide metrics. Raw TRACK observations replaced or
evicted by all-`LATEST` ingress coalescing are not decoded-event buffer drops and do
not increment `dropped_count`; their pending identity count and aggregate raw bytes
are independently bounded.

## Reconnection and cancellation

The transport reconnects with the configured full-jitter policy and reinstalls only
subscriptions that still have local owners. Consumers should tolerate duplicates and
gaps, preserve idempotency, and observe `client.status.state` instead of assuming
uninterrupted delivery.

After prior readiness, calling `start()` again only waits for readiness. A timeout
does not replace or close the reconnect supervisor and does not release active
watchers; use `wait_until_ready()` when the caller wants its distinct timeout error.

:::caution[Reliability amendment implemented; artifact verification pending]
The local candidate implements the typed full-jitter reconnect policy, connection
event/wait/retry surface, credential-notification recovery from post-readiness
credential-terminal state, explicit session settings, per-attempt credential reload,
isolated restore denial, and delivery-phase/outcome-unknown behavior. Exact
installed-wheel and hosted verification remain pending. DNS and bounded path-backed
TLS-material reads use lifecycle-owned disposable processes, and their cancellation
cleanup requests kill before a bounded drain. A runtime cleanup primitive that
resists cancellation can outlive that bound, so the zero-background-task shutdown
guarantee remains open. TCP and TLS handshake work is non-blocking, and temporary PEM
files are removed before the TLS builder returns. Standard-library/OpenSSL context
parsing remains synchronous, so a close requested during that native call is observed
only after it returns; immediate hard-deadline shutdown remains incomplete. The
amendment has no staging or production evidence; historical reconnect behavior and
staging results do not satisfy it. The session contract is MQTT v5 Clean Start
enabled, Session Expiry Interval zero, one stable client ID for each `ECNClient`
object's lifetime, and restoration of only subscriptions that still have local
owners.

An interrupted QoS 0 publication is never safe to retry automatically. An in-flight
QoS 1 task request with no PUBACK is outcome-unknown after disconnect or PUBACK-wait
expiry; after PUBACK, response loss, expiry, malformed content, or result-model
validation failure remains a separate response-pending unknown outcome. The SDK
never automatically retries an interrupted QoS 0 send, a QoS 1 mutation without
PUBACK, or a response-pending task or future local FMV mutation.
:::

Cancellation is propagated. Task handlers are async-only and must cooperate with
cancellation. Close streams in `finally`, unregister exact task handlers, and close
the client even after startup or handler failure. See
[cleanup](../how-to/cleanup.md), [troubleshooting](../how-to/troubleshooting.md), and
the [deployment checklist](../deployment-support.md).
