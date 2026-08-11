---
title: Configuration reference
tableOfContents: false
sidebar:
  order: 2
---

Use this page to find profile behavior, `ECNConfig` fields and defaults, and
authentication-source mapping.

## Named profiles

`picogrid-ecn configure` writes a versioned profile file to the platform
configuration directory. `$XDG_CONFIG_HOME`, when set, takes precedence on supported
POSIX platforms:

- XDG: `$XDG_CONFIG_HOME/picogrid/ecn-sdk/profiles.json`;
- Linux fallback: `~/.config/picogrid/ecn-sdk/profiles.json`;
- macOS: `~/Library/Application Support/picogrid/ecn-sdk/profiles.json`.

Named-profile persistence is currently supported on POSIX systems where the SDK can
enforce ownership-safe directory operations and modes. On Windows, use environment
variables or construct `ECNConfig` directly until an equivalent fail-closed storage
implementation is verified.

The SDK-owned directory is mode `0700` and the file is mode `0600`. Writes are
atomic. Symlinks, unsafe modes,
duplicate JSON keys, and unsupported profile versions fail closed.

The file stores only endpoint settings, authentication kind, integration identity,
reconnect policy, and credential-file references. It never stores bearer tokens,
private-key passwords, refresh tokens, or client secrets. Load a profile through the
public API:

```python
from picogrid_ecn_client import load_config


config = load_config(profile="NAME")
```

Reviewed-container-network mode is not a named-profile authentication kind. It is
available only through environment variables or direct `ECNConfig` construction, so
the operator's security attestation is never silently persisted to a profile file.

The named-profile authentication kinds and defaults are:

| Kind | Default port | Credential source |
|---|---:|---|
| `mtls` | 8883 | CA, client-certificate, and private-key file references |
| `bearer` | 8884 | Provided MQTT username plus a current token supplied at runtime |
| `legion` | 8884 | Current local `legion-system-auth` access token and `integrationId` |

Override a default port only when Picogrid provided a different endpoint. Examples
accept `--profile NAME` or `ECN_PROFILE`. Environment variables remain supported for
CI and automation.

## Direct configuration

`ECNConfig` is immutable validated configuration for one SDK MQTT v5 connection.
Fields are grouped by purpose for faster lookup.

### Connection, identity, and transport

| Field | Default | Meaning |
| --- | --- | --- |
| `host` | required | DNS name or IP literal (IPv4 or IPv6) without scheme or port. |
| `mqtt_port` | required | Port from 1 through 65535; profile loading defaults to 8883 for mTLS and 8884 for bearer or local `legion-system-auth`, while environment loading defaults to 1883 for `none`. |
| `ntp_host` | ECN `host` | Optional issued alternate endpoint for the ECN-relative clock diagnostic. |
| `ntp_port` | 123 | UDP port from 1 through 65535 for the ECN-relative clock diagnostic. |
| `integration_name` | required | 2-128 ASCII letters, digits, `_`, or `-`; starts and ends alphanumeric; `geolocation` is reserved case-insensitively. Also the client-ID prefix and offline mock bearer username. |
| `terminal_id` | `None` | Canonical UUID of the connected ECN terminal, used only for terminal-derived task source and response routing. |
| `auth` | required | `BearerTokenAuth`, `MTLSAuth`, or `NoAuth`. |
| `tls` | verified TLS | `TLSConfig`; must be disabled with the reviewed-container-network attestation. |
| `plaintext_container_network` | `None` | `ReviewedContainerNetwork` operator attestation, mutually required with `NoAuth`. |
| `wire_format` | `JSON` | JSON or protobuf payloads; MQTT v5 remains fixed. |

Path-backed CA, client-certificate, and key material is snapshotted on every
connection attempt so atomic replacement can take effect. Each path must open as a
non-empty regular file (a symlink to a regular file is accepted), each material value
is limited to 1 MiB, each encoded path to 16 KiB, and an encrypted-key password to
64 KiB. Secret values and credential-bearing paths are omitted from public errors.

### Timeouts and reconnect

| Field | Default | Meaning |
| --- | --- | --- |
| `connection_timeout` | 10 seconds | Initial credential-resolution-through-CONNACK deadline and post-ready per-attempt bound; startup owns no subscription. |
| `operation_timeout` | 30 seconds | General operation bound, including restored SUBSCRIBE/SUBACK clipped by a configured recovery elapsed deadline. |
| `task_timeout` | 10 seconds | Default task response bound. |
| `shutdown_timeout` | 5 seconds | Whole-client cleanup bound. |
| `reconnect_policy` | `ReconnectPolicy()` | Full-jitter backoff, stable reset, and optional recovery attempt/elapsed budgets. |

### Resource limits and local mock access

| Field | Default | Meaning |
| --- | --- | --- |
| `watcher_buffer_size` | 256 | Maximum bounded event buffer per watcher. |
| `maximum_payload_size` | 1 MiB | Maximum accepted or emitted payload. |
| `maximum_outstanding_operations` | 128 | Bound on correlated task work. |
| `allow_insecure` | `False` | Explicit opt-in accepted only for loopback mock use. |

