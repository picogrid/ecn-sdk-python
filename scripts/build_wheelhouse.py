# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0
"""Build a hash-pinned offline wheelhouse from the verified release artifacts.

The wheelhouse bundles the exact verified project wheel from ``dist/`` together
with its locked runtime dependencies so that an offline or DDIL host can install
the SDK with ``--no-index --find-links ./wheelhouse --require-hashes`` and no
package-index reachability. The project wheel is never rebuilt or downloaded:
the single wheel already present in ``dist/`` is copied in and pinned by its
SHA-256 hash, so the offline installation consumes the same bytes the release
verifier approved.

Dependencies are resolved from ``uv.lock`` through ``uv export``, which emits a
hash-pinned requirements file, and are downloaded as wheels only. By default the
download targets the build host's platform and interpreter. Set
``WHEELHOUSE_PLATFORM`` (for example ``manylinux2014_x86_64`` or
``manylinux2014_aarch64``) to assemble a wheelhouse for a different target
platform. Set ``WHEELHOUSE_PYTHON`` (for example ``3.12`` or ``3.13``) to
build a wheelhouse for a different target Python interpreter version. Each
wheelhouse is specific to one platform and one interpreter version.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_NAME = "picogrid-ecn-client"
IMPORT_NAME = "picogrid_ecn_client"
REPOSITORY = Path(__file__).resolve().parent.parent
WHEELHOUSE = REPOSITORY / "wheelhouse"
REQUIREMENTS = WHEELHOUSE / "requirements.txt"


class WheelhouseError(RuntimeError):
    """Raised when the wheelhouse cannot be assembled from verified inputs."""


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=REPOSITORY)


def _verified_wheel() -> Path:
    wheels = sorted((REPOSITORY / "dist").glob(f"{IMPORT_NAME}-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise WheelhouseError(
            "expected exactly one verified wheel in dist/; run 'make verify-release'"
            f" first (found {len(wheels)})"
        )
    return wheels[0]


def main() -> None:
    wheel = _verified_wheel()
    version = wheel.name.removeprefix(f"{IMPORT_NAME}-").removesuffix("-py3-none-any.whl")

    # Use a clean staging directory to avoid orphan wheels from prior runs
    with tempfile.TemporaryDirectory(dir=REPOSITORY, prefix=".wheelhouse-stage-") as staging_dir:
        staging = Path(staging_dir)
        requirements = staging / "requirements.txt"

        _run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ]
        )

        download = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--dest",
            str(staging),
            "-r",
            str(requirements),
        ]
        platform = os.environ.get("WHEELHOUSE_PLATFORM")
        if platform:
            download.extend(["--platform", platform])
        python_version = os.environ.get("WHEELHOUSE_PYTHON")
        if python_version:
            download.extend(["--python-version", python_version])
            major, minor = python_version.split(".")[:2]
            download.extend(["--implementation", "cp"])
            download.extend(["--abi", f"cp{major}{minor}"])
        _run(download)

        shutil.copy2(wheel, staging / wheel.name)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        with requirements.open("a", encoding="utf-8") as handle:
            handle.write(f"{PACKAGE_NAME}=={version} \\\n    --hash=sha256:{digest}\n")

        # Atomically replace the wheelhouse with the complete staging directory
        if WHEELHOUSE.exists():
            shutil.rmtree(WHEELHOUSE)
        shutil.move(str(staging), str(WHEELHOUSE))

    wheel_count = len(list(WHEELHOUSE.glob("*.whl"))) - 1
    print(
        f"wheelhouse assembled: {wheel.name} plus {wheel_count} dependency wheels in"
        f" {WHEELHOUSE.relative_to(REPOSITORY)}"
    )


if __name__ == "__main__":
    main()
