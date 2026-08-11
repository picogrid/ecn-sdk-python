---
title: Operator workflows
sidebar:
  order: 1
---

Use this workflow to turn authorized entity and location observations into an
operator display with clear freshness, identity, connection, and task states. Scope
the display to the integrations and data categories assigned to the operator.

## Authenticate

Live observation always uses verified TLS. Select the mTLS or bearer profile issued
for the ECN; never combine the two. The profile identifies the connection, while
broker ACLs decide which observations it can receive.

## Observe

Design the display to:

- Open entity watchers for only the categories on the current display.
- Correlate entity and location events by canonical UUID.
- Age entity and location observations independently and label each **fresh** or
  **stale**.
- Distinguish **connected**, **disconnected**, and **reconnecting** conditions.
- Report **data dropped** when decoding fails or a watcher reaches its local buffer
  limit.
- Treat client-observed location as ephemeral, not authoritative history.

## Decide

Use affiliation, category, type, display metadata, and freshness to help an operator
select a target. A location-only marker must remain visibly distinct from an entity
with confirmed metadata. Never present stale data as current.

## Act

Keep tasking **disabled** for ordinary observation. If the deployment enables it:

1. Restrict tasking to the approved integration, command, and target-UUID allowlists.
2. Require the payload to pass validation.
3. Show the complete target, command, response mode, and payload for operator review.
4. Stop if the state is **authorization denied**, **disconnected**, or
   **reconnecting**.
5. Require final human confirmation labeled **This action sends the task**.
6. Publish once with no automatic retry.

In an operator display, distinguish **sent** from downstream execution. If no
conclusive result arrives, show **outcome unknown**, never success. Show denied
requests as **authorization denied** and unsuccessful requests as **failed**.

The included [operator application](application.md) demonstrates the allowlists,
payload validation, complete review, final confirmation, and single-send controls.
For a smaller downstream consumer, use the
[track-viewer walkthrough](../walkthroughs/track-viewer.md).
