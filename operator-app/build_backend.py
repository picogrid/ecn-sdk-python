# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Build the operator wheel with its locked browser application embedded.

Runtime installations must not need Node.js or a source checkout.  This PEP 517
backend stages only the files needed to build the Python package, compiles the
frontend from ``package-lock.json``, and places the generated files inside the
wheel before delegating to setuptools.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from setuptools import build_meta as _setuptools  # type: ignore[import-untyped]

_ROOT = Path(__file__).resolve().parent
_PREBUILT_FRONTEND = "PICOGRID_OPERATOR_PREBUILT_FRONTEND"
_MAX_PACKAGE_JSON_BYTES = 64 * 1024
_SOURCE_FILES = (
    "README.md",
    "THIRD_PARTY_LICENSES.md",
    "build_backend.py",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "tsconfig.json",
)
_IGNORED_TREE_NAMES = {
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
    "wheelhouse",
}


def _require_regular_source_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"operator source member is not a regular file: {path}")


def _copy_regular_source_file(source: str, destination: str) -> str:
    _require_regular_source_file(Path(source))
    return shutil.copy2(source, destination)


def _copy_source_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"operator source tree is not a regular directory: {source}")

    def ignored(directory: str, names: list[str]) -> set[str]:
        ignored_names = {
            name
            for name in names
            if name in _IGNORED_TREE_NAMES or name.endswith((".egg-info", ".pyc", ".pyo"))
        }
        for name in names:
            member = Path(directory) / name
            if member.is_symlink():
                raise RuntimeError(f"operator source member is not a regular file: {member}")
        return ignored_names

    shutil.copytree(
        source,
        destination,
        ignore=ignored,
        copy_function=_copy_regular_source_file,
    )


