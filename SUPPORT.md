# Picogrid ECN SDK support

The Picogrid ECN SDK is an alpha release candidate. No supported public package
version has been published. Before an approved release exists, help for repository
candidates is best-effort and does not establish a compatibility or service
commitment.

For an approved release, support covers its documented public API, confirmed MQTT v5
topic families, loopback mock, and source-distributed examples. It does not include
broker administration, credential provisioning, authoritative state lookup,
platform command discovery, or private runtime behavior.

The SDK is provided without warranty, as stated in sections 6 and 7 of the Mozilla
Public License 2.0. The support described here is not a warranty or guaranteed
remedy.

## Asking for help

Open a repository issue only when the report can be reproduced with synthetic data
and contains no credential, endpoint, operational identifier, customer detail, or raw
traffic. Include:

- package and Python versions;
- operating system;
- the public method or example involved;
- a minimal synthetic reproducer;
- expected and observed behavior; and
- whether the result came from the loopback mock or an explicitly authorized target.

Do not post secrets even if they appear expired. Remove local paths and connection
details from tracebacks before sharing them.

Security reports must use the private process in
[VULNERABILITY_REPORTING.md](VULNERABILITY_REPORTING.md). The project does not promise
an individual response time or service-level agreement. Supported-version and
deprecation policies are defined in [VERSIONING.md](VERSIONING.md).