JSON payloads additionally enforce a fixed 64-level container nesting bound in
both directions. The bound is not configurable and matches the protobuf
group-depth bound on every supported Python version: deeper values are rejected
as unsupported values on encode and as malformed payloads on decode.

Profiles use `ntp_host` and `ntp_port`; automation may override them with
`ECN_NTP_HOST` and `ECN_NTP_PORT`. The clock API accepts no per-call host. Use an
alternate endpoint only when Picogrid provided it for the configured ECN.

Profiles may also contain a `reconnect_policy` object with the same six fields as
`ReconnectPolicy`. Supply only the fields you intend to override when running
`configure`; automation can override individual fields after profile loading:

| `configure` option | Environment variable | Policy field |
| --- | --- | --- |
| `--reconnect-initial-delay-seconds` | `ECN_RECONNECT_INITIAL_DELAY_SECONDS` | `initial_delay_seconds` |
| `--reconnect-multiplier` | `ECN_RECONNECT_MULTIPLIER` | `multiplier` |
| `--reconnect-maximum-delay-seconds` | `ECN_RECONNECT_MAXIMUM_DELAY_SECONDS` | `maximum_delay_seconds` |
| `--reconnect-stable-reset-seconds` | `ECN_RECONNECT_STABLE_RESET_SECONDS` | `stable_reset_seconds` |
| `--reconnect-maximum-attempts` | `ECN_RECONNECT_MAXIMUM_ATTEMPTS` | `maximum_attempts` |
| `--reconnect-maximum-elapsed-seconds` | `ECN_RECONNECT_MAXIMUM_ELAPSED_SECONDS` | `maximum_elapsed_seconds` |

Omitted values retain the `ReconnectPolicy` defaults. Environment values override
the corresponding stored profile values; blank environment values are ignored. All
values receive the same validation as direct configuration. The delay, reset, and
elapsed values are seconds; the attempt budget is a positive integer. The two
maximum budgets remain unlimited when omitted.

## `legion-system-auth` mapping

The `legion` provider follows the behavior pinned from
`picogrid/legion-system-auth@9f618b7ce1648789d816a49b8fd0ec0ab21ea24a`.
On every connect and reconnect, it rereads the current access-token record and the
non-secret integration configuration. The source `integrationId` becomes the MQTT
username and the current access token becomes the MQTT password. No other field is
used for broker authentication.

The provider rejects missing, malformed, expired, symlinked, non-regular files,
files with unsafe permissions, and unexpectedly large files. It does not select,
expose, or use refresh tokens or client secrets, call the Legion cloud API, refresh a
token, install software, run `sudo`, or modify a service. If local authentication is not configured,
follow the
[official setup instructions](https://github.com/picogrid/legion-system-auth/blob/9f618b7ce1648789d816a49b8fd0ec0ab21ea24a/README.md#1-initial-setup)
and run `legion-auth setup` yourself.

For a Picogrid-provided non-default storage reference, set
`ECN_LEGION_AUTH_STORAGE` to that same value and run
`legion-auth setup --storage-path "$ECN_LEGION_AUTH_STORAGE"`. The SDK never prints
the expanded path in diagnostics. `LEGION_AUTH_STORAGE_PATH` is retained as a
compatibility alias for an existing local `legion-system-auth` installation;
`ECN_LEGION_AUTH_STORAGE` takes precedence when both are set. The alias also works
without a named profile.

Certificate and private-key material may come from a path or in-memory `SecretStr`,
never both. Static bearer authentication requires a provided MQTT username for
non-loopback use and accepts exactly one of a `SecretStr`, a cooperative async
`TokenProvider`, or a cooperative async paired `CredentialsProvider`. Provider
callables are validated before invocation; callers that need blocking work must own
and bound it explicitly. The token is the MQTT CONNECT password. mTLS always requires
TLS.

`NoAuth` and `plaintext_container_network` form the only credential-free
configuration. TLS must be disabled explicitly; bearer, provider, mTLS certificate,
and key material are rejected. On each connect or reconnect, every resolved address
must be private. A mixed private/public answer raises `TransportBoundaryError`, and
the SDK dials the validated address literal rather than resolving the hostname again.

```python
import os

from pydantic import SecretStr

from picogrid_ecn_client import BearerTokenAuth, ECNConfig


config = ECNConfig(
    host=os.environ["ECN_HOST"],
    mqtt_port=int(os.environ["ECN_MQTT_PORT"]),
    integration_name=os.environ["ECN_INTEGRATION_NAME"],
    auth=BearerTokenAuth(
        username=os.environ["ECN_MQTT_USERNAME"],
        token=SecretStr(os.environ["ECN_BEARER_TOKEN"]),
    ),
)
```

See [authentication](../getting-started/authentication.md),
[security and credentials](../security/credentials.md), and the runnable
[preflight example](../../examples/preflight.py).
