---
title: Security model
tableOfContents: false
sidebar:
  order: 7
---

Broker authentication and ACLs are the authorization boundary. The SDK never turns
method availability or connection success into a claim of permission.

## SDK security boundaries

Safe defaults include:

- verified TLS by default for every non-loopback connection;
- a separate, mutually required `NoAuth` plus `ReviewedContainerNetwork` opt-in for
  credential-free plaintext inside an operator-reviewed private container network;
- no arbitrary topic API or multi-level wildcard;
- no startup subscriptions;
- fixed payload, queue, task, and timeout limits;
- secret-safe public exceptions and diagnostics; and
- read-only preflight with publish authorization reported as unknown.

The models and wheel are fully inspectable and modifiable by recipients. Keep
authorization on the broker; do not rely on hidden SDK code. Treat examples
that publish or dispatch as mutations and run them only against an explicitly
authorized target.

The reviewed-container-network name is an operator attestation that the SDK cannot
independently verify. The SDK enforces the part it can observe: on every connect and
reconnect it resolves the configured host, requires every returned address to be
private, refuses a mixed private/public answer, and dials the validated address
literal. It supplies no credential on that plaintext hop. Operators remain
responsible for container-network isolation, routing, and protection against route
changes after resolution.

The offline mock's synthetic ACL labels are testing controls, not deployed scopes.
Current external compatibility status is centralized on
[Compatibility and limitations](../compatibility/limitations.md).

See [authentication](../getting-started/authentication.md),
[preflight](../getting-started/preflight.md), and the
[credential guide](../security/credentials.md).
