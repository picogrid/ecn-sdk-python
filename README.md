<div align="center">

<picture>
  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" media="(prefers-color-scheme: light)">
  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-dark.png" media="(prefers-color-scheme: dark)">
  <img src="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" alt="Picogrid" width="576">
</picture>

<h1>ECN SDK</h1>

[ECN](https://picogrid.com/ecn) · [Documentation](https://docs.picogrid.com/ecn-sdk/) · [Examples](https://github.com/picogrid/ecn-sdk-python/tree/main/examples) · [Security](https://github.com/picogrid/ecn-sdk-python/security/policy) · [Support](https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md) · [License](https://github.com/picogrid/ecn-sdk-python/blob/main/LICENSE)

</div>

The **Picogrid ECN SDK** is the Python interface to a Picogrid Expeditionary C2
Node (ECN). An ECN is a deployable edge node that connects sensors, applications,
operator tools, data, and tasking.

Use it to observe ECN data, publish typed sensor observations, receive and respond to
tasks, dispatch supported tasks, and build applications that work with capabilities at
the edge. The SDK gives Picogrid users, operators, partners, and developers one typed,
async interface for these workflows.

Live use requires network access to an ECN and, except for an explicitly configured
credential-free mode, deployment-issued credentials. The included mock needs neither.

## What you can build

- **Sensor integrations** that publish detections, tracks, entities, locations, and
  Position Location Information (PLI).
- **Effector integrations** that receive typed tasks, acknowledge work when required,
  and return results or failures.
- **Operator tools and displays** that observe live ECN data and dispatch supported
  tasks.
- **Applications for autonomy and C2 integrations** that use typed entity, location,
  and task services while the deployment provides routing and authorization.

Start with an [ECN SDK quickstart](https://docs.picogrid.com/ecn-sdk/quickstarts/observe-data/)
or use the local mock to exercise the SDK without an ECN.

## Install

The Python distribution is named `picogrid-ecn-client` and supports Python 3.11,
3.12, 3.13, and 3.14. Install the public PyPI release in a virtual environment:

```console
python -m venv .venv-ecn-sdk
. .venv-ecn-sdk/bin/activate
python -m pip install picogrid-ecn-client==0.1.0 # x-release-please-version
python -c "import picogrid_ecn_client; print(picogrid_ecn_client.__version__)"
```

See [Installation](https://docs.picogrid.com/ecn-sdk/getting-started/installation/)
for verified local wheel, offline wheelhouse, and platform guidance.

## Connect to an ECN

A live connection requires a reachable ECN, an assigned integration name, appropriate
credentials, and the ECN CA and TLS configuration where applicable. The SDK supports
bearer-token and mTLS authentication. Configure a named profile, check the connection,
and run read-only preflight diagnostics:

```console
picogrid-ecn configure --profile NAME
picogrid-ecn doctor --profile NAME
picogrid-ecn preflight --profile NAME
```

`preflight` publishes no application data. Once it succeeds, open a narrow watcher:

```console
export ECN_OBSERVED_INTEGRATION=authorized-source
export ECN_MAX_EVENTS=10
python examples/watch_tracks.py --profile NAME
```

Some deployments provide an advanced credential-free mode for applications on a
reviewed local container network. Use that mode only when it is part of your deployment
configuration. See [Authentication](https://docs.picogrid.com/ecn-sdk/getting-started/authentication/)
for bearer-token, mTLS, and deployment-specific options.

## Start with a workflow

| Workflow | Start here | For |
| --- | --- | --- |
| Observe tracks, detections, or locations | [Observe ECN data](https://docs.picogrid.com/ecn-sdk/quickstarts/observe-data/) | Operators and application developers |
| Connect a sensor | [Build a sensor publisher](https://docs.picogrid.com/ecn-sdk/quickstarts/sensor-publisher/) | Sensor integration teams |
| Connect an effector | [Build an effector task handler](https://docs.picogrid.com/ecn-sdk/quickstarts/effector-handler/) | Effector integration teams |
| Run an operator display | [Run the operator view](https://docs.picogrid.com/ecn-sdk/quickstarts/operator-view/) | Operators and application developers |

The optional browser operator application is distributed separately as
`picogrid-ecn-operator-app`; the core CLI loads it only when
`picogrid-ecn operator` is invoked.

## Try it offline

The included mock lets you exercise the SDK locally without an ECN. Start it in one
shell:

```console
python -m picogrid_ecn_client.testing.mock_ecn --mqtt-port 1883
```

In another shell, configure loopback access and run preflight:

```console
export ECN_HOST=127.0.0.1
export ECN_MQTT_PORT=1883
export ECN_INTEGRATION_NAME=example-integration
export ECN_BEARER_TOKEN=mock-full-access
export ECN_ALLOW_INSECURE=1
python examples/preflight.py
```

Continue with [Mock ECN setup](https://docs.picogrid.com/ecn-sdk/getting-started/mock-setup/)
for a complete publish-and-observe round trip. Successful mock behavior does not by
itself establish compatibility with a live ECN.

## Programming model

Create an `ECNClient`, use typed services such as `entities`, `locations`, and `tasks`,
and consume bounded async streams. Close each stream when finished; the async client
context manager closes the connection and remaining resources.

```python
from picogrid_ecn_client import DeliveryPolicy, ECNClient, EntityCategory


async def observe_tracks(config) -> None:
    async with ECNClient(config) as client:
        stream = await client.entities.watch(
            integrations={"authorized-source"},
            categories={EntityCategory.TRACK},
            delivery=DeliveryPolicy.LATEST,
        )
        try:
            async for event in stream:
                print(event.entity.id, event.entity.type)
        finally:
            await stream.aclose()
```

Opening a watcher waits for the ECN to accept its subscription. Closing the final
consumer releases that subscription.

## Supported capabilities

| Workflow | API | What it does |
| --- | --- | --- |
| Check a connection | `await client.preflight()` | Reports connection and authentication diagnostics without publishing application data |
| Observe entities | `client.entities.watch(...)` | Streams typed entities, detections, or tracks from the bounded sources requested by the application |
| Publish an entity | `client.entities.publish(...)` | Publishes one validated entity, detection, or track |
| Observe locations | `client.locations.watch(...)`, `client.locations.last_observed(...)`, `client.locations.wait_for_update(...)` | Streams locations and exposes state observed by this client |
| Publish a location or PLI | `client.locations.publish(...)` | Publishes one validated location for an entity |
| Receive tasks | `client.tasks.register(...)` | Registers a typed handler for an exact local task request |
| Dispatch tasks | `client.tasks.send(...)` | Sends a supported task locally or to a caller-supplied terminal through deployment-provided routing |
| Decode protobuf | `decode_entity_event_protobuf(...)`, `decode_entity_location_protobuf(...)` | Decodes supported payloads locally without network access |

Available data and task delivery depend on deployment routing and access controls.
When broker credentials and ECN routing authorize it, bounded watchers can observe
local and mesh-routed data from configured ECN data families. The SDK does not
discover terminals or create routes. Cross-terminal compatibility remains externally
unverified.

## Security and behavior

- ECN connections use MQTT v5. Non-loopback connections use verified TLS by default.
- Authentication and broker access controls determine which data and tasks an
  application may use.
- Constructing or starting a client creates no entity, location, or task subscription.
  Watchers subscribe only to the exact or bounded data families requested.
- Entity IDs are canonical UUIDs. Location reads expose state observed by the current
  client rather than an authoritative server-side query.
- Deployment configuration owns task allowlists for integrations, commands, and
  targets; broker ACLs authorize MQTT operations; ECN infrastructure controls
  terminal routing and fan-out.
- The public SDK exposes no broker administration, route creation, command discovery,
  unrestricted wildcard subscription, or raw-topic API.

Read [Compatibility and limitations](https://docs.picogrid.com/ecn-sdk/compatibility/limitations/)
and [Security](https://docs.picogrid.com/ecn-sdk/concepts/security/) before using a new
integration with a live ECN.

## Documentation

- **Get started:** [Installation](https://docs.picogrid.com/ecn-sdk/getting-started/installation/), [Configuration](https://docs.picogrid.com/ecn-sdk/getting-started/configuration/), and [Authentication](https://docs.picogrid.com/ecn-sdk/getting-started/authentication/)
- **Understand the model:** [ECNs](https://docs.picogrid.com/ecn-sdk/concepts/ecns/), [Entities](https://docs.picogrid.com/ecn-sdk/concepts/entities/), [Locations](https://docs.picogrid.com/ecn-sdk/concepts/locations/), and [Tasks](https://docs.picogrid.com/ecn-sdk/concepts/tasks/)
- **Build integrations:** [Sensors](https://docs.picogrid.com/ecn-sdk/integrations/sensors/), [Effectors](https://docs.picogrid.com/ecn-sdk/integrations/effectors/), and [Operator workflows](https://docs.picogrid.com/ecn-sdk/operator/workflows/)
- **Use the API:** [API reference](https://docs.picogrid.com/ecn-sdk/reference/api/)
- **Operate and troubleshoot:** [Deployment and support](https://docs.picogrid.com/ecn-sdk/deployment-support/) and [Troubleshooting](https://docs.picogrid.com/ecn-sdk/how-to/troubleshooting/)
- **Track changes:** [Changelog](https://docs.picogrid.com/ecn-sdk/changelog/)

The [deployed documentation](https://docs.picogrid.com/ecn-sdk/) is the canonical
guide. Source distributions also include the guide for offline use.

Report vulnerabilities through the repository
[security policy](https://github.com/picogrid/ecn-sdk-python/security/policy). For
product help, see [SUPPORT.md](https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md).

## License

The Picogrid ECN SDK is licensed under the
[Mozilla Public License 2.0](https://github.com/picogrid/ecn-sdk-python/blob/main/LICENSE).
Picogrid names and marks are not licensed as trademarks..
