---
title: Exceptions reference
tableOfContents: false
sidebar:
  order: 3
---

Every SDK operational failure derives from `ECNClientError`. Exceptions expose a
stable `code`, optional operation label, and redacted details. Messages deliberately
omit raw transport errors, credentials, and payload inputs.

| Exception | Meaning |
| --- | --- |
| `ConfigurationError` | Configuration could not be used at runtime. |
| `AuthenticationError` | MQTT authentication failed. |
| [`AuthorizationError`](python/exceptions/authorization-error.md) | A broker ACL or operation-specific negative acknowledgment, including SUBACK, PUBACK, or UNSUBACK, denied the exact operation. |
| `ConnectionError` | DNS, TCP, TLS, MQTT, or reconnect failure. |
| [`DeliveryError`](python/exceptions/delivery-error.md) | A mutation failed at a delivery boundary. The error carries the strongest safe delivery phase and, when applicable, a task or operation identifier; it does not imply that retry is safe. |
| [`OutcomeUnknownError`](python/exceptions/outcome-unknown-error.md) | A mutation may have reached the broker or downstream handler, but completion is unknown. The client does not replay it automatically. |
| `TransportBoundaryError` | A reviewed-container-network endpoint resolved outside its required private-address boundary. |
| `ProtocolError` | Received MQTT or payload violates the supported wire. |
| `ValidationError` | Caller input violates an SDK model or operation contract. |
| `TimeoutError` | A bounded operation exceeded its deadline. |
| `NotReadyError` | An operation requires a ready, open client. |
| `ResourceLimitError` | A payload, buffer, task, or outstanding-work bound was reached. |
| `ClockError` | The configured ECN clock endpoint could not be measured. |
| `ClockProtocolError` | An NTP response or local timing measurement was malformed, mismatched, unsynchronized, or otherwise unusable. |
| `ClockToleranceError` | A valid report's absolute offset plus conservative local timing uncertainty exceeded the caller's tolerance; the report remains available on the exception. |

Catch narrowly when recovery differs, or catch the base type at an application
boundary:

```python
from picogrid_ecn_client import ECNClient, ECNClientError


async def run(client: ECNClient) -> None:
    try:
        await client.start()
    except ECNClientError as exc:
        print(exc.code, exc.operation)
```

Do not log authentication objects or add secret values to a replacement exception.
Use [troubleshooting](../how-to/troubleshooting.md) with the runnable
[preflight example](../../examples/preflight.py).
