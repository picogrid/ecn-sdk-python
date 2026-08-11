---
title: Authenticate to an ECN
tableOfContents: false
sidebar:
  order: 3
---

Authenticate a configured profile with mTLS client credentials, a bearer token, or
an existing local `legion-system-auth` session. A distinct credential-free mode is
available only for an operator-reviewed private container network. Choose the method
that matches the credential set or network boundary approved for the deployment;
these methods use different material and may use different ports.

:::note[Credentials]
Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact.
:::

## Choose an authentication method

| Profile | What you need | Default port |
|---|---|---|
| mTLS | CA certificate, client certificate, and client key | 8883 |
| Bearer | CA certificate, bearer token, and the MQTT username supplied with it | 8884 |
| Local `legion-system-auth` | An existing local session | 8884 |
| Reviewed container network | `NoAuth`, disabled TLS, and the operator-supplied network attestation | 1883 |

The SDK never derives a bearer profile's MQTT username from the integration name.
The reviewed-container-network row is not a general unauthenticated remote profile.
It is the only accepted credential-free combination: `NoAuth` and the attestation
are mutually required, and bearer, provider, certificate, and private-key material
are rejected.

## Create a named profile

Run the interactive configuration command:

```bash
picogrid-ecn configure
```

Use port **8883** for mTLS or **8884** for bearer or local `legion-system-auth`
authentication. Override that default only when Picogrid provided a different
endpoint. The command stores non-secret connection settings and credential-file
references in the platform/XDG configuration location with restrictive permissions.
It does not store a bearer token or change shell startup files.

## Verify the profile

Validate the saved values locally, then run read-only preflight:

```bash
picogrid-ecn doctor --profile NAME
picogrid-ecn preflight --profile NAME
```

Examples accept `--profile NAME`. Set `ECN_PROFILE` to select the same profile in CI
or another environment-driven launch.

## Use environment variables for automation

For mTLS, omit all bearer variables. Port 8883 is the supported default:

```bash
export ECN_HOST=authorized.example
export ECN_MQTT_PORT=8883
export ECN_INTEGRATION_NAME=example-client
export ECN_CA_CERT=/secure/path/ca.crt
export ECN_CLIENT_CERT=/secure/path/client.crt
export ECN_CLIENT_KEY=/secure/path/client.key
unset ECN_BEARER_TOKEN ECN_MQTT_USERNAME ECN_ALLOW_INSECURE
python examples/preflight.py
```

Bearer interoperability depends on the authentication profile issued for the target.
Review the current [compatibility status](../compatibility/limitations.md) before live
use.

For bearer authentication, omit all client-certificate variables. Port 8884 is the
supported default:

```bash
export ECN_HOST=authorized.example
export ECN_MQTT_PORT=8884
export ECN_INTEGRATION_NAME=example-client
export ECN_CA_CERT=/secure/path/ca.crt
export ECN_MQTT_USERNAME=provided-identity
unset ECN_CLIENT_CERT ECN_CLIENT_KEY ECN_CLIENT_KEY_PASSWORD ECN_ALLOW_INSECURE
read -r -s ECN_BEARER_TOKEN
export ECN_BEARER_TOKEN
python examples/preflight.py
```

For an operator-approved container network, omit every credential and CA variable,
select `ECN_AUTH=none`, explicitly disable TLS, and supply the reviewed network name:

```bash
export ECN_HOST=private-broker.example
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-client
export ECN_AUTH=none
export ECN_TLS_ENABLED=0
export ECN_PLAINTEXT_CONTAINER_NETWORK=reviewed-network-name
unset ECN_CA_CERT ECN_MQTT_USERNAME ECN_BEARER_TOKEN
unset ECN_CLIENT_CERT ECN_CLIENT_KEY ECN_CLIENT_KEY_PASSWORD
unset ECN_LEGION_AUTH_STORAGE LEGION_AUTH_STORAGE_PATH ECN_ALLOW_INSECURE
python examples/preflight.py
```

The network name is an operator attestation, not proof the SDK can derive from the
host. On each connect and reconnect, the SDK resolves the host, requires every
resolved address to be private, rejects mixed private/public answers, and dials the
validated private address literal.

The bearer token is never derived from the integration name. For an explicitly
configured static-token profile, use the MQTT username provided with that profile.

## Use an existing `legion-system-auth` session

The SDK can use a local `legion-system-auth` session without calling the Legion cloud
API or implementing token refresh. For each connect or reconnect, the provider reads
the current credential documents, selects the current access token, and derives the
MQTT username only from the source-confirmed `integrationId`. It never selects,
exposes, or uses a refresh token or client secret, installs a service,
invokes `sudo`, or changes service state.

If local authentication is not configured, follow the
[official `legion-auth` setup instructions](https://github.com/picogrid/legion-system-auth/blob/9f618b7ce1648789d816a49b8fd0ec0ab21ea24a/README.md#1-initial-setup),
then run:

```bash
legion-auth setup
```

Then select the existing local session without putting a token on the command line:

```bash
picogrid-ecn configure --profile NAME --host authorized.example \
  --integration-name example-client --auth legion --non-interactive
picogrid-ecn doctor --profile NAME
```

Setup is an operator action; the SDK never runs it for you. Missing, malformed,
expired, or unsafe local material fails closed. A broker rejection remains an
authentication failure. Neither path prints the token or its location. Detailed
source mapping is in the
[configuration reference](../reference/configuration.md).

If Picogrid supplied a non-default local-auth storage reference, use that same
reference for both the profile and setup. The fixed failure guidance uses
`legion-auth setup --storage-path "$ECN_LEGION_AUTH_STORAGE"` and never expands the
path into an error.

An encrypted mTLS key may also use `ECN_CLIENT_KEY_PASSWORD`.
Keep `ECN_TLS_VERIFY=1` for every live connection. `ECN_ALLOW_INSECURE=1` is accepted
only with an explicit loopback mock endpoint.

Never print credential objects, embed credentials in configuration files, disable
TLS verification for a remote host, or treat a successful connection as publish
permission. [Broker ACLs](../concepts/acls.md) remain the authorization boundary.
For handling guidance, read [Security and credentials](../security/credentials.md).

Next, [run the offline mock](mock-setup.md) to exercise SDK workflows locally. When
you are ready to validate a live profile, continue with
[read-only preflight](preflight.md).
