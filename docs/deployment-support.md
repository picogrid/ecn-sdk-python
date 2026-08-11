---
title: Deployment and support
sidebar:
  order: 1
---

Deploy the Picogrid ECN SDK as a Python dependency inside the integration that owns
its sensor, effector, observer, or operator workflow. Use this checklist to prepare
that integration for an authorized ECN (Expeditionary C2 Node) deployment.

## Deployment checklist

1. Install the SDK wheel from your approved distribution channel in an isolated
   environment.
2. Supply the ECN endpoint, MQTT port, and integration identity. Use verified TLS
   with approved credentials. For the credential-free plaintext mode, configure
   `NoAuth`, explicitly disable TLS, and explicitly attest the reviewed private
   container network.
3. Keep credentials outside source, container images, build contexts, and logs; never
   combine them with the plaintext mode.
4. Run read-only preflight and evaluate every failed check.
5. Configure exact integration/category or UUID/command allowlists.
6. Validate the workflow against the loopback mock, then use separately authorized
   staging validation before operational deployment.
7. Monitor connection state, watcher activity, decode errors, dropped events, task
   outcomes, and shutdown failures.
8. Close streams, unregister task handlers, and close the client during service
   termination.

The included operator application has additional container, loopback binding, and
human-confirmation requirements. See its
[production-use checklist](walkthroughs/tactical-live-map.md#production-use-checklist).

## Troubleshooting and support

- Use [Troubleshooting](how-to/troubleshooting.md) for connection, subscription,
  decode, and timeout symptoms.
- Use [Cleanup and cancellation](how-to/cleanup.md) for deterministic teardown.
- Review [Compatibility and limitations](compatibility/limitations.md) before filing
  an interoperability issue.
- Follow [SUPPORT.md](../SUPPORT.md) for product help.
- Report security issues through
  [VULNERABILITY_REPORTING.md](../VULNERABILITY_REPORTING.md), never a public issue.

Include SDK version, Python version, exception code, operation name, and sanitized
broker reason codes in support requests. Do not include endpoints, credentials,
certificate paths, raw payload captures, or operational entity data.
