# Picogrid ECN SDK versioning and compatibility

The human-facing product name is **Picogrid ECN SDK**. Its Python distribution is
`picogrid-ecn-client` and its import package is `picogrid_ecn_client`. A repository
name, documentation host, or operator-application name is not a substitute for those
public package identifiers.

The optional operator distribution is `picogrid-ecn-operator-app`. It uses the same
version as the client candidate it requires, is verified and retained as a separate
wheel, and does not add browser-server dependencies to the core wheel. The existing
PyPI publication identity covers only `picogrid-ecn-client`; publishing the operator
distribution requires a separately approved project and trusted-publisher binding.

The Python distribution uses Semantic Versioning. The package version in project
metadata and the public `__version__` value agree.

No public package or supported version has been released yet.
`0.1.0` identifies the current release candidate only. <!-- x-release-please-version -->

## What counts as compatibility

- After `1.0.0`, a breaking change to a public import, model, method signature,
  exception, lifecycle guarantee, or supported wire interpretation requires a major
  version.
- A backward-compatible public capability or optional model field requires a minor
  version.
- A compatible bug, security, documentation, or build correction requires a patch
  version.

During the `0.x` alpha series, a minor release may contain a necessary breaking public
API change, but the changelog and migration guidance must identify it explicitly. A
patch release must remain backward compatible. Confirmed MQTT topic shapes and
protobuf field numbers are interoperability contracts; changing them requires an
explicit protocol-compatibility review regardless of package version.

Package-version compatibility does not prove compatibility with an ECN deployment.
Authentication, broker ACLs, routing, and enabled topic families must be verified for
each target. Results from the loopback mock or one authorized environment do not prove
behavior in another environment. Production compatibility is currently unverified.

## Python versions

The candidate line supports Python 3.11, 3.12, 3.13, and 3.14. The package metadata and
release notes identify the supported versions for each release. Dropping a Python
minor requires an announced compatibility change and a version bump appropriate to
user impact.

After the first release, only the latest released line is actively maintained during
alpha development unless a release note explicitly states otherwise. Vulnerability
fixes may be issued for an older line when maintainers determine that a safe backport
is practical.

## Deprecation policy

No public API is currently deprecated. A future deprecation must be identified in
the public API documentation and `CHANGELOG.md`, include a supported replacement and
migration guidance, and emit `DeprecationWarning` when there is an executable Python
call site where a warning is useful.

During the `0.x` alpha series, a deprecated API may be removed in the next minor
release, but the release notes must call out the break explicitly. After `1.0.0`, a
deprecated API normally remains available for at least one subsequent minor release
before removal. A security flaw, unsafe behavior, or incorrect wire implementation
may require faster removal; that exception requires a security or protocol review and
an explicit changelog entry.

MQTT topic shapes, protobuf field numbers, and payload identity rules are
interoperability contracts rather than ordinary convenience APIs. They are never
silently deprecated or repurposed. Any change follows the protocol-compatibility and
versioning rules above.
