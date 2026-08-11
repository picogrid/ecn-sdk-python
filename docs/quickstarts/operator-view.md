---
title: Run the operator view
sidebar:
  order: 4
---

Use this quickstart to run the installed operator application as a local map for
entity and location data. In three steps, you will start its offline demonstration,
verify read-only observation, and understand the controls that can enable tasking.

Before you begin, complete [installation](../getting-started/installation.md). The
client and operator are separate wheels from the same inspected release and must be
installed together. The operator wheel contains the compiled browser application;
Node.js and npm are build-time tools, not runtime requirements.

**Run profile:** offline mock by default. Live observation is staging- and
production-oriented only with issued credentials and explicit integration and
category allowlists. Tasking remains disabled until separately armed with command and
target-UUID allowlists.

## 1. Start the offline demonstration

```bash
picogrid-ecn operator --demo
```

Open `http://127.0.0.1:8080`. The command starts one loopback application server,
uses only synthetic UUIDs, and keeps tasking disabled. It does not require a source
checkout, temporary application copy, Node.js installation, npm command, ECN
credential, or network connection to an ECN.

## 2. Verify observation

Open the loopback browser URL and confirm:

- connection state is ready;
- only configured integration and category allowlists appear;
- track, detection, and location-only markers use canonical UUIDs;
- stale and disconnected state are visible; and
- dropped-event and decode-error counters remain bounded diagnostics.

## 3. Keep tasking off until needed

Tasking remains disabled in both mock and live modes unless explicit integration,
command, and target-UUID allowlists are supplied and the tasking flag is enabled.
Every task requires a separate prepare/review step and a final operator confirmation.
The application publishes once and never retries automatically.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact. After the
profile and read-only observation allowlists are configured, live observation uses:

```bash
export OPERATOR_ECN_INTEGRATION_ALLOWLIST=authorized-integration
export OPERATOR_ECN_CATEGORY_ALLOWLIST=TRACK,DETECTION
picogrid-ecn operator --profile NAME
```

This command does not enable tasking. The included application has offline and
installed-artifact verification, but no staging or production operator validation is
claimed.

Your next step is to follow [Operator workflows](../operator/workflows.md), then use
the [production-use checklist](../walkthroughs/tactical-live-map.md#production-use-checklist)
before deployment.
