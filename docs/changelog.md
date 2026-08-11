---
title: Changelog
tableOfContents: false
sidebar:
  order: 1
---

Picogrid ECN SDK release notes are maintained in
[CHANGELOG.md](../CHANGELOG.md).

## Before upgrading

1. review changes to the public API, wire profile, dependency set, and supported
   Python versions;
2. obtain the SDK wheel through your approved distribution channel and verify its
   provenance;
3. run your integration against the offline mock with the new wheel;
4. rerun read-only preflight in each authorized environment; and
5. review [Compatibility and limitations](compatibility/limitations.md) for any
   externally unverified behavior.

The package follows Semantic Versioning for its documented public Python API and
confirmed MQTT wire behavior. Authentication, ACL, and routing compatibility remain
specific to each ECN deployment.
