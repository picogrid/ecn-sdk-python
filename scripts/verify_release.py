# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""One-command verification for the exact public wheel and source distribution."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from scripts import installed_examples
from scripts._release_stages import Stage, configure_resources, emit, resource, run_stages
from scripts.release_checks import (
    IMPORT_NAME,
    PACKAGE_NAME,
    ArtifactInspection,
    ArtifactPolicyError,
    DocumentationInspection,
    _string_value,
    compare_rebuilt_artifacts,
    documented_operator_install_argv,
    inspect_documentation,
    inspect_example_manifest,
    inspect_operator_wheel,
    inspect_sdist,
    inspect_wheel,
    load_documentation_base_path,
    load_policy,
    scan_secret_and_address_content,
    sha256_file,
    validate_public_brand_assets,
)
from scripts.version_sync import VersionSyncError, check_version_sync

REPOSITORY = Path(__file__).resolve().parents[1]
_PUBLIC_EXPORT_TOOLING_PRESENT = all(
    (REPOSITORY / "scripts" / name).is_file()
    for name in ("public_export.py", "public-export-manifest.json")
)
POLICY_PATH = REPOSITORY / "scripts" / "release-policy.json"
REPORT_DIRECTORY = REPOSITORY / "reports" / "generated"
DIST_DIRECTORY = REPOSITORY / "dist"
PROJECT_EGG_INFO_DIRECTORY = REPOSITORY / "src" / f"{IMPORT_NAME}.egg-info"
DEFAULT_SOURCE_DATE_EPOCH = 1_735_689_600
EXAMPLE_SUPPORT_FILES = ("__init__.py", "_common.py")
EXAMPLE_MANIFEST_FILE = installed_examples.MANIFEST_NAME
OPERATOR_PACKAGE_NAME = "picogrid-ecn-operator-app"
OPERATOR_NODE_PACKAGE_NAME = "operator-app"
PYTHON_313_SHUTDOWN_TEST = (
    "tests/unit/test_mock_ecn.py::test_delayed_publication_does_not_block_mock_shutdown"
)
_EXPECTED_IGNORED_ROOTS = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
        "site-dist",
    }
)
_UNCONDITIONAL_RISK_MARKERS = (
    "internal-schema",
    "internal_schema",
    "operational-capture",
    "operational_capture",
    "prod-validation",
    "prod_validation",
    "validation-capture",
    "validation_capture",
)
_ASSISTANT_ARTIFACT_NAMES = frozenset(
    {
        ".claude",
        ".codex",
        ".cursor",
        "agents.md",
        "claude.md",
        "codex.md",
        "copilot-instructions.md",
        "gemini.md",
    }
)
_EXPECTED_INTERNAL_ASSISTANT_ARTIFACT_PATHS = frozenset({Path("AGENTS.md")})
_ARCHIVE_SUFFIXES = (".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".zip")
_GENERATED_REPORT_FILES = frozenset(
    {
        "artifact-inspection.json",
        "checksums.sha256",
        "coverage.json",
        "dependencies.json",
        "dependency-licenses.json",
        "pyright-verifytypes.json",
        "documentation-inspection.json",
        "operator-dependencies.json",
        "operator-frontend-sbom.cdx.json",
        "operator-inspection.json",
        "operator-npm-audit.json",
        "operator-sbom.cdx.json",
        "operator-vulnerability-scan.json",
        "provenance.json",
        "reproducibility.json",
        "sbom.cdx.json",
        "site-inspection.json",
        "site-npm-audit.json",
        "verification-summary.json",
        "type-completeness.json",
        "vulnerability-scan.json",
    }
)
_SENSITIVE_IGNORED_BASENAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "secrets.json",
    }
)
_SENSITIVE_NAME_WORD = re.compile(r"(?:^|[._-])(?:credentials?|secrets?|tokens?)(?:[._-]|$)")
_VIRTUALENV_SECRET_TOKEN_MODULE = re.compile(
    r"(?:secrets?|tokens?)(?:\.cpython-[0-9]+)?\.(?:py|pyc|pyi)"
)
_RETIRED_REPOSITORY_MARKERS = (
    b"aio" + b"http",
    b"api_base" + b"_url",
    b"any" + b"httpurl",
    b"http" + b".client",
    b"http_" + b"port",
    b"http" + b"transport",
    b"mqttv" + b"31",
    b"protocol-" + b"manifest.json",
    b"protocol_" + b"manifest",
    b"platform-" + b"location indicator",
    b"platform " + b"location indicator",
    b"rest" + b"transport",
    b"urllib" + b".request",
)
_RETIRED_ENDPOINT_PATH = re.compile(rb"/v(?:1|3)(?=$|[/\s\"'`?#),.;:<>])", re.IGNORECASE)
_QUALITY_TOOL_CACHE_DIRECTORIES = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
)
_QUALITY_TOOL_CACHE_FILE = ".coverage"
_GENERATED_CONTENT_SCAN_ROOTS = frozenset(_QUALITY_TOOL_CACHE_DIRECTORIES)
_RETIRED_GENERATED_CONTENT_MARKERS = (
    b"aio" + b"http",
    b"_transport/" + b"rest.py",
    b"_transport." + b"rest",
    b"models/" + b"search.py",
    b"mqttv" + b"311",
    b"protocol-" + b"manifest.json",
    b"protocol_" + b"manifest",
    b"rest" + b"transport",
    b"test_transport_" + b"rest.py",
    b"/v" + b"1/",
    b"/v" + b"3/",
)