def _frontend_environment(root: Path) -> dict[str, str]:
    """Return a build-only environment that cannot leak runtime credentials."""

    environment: dict[str, str] = {
        "CI": "1",
        "HOME": str(root / ".home"),
        "PATH": os.environ.get("PATH", ""),
        "npm_config_audit": "false",
        "npm_config_cache": str(root / ".npm-cache"),
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    }
    for name in ("LANG", "LC_ALL", "SOURCE_DATE_EPOCH", "SYSTEMROOT", "TMP", "TEMP"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    (root / ".home").mkdir(mode=0o700)
    return environment


def _validate_frontend(directory: Path) -> None:
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("operator frontend build did not produce a regular directory")
    required = (directory / "index.html", directory / "assets")
    if not required[0].is_file() or not required[1].is_dir():
        raise RuntimeError("operator frontend build omitted its entry page or assets")
    for path in directory.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise RuntimeError("operator frontend build contains an unsupported member")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("operator package metadata contains a duplicate key")
        decoded[key] = value
    return decoded


def _reject_non_finite_json_value(_value: str) -> Any:
    raise ValueError("operator package metadata contains a non-finite value")


def _required_frontend_tool_versions(staged_root: Path) -> tuple[str, str]:
    package_path = staged_root / "package.json"
    try:
        with package_path.open("rb") as stream:
            encoded = stream.read(_MAX_PACKAGE_JSON_BYTES + 1)
        if len(encoded) > _MAX_PACKAGE_JSON_BYTES:
            raise ValueError("operator package metadata exceeds the size limit")
        package = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json_value,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("operator package metadata could not be read") from exc
    if not isinstance(package, dict) or not isinstance(package.get("engines"), dict):
        raise RuntimeError("operator package metadata must pin Node.js and npm")
    engines = package["engines"]
    node_version = engines.get("node")
    npm_version = engines.get("npm")
    exact_version = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
    if (
        not isinstance(node_version, str)
        or exact_version.fullmatch(node_version) is None
        or not isinstance(npm_version, str)
        or exact_version.fullmatch(npm_version) is None
        or package.get("packageManager") != f"npm@{npm_version}"
    ):
        raise RuntimeError("operator package metadata must pin exact Node.js and npm versions")
    return node_version, npm_version


def _build_frontend(staged_root: Path) -> Path:
    prebuilt = os.environ.get(_PREBUILT_FRONTEND)
    if prebuilt is not None:
        candidate = Path(prebuilt)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise RuntimeError("operator prebuilt frontend path is not an approved build input")
        frontend = candidate.resolve(strict=True)
        approved = {
            (_ROOT / "frontend" / "dist").resolve(),
            (_ROOT / "prebuilt-frontend").resolve(),
        }
        if frontend not in approved:
            raise RuntimeError("operator prebuilt frontend path is not an approved build input")
        _validate_frontend(frontend)
        return frontend

    environment = _frontend_environment(staged_root)
    if (
        shutil.which("node", path=environment["PATH"]) is None
        or shutil.which("npm", path=environment["PATH"]) is None
    ):
        raise RuntimeError("the pinned Node.js and npm toolchain is required to build the wheel")
    node_required, npm_required = _required_frontend_tool_versions(staged_root)
    try:
        node_result = subprocess.run(
            ["node", "--version"],
            cwd=staged_root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        npm_result = subprocess.run(
            ["npm", "--version"],
            cwd=staged_root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("the pinned Node.js and npm toolchain could not be validated") from exc
    if node_result.stdout.strip() != f"v{node_required}":
        raise RuntimeError(f"Node.js {node_required} is required to build the operator wheel")
    if npm_result.stdout.strip() != npm_required:
        raise RuntimeError(f"npm {npm_required} is required to build the operator wheel")
    subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=staged_root,
        env=environment,
        check=True,
    )
    subprocess.run(
        ["npm", "run", "build"],
        cwd=staged_root,
        env=environment,
        check=True,
    )
    frontend = staged_root / "frontend" / "dist"
    _validate_frontend(frontend)
    return frontend


def _stage_source(root: Path) -> None:
    for name in _SOURCE_FILES:
        source = _ROOT / name
        _require_regular_source_file(source)
        shutil.copy2(source, root / name)
    license_candidates = (_ROOT / "LICENSE", _ROOT.parent / "LICENSE")
    license_source = next(
        (
            candidate
            for candidate in license_candidates
            if candidate.is_file() and not candidate.is_symlink()
        ),
        None,
    )
    if license_source is None:
        raise RuntimeError("operator source is missing the repository license")
    shutil.copy2(license_source, root / "LICENSE")
    _copy_source_tree(_ROOT / "backend", root / "backend")
    _copy_source_tree(_ROOT / "frontend", root / "frontend")


@contextlib.contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def _wheel_source() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="picogrid-operator-build-") as raw:
        root = Path(raw)
        _stage_source(root)
        frontend = _build_frontend(root)
        packaged_frontend = root / "backend" / "operator_app" / "static"
        shutil.copytree(frontend, packaged_frontend)
        yield root


def build_wheel(
    wheel_directory: str,
    config_settings: Mapping[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    output = str(Path(wheel_directory).resolve())
    metadata = str(Path(metadata_directory).resolve()) if metadata_directory else None
    with _wheel_source() as source, _working_directory(source):
        return cast(str, _setuptools.build_wheel(output, config_settings, metadata))


def build_sdist(
    sdist_directory: str,
    config_settings: Mapping[str, Any] | None = None,
) -> str:
    output = str(Path(sdist_directory).resolve())
    with tempfile.TemporaryDirectory(prefix="picogrid-operator-sdist-") as raw:
        source = Path(raw)
        _stage_source(source)
        (source / "MANIFEST.in").write_text(
            "include LICENSE README.md THIRD_PARTY_LICENSES.md build_backend.py "
            "package-lock.json package.json pyproject.toml tsconfig.json\n"
            "recursive-include frontend *\n"
            "recursive-include backend/operator_app *.py\n",
            encoding="utf-8",
        )
        with _working_directory(source):
            return cast(str, _setuptools.build_sdist(output, config_settings))


def get_requires_for_build_wheel(
    config_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="picogrid-operator-requires-") as raw:
        source = Path(raw)
        _stage_source(source)
        with _working_directory(source):
            return cast(list[str], _setuptools.get_requires_for_build_wheel(config_settings))


def get_requires_for_build_sdist(
    config_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="picogrid-operator-requires-") as raw:
        source = Path(raw)
        _stage_source(source)
        with _working_directory(source):
            return cast(list[str], _setuptools.get_requires_for_build_sdist(config_settings))


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: Mapping[str, Any] | None = None,
) -> str:
    output = str(Path(metadata_directory).resolve())
    with tempfile.TemporaryDirectory(prefix="picogrid-operator-metadata-") as raw:
        source = Path(raw)
        _stage_source(source)
        with _working_directory(source):
            return cast(
                str,
                _setuptools.prepare_metadata_for_build_wheel(output, config_settings),
            )
