---
title: Install the SDK
tableOfContents: false
sidebar:
  order: 1
---

The Picogrid ECN SDK supports Python 3.11 through 3.14. Review the [license and notices](../reference/licensing.md) before modifying or redistributing the SDK.

If you do not already have a supported interpreter, install Python 3.14 via pyenv, uv, or your preferred toolchain. The default `python3` on some systems may be a newer version that the SDK does not yet support. Invoke the Python version explicitly in all commands below.

## Install from PyPI

The recommended stable installation path uses the public PyPI release:

```bash
python3.14 -m venv .venv-ecn-sdk       # or python3.11 / python3.12 / python3.13
. .venv-ecn-sdk/bin/activate
python -m pip install picogrid-ecn-client==0.1.0  # x-release-please-version
python -c "import picogrid_ecn_client; print(picogrid_ecn_client.__version__)"
```

Pip resolves the declared runtime dependencies through your configured package index.

## Install from a local wheel file

If you have a distributed wheel file in the current directory, verify its SHA-256 digest against the published release checksums and confirm provenance before installation. See [VERIFYING_ARTIFACTS.md](../../VERIFYING_ARTIFACTS.md) for the complete verification procedure.

```bash
python3.14 -m venv .venv-ecn-sdk       # or python3.11 / python3.12 / python3.13
. .venv-ecn-sdk/bin/activate
python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl  # x-release-please-version
python -c "import picogrid_ecn_client; print(picogrid_ecn_client.__version__)"
```

If building from a source checkout, build the wheel first:

```bash
python -m pip install build
python -m build --wheel --outdir .     # writes ./picogrid_ecn_client-*.whl
```

## Offline installation from a verified wheelhouse

For fully offline or air-gapped installations with hash verification, prepare a wheelhouse with pinned, hashed dependencies.

### Build the wheelhouse

Use the project's `make wheelhouse` target to build a hash-pinned wheelhouse. The target interpreter must match the Python version the offline host will use; the example below matches the Python 3.14 environment created in the next section:

```bash
WHEELHOUSE_PYTHON=3.14 make wheelhouse  # creates ./wheelhouse with requirements.txt
```

The target requires the verified wheel in `dist/` produced by `make verify-release`; it copies that exact wheel into the wheelhouse rather than rebuilding or downloading it, and it exports the locked runtime dependencies with their hashes from `uv.lock` into `wheelhouse/requirements.txt`. Dependency wheels are downloaded for the build host's platform by default; set `WHEELHOUSE_PLATFORM` (for example `manylinux2014_x86_64` or `manylinux2014_aarch64`) to assemble a wheelhouse for a different target platform. Each wheelhouse targets one platform and one interpreter version from the supported range (3.11 through 3.14); build a separate wheelhouse for each Python version offline recipients use.

### Install from the wheelhouse

The `--require-hashes` installation verifies every wheel in the bundle against `wheelhouse/requirements.txt`, but that file itself is not authenticated, and the published `checksums.sha256` covers only the release wheel and source distribution, not a wheelhouse as a whole. If you received a prebuilt wheelhouse rather than building it yourself, first verify the SDK wheel inside it against the published release checksums and provenance (see [VERIFYING_ARTIFACTS.md](../../VERIFYING_ARTIFACTS.md) for the complete procedure), and then verify the dependency pins: regenerate the requirements from the verified source distribution's committed lock file with `uv export --frozen --no-dev --no-emit-project` and confirm the prebuilt `wheelhouse/requirements.txt` matches it, so every dependency hash traces back to the release evidence rather than to the wheelhouse author.

Once the wheelhouse is prepared and verified, install without internet access:

```bash
python3.14 -m venv .venv-ecn-sdk
. .venv-ecn-sdk/bin/activate
python -m pip install --no-index --find-links ./wheelhouse --require-hashes -r wheelhouse/requirements.txt
python -c "import picogrid_ecn_client; print(picogrid_ecn_client.__version__)"
```

## Install the operator application

The browser operator application is a separate wheel with local-server dependencies.
Choose either the client-only install above or the two-wheel operator install below;
the two-wheel command replaces the client-only install step. Use both retained wheel
artifacts from the same inspected release. If you are building from source instead,
build both wheels from the same source commit: run the client build above, then use
Node.js 24.19.0 with its bundled npm 11.17.0 to build the matching operator wheel:

```bash
python -m build --wheel --outdir . operator-app
```

Install the resulting pair together:

<!-- x-release-please-start-version -->
```bash
python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl ./picogrid_ecn_operator_app-0.1.0-py3-none-any.whl # x-release-please-version
picogrid-ecn operator --demo
```
<!-- x-release-please-end -->

Open `http://127.0.0.1:8080`. The operator wheel already contains its compiled
frontend. Runtime users do not copy repository source, install Node.js or npm
dependencies, or start a separate frontend server. For live read-only operation,
configure a named profile and explicit observation allowlists, then use
`picogrid-ecn operator --profile NAME`. The operator wheel also installs the direct
`picogrid-ecn-operator` entry point. Tasking remains disabled unless its separate
closed policy, exact target allowlist, enable flag, and per-task confirmation are all
present.

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact. The installed
operator artifact is covered by offline release checks; staging and production
operator behavior remain unverified.

## Verify, upgrade, and uninstall

### Verify the installation

Check which version is installed:

```bash
python -m pip show picogrid-ecn-client
# Or query the package metadata directly:
python -c "import importlib.metadata; print(importlib.metadata.version('picogrid-ecn-client'))"
```

### Upgrade to a newer version

Upgrade the package to a specific version while preserving the virtual environment:

```bash
python -m pip install --upgrade picogrid-ecn-client==0.1.0  # x-release-please-version
```

### Uninstall the SDK

Remove the package from the virtual environment:

```bash
python -m pip uninstall picogrid-ecn-client
```

## Validate the runnable examples

If you have the matching SDK source bundle, validate its runnable examples without credentials or a network connection:

```bash
python examples/preflight.py --check
python examples/watch_tracks.py --check
python examples/get_ecn_location.py --check
```

Each example imports the installed package. Next, [configure a connection](configuration.md); the authentication page that follows will help you apply the credentials provided for your ECN (Expeditionary C2 Node).
