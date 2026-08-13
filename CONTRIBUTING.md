# Contributing

Thank you for improving the Picogrid ECN SDK. This guide covers reporting bugs and
requesting changes, local setup, and the checks used to validate changes.

While the SDK is in its `0.1` alpha series, external pull requests are not being
accepted. Report bugs and request changes through GitHub issues; the development
and licensing sections below describe how changes are validated and the terms that
will govern contributions when external intake opens.

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

## Inbound licensing and review

When external contribution intake opens, contributions will be accepted under
MPL-2.0 only; inbound terms match the project's outbound license. Contributors must
have the right to submit their work and must not introduce third-party code without
clear provenance.

Maintainers review every contribution and may decline changes that do not fit the
SDK's narrow supported surface. Submission does not guarantee acceptance.
