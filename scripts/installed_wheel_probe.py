# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Probe an installed wheel from an isolated interpreter using only the stdlib."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_NATIVE_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
_TLS_READER_CANARY = b"installed-wheel-path-reader-smoke\n"


def _metadata_value(metadata: importlib.metadata.PackageMetadata, key: str) -> str | None:
    try:
        return metadata[key]
    except KeyError:
        return None


def _distribution_record(distribution: importlib.metadata.Distribution) -> dict[str, Any]:
    metadata = distribution.metadata
    classifiers = metadata.get_all("Classifier", failobj=[])
    license_classifiers = sorted(
        classifier for classifier in classifiers if classifier.startswith("License ::")
    )
    license_value = (
        _metadata_value(metadata, "License-Expression")
        or _metadata_value(metadata, "License")
        or "UNKNOWN"
    )
    if "\n" in license_value or len(license_value) > 160:
        license_value = "SEE DISTRIBUTION LICENSE FILE"
    files = distribution.files or ()
    native_files = sorted(
        str(file) for file in files if str(file).casefold().endswith(_NATIVE_SUFFIXES)
    )
    return {
        "has_native_binary": bool(native_files),
        "license": license_value.strip() or "UNKNOWN",
        "license_classifiers": license_classifiers,
        "name": _metadata_value(metadata, "Name") or "UNKNOWN",
        "native_binary_count": len(native_files),
        "requires": sorted(distribution.requires or ()),
        "version": distribution.version,
    }


def _resolved_sys_path() -> tuple[Path, ...]:
    resolved: list[Path] = []
    for item in sys.path:
        if not item:
            continue
        try:
            resolved.append(Path(item).resolve())
        except OSError:
            continue
    return tuple(resolved)


def _probe_path_backed_tls_reader() -> None:
    """Exercise the installed package's isolated path-material child reader."""

    from picogrid_ecn_client._transport.credentials import _read_tls_paths_in_subprocess

    with tempfile.TemporaryDirectory(prefix="picogrid-ecn-installed-tls-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        material = directory / "ca.pem"
        descriptor = os.open(
            material,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(_TLS_READER_CANARY)
        finally:
            os.close(descriptor)
        observed = asyncio.run(_read_tls_paths_in_subprocess({"ca": str(material)}))

    if observed != {"ca": _TLS_READER_CANARY}:
        raise RuntimeError("installed-wheel path-backed TLS reader failed")


def probe(
    repository_root: Path, expected_python_minor: tuple[int, int] = (3, 11)
) -> dict[str, Any]:
    """Import and inventory the exact isolated installation or fail closed."""

    if sys.version_info[:2] != expected_python_minor:
        expected = ".".join(str(part) for part in expected_python_minor)
        raise RuntimeError(
            f"installed-wheel gate requires Python {expected}, got {sys.version_info[:3]}"
        )
    if os.environ.get("PYTHONPATH"):
        raise RuntimeError("PYTHONPATH must be unset during installed-wheel verification")

    import picogrid_ecn_client

    module_file = Path(picogrid_ecn_client.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    repository_root = repository_root.resolve()
    paths = _resolved_sys_path()
    if repository_root in paths or any(repository_root in path.parents for path in paths):
        raise RuntimeError("repository path leaked into the isolated interpreter search path")
    if environment_root not in module_file.parents or "site-packages" not in module_file.parts:
        raise RuntimeError(
            f"package did not import from isolated site-packages: {module_file.name}"
        )
    if importlib.util.find_spec("picogrid_edge_sdk") is not None:
        raise RuntimeError("private edge SDK is present in the installed-wheel environment")
    if any(name.startswith("picogrid_edge_sdk") for name in sys.modules):
        raise RuntimeError("private edge SDK was imported")

    _probe_path_backed_tls_reader()

    project = importlib.metadata.distribution("picogrid-ecn-client")
    dependencies = sorted(
        (_distribution_record(distribution) for distribution in importlib.metadata.distributions()),
        key=lambda item: (str(item["name"]).casefold(), str(item["version"])),
    )
    return {
        "dependencies": dependencies,
        "import_origin": "isolated-environment-site-packages",
        "private_sdk_available": False,
        "private_sdk_imported": False,
        "project_name": project.metadata["Name"],
        "project_version": project.version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "repository_on_sys_path": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument(
        "--expected-python-minor",
        choices=("3.11", "3.12", "3.13", "3.14"),
        default="3.11",
    )
    arguments = parser.parse_args(argv)
    expected_python_minor = tuple(int(part) for part in arguments.expected_python_minor.split("."))
    if len(expected_python_minor) != 2:
        raise RuntimeError("expected Python minor must have two components")
    print(
        json.dumps(
            probe(
                arguments.repository_root,
                (expected_python_minor[0], expected_python_minor[1]),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