class VerificationError(RuntimeError):
    """Raised when a release verification stage fails."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    capture: bool = False,
    timeout: int = 300,
) -> str:
    printable = " ".join(command)
    emit(f"\n$ {printable}")
    try:
        result = subprocess.run(  # noqa: UP022
            command,
            cwd=cwd,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            emit(exc.stdout)
        if exc.stderr:
            emit(exc.stderr)
        raise VerificationError(
            f"command failed with exit code {exc.returncode}: {printable}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            emit(
                exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
            )
        if exc.stderr:
            emit(
                exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            )
        raise VerificationError(f"command timed out after {timeout}s: {printable}") from exc
    if not capture:
        if result.stdout:
            emit(result.stdout)
        if result.stderr:
            emit(result.stderr)
    return result.stdout if capture else ""


def _base_environment(source_date_epoch: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PICOGRID_OPERATOR_PREBUILT_FRONTEND", None)
    environment.update(
        {
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "TZ": "UTC",
        }
    )
    return environment


def _git_paths(arguments: Sequence[str], environment: Mapping[str, str]) -> tuple[Path, ...]:
    output = _run(
        ["git", "ls-files", "-z", *arguments],
        cwd=REPOSITORY,
        environment=environment,
        capture=True,
    )
    paths: list[Path] = []
    for raw_path in output.split("\0"):
        if not raw_path:
            continue
        posix_path = PurePosixPath(raw_path)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise VerificationError("git returned an unsafe worktree path")
        paths.append(Path(*posix_path.parts))
    return tuple(paths)


def _verification_input_digest(environment: Mapping[str, str]) -> str:
    """Hash every Git-visible file used by the release without exposing its contents."""

    paths = _git_paths(("--cached", "--others", "--exclude-standard"), environment)
    repository = REPOSITORY.resolve()
    digest = hashlib.sha256()
    seen: set[Path] = set()
    for relative in sorted(paths, key=lambda path: path.as_posix()):
        if relative.is_absolute() or ".." in relative.parts:
            raise VerificationError("verified release input inventory contains an unsafe path")
        if relative in seen:
            raise VerificationError("verified release input inventory contains a duplicate path")
        seen.add(relative)
        candidate = REPOSITORY / relative
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve() != repository / relative
        ):
            raise VerificationError("verified release input is missing or is not a regular file")
        encoded_path = relative.as_posix().encode("utf-8")
        data = candidate.read_bytes()
        mode = candidate.stat().st_mode & 0o777
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(mode.to_bytes(2, "big"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    digest.update(len(seen).to_bytes(8, "big"))
    return digest.hexdigest()


def _require_verification_inputs_unchanged(
    expected_digest: str,
    environment: Mapping[str, str],
) -> None:
    if _verification_input_digest(environment) != expected_digest:
        raise VerificationError("verified release inputs changed during verification")


def _is_expected_ignored_path(path: Path) -> bool:
    parts = path.parts
    if not parts:
        return False
    if parts[0] in _EXPECTED_IGNORED_ROOTS:
        return True
    if "__pycache__" in parts:
        return True
    if parts[0] == "reports" and len(parts) > 1 and parts[1] == "generated":
        return True
    if parts[0] == "src" and len(parts) > 1 and parts[1].casefold().endswith(".egg-info"):
        return True
    return path.name == ".coverage"


def _is_stale_project_output(path: Path) -> bool:
    parts = path.parts
    if not parts:
        return False
    if parts[0] == "build":
        return True
    if "__pycache__" in parts and parts[0] != ".venv":
        return True
    return parts[0] == "src" and len(parts) > 1 and parts[1].casefold().endswith(".egg-info")


def _is_allowed_virtualenv_ca_bundle(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if (
        len(parts) < 6
        or parts[:2] != (".venv", "lib")
        or re.fullmatch(r"python3\.[0-9]+", parts[2]) is None
        or parts[3] != "site-packages"
    ):
        return False
    return parts[4:] in {
        ("certifi", "cacert.pem"),
        ("pip", "_vendor", "certifi", "cacert.pem"),
    }


def _ignored_path_categories(
    path: Path, policy: dict[str, Any], *, check_credentials: bool = True
) -> set[str]:
    lowered = path.as_posix().casefold()
    basename = path.name.casefold()
    categories: set[str] = set()
    if any(marker in lowered for marker in _UNCONDITIONAL_RISK_MARKERS):
        categories.add("validation-or-internal-capture-name")
    if path not in _EXPECTED_INTERNAL_ASSISTANT_ARTIFACT_PATHS and any(
        part.casefold() in _ASSISTANT_ARTIFACT_NAMES for part in path.parts
    ):
        categories.add("assistant-artifact-name")
    if _is_stale_project_output(path):
        categories.add("stale-project-output")
    if lowered.endswith(_ARCHIVE_SUFFIXES) and not _is_expected_ignored_path(path):
        categories.add("unexpected-archive")
    if not check_credentials:
        return categories

    inside_virtual_environment = bool(path.parts) and path.parts[0] == ".venv"
    allowed_virtualenv_ca_bundle = _is_allowed_virtualenv_ca_bundle(path)
    known_virtualenv_code_module = (
        inside_virtual_environment
        and _VIRTUALENV_SECRET_TOKEN_MODULE.fullmatch(basename) is not None
    )
    sensitive_suffixes = tuple(
        suffix.casefold()
        for suffix in (
            *_string_policy_list(policy, "forbidden_path_suffixes"),
            ".cer",
            ".jks",
            ".p12",
            ".pfx",
        )
    )
    if (
        basename in _SENSITIVE_IGNORED_BASENAMES
        or basename.startswith(".env.")
        or (_SENSITIVE_NAME_WORD.search(basename) is not None and not known_virtualenv_code_module)
        or (basename.endswith(sensitive_suffixes) and not allowed_virtualenv_ca_bundle)
    ):
        categories.add("credential-like-name")
    if basename.endswith((".har", ".pcap", ".pcapng", ".tfstate")):
        categories.add("operational-capture-name")
    return categories


def _string_policy_list(policy: dict[str, Any], key: str) -> tuple[str, ...]:
    value = policy.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VerificationError(f"release policy key {key!r} must be a string list")
    return tuple(value)


def _is_generated_content_scan_path(path: Path) -> bool:
    parts = path.parts
    if not parts:
        return False
    if parts[0] in _GENERATED_CONTENT_SCAN_ROOTS:
        return True
    if parts[0] == "reports" and len(parts) > 1 and parts[1] == "generated":
        return True
    return path.name == ".coverage"


def _is_generated_report_path(path: Path) -> bool:
    parts = path.parts
    return parts[:2] == ("reports", "generated")


def _scan_ignored_generated_content(
    repository: Path, ignored_paths: Sequence[Path], policy: dict[str, Any]
) -> int:
    scanned = 0
    for relative in ignored_paths:
        if not _is_generated_content_scan_path(relative):
            continue
        candidate = repository / relative
        if candidate.is_symlink():
            raise VerificationError("ignored generated output is a symbolic link")
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise VerificationError("ignored generated output contains an unsupported entry")
        data = candidate.read_bytes()
        if any(marker in data.lower() for marker in _RETIRED_GENERATED_CONTENT_MARKERS):
            raise VerificationError("ignored generated output failed retired protocol marker scan")
        try:
            scan_secret_and_address_content(
                relative.as_posix(),
                data,
                policy,
                allow_synthetic_hosts=not _is_generated_report_path(relative),
            )
        except ArtifactPolicyError as exc:
            category = str(exc).split(" found in ", maxsplit=1)[0]
            output = (
                "generated report" if _is_generated_report_path(relative) else "generated output"
            )
            raise VerificationError(f"ignored {output} failed {category} scan") from None
        scanned += 1
    return scanned


def _scan_worktree_paths(
    repository: Path,
    visible_paths: Sequence[Path],
    ignored_paths: Sequence[Path],
    policy: dict[str, Any],
) -> dict[str, int]:
    name_findings: dict[str, int] = {}
    for path in visible_paths:
        for category in _ignored_path_categories(path, policy, check_credentials=False):
            name_findings[category] = name_findings.get(category, 0) + 1
    for path in ignored_paths:
        if not _is_expected_ignored_path(path):
            name_findings["unexpected-ignored-file"] = (
                name_findings.get("unexpected-ignored-file", 0) + 1
            )
        for category in _ignored_path_categories(path, policy):
            name_findings[category] = name_findings.get(category, 0) + 1
    if name_findings:
        counts = ", ".join(
            f"{category}={count}" for category, count in sorted(name_findings.items())
        )
        raise VerificationError(f"suspicious repository filenames found ({counts})")

    ignored_generated_scanned = _scan_ignored_generated_content(repository, ignored_paths, policy)

    scanned = 0
    for relative in visible_paths:
        candidate = repository / relative
        if candidate.is_symlink():
            raise VerificationError("Git-visible publication input is a symbolic link")
        if not candidate.exists() or not candidate.is_file():
            continue
        data = candidate.read_bytes()
        lowered = data.lower()
        if any(marker in lowered for marker in _RETIRED_REPOSITORY_MARKERS) or (
            _RETIRED_ENDPOINT_PATH.search(data) is not None
        ):
            raise VerificationError(
                "Git-visible publication input failed retired protocol marker scan"
            )
        try:
            allowed_exact_urls = (
                frozenset(_string_policy_list(policy, "generated_site_placeholder_urls"))
                if relative == Path("scripts/release-policy.json")
                else frozenset()
            )
            scan_secret_and_address_content(
                relative.as_posix(),
                data,
                policy,
                allow_synthetic_hosts=True,
                allowed_exact_urls=allowed_exact_urls,
            )
        except ArtifactPolicyError as exc:
            message = str(exc)
            category = message.split(" found in ", maxsplit=1)[0]
            raise VerificationError(
                f"Git-visible publication input failed {category} scan"
            ) from exc
        scanned += 1
    return {
        "git_visible_files_scanned": scanned,
        "ignored_generated_files_scanned": ignored_generated_scanned,
        "ignored_files_reviewed": len(ignored_paths),
    }


def _scan_git_visible_worktree(
    policy: dict[str, Any], environment: Mapping[str, str]
) -> dict[str, int]:
    visible = _git_paths(("--cached", "--others", "--exclude-standard"), environment)
    ignored = _git_paths(("--others", "--ignored", "--exclude-standard"), environment)
    return _scan_worktree_paths(REPOSITORY, visible, ignored, policy)


def _reset_generated_reports(policy: dict[str, Any]) -> None:
    repository = REPOSITORY.resolve()
    reports_parent = REPOSITORY / "reports"
    expected_directory = reports_parent / "generated"
    if REPORT_DIRECTORY.absolute() != expected_directory.absolute():
        raise VerificationError("refusing to clear an unexpected report directory")
    if not reports_parent.exists():
        if reports_parent.is_symlink() or reports_parent.resolve() != repository / "reports":
            raise VerificationError("refusing to create an unsafe report parent directory")
        try:
            reports_parent.mkdir()
        except OSError as exc:
            raise VerificationError("refusing to create an unsafe report parent directory") from exc
    if (
        reports_parent.is_symlink()
        or not reports_parent.is_dir()
        or reports_parent.resolve() != repository / "reports"
    ):
        raise VerificationError("refusing to clear an unsafe report parent directory")
    if REPORT_DIRECTORY.is_symlink():
        raise VerificationError("refusing to clear an unexpected report directory")
    if REPORT_DIRECTORY.exists():
        if not REPORT_DIRECTORY.is_dir():
            raise VerificationError("refusing to clear an unexpected report directory")
        resolved = REPORT_DIRECTORY.resolve()
        if resolved.parent != reports_parent.resolve() or resolved.name != "generated":
            raise VerificationError("refusing to clear an unexpected report directory")
        if any(resolved.iterdir()):
            _inspect_preexisting_generated_reports(policy)
        shutil.rmtree(resolved)
    REPORT_DIRECTORY.mkdir()


def _reset_candidate_artifacts(policy: dict[str, Any]) -> None:
    if DIST_DIRECTORY.is_symlink():
        raise VerificationError("refusing to clear an unexpected artifact directory")
    if not DIST_DIRECTORY.exists():
        return
    if not DIST_DIRECTORY.is_dir():
        raise VerificationError("refusing to clear an unexpected artifact directory")
    resolved = DIST_DIRECTORY.resolve()
    if resolved.parent != REPOSITORY.resolve() or resolved.name != "dist":
        raise VerificationError("refusing to clear an unexpected artifact directory")
    entries = tuple(resolved.iterdir())
    if any(
        path.is_symlink() or not path.is_file() or not _is_candidate_artifact_name(path.name)
        for path in entries
    ):
        raise VerificationError("dist contains an unexpected pre-build entry")
    for path in entries:
        try:
            if path.name.startswith("picogrid_ecn_operator_app-") and path.suffix == ".whl":
                inspect_operator_wheel(path, policy)
            elif path.suffix == ".whl":
                inspect_wheel(path, policy)
            else:
                inspect_sdist(path, policy)
        except Exception:
            raise VerificationError(
                "pre-existing candidate artifact failed publication inspection"
            ) from None
    for path in entries:
        path.unlink()


def _reset_quality_tool_outputs() -> None:
    repository = REPOSITORY.resolve()
    directories: list[Path] = []
    for name in _QUALITY_TOOL_CACHE_DIRECTORIES:
        candidate = REPOSITORY / name
        if candidate.is_symlink():
            raise VerificationError("quality-tool cache root is a symbolic link")
        if not candidate.exists():
            continue
        if not candidate.is_dir():
            raise VerificationError("quality-tool cache root is not a directory")
        resolved = candidate.resolve()
        if resolved.parent != repository or resolved.name != name:
            raise VerificationError("quality-tool cache root is outside its expected boundary")
        if any(path.is_symlink() for path in candidate.rglob("*")):
            raise VerificationError("quality-tool cache contains a symbolic link")
        directories.append(resolved)

    coverage = REPOSITORY / _QUALITY_TOOL_CACHE_FILE
    if coverage.is_symlink():
        raise VerificationError("quality-tool coverage data is a symbolic link")
    coverage_file: Path | None = None
    if coverage.exists():
        if not coverage.is_file():
            raise VerificationError("quality-tool coverage data is not a regular file")
        resolved_coverage = coverage.resolve()
        if (
            resolved_coverage.parent != repository
            or resolved_coverage.name != _QUALITY_TOOL_CACHE_FILE
        ):
            raise VerificationError("quality-tool coverage data is outside its expected boundary")
        coverage_file = resolved_coverage

    for directory in directories:
        shutil.rmtree(directory)
    if coverage_file is not None:
        coverage_file.unlink()


def _reset_project_egg_info_output() -> None:
    """Remove only setuptools metadata generated for this project by ``uv run``."""

    repository = REPOSITORY.resolve()
    source_parent = REPOSITORY / "src"
    expected = source_parent / f"{IMPORT_NAME}.egg-info"
    if PROJECT_EGG_INFO_DIRECTORY.absolute() != expected.absolute():
        raise VerificationError("project egg-info output is outside its expected boundary")
    if (
        source_parent.is_symlink()
        or not source_parent.is_dir()
        or source_parent.resolve() != repository / "src"
    ):
        raise VerificationError("project source parent is not a safe directory")
    candidate = PROJECT_EGG_INFO_DIRECTORY
    if candidate.is_symlink():
        raise VerificationError("project egg-info output is a symbolic link")
    if not candidate.exists():
        return
    if not candidate.is_dir():
        raise VerificationError("project egg-info output is not a directory")
    resolved = candidate.resolve()
    if resolved.parent != source_parent.resolve() or resolved.name != f"{IMPORT_NAME}.egg-info":
        raise VerificationError("project egg-info output is outside its expected boundary")
    entries = tuple(candidate.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in entries):
        raise VerificationError("project egg-info output contains an unsupported entry")
    shutil.rmtree(resolved)


def _reset_python_bytecode_outputs() -> None:
    """Remove only disposable Python bytecode outside the retained virtualenv."""

    repository = REPOSITORY.resolve()
    cache_directories: list[Path] = []
    loose_bytecode: list[Path] = []
    for raw_root, directory_names, file_names in os.walk(repository, followlinks=False):
        root = Path(raw_root)
        if root == repository:
            directory_names[:] = [name for name in directory_names if name not in {".git", ".venv"}]
        for name in tuple(directory_names):
            candidate = root / name
            if name != "__pycache__":
                continue
            directory_names.remove(name)
            if candidate.is_symlink() or not candidate.is_dir():
                raise VerificationError("Python bytecode cache is not a safe directory")
            resolved = candidate.resolve()
            if repository not in resolved.parents or resolved.name != "__pycache__":
                raise VerificationError("Python bytecode cache is outside the repository")
            if any(path.is_symlink() for path in candidate.rglob("*")):
                raise VerificationError("Python bytecode cache contains a symbolic link")
            cache_directories.append(resolved)
        for name in file_names:
            if not name.endswith((".pyc", ".pyo")):
                continue
            candidate = root / name
            if candidate.is_symlink() or not candidate.is_file():
                raise VerificationError("loose Python bytecode is not a safe file")
            resolved = candidate.resolve()
            if repository not in resolved.parents:
                raise VerificationError("loose Python bytecode is outside the repository")
            loose_bytecode.append(resolved)

    for directory in cache_directories:
        shutil.rmtree(directory)
    for bytecode in loose_bytecode:
        bytecode.unlink()


def _reset_local_web_outputs() -> None:
    """Remove only known disposable web-tool outputs inside the repository.

    The root entries are where the documentation toolchain wrote before it moved
    into `docs/`. A checkout that predates the move still holds them, and the
    worktree scan below would then refuse the run over files no longer belonging
    to any tool, so the migration cleans them once.
    """

    repository = REPOSITORY.resolve()
    for relative in (
        Path(".astro"),
        Path("docs/.astro"),
        Path("docs/node_modules"),
        Path("node_modules"),
        Path("site-dist"),
    ):
        candidate = REPOSITORY / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise VerificationError(f"web-tool output {relative} is not a safe directory")
        resolved = candidate.resolve()
        expected = repository / relative
        if resolved != expected:
            raise VerificationError(f"web-tool output {relative} is outside the repository")
        shutil.rmtree(resolved)
    metadata = REPOSITORY / ".DS_Store"
    if metadata.exists() or metadata.is_symlink():
        if metadata.is_symlink() or not metadata.is_file():
            raise VerificationError("desktop metadata output is not a safe file")
        if metadata.resolve().parent != repository or metadata.name != ".DS_Store":
            raise VerificationError("desktop metadata output is outside the repository")
        metadata.unlink()


def _quality_gate_environment(
    environment: Mapping[str, str], temporary_root: Path
) -> dict[str, str]:
    if temporary_root.is_symlink() or not temporary_root.is_dir():
        raise VerificationError("quality-gate temporary directory is invalid")
    resolved = temporary_root.resolve()
    repository = REPOSITORY.resolve()
    if resolved == repository or repository in resolved.parents:
        raise VerificationError("quality-gate temporary directory must be outside the repository")
    resolved.chmod(0o700)
    quality_environment = dict(environment)
    quality_environment["COVERAGE_FILE"] = str(resolved / "coverage-data")
    quality_environment["MYPY_CACHE_DIR"] = str(resolved / "mypy-cache")
    return quality_environment


def _require_safe_local_virtual_environment() -> None:
    """Reject a project virtual-environment root that escapes the repository."""

    candidate = REPOSITORY / ".venv"
    if candidate.is_symlink():
        raise VerificationError("project virtual-environment root is a symbolic link")
    if not candidate.exists():
        return
    if not candidate.is_dir():
        raise VerificationError("project virtual-environment root is not a directory")
    resolved = candidate.resolve()
    if resolved.parent != REPOSITORY.resolve() or resolved.name != ".venv":
        raise VerificationError("project virtual-environment root is outside the repository")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _public_example_names(directory: Path) -> tuple[str, ...]:
    if not directory.is_dir():
        raise VerificationError("examples directory is required before release verification")
    missing_support = [name for name in EXAMPLE_SUPPORT_FILES if not (directory / name).is_file()]
    if missing_support:
        raise VerificationError("examples directory is missing its approved local support files")
    private_helpers = sorted(
        path.name for path in directory.glob("_*.py") if path.name not in EXAMPLE_SUPPORT_FILES
    )
    if private_helpers:
        raise VerificationError("examples directory contains an unstaged local helper")
    names = tuple(
        sorted(path.name for path in directory.glob("*.py") if not path.name.startswith("_"))
    )
    if not names or any(re.fullmatch(r"[a-z][a-z0-9_]*\.py", name) is None for name in names):
        raise VerificationError("public example inventory is empty or contains an unsafe name")
    return names


def _quality_gates(
    environment: Mapping[str, str], policy: dict[str, Any]
) -> DocumentationInspection:
    _public_example_names(REPOSITORY / "examples")
    try:
        documentation = inspect_documentation(REPOSITORY, policy)
    except ArtifactPolicyError as exc:
        raise VerificationError(str(exc)) from exc
    # Run before anything reads a version: every later gate reads one copy, so a
    # disagreement between copies would otherwise pass all of them.
    try:
        synchronized = check_version_sync(REPOSITORY)
    except VersionSyncError as exc:
        raise VerificationError(f"released version is stated inconsistently:\n{exc}") from exc
    if synchronized != str(policy["project_version"]):
        raise VerificationError("release policy version does not match the synchronized version")
    if not (REPOSITORY / "tests" / "performance").is_dir():
        raise VerificationError("tests/performance is required before release verification")

    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError("uv is required to enforce the committed dependency lock")
    _run(
        [
            uv,
            "run",
            "--frozen",
            "python",
            "-m",
            "scripts.generate_api_reference",
            "--check",
        ],
        cwd=REPOSITORY,
        environment=environment,
    )
    _run(
        [
            uv,
            "run",
            "--frozen",
            "ruff",
            "check",
            "--no-cache",
            "src",
            "tests",
            "examples",
            "scripts",
        ],
        cwd=REPOSITORY,
        environment=environment,
    )
    _run(
        [
            uv,
            "run",
            "--frozen",
            "ruff",
            "format",
            "--check",
            "--no-cache",
            "src",
            "tests",
            "examples",
            "scripts",
        ],
        cwd=REPOSITORY,
        environment=environment,
    )
    _run(
        [uv, "run", "--frozen", "mypy", "--no-incremental", "src", "scripts"],
        cwd=REPOSITORY,
        environment=environment,
    )
    _run(
        [
            uv,
            "run",
            "--frozen",
            "mypy",
            "--no-incremental",
            "--strict",
            "examples",
            "tests/examples",
        ],
        cwd=REPOSITORY,
        environment=environment,
    )
    _run(
        [
            uv,
            "run",
            "--frozen",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests",
            "--cov=picogrid_ecn_client",
            "--cov-branch",
            "--cov-report=term-missing",
            f"--cov-report=json:{REPORT_DIRECTORY / 'coverage.json'}",
            "--cov-fail-under=80",
        ],
        cwd=REPOSITORY,
        environment=environment,
        timeout=600,
    )
    return documentation


def _require_safe_build_input(source: Path) -> None:
    try:
        relative = source.relative_to(REPOSITORY)
    except ValueError as exc:
        raise VerificationError("release build input is outside the repository") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise VerificationError("release build input is outside the repository")

    if REPOSITORY.is_symlink():
        raise VerificationError("release build input contains a symbolic link")
    current = REPOSITORY
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise VerificationError("release build input contains a symbolic link")
    if source.is_dir() and any(path.is_symlink() for path in source.rglob("*")):
        raise VerificationError("release build input contains a symbolic link")


def _copy_build_input(source: Path, destination: Path) -> None:
    _require_safe_build_input(source)
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _prepare_source_snapshot(
    destination: Path, source_date_epoch: int, policy: dict[str, Any]
) -> None:
    destination.mkdir()
    for relative in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        _copy_build_input(REPOSITORY / relative, destination / relative)
    for key in (
        "sdist_documentation_files",
        "sdist_example_files",
        "sdist_auxiliary_files",
    ):
        for raw_relative in _string_policy_list(policy, key):
            policy_relative = Path(raw_relative)
            if policy_relative.is_absolute() or ".." in policy_relative.parts:
                raise VerificationError("release build input is outside the repository")
            _copy_build_input(
                REPOSITORY / policy_relative,
                destination / policy_relative,
            )
    package_source = REPOSITORY / "src" / IMPORT_NAME
    _copy_build_input(package_source, destination / "src" / IMPORT_NAME)
    for path in sorted(destination.rglob("*"), reverse=True):
        os.utime(path, (source_date_epoch, source_date_epoch), follow_symlinks=False)
    os.utime(destination, (source_date_epoch, source_date_epoch), follow_symlinks=False)


def _source_tree_digest(snapshot: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in snapshot.rglob("*") if item.is_file()):
        relative = path.relative_to(snapshot).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _require_root_redirect_targets(root_redirect: str, mount: str) -> None:
    targets: list[str] = []
    attribute_pattern = re.compile(
        r"""(?P<name>[\w:-]+)\s*=\s*"""
        r"""(?:(?P<quote>["'])(?P<quoted>.*?)(?P=quote)|"""
        r"""(?P<bare>[^\s"'=<>`]+))""",
        re.DOTALL,
    )
    for tag_match in re.finditer(r"<meta\b[^>]*>", root_redirect, re.IGNORECASE):
        tag = tag_match.group()
        attribute_matches = list(attribute_pattern.finditer(tag))
        attribute_names = [match.group("name").lower() for match in attribute_matches]
        if len(attribute_names) != len(set(attribute_names)):
            raise VerificationError(
                "documentation root redirect contains an unparseable navigation target"
            )
        attributes = {
            match.group("name").lower(): (
                match.group("quoted") if match.group("quoted") is not None else match.group("bare")
            )
            for match in attribute_matches
        }
        if attributes.get("http-equiv", "").strip().lower() != "refresh":
            if re.search(r"\bhttp-equiv\b", tag, re.IGNORECASE) and re.search(
                r"\brefresh\b", tag, re.IGNORECASE
            ):
                raise VerificationError(
                    "documentation root redirect contains an unparseable navigation target"
                )
            continue
        refresh_match = re.search(
            r"""\burl\s*=\s*(?:(?P<quote>["'])(?P<quoted>.*?)(?P=quote)|"""
            r"""(?P<bare>[^;\s]+))\s*$""",
            attributes.get("content", ""),
            re.IGNORECASE,
        )
        if not refresh_match:
            raise VerificationError(
                "documentation root redirect contains an unparseable navigation target"
            )
        targets.append(refresh_match.group("quoted") or refresh_match.group("bare"))

    location_candidate_pattern = re.compile(
        r"""(?:(?:window|document)\s*\.\s*)?location\s*"""
        r"""(?:\.\s*(?:replace|assign)\s*\(\s*|\.\s*href\s*=\s*|=\s*)""",
        re.IGNORECASE,
    )
    location_pattern = re.compile(
        location_candidate_pattern.pattern + r"""(?P<quote>["'])(?P<target>.*?)(?P=quote)""",
        re.IGNORECASE | re.DOTALL,
    )
    location_matches = list(location_pattern.finditer(root_redirect))
    parsed_starts = {match.start() for match in location_matches}
    if any(
        match.start() not in parsed_starts
        for match in location_candidate_pattern.finditer(root_redirect)
    ):
        raise VerificationError(
            "documentation root redirect contains an unparseable navigation target"
        )
    for match in location_matches:
        tail = re.split(r"[;<]", root_redirect[match.end() :], maxsplit=1)[0]
        is_call = re.search(r"""\.\s*(?:replace|assign)\s*\(""", match.group(), re.IGNORECASE)
        allowed_tail = (
            r"""\s*(?:\+\s*location\s*\.\s*search\s*"""
            r"""\+\s*location\s*\.\s*hash\s*)?\)\s*"""
            if is_call
            else r"\s*"
        )
        if not re.fullmatch(allowed_tail, tail, re.IGNORECASE):
            raise VerificationError(
                "documentation root redirect contains an unparseable navigation target"
            )
        targets.append(match.group("target"))

    if not targets:
        raise VerificationError("documentation root redirect has no extractable navigation target")
    for target in targets:
        parsed_target = urlsplit(target)
        if (
            not target.startswith("/")
            or target.startswith("//")
            or parsed_target.scheme
            or parsed_target.netloc
        ):
            raise VerificationError(
                f"documentation root redirect navigation target {target!r} is not origin-relative"
            )
        if parsed_target.path != mount:
            raise VerificationError(
                "documentation root redirect navigation target "
                f"{parsed_target.path!r} does not equal configured content mount "
                f"{mount!r}"
            )


def _inspect_static_site(
    directory: Path,
    policy: dict[str, Any],
    documentation_base_path: str,
) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise VerificationError("documentation build did not produce a safe site directory")
    content_mount = documentation_base_path.removeprefix("/")
    content_mount_parts = PurePosixPath(content_mount).parts
    required = {
        "index.html",
        "404.html",
        f"{content_mount}/404.html",
        f"{content_mount}/brand/ecn-client-og.png",
        f"{content_mount}/index.html",
        f"{content_mount}/operator-mock-light.png",
        f"{content_mount}/operator-mock-mobile-dark.png",
        f"{content_mount}/operator-mock-mobile-light.png",
        f"{content_mount}/operator-mock.png",
        f"{content_mount}/pagefind/pagefind.js",
        f"{content_mount}/sitemap-index.xml",
    }
    files: list[str] = []
    brand_contents: dict[str, bytes] = {}
    total_bytes = 0
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise VerificationError("documentation site contains an unsupported entry")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise VerificationError("documentation site contains an unsafe path")
        data = path.read_bytes()
        if relative.startswith(f"{content_mount}/brand/"):
            brand_contents[relative.removeprefix(f"{content_mount}/")] = data
        under_generated_bundle = (
            pure.suffix == ".js"
            and len(pure.parts) > len(content_mount_parts)
            and pure.parts[: len(content_mount_parts)] == content_mount_parts
            and pure.parts[len(content_mount_parts)] in {"pagefind", "_astro"}
        )
        allowed_exact_urls = (
            frozenset(_string_policy_list(policy, "generated_site_placeholder_urls"))
            if under_generated_bundle
            else frozenset()
        )
        try:
            scan_secret_and_address_content(
                f"site-dist/{relative}",
                data,
                policy,
                allow_synthetic_hosts=True,
                allowed_exact_urls=allowed_exact_urls,
            )
        except ArtifactPolicyError as exc:
            category = str(exc).split(" found in ", maxsplit=1)[0]
            raise VerificationError(f"documentation site failed {category} scan") from None
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        files.append(relative)
        total_bytes += len(data)
    missing = required - set(files)
    if missing:
        raise VerificationError("documentation site is missing required generated files")
    root_redirect = (directory / "index.html").read_text(encoding="utf-8")
    mount = f"{documentation_base_path}/"
    _require_root_redirect_targets(root_redirect, mount)
    try:
        validate_public_brand_assets(brand_contents, policy, surface="documentation")
    except ArtifactPolicyError as exc:
        raise VerificationError(str(exc)) from None
    return {
        "byte_for_byte_rebuild": True,
        "file_count": len(files),
        "files": files,
        "sha256": digest.hexdigest(),
        "total_bytes": total_bytes,
    }


def _run_npm_audit(
    npm: str,
    directory: Path,
    *,
    environment: Mapping[str, str],
    report: Path,
) -> dict[str, Any]:
    output = _run(
        [npm, "audit", "--audit-level=high", "--json"],
        cwd=directory,
        environment=environment,
        capture=True,
        timeout=300,
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise VerificationError("npm audit did not produce valid JSON") from exc
    if not isinstance(value, dict):
        raise VerificationError("npm audit did not produce an object")
    metadata = value.get("metadata")
    vulnerabilities = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    if not isinstance(vulnerabilities, dict):
        raise VerificationError("npm audit omitted its vulnerability summary")
    for severity in ("high", "critical"):
        count = vulnerabilities.get(severity)
        if not isinstance(count, int) or count != 0:
            raise VerificationError(f"npm audit reported {severity}-severity vulnerabilities")
    _write_json(report, value)
    return {
        key: vulnerabilities.get(key, 0)
        for key in ("info", "low", "moderate", "high", "critical", "total")
    }


def _inspect_generated_web_tree(
    directory: Path,
    policy: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise VerificationError(f"{label} did not produce a safe output directory")
    files: list[str] = []
    brand_contents: dict[str, bytes] = {}
    total_bytes = 0
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise VerificationError(f"{label} contains an unsupported entry")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise VerificationError(f"{label} contains an unsafe path")
        data = path.read_bytes()
        if relative.startswith("brand/"):
            brand_contents[relative] = data
        try:
            scan_secret_and_address_content(
                f"{label}/{relative}",
                data,
                policy,
                allow_synthetic_hosts=True,
            )
        except ArtifactPolicyError as exc:
            category = str(exc).split(" found in ", maxsplit=1)[0]
            raise VerificationError(f"{label} failed {category} scan") from None
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        files.append(relative)
        total_bytes += len(data)
    if not files:
        raise VerificationError(f"{label} output is empty")
    try:
        validate_public_brand_assets(brand_contents, policy, surface="operator")
    except ArtifactPolicyError as exc:
        raise VerificationError(f"{label} {exc}") from None
    return {
        "file_count": len(files),
        "files": files,
        "sha256": digest.hexdigest(),
        "total_bytes": total_bytes,
    }


def _require_operator_frontend_matches(
    wheel: Path,
    frontend_dir: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Require the operator wheel to embed the separately inspected frontend bytes."""

    inspection = _inspect_generated_web_tree(
        frontend_dir,
        policy,
        label="operator frontend",
    )
    expected_files = set(inspection["files"])
    prefix = "operator_app/static/"
    with zipfile.ZipFile(wheel) as archive:
        archived = {
            name.removeprefix(prefix): name
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        }
        if set(archived) != expected_files:
            raise VerificationError("operator wheel frontend inventory differs from built output")
        for relative, member in sorted(archived.items()):
            if archive.read(member) != (frontend_dir / relative).read_bytes():
                raise VerificationError("operator wheel frontend bytes differ from built output")
    policy_static = {
        name.removeprefix(prefix)
        for name in _string_policy_list(policy, "operator_wheel_package_files")
        if name.startswith(prefix)
    }
    if policy_static != expected_files:
        raise VerificationError("operator wheel frontend policy differs from built output")
    return inspection


def _playwright_browsers_path(temporary_root: Path) -> Path:
    """The single per-run Chromium download shared by the docs and operator gates."""
    return temporary_root / "playwright-browsers"


def _site_tooling(
    temporary_root: Path,
    environment: Mapping[str, str],
    *,
    git_commit: str,
    git_tag: str,
) -> tuple[str, dict[str, str]]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise VerificationError("Node.js and npm are required for the documentation site")
    raw_version = _run(
        [node, "--version"],
        cwd=temporary_root,
        environment=environment,
        capture=True,
    ).strip()
    match = re.fullmatch(r"v([0-9]+)(?:\.[0-9]+){2}", raw_version)
    if match is None or int(match.group(1)) not in {24, 25}:
        raise VerificationError("documentation build requires Node.js >=24 and <26")

    npm_environment = dict(environment)
    npm_environment.update(
        {
            "CI": "1",
            # The snapshot has no Git history, so the site cannot discover which
            # source it came from. Inject one identity into both builds: it must
            # be constant or the byte-for-byte rebuild comparison below fails.
            "DOCS_GIT_COMMIT": git_commit,
            "DOCS_GIT_TAG": git_tag,
            "LEGION_DOCS_URL": environment.get("LEGION_DOCS_URL", ""),
            "LEGION_DOCS_VERSION": environment.get("LEGION_DOCS_VERSION", ""),
            "DOCS_TEST_OUTPUT_DIR": str(temporary_root / "docs-playwright-output"),
            "PLAYWRIGHT_BROWSERS_PATH": str(_playwright_browsers_path(temporary_root)),
            "npm_config_audit": "false",
            "npm_config_cache": str(temporary_root / "npm-cache"),
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }
    )
    return npm, npm_environment


def _required_operator_frontend_tool_versions(operator_root: Path) -> tuple[str, str]:
    try:
        package = json.loads((operator_root / "package.json").read_text(encoding="utf-8"))
        engines = package["engines"]
        node_version = engines["node"]
        npm_version = engines["npm"]
        package_manager = package["packageManager"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise VerificationError("operator frontend toolchain declaration is invalid") from None
    exact_version = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
    if (
        not isinstance(node_version, str)
        or exact_version.fullmatch(node_version) is None
        or not isinstance(npm_version, str)
        or exact_version.fullmatch(npm_version) is None
        or package_manager != f"npm@{npm_version}"
    ):
        raise VerificationError("operator frontend toolchain declaration is invalid")
    return node_version, npm_version


def _require_operator_frontend_toolchain(
    operator_root: Path,
    *,
    npm: str,
    environment: Mapping[str, str],
) -> None:
    required_node, required_npm = _required_operator_frontend_tool_versions(operator_root)
    executable_path = environment.get("PATH", os.defpath)
    node = shutil.which("node", path=executable_path)
    selected_npm = shutil.which("npm", path=executable_path)
    if node is None or selected_npm is None:
        raise VerificationError("the pinned operator frontend toolchain is required")
    try:
        npm_matches = Path(selected_npm).resolve(strict=True) == Path(npm).resolve(strict=True)
        actual_node = _run(
            [node, "--version"],
            cwd=operator_root,
            environment=environment,
            capture=True,
            timeout=30,
        ).strip()
        actual_npm = _run(
            [npm, "--version"],
            cwd=operator_root,
            environment=environment,
            capture=True,
            timeout=30,
        ).strip()
    except (OSError, VerificationError):
        raise VerificationError("the operator frontend toolchain could not be validated") from None
    if not npm_matches or actual_node != f"v{required_node}" or actual_npm != required_npm:
        raise VerificationError("the pinned operator frontend toolchain is required")


def _playwright_versions(package_lock: Path) -> tuple[str, str]:
    try:
        lock = json.loads(package_lock.read_text(encoding="utf-8"))
        packages = lock["packages"]
        test_version = packages["node_modules/@playwright/test"]["version"]
        core_version = packages["node_modules/playwright-core"]["version"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise VerificationError(
            f"could not read pinned Playwright versions from {package_lock}"
        ) from exc
    if not isinstance(test_version, str) or not isinstance(core_version, str):
        raise VerificationError(f"could not read pinned Playwright versions from {package_lock}")
    return test_version, core_version


def _require_shared_playwright_versions(source_snapshot: Path) -> None:
    root_test, root_core = _playwright_versions(source_snapshot / "docs" / "package-lock.json")
    operator_test, operator_core = _playwright_versions(
        source_snapshot / "operator-app" / "package-lock.json"
    )
    if len({root_test, root_core, operator_test, operator_core}) != 1:
        raise VerificationError(
            "shared Playwright install requires matching versions: "
            f"root @playwright/test={root_test}, playwright-core={root_core}; "
            f"operator @playwright/test={operator_test}, playwright-core={operator_core}"
        )


def _prepare_site_context(
    temporary_root: Path,
    source_snapshot: Path,
    number: int,
    *,
    npm: str,
    environment: Mapping[str, str],
) -> Path:
    context = temporary_root / f"site-context-{number}"
    shutil.copytree(source_snapshot, context, copy_function=shutil.copy2)
    with resource("network"):
        _run(
            [npm, "ci", "--ignore-scripts"],
            cwd=context / "docs",
            environment=environment,
            timeout=600,
        )
    if number == 1:
        # Keep this boundary separate: future gates need ctx1's locked pyright early.
        audit_environment = dict(environment)
        audit_environment.pop("npm_config_audit")
        _run_npm_audit(
            npm,
            context / "docs",
            environment=audit_environment,
            report=REPORT_DIRECTORY / "site-npm-audit.json",
        )
    return context


def _install_docs_browsers(
    temporary_root: Path,
    source_snapshot: Path,
    context: Path,
    *,
    environment: Mapping[str, str],
    git_commit: str,
    git_tag: str,
) -> Path:
    _require_shared_playwright_versions(source_snapshot)
    npm, npm_environment = _site_tooling(
        temporary_root,
        environment,
        git_commit=git_commit,
        git_tag=git_tag,
    )
    with resource("network"):
        _run(
            [npm, "run", "docs:test:browser:install"],
            cwd=context / "docs",
            environment=npm_environment,
            timeout=900,
        )
    return _playwright_browsers_path(temporary_root)


def _build_docs_site(
    temporary_root: Path,
    source_snapshot: Path,
    context_one: Path,
    *,
    documentation_base_path: str,
    environment: Mapping[str, str],
    policy: dict[str, Any],
    jobs: int,
    git_commit: str,
    git_tag: str,
) -> tuple[Path, dict[str, Any]]:
    npm, npm_environment = _site_tooling(
        temporary_root,
        environment,
        git_commit=git_commit,
        git_tag=git_tag,
    )
    contexts = {1: context_one}
    builds: dict[int, tuple[Path, dict[str, Any]]] = {}

    def install_context(number: int) -> Path:
        context = _prepare_site_context(
            temporary_root,
            source_snapshot,
            number,
            npm=npm,
            environment=npm_environment,
        )
        contexts[number] = context
        return context

    def check(number: int) -> None:
        _run(
            [npm, "run", "docs:check"],
            cwd=contexts[number] / "docs",
            environment=npm_environment,
            timeout=600,
        )

    def browser_test() -> None:
        _run(
            [npm, "run", "docs:test:browser"],
            cwd=contexts[1] / "docs",
            environment=npm_environment,
            timeout=600,
        )

    def inspect(number: int) -> tuple[Path, dict[str, Any]]:
        site = contexts[number] / "site-dist"
        inspection = _inspect_static_site(site, policy, documentation_base_path)
        if number == 1:
            for name in (
                "operator-mock-light.png",
                "operator-mock-mobile-dark.png",
                "operator-mock-mobile-light.png",
                "operator-mock.png",
            ):
                if sha256_file(
                    site / documentation_base_path.removeprefix("/") / name
                ) != sha256_file(source_snapshot / "operator-app" / "docs" / name):
                    raise VerificationError("documentation site operator screenshot changed")
        published = (site / documentation_base_path.removeprefix("/") / "index.html").read_text(
            encoding="utf-8"
        )
        if f'<meta name="source-commit" content="{git_commit}"' not in published:
            raise VerificationError("documentation site does not advertise the verified commit")
        inspection["browser_accessibility_workflows"] = number == 1
        inspection["git_commit"] = git_commit
        inspection["git_tag"] = git_tag
        inspection["package_lock_sha256"] = sha256_file(
            source_snapshot / "docs" / "package-lock.json"
        )
        result = (site, inspection)
        builds[number] = result
        return result

    def compare() -> tuple[Path, dict[str, Any]]:
        if builds[1][1]["sha256"] != builds[2][1]["sha256"]:
            raise VerificationError("documentation site rebuild is not byte-for-byte reproducible")
        return builds[1]

    results = run_stages(
        [
            Stage("check1", lambda: check(1)),
            Stage("browsertest", browser_test, ("check1",)),
            Stage("inspect1", lambda: inspect(1), ("check1",)),
            Stage("ci2", lambda: install_context(2)),
            Stage("check2", lambda: check(2), ("ci2",)),
            Stage("inspect2", lambda: inspect(2), ("check2",)),
            Stage("compare", compare, ("inspect1", "inspect2", "browsertest")),
        ],
        jobs=jobs,
    )
    result = results["compare"]
    if not isinstance(result, tuple):
        raise VerificationError("documentation site stage returned an invalid result")
    return result


def _promote_verified_site(
    site: Path,
    inspection: Mapping[str, Any],
    policy: dict[str, Any],
    documentation_base_path: str,
) -> Path:
    target = REPOSITORY / "site-dist"
    if target.exists() or target.is_symlink():
        raise VerificationError("documentation output changed after pre-build cleanup")
    shutil.copytree(site, target, copy_function=shutil.copy2)
    promoted = _inspect_static_site(target, policy, documentation_base_path)
    if promoted["sha256"] != inspection.get("sha256"):
        raise VerificationError("promoted documentation site differs from verified input")
    return target


def _create_environment(
    path: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    cwd: Path,
    interpreter: str | Path = sys.executable,
    seed: bool = False,
) -> Path:
    command = [uv, "venv"]
    if seed:
        command.append("--seed")
    command.extend(["--python", str(interpreter), str(path)])
    _run(
        command,
        cwd=cwd,
        environment=environment,
        timeout=180,
    )
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise VerificationError("temporary environment did not contain a Python interpreter")
    return python


def _find_python_minor(minor: str, uv: str, environment: Mapping[str, str]) -> Path:
    if re.fullmatch(r"3\.[0-9]+", minor) is None:
        raise VerificationError("requested Python minor is invalid")
    output = _run(
        [
            uv,
            "python",
            "find",
            "--no-project",
            "--no-python-downloads",
            minor,
        ],
        cwd=REPOSITORY,
        environment=environment,
        capture=True,
    ).strip()
    if not output:
        raise VerificationError(f"an installed Python {minor} interpreter is required")
    interpreter = Path(output).resolve()
    if not interpreter.is_file():
        raise VerificationError(f"uv returned an invalid Python {minor} interpreter")
    selected_minor = _run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        cwd=REPOSITORY,
        environment=environment,
        capture=True,
    ).strip()
    if selected_minor != minor:
        raise VerificationError(f"expected Python {minor}, got {selected_minor or 'unknown'}")
    return interpreter


def _install_requirements(
    python: Path,
    requirements: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    cwd: Path,
) -> None:
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--strict",
            "--requirements",
            str(requirements),
        ],
        cwd=cwd,
        environment=environment,
        timeout=600,
    )


def _normalize_sdist(path: Path, source_date_epoch: int) -> None:
    temporary = path.with_name(f".{path.name}.normalized")
    with tarfile.open(path, mode="r:gz") as source:
        members = source.getmembers()
        if any(
            member.issym() or member.islnk() or member.isdev() or member.isfifo()
            for member in members
        ):
            raise VerificationError("build produced an unsafe sdist member")
        with (
            temporary.open("wb") as raw_target,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_target, mtime=source_date_epoch
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target,
        ):
            for member in sorted(members, key=lambda item: item.name):
                normalized = tarfile.TarInfo(member.name)
                normalized.mtime = source_date_epoch
                normalized.uid = 0
                normalized.gid = 0
                normalized.uname = ""
                normalized.gname = ""
                if member.isdir():
                    normalized.type = tarfile.DIRTYPE
                    normalized.mode = 0o755
                    target.addfile(normalized)
                elif member.isfile():
                    normalized.type = tarfile.REGTYPE
                    normalized.mode = 0o644
                    normalized.size = member.size
                    payload = source.extractfile(member)
                    if payload is None:
                        raise VerificationError(f"could not read built sdist member {member.name}")
                    target.addfile(normalized, payload)
                else:
                    raise VerificationError(
                        f"build produced unsupported sdist member {member.name}"
                    )
    temporary.replace(path)


def _prepare_build_inputs(
    temporary_root: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    policy: dict[str, Any],
    source_date_epoch: int,
) -> tuple[Path, Path]:
    build_python = _create_environment(
        temporary_root / "build-environment",
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    _install_requirements(
        build_python,
        REPOSITORY / "scripts" / "build-requirements.txt",
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    snapshot = temporary_root / "source-snapshot"
    _prepare_source_snapshot(snapshot, source_date_epoch, policy)
    return build_python, snapshot


def _build_artifacts(
    temporary_root: Path,
    build_python: Path,
    snapshot: Path,
    *,
    environment: Mapping[str, str],
    source_date_epoch: int,
    jobs: int,
) -> tuple[Path, Path, str, dict[str, str]]:
    def build(number: int) -> Path:
        context = temporary_root / f"build-context-{number}"
        shutil.copytree(snapshot, context, copy_function=shutil.copy2)
        output = temporary_root / f"build-output-{number}"
        output.mkdir()
        _run(
            [
                str(build_python),
                "-I",
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output),
            ],
            cwd=context,
            environment=environment,
            timeout=300,
        )
        sdist = next(output.glob("*.tar.gz"), None)
        if sdist is None:
            raise VerificationError("build did not produce a source distribution")
        _normalize_sdist(sdist, source_date_epoch)
        return output

    outputs = run_stages(
        [
            Stage("build1", lambda: build(1)),
            Stage("build2", lambda: build(2)),
        ],
        jobs=jobs,
    )
    first_output = outputs["build1"]
    second_output = outputs["build2"]
    if not isinstance(first_output, Path) or not isinstance(second_output, Path):
        raise VerificationError("artifact build stage returned an invalid result")
    digests = compare_rebuilt_artifacts(first_output, second_output)
    wheel = next(first_output.glob("*.whl"), None)
    sdist = next(first_output.glob("*.tar.gz"), None)
    if wheel is None or sdist is None:
        raise VerificationError("verified build did not produce both artifacts")
    return wheel, sdist, _source_tree_digest(snapshot), digests


def _promote_verified_artifacts(
    wheel: Path,
    sdist: Path,
    operator_wheel: Path,
) -> tuple[Path, Path, Path]:
    expected = (
        wheel.name.startswith("picogrid_ecn_client-")
        and wheel.suffix == ".whl"
        and sdist.name.startswith("picogrid_ecn_client-")
        and sdist.name.endswith(".tar.gz")
        and operator_wheel.name.startswith("picogrid_ecn_operator_app-")
        and operator_wheel.suffix == ".whl"
    )
    if not expected:
        raise VerificationError("refusing to promote artifacts with unexpected names")
    repository = REPOSITORY.resolve()
    if DIST_DIRECTORY.is_symlink():
        raise VerificationError("refusing to promote into a symbolic-link artifact directory")
    if DIST_DIRECTORY.exists():
        if (
            not DIST_DIRECTORY.is_dir()
            or DIST_DIRECTORY.resolve().parent != repository
            or DIST_DIRECTORY.resolve().name != "dist"
        ):
            raise VerificationError("refusing to promote into an unexpected artifact directory")
    else:
        if DIST_DIRECTORY.parent.resolve() != repository or DIST_DIRECTORY.name != "dist":
            raise VerificationError("refusing to promote into an unexpected artifact directory")
        DIST_DIRECTORY.mkdir()
    if any(DIST_DIRECTORY.iterdir()):
        raise VerificationError("artifact directory changed after pre-build cleanup")
    promoted_wheel = DIST_DIRECTORY / wheel.name
    promoted_sdist = DIST_DIRECTORY / sdist.name
    promoted_operator_wheel = DIST_DIRECTORY / operator_wheel.name
    shutil.copy2(wheel, promoted_wheel)
    shutil.copy2(sdist, promoted_sdist)
    shutil.copy2(operator_wheel, promoted_operator_wheel)
    if (
        sha256_file(promoted_wheel) != sha256_file(wheel)
        or sha256_file(promoted_sdist) != sha256_file(sdist)
        or sha256_file(promoted_operator_wheel) != sha256_file(operator_wheel)
    ):
        raise VerificationError("promoted artifacts differ from their verified inputs")
    return promoted_wheel, promoted_sdist, promoted_operator_wheel


def _is_candidate_artifact_name(name: str) -> bool:
    return (
        name.startswith("picogrid_ecn_client-")
        and (name.endswith(".whl") or name.endswith(".tar.gz"))
    ) or (name.startswith("picogrid_ecn_operator_app-") and name.endswith(".whl"))


def _inspect_final_dist_inventory(wheel: Path, sdist: Path, operator_wheel: Path) -> None:
    if DIST_DIRECTORY.is_symlink() or not DIST_DIRECTORY.is_dir():
        raise VerificationError("final dist inventory is not a regular directory")
    resolved = DIST_DIRECTORY.resolve()
    expected = {wheel.name, sdist.name, operator_wheel.name}
    if (
        wheel.parent.resolve() != resolved
        or sdist.parent.resolve() != resolved
        or operator_wheel.parent.resolve() != resolved
        or not _is_candidate_artifact_name(wheel.name)
        or not _is_candidate_artifact_name(sdist.name)
        or not operator_wheel.name.startswith("picogrid_ecn_operator_app-")
        or operator_wheel.suffix != ".whl"
    ):
        raise VerificationError("final dist inventory does not match promoted artifacts")
    entries = tuple(resolved.iterdir())
    if {path.name for path in entries} != expected or len(entries) != 3:
        raise VerificationError("final dist inventory does not match promoted artifacts")
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise VerificationError("final dist inventory contains an unsupported entry")


def _export_requirements(
    target: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    include_dev: bool,
) -> None:
    command = [
        uv,
        "export",
        "--frozen",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
        "--quiet",
    ]
    if not include_dev:
        command.append("--no-dev")
    command.extend(["--output-file", str(target)])
    _run(command, cwd=REPOSITORY, environment=environment)


def _install_exact_wheel(
    python: Path,
    wheel: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    cwd: Path,
) -> None:
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--strict",
            str(wheel),
        ],
        cwd=cwd,
        environment=environment,
        timeout=300,
    )


def _stage_verification_inputs(staging: Path) -> None:
    shutil.copytree(
        REPOSITORY / "tests",
        staging / "tests",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    examples_source = REPOSITORY / "examples"
    try:
        source_paths = inspect_example_manifest(REPOSITORY)
    except ArtifactPolicyError as exc:
        raise VerificationError(str(exc)) from exc
    public_examples = tuple(PurePosixPath(path).name for path in source_paths)
    _public_example_names(examples_source)
    staged_examples = staging / "examples"
    staged_examples.mkdir()
    for name in (*EXAMPLE_SUPPORT_FILES, *public_examples):
        shutil.copy2(examples_source / name, staged_examples / name)
    shutil.copy2(
        examples_source / "manifest.json",
        staging / EXAMPLE_MANIFEST_FILE,
    )
    staged_scripts = staging / "scripts"
    staged_scripts.mkdir()
    for name in ("__init__.py", "release_checks.py"):
        shutil.copy2(REPOSITORY / "scripts" / name, staged_scripts / name)
    shutil.copy2(REPOSITORY / "pyproject.toml", staging / "pyproject.toml")
    shutil.copy2(
        REPOSITORY / "scripts" / "installed_wheel_probe.py",
        staging / "installed_wheel_probe.py",
    )
    shutil.copy2(
        REPOSITORY / "scripts" / "installed_mock_process.py",
        staging / "installed_mock_process.py",
    )
    shutil.copy2(
        REPOSITORY / "scripts" / "installed_cli_probe.py",
        staging / "installed_cli_probe.py",
    )
    example_driver = REPOSITORY / "scripts" / "installed_examples.py"
    if not example_driver.is_file():
        raise VerificationError("scripts/installed_examples.py is required")
    shutil.copy2(example_driver, staging / "installed_examples.py")


def _installed_search_root(
    python: Path,
    temporary_root: Path,
    *,
    environment: Mapping[str, str],
) -> Path:
    """Return the isolated site-packages directory holding the installed package.

    The generator takes a search path, not the package directory, so this returns
    the parent while proving the package itself resolves outside the repository.
    """

    output = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import pathlib,picogrid_ecn_client as package;"
                "print(pathlib.Path(package.__file__).resolve().parent)"
            ),
        ],
        cwd=temporary_root,
        environment=environment,
        capture=True,
    ).strip()
    package_root = Path(output)
    repository = REPOSITORY.resolve()
    if (
        not package_root.is_absolute()
        or not package_root.is_dir()
        or package_root.name != IMPORT_NAME
        or package_root == repository
        or repository in package_root.parents
    ):
        raise VerificationError("installed-wheel API package root is invalid")
    return package_root.parent


def _generate_reference_inventory(
    package_root: Path,
    output: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Emit the normalized public API inventory for one package search root.

    The generator is a repository development tool, so it runs from the locked
    project environment; ``--package-root`` selects the package under inspection
    and the generator refuses to fall back to the source tree.
    """

    _run(
        [
            uv,
            "run",
            "--frozen",
            "python",
            "-m",
            "scripts.generate_api_reference",
            "--package-root",
            str(package_root),
            "--inventory",
            str(output),
        ],
        cwd=REPOSITORY,
        environment=environment,
    )
    try:
        inventory = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("Python API inventory output is missing or invalid") from exc
    if not isinstance(inventory, dict):
        raise VerificationError("Python API inventory output must be a JSON object")
    return inventory


def _reference_symbol_map(inventory: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_symbols = inventory.get("symbols")
    if not isinstance(raw_symbols, list):
        raise VerificationError("Python API inventory symbols must be a list")
    symbols: dict[str, dict[str, Any]] = {}
    for raw_symbol in raw_symbols:
        if not isinstance(raw_symbol, dict):
            raise VerificationError("Python API inventory contains an invalid symbol")
        name = raw_symbol.get("canonical_import")
        source_path = raw_symbol.get("source_path")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(source_path, str)
            or Path(source_path).is_absolute()
        ):
            raise VerificationError("Python API inventory contains an invalid symbol")
        normalized = dict(raw_symbol)
        normalized["source_path"] = PurePosixPath(source_path.replace("\\", "/")).as_posix()
        if name in symbols:
            raise VerificationError("Python API inventory contains a duplicate symbol")
        symbols[name] = normalized
    return symbols


def _compare_reference_inventories(source: Mapping[str, Any], installed: Mapping[str, Any]) -> None:
    source_symbols = _reference_symbol_map(source)
    installed_symbols = _reference_symbol_map(installed)
    mismatches = sorted(
        name
        for name in source_symbols.keys() | installed_symbols.keys()
        if source_symbols.get(name) != installed_symbols.get(name)
    )
    if mismatches:
        raise VerificationError(
            "installed-wheel Python API inventory mismatch: " + ", ".join(mismatches)
        )
    for key in ("manifest_version", "package", "source_paths"):
        if source.get(key) != installed.get(key):
            raise VerificationError(f"installed-wheel Python API inventory {key} mismatch")


def _run_installed_reference_gate(
    python: Path,
    source_snapshot: Path,
    temporary_root: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
) -> None:
    """Require the immutable source snapshot and the exact installed wheel to
    produce the identical approved public API inventory."""

    source = _generate_reference_inventory(
        source_snapshot / "src",
        temporary_root / "source-api-inventory.json",
        uv=uv,
        environment=environment,
    )
    installed = _generate_reference_inventory(
        _installed_search_root(python, temporary_root, environment=environment),
        temporary_root / "installed-api-inventory.json",
        uv=uv,
        environment=environment,
    )
    _compare_reference_inventories(source, installed)


def _run_typing_gate(
    wheel: Path,
    *,
    uv: str,
    site_context: Path,
    environment: Mapping[str, str],
) -> None:
    """Verify the exact wheel's typing contract on Python 3.11, 3.12, 3.13,
    and 3.14.

    Pyright comes from the verified documentation build context so the release
    uses the same npm-locked checker the site build installed.
    """

    pyright = site_context / "docs" / "node_modules" / ".bin" / "pyright"
    if not pyright.is_file():
        raise VerificationError("the locked pyright executable is missing from the site context")
    _run(
        [
            uv,
            "run",
            "--frozen",
            "python",
            "-m",
            "scripts.verify_types",
            "--wheel",
            str(wheel),
            "--pyright",
            str(pyright),
            "--report",
            str(REPORT_DIRECTORY / "type-completeness.json"),
            "--pyright-report",
            str(REPORT_DIRECTORY / "pyright-verifytypes.json"),
        ],
        cwd=REPOSITORY,
        environment=environment,
        timeout=1800,
    )


def _probe_installed_wheel(
    python: Path,
    staging: Path,
    *,
    environment: Mapping[str, str],
    expected_python_minor: str = "3.11",
) -> dict[str, Any]:
    output = _run(
        [
            str(python),
            "-I",
            str(staging / "installed_wheel_probe.py"),
            "--repository-root",
            str(REPOSITORY),
            "--expected-python-minor",
            expected_python_minor,
        ],
        cwd=staging,
        environment=environment,
        capture=True,
    )
    value = json.loads(output)
    if not isinstance(value, dict):
        raise VerificationError("installed-wheel probe did not return an object")
    return value


def _run_python_313_shutdown_gate(
    python: Path,
    staging: Path,
    *,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    probe = _probe_installed_wheel(
        python,
        staging,
        environment=environment,
        expected_python_minor="3.13",
    )
    test_path, test_name = PYTHON_313_SHUTDOWN_TEST.split("::", maxsplit=1)
    staged_test = staging / test_path
    if not staged_test.is_file():
        raise VerificationError("Python 3.13 shutdown regression test is missing")
    _run(
        [
            str(python),
            "-I",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(staging / "pyproject.toml"),
            f"{staged_test}::{test_name}",
        ],
        cwd=staging,
        environment=environment,
        timeout=60,
    )
    return probe


def _sanitize_sbom(
    path: Path,
    *,
    allowed_local_projects: Sequence[str] = (PACKAGE_NAME,),
) -> None:
    """Remove external locations while retaining the exact component inventory."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError("CycloneDX SBOM did not contain an object")

    components = value.get("components")
    if not isinstance(components, list):
        raise VerificationError("CycloneDX SBOM omitted its component inventory")
    if not all(isinstance(component, dict) for component in components):
        raise VerificationError("CycloneDX SBOM contains an invalid component")

    def remove_external_locations(node: Any) -> None:
        if isinstance(node, dict):
            references = node.get("externalReferences")
            if references is not None:
                if not isinstance(references, list):
                    raise VerificationError("CycloneDX SBOM contains invalid external references")
                local_references = [
                    reference
                    for reference in references
                    if isinstance(reference, dict)
                    and str(reference.get("url", "")).startswith("file://")
                ]
                if local_references and node.get("name") not in allowed_local_projects:
                    raise VerificationError(
                        "CycloneDX SBOM contains an unexpected local dependency reference"
                    )
                node.pop("externalReferences")
            for child in node.values():
                remove_external_locations(child)
        elif isinstance(node, list):
            for child in node:
                remove_external_locations(child)

    value.pop("$schema", None)
    value.pop("serialNumber", None)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("timestamp", None)
    remove_external_locations(value)

    identity = uuid.uuid5(
        uuid.NAMESPACE_URL,
        json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    value["serialNumber"] = f"urn:uuid:{identity}"
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if "file://" in serialized or str(REPOSITORY) in serialized:
        raise VerificationError("CycloneDX SBOM contains a local filesystem reference")
    path.write_text(serialized, encoding="utf-8")


def _validate_sbom(path: Path) -> None:
    try:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonValidator
    except ImportError as exc:
        raise VerificationError("CycloneDX validation support is unavailable") from exc
    contents = path.read_text(encoding="utf-8")
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise VerificationError("sanitized CycloneDX SBOM is not valid JSON") from exc
    version_by_value = {
        "1.5": SchemaVersion.V1_5,
        "1.6": SchemaVersion.V1_6,
    }
    raw_spec_version = value.get("specVersion") if isinstance(value, dict) else None
    schema_version = (
        version_by_value.get(raw_spec_version) if isinstance(raw_spec_version, str) else None
    )
    if schema_version is None:
        raise VerificationError("sanitized CycloneDX SBOM is invalid: unsupported spec version")
    error = JsonValidator(schema_version).validate_str(contents)
    if error is not None:
        raise VerificationError(f"sanitized CycloneDX SBOM is invalid: {error}")


def _audit_report_vulnerability_count(path: Path) -> int | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("dependencies"), list):
        return None
    count = 0
    for dependency in value["dependencies"]:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("vulns"), list):
            return None
        count += len(dependency["vulns"])
    return count


def _run_pip_audit_with_bounded_network_retry(
    command: Sequence[str],
    *,
    report: Path,
    environment: Mapping[str, str],
) -> None:
    """Retry only recognizable transient advisory-network failures."""

    printable = " ".join(command)
    transient_markers = (
        "connection aborted",
        "connection error",
        "connection reset",
        "connection timed out",
        "name resolution",
        "remote end closed",
        "service unavailable",
        "temporary failure",
        "temporarily unavailable",
        "timed out",
        "too many requests",
    )
    for attempt in range(1, 4):
        print(f"\n$ {printable}", flush=True)
        try:
            result = subprocess.run(
                command,
                cwd=REPORT_DIRECTORY,
                env=environment,
                check=False,
                text=True,
                capture_output=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            if attempt == 3:
                raise VerificationError(
                    "pip-audit advisory lookup timed out after three bounded attempts"
                ) from None
            time.sleep(attempt)
            continue

        vulnerability_count = _audit_report_vulnerability_count(report)
        if vulnerability_count:
            raise VerificationError(
                f"pip-audit found {vulnerability_count} known runtime vulnerabilities"
            )
        if result.returncode == 0:
            if vulnerability_count is None:
                raise VerificationError("pip-audit did not produce a valid JSON report")
            return

        diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
        transient = any(marker in diagnostic for marker in transient_markers)
        if not transient:
            raise VerificationError(
                f"pip-audit failed with exit code {result.returncode}; failure was not transient"
            )
        if attempt == 3:
            raise VerificationError(
                "pip-audit advisory lookup failed after three bounded transient retries"
            )
        if report.exists():
            report.unlink()
        time.sleep(attempt)


def _generate_supply_chain_reports(
    runtime_python: Path,
    runtime_requirements: Path,
    probe: dict[str, Any],
    *,
    environment: Mapping[str, str],
    policy: dict[str, Any],
) -> None:
    cyclonedx = shutil.which("cyclonedx-py")
    pip_audit = shutil.which("pip-audit")
    if cyclonedx is None or pip_audit is None:
        raise VerificationError("cyclonedx-py and pip-audit are required release tools")
    _run(
        [
            cyclonedx,
            "environment",
            str(runtime_python),
            "--mc-type",
            "library",
            "--spec-version",
            "1.6",
            "--output-reproducible",
            "--output-format",
            "JSON",
            "--output-file",
            str(REPORT_DIRECTORY / "sbom.cdx.json"),
            "--validate",
        ],
        cwd=REPORT_DIRECTORY,
        environment=environment,
        timeout=300,
    )
    _sanitize_sbom(REPORT_DIRECTORY / "sbom.cdx.json")
    _validate_sbom(REPORT_DIRECTORY / "sbom.cdx.json")
    vulnerability_report = REPORT_DIRECTORY / "vulnerability-scan.json"
    _run_pip_audit_with_bounded_network_retry(
        [
            pip_audit,
            "--requirement",
            str(runtime_requirements),
            "--require-hashes",
            "--disable-pip",
            "--strict",
            "--progress-spinner",
            "off",
            "--format",
            "json",
            "--output",
            str(vulnerability_report),
        ],
        report=vulnerability_report,
        environment=environment,
    )

    dependencies = probe.get("dependencies")
    if not isinstance(dependencies, list):
        raise VerificationError("installed-wheel probe omitted dependency inventory")
    _write_json(
        REPORT_DIRECTORY / "dependencies.json",
        {
            "environment": "exact wheel runtime dependency closure",
            "packages": dependencies,
        },
    )
    indexed = {
        str(item.get("name", "")).replace("_", "-").casefold(): item
        for item in dependencies
        if isinstance(item, dict)
    }
    direct_policy = policy.get("direct_runtime_dependencies")
    if not isinstance(direct_policy, dict):
        raise VerificationError("release policy omitted direct runtime dependency review")
    reviewed: list[dict[str, Any]] = []
    for name, review in sorted(direct_policy.items()):
        installed = indexed.get(name.replace("_", "-").casefold())
        if installed is None or not isinstance(review, dict):
            raise VerificationError(f"dependency review could not resolve {name}")
        reviewed.append({**review, **installed, "direct": True})
    _write_json(
        REPORT_DIRECTORY / "dependency-licenses.json",
        {
            "legal_status": (
                "Technical inventory only; recording authorized legal/product approval of the "
                "MPL-2.0 selection remains a publication prerequisite."
            ),
            "reviewed_direct_dependencies": reviewed,
            "runtime_closure": dependencies,
        },
    )


def _generate_operator_supply_chain_reports(
    runtime_python: Path,
    runtime_requirements: Path,
    probe: dict[str, Any],
    *,
    environment: Mapping[str, str],
) -> None:
    cyclonedx = shutil.which("cyclonedx-py")
    pip_audit = shutil.which("pip-audit")
    if cyclonedx is None or pip_audit is None:
        raise VerificationError("cyclonedx-py and pip-audit are required release tools")
    sbom = REPORT_DIRECTORY / "operator-sbom.cdx.json"
    _run(
        [
            cyclonedx,
            "environment",
            str(runtime_python),
            "--mc-type",
            "application",
            "--spec-version",
            "1.6",
            "--output-reproducible",
            "--output-format",
            "JSON",
            "--output-file",
            str(sbom),
            "--validate",
        ],
        cwd=REPORT_DIRECTORY,
        environment=environment,
        timeout=300,
    )
    _sanitize_sbom(
        sbom,
        allowed_local_projects=(PACKAGE_NAME, OPERATOR_PACKAGE_NAME),
    )
    _validate_sbom(sbom)
    vulnerability_report = REPORT_DIRECTORY / "operator-vulnerability-scan.json"
    _run_pip_audit_with_bounded_network_retry(
        [
            pip_audit,
            "--requirement",
            str(runtime_requirements),
            "--require-hashes",
            "--disable-pip",
            "--strict",
            "--progress-spinner",
            "off",
            "--format",
            "json",
            "--output",
            str(vulnerability_report),
        ],
        report=vulnerability_report,
        environment=environment,
    )
    dependencies = probe.get("dependencies")
    if not isinstance(dependencies, list):
        raise VerificationError("operator installed-wheel probe omitted dependency inventory")
    _write_json(
        REPORT_DIRECTORY / "operator-dependencies.json",
        {
            "environment": "exact operator runtime with exact public client wheel",
            "packages": dependencies,
        },
    )


def _generate_npm_sbom(
    npm: str,
    directory: Path,
    *,
    environment: Mapping[str, str],
    report: Path,
) -> dict[str, Any]:
    output = _run(
        [npm, "sbom", "--sbom-format", "cyclonedx", "--omit=dev"],
        cwd=directory,
        environment=environment,
        capture=True,
        timeout=300,
    )
    report.write_text(output, encoding="utf-8")
    _sanitize_sbom(
        report,
        allowed_local_projects=(OPERATOR_PACKAGE_NAME, OPERATOR_NODE_PACKAGE_NAME),
    )
    _validate_sbom(report)
    value = json.loads(report.read_text(encoding="utf-8"))
    components = value.get("components") if isinstance(value, dict) else None
    if not isinstance(components, list):
        raise VerificationError("npm CycloneDX SBOM omitted its component inventory")
    return {
        "component_count": len(components),
        "sha256": sha256_file(report),
        "spec_version": value.get("specVersion"),
    }


def _run_installed_suite(
    python: Path,
    staging: Path,
    *,
    environment: Mapping[str, str],
) -> None:
    selected = [
        staging / "tests" / "unit",
        staging / "tests" / "contract",
        staging / "tests" / "e2e",
        staging / "tests" / "performance",
        staging / "tests" / "examples",
    ]
    if any(not path.is_dir() for path in selected):
        raise VerificationError("installed-wheel test stage is missing a required test directory")
    _run(
        [
            str(python),
            "-I",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(staging / "pyproject.toml"),
            *(str(path) for path in selected),
        ],
        cwd=staging,
        environment=environment,
        timeout=600,
    )
    _run(
        [str(python), "-I", str(staging / "installed_mock_process.py")],
        cwd=staging,
        environment=environment,
        timeout=30,
    )
    _run(
        [str(python), "-I", str(staging / "installed_cli_probe.py")],
        cwd=staging,
        environment=environment,
        timeout=60,
    )
    _run(
        [str(python), "-I", str(staging / "installed_examples.py")],
        cwd=staging,
        environment=environment,
        timeout=300,
    )


def _run_documented_installation_smoke(
    python: Path,
    wheel: Path,
    root: Path,
    *,
    environment: Mapping[str, str],
    version: str,
) -> None:
    """Execute the exact installation-guide wheel command in a clean environment."""

    staging = root / "documentation-installation"
    staging.mkdir()
    staged_wheel = staging / wheel.name
    shutil.copy2(wheel, staged_wheel)
    install_environment = dict(environment)
    install_environment.pop("PIP_INDEX_URL", None)
    install_environment.pop("PIP_EXTRA_INDEX_URL", None)
    install_environment.pop("PIP_FIND_LINKS", None)
    install_environment.pop("PIP_NO_INDEX", None)
    install_environment["PIP_CONFIG_FILE"] = os.devnull
    install_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata as metadata; "
                "present={d.metadata['Name'].lower().replace('_','-') "
                "for d in metadata.distributions() if d.metadata['Name']}; "
                "assert not ({'picogrid-ecn-client','aiomqtt','paho-mqtt',"
                "'protobuf','pydantic'} & present), present"
            ),
        ],
        cwd=staging,
        environment=install_environment,
        timeout=30,
    )
    _run(
        [str(python), "-m", "pip", "install", f"./{staged_wheel.name}"],
        cwd=staging,
        environment=install_environment,
        timeout=300,
    )
    _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "from pathlib import Path; import picogrid_ecn_client as package; "
                "origin = Path(package.__file__).resolve(); "
                "assert 'site-packages' in origin.parts; "
                f"assert package.__version__ == {version!r}"
            ),
        ],
        cwd=staging,
        environment=install_environment,
        timeout=30,
    )


def _run_documented_operator_installation_smoke(
    python: Path,
    client_wheel: Path,
    operator_wheel: Path,
    root: Path,
    *,
    environment: Mapping[str, str],
    version: str,
) -> dict[str, Any]:
    """Execute the documented two-wheel install with dependency resolution."""

    staging = root / "operator-documentation-installation"
    staging.mkdir()
    staged_client = staging / client_wheel.name
    staged_operator = staging / operator_wheel.name
    shutil.copy2(client_wheel, staged_client)
    shutil.copy2(operator_wheel, staged_operator)
    install_environment = dict(environment)
    install_environment.pop("PIP_INDEX_URL", None)
    install_environment.pop("PIP_EXTRA_INDEX_URL", None)
    install_environment.pop("PIP_FIND_LINKS", None)
    install_environment.pop("PIP_NO_INDEX", None)
    install_environment["PIP_CONFIG_FILE"] = os.devnull
    install_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata as metadata; "
                "present={d.metadata['Name'].lower().replace('_','-') "
                "for d in metadata.distributions() if d.metadata['Name']}; "
                "assert not ({'picogrid-ecn-client','picogrid-ecn-operator-app',"
                "'fastapi','pydantic'} & present), present"
            ),
        ],
        cwd=staging,
        environment=install_environment,
        timeout=30,
    )
    install_argv = documented_operator_install_argv(version)
    if not install_argv or install_argv[0] != "python":
        raise VerificationError("documented operator install command changed")
    with resource("network"):
        _run(
            [str(python), *install_argv[1:]],
            cwd=staging,
            environment=install_environment,
            timeout=300,
        )
    probe = _probe_installed_operator_application(
        python,
        environment=install_environment,
        cwd=staging,
    )
    if probe.get("client_version") != version or probe.get("operator_version") != version:
        raise VerificationError("documented operator install resolved an unexpected version")
    return probe


def _probe_installed_operator_application(
    python: Path,
    *,
    environment: Mapping[str, str],
    cwd: Path,
) -> dict[str, Any]:
    output = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata as m, json, sys; "
                "from pathlib import Path; "
                "import operator_app, picogrid_ecn_client; "
                "operator_origin=Path(operator_app.__file__).resolve(); "
                "client_origin=Path(picogrid_ecn_client.__file__).resolve(); "
                "environment=Path(sys.prefix).resolve(); "
                "assert environment in operator_origin.parents; "
                "assert environment in client_origin.parents; "
                "assert 'site-packages' in operator_origin.parts; "
                "assert 'site-packages' in client_origin.parts; "
                "print(json.dumps({"
                "'client_import_origin':'isolated-environment-site-packages',"
                "'client_version':m.version('picogrid-ecn-client'),"
                "'operator_import_origin':'isolated-environment-site-packages',"
                "'operator_version':m.version('picogrid-ecn-operator-app'),"
                "'python':'.'.join(map(str,sys.version_info[:3]))},sort_keys=True))"
            ),
        ],
        cwd=cwd,
        environment=environment,
        capture=True,
        timeout=30,
    )
    value = json.loads(output)
    if not isinstance(value, dict):
        raise VerificationError("installed operator probe did not return an object")
    return value


def _run_installed_operator_python_suite(
    python: Path,
    operator_root: Path,
    runtime_directory: Path,
    *,
    expected_python_minor: str,
    uv: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Exercise the exact operator wheel on one supported interpreter."""

    probe = _probe_installed_operator_application(
        python,
        environment=environment,
        cwd=runtime_directory,
    )
    version = probe.get("python")
    if not isinstance(version, str) or not version.startswith(f"{expected_python_minor}."):
        raise VerificationError("installed operator probe used an unexpected Python version")
    _install_requirements(
        python,
        operator_root / "requirements-dev.txt",
        uv=uv,
        environment=environment,
        cwd=operator_root,
    )
    _run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            "pyproject.toml",
            "backend/tests",
        ],
        cwd=operator_root,
        environment=environment,
        timeout=300,
    )
    final_probe = _probe_installed_operator_application(
        python,
        environment=environment,
        cwd=runtime_directory,
    )
    if final_probe != probe:
        raise VerificationError("installed operator changed during the supported-Python suite")
    return final_probe


def _docker_resource_exists(
    docker: str,
    resource: str,
    identifier: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> bool:
    try:
        result = subprocess.run(
            [docker, resource, "inspect", identifier],
            cwd=cwd,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise VerificationError(f"Docker {resource} inventory check timed out") from None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise VerificationError(f"Docker {resource} inventory check failed")


def _run_operator_container_gate(
    operator_root: Path,
    wheel: Path,
    operator_wheel: Path,
    *,
    environment: Mapping[str, str],
) -> dict[str, bool]:
    docker = shutil.which("docker")
    if docker is None:
        raise VerificationError("Docker is required for the operator application gate")
    wheelhouse = operator_root / "wheelhouse"
    wheelhouse.mkdir()
    staged_wheel = wheelhouse / wheel.name
    staged_operator_wheel = wheelhouse / operator_wheel.name
    shutil.copy2(wheel, staged_wheel)
    shutil.copy2(operator_wheel, staged_operator_wheel)
    identifier = f"{sha256_file(wheel)[:6]}{sha256_file(operator_wheel)[:6]}-{os.getpid()}"
    tag = f"picogrid-ecn-operator-verify:{identifier}"
    container_name = f"picogrid-ecn-operator-verify-{identifier}"
    if _docker_resource_exists(docker, "image", tag, cwd=operator_root, environment=environment):
        raise VerificationError("operator verification image tag already exists")
    if _docker_resource_exists(
        docker,
        "container",
        container_name,
        cwd=operator_root,
        environment=environment,
    ):
        raise VerificationError("operator verification container name already exists")

    compose_environment = dict(environment)
    compose_environment.update(
        {
            "CLIENT_WHEEL": wheel.name,
            "OPERATOR_WHEEL": operator_wheel.name,
            "OPERATOR_CA_CERT_HOST": "/tmp/operator-placeholder-ca.crt",
            "OPERATOR_CLIENT_CERT_HOST": "/tmp/operator-placeholder-client.crt",
            "OPERATOR_CLIENT_KEY_HOST": "/tmp/operator-placeholder-client.key",
            "OPERATOR_CONTAINER_GID": "65532",
            "OPERATOR_CONTAINER_UID": "65532",
        }
    )
    shutil.copy2(operator_root / ".env.example", operator_root / ".env")
    _run(
        [docker, "compose", "--env-file", ".env.example", "config", "--quiet"],
        cwd=operator_root,
        environment=compose_environment,
        timeout=60,
    )
    body_completed = False
    cleanup_error: VerificationError | None = None
    try:
        with resource("network"):
            _run(
                [
                    docker,
                    "build",
                    "--build-arg",
                    f"CLIENT_WHEEL={wheel.name}",
                    "--build-arg",
                    f"OPERATOR_WHEEL={operator_wheel.name}",
                    "--tag",
                    tag,
                    ".",
                ],
                cwd=operator_root,
                environment=environment,
                timeout=900,
            )
        _run(
            [
                docker,
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--entrypoint",
                "python",
                tag,
                "-I",
                "-c",
                "import operator_app, picogrid_ecn_client",
            ],
            cwd=operator_root,
            environment=environment,
            timeout=60,
        )
        body_completed = True
    finally:
        try:
            if _docker_resource_exists(
                docker,
                "container",
                container_name,
                cwd=operator_root,
                environment=environment,
            ):
                _run(
                    [docker, "container", "rm", "--force", container_name],
                    cwd=operator_root,
                    environment=environment,
                    timeout=60,
                )
            if _docker_resource_exists(
                docker, "image", tag, cwd=operator_root, environment=environment
            ):
                _run(
                    [docker, "image", "rm", tag],
                    cwd=operator_root,
                    environment=environment,
                    timeout=120,
                )
            final_container = _docker_resource_exists(
                docker,
                "container",
                container_name,
                cwd=operator_root,
                environment=environment,
            )
            final_image = _docker_resource_exists(
                docker, "image", tag, cwd=operator_root, environment=environment
            )
            if final_container or final_image:
                raise VerificationError("operator container gate left Docker resources behind")
        except VerificationError as error:
            cleanup_error = error
        if body_completed and cleanup_error is not None:
            raise cleanup_error
    return {
        "compose_configuration": True,
        "container_removed": True,
        "image_built": True,
        "image_import_smoke": True,
        "image_removed": True,
    }


OPERATOR_CONSOLE_PORT = 8080
OPERATOR_SCREENSHOT_PORT = 4173


def _await_local_port_released(port: int, *, timeout: float = 60.0) -> None:
    """Block until nothing is accepting connections on a loopback port.

    The installed console smoke and the operator end-to-end suite both bind the
    same fixed port. Running the gate's phases as separate stages shortened the
    gap between them, so a listener that has not finished closing surfaced as an
    opaque Playwright "port is already used" error. Waiting here turns that race
    into a bounded wait that names the real problem if a port is truly squatted.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return
        if time.monotonic() >= deadline:
            raise VerificationError(
                f"loopback port {port} is still serving after {timeout:.0f}s; "
                "a previous operator console or end-to-end server did not shut down"
            )
        time.sleep(0.1)


def _run_installed_operator_console_smoke(
    runtime_directory: Path,
    runtime_python: Path,
    *,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Launch the documented installed console entry point in bounded mock mode."""

    entry_point = runtime_python.parent / "picogrid-ecn"
    if not entry_point.is_file() or entry_point.is_symlink():
        raise VerificationError("installed client console entry point is missing or unsafe")
    if runtime_directory.is_symlink() or not runtime_directory.is_dir():
        raise VerificationError("installed operator runtime directory is unsafe")
    process_environment = {
        name: value for name, value in environment.items() if not name.startswith("OPERATOR_")
    }
    process_environment["PATH"] = f"{runtime_python.parent}{os.pathsep}/usr/bin:/bin"
    if shutil.which("npm", path=process_environment["PATH"]) is not None:
        raise VerificationError("installed operator runtime unexpectedly exposes npm")
    with tempfile.TemporaryFile(prefix="picogrid-operator-console-") as output:
        process = subprocess.Popen(
            [str(entry_point), "operator", "--demo"],
            cwd=runtime_directory,
            env=process_environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        healthy = False
        packaged_asset = False
        graceful_shutdown = True
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with socket.create_connection(
                        ("127.0.0.1", OPERATOR_CONSOLE_PORT), timeout=0.5
                    ) as connection:
                        connection.sendall(
                            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                        )
                        response = connection.recv(64)
                    if response.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                        healthy = True
                        break
                except (ConnectionError, OSError, TimeoutError):
                    time.sleep(0.1)
            if not healthy:
                raise VerificationError("installed operator console did not become healthy")
            with socket.create_connection(("127.0.0.1", OPERATOR_CONSOLE_PORT), timeout=2) as conn:
                conn.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                index_response = b""
                while chunk := conn.recv(65536):
                    index_response += chunk
                    if len(index_response) > 512 * 1024:
                        raise VerificationError("installed operator page returned excessive bytes")
            match = re.search(
                rb'(?:src|href)="(?P<asset>/assets/[^"?\s]+)"',
                index_response,
            )
            if match is None:
                raise VerificationError("installed operator page omitted its packaged asset")
            asset = match.group("asset")
            with socket.create_connection(("127.0.0.1", OPERATOR_CONSOLE_PORT), timeout=2) as conn:
                conn.sendall(
                    b"GET " + asset + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                asset_response = conn.recv(64)
            packaged_asset = asset_response.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200"))
            if not packaged_asset:
                raise VerificationError("installed operator packaged asset was not served")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    graceful_shutdown = False
                    process.kill()
                    process.wait(timeout=5)
            output.flush()
            output.seek(0, os.SEEK_END)
            output_size = output.tell()
            output.seek(0)
            console_output = output.read(128 * 1024)
    if not graceful_shutdown:
        raise VerificationError("installed operator console did not stop gracefully")
    if output_size > len(console_output):
        raise VerificationError("installed operator console produced excessive output")
    if (
        b"Application shutdown complete." not in console_output
        or b"Application shutdown failed." in console_output
        or b"Traceback (most recent call last)" in console_output
    ):
        raise VerificationError("installed operator console did not confirm clean shutdown")
    # Uvicorn 0.35 deliberately re-raises the captured signal after completing
    # application shutdown, so POSIX reports -SIGTERM for a clean termination.
    expected_return_codes = {0}
    if os.name == "posix":
        expected_return_codes.add(-signal.SIGTERM)
    if process.returncode not in expected_return_codes:
        raise VerificationError("installed operator console returned a failed shutdown status")
    # Do not report success until the console has actually surrendered the port,
    # so a later stage binding it cannot race this shutdown.
    _await_local_port_released(OPERATOR_CONSOLE_PORT)
    return {
        "entry_point": "picogrid-ecn operator",
        "external_working_directory": True,
        "health_check": True,
        "mock_only": True,
        "node_runtime_required": False,
        "packaged_asset": packaged_asset,
        "shutdown_confirmed": True,
        "process_stopped": True,
    }


def _inspect_operator_screenshot(
    path: Path,
    policy: dict[str, Any],
    *,
    expected_size: tuple[int, int] = (1440, 920),
) -> dict[str, Any]:
    """Require the exact bounded, metadata-free viewport screenshot shape."""

    if path.is_symlink() or not path.is_file():
        raise VerificationError("operator publication screenshot is missing or unsafe")
    data = path.read_bytes()
    if not 1_000 <= len(data) <= 2_000_000:
        raise VerificationError("operator publication screenshot has an invalid size")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VerificationError("operator publication screenshot is not a PNG")
    try:
        scan_secret_and_address_content(path.as_posix(), data, policy)
    except ArtifactPolicyError as exc:
        raise VerificationError("operator publication screenshot failed secret scan") from exc

    cursor = 8
    chunk_types: list[str] = []
    width = height = 0
    while cursor < len(data):
        if cursor + 12 > len(data):
            raise VerificationError("operator publication screenshot is truncated")
        length = struct.unpack(">I", data[cursor : cursor + 4])[0]
        end = cursor + 12 + length
        if end > len(data):
            raise VerificationError("operator publication screenshot has a truncated chunk")
        chunk_type_bytes = data[cursor + 4 : cursor + 8]
        chunk_data = data[cursor + 8 : cursor + 8 + length]
        expected_crc = struct.unpack(">I", data[cursor + 8 + length : end])[0]
        actual_crc = zlib.crc32(chunk_type_bytes)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise VerificationError("operator publication screenshot has an invalid checksum")
        try:
            chunk_type = chunk_type_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise VerificationError("operator publication screenshot has an invalid chunk") from exc
        if chunk_type not in {"IDAT", "IEND", "IHDR"}:
            raise VerificationError("operator publication screenshot contains metadata")
        chunk_types.append(chunk_type)
        if chunk_type == "IHDR":
            if len(chunk_types) != 1 or length != 13:
                raise VerificationError("operator publication screenshot has an invalid header")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (
                (width, height) != expected_size
                or bit_depth != 8
                or color_type != 2
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise VerificationError(
                    "operator publication screenshot shape changed: "
                    f"expected {expected_size[0]}x{expected_size[1]} RGB8, "
                    f"got {width}x{height} depth={bit_depth} color={color_type}"
                )
        cursor = end
        if chunk_type == "IEND":
            break
    if (
        cursor != len(data)
        or not chunk_types
        or chunk_types[0] != "IHDR"
        or chunk_types[-1] != "IEND"
        or "IDAT" not in chunk_types
        or chunk_types.count("IHDR") != 1
        or chunk_types.count("IEND") != 1
    ):
        raise VerificationError("operator publication screenshot structure is invalid")
    return {
        "bytes": len(data),
        "height": height,
        "metadata_chunks": 0,
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": width,
    }


def _validate_operator_wheel_license(
    operator_wheel: Path,
    operator_license: Path,
    policy: dict[str, Any],
) -> None:
    with zipfile.ZipFile(operator_wheel) as archive:
        names = archive.namelist()
        metadata_members = tuple(name for name in names if name.endswith(".dist-info/METADATA"))
        if len(metadata_members) != 1:
            raise VerificationError(
                f"operator application wheel {operator_wheel.name!r} "
                f"must contain exactly one METADATA file, found {len(metadata_members)}"
            )
        # The license must live in the same .dist-info directory as the metadata that
        # declares it. Selecting the two independently would accept a wheel carrying a
        # canonical license under an unrelated .dist-info while its own distribution
        # ships none.
        dist_info = metadata_members[0][: -len("METADATA")]
        expected_member = f"{dist_info}licenses/LICENSE"
        license_members = tuple(
            name for name in names if name.endswith(".dist-info/licenses/LICENSE")
        )
        if license_members != (expected_member,):
            raise VerificationError(
                f"operator application wheel {operator_wheel.name!r} must contain exactly "
                f"one license member at {expected_member!r}, found {list(license_members)}"
            )
        if archive.read(expected_member) != operator_license.read_bytes():
            raise VerificationError("operator application wheel omitted its declared license")

        metadata = BytesParser().parsebytes(archive.read(metadata_members[0]))
        expected_license_expression = _string_value(policy, "license_expression")
        actual_license_expression = metadata.get("License-Expression")
        if actual_license_expression != expected_license_expression:
            raise VerificationError(
                f"operator application wheel {operator_wheel.name!r} "
                "License-Expression mismatch: "
                f"expected {expected_license_expression!r}, got {actual_license_expression!r}"
            )


@dataclass(frozen=True)
class _OperatorApplicationState:
    """Browser-independent operator results handed to the browser phase.

    Splitting the gate lets its backend, lint, test, and frontend-build work run
    while the shared Chromium download is still in flight. The freeze is shallow;
    mapping fields are read-only by convention.
    """

    source: Path
    operator_root: Path
    operator_wheel: Path
    operator_wheel_inspection: ArtifactInspection
    operator_reproducible_digest: str
    wheel: Path
    runtime_directory: Path
    runtime_entry_point: Path
    commands_file: Path
    npm: str
    npm_environment: dict[str, str]
    screenshot_specs: tuple[tuple[str, tuple[int, int]], ...]
    committed_screenshots: dict[str, Any]
    source_digest: str
    operator_probe: dict[str, Any]
    documented_install_probe: dict[str, Any]
    console_smoke: dict[str, Any]
    npm_audit: dict[str, Any]
    frontend: dict[str, Any]
    frontend_sbom: dict[str, Any]


def _run_operator_application_gate(
    temporary_root: Path,
    source_snapshot: Path,
    wheel: Path,
    staging: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    policy: dict[str, Any],
) -> _OperatorApplicationState:
    source = source_snapshot / "operator-app"
    screenshot_specs = (
        ("operator-mock-light.png", (1440, 920)),
        ("operator-mock-mobile-dark.png", (390, 844)),
        ("operator-mock-mobile-light.png", (390, 844)),
        ("operator-mock.png", (1440, 920)),
    )
    required = (
        ".dockerignore",
        ".env.example",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "build_backend.py",
        "compose.yaml",
        "docs/operator-mock-light.png",
        "docs/operator-mock-mobile-dark.png",
        "docs/operator-mock-mobile-light.png",
        "docs/operator-mock.png",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        "requirements-build.in",
        "requirements-build.txt",
        "requirements-dev.txt",
        "requirements.txt",
        "backend/tests/test_packaged_frontend.py",
        "tests/playwright.config.ts",
        "tests/publication-screenshot.spec.ts",
        "tests/screenshot.config.ts",
    )
    if (
        source.is_symlink()
        or not source.is_dir()
        or (source_snapshot / "LICENSE").is_symlink()
        or not (source_snapshot / "LICENSE").is_file()
        or any(not (source / relative).is_file() for relative in required)
    ):
        raise VerificationError("immutable source snapshot omitted the operator application")
    operator_root = temporary_root / "operator-application"
    shutil.copytree(source, operator_root, copy_function=shutil.copy2)
    shutil.copy2(source_snapshot / "LICENSE", operator_root / "LICENSE")
    source_digest = _source_tree_digest(operator_root)
    committed_screenshots = {
        name: _inspect_operator_screenshot(
            operator_root / "docs" / name,
            policy,
            expected_size=size,
        )
        for name, size in screenshot_specs
    }

    npm = shutil.which("npm")
    if npm is None:
        raise VerificationError("npm is required for the operator application gate")
    npm_environment = dict(environment)
    npm_environment.update(
        {
            "CI": "1",
            "PLAYWRIGHT_BROWSERS_PATH": str(_playwright_browsers_path(temporary_root)),
            "npm_config_audit": "false",
            "npm_config_cache": str(temporary_root / "operator-npm-cache"),
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }
    )
    _require_operator_frontend_toolchain(
        operator_root,
        npm=npm,
        environment=npm_environment,
    )
    with resource("network"):
        _run(
            [npm, "ci", "--ignore-scripts"],
            cwd=operator_root,
            environment=npm_environment,
            timeout=600,
        )
    audit_environment = dict(npm_environment)
    audit_environment.pop("npm_config_audit")
    npm_audit = _run_npm_audit(
        npm,
        operator_root,
        environment=audit_environment,
        report=REPORT_DIRECTORY / "operator-npm-audit.json",
    )
    _run(
        [npm, "run", "build"],
        cwd=operator_root,
        environment=npm_environment,
        timeout=300,
    )
    frontend_directory = operator_root / "frontend" / "dist"
    frontend = _inspect_generated_web_tree(
        frontend_directory,
        policy,
        label="operator frontend",
    )
    if "index.html" not in frontend["files"]:
        raise VerificationError("operator frontend omitted its entry page")
    frontend_sbom = _generate_npm_sbom(
        npm,
        operator_root,
        environment=npm_environment,
        report=REPORT_DIRECTORY / "operator-frontend-sbom.cdx.json",
    )

    build_python = _create_environment(
        temporary_root / "operator-build-environment",
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    _install_requirements(
        build_python,
        REPOSITORY / "scripts" / "build-requirements.txt",
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    build_environment = dict(environment)
    build_environment["PICOGRID_OPERATOR_PREBUILT_FRONTEND"] = str(frontend_directory)
    built_operator_wheels: list[Path] = []
    for index in (1, 2):
        operator_dist = temporary_root / f"operator-dist-{index}"
        operator_dist.mkdir()
        _run(
            [
                str(build_python),
                "-I",
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(operator_dist),
                str(operator_root),
            ],
            cwd=temporary_root,
            environment=build_environment,
            timeout=300,
        )
        operator_wheels = tuple(operator_dist.glob("picogrid_ecn_operator_app-*.whl"))
        if len(operator_wheels) != 1 or operator_wheels[0].is_symlink():
            raise VerificationError("operator application build did not produce exactly one wheel")
        built_operator_wheels.append(operator_wheels[0])
    operator_wheel, rebuilt_operator_wheel = built_operator_wheels
    if operator_wheel.name != rebuilt_operator_wheel.name or sha256_file(
        operator_wheel
    ) != sha256_file(rebuilt_operator_wheel):
        raise VerificationError("operator wheel is not byte-for-byte reproducible")
    operator_reproducible_digest = sha256_file(operator_wheel)
    try:
        operator_wheel_inspection = inspect_operator_wheel(operator_wheel, policy)
    except ArtifactPolicyError as exc:
        raise VerificationError(str(exc)) from exc
    if operator_wheel_inspection.sha256 != operator_reproducible_digest:
        raise VerificationError("operator wheel inspection digest changed")
    embedded_frontend = _require_operator_frontend_matches(
        operator_wheel,
        frontend_directory,
        policy,
    )
    if embedded_frontend != frontend:
        raise VerificationError("operator embedded frontend inspection changed")

    documentation_python = _create_environment(
        temporary_root / "operator-documentation-install-environment",
        uv=uv,
        environment=environment,
        cwd=temporary_root,
        seed=True,
    )
    documented_install_probe = _run_documented_operator_installation_smoke(
        documentation_python,
        wheel,
        operator_wheel,
        temporary_root,
        environment=environment,
        version=str(policy["project_version"]),
    )
    _validate_operator_wheel_license(operator_wheel, operator_root / "LICENSE", policy)

    runtime_root = temporary_root / "operator-runtime-environment"
    runtime_python = _create_environment(
        runtime_root,
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    _install_requirements(
        runtime_python,
        operator_root / "requirements.txt",
        uv=uv,
        environment=environment,
        cwd=operator_root,
    )
    _install_exact_wheel(
        runtime_python,
        wheel,
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    _install_exact_wheel(
        runtime_python,
        operator_wheel,
        uv=uv,
        environment=environment,
        cwd=temporary_root,
    )
    operator_probe = _probe_installed_operator_application(
        runtime_python,
        environment=environment,
        cwd=temporary_root,
    )
    runtime_directory = temporary_root / "operator-installed-runtime"
    runtime_directory.mkdir()
    commands_file = runtime_directory / "commands.json"
    shutil.copy2(operator_root / "config" / "commands.example.json", commands_file)
    console_smoke = _run_installed_operator_console_smoke(
        runtime_directory,
        runtime_python,
        environment=environment,
    )
    dependency_probe = _probe_installed_wheel(
        runtime_python,
        staging,
        environment=environment,
    )
    _generate_operator_supply_chain_reports(
        runtime_python,
        operator_root / "requirements.txt",
        dependency_probe,
        environment=environment,
    )

    _install_requirements(
        runtime_python,
        operator_root / "requirements-dev.txt",
        uv=uv,
        environment=environment,
        cwd=operator_root,
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "backend",
            "build_backend.py",
        ],
        cwd=operator_root,
        environment=environment,
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "ruff",
            "format",
            "--check",
            "--no-cache",
            "backend",
            "build_backend.py",
        ],
        cwd=operator_root,
        environment=environment,
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "mypy",
            "--no-incremental",
            "backend/operator_app",
            "build_backend.py",
        ],
        cwd=operator_root,
        environment=environment,
    )
    _run(
        [
            str(runtime_python),
            "-I",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            "pyproject.toml",
            "backend/tests",
        ],
        cwd=operator_root,
        environment=environment,
        timeout=300,
    )

    runtime_entry_point = runtime_python.parent / "picogrid-ecn-operator"
    runtime_path = f"{runtime_python.parent}{os.pathsep}/usr/bin:/bin"
    npm_environment.update(
        {
            "OPERATOR_TEST_COMMAND": shlex.quote(str(runtime_entry_point)),
            "OPERATOR_TEST_COMMANDS_FILE": str(commands_file),
            "OPERATOR_TEST_PYTHON": str(runtime_python),
            "OPERATOR_TEST_RUNTIME_DIR": str(runtime_directory),
            "OPERATOR_TEST_RUNTIME_PATH": runtime_path,
        }
    )
    return _OperatorApplicationState(
        source=source,
        operator_root=operator_root,
        operator_wheel=operator_wheel,
        operator_wheel_inspection=operator_wheel_inspection,
        operator_reproducible_digest=operator_reproducible_digest,
        wheel=wheel,
        runtime_directory=runtime_directory,
        runtime_entry_point=runtime_entry_point,
        commands_file=commands_file,
        npm=npm,
        npm_environment=npm_environment,
        screenshot_specs=screenshot_specs,
        committed_screenshots=committed_screenshots,
        source_digest=source_digest,
        operator_probe=operator_probe,
        documented_install_probe=documented_install_probe,
        console_smoke=console_smoke,
        npm_audit=npm_audit,
        frontend=frontend,
        frontend_sbom=frontend_sbom,
    )


def _run_operator_browser_gate(
    state: _OperatorApplicationState,
    *,
    environment: Mapping[str, str],
    policy: dict[str, Any],
    supported_python_probes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Run the operator gates that need the shared Chromium build, then publish."""

    if sha256_file(state.operator_wheel) != state.operator_reproducible_digest:
        raise VerificationError("operator wheel changed after reproducibility verification")
    if set(supported_python_probes) != {"3.11", "3.12", "3.13", "3.14"}:
        raise VerificationError("installed operator supported-Python evidence is incomplete")
    for expected_minor, probe in supported_python_probes.items():
        version = probe.get("python")
        if not isinstance(version, str) or not version.startswith(f"{expected_minor}."):
            raise VerificationError("installed operator supported-Python evidence is inconsistent")

    _await_local_port_released(OPERATOR_SCREENSHOT_PORT)
    try:
        _run(
            [state.npm, "run", "screenshot:generate"],
            cwd=state.operator_root,
            environment=state.npm_environment,
            timeout=300,
        )
    finally:
        _await_local_port_released(OPERATOR_SCREENSHOT_PORT)
    regenerated_screenshots = {
        name: _inspect_operator_screenshot(
            state.operator_root / "docs" / name,
            policy,
            expected_size=size,
        )
        for name, size in state.screenshot_specs
    }
    # The end-to-end suite starts its own server on the same fixed loopback port.
    _await_local_port_released(OPERATOR_CONSOLE_PORT)
    try:
        _run(
            [state.npm, "run", "test:e2e:built"],
            cwd=state.operator_root,
            environment=state.npm_environment,
            timeout=300,
        )
    finally:
        _await_local_port_released(OPERATOR_CONSOLE_PORT)
    runtime_entries = tuple(state.runtime_directory.iterdir())
    if {path.name for path in runtime_entries} != {state.commands_file.name} or any(
        path.is_symlink() or not path.is_file() for path in runtime_entries
    ):
        raise VerificationError("installed operator browser test left runtime resources behind")
    container = _run_operator_container_gate(
        state.operator_root,
        state.wheel,
        state.operator_wheel,
        environment=environment,
    )
    inspection = {
        "browser_mock_workflow": True,
        "browser_server_released": True,
        "client_wheel_sha256": sha256_file(state.wheel),
        "console_smoke": state.console_smoke,
        "container": container,
        "documented_two_wheel_install": state.documented_install_probe,
        "frontend": state.frontend,
        "frontend_sbom": state.frontend_sbom,
        "npm_audit": state.npm_audit,
        "operator_application_sha256": state.source_digest,
        "operator_build_requirements_sha256": sha256_file(state.source / "requirements-build.txt"),
        "operator_package_lock_sha256": sha256_file(state.source / "package-lock.json"),
        "operator_probe": state.operator_probe,
        "operator_supported_python_probes": {
            version: dict(probe) for version, probe in sorted(supported_python_probes.items())
        },
        "operator_wheel_inspection": state.operator_wheel_inspection.to_dict(),
        "operator_wheel_reproducible": True,
        "publication_screenshots": {
            "committed": state.committed_screenshots,
            "pixel_hash_is_informational_across_platforms": True,
            "regenerated": regenerated_screenshots,
            "same_bytes_in_verification_environment": all(
                state.committed_screenshots[name]["sha256"]
                == regenerated_screenshots[name]["sha256"]
                for name, _ in state.screenshot_specs
            ),
        },
        "operator_requirements_dev_sha256": sha256_file(state.source / "requirements-dev.txt"),
        "operator_requirements_sha256": sha256_file(state.source / "requirements.txt"),
        "operator_wheel_sha256": sha256_file(state.operator_wheel),
        "status": "pass",
        "screenshot_server_released": True,
    }
    _write_json(REPORT_DIRECTORY / "operator-inspection.json", inspection)
    return inspection


def _git_value(arguments: Sequence[str], environment: Mapping[str, str]) -> str:
    return _run(
        ["git", *arguments],
        cwd=REPOSITORY,
        environment=environment,
        capture=True,
    ).strip()


def _require_clean_release_worktree(status: str) -> None:
    """Refuse to verify artifacts that cannot be attributed to one commit."""

    if status:
        raise VerificationError("release verification requires a clean Git worktree")


def _release_tag(environment: Mapping[str, str], project_version: str) -> str:
    """Name the tag this candidate publishes as, or nothing.

    The tag is injected, never discovered. Reading it from the checkout would
    make the built guide depend on when the build ran rather than on which
    commit it built: the same commit verified before and after the release tag
    appears would produce different bytes, and the Pages deployment publishes
    whichever of the two happened to be kept.
    """

    tag = environment.get("RELEASE_TAG", "").strip()
    if not tag:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise VerificationError("the release tag is not an ordinary Git tag name")
    expected_tag = f"v{project_version}"
    if tag != expected_tag:
        raise VerificationError(
            f"release tag {tag!r} does not match policy project version "
            f"{project_version!r}; expected {expected_tag!r}"
        )
    return tag


def _write_final_reports(
    *,
    wheel: Path,
    sdist: Path,
    operator_wheel: Path,
    source_digest: str,
    reproducible_digests: dict[str, str],
    inspections: Sequence[ArtifactInspection],
    documentation: DocumentationInspection,
    operator_inspection: Mapping[str, Any],
    site_inspection: Mapping[str, Any],
    source_date_epoch: int,
    probe: dict[str, Any],
    python_312_probe: dict[str, Any],
    python_313_probe: dict[str, Any],
    python_314_probe: dict[str, Any],
    worktree_scan: dict[str, int],
    verified_inputs_digest: str,
    build_requirements_digest: str,
    release_policy_digest: str,
    uv_lock_digest: str,
    git_commit: str,
    git_tag: str,
    git_worktree_dirty: bool,
) -> None:
    checksums = {
        wheel.name: sha256_file(wheel),
        sdist.name: sha256_file(sdist),
        operator_wheel.name: sha256_file(operator_wheel),
    }
    if checksums[operator_wheel.name] != operator_inspection.get("operator_wheel_sha256"):
        raise VerificationError("promoted operator wheel differs from its inspected input")
    checksum_lines = [f"{digest}  {name}" for name, digest in sorted(checksums.items())]
    (REPORT_DIRECTORY / "checksums.sha256").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    _write_json(
        REPORT_DIRECTORY / "artifact-inspection.json",
        {"artifacts": [inspection.to_dict() for inspection in inspections], "status": "pass"},
    )
    _write_json(
        REPORT_DIRECTORY / "documentation-inspection.json",
        {**documentation.to_dict(), "status": "pass"},
    )
    _write_json(
        REPORT_DIRECTORY / "site-inspection.json",
        {**site_inspection, "status": "pass"},
    )
    _write_json(
        REPORT_DIRECTORY / "reproducibility.json",
        {
            "artifacts": {
                **reproducible_digests,
                operator_wheel.name: checksums[operator_wheel.name],
            },
            "byte_for_byte_rebuild": True,
            "source_date_epoch": source_date_epoch,
            "status": "pass",
        },
    )
    reproducibility_epoch = datetime.fromtimestamp(source_date_epoch, tz=UTC).isoformat()
    _write_json(
        REPORT_DIRECTORY / "provenance.json",
        {
            "build_type": "local-picogrid-public-wheel-verification-1",
            "builder": "make verify-release",
            "reproducibility_epoch": reproducibility_epoch,
            "invocation": {
                "build_requirements_sha256": build_requirements_digest,
                "release_policy_sha256": release_policy_digest,
                "source_date_epoch": source_date_epoch,
                "uv_lock_sha256": uv_lock_digest,
                "verified_inputs_sha256": verified_inputs_digest,
            },
            "materials": {
                "git_commit": git_commit,
                "git_tag": git_tag,
                "git_worktree_dirty": git_worktree_dirty,
                "operator_application_sha256": operator_inspection["operator_application_sha256"],
                "operator_build_requirements_sha256": operator_inspection[
                    "operator_build_requirements_sha256"
                ],
                "operator_package_lock_sha256": operator_inspection["operator_package_lock_sha256"],
                "site_tree_sha256": site_inspection["sha256"],
                "source_snapshot_sha256": source_digest,
                "verified_inputs_sha256": verified_inputs_digest,
            },
            "reproducibility": {"byte_for_byte_rebuild": True},
            "subjects": checksums,
        },
    )
    _write_json(
        REPORT_DIRECTORY / "verification-summary.json",
        {
            "coverage_threshold_percent": 80,
            "exact_wheel": wheel.name,
            "exact_operator_wheel": operator_wheel.name,
            "gates": [
                "git-visible-worktree-secret-address-and-ignored-file-scan",
                "generated-python-reference-parity-and-docstring-completeness",
                "immutable-verified-input-drift-guard",
                "ruff-lint-and-format",
                "strict-mypy",
                "source-tests-with-branch-coverage",
                "hash-pinned-build-runtime-and-test-environments",
                "byte-for-byte-wheel-and-sdist-rebuild",
                "artifact-allowlist-denylist-content-secret-inspection",
                "exact-final-dist-client-wheel-operator-wheel-and-sdist-inventory",
                "generated-report-allowlist-secret-address-and-path-inspection",
                "released-docs-links-snippets-commands-and-example-coverage",
                "clean-two-build-static-site-check-and-promotion",
                "documented-dependency-resolving-wheel-install-command",
                "runtime-vulnerability-audit",
                "reproducible-cyclonedx-sbom",
                "python-3.11-exact-wheel-installed-suite",
                "python-3.12-exact-wheel-installed-suite",
                "python-3.13-exact-wheel-installed-suite",
                "python-3.14-exact-wheel-installed-suite",
                "clean-python-3.13-exact-wheel-shutdown-regression",
                "installed-wheel-unit-contract-e2e-performance-and-example-tests-on-all-supported-python-minors",
                "source-installed-wheel-python-reference-inventory-parity",
                "python-3.11-3.14-installed-wheel-type-completeness",
                "installed-mock-process-and-socket-cleanup",
                "installed-wheel-example-check-modes",
                "installed-wheel-operator-python-browser-and-container-workflows",
                "operator-python-and-javascript-audit-and-sbom",
            ],
            "installed_probe": {
                key: probe[key]
                for key in (
                    "import_origin",
                    "project_name",
                    "project_version",
                    "python",
                    "repository_on_sys_path",
                )
            },
            "released_documentation": documentation.to_dict(),
            "operator_application": dict(operator_inspection),
            "site": dict(site_inspection),
            "python_312_probe": {
                key: python_312_probe[key]
                for key in (
                    "import_origin",
                    "project_name",
                    "project_version",
                    "python",
                    "repository_on_sys_path",
                )
            },
            "python_313_probe": {
                key: python_313_probe[key]
                for key in (
                    "import_origin",
                    "project_name",
                    "project_version",
                    "python",
                    "repository_on_sys_path",
                )
            },
            "python_314_probe": {
                key: python_314_probe[key]
                for key in (
                    "import_origin",
                    "project_name",
                    "project_version",
                    "python",
                    "repository_on_sys_path",
                )
            },
            "status": "pass",
            "worktree_scan": worktree_scan,
        },
    )


def _inspect_generated_report_entries(entries: Sequence[Path], policy: dict[str, Any]) -> None:
    if any(not path.is_file() or path.is_symlink() for path in entries):
        raise VerificationError("generated release reports contain an unsupported entry")
    repository_bytes = str(REPOSITORY.resolve()).encode()
    for path in sorted(entries):
        data = path.read_bytes()
        if repository_bytes in data:
            raise VerificationError("generated release report contains a local repository path")
        if any(marker in data.lower() for marker in _RETIRED_GENERATED_CONTENT_MARKERS):
            raise VerificationError("generated release report failed retired protocol marker scan")
        try:
            scan_secret_and_address_content(
                f"reports/generated/{path.name}",
                data,
                policy,
            )
        except ArtifactPolicyError as exc:
            category = str(exc).split(" found in ", maxsplit=1)[0]
            raise VerificationError(f"generated release report failed {category} scan") from None


def _inspect_preexisting_generated_reports(policy: dict[str, Any]) -> None:
    entries = tuple(REPORT_DIRECTORY.iterdir())
    if not {path.name for path in entries}.issubset(_GENERATED_REPORT_FILES):
        raise VerificationError("generated release report allowlist mismatch")
    for name in ("sbom.cdx.json", "operator-sbom.cdx.json", "operator-frontend-sbom.cdx.json"):
        sbom = REPORT_DIRECTORY / name
        if sbom.is_file() and not sbom.is_symlink():
            _sanitize_sbom(
                sbom,
                allowed_local_projects=(
                    PACKAGE_NAME,
                    OPERATOR_PACKAGE_NAME,
                    OPERATOR_NODE_PACKAGE_NAME,
                ),
            )
    _inspect_generated_report_entries(entries, policy)


def _inspect_generated_reports(policy: dict[str, Any]) -> None:
    """Require the exact report set and reject sensitive or local material."""

    entries = tuple(REPORT_DIRECTORY.iterdir())
    if {path.name for path in entries} != _GENERATED_REPORT_FILES:
        raise VerificationError("generated release report allowlist mismatch")
    _inspect_generated_report_entries(entries, policy)


# The release DAG, as a single introspectable source of truth. ``verify_release``
# builds its ``Stage`` list from this map, and tests/release asserts the edges that
# encode real ordering guarantees rather than the incidental order of source lines.
_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "quality": (),
    "build_inputs": (),
    **({"public_export": ()} if _PUBLIC_EXPORT_TOOLING_PRESENT else {}),
    "build": ("build_inputs",),
    "site_ci1": ("build_inputs",),
    "browsers": ("site_ci1",),
    "docs": ("site_ci1", "browsers"),
    "requirements_runtime": (),
    "requirements_test": (),
    "staging": (),
    "artifact_inspection": ("build",),
    "docsmoke": ("build",),
    # `docsmoke` proves the documented `pip install <wheel>` flow against a pristine
    # seeded environment. Every stage that installs the release artifacts into an
    # environment therefore waits on it, preserving the serial verifier's guarantee
    # that the documented install is exercised before any other installation. These
    # stages are all off the critical path (the shared browser download dominates),
    # so the edges cost nothing. `_INSTALLATION_STAGES` names the same set for the
    # structural test; keep the two in step.
    "runtime": (
        "build",
        "docsmoke",
        "requirements_runtime",
        "requirements_test",
        "staging",
    ),
    "py312": ("build", "docsmoke", "requirements_test", "staging", "operator"),
    "py313": ("build", "docsmoke", "requirements_test", "staging", "operator"),
    "py314": ("build", "docsmoke", "requirements_test", "staging", "operator"),
    "operator": ("build", "build_inputs", "docsmoke", "staging"),
    "operator_web": ("operator", "browsers", "py312", "py313", "py314"),
    # The typing gate installs the exact wheel into isolated Python 3.11, 3.12,
    # 3.13, and 3.14 environments through scripts.verify_types, so it waits on
    # every other installing stage, and on `site_ci1` for the npm-locked pyright.
    "typing": ("build", "site_ci1", "docsmoke"),
    # Reference parity reads the already-installed runtime environment and the
    # immutable snapshot; it installs nothing itself.
    "reference_parity": ("build_inputs", "runtime"),
}

# Stages that install the release artifacts into an environment, i.e. every stage
# that must be ordered after the documented clean-install smoke. A future gate that
# installs the wheel (for example an installed-wheel typing gate) belongs here and
# must carry a `docsmoke` dependency.
_INSTALLATION_STAGES: frozenset[str] = frozenset(
    {"runtime", "py312", "py313", "py314", "operator", "typing"}
)


def _resolve_jobs() -> int:
    raw_jobs = os.environ.get("VERIFY_RELEASE_JOBS", "").strip()
    # Unset, empty, or zero selects a bounded CPU-count-based default.
    if not raw_jobs or raw_jobs == "0":
        return min(8, os.cpu_count() or 1)
    try:
        jobs = int(raw_jobs)
    except ValueError:
        raise VerificationError(
            f"VERIFY_RELEASE_JOBS must be an integer, got {raw_jobs!r}"
        ) from None
    if jobs < 1:
        raise VerificationError("VERIFY_RELEASE_JOBS must be at least 1")
    return jobs


def verify_release(source_date_epoch: int) -> None:
    """Run every release gate against one immutable source snapshot and exact wheel."""

    if sys.version_info[:2] != (3, 11):
        raise VerificationError(
            f"make verify-release must run under Python 3.11, got {sys.version_info[:3]}"
        )
    if source_date_epoch < 315_532_800:
        raise VerificationError("SOURCE_DATE_EPOCH must be at or after 1980-01-01")
    environment = _base_environment(source_date_epoch)
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError("uv is required")
    policy = load_policy(POLICY_PATH)
    _reset_generated_reports(policy)
    _reset_candidate_artifacts(policy)
    documentation_base_path = load_documentation_base_path(
        REPOSITORY / "docs" / "site" / "site-config.mjs"
    )
    _reset_quality_tool_outputs()
    _reset_python_bytecode_outputs()
    _reset_project_egg_info_output()
    _reset_local_web_outputs()
    worktree_scan = _scan_git_visible_worktree(policy, environment)
    _require_safe_local_virtual_environment()
    verified_inputs_digest = _verification_input_digest(environment)
    build_requirements_digest = sha256_file(REPOSITORY / "scripts" / "build-requirements.txt")
    release_policy_digest = sha256_file(POLICY_PATH)
    uv_lock_digest = sha256_file(REPOSITORY / "uv.lock")
    git_commit = _git_value(("rev-parse", "HEAD"), environment)
    git_tag = _release_tag(environment, str(policy["project_version"]))
    git_worktree_status = _git_value(
        ("status", "--porcelain", "--untracked-files=all"), environment
    )
    _require_clean_release_worktree(git_worktree_status)
    git_worktree_dirty = False
    _require_verification_inputs_unchanged(verified_inputs_digest, environment)
    python_312_interpreter = _find_python_minor("3.12", uv, environment)
    python_313_interpreter = _find_python_minor("3.13", uv, environment)
    python_314_interpreter = _find_python_minor("3.14", uv, environment)
    jobs = _resolve_jobs()

    with (
        tempfile.TemporaryDirectory(prefix="picogrid-ecn-quality-") as raw_quality,
        tempfile.TemporaryDirectory(prefix="picogrid-ecn-release-") as raw_temporary,
    ):
        quality_root = Path(raw_quality)
        quality_environment = _quality_gate_environment(environment, quality_root)
        temporary_root = Path(raw_temporary).resolve()
        if REPOSITORY.resolve() in temporary_root.parents:
            raise VerificationError("clean release environments must be outside the repository")

        runtime_requirements = temporary_root / "runtime-requirements.txt"
        test_requirements = temporary_root / "test-requirements.txt"
        staging = temporary_root / "verification-inputs"
        stage_values: dict[str, Any] = {}

        def quality_stage() -> DocumentationInspection:
            documentation = _quality_gates(quality_environment, policy)
            stage_values["quality"] = documentation
            return documentation

        def build_inputs_stage() -> tuple[Path, Path]:
            build_inputs = _prepare_build_inputs(
                temporary_root,
                uv=uv,
                environment=environment,
                policy=policy,
                source_date_epoch=source_date_epoch,
            )
            stage_values["build_inputs"] = build_inputs
            return build_inputs

        def public_export_stage() -> Any:
            public_export = importlib.import_module("scripts.public_export")
            PublicExportError = public_export.PublicExportError
            git_tracked_paths = public_export.git_tracked_paths
            verify_public_export = public_export.verify_public_export

            tracked = git_tracked_paths(REPOSITORY)
            try:
                result = verify_public_export(REPOSITORY, tracked=tracked)
            except PublicExportError as exc:
                emit(str(exc))
                raise VerificationError("public export policy verification failed") from exc
            stage_values["public_export"] = result
            return result

        def build_stage() -> tuple[Path, Path, str, dict[str, str]]:
            build_python, snapshot = stage_values["build_inputs"]
            build_result = _build_artifacts(
                temporary_root,
                build_python,
                snapshot,
                environment=environment,
                source_date_epoch=source_date_epoch,
                jobs=jobs,
            )
            stage_values["build"] = build_result
            return build_result

        def site_ci1_stage() -> Path:
            _, snapshot = stage_values["build_inputs"]
            npm, npm_environment = _site_tooling(
                temporary_root,
                environment,
                git_commit=git_commit,
                git_tag=git_tag,
            )
            context = _prepare_site_context(
                temporary_root,
                snapshot,
                1,
                npm=npm,
                environment=npm_environment,
            )
            stage_values["site_ci1"] = context
            return context

        def browsers_stage() -> Path:
            _, snapshot = stage_values["build_inputs"]
            context = stage_values["site_ci1"]
            browsers_path = _install_docs_browsers(
                temporary_root,
                snapshot,
                context,
                environment=environment,
                git_commit=git_commit,
                git_tag=git_tag,
            )
            stage_values["browsers"] = browsers_path
            return browsers_path

        def docs_stage() -> tuple[Path, dict[str, Any]]:
            _, snapshot = stage_values["build_inputs"]
            context = stage_values["site_ci1"]
            docs_result = _build_docs_site(
                temporary_root,
                snapshot,
                context,
                environment=environment,
                policy=policy,
                documentation_base_path=documentation_base_path,
                jobs=jobs,
                git_commit=git_commit,
                git_tag=git_tag,
            )
            stage_values["docs"] = docs_result
            return docs_result

        def requirements_runtime_stage() -> Path:
            _export_requirements(
                runtime_requirements,
                uv=uv,
                environment=environment,
                include_dev=False,
            )
            stage_values["requirements_runtime"] = runtime_requirements
            return runtime_requirements

        def requirements_test_stage() -> Path:
            _export_requirements(
                test_requirements,
                uv=uv,
                environment=environment,
                include_dev=True,
            )
            stage_values["requirements_test"] = test_requirements
            return test_requirements

        def staging_stage() -> Path:
            staging.mkdir()
            _stage_verification_inputs(staging)
            stage_values["staging"] = staging
            return staging

        def artifact_inspection_stage() -> tuple[ArtifactInspection, ArtifactInspection]:
            wheel, sdist, _, _ = stage_values["build"]
            try:
                inspections = (inspect_wheel(wheel, policy), inspect_sdist(sdist, policy))
            except ArtifactPolicyError as exc:
                raise VerificationError(str(exc)) from exc
            stage_values["artifact_inspection"] = inspections
            return inspections

        def docsmoke_stage() -> None:
            wheel, _, _, _ = stage_values["build"]
            documentation_python = _create_environment(
                temporary_root / "documentation-install-environment",
                uv=uv,
                environment=environment,
                cwd=temporary_root,
                seed=True,
            )
            _run_documented_installation_smoke(
                documentation_python,
                wheel,
                temporary_root,
                environment=environment,
                version=str(policy["project_version"]),
            )

        def runtime_stage() -> dict[str, Any]:
            wheel, _, _, _ = stage_values["build"]
            runtime_python = _create_environment(
                temporary_root / "runtime-environment",
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _install_requirements(
                runtime_python,
                runtime_requirements,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _install_exact_wheel(
                runtime_python,
                wheel,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            probe = _probe_installed_wheel(
                runtime_python,
                staging,
                environment=environment,
            )
            _generate_supply_chain_reports(
                runtime_python,
                runtime_requirements,
                probe,
                environment=environment,
                policy=policy,
            )
            _install_requirements(
                runtime_python,
                test_requirements,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _run_installed_suite(runtime_python, staging, environment=environment)
            stage_values["runtime"] = probe
            stage_values["runtime_python"] = runtime_python
            return probe

        def python_312_stage() -> dict[str, Any]:
            wheel, _, _, _ = stage_values["build"]
            python_312 = _create_environment(
                temporary_root / "python-3.12-environment",
                uv=uv,
                environment=environment,
                cwd=temporary_root,
                interpreter=python_312_interpreter,
            )
            _install_requirements(
                python_312,
                test_requirements,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _install_exact_wheel(
                python_312,
                wheel,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            probe = _probe_installed_wheel(
                python_312,
                staging,
                environment=environment,
                expected_python_minor="3.12",
            )
            _run_installed_suite(python_312, staging, environment=environment)
            operator_state = stage_values["operator"]
            _install_requirements(
                python_312,
                operator_state.operator_root / "requirements.txt",
                uv=uv,
                environment=environment,
                cwd=operator_state.operator_root,
            )
            _install_exact_wheel(
                python_312,
                operator_state.operator_wheel,
                uv=uv,
                environment=environment,
                cwd=operator_state.runtime_directory,
            )
            stage_values["operator_py312"] = _run_installed_operator_python_suite(
                python_312,
                operator_state.operator_root,
                operator_state.runtime_directory,
                expected_python_minor="3.12",
                uv=uv,
                environment=environment,
            )
            stage_values["py312"] = probe
            return probe

        def python_313_stage() -> dict[str, Any]:
            wheel, _, _, _ = stage_values["build"]
            python_313 = _create_environment(
                temporary_root / "python-3.13-environment",
                uv=uv,
                environment=environment,
                cwd=temporary_root,
                interpreter=python_313_interpreter,
            )
            _install_requirements(
                python_313,
                test_requirements,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _install_exact_wheel(
                python_313,
                wheel,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _run_installed_suite(python_313, staging, environment=environment)
            probe = _run_python_313_shutdown_gate(
                python_313,
                staging,
                environment=environment,
            )
            operator_state = stage_values["operator"]
            _install_requirements(
                python_313,
                operator_state.operator_root / "requirements.txt",
                uv=uv,
                environment=environment,
                cwd=operator_state.operator_root,
            )
            _install_exact_wheel(
                python_313,
                operator_state.operator_wheel,
                uv=uv,
                environment=environment,
                cwd=operator_state.runtime_directory,
            )
            stage_values["operator_py313"] = _run_installed_operator_python_suite(
                python_313,
                operator_state.operator_root,
                operator_state.runtime_directory,
                expected_python_minor="3.13",
                uv=uv,
                environment=environment,
            )
            stage_values["py313"] = probe
            return probe

        def python_314_stage() -> dict[str, Any]:
            wheel, _, _, _ = stage_values["build"]
            python_314 = _create_environment(
                temporary_root / "python-3.14-environment",
                uv=uv,
                environment=environment,
                cwd=temporary_root,
                interpreter=python_314_interpreter,
            )
            _install_requirements(
                python_314,
                test_requirements,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            _install_exact_wheel(
                python_314,
                wheel,
                uv=uv,
                environment=environment,
                cwd=temporary_root,
            )
            probe = _probe_installed_wheel(
                python_314,
                staging,
                environment=environment,
                expected_python_minor="3.14",
            )
            _run_installed_suite(python_314, staging, environment=environment)
            operator_state = stage_values["operator"]
            _install_requirements(
                python_314,
                operator_state.operator_root / "requirements.txt",
                uv=uv,
                environment=environment,
                cwd=operator_state.operator_root,
            )
            _install_exact_wheel(
                python_314,
                operator_state.operator_wheel,
                uv=uv,
                environment=environment,
                cwd=operator_state.runtime_directory,
            )
            stage_values["operator_py314"] = _run_installed_operator_python_suite(
                python_314,
                operator_state.operator_root,
                operator_state.runtime_directory,
                expected_python_minor="3.14",
                uv=uv,
                environment=environment,
            )
            stage_values["py314"] = probe
            return probe

        def operator_stage() -> _OperatorApplicationState:
            wheel, _, _, _ = stage_values["build"]
            _, snapshot = stage_values["build_inputs"]
            state = _run_operator_application_gate(
                temporary_root,
                snapshot,
                wheel,
                staging,
                uv=uv,
                environment=environment,
                policy=policy,
            )
            stage_values["operator"] = state
            return state

        def operator_web_stage() -> dict[str, Any]:
            inspection = _run_operator_browser_gate(
                stage_values["operator"],
                environment=environment,
                policy=policy,
                supported_python_probes={
                    "3.11": stage_values["operator"].operator_probe,
                    "3.12": stage_values["operator_py312"],
                    "3.13": stage_values["operator_py313"],
                    "3.14": stage_values["operator_py314"],
                },
            )
            stage_values["operator_web"] = inspection
            return inspection

        def typing_stage() -> None:
            wheel, _, _, _ = stage_values["build"]
            _run_typing_gate(
                wheel,
                uv=uv,
                site_context=stage_values["site_ci1"],
                environment=environment,
            )

        def reference_parity_stage() -> None:
            _, snapshot = stage_values["build_inputs"]
            _run_installed_reference_gate(
                stage_values["runtime_python"],
                snapshot,
                temporary_root,
                uv=uv,
                environment=environment,
            )

        runners: dict[str, Callable[[], Any]] = {
            "quality": quality_stage,
            "build_inputs": build_inputs_stage,
            **({"public_export": public_export_stage} if _PUBLIC_EXPORT_TOOLING_PRESENT else {}),
            "build": build_stage,
            "site_ci1": site_ci1_stage,
            "browsers": browsers_stage,
            "docs": docs_stage,
            "requirements_runtime": requirements_runtime_stage,
            "requirements_test": requirements_test_stage,
            "staging": staging_stage,
            "artifact_inspection": artifact_inspection_stage,
            "docsmoke": docsmoke_stage,
            "runtime": runtime_stage,
            "py312": python_312_stage,
            "py313": python_313_stage,
            "py314": python_314_stage,
            "operator": operator_stage,
            "operator_web": operator_web_stage,
            "typing": typing_stage,
            "reference_parity": reference_parity_stage,
        }
        if runners.keys() != _STAGE_DEPENDENCIES.keys():
            raise VerificationError("release stage runners and dependency map disagree")
        stages = [
            Stage(name, runners[name], dependencies)
            for name, dependencies in _STAGE_DEPENDENCIES.items()
        ]
        # These guards reject net input drift across the entire parallel region.
        _require_verification_inputs_unchanged(verified_inputs_digest, environment)
        # Resource limits are intentionally process-global for the lifetime of this run.
        configure_resources({"network": 1})
        results = run_stages(stages, jobs=jobs)

        _require_safe_local_virtual_environment()
        _reset_project_egg_info_output()
        _reset_python_bytecode_outputs()
        _require_verification_inputs_unchanged(verified_inputs_digest, environment)
        wheel, sdist, source_digest, reproducible_digests = results["build"]
        site, site_inspection = results["docs"]
        operator_state = results["operator"]
        wheel, sdist, operator_wheel = _promote_verified_artifacts(
            wheel,
            sdist,
            operator_state.operator_wheel,
        )
        _promote_verified_site(
            site,
            site_inspection,
            policy,
            documentation_base_path,
        )
        _inspect_final_dist_inventory(wheel, sdist, operator_wheel)
        final_worktree_scan = _scan_git_visible_worktree(policy, environment)
        _require_verification_inputs_unchanged(verified_inputs_digest, environment)
        worktree_scan.update(
            {
                "final_git_visible_files_scanned": final_worktree_scan["git_visible_files_scanned"],
                "final_ignored_generated_files_scanned": final_worktree_scan[
                    "ignored_generated_files_scanned"
                ],
                "final_ignored_files_reviewed": final_worktree_scan["ignored_files_reviewed"],
            }
        )
        _write_final_reports(
            wheel=wheel,
            sdist=sdist,
            operator_wheel=operator_wheel,
            source_digest=source_digest,
            reproducible_digests=reproducible_digests,
            inspections=(
                *results["artifact_inspection"],
                operator_state.operator_wheel_inspection,
            ),
            documentation=results["quality"],
            operator_inspection=results["operator_web"],
            site_inspection=site_inspection,
            source_date_epoch=source_date_epoch,
            probe=results["runtime"],
            python_312_probe=results["py312"],
            python_313_probe=results["py313"],
            python_314_probe=results["py314"],
            worktree_scan=worktree_scan,
            verified_inputs_digest=verified_inputs_digest,
            build_requirements_digest=build_requirements_digest,
            release_policy_digest=release_policy_digest,
            uv_lock_digest=uv_lock_digest,
            git_commit=git_commit,
            git_tag=git_tag,
            git_worktree_dirty=git_worktree_dirty,
        )
        _inspect_generated_reports(policy)
        _require_verification_inputs_unchanged(verified_inputs_digest, environment)

    print("\nRelease verification passed for the exact installed artifacts.", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH)),
    )
    arguments = parser.parse_args(argv)
    verify_release(arguments.source_date_epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
