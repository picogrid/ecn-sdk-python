---
title: ECNs
tableOfContents: false
sidebar:
  order: 1
---

An Expeditionary C2 Node (ECN) is a deployable command-and-control node that
connects mission software and operational data at the edge. The Picogrid ECN SDK
connects an application to one ECN so it can exchange entities, locations, and tasks
that the application is authorized to access.

An **integration** is an application or service identified on ECN topics. A
**terminal** is the ECN endpoint identity used to address traffic across a configured
route. The SDK represents ECN messages as typed Python models; ECN infrastructure
remains responsible for the broker, routing, persistence, and administration.

## What the SDK receives from a deployment

From an SDK application's perspective, an ECN provides:

- an MQTT v5 endpoint and trust material;
- an integration identity used in topic segments;
- broker ACL decisions for each connection, subscription, and publication; and
- the supported entity, location, and exact local or terminal-addressed task paths.

Connecting does not discover integrations, entities, topics, commands, or granted
scopes. Start with issued configuration and scope every observer to the integrations
and categories it needs.

Continue with [MQTT topics and delivery](mqtt-wire.md), [ACLs](acls.md), and
[mesh routing](mesh-routing.md).
