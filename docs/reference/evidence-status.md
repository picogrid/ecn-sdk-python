---
title: Evidence and workflow status
description: Meaning of support and verification labels used throughout the guide.
tableOfContents: false
sidebar:
  hidden: true
---

:::tip[Supported]
The public API implements this workflow on the retained MQTT v5 wire.
:::

:::caution[Changed semantics]
A safe equivalent exists, but private infrastructure, authoritative-query, routing,
or provisioning behavior was deliberately removed. Migration differences are stated
on the workflow page and in the parity matrix.
:::

:::danger[Deferred]
No confirmed public MQTT wire exists. The client raises a typed unsupported error or
documents the application boundary instead of inventing server behavior.
:::

:::note[Offline only]
The behavior is verified with the minimal mock or local static tooling. This is not
deployed compatibility evidence.
:::

:::note[Installed-wheel verified]
This label is applied only when a workflow runs outside the repository from the exact
inspected wheel, with `PYTHONPATH` removed. It is stronger release-artifact evidence
than a source-checkout test, but it is still offline and says nothing about a deployed
broker.
:::

:::caution[Reconnect amendment implemented; artifact verification pending]
The local candidate implements the typed reconnect policy, classified recovery,
connection event/wait/credential-notification/retry surface, delivery phases,
interrupted-QoS-0 and task outcome-unknown failures, explicit MQTT session settings,
and no-replay guarantees. Exact installed-wheel and hosted verification remain
pending. DNS and path-backed TLS-material reader cleanup requests kill the child and
perform a bounded drain; a runtime cleanup primitive that resists cancellation can
outlive that bound. Temporary PEM is removed before the builder returns.
Standard-library/OpenSSL context parsing remains synchronous and cannot be interrupted
immediately, so immediate hard-deadline and zero-background-task shutdown remain
incomplete. The amendment has no staging or production evidence. Historical reconnect
behavior and staging results do not satisfy it.
:::

:::caution[Operator artifact candidate]
The separate operator wheel has focused offline evidence for two-build
reproducibility, its fixed allowlist and embedded frontend bytes, a dependency-resolving
combined install with the matching client wheel, the installed one-command demo, and
the supported Python backend suites. The fresh hosted exact-artifact Verify and Pages
gates remain pending. The operator application has not been validated against staging
or production.
:::

:::note[Configured-ECN clock diagnostic]
The clock API is offline-verified against deterministic NTPv4 fixtures. Its
installed-wheel and authorized-staging labels are recorded separately: installed
verification proves the packaged API, CLI, example, validation, cancellation, and
cleanup behavior; staging verification additionally requires a bounded response from
the configured ECN NTP service. Neither label implies MQTT readiness, authentication,
authorization, or permission to modify a clock.
:::

:::caution[Staging pending]
The workflow has not completed a bounded, sanitized session against the authorized
staging target. A narrower prerequisite may have completed without making the whole
named workflow staging-verified; the associated row states that boundary explicitly.
:::

:::tip[Staging verified historical slice]
This label applies only to the operation and exact candidate named. A bounded
sanitized run used an installed immutable candidate and target-specific mTLS
credentials. It verified zero-publish MQTT v5 preflight; synthetic JSON and protobuf
TRACK at QoS 0; JSON and protobuf location, JSON Position Location Information (PLI),
and one exact same-ECN local task result at QoS 1. It does not imply broader topic,
route, or current credential access.
:::

:::caution[Current staging readiness]
A later bounded check of the then-current exact candidate passed configuration, DNS,
TCP, verified TLS, and authentication-material validation, but MQTT v5 authentication
was rejected before any subscription or application publication. The credential or
ACL state remains a blocker for validating the next exact candidate. The historical
slice above remains compatibility evidence only.
:::

:::tip[Historical staging clock diagnostic]
An installed-wheel candidate completed the separately authorized, clock-only
staging check within its request and duration limits. The CLI tolerance check
returned exit status `0`; it started no MQTT operation, changed no clock or
configuration, and left no retained raw output or temporary validation files.
Precise timestamps, measurements, endpoint details, and other validation-capture
data are deliberately not retained in this repository. The temporary wheel digest
was not retained, and that candidate predates the public
local-capture-uncertainty field. This is therefore historical compatibility
evidence only, not verification of the current exact artifact or its uncertainty
reporting.
:::

No production workflow is verified or authorized by this repository. Cross-terminal
task routing, terminal geolocation, bearer authentication, and the live operator
application remain staging-unverified. The bounded session retained no raw output;
closed every watcher, task registration, and client; and left no pending local task.
It recorded zero decode errors or local drops. Its synthetic publications may remain
in broker-retained staging state because the public wire defines no deletion
operation.

An additional receive-only fixed-depth retry immediately after an
operator-controlled peer restart received 100 TRACK events representing 72 canonical
UUID identities, all with embedded locations, plus 74 dedicated location events. It
performed zero application publishes and closed with no drops, decode errors, leaked
SDK tasks, or remaining connection. This is operator-corroborated target-side mesh RX
evidence. Prefix stripping removes authenticated peer provenance from the public
event, and the public receive API exposes neither original JSON/protobuf format nor
delivered QoS, so peer-specific origin and cross-terminal task routing remain
unverified.

The optional configured-ECN clock diagnostic is offline-, exact-installed-wheel-, and
bounded-staging-verified only for the candidate and response described above. This is
not production evidence and does not verify MQTT readiness or authorization.
