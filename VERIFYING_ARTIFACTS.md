# Verify release artifacts

Verify a release before installing it. A complete GitHub release contains exactly one
client wheel, one operator wheel, one client source distribution, `checksums.sha256`,
Sigstore bundles, a client-runtime SBOM, an operator Python-runtime SBOM, an operator
embedded-frontend SBOM, provenance, artifact inspection, vulnerability results, and
the sanitized verification summary.

## Check exact bytes

Download every release asset into a new directory. On a system with GNU coreutils:

```console
(
  set -eu
  set -- ./picogrid_ecn_client-*.whl
  test "$#" -eq 1 || exit 1
  client_wheel=$1
  test -f "$client_wheel" || exit 1
  set -- ./picogrid_ecn_operator_app-*.whl
  test "$#" -eq 1 || exit 1
  operator_wheel=$1
  test -f "$operator_wheel" || exit 1
  set -- ./picogrid_ecn_client-*.tar.gz
  test "$#" -eq 1 || exit 1
  client_sdist=$1
  test -f "$client_sdist" || exit 1
  expected_distributions=$(printf '%s\n' "$client_wheel" "$operator_wheel" "$client_sdist" | LC_ALL=C sort)
  actual_distributions=$(find . -type f \( -name '*.whl' -o -name '*.tar.gz' \) -print | LC_ALL=C sort)
  test "$actual_distributions" = "$expected_distributions" || exit 1
  checksum_subjects=$(awk '{print "./" $2}' checksums.sha256 | LC_ALL=C sort)
  test "$checksum_subjects" = "$expected_distributions" || exit 1
  sha256sum --check checksums.sha256
)
```

On macOS, use:

```console
(
  set -eu
  set -- ./picogrid_ecn_client-*.whl
  test "$#" -eq 1 || exit 1
  client_wheel=$1
  test -f "$client_wheel" || exit 1
  set -- ./picogrid_ecn_operator_app-*.whl
  test "$#" -eq 1 || exit 1
  operator_wheel=$1
  test -f "$operator_wheel" || exit 1
  set -- ./picogrid_ecn_client-*.tar.gz
  test "$#" -eq 1 || exit 1
  client_sdist=$1
  test -f "$client_sdist" || exit 1
  expected_distributions=$(printf '%s\n' "$client_wheel" "$operator_wheel" "$client_sdist" | LC_ALL=C sort)
  actual_distributions=$(find . -type f \( -name '*.whl' -o -name '*.tar.gz' \) -print | LC_ALL=C sort)
  test "$actual_distributions" = "$expected_distributions" || exit 1
  checksum_subjects=$(awk '{print "./" $2}' checksums.sha256 | LC_ALL=C sort)
  test "$checksum_subjects" = "$expected_distributions" || exit 1
  shasum -a 256 --check checksums.sha256
)
```

The checksum file covers both wheels and the client source distribution. A missing,
additional, or mismatched distribution file is a failed verification.

## Verify GitHub provenance

With an authenticated GitHub CLI whose account may read this repository, verify each
distribution against it:

```console
gh attestation verify picogrid_ecn_client-0.1.0-py3-none-any.whl --repo picogrid/ecn-sdk-python # x-release-please-version
gh attestation verify picogrid_ecn_operator_app-0.1.0-py3-none-any.whl --repo picogrid/ecn-sdk-python # x-release-please-version
gh attestation verify picogrid_ecn_client-0.1.0.tar.gz --repo picogrid/ecn-sdk-python # x-release-please-version
```

The attestation subject digest must equal the checksum already verified. The local
`provenance.json` must name the same artifacts, a clean Git commit, and a successful
byte-for-byte rebuild. Treat local provenance as supporting evidence, not a substitute
for the GitHub attestation. If the intended recipient cannot read the attestation's
repository, the release owner must establish an approved recipient-verifiable
attestation path before distribution; lack of repository access is not a reason to
skip provenance verification.

## Verify signatures and contents

Each distribution has a same-named `.sigstore.json` bundle. Use a current Sigstore
verifier and require the repository release workflow identity; do not accept an
arbitrary certificate identity. The exact verifier invocation, certificate identity,
and issuer constraints have not yet been exercised against a bundle from a protected
unpublished draft, so this document intentionally does not invent a command. Before
the first publication, record and test that exact recipient-side command against all
three draft distributions and require it to reject an incorrect workflow identity. Until
that test passes, signature verification is a publication blocker and the presence of
a signing step or bundle is not sufficient evidence.

Inspect `artifact-inspection.json`, `verification-summary.json`, `sbom.cdx.json`,
`operator-sbom.cdx.json`, and `operator-frontend-sbom.cdx.json` before installation.
The operator wheel has two SBOM attestations because its Python package embeds the
compiled browser application. Both wheels should be pure Python. The client must not
contain an unlisted transport, private SDK, credential, operational address, or
arbitrary MQTT topic API; the operator wheel must match its exact member allowlist and
embedded frontend report.

After every check passes, install the exact wheel with normal dependency resolution:

```console
python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl # x-release-please-version
python -m pip install ./picogrid_ecn_operator_app-0.1.0-py3-none-any.whl # x-release-please-version
picogrid-ecn operator --demo
```

Artifact verification proves origin and tested contents. It does not grant broker
authorization or prove compatibility with an untested ECN.
