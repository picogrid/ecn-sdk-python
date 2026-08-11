---
title: Tasks
tableOfContents: false
sidebar:
  order: 5
---

Tasks use one exact request topic and its `/response` suffix. Registration is only
an MQTT subscription; it does not advertise commands or mutate platform
configuration.

## Local and cross-terminal tasking

Use the ordinary same-ECN task path unless the deployment has supplied canonical
terminal identities and an authorized cross-terminal route.

Without `ECNConfig.terminal_id`, the SDK emits the established literal
`source="local"` compatibility profile and ignores UUID-sourced requests because it
cannot construct a safe return route. With a configured canonical terminal UUID,
normal requests serialize that UUID as their source. A handler accepts either
literal local or a canonical UUID source and exposes the latter through
`TaskRequestContext.source`.

Passing a `target_terminal_id` different from the configured source terminal
dispatches one exact terminal-addressed request. The response subscription remains
the ordinary unprefixed `/response` topic because ECN infrastructure performs the
prefix routing. Explicit self-targeting is rejected; omit `target_terminal_id` to
select the unprefixed same-ECN request. Route
availability, fan-out policy, and authorization are deployment properties, not SDK
discovery results.

Legacy hostname-valued task sources are not part of this public identity contract.
The SDK accepts only literal `local` or canonical terminal UUID sources. Remote
callers must pass an exact `target_terminal_id`; an unprefixed configured-terminal
dispatch accepts only the same-terminal response source and does not reinterpret a
remote fan-out result as local.

## Task modes

- `COMPLETE`: run the handler and return `SUCCESS`, `PENDING`, or `FAILED`.
- `ACKNOWLEDGMENT`: emit exactly one `Task started` acknowledgement, then run the
  handler without a completion response.
- `FIRE_AND_FORGET`: run the handler without a response subscription or response.

Requests and results are caller-supplied Pydantic models. Correlation IDs,
outstanding work, deduplication state, payload size, timeouts, and handler tasks are
bounded. Cancellation removes the local waiter; it does not send a cancellation
message.

The SDK completes ACK publication before invoking the handler. This makes the
single-ack contract deterministic: exactly one acknowledgement is emitted, and
handler completion does not generate another response.

See [receive local tasks](../how-to/receive-local-tasks.md),
[dispatch local tasks](../how-to/dispatch-local-tasks.md), the
[terminal-addressed task guide](../how-to/dispatch-mesh-tasks.md), and the
[task wire reference](../reference/wire-formats.md#task-json). Effector services
should also follow the [Effector integration guide](../integrations/effectors.md).
