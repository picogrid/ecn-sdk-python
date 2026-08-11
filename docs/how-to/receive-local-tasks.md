---
title: Receive a local task
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow to receive and validate tasks addressed to one exact
entity-and-command handler on the connected ECN.

## Run the local receiver

The [runnable task-handler example](../../examples/receive_task.py) registers one
exact entity-and-command subscription and validates requests and results with
Pydantic models.

```bash
python examples/receive_task.py --check
export ECN_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_TASK_COMMAND=echo
export ECN_TASK_LIMIT=1
python examples/receive_task.py
```

## Operate the handler safely

Registration itself subscribes and is operational behavior. Use it only where task
receipt is explicitly authorized. The handler is invoked only for literal-local
requests when no terminal identity is configured. With `ECN_TERMINAL_ID`, it also
accepts canonical terminal UUID sources and constructs the confirmed response route.
Malformed sources, or UUID sources without local terminal configuration, are ignored
without a response. Always unregister in `finally`.
For a service, keep the client context open and make handler effects idempotent.
Handlers must be async; blocking work must use a caller-owned, explicitly bounded
facility whose lifecycle the caller controls.

## Interpret the response mode

ACK mode sends `Task started` before handler completion and sends no final response.
If that ACK's broker acceptance becomes unknown after send begins, the receiver does
not retry it and still invokes the handler once locally so a possibly delivered
`Task started` response is not false; duplicate deliveries remain suppressed. A sender
that did not receive the ACK must still treat the outcome as unknown. During service
shutdown, close retains cancellation ownership and does not start a new handler.

Complete mode may return a typed success, pending, or failure result. Next, see
[Effector integration](../integrations/effectors.md) and the
[task-handler walkthrough](../walkthroughs/task-handler-service.md).
