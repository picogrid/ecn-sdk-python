# Contributing

Thank you for improving the Picogrid ECN SDK. This guide covers reporting bugs and
requesting changes, local setup, and the checks used to validate changes.

External pull-request intake is closed. It will remain closed unless and until
Picogrid records counsel approval of the contributor workflow. Do not submit an
external pull request while this notice remains in place. Report bugs and request
changes through GitHub issues; the terms below document the workflow that would
apply only after intake opens and do not constitute acceptance of submissions.

The supported contract is described by [VERSIONING.md](VERSIONING.md),
[SECURITY.md](SECURITY.md), the generated [Python API reference](docs/reference/python/index.md),
and its machine-readable [API manifest](scripts/public-api-manifest.json).

## Bugs, change requests, and security reports

Open a GitHub issue for a bug or change request only when it can be described
with synthetic data. Follow [SUPPORT.md](SUPPORT.md) for the information to
include and the data that must be removed before posting.

Suspected security issues must never be filed as public issues. Report them privately
by following [VULNERABILITY_REPORTING.md](VULNERABILITY_REPORTING.md).

## Safety while developing

- Use only ECNs and credentials you are authorized to access, and only for the
  exact target and action that authorization covers.
- Prefer the bundled offline `MockECN` for deterministic development and tests.
- Do not run production-impacting tests without approval for the exact target
  and operation.
- Never commit credentials, operational endpoints, or traffic captures.

## Development setup

The project uses Python 3.11 for development and supports Python 3.11 through 3.14.
Install `uv`, then create the locked environment:

```console
uv sync --frozen
```

Documentation and example applications use Node.js 24 and the committed npm
lockfile. Install and check the documentation workspace with:

```console
make docs-install
make docs-check
npm --prefix docs run docs:test:browser:install
npm --prefix docs run docs:test:browser
```

Preview the documentation with:

```console
npm --prefix docs run docs:dev
```

The preview accepts local hosts by default. To use a tunnel, allow its host:

```console
DOCS_DEV_ALLOWED_HOSTS=<host> npm --prefix docs run docs:dev
```

## Contributor checks

Run focused tests while iterating, for example:

```console
uv run pytest path/to/test_file.py
```

Before requesting review, run the generated-content, dependency, license, and typing checks:

```console
make generate-reference
make check-deps
make check-license
make check-reference
make verify-types
```

Use the complete release gate for the final candidate:

```console
make verify-release
```

Run the release gate from a clean Git worktree. It fails before building when tracked
or untracked changes prevent the candidate from being attributed to one commit.

The release gate removes `node_modules`, `.astro`, and `site-dist` while reproducing
the release environment. Run `make docs-install` again before another documentation
check or preview.

## Generated Python API reference

Files under [docs/reference/python/](docs/reference/python/index.md) come from
[scripts/public-api-manifest.json](scripts/public-api-manifest.json). Never edit them by hand.

```console
make generate-reference
```

A supported-public-surface change must update the manifest in the same change and
receive maintainer review. `make check-reference` detects stale or incomplete pages.

## Dependency and lock consistency

When a dependency declaration changes, synchronize every committed lock and
policy pin:

```console
make sync-deps
make check-deps
```

Review all generated changes before submitting them.

Direct dependency declarations must be markerless PEP 508 requirements. The check
compares declarations textually with `uv`-generated locks, while `uv` rewrites markers
during compilation. Marker-qualified declarations are therefore rejected.

This fail-closed rule also covers editable requirements, bare local paths, and
direct-reference URLs. Add and review checker support before using a new form.

## Operator application changes

From `operator-app/`, run the focused backend and browser checks:

```console
ruff check backend
ruff format --check backend
mypy backend/operator_app
pytest backend/tests
npm run build
npm run test:e2e
```

Operator screenshots use synthetic UUIDs and deterministic fixtures, but rendered
pixels can vary by platform. Regenerate them with the pinned browser by running
`npm run screenshot:generate` in `operator-app/`.

## Change expectations

- Keep changes focused and reviewable.
- Add regression tests for behavior changes.
- Update guide pages and runnable examples in the same change as API changes.
- Keep [CHANGELOG.md](CHANGELOG.md) user-facing. Use Conventional Commit
  prefixes such as `fix:`, `feat:`, `docs:`, or `chore:` so release automation
  can classify changes.
- Handle compatibility changes with the care described in
  [VERSIONING.md](VERSIONING.md).
- Do not weaken release checks to accommodate stale generated output. Fix the
  source and regenerate the candidate instead.

A proposed change should record what changed, the focused checks that were run, and
any compatibility uncertainty that remains.

## Inbound licensing and sign-off

### Intake status

External pull requests are not currently accepted. Opening intake requires a
separately recorded counsel approval; publication of this policy, the pull-request
template, or the automated sign-off check is not that approval. Picogrid may close
an external pull request submitted while intake is closed without reviewing it.

Before external intake opens, public repository branch protection must require
`.github/workflows/dco.yml` from trusted public `main` for pull-request and
merge-group head SHAs. It must also require the license-policy checker to be loaded
and executed from trusted public `main` against the proposed tree for pull-request
heads and merge-group heads if merge queue is enabled. There must be no general
bypass for either requirement. Ruleset configuration and hosted proof are pending;
the required hosted evidence must show that these results attach to the stated head
SHAs. The presence of repository automation does not claim either one.

If intake opens, every contribution must be submitted under the unmodified Mozilla
Public License 2.0 (`MPL-2.0`), matching the project's outbound license. By
submitting, you agree that your contribution may be used, modified, and distributed
under those terms.

### Developer Certificate of Origin 1.1

