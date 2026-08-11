---
title: Decode protobuf
tableOfContents: false
sidebar:
  hidden: true
---

Use the installed SDK decoder functions rather than generated classes. The
[runnable decoder example](../../examples/decode_public_protobuf.py) accepts a
payload file, integration, category, and bounded maximum payload size.

```bash
python examples/decode_public_protobuf.py --check
export ECN_PROTOBUF_PAYLOAD_FILE=/path/to/public-payload.bin
export ECN_INTEGRATION_NAME=authorized-source
export ECN_ENTITY_CATEGORY=detection
export ECN_MAXIMUM_PAYLOAD_SIZE=1048576
python examples/decode_public_protobuf.py
```

`decode_entity_event_protobuf` needs the topic-derived integration and category.
`decode_entity_location_protobuf` needs the topic-derived integration and entity
UUID. Unknown fields are ignored and future category numbers become `OTHER`. Strict
topic agreement remains: a future payload category on a known topic is rejected,
and `OTHER` passes only when the topic category is also unknown.

Entity category is derived from the protobuf topic; publication omits its optional
numeric field. The descriptor field remains available for inbound compatibility. See
[wire formats](../reference/wire-formats.md#protobuf).
