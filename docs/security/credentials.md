---
title: Security and credentials
sidebar:
  order: 1
---

Treat ECN connection material as secret. Picogrid ECN SDK accepts caller-provided
trust and identity material but does not issue credentials or store secret values.
Named profiles persist only non-secret settings and credential-file references.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact.

## TLS and mTLS

Verified TLS remains the default for non-loopback connections. For mTLS, provide a
client certificate and private key. `TLSConfig` accepts optional CA material when the
deployment requires a custom trust root; otherwise the system trust store is used.
Configure an optional password only for an encrypted private key. mTLS is rejected
when TLS is disabled. The connection requires TLS 1.2 or newer.
Certificate paths are reopened and the TLS context is rebuilt on every connection
attempt so atomic file rotation can take effect. Each configured path must resolve to
a non-empty regular file; a symlink to a regular file is accepted. Each CA,
certificate, or key is limited to 1 MiB, each encoded path to 16 KiB, and an encrypted
private-key password to 64 KiB. Path reads run in a disposable process. Cancellation
requests kill it and perform a bounded cleanup drain; a runtime child-cleanup
primitive that resists cancellation can outlive that bound. SDK-created snapshots are
mode 0600 below a mode-0700 directory and are removed before the asynchronous TLS
builder returns.

Python's standard-library TLS context cannot be transferred from that process, so its
native trust and certificate parsing still runs synchronously over the bounded private
snapshots. Temporary PEM is removed before the builder returns, but a close request
during that native call is observed only when the call returns. Immediate
hard-deadline and zero-background-task shutdown remain pending.

Plaintext has two separate explicit opt-ins. `allow_insecure=True` remains a
loopback-mock developer affordance. A `ReviewedContainerNetwork` attestation permits
plaintext only with `NoAuth` and disabled TLS; neither can be supplied without the
other, and all bearer, provider, certificate, and key material is rejected so no
credential can leak on that plaintext hop. The attestation is an operator assertion,
not a network-isolation fact the SDK can prove. The SDK resolves and requires every
address to be private on every connect and reconnect.

## Bearer authentication

The bearer/JWT profile uses the provided MQTT username and current access token. A
cooperative async token provider is resolved on each connection attempt, including
reconnect. Synchronous provider callables are rejected before invocation so a blocked
worker cannot outlive cancellation or shutdown. Token issuance, refresh, rotation,
and revocation remain external. A static token can expire and a later reconnect with
stale material can be rejected.

The required username identity varies by deployment role, so it is never inferred
from `integration_name`. External bearer CONNACK compatibility is still unverified.
mTLS and bearer are separate profiles and cannot be combined.

For local `legion-system-auth` integration, the SDK selects and uses only the current
access token and source-confirmed `integrationId` on each connect or reconnect. It
does not select, expose, or use refresh tokens or client secrets and never runs
setup, installs a service, invokes `sudo`, or changes service state. If configuration is absent, follow the
[official setup instructions](https://github.com/picogrid/legion-system-auth/blob/9f618b7ce1648789d816a49b8fd0ec0ab21ea24a/README.md#1-initial-setup)
and run `legion-auth setup` yourself.

## Secret handling

- Load secret values from an approved secret manager or runtime environment.
- Keep certificate and key files outside source, images, fixtures, and reports.
- Restrict file permissions and mount credentials read-only where practical.
- Never print secret values or credential-bearing paths.
- Do not put tokens in broker URLs or add private keys to an agent.
- Close the SDK so temporary in-memory credential material and connections are
  released.

## Authentication and authorization

Authentication establishes identity. [Broker ACLs](../concepts/acls.md) remain the
authorization boundary.
