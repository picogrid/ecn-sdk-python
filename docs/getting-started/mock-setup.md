---
title: Develop with the offline mock
tableOfContents: false
sidebar:
  order: 4
---

Use `MockECN`, a fixed-purpose MQTT v5 simulator, to exercise SDK workflows in
deterministic local development. It rejects non-loopback bind hosts and supports only
the packets and ACL behavior needed by SDK workflows.

## Run the mock broker

The installed wheel ships a `picogrid-mock-ecn` console entry point.

**Shell A — mock broker (leave running):**

```bash
picogrid-mock-ecn --mqtt-port 1883
```

The development/CI-only `--allow-external-bind` flag permits a non-loopback listener,
and `--allow-unauthenticated` permits credential-free MQTT CONNECT packets. Both
default off. Use neither for the ordinary loopback workflow, and never deploy this
mock as infrastructure. The equivalent `MockECN` constructor arguments are
`allow_external_bind=False` and `allow_unauthenticated=False`.

Stop the mock, watchers, handlers, and operator app with `Ctrl+C` when done.

**Shell B — point the runnable examples at the loopback broker:**

Activate the same virtual environment, then export the mock connection block with a
synthetic full-access token:

```bash
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1
python examples/preflight.py
```

`ECN_ALLOW_INSECURE=1` opts into unverified plaintext and is accepted only against
the loopback mock. The [quickstarts](../quickstarts/observe-data.md) reuse this same
environment block; supply the quickstart-specific variables on top of it.

## Embed MockECN in tests

`MockECN` can also be used directly from Python when both the mock broker and the
client should live inside a single process, for example in unit tests:

```python
import asyncio

from picogrid_ecn_client import ECNClient
from picogrid_ecn_client.testing import MockECN


async def main() -> None:
    async with MockECN() as mock:
        async with ECNClient(mock.client_config("example-client")) as client:
            print(client.status.model_dump())


asyncio.run(main())
```

Use it for CONNECT/SUBSCRIBE/PUBLISH flows, typed payloads, bounded faults, and
cleanup. Mock permissions, persistence, and routing are not evidence of deployed ECN
behavior.

The runnable [preflight example](../../examples/preflight.py) also provides a
network-free `--check` path. Next, run [read-only preflight](preflight.md) against
your configured profile. After it connects and authenticates, continue with an
[observer quickstart](../quickstarts/observe-data.md) or
[sensor publisher quickstart](../quickstarts/sensor-publisher.md).
