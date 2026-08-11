---
title: Broker ACLs
tableOfContents: false
sidebar:
  order: 8
---

Broker access-control lists (ACLs) are the authorization boundary for every SDK
operation. The SDK validates inputs and constructs bounded topic filters, but cannot
grant or infer server permissions.

## Interpret broker results narrowly

A successful MQTT connection proves only that the broker accepted that connection.
A successful SUBACK applies only to the requested filter. A successful QoS 1 PUBACK
applies only to that publication. None of these results implies access to another
topic, persistence, or downstream processing.

`preflight()` therefore reports publish authorization as unknown. To learn whether a
publication is allowed, an authorized caller must deliberately perform that exact
publication with synthetic or otherwise approved data.

Keep allowlists in deployment configuration, use the narrowest watcher filters, and
handle negative CONNACK, SUBACK, PUBACK, and UNSUBACK results as typed failures.
See [Security and credentials](../security/credentials.md).
