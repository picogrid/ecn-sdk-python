---
title: Build an effector task handler
sidebar:
  order: 3
---

Use this quickstart to register a typed task handler for one effector UUID and
command, then exercise the complete task exchange. In three steps, you will validate
the task models, register the handler, and dispatch one task through the loopback
mock.

Before you begin, complete [installation](../getting-started/installation.md) and
have three shells available for the broker, handler, and dispatcher.

The loopback mock uses literal `source="local"`; deployments can supply a canonical
`ECN_TERMINAL_ID` for terminal-derived and addressed task sources.

**Run profile:** offline model check and loopback mock. The handler and dispatcher are
staging- and production-oriented only for an authorized target entity UUID, command,
and task route. Dispatch publishes a task and may cause a remote effect.

Shell labels (**A**, **B**, **C**) mark blocks that must run concurrently. Shell A
(the mock broker) stays open through the whole walkthrough. Each block inlines the
loopback-mock connection variables; for a live ECN, replace them with the
[authentication](../getting-started/authentication.md) block.

## 1. Validate the task models offline

```bash
python examples/receive_task.py --check
python examples/dispatch_task.py --check
```

## 2. Register one exact handler

**Shell A — mock broker (leave running):**

```bash
picogrid-mock-ecn --mqtt-port 1883
```

Skip Shell A if you are targeting a live ECN.

**Shell B — handler (leave running):**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_TASK_COMMAND=echo
export ECN_TASK_LIMIT=1
python examples/receive_task.py
```

Registration subscribes only to
`task/{integration}/{entity_id}/{command}` and waits for SUBACK. It does not
advertise the command, register infrastructure, or discover routes.

## 3. Exercise it with the offline mock

While Shell B is waiting for a task, dispatch one from Shell C targeting the same
integration and UUID.

**Shell C — dispatcher (fires once):**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1

export ECN_TARGET_INTEGRATION=example-integration
export ECN_TARGET_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_TASK_COMMAND=echo
export ECN_TASK_MESSAGE='synthetic request'
export ECN_TASK_MODE=complete
export ECN_TASK_TIMEOUT=10
python examples/dispatch_task.py
```

Dispatch is a mutation. Use it against a live ECN only when the exact target and
command are authorized.

Your next step is [Effector integration](../integrations/effectors.md), which covers
response modes, idempotency, cancellation, and shutdown.
