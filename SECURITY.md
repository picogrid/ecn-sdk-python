# Picogrid ECN SDK security guide

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub private vulnerability reporting as described in
[VULNERABILITY_REPORTING.md](VULNERABILITY_REPORTING.md). Do not open a public issue or include
secrets, credential-bearing paths, customer data, operational endpoints, or raw traffic captures.
Coordinate validation and disclosure privately with the maintainers.

## Supported versions

Supported-version, compatibility, and deprecation policies are defined in
[VERSIONING.md](VERSIONING.md). No public package or supported version has been published yet;
these policies apply beginning with the first published release.

## Responsible disclosure

Give maintainers a reasonable opportunity to investigate and publish a safe release or mitigation
before public disclosure. Test only with synthetic data or against a target for which the exact
action is authorized, and stop if validation could affect another user or persistent state.

## Security boundary

The SDK runs on a recipient-controlled machine and can be inspected or modified. No security
property depends on wheel secrecy, obfuscation, private Python names, or the absence of a client
method.

The ECN's MQTT broker authenticates credentials and enforces ACLs for every operation. The client
cannot grant access. A successful connection or subscription does not establish publish
permission, and application policy does not replace broker authorization.

The SDK exposes typed methods over a fixed set of supported topic families. See
[MQTT topics and delivery](docs/concepts/mqtt-wire.md) for the broker ACL contract and
[wire formats](docs/reference/wire-formats.md) for payload interoperability.

## Credentials and certificates

- Give each integration a distinct, least-privilege credential and narrow broker ACLs.
- Do not use broker-administrator credentials or broad wildcard grants.
- Keep tokens, private keys, and certificates out of source control, logs, build contexts, test
  fixtures, reports, provenance, and release artifacts.
- Prefer operating-system secret storage or an injected token provider over embedded values.
- Rotate credentials promptly when exposure is suspected.

`SecretStr` masks values in ordinary representations but does not protect a compromised process or
host. When the MQTT library requires certificate or key bytes in files, the SDK creates temporary
files outside the repository with mode `0600` and removes them during close and failure cleanup.

## Verified TLS

Certificate-chain and hostname verification are enabled by default. Never disable verification to
work around a hostname or trust-chain mismatch.

For remote ECN connections, the only plaintext exception is the explicit `NoAuth` plus
`ReviewedContainerNetwork` pairing. It requires TLS to be disabled, rejects all credential
material, and revalidates private addresses on every connect and reconnect. The operator remains
responsible for the isolation and routing of that private container network. Separately, for
loopback development against the bundled mock only, `ECNConfig` accepts plaintext or unverified
TLS when `allow_insecure=True` is set explicitly and the host is a loopback address; it rejects
that combination for any non-loopback endpoint.

## Least privilege and read-only preflight

Preflight performs configuration, DNS, TCP, TLS, and MQTT v5 CONNACK checks, plus only the bounded
subscription probes explicitly requested by the caller. It sends zero application-level publishes;
without probes, it sends no subscription.

A successful probe means only that the broker accepted that filter at that time. It is not a scope
grant, proof of publish access, or permission negotiation. Use the narrowest authorized filters and
keep authorization enforcement on the broker.

## Untrusted input, duplicates, and tasks

Treat every MQTT packet, JSON object, protobuf message, and task payload as untrusted, even after
authenticated transport. The SDK applies payload bounds, strict identity and value validation,
bounded queues and concurrency, and redacted public errors; applications must still validate data
for their domain before acting on it.

MQTT QoS and reconnect behavior can produce duplicates. Duplicate suppression is bounded and
process-local, so restart or eviction can permit re-execution. Task handlers with side effects must
be idempotent, and the broker must prevent unauthorized publishers from spoofing requests or
results.

Task and response payloads carry canonical source and target UUIDs. These values are routing
metadata, not authenticated identity: the SDK validates their syntax and derives response routes
from them, but any publisher the broker permits on a task topic can choose them. Never use a
payload UUID as an authorization boundary; authorize publishers with broker ACLs and validate
requests in the handler.

Timeout or cancellation ends only the caller's local wait; it does not cancel remote work. Late or
repeated responses may still exist even when the local caller no longer accepts them.

## Secret-safe logging and diagnostics

Public errors and diagnostics may include operation names, status categories, durations, and safe
topic-family names. They must never include:

- bearer or JWT values;
- private-key or certificate contents;
- credential-bearing paths or connection strings;
- authorization headers;
- token-provider return values; or
- raw hostile payloads.

Review tracebacks and application logs before sharing them, and use synthetic values in examples
and reports.

## Mocks and live ECNs

`MockECN` is a loopback development simulator, not a hardened broker. Its synthetic tokens and ACL
labels are public fixtures and must not be treated as deployed credentials or authorization.
Prefer the mock for deterministic development.

Use only ECNs and credentials you are authorized to access. Avoid tests that could affect production
without approval for the exact target and action. A passing mock or one authorized ECN does not
establish authentication, ACL, routing, or payload compatibility for another deployment.

## Operator application

The optional browser operator application is outside the client runtime wheel and is distributed
as a separately allowlisted wheel. Its compiled frontend is package data; runtime users do not
need Node.js, npm, or repository source. It defaults to read-only mock mode and requires explicit
integration and category allowlists for observation, plus explicit integration, command, and exact
target-UUID allowlists and per-task human confirmation for tasking. Human confirmation is not
broker authorization. It is a trusted single-user, local-host control surface. Its Host, Origin,
body-size, intent-header, and loopback-bind controls do not authenticate another local process or
user. Do not expose it directly to untrusted users or treat its application policy as a broker ACL.

## Dependencies and artifact verification

Runtime dependencies are Pydantic, aiomqtt/paho-mqtt, and protobuf. TLS is provided through the
Python standard library's `ssl` module.

Each published release must use separate two-build reproducibility checks and fixed allowlists for
both wheels, prove that the operator wheel's embedded frontend is byte-equal to the separately
inspected locked build, bind separate runtime and frontend SBOMs to the operator wheel, and include
three-artifact checksums and provenance.
No package has been published yet; when one is, verify
downloaded artifacts before installation by following
[VERIFYING_ARTIFACTS.md](VERIFYING_ARTIFACTS.md).

The distribution is licensed under MPL-2.0 and is provided without warranty as stated in sections
6 and 7 of that license. These security practices do not create a warranty.
