---
title: Dispatch a local task
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow to send one task to a configured integration and entity through the
connected ECN.

## Run the local dispatch

The [runnable dispatch example](../../examples/dispatch_task.py) publishes one task
request and, when the selected mode expects it, subscribes only to the exact derived
response topic.

```bash
python examples/dispatch_task.py --check
export ECN_TARGET_ENTITY_ID=00000000-0000-4000-8000-000000000001
export ECN_TARGET_INTEGRATION=authorized-receiver
export ECN_TASK_COMMAND=echo
export ECN_TASK_MESSAGE='synthetic message'
export ECN_TASK_MODE=complete
export ECN_TASK_TIMEOUT=10
python examples/dispatch_task.py
```

## Understand the route

Dispatch is a mutation and is prohibited in read-only validation. The SDK uses the
literal `source="local"` compatibility profile when `ECN_TERMINAL_ID` is absent.
When that canonical UUID is configured, it becomes the request source, matching the
normal terminal-derived wire. The request topic remains unprefixed.

## Interpret the outcome

An unprefixed task can be forwarded when the deployment routes the task family, so
“local” is not an infrastructure guarantee. Use the
[terminal-addressed task workflow](dispatch-mesh-tasks.md) when one configured target
terminal is required. A timeout or caller cancellation cleans up the response
subscription but does not publish a cancellation request.

When a human must review and confirm a task before publication, continue with the
[operator workflow](../operator/workflows.md).
