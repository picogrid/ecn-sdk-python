---
title: Operator application
tableOfContents: false
sidebar:
  order: 2
---

Use the local browser operator application to observe authorized ECN data and rehearse
operator workflows. It is a separate, installable reference-consumer wheel built on
the public Picogrid ECN SDK wheel. The operator package does not add its browser
server or frontend dependencies to the thin SDK runtime.

## Operator surface

Mock mode provides moving synthetic entities, locations, filters, freshness,
diagnostics, and deterministic cleanup. Task controls are off by default, so the
application reports that tasking is **disabled**.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact.

## Connect to a live deployment

Live mode requires TLS with server-certificate verification and exactly one selected
authentication method: mTLS, or the MQTT username and bearer token supplied together
for that ECN.
Use the host and port issued for the selected profile. Both profiles require explicit
integration and category allowlists. Enabling tasking additionally requires a closed
command policy, exact target-UUID allowlist, per-task prepare/review, and confirmation.

## Trusted local browser boundary

The browser-facing HTTP and WebSocket routes are a trusted, local, single-user
application surface. They are not ECN APIs. ECN traffic remains MQTT v5 through
`ECNClient`; the application does not proxy or invent ECN HTTP resources.
Use the packaged `picogrid-ecn-operator` launcher. Mounting `operator_app.app:app`
under another application server is unsupported because it bypasses the launcher's bind
check.

## Run the application

Install the matching client and operator wheels as described in
[Install the SDK](../getting-started/installation.md#install-the-operator-application).
The operator wheel already contains its compiled frontend, so runtime users do not
copy repository source, install Node.js dependencies, or start a separate Vite
process.

Start the read-only synthetic demonstration:

```bash
picogrid-ecn operator --demo
```

Or, after configuring a named profile and explicit observation allowlists, start a
live read-only view:

```bash
export OPERATOR_ECN_INTEGRATION_ALLOWLIST=authorized-integration
export OPERATOR_ECN_CATEGORY_ALLOWLIST=TRACK,DETECTION
picogrid-ecn operator --profile NAME
```

Before any live launch, obtain written authorization that names the exact ECN target
and each permitted operation. Observation authorization does not authorize task
publication.

Tasking still requires its separate enable flag, closed command policy, exact target
UUID allowlist, and per-task confirmation. The installed application is verified in
offline mode; staging and production operator behavior remain unverified. Continue
to the [operator walkthrough](../walkthroughs/tactical-live-map.md#operator-walkthrough).