This DCO requirement applies only to contributions submitted to the public
distribution repository, `picogrid/ecn-sdk-python`, after intake opens. It applies
equally to external contributors and Picogrid staff. The private engineering
upstream retains byte-identical copies of this policy and the DCO workflow only as
authoritative inputs to the one-way public export; changes made only in that private
upstream are not public-repository contributions and do not require DCO sign-off.

If intake opens, an inbound contribution proposal submitted through a public pull
request is never merged directly. Maintainers reproduce an accepted proposal in the
private engineering upstream, qualify the resulting change there, and export a new
public candidate through the one-way release process. Qualified public
export-candidate pull requests created by maintainers are the normal public promotion
path and may be merged after the required gates pass. The private engineering
upstream remains the source of truth.

Before private reproduction, maintainers record the accepted proposal head SHA and
the DCO result for that head. A later head change requires a DCO rerun and an updated
export mapping before the proposal can be accepted or reproduced.

Every commit, including a merge commit created by a contributor, must carry exactly
one valid `Signed-off-by: Name <email>` trailer. The trailer certifies the
authoritative [Developer Certificate of Origin 1.1](https://developercertificate.org/)
(DCO 1.1); read that certificate before signing. In summary, it requires an
appropriate right to submit the contribution, whether you created it, received it
under a compatible license, or received it unmodified from someone who certified it,
and acknowledges that the contribution and sign-off record are public. This summary
does not replace or modify the DCO 1.1.

The sign-off name and email must match the commit author or committer identity after
NFC normalization, lowercase conversion, and collapsing ASCII whitespace. Non-ASCII
whitespace remains identity-significant. A duplicate, conflicting, malformed, or
non-matching trailer does not satisfy the DCO check. Use an identity you are
authorized to make public, and do not add addresses, account details, or other
personal information beyond what the commit and sign-off require.

Create a signed-off commit with:

```bash
git commit -s
```

To repair your latest commit, set the correct Git author or committer identity if
needed, then edit the commit message:

```bash
git commit --amend
```

Delete every malformed, duplicate, conflicting, or non-matching sign-off-like line
from the message, then leave exactly one valid matching
`Signed-off-by: Name <email>` trailer.

To repair several commits while preserving merge topology, start a merge-preserving
interactive rebase:

```bash
git rebase -i --rebase-merges <base>
```

In the todo list, change `pick` to `edit` for every affected non-merge commit. For
an affected contributor-created merge, preserve its `merge` line and add a `break`
line immediately after it. At every resulting edit or break stop, edit that commit
message as above, leave exactly one valid matching trailer, and continue:

```bash
git commit --amend
git rebase --continue
```

Rewriting published commits changes their IDs; coordinate with collaborators before
updating a shared branch.

There are no bot or actor exemptions. The deterministic checker rejects GitHub
`[bot]` author and committer identities, and the hosted workflow rejects pull
requests whose pull request author is a bot. Commit metadata cannot establish whether
a human-shaped identity belongs to a person or to automation, so the checker does
not guess from other names or email addresses. Maintainers enforce that provenance
boundary during review. Work originating from Dependabot, release-please, or other
automation cannot merge as the automation-owned branch or pull request. A human
contributor with the necessary rights must substantively review the proposed change,
recreate it on a separate human-authored branch, sign off every commit, and open a
separate pull request that preserves and discloses the upstream bot, source, license,
and generation or automation provenance. Maintainers then close the automation-owned
branch or pull request.

### Rights, provenance, and responsible submission

Before submitting, you must:

- confirm that you have authority to contribute the work, including any approval
  required by an employer, client, contract, or other obligation;
- identify all third-party code, generated content, datasets, samples, and other
  incorporated material; provide its source and license; preserve required notices;
  and confirm that its terms permit distribution under the proposed contribution;
- record the generator, input source, applicable licenses, and material
  transformations for generated work and data rather than presenting it as wholly
  original;
- take full responsibility for AI-assisted work. Review it substantively for
  correctness, security, licensing, provenance, and compatibility; disclose the
  assistance and relevant source provenance; and do not rely on an AI tool's
  assertion that material is safe to submit;
- disclose known patent claims, trademark material, additional license terms, export
  controls, confidentiality duties, or other restrictions that may affect use or
  redistribution;
- exclude credentials, private keys, personal or customer data, confidential
  information, non-public operational details, sensitive traffic or telemetry, and
  any other material you are not authorized to publish;
- minimize personal information in the pull request, commits, tests, fixtures,
  screenshots, and logs. Use synthetic data whenever possible; and
- comply with the
  [GitHub Community Guidelines](https://docs.github.com/en/site-policy/github-terms/github-community-guidelines)
  in all project interactions.

Suspected vulnerabilities and sensitive security evidence must not be included in a
pull request or public issue. Follow
[VULNERABILITY_REPORTING.md](VULNERABILITY_REPORTING.md) for private reporting.

### Review and enforcement

Maintainers perform substantive review of the change, its rights, provenance,
security, compatibility, and fit with the SDK's supported surface. Passing an
automated DCO check is necessary but does not establish ownership, license
compatibility, safety, correctness, or acceptance.

Raise any uncertainty about authority, licensing, provenance, patents, trademarks,
other restrictions, privacy, or sensitive content with maintainers before
submission; route security-sensitive questions privately as described above.

The project does not routinely require a contributor license agreement (CLA) or
copyright assignment, and the applicable contributor or rightsholder retains
ownership. Before intake opens, counsel must confirm this contributor workflow.
After intake opens, only exceptional contributions involving ambiguous ownership,
large code donations, or unusual legal restrictions are escalated to counsel before
submission. Counsel may then require a separate contribution agreement or software
grant; otherwise the standard `MPL-2.0` and DCO terms apply. Maintainers may request
evidence, removal, or clean human recreation of material, escalate the question for
legal or security review, or reject the contribution. Submission never guarantees
review or acceptance.
