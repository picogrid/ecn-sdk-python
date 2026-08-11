---
title: Configure a connection
tableOfContents: false
sidebar:
  order: 2
---

Create a named connection profile after installing the SDK. The profile gives the
command-line tools and examples one reusable set of ECN connection settings without
storing secret values.

## Create a named profile

For interactive work, run:

```bash
picogrid-ecn configure
picogrid-ecn doctor --profile NAME
```

A profile selects the endpoint, integration identity, credentialed authentication
method, credential-file references, publication wire format, and optional reconnect
policy. It persists only non-secret settings and credential-file references in the
platform/XDG configuration location with restrictive directory and file permissions.
It never writes a bearer token, edits shell startup files, or stores secret values in
command history.
Reviewed-container-network mode is deliberately available only through environment
variables or direct `ECNConfig` construction, so its security attestation is never
silently persisted to a profile file.

Provide:

- a DNS name or IP literal (IPv4 or IPv6) without a scheme or port;
- a port from 1 through 65535; use 8883 for mTLS or 8884 for bearer or local
  `legion-system-auth` authentication, unless the endpoint owner provided a different
  port;
- an integration name of 2 through 128 ASCII letters, digits, underscores, or
  hyphens, starting and ending with a letter or digit (`geolocation` is reserved,
  case-insensitively); and
- exactly one credentialed authentication method.

Verified TLS is the default. Plaintext or unverified TLS still requires the explicit
`allow_insecure=True` opt-in for a loopback mock. The separate
`ReviewedContainerNetwork` opt-in permits plaintext only with `NoAuth`; each is
rejected without the other, and credential or mTLS material cannot be combined with
it.

## Use the profile

Examples accept `--profile NAME` or `ECN_PROFILE`. Environment variables remain
supported for CI and automation: `ECN_HOST`,
`ECN_MQTT_PORT`, `ECN_INTEGRATION_NAME`, and the authentication variables described
on the [authentication page](authentication.md). `ECN_AUTH` explicitly selects
`mtls`, `bearer`, `legion`, or `none`; in environment-only use it selects the
authentication path, and with a profile it must remain coherent with that profile's
credential references. `ECN_AUTH=none` additionally requires
`ECN_PLAINTEXT_CONTAINER_NETWORK` and defaults to port 1883.
`ECN_WIRE_FORMAT` selects `json` or `protobuf` for entity and location publication.
Optional `configure` flags and the six per-field reconnect-policy environment
overrides are listed with their precedence in the
[configuration reference](../reference/configuration.md#timeouts-and-reconnect).

Application code may still construct one immutable `ECNConfig` directly. Use the
detailed [configuration reference](../reference/configuration.md) for fields and
limits.

Next, [select and verify an authentication method](authentication.md).
