---
title: Dispatch and receive mesh tasks
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow for cross-terminal tasking: address one task to a configured terminal
on another ECN and run a receiver for that task. The wire contract uses a
terminal-addressed request keyed by a canonical target terminal UUID. The source
terminal UUID supplies the return address; deployment infrastructure, not the SDK,
must route the request and response.

## Configure terminal identities

When `ECN_TERMINAL_ID` is configured, the SDK serializes that UUID as the task source
for both unprefixed and terminal-addressed dispatch. Omit
`ECN_TARGET_TERMINAL_ID` for the unprefixed same-ECN path. A supplied target must
differ from the source UUID and selects the exact terminal-addressed request route;
an explicit self-target is rejected before any subscription or publication. The SDK
does not discover or validate a route.

## Dispatch one task

The [mesh dispatch example](../../examples/dispatch_mesh_task.py) sends one typed
request to an explicitly configured target terminal, integration, entity, and
command:

```bash
python examples/dispatch_mesh_task.py --check
export ECN_TERMINAL_ID=00000000-0000-4000-8000-000000000011
export ECN_TARGET_TERMINAL_ID=00000000-0000-4000-8000-000000000012
export ECN_TARGET_ENTITY_ID=00000000-0000-4000-8000-000000000013
export ECN_TARGET_INTEGRATION=authorized-receiver
export ECN_TASK_COMMAND=echo
export ECN_TASK_MESSAGE='synthetic message'
export ECN_TASK_MODE=complete
export ECN_TASK_TIMEOUT=10
python examples/dispatch_mesh_task.py
```

## Receive the task

The [mesh receiver example](../../examples/receive_mesh_task.py) registers one exact
entity-and-command handler and prints the validated `TaskRequestContext.source`:

```bash
python examples/receive_mesh_task.py --check
export ECN_TERMINAL_ID=00000000-0000-4000-8000-000000000012
export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000013
export ECN_TASK_COMMAND=echo
export ECN_TASK_LIMIT=1
python examples/receive_mesh_task.py
```

## Interpret the result

Both examples use only the confirmed MQTT task family and issue at most one request
publication. A timeout or caller cancellation removes the local response subscription
but does not cancel the remote effect. Use synthetic targets until the deployment
owner confirms the exact terminal identifiers, route policy, command allowlist, and
broker ACLs.

The examples' offline checks validate the public request and source models without a
network connection. Complete cross-ECN execution remains externally unverified and
must not be inferred from the mock.

Continue with
[Compatibility and limitations](../compatibility/limitations.md).
