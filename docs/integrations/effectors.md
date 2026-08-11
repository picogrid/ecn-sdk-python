---
title: Effector integration
sidebar:
  order: 2
---

Use an effector integration to receive a task for one entity, validate it, perform the
approved action, and acknowledge or return a terminal result or failure according to
the selected task mode.

Start with the [effector handler quickstart](../quickstarts/effector-handler.md). Use
the ordinary same-ECN path unless the deployment has supplied canonical terminal
identities and an authorized cross-terminal route.

## Define the command contract

Use Pydantic request and result models with explicit field types and size bounds.
Treat validation failure as an expected input condition, and keep secrets out of
payloads and error messages.

Register one handler for each exact `(entity UUID, command)` pair. Registration is an
MQTT subscription only; command discovery and advertisement are infrastructure
responsibilities outside the SDK.

## Choose a response mode

- `COMPLETE` waits for the handler and returns success, pending, or failure.
- `ACKNOWLEDGMENT` publishes exactly one acknowledgement, then runs the handler
  without an unsolicited final response.
- `FIRE_AND_FORGET` runs the handler without a response.

Handlers must be async and cooperate with cancellation. Keep effects idempotent:
deduplication is bounded and process-local, so it does not survive restart.

## Shut down deterministically

Own the client for the service lifetime. Unregister handlers in `finally`, bound any
external work the handler starts, and let client shutdown cancel remaining handler
tasks. A sender timeout or cancellation removes only its local waiter; it does not
publish a remote cancellation command.

Literal local remains the compatibility profile. A configured terminal UUID enables
the documented terminal-derived source. An exact addressed response route additionally
requires a different `target_terminal_id` and a compatible authorized route. Review
[mesh routing](../concepts/mesh-routing.md) and
[Compatibility and limitations](../compatibility/limitations.md) before accepting or
dispatching a task through another ECN.
