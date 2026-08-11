---
title: Run read-only preflight
sidebar:
  order: 5
---

Use read-only preflight to verify that a configured profile can reach an ECN and
authenticate before you start an integration workflow. It checks connection
readiness without publishing application data.

## Run preflight

Check a saved profile locally, then run read-only connection diagnostics:

```bash
picogrid-ecn doctor --profile NAME
picogrid-ecn preflight --profile NAME
```

`doctor` validates local configuration and credential references without connecting.
`preflight` checks configuration, DNS, TCP, TLS, and the authenticated ECN
connection. Because it performs zero application-level publishes, the report marks
publish authorization as `unknown`. This is not a successful authorization result:
preflight cannot prove publish permission.

The runnable example accepts the same profile name or `ECN_PROFILE`:

```bash
python examples/preflight.py --check
python examples/preflight.py --profile NAME
```

`--check` is fully offline. For local development without a profile, start the
[loopback mock](mock-setup.md#run-the-mock-broker) alongside.

**Shell A — mock broker (leave running):**

```bash
picogrid-mock-ecn --mqtt-port 1883
```

**Shell B — preflight:**

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1
python examples/preflight.py
```

## Probe one exact subscription

No subscription is installed by default. A caller may add an explicitly requested
`SubscriptionProbe` with an exact integration and category or entity ID. A probe
subscribes, waits for SUBACK, unsubscribes, and disconnects.

```python
from picogrid_ecn_client import (
    ECNClient,
    EntityCategory,
    SubscriptionProbe,
    SubscriptionProbeKind,
)


async def probe_one_track_family(client: ECNClient) -> None:
    report = await client.preflight(
        subscription_probes=(
            SubscriptionProbe(
                kind=SubscriptionProbeKind.ENTITY,
                integration="authorized-source",
                category=EntityCategory.TRACK,
            ),
        )
    )
    print(report.model_dump())
```

## Authorization scope

Request a probe only for an approved integration and category or entity ID. A
passing probe applies only to that subscription and does not authorize publication.
See [Security and credentials](../security/credentials.md).

After preflight connects and authenticates, continue with an
[observer quickstart](../quickstarts/observe-data.md) or
[sensor publisher quickstart](../quickstarts/sensor-publisher.md).
