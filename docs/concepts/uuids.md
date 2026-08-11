---
title: UUID identity
tableOfContents: false
sidebar:
  order: 10
---

Every public [entity](entities.md) identity is a canonical hyphenated UUID. The same
UUID follows an entity across JSON and protobuf messages, embedded or dedicated
locations, Position Location Information (PLI), and exact task topics.

## Assign durable identities

Generate an identity once for a durable sensor, effector, or platform and persist it
in the integration's own configuration. Do not derive a UUID from a display name or
reuse one for a different real-world object.

```python
from uuid import UUID

entity_id = UUID("00000000-0000-4000-8000-000000000001")
```

JSON entity topics include the UUID. Protobuf entity topics carry it in the payload.
Both JSON and protobuf location topics include it. Topic and payload identities must
agree whenever both are present; disagreement is rejected.
