# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Deterministic artifact inspection helpers used by the release gate."""

from __future__ import annotations

import ast
import hashlib
import importlib
import ipaddress
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
import textwrap
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from packaging.version import InvalidVersion, Version

PACKAGE_NAME = "picogrid-ecn-client"
IMPORT_NAME = "picogrid_ecn_client"
OPERATOR_PACKAGE_NAME = "picogrid-ecn-operator-app"
OPERATOR_DISTRIBUTION_NAME = "picogrid_ecn_operator_app"
OPERATOR_IMPORT_NAME = "operator_app"


def documented_operator_install_argv(version: str) -> tuple[str, ...]:
    """Return the exact documented argv for installing both release wheels."""

    normalized_version = _normalize_version(version)
    return (
        "python",
        "-m",
        "pip",
        "install",
        f"./{IMPORT_NAME}-{normalized_version}-py3-none-any.whl",
        f"./{OPERATOR_DISTRIBUTION_NAME}-{normalized_version}-py3-none-any.whl",
    )


_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".html",
        ".in",
        ".ini",
        ".js",
        ".jsx",
        ".json",
        ".lock",
        ".md",
        ".mjs",
        ".proto",
        ".py",
        ".sh",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_NONPUBLIC_SDK_IMPORT = re.compile(rb"picogrid[_-][a-z0-9_-]+[_-]sdk(?![a-z0-9_-])", re.IGNORECASE)
_UNAPPROVED_PICOGRID_REPOSITORY = re.compile(
    rb"github\.com(?::[0-9]+)?[/:]picogrid/(?!ecn-sdk-python(?:\.git)?(?:[/?#\s\"']|$)|legion-system-auth(?:\.git)?(?:[/?#\s\"']|$))[a-z0-9_.-]+",
    re.IGNORECASE,
)
_PRIVATE_INDEX = re.compile(
    rb"https?://[^\s\"']*(?:packages|pypi|index)[^\s\"']*picogrid[^\s\"']*",
    re.IGNORECASE,
)
_PRIVATE_API_PATH = re.compile(
    rb"/(?:admin|internal|legion|private)(?:/|(?=[?#\s\"'<>)]|$))",
    re.IGNORECASE,
)
_PRIVATE_HOSTNAME = re.compile(
    rb"\b[a-z0-9][a-z0-9.-]*\.(?:corp|internal|lan)(?::\d+)?\b", re.IGNORECASE
)
_IPV4 = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_JWT = re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_GITHUB_TOKEN = re.compile(rb"\b(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_AWS_KEY = re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PROVIDER_TOKEN = re.compile(
    rb"\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{20,}|"
    rb"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|sk-(?:admin|proj)-[A-Za-z0-9_-]{20,}|"
    rb"glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{20,}|"
    rb"hf_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{35}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})\b"
)
_SLACK_WEBHOOK = re.compile(
    rb"https:"
    rb"//hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[A-Za-z0-9]{16,}"
)
_NETWORK_URL = re.compile(
    r"\b(?:https?|wss?|mqtts?|amqps?|ssl|tcp|tls)://"
    r"[^\s<>{}\"'`/$?#),;!]+",
    re.IGNORECASE,
)
_BARE_OPERATIONAL_FQDN = re.compile(
    r"(?<![A-Za-z0-9_.%-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:ai|cloud|com|dev|gov|io|mil|net|org|uk|us)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_IPV6_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_.#:-])"
    r"(?:\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f]*:[0-9A-Fa-f:]+)"
    r"(?![A-Za-z0-9_.:-])"
)
_RETIRED_RUNTIME_MARKERS = (
    b"aio" + b"http",
    b"api_base" + b"_url",
    b"any" + b"httpurl",
    b"http" + b".client",
    b"http_" + b"port",
    b"http" + b"://",
    b"https" + b"://",
    b"http" + b"transport",
    b"mqttv" + b"31",
    b"protocol-" + b"manifest.json",
    b"protocol_" + b"manifest",
    b"rest" + b"transport",
    b"urllib" + b".request",
    b"/v" + b"1",
    b"/v" + b"3",
)
_MARKDOWN_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>\n]+>|[^\s)\n]+)(?:\s+[^)\n]+)?\s*\)"
)
_MARKDOWN_REFERENCE_TARGET = re.compile(r"(?m)^\s*\[[^\]\n]+\]:\s*(?P<target><[^>\n]+>|\S+)")
_MARKDOWN_HEADING = re.compile(r"(?m)^#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_MARKDOWN_ANCHOR = re.compile(r"<a\s+(?:name|id)=[\"'](?P<anchor>[^\"']+)[\"']\s*>", re.I)
_FENCE_OPEN = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})[ \t]*(?P<info>.*)$")
_SHELL_FENCE_LANGUAGES = frozenset({"bash", "console", "sh", "shell"})
_INSTALLATION_GUIDE = "docs/getting-started/installation.md"
_EXAMPLE_ENVIRONMENT_VARIABLE = re.compile(r"\bECN_[A-Z][A-Z0-9_]*\b")
_DOCUMENTATION_BASE_PATH = re.compile(
    r"""^\s*export\s+const\s+documentationBasePath\s*=\s*"""
    r"""(?P<quote>["'])(?P<path>.*?)(?P=quote)\s*;\s*$""",
    re.MULTILINE,
)
_README_HEADER = (
    '<div align="center">\n'
    "\n"
    "<picture>\n"
    '  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" media="(prefers-color-scheme: light)">\n'
    '  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-dark.png" media="(prefers-color-scheme: dark)">\n'
    '  <img src="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" alt="Picogrid" width="576">\n'
    "</picture>\n"
    "\n"
    "<h1>ECN SDK</h1>\n"
    "\n"
    "[ECN](https://picogrid.com/ecn) · [Documentation](https://docs.picogrid.com/ecn-sdk/) · [Examples](https://github.com/picogrid/ecn-sdk-python/tree/main/examples) · [Security](https://github.com/picogrid/ecn-sdk-python/security/policy) · [Support](https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md) · [License](https://github.com/picogrid/ecn-sdk-python/blob/main/LICENSE)\n"
    "\n"
    "</div>\n"
)


def _normalize_version(version: str) -> str:
    """Normalize a version string to canonical PEP 440 form.

    Converts dashed pre-release spelling (0.1.0-rc1) to canonical (0.1.0rc1).
    """
    try:
        return str(Version(version))
    except InvalidVersion as exc:
        raise ArtifactPolicyError(f"invalid version string: {version}") from exc


def load_documentation_base_path(config_path: Path) -> str:
    """Load one normalized documentation mount from the JavaScript site config."""

    try:
        source = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactPolicyError(
            "documentationBasePath configuration is missing or invalid"
        ) from exc
    matches = list(_DOCUMENTATION_BASE_PATH.finditer(source))
    if len(matches) != 1:
        raise ArtifactPolicyError(
            "documentationBasePath configuration must contain exactly one string value"
        )
    configured = matches[0].group("path")
    if not configured.startswith("/") or configured.startswith("//"):
        raise ArtifactPolicyError("documentationBasePath must start with a single '/'")
    if (
        "\\" in configured
        or "//" in configured
        or any(part in {".", ".."} for part in configured.split("/"))
    ):
        raise ArtifactPolicyError("documentationBasePath is not a safe mount path")
    normalized = configured.rstrip("/")
    if not normalized:
        raise ArtifactPolicyError("documentationBasePath is not a safe mount path")
    return normalized


_EXAMPLE_MANIFEST_NAME = "manifest.json"
_EXAMPLE_ENTRY_FIELDS = frozenset(
    {
        "id",
        "source_path",
        "title",
        "summary",
        "workflow",
        "required_inputs",
        "safety_class",
        "modes",
        "documentation",
        "notebook_eligible",
        "exclusion_reason",
    }
)
_EXAMPLE_INPUT_TYPES = frozenset({"enum", "integer", "number", "path", "string", "uuid"})


class ArtifactPolicyError(RuntimeError):
    """Raised when a release artifact violates the committed publication policy."""


@dataclass(frozen=True, slots=True)
class ArtifactInspection:
    """Serializable result of inspecting one built artifact."""

    artifact: str
    artifact_type: str
    file_count: int
    files: tuple[str, ...]
    sha256: str
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DocumentationInspection:
    """Serializable result of validating release documentation and examples."""

    command_blocks: int
    documentation_files: tuple[str, ...]
    example_files: tuple[str, ...]
    link_count: int
    python_snippets: int
    supported_how_tos: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _generated_reference_documentation_files(policy_path: Path) -> tuple[str, ...]:
    """Derive generated reference pages from the authoritative API manifest."""

    manifest_path = policy_path.with_name("public-api-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactPolicyError("public API manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise ArtifactPolicyError("public API manifest must be a JSON object")
    prefix = manifest.get("reference_route_prefix")
    groups = manifest.get("groups")
    symbols = manifest.get("symbols")
    testing_symbols = manifest.get("testing_symbols")
    workflow_symbols = manifest.get("workflow_symbols")
    if (
        not isinstance(prefix, str)
        or prefix != "reference/python"
        or not isinstance(groups, list)
        or not isinstance(symbols, list)
        or not isinstance(testing_symbols, list)
        or not isinstance(workflow_symbols, list)
    ):
        raise ArtifactPolicyError("public API manifest reference routes are invalid")
    routes: set[str] = set()
    routes.add(f"docs/{prefix}/index.md")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("route"), str):
            raise ArtifactPolicyError("public API manifest reference routes are invalid")
        route = group["route"]
        route_path = PurePosixPath(route)
        if (
            route_path.is_absolute()
            or ".." in route_path.parts
            or not route.startswith(f"{prefix}/")
        ):
            raise ArtifactPolicyError("public API manifest contains an unsafe reference route")
        routes.add(f"docs/{route}/index.md")
    for entry in [*symbols, *testing_symbols, *workflow_symbols]:
        if not isinstance(entry, dict) or not isinstance(entry.get("route"), str):
            raise ArtifactPolicyError("public API manifest reference routes are invalid")
        route = entry["route"]
        route_path = PurePosixPath(route)
        if (
            route_path.is_absolute()
            or ".." in route_path.parts
            or not route.startswith(f"{prefix}/")
        ):
            raise ArtifactPolicyError("public API manifest contains an unsafe reference route")
        routes.add(f"docs/{route}.md")
    return tuple(sorted(routes))


def _publication_manifest_lines(policy: dict[str, Any]) -> tuple[str, ...]:
    """Return the deterministic MANIFEST.in contract."""

    documentation = _string_list(policy, "sdist_documentation_files")
    generated_prefix = "docs/reference/python/"
    curated = tuple(name for name in documentation if not name.startswith(generated_prefix))
    generated = tuple(name for name in documentation if name.startswith(generated_prefix))
    if not generated:
        return tuple(
            f"include {name}"
            for key in (
                "sdist_documentation_files",
                "sdist_example_files",
                "sdist_auxiliary_files",
            )
            for name in _string_list(policy, key)
        )
    return (
        *(f"include {name}" for name in curated),
        "recursive-include docs/reference/python *.md",
        *(f"include {name}" for name in _string_list(policy, "sdist_example_files")),
        *(f"include {name}" for name in _string_list(policy, "sdist_auxiliary_files")),
    )


def load_policy(path: Path) -> dict[str, Any]:
    """Load and minimally validate the committed release policy."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ArtifactPolicyError("release policy must be a JSON object")
    required = {
        "approved_public_hostnames",
        "approved_topic_filters",
        "direct_runtime_dependencies",
        "documentation_deferred_how_tos",
        "documentation_deferred_how_to_examples",
        "documentation_guide",
        "documentation_maintainer_index",
        "forbidden_path_fragments",
        "forbidden_path_suffixes",
        "generated_site_placeholder_urls",
        "license_expression",
        "license_text_sha256",
        "operator_runtime_requirements",
        "operator_third_party_licenses_sha256",
        "operator_wheel_dist_info_files",
        "operator_wheel_package_files",
        "project_version",
        "public_brand_assets",
        "requires_python",
        "runtime_requirements",
        "runtime_protocol",
        "retired_document_references",
        "sdist_auxiliary_files",
        "sdist_documentation_files",
        "sdist_example_files",
        "wheel_dist_info_files",
        "wheel_package_files",
        "worktree_synthetic_hostnames",
    }
    missing = required - raw.keys()
    if missing:
        raise ArtifactPolicyError(f"release policy is missing keys: {sorted(missing)}")
    if raw.get("runtime_protocol") != "mqtt-v5-only":
        raise ArtifactPolicyError("release policy must require MQTT v5 only")
    configured_documentation = raw.get("sdist_documentation_files")
    if not isinstance(configured_documentation, list) or any(
        not isinstance(name, str) for name in configured_documentation
    ):
        raise ArtifactPolicyError("release policy documentation inventory must be a string list")
    generated = _generated_reference_documentation_files(path)
    if any(name.startswith("docs/reference/python/") for name in configured_documentation):
        raise ArtifactPolicyError(
            "generated Python reference pages must be derived from the public API manifest"
        )
    raw["sdist_documentation_files"] = sorted([*configured_documentation, *generated])
    _string_list(raw, "retired_document_references")
    return raw


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_license_text(data: bytes, policy: dict[str, Any], member: str) -> None:
    """Require a packaged license copy to match the canonical text."""

    expected = _string_value(policy, "license_text_sha256")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ArtifactPolicyError(
            f"license text digest mismatch for {member}: expected {expected}, got {actual}"
        )


def _public_brand_asset_specs(
    policy: dict[str, Any],
) -> dict[str, tuple[str, int, int, frozenset[str]]]:
    value = policy.get("public_brand_assets")
    expected_names = {
        "brand/ecn-client-og.png",
        "brand/picogrid-app-icon-192.png",
        "brand/picogrid-app-icon-512.png",
        "brand/picogrid-nav-texture.png",
        "brand/picogrid-wordmark-dark.png",
        "brand/picogrid-wordmark-light.png",
    }
    if not isinstance(value, dict) or set(value) != expected_names:
        raise ArtifactPolicyError("release policy public brand asset inventory is invalid")
    specs: dict[str, tuple[str, int, int, frozenset[str]]] = {}
    for name, raw_spec in value.items():
        if not isinstance(name, str) or not isinstance(raw_spec, dict):
            raise ArtifactPolicyError("release policy public brand asset entry is invalid")
        expected_keys = {"height", "sha256", "width"}
        if name == "brand/picogrid-nav-texture.png":
            expected_keys.add("surfaces")
        if set(raw_spec) != expected_keys:
            raise ArtifactPolicyError("release policy public brand asset entry is invalid")
        digest = raw_spec.get("sha256")
        width = raw_spec.get("width")
        height = raw_spec.get("height")
        raw_surfaces = raw_spec.get("surfaces", ["documentation", "operator"])
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
            or not isinstance(raw_surfaces, list)
            or not raw_surfaces
            or any(
                not isinstance(surface, str) or surface not in {"documentation", "operator"}
                for surface in raw_surfaces
            )
            or len(set(raw_surfaces)) != len(raw_surfaces)
        ):
            raise ArtifactPolicyError("release policy public brand asset entry is invalid")
        specs[name] = (digest, width, height, frozenset(raw_surfaces))
    return specs


def validate_public_brand_assets(
    contents: Mapping[str, bytes],
    policy: dict[str, Any],
    *,
    prefix: str = "",
    surface: str | None = None,
) -> None:
    """Require each applicable public brand asset's bytes and PNG dimensions."""
    for relative, (expected_digest, expected_width, expected_height, surfaces) in sorted(
        _public_brand_asset_specs(policy).items()
    ):
        if surface is not None and surface not in surfaces:
            continue
        name = f"{prefix}{relative}"
        data = contents.get(name)
        if data is None:
            raise ArtifactPolicyError(f"public brand asset is missing: {relative}")
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise ArtifactPolicyError(f"public brand asset hash changed: {relative}")
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or int.from_bytes(data[8:12], "big") != 13
            or data[12:16] != b"IHDR"
            or int.from_bytes(data[16:20], "big") != expected_width
            or int.from_bytes(data[20:24], "big") != expected_height
        ):
            raise ArtifactPolicyError(f"public brand asset dimensions changed: {relative}")


def compare_rebuilt_artifacts(first: Path, second: Path) -> dict[str, str]:
    """Require two build directories to contain byte-identical wheel and sdist files."""

    first_files = _artifact_digest_map(first)
    second_files = _artifact_digest_map(second)
    if first_files.keys() != second_files.keys():
        raise ArtifactPolicyError(
            "rebuild produced a different artifact set: "
            f"{sorted(first_files)} != {sorted(second_files)}"
        )
    differences = [name for name in first_files if first_files[name] != second_files[name]]
    if differences:
        raise ArtifactPolicyError(
            f"artifacts are not byte-for-byte reproducible: {sorted(differences)}"
        )
    return first_files


def inspect_example_manifest(repository: Path) -> tuple[str, ...]:
    """Validate the example manifest against its source, docs, and public workflows."""

    manifest_path = repository / "examples" / _EXAMPLE_MANIFEST_NAME
    manifest_label = f"examples/{_EXAMPLE_MANIFEST_NAME}"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ArtifactPolicyError(f"{manifest_label} is missing or unsafe")
    try:
        data = manifest_path.read_bytes()
        manifest = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactPolicyError(f"{manifest_label} must be valid UTF-8 JSON") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "examples"}
        or manifest.get("schema_version") != 1
    ):
        raise ArtifactPolicyError(f"{manifest_label} must use schema_version 1")
    entries = manifest.get("examples")
    if not isinstance(entries, list) or not entries:
        raise ArtifactPolicyError(f"{manifest_label} must contain examples")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ArtifactPolicyError(f"{manifest_label} contains an invalid entry")

    canonical = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if data != canonical:
        raise ArtifactPolicyError(f"{manifest_label} has non-deterministic formatting")

    source_paths: list[str] = []
    ids: list[str] = []
    allowed_safety_classes = {"local", "read", "task-receive", "write"}
    allowed_modes = {"live", "mock", "offline-check"}
    workflows = importlib.import_module(f"{IMPORT_NAME}.workflows")
    exported = getattr(workflows, "__all__", None)
    if not isinstance(exported, list) or any(not isinstance(name, str) for name in exported):
        raise ArtifactPolicyError("public workflows.__all__ is invalid")

    for entry in entries:
        if set(entry) != _EXAMPLE_ENTRY_FIELDS:
            raise ArtifactPolicyError("example manifest entry fields are invalid")
        identifier_value = entry.get("id")
        title = entry.get("title")
        summary = entry.get("summary")
        if not isinstance(title, str) or not title.strip():
            raise ArtifactPolicyError(f"example manifest title is invalid: {identifier_value!r}")
        if not isinstance(summary, str) or not summary.strip():
            raise ArtifactPolicyError(f"example manifest summary is invalid: {identifier_value!r}")
        required_inputs = entry.get("required_inputs")
        if not isinstance(required_inputs, list):
            raise ArtifactPolicyError(
                f"example manifest required_inputs is invalid: {identifier_value!r}"
            )
        input_names: list[str] = []
        for required_input in required_inputs:
            if not isinstance(required_input, dict):
                raise ArtifactPolicyError(
                    f"example manifest required_inputs is invalid: {identifier_value!r}"
                )
            required = required_input.get("required")
            expected_fields = {
                "name",
                "kind",
                "type",
                "required",
                "description",
            }
            if required is False:
                expected_fields.add("default")
            if set(required_input) != expected_fields:
                raise ArtifactPolicyError(
                    f"example manifest required_inputs fields are invalid: {identifier_value!r}"
                )
            name = required_input.get("name")
            description = required_input.get("description")
            if not isinstance(name, str) or not name.strip():
                raise ArtifactPolicyError(
                    f"example manifest required_inputs name is invalid: {identifier_value!r}"
                )
            if required_input.get("kind") != "env":
                raise ArtifactPolicyError(
                    f"example manifest required_inputs kind is invalid: {identifier_value!r}"
                )
            if required_input.get("type") not in _EXAMPLE_INPUT_TYPES:
                raise ArtifactPolicyError(
                    f"example manifest required_inputs type is invalid: {identifier_value!r}"
                )
            if not isinstance(required, bool):
                raise ArtifactPolicyError(
                    f"example manifest required_inputs required is invalid: {identifier_value!r}"
                )
            if required is False:
                default = required_input["default"]
                input_type = required_input["type"]
                default_is_valid = (
                    default is None
                    or (
                        input_type == "integer"
                        and isinstance(default, int)
                        and not isinstance(default, bool)
                    )
                    or (
                        input_type == "number"
                        and isinstance(default, int | float)
                        and not isinstance(default, bool)
                    )
                    or (
                        input_type in {"enum", "path", "string", "uuid"}
                        and isinstance(default, str)
                    )
                )
                if not default_is_valid:
                    raise ArtifactPolicyError(
                        f"example manifest required_inputs default is invalid: {identifier_value!r}"
                    )
            if not isinstance(description, str) or not description.strip():
                raise ArtifactPolicyError(
                    f"example manifest required_inputs description is invalid: {identifier_value!r}"
                )
            input_names.append(name)
        if input_names != list(dict.fromkeys(input_names)):
            raise ArtifactPolicyError(
                f"example manifest required_inputs names are duplicated: {identifier_value!r}"
            )
        source_path = entry.get("source_path")
        if not isinstance(source_path, str):
            raise ArtifactPolicyError("example manifest source_path is invalid")
        relative = PurePosixPath(source_path)
        if (
            len(relative.parts) != 2
            or relative.parts[0] != "examples"
            or relative.suffix != ".py"
            or source_path != relative.as_posix()
        ):
            raise ArtifactPolicyError(f"example manifest source_path is unsafe: {source_path!r}")
        source = repository.joinpath(*relative.parts)
        if not source.is_file() or source.is_symlink():
            raise ArtifactPolicyError(f"example manifest source_path is missing: {source_path}")
        source_paths.append(source_path)

        identifier = entry.get("id")
        if not isinstance(identifier, str):
            raise ArtifactPolicyError("example manifest id is invalid")
        expected_identifier = relative.stem.replace("_", "-")
        if identifier != expected_identifier:
            raise ArtifactPolicyError(
                f"example manifest id does not match source filename: {identifier!r}"
            )
        ids.append(identifier)

        workflow = entry.get("workflow")
        if not isinstance(workflow, dict) or set(workflow) != {"module", "function"}:
            raise ArtifactPolicyError(f"example manifest workflow is invalid: {identifier}")
        module_name = workflow.get("module")
        function_name = workflow.get("function")
        if (
            not isinstance(module_name, str)
            or not module_name.startswith(f"{IMPORT_NAME}.workflows.")
            or not isinstance(function_name, str)
            or not function_name
            # The prefix alone admits private submodules such as `workflows._retention`.
            or any(part.startswith("_") for part in module_name.split(".")[2:])
        ):
            raise ArtifactPolicyError(f"example manifest workflow is invalid: {identifier}")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise ArtifactPolicyError(
                f"example manifest workflow module cannot be imported: {module_name}"
            ) from exc
        # Identity, not just presence: the declared module must own the exported callable.
        routed = getattr(module, function_name, None)
        if (
            function_name not in exported
            or not callable(routed)
            or getattr(workflows, function_name, None) is not routed
        ):
            raise ArtifactPolicyError(
                f"example manifest workflow function is not exported: {function_name}"
            )

        documentation = entry.get("documentation")
        if (
            not isinstance(documentation, list)
            or not documentation
            or any(not isinstance(reference, str) for reference in documentation)
        ):
            raise ArtifactPolicyError(f"example manifest documentation is invalid: {identifier}")
        if len(documentation) != len(set(documentation)):
            raise ArtifactPolicyError(f"example manifest documentation is duplicated: {identifier}")
        for reference in documentation:
            documentation_path = PurePosixPath(reference)
            if (
                documentation_path.is_absolute()
                or ".." in documentation_path.parts
                or not reference.startswith("docs/")
                or reference != documentation_path.as_posix()
            ):
                raise ArtifactPolicyError(
                    f"example manifest documentation reference is unsafe: {reference!r}"
                )
            resolved = repository.joinpath(*documentation_path.parts)
            if not resolved.is_file() or resolved.is_symlink():
                raise ArtifactPolicyError(
                    f"example manifest documentation reference is missing: {reference}"
                )

        notebook_eligible = entry.get("notebook_eligible")
        exclusion_reason = entry.get("exclusion_reason")
        if not isinstance(notebook_eligible, bool):
            raise ArtifactPolicyError(
                f"example manifest notebook_eligible is invalid: {identifier}"
            )
        if notebook_eligible:
            if exclusion_reason is not None:
                raise ArtifactPolicyError(
                    f"example manifest exclusion_reason is invalid: {identifier}"
                )
        elif not isinstance(exclusion_reason, str) or not exclusion_reason.strip():
            raise ArtifactPolicyError(
                f"example manifest exclusion_reason is required: {identifier}"
            )

        safety_class = entry.get("safety_class")
        if safety_class not in allowed_safety_classes:
            raise ArtifactPolicyError(f"example manifest safety_class is invalid: {identifier}")
        modes = entry.get("modes")
        if (
            not isinstance(modes, list)
            or not modes
            or any(not isinstance(mode, str) for mode in modes)
            or len(modes) != len(set(modes))
            or not set(modes).issubset(allowed_modes)
        ):
            raise ArtifactPolicyError(f"example manifest modes are invalid: {identifier}")

    if ids != sorted(set(ids)):
        raise ArtifactPolicyError("example manifest ids must be sorted and unique")
    actual_sources = tuple(
        sorted(
            path.relative_to(repository).as_posix()
            for path in (repository / "examples").glob("*.py")
            if path.is_file() and path.name not in {"__init__.py", "_common.py"}
        )
    )
    declared_sources = tuple(sorted(source_paths))
    if len(source_paths) != len(set(source_paths)) or declared_sources != actual_sources:
        raise ArtifactPolicyError(
            "example inventory mismatch between examples/manifest.json and examples/*.py"
        )
    return tuple(source_paths)


def inspect_documentation(repository: Path, policy: dict[str, Any]) -> DocumentationInspection:
    """Validate the exact release docs/examples tree and its machine-checkable guide."""

    inspect_example_manifest(repository)
    documentation_files = _publication_paths(
        policy,
        "sdist_documentation_files",
        prefix="docs/",
        suffix=".md",
    )
    example_files = _publication_paths(
        policy,
        "sdist_example_files",
        prefix="examples/",
        suffix=(".py", "/manifest.json"),
    )
    auxiliary_files = _safe_publication_files(policy, "sdist_auxiliary_files")
    actual_documentation = _documentation_file_inventory(repository)
    actual_examples = tuple(
        sorted(
            path.relative_to(repository).as_posix()
            for path in (repository / "examples").glob("*")
            if path.is_file() and (path.suffix == ".py" or path.name == "manifest.json")
        )
    )
    _require_publication_inventory(
        actual_documentation,
        documentation_files,
        label="documentation",
    )
    _require_publication_inventory(actual_examples, example_files, label="example")
    operator_readme = "operator-app/README.md"
    operator_files = tuple(name for name in auxiliary_files if name.startswith("operator-app/"))
    if operator_files:
        if operator_readme not in operator_files:
            raise ArtifactPolicyError("operator application inventory omits its README")
        actual_operator_files = tuple(
            name
            for name in _tree_file_inventory(repository, "operator-app")
            if name != "operator-app/.gitignore"
        )
        _require_publication_inventory(
            actual_operator_files,
            operator_files,
            label="operator application",
        )
    manifest = repository / "MANIFEST.in"
    expected_manifest = _publication_manifest_lines(policy)
    if (
        not manifest.is_file()
        or manifest.is_symlink()
        or tuple(manifest.read_text(encoding="utf-8").splitlines()) != expected_manifest
    ):
        raise ArtifactPolicyError("MANIFEST.in differs from the exact docs/example inventory")

    guide = _string_value(policy, "documentation_guide")
    if guide not in documentation_files:
        raise ArtifactPolicyError("documentation guide is outside the exact docs inventory")
    maintainer_index = _string_value(policy, "documentation_maintainer_index")
    if maintainer_index not in documentation_files:
        raise ArtifactPolicyError(
            "documentation maintainer index is outside the exact docs inventory"
        )
    public_examples = {
        path
        for path in example_files
        if path.endswith(".py") and not PurePosixPath(path).name.startswith("_")
    }
    if not public_examples:
        raise ArtifactPolicyError("release documentation has no runnable public examples")

    markdown_files = (
        "README.md",
        *documentation_files,
        *((operator_readme,) if operator_readme in operator_files else ()),
    )
    contents: dict[str, str] = {}
    fences: dict[str, tuple[tuple[str, str, int], ...]] = {}
    prose: dict[str, str] = {}
    for name in markdown_files:
        path = repository / name
        if not path.is_file() or path.is_symlink():
            raise ArtifactPolicyError(f"release documentation file is missing or unsafe: {name}")
        data = path.read_bytes()
        _scan_content(name, data, policy)
        try:
            contents[name] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactPolicyError(f"release documentation is not UTF-8: {name}") from exc
        fences[name], prose[name] = _split_markdown_fences(contents[name], name=name)
    _validate_readme_surface(contents["README.md"])

    released_paths = {
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        *documentation_files,
        *example_files,
        *auxiliary_files,
    }
    anchors = {name: _markdown_anchors(prose[name]) for name in markdown_files}
    resolved_links: dict[str, tuple[str, ...]] = {}
    link_count = 0
    for name in markdown_files:
        targets = _markdown_targets(prose[name])
        link_count += len(targets)
        resolved_links[name] = tuple(
            resolved
            for target in targets
            if (
                resolved := _resolve_markdown_target(
                    source=name,
                    target=target,
                    released_paths=released_paths,
                    anchors=anchors,
                )
            )
            is not None
        )

    python_snippets = 0
    command_blocks = 0
    for name, blocks in fences.items():
        for language, body, line in blocks:
            if language in {"py", "python"}:
                if name.startswith("docs/reference/python/"):
                    continue
                _validate_python_snippet(body, name=name, line=line)
                python_snippets += 1
            elif language in _SHELL_FENCE_LANGUAGES:
                _validate_command_block(
                    body,
                    name=name,
                    line=line,
                    public_examples=public_examples,
                )
                command_blocks += 1
    if python_snippets == 0 or command_blocks == 0:
        raise ArtifactPolicyError(
            "release documentation must contain validated Python snippets and commands"
        )
    if operator_files:
        required_operator_commands = (
            "picogrid-ecn operator --demo",
            "picogrid-ecn operator --profile NAME",
            "picogrid-ecn-operator --demo",
            "picogrid-ecn-operator --profile NAME",
            "docker compose up --build operator-mock",
            "npm ci --ignore-scripts",
            "npm run build",
        )
        if any(command not in contents[operator_readme] for command in required_operator_commands):
            raise ArtifactPolicyError(
                "operator application guide omits a required installed-artifact command"
            )
    _validate_installation_guide(
        contents=contents,
        fences=fences,
        documentation_files=documentation_files,
        policy=policy,
    )

    how_to_files = {name for name in documentation_files if name.startswith("docs/how-to/")}
    if not how_to_files:
        raise ArtifactPolicyError("release documentation has no supported how-to pages")
    deferred_how_tos = set(_string_list(policy, "documentation_deferred_how_tos"))
    if not deferred_how_tos.issubset(how_to_files):
        raise ArtifactPolicyError("deferred how-to inventory is outside released how-to pages")
    supported_how_tos = how_to_files - deferred_how_tos
    if not supported_how_tos:
        raise ArtifactPolicyError("release documentation has no supported how-to pages")
    for name in sorted(supported_how_tos):
        example_links = {target for target in resolved_links[name] if target in public_examples}
        if not example_links:
            raise ArtifactPolicyError(f"supported how-to does not link a runnable example: {name}")
    deferred_example_policy = policy.get("documentation_deferred_how_to_examples")
    if not isinstance(deferred_example_policy, dict) or set(deferred_example_policy) != (
        deferred_how_tos
    ):
        raise ArtifactPolicyError(
            "deferred how-to example policy must exactly cover deferred how-to pages"
        )
    for name in sorted(deferred_how_tos):
        expected_links = deferred_example_policy.get(name)
        if (
            not isinstance(expected_links, list)
            or expected_links != sorted(set(expected_links))
            or any(not isinstance(target, str) for target in expected_links)
            or not set(expected_links).issubset(public_examples)
        ):
            raise ArtifactPolicyError("deferred how-to example policy is invalid")
        actual_links = {target for target in resolved_links[name] if target in public_examples}
        if actual_links != set(expected_links):
            raise ArtifactPolicyError(
                f"deferred how-to runnable examples differ from policy: {name}"
            )
    documented_examples = {
        target
        for name in documentation_files
        for target in resolved_links[name]
        if target in public_examples
    }
    if documented_examples != public_examples:
        raise ArtifactPolicyError(
            "released guide pages do not cover the exact public example inventory"
        )

    example_environment_variables: set[str] = set()
    for name in public_examples:
        path = repository / name
        try:
            example_environment_variables.update(
                _EXAMPLE_ENVIRONMENT_VARIABLE.findall(path.read_text(encoding="utf-8"))
            )
        except UnicodeDecodeError as exc:
            raise ArtifactPolicyError(f"release example is not UTF-8: {name}") from exc
    documented_environment_variables = {
        value for text in contents.values() for value in _EXAMPLE_ENVIRONMENT_VARIABLE.findall(text)
    }
    if not example_environment_variables.issubset(documented_environment_variables):
        raise ArtifactPolicyError(
            "released guide does not document every example environment variable"
        )

    return DocumentationInspection(
        command_blocks=command_blocks,
        documentation_files=documentation_files,
        example_files=example_files,
        link_count=link_count,
        python_snippets=python_snippets,
        supported_how_tos=len(supported_how_tos),
    )


def _publication_paths(
    policy: dict[str, Any],
    key: str,
    *,
    prefix: str,
    suffix: str | tuple[str, ...],
) -> tuple[str, ...]:
    paths = _string_list(policy, key)
    if not paths or list(paths) != sorted(set(paths)):
        raise ArtifactPolicyError(f"release policy key {key!r} must be a sorted unique list")
    for name in paths:
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not name.startswith(prefix)
            or not name.endswith(suffix)
        ):
            raise ArtifactPolicyError(f"release policy key {key!r} contains an unsafe path")
    return paths


def _safe_publication_files(policy: dict[str, Any], key: str) -> tuple[str, ...]:
    paths = _string_list(policy, key)
    if list(paths) != sorted(set(paths)):
        raise ArtifactPolicyError(f"release policy key {key!r} must be a sorted unique list")
    for name in paths:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ArtifactPolicyError(f"release policy key {key!r} contains an unsafe path")
    return paths


# The documentation workspace holds its own tooling, dependencies and build
# caches beside the published prose. These top-level entries are the workspace
# itself; everything else under `docs/` is documentation and must be inventoried
# whatever its extension, so a new file there is classified deliberately rather
# than escaping the shipped artifact.
#
# This is the one place that says which parts of the workspace are tooling. Every
# gate that has to make that distinction reads it here rather than restating it,
# so two gates cannot come to disagree about a directory added to only one list.
DOCUMENTATION_WORKSPACE_DIRECTORIES = frozenset(
    {".astro", ".wrangler", "cloudflare", "node_modules", "site", "src"}
)
DOCUMENTATION_WORKSPACE_FILES = frozenset(
    {
        "astro.config.mjs",
        "cspell.json",
        "package-lock.json",
        "package.json",
        "tsconfig.json",
        "wrangler.jsonc",
    }
)
_DOCUMENTATION_WORKSPACE_ENTRIES = (
    DOCUMENTATION_WORKSPACE_DIRECTORIES | DOCUMENTATION_WORKSPACE_FILES
)


def _documentation_file_inventory(repository: Path) -> tuple[str, ...]:
    """Every shipped documentation file, whatever its extension.

    The workspace holds its own tooling beside the prose, so only the workspace
    entries are skipped. Narrowing this to one extension would let a page the
    site publishes — an `.mdx` route, an inlined asset — escape the exact
    artifact while every source-side check still passed.
    """

    directory = repository / "docs"
    if not directory.is_dir() or directory.is_symlink():
        raise ArtifactPolicyError("release docs directory is missing or unsafe")
    entries = tuple(
        path
        for path in directory.rglob("*")
        if path.relative_to(directory).parts[0] not in _DOCUMENTATION_WORKSPACE_ENTRIES
    )
    if any(path.is_symlink() for path in entries):
        raise ArtifactPolicyError("release docs directory contains a symbolic link")
    return tuple(
        sorted(path.relative_to(repository).as_posix() for path in entries if path.is_file())
    )


def _tree_file_inventory(repository: Path, root: str) -> tuple[str, ...]:
    directory = repository / root
    if not directory.is_dir() or directory.is_symlink():
        raise ArtifactPolicyError(f"release {root} directory is missing or unsafe")
    entries = tuple(directory.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ArtifactPolicyError(f"release {root} directory contains a symbolic link")
    return tuple(
        sorted(path.relative_to(repository).as_posix() for path in entries if path.is_file())
    )


def _require_publication_inventory(
    actual: tuple[str, ...], expected: tuple[str, ...], *, label: str
) -> None:
    if actual != expected:
        unexpected = sorted(set(actual) - set(expected))
        missing = sorted(set(expected) - set(actual))
        raise ArtifactPolicyError(
            f"{label} inventory mismatch; unexpected={unexpected}, missing={missing}"
        )


def _split_markdown_fences(text: str, *, name: str) -> tuple[tuple[tuple[str, str, int], ...], str]:
    blocks: list[tuple[str, str, int]] = []
    prose: list[str] = []
    marker_character = ""
    marker_length = 0
    language = ""
    block_lines: list[str] = []
    start_line = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not marker_character:
            match = _FENCE_OPEN.fullmatch(line)
            if match is None:
                prose.append(line)
                continue
            marker = match.group("marker")
            marker_character = marker[0]
            marker_length = len(marker)
            info = match.group("info").strip()
            language = info.split(maxsplit=1)[0].casefold() if info else ""
            start_line = line_number
            prose.append("")
            continue

        stripped = line.strip()
        if stripped and set(stripped) == {marker_character} and len(stripped) >= marker_length:
            blocks.append((language, "\n".join(block_lines) + "\n", start_line))
            marker_character = ""
            marker_length = 0
            language = ""
            block_lines = []
            prose.append("")
            continue
        block_lines.append(line)
        prose.append("")
    if marker_character:
        raise ArtifactPolicyError(f"unclosed Markdown code fence in {name}:{start_line}")
    return tuple(blocks), "\n".join(prose)


def _markdown_targets(text: str) -> tuple[str, ...]:
    targets = [match.group("target") for match in _MARKDOWN_LINK.finditer(text)]
    targets.extend(match.group("target") for match in _MARKDOWN_REFERENCE_TARGET.finditer(text))
    return tuple(target.removeprefix("<").removesuffix(">") for target in targets)


def _validate_readme_surface(text: str) -> None:
    if not text.startswith(_README_HEADER):
        raise ArtifactPolicyError("README wordmark contract is incomplete")


def _markdown_anchors(text: str) -> frozenset[str]:
    anchors = {match.group("anchor").casefold() for match in _MARKDOWN_ANCHOR.finditer(text)}
    duplicates: dict[str, int] = {}
    for match in _MARKDOWN_HEADING.finditer(text):
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", match.group("title"))
        title = re.sub(r"[`*_~]", "", title)
        base = re.sub(r"[^\w\- ]", "", title.casefold())
        base = re.sub(r"[ -]+", "-", base).strip("-")
        if not base:
            continue
        duplicate = duplicates.get(base, 0)
        anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
        duplicates[base] = duplicate + 1
    return frozenset(anchors)


def _resolve_markdown_target(
    *,
    source: str,
    target: str,
    released_paths: set[str],
    anchors: dict[str, frozenset[str]],
) -> str | None:
    parsed = urlsplit(target)
    public_blob_prefix = "/picogrid/ecn-sdk-python/blob/main/"
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith(public_blob_prefix)
        ):
            return None
        resolved = unquote(parsed.path.removeprefix(public_blob_prefix))
    else:
        raw_path = unquote(parsed.path)
        if raw_path.startswith("/"):
            if not source.startswith("docs/reference/python/"):
                raise ArtifactPolicyError(f"absolute Markdown link found in {source}")
            # Generated API reference emits mount-neutral root-absolute site
            # routes; the deployment mount is Astro configuration, and source
            # Markdown lives under docs/.
            route = raw_path.strip("/")
            candidates = (
                (f"docs/{route}.md", f"docs/{route}/index.md") if route else ("docs/index.md",)
            )
            resolved = next(
                (candidate for candidate in candidates if candidate in released_paths),
                candidates[0],
            )
        elif raw_path:
            resolved = posixpath.normpath(
                posixpath.join(PurePosixPath(source).parent.as_posix(), raw_path)
            )
        else:
            resolved = source
    if resolved == ".." or resolved.startswith("../") or resolved not in released_paths:
        raise ArtifactPolicyError(f"broken released link found in {source}")
    if parsed.fragment and resolved.endswith(".md"):
        anchor = unquote(parsed.fragment).casefold()
        if anchor not in anchors.get(resolved, frozenset()):
            raise ArtifactPolicyError(f"broken Markdown anchor found in {source}")
    return resolved


def _validate_python_snippet(body: str, *, name: str, line: int) -> None:
    if not body.strip():
        raise ArtifactPolicyError(f"empty Python snippet found in {name}:{line}")
    try:
        ast.parse(body)
        return
    except SyntaxError:
        wrapped = "async def _documentation_snippet() -> None:\n" + textwrap.indent(body, "    ")
    try:
        ast.parse(wrapped)
    except SyntaxError as exc:
        raise ArtifactPolicyError(f"invalid Python snippet found in {name}:{line}") from exc


def _validate_command_block(
    body: str,
    *,
    name: str,
    line: int,
    public_examples: set[str],
) -> None:
    logical_body = body.replace("\\\n", " ")
    syntax_body = "\n".join(
        line.lstrip().removeprefix("$ ") if line.lstrip().startswith("$ ") else line
        for line in logical_body.splitlines()
    )
    bash = shutil.which("bash")
    if bash is None:
        raise ArtifactPolicyError("bash is required to validate documentation commands")
    syntax = subprocess.run(
        [bash, "-n"],
        input=syntax_body,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if syntax.returncode != 0:
        raise ArtifactPolicyError(f"invalid shell syntax found in {name}:{line}")
    commands = 0
    for offset, raw_line in enumerate(logical_body.splitlines()):
        command = raw_line.strip()
        if not command or command.startswith("#"):
            continue
        if command.startswith("$ "):
            command = command[2:].lstrip()
        try:
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError as exc:
            raise ArtifactPolicyError(
                f"invalid shell command found in {name}:{line + offset + 1}"
            ) from exc
        if not tokens:
            continue
        if any(token in {"&&", "||", ";", "|"} for token in tokens):
            raise ArtifactPolicyError(f"compound shell command found in {name}:{line + offset + 1}")
        for token in tokens:
            normalized = token.removeprefix("./")
            if "examples/" not in normalized or not normalized.endswith(".py"):
                continue
            example_path = posixpath.normpath(normalized)
            if example_path not in public_examples:
                raise ArtifactPolicyError(
                    f"documentation command references an unshipped example in {name}"
                )
        commands += 1
    if commands == 0:
        raise ArtifactPolicyError(f"empty shell command block found in {name}:{line}")


def _validate_installation_guide(
    *,
    contents: dict[str, str],
    fences: dict[str, tuple[tuple[str, str, int], ...]],
    documentation_files: tuple[str, ...],
    policy: dict[str, Any],
) -> None:
    """Require the reviewed wheel and allow only vetted companion pip commands."""

    if _INSTALLATION_GUIDE not in documentation_files or _INSTALLATION_GUIDE not in contents:
        raise ArtifactPolicyError("release documentation is missing the installation guide")
    version = _string_value(policy, "project_version")
    normalized_version = _normalize_version(version)
    wheel = f"{IMPORT_NAME}-{normalized_version}-py3-none-any.whl"
    required = ("python", "-m", "pip", "install", f"./{wheel}")
    required_operator = documented_operator_install_argv(version)
    # A source checkout builds the same reviewed wheel into ./dist and needs the build
    # front-end first; every permitted command still resolves declared dependencies.
    # Canonical PyPI install and offline verified-artifact paths are also permitted.
    permitted = {
        required,
        ("python", "-m", "pip", "install", f"./dist/{wheel}"),
        ("python", "-m", "pip", "install", "build"),
        required_operator,
        ("python", "-m", "pip", "install", f"{PACKAGE_NAME}=={version}"),
        (
            "python",
            "-m",
            "pip",
            "install",
            "--upgrade",
            f"{PACKAGE_NAME}=={version}",
        ),
        (
            "python",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            "./wheelhouse",
            "--require-hashes",
            "-r",
            "wheelhouse/requirements.txt",
        ),
    }
    install_commands: list[tuple[str, ...]] = []
    for language, body, _line in fences[_INSTALLATION_GUIDE]:
        if language not in _SHELL_FENCE_LANGUAGES:
            continue
        logical_body = body.replace("\\\n", " ")
        for raw_line in logical_body.splitlines():
            command = raw_line.strip()
            if not command or command.startswith("#"):
                continue
            if command.startswith("$ "):
                command = command[2:].lstrip()
            try:
                tokens = tuple(shlex.split(command, comments=True, posix=True))
            except ValueError as exc:  # already rejected by the generic command validator
                raise ArtifactPolicyError(
                    "installation guide contains an invalid shell command"
                ) from exc
            if tokens[:4] == ("python", "-m", "pip", "install"):
                install_commands.append(tokens)
    if (
        required not in install_commands
        or required_operator not in install_commands
        or len(set(install_commands)) != len(install_commands)
        or not set(install_commands) <= permitted
    ):
        raise ArtifactPolicyError(
            "installation guide must install the exact wheel with dependency resolution enabled"
        )


def inspect_wheel(path: Path, policy: dict[str, Any]) -> ArtifactInspection:
    """Inspect a wheel against an exact allowlist plus publication deny rules."""

    version = _string_value(policy, "project_version")
    normalized_version = _normalize_version(version)
    expected_name = f"{IMPORT_NAME}-{normalized_version}-py3-none-any.whl"
    if path.name != expected_name:
        raise ArtifactPolicyError(f"expected wheel {expected_name}, got {path.name}")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members if not member.is_dir()]
        _validate_member_names(names)
        if len(names) != len(set(names)):
            raise ArtifactPolicyError("wheel contains duplicate paths")
        for member in members:
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ArtifactPolicyError(f"wheel contains a symbolic link: {member.filename}")

        dist_info_prefix = _wheel_dist_info_prefix(names, distribution_name=IMPORT_NAME)
        expected_prefix = f"{IMPORT_NAME}-{normalized_version}.dist-info/"
        if dist_info_prefix != expected_prefix:
            raise ArtifactPolicyError(
                f"wheel dist-info version changed: {dist_info_prefix} != {expected_prefix}"
            )
        expected = set(_string_list(policy, "wheel_package_files"))
        expected.update(
            f"{dist_info_prefix}{name}" for name in _string_list(policy, "wheel_dist_info_files")
        )
        _require_exact_allowlist(names, expected, artifact="wheel")
        _scan_paths(names, policy)

        contents: dict[str, bytes] = {}
        for name in names:
            data = archive.read(name)
            contents[name] = data
            _scan_content(name, data, policy)
            _scan_retired_document_references(name, data, policy)
        license_member = f"{dist_info_prefix}licenses/LICENSE"
        _validate_license_text(contents[license_member], policy, license_member)
        _validate_metadata(contents[f"{dist_info_prefix}METADATA"], policy)
        _validate_wheel_metadata(contents[f"{dist_info_prefix}WHEEL"])
        _validate_topic_filters(contents[f"{IMPORT_NAME}/_protocol/topics.py"], policy)
        _validate_mqtt_v5_runtime(contents, package_prefix=f"{IMPORT_NAME}/")

    ordered = tuple(sorted(names))
    return ArtifactInspection(
        artifact=path.name,
        artifact_type="wheel",
        file_count=len(ordered),
        files=ordered,
        sha256=sha256_file(path),
        checks=(
            "exact-file-allowlist",
            "path-denylist",
            "content-and-secret-scan",
            "public-dependency-metadata",
            "fixed-topic-filter-allowlist",
            "mqtt-v5-only-runtime",
            "pure-python-wheel",
        ),
    )


def inspect_operator_wheel(path: Path, policy: dict[str, Any]) -> ArtifactInspection:
    """Inspect the separately installable operator wheel against an exact policy."""

    version = _string_value(policy, "project_version")
    normalized_version = _normalize_version(version)
    expected_name = f"{OPERATOR_DISTRIBUTION_NAME}-{normalized_version}-py3-none-any.whl"
    if path.name != expected_name:
        raise ArtifactPolicyError(f"expected operator wheel {expected_name}, got {path.name}")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members if not member.is_dir()]
        _validate_member_names(names)
        if len(names) != len(set(names)):
            raise ArtifactPolicyError("operator wheel contains duplicate paths")
        for member in members:
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ArtifactPolicyError(
                    f"operator wheel contains a symbolic link: {member.filename}"
                )

        dist_info_prefix = _wheel_dist_info_prefix(
            names,
            distribution_name=OPERATOR_DISTRIBUTION_NAME,
        )
        expected_prefix = f"{OPERATOR_DISTRIBUTION_NAME}-{normalized_version}.dist-info/"
        if dist_info_prefix != expected_prefix:
            raise ArtifactPolicyError(
                f"operator wheel dist-info version changed: {dist_info_prefix} != {expected_prefix}"
            )
        expected = set(_string_list(policy, "operator_wheel_package_files"))
        expected.update(
            f"{dist_info_prefix}{name}"
            for name in _string_list(policy, "operator_wheel_dist_info_files")
        )
        _require_exact_allowlist(names, expected, artifact="operator wheel")
        _scan_paths(names, policy)

        contents: dict[str, bytes] = {}
        for name in names:
            data = archive.read(name)
            contents[name] = data
            _scan_content(name, data, policy)
            _scan_retired_document_references(name, data, policy)
        license_name = f"{dist_info_prefix}licenses/LICENSE"
        _validate_license_text(contents[license_name], policy, license_name)
        validate_public_brand_assets(
            contents,
            policy,
            prefix="operator_app/static/",
            surface="operator",
        )
        third_party_name = f"{dist_info_prefix}licenses/THIRD_PARTY_LICENSES.md"
        third_party_digest = hashlib.sha256(contents[third_party_name]).hexdigest()
        expected_third_party_digest = _string_value(
            policy,
            "operator_third_party_licenses_sha256",
        )
        if third_party_digest != expected_third_party_digest:
            raise ArtifactPolicyError("operator third-party license notice changed")
        _validate_operator_metadata(contents[f"{dist_info_prefix}METADATA"], policy)
        _validate_wheel_metadata(contents[f"{dist_info_prefix}WHEEL"])
        entry_points = contents[f"{dist_info_prefix}entry_points.txt"].decode("utf-8")
        expected_entry_point = (
            f"[console_scripts]\npicogrid-ecn-operator = {OPERATOR_IMPORT_NAME}.__main__:main"
        )
        if entry_points.strip() != expected_entry_point:
            raise ArtifactPolicyError("operator wheel console entry point changed")
        if contents[f"{dist_info_prefix}top_level.txt"] != f"{OPERATOR_IMPORT_NAME}\n".encode():
            raise ArtifactPolicyError("operator wheel top-level package changed")

    ordered = tuple(sorted(names))
    return ArtifactInspection(
        artifact=path.name,
        artifact_type="operator-wheel",
        file_count=len(ordered),
        files=ordered,
        sha256=sha256_file(path),
        checks=(
            "exact-file-allowlist",
            "path-denylist",
            "content-and-secret-scan",
            "public-brand-integrity",
            "third-party-license-integrity",
            "public-dependency-metadata",
            "installed-console-entry-point",
            "pure-python-wheel",
        ),
    )


def inspect_sdist(path: Path, policy: dict[str, Any]) -> ArtifactInspection:
    """Inspect the source distribution with the same publication boundary as the wheel."""

    version = _string_value(policy, "project_version")
    normalized_version = _normalize_version(version)
    expected_name = f"{IMPORT_NAME}-{normalized_version}.tar.gz"
    if path.name != expected_name:
        raise ArtifactPolicyError(f"expected sdist {expected_name}, got {path.name}")
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        files = [member for member in members if member.isfile()]
        names = [member.name for member in files]
        _validate_member_names(names)
        if len(names) != len(set(names)):
            raise ArtifactPolicyError("sdist contains duplicate paths")
        unsafe = [
            member.name
            for member in members
            if member.issym() or member.islnk() or member.isdev() or member.isfifo()
        ]
        if unsafe:
            raise ArtifactPolicyError(f"sdist contains unsafe member types: {sorted(unsafe)}")

        root = _sdist_root(names)
        expected_root = f"{IMPORT_NAME}-{normalized_version}"
        if root != expected_root:
            raise ArtifactPolicyError(f"sdist version changed: {root} != {expected_root}")
        expected = _expected_sdist_files(root, policy)
        _require_exact_allowlist(names, expected, artifact="sdist")
        _scan_paths(names, policy)
        contents: dict[str, bytes] = {}
        for member in files:
            source = archive.extractfile(member)
            if source is None:
                raise ArtifactPolicyError(f"could not read sdist member: {member.name}")
            data = source.read()
            contents[member.name] = data
            _scan_content(member.name, data, policy)
            _scan_retired_document_references(member.name, data, policy)
        for license_member in (f"{root}/LICENSE", f"{root}/operator-app/LICENSE"):
            _validate_license_text(contents[license_member], policy, license_member)
        validate_public_brand_assets(
            contents,
            policy,
            prefix=f"{root}/docs/site/public/",
            surface="documentation",
        )
        validate_public_brand_assets(
            contents,
            policy,
            prefix=f"{root}/operator-app/frontend/public/",
            surface="operator",
        )
        _validate_metadata(contents[f"{root}/PKG-INFO"], policy)
        _validate_metadata(contents[f"{root}/src/{IMPORT_NAME}.egg-info/PKG-INFO"], policy)
        _validate_publication_manifest(contents[f"{root}/MANIFEST.in"], policy)
        package_prefix = f"{root}/src/{IMPORT_NAME}/"
        _validate_topic_filters(contents[f"{package_prefix}_protocol/topics.py"], policy)
        _validate_mqtt_v5_runtime(contents, package_prefix=package_prefix)

    ordered = tuple(sorted(names))
    return ArtifactInspection(
        artifact=path.name,
        artifact_type="sdist",
        file_count=len(ordered),
        files=ordered,
        sha256=sha256_file(path),
        checks=(
            "exact-file-allowlist",
            "path-denylist",
            "content-and-secret-scan",
            "released-documentation-and-examples",
            "public-brand-integrity",
            "fixed-topic-filter-allowlist",
            "mqtt-v5-only-runtime",
            "public-dependency-metadata",
            "safe-tar-member-types",
        ),
    )


def _artifact_digest_map(directory: Path) -> dict[str, str]:
    artifacts = sorted(
        path
        for path in directory.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    wheel_count = sum(path.suffix == ".whl" for path in artifacts)
    sdist_count = sum(path.name.endswith(".tar.gz") for path in artifacts)
    if wheel_count != 1 or sdist_count != 1 or len(artifacts) != 2:
        raise ArtifactPolicyError(
            f"build directory must contain one wheel and one sdist, found {[p.name for p in artifacts]}"
        )
    return {path.name: sha256_file(path) for path in artifacts}


def _string_list(policy: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = policy.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ArtifactPolicyError(f"release policy key {key!r} must be a string list")
    return tuple(value)


def _string_value(policy: dict[str, Any], key: str) -> str:
    value = policy.get(key)
    if not isinstance(value, str) or not value:
        raise ArtifactPolicyError(f"release policy key {key!r} must be a non-empty string")
    return value


def _validate_member_names(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or not name:
            raise ArtifactPolicyError(f"artifact contains an unsafe path: {name!r}")


def _wheel_dist_info_prefix(names: list[str], *, distribution_name: str) -> str:
    candidates = {
        name[: -len("METADATA")] for name in names if name.endswith(".dist-info/METADATA")
    }
    if len(candidates) != 1:
        raise ArtifactPolicyError(f"wheel must contain one dist-info directory, found {candidates}")
    prefix = next(iter(candidates))
    if (
        re.fullmatch(rf"{re.escape(distribution_name)}-[0-9][A-Za-z0-9.]*\.dist-info/", prefix)
        is None
    ):
        raise ArtifactPolicyError(f"unexpected dist-info path: {prefix}")
    return prefix


def _sdist_root(names: list[str]) -> str:
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise ArtifactPolicyError(f"sdist must have one root directory, found {roots}")
    root = next(iter(roots))
    if re.fullmatch(r"picogrid_ecn_client-[0-9][A-Za-z0-9.]*", root) is None:
        raise ArtifactPolicyError(f"unexpected sdist root: {root}")
    return root


def _expected_sdist_files(root: str, policy: dict[str, Any]) -> set[str]:
    package_files = _string_list(policy, "wheel_package_files")
    expected = {
        f"{root}/LICENSE",
        f"{root}/MANIFEST.in",
        f"{root}/PKG-INFO",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
        f"{root}/setup.cfg",
    }
    expected.update(
        f"{root}/{name}"
        for key in (
            "sdist_documentation_files",
            "sdist_example_files",
            "sdist_auxiliary_files",
        )
        for name in _string_list(policy, key)
    )
    expected.update(f"{root}/src/{name}" for name in package_files)
    egg_info = f"{root}/src/{IMPORT_NAME}.egg-info"
    expected.update(
        {
            f"{egg_info}/PKG-INFO",
            f"{egg_info}/SOURCES.txt",
            f"{egg_info}/dependency_links.txt",
            f"{egg_info}/entry_points.txt",
            f"{egg_info}/requires.txt",
            f"{egg_info}/top_level.txt",
        }
    )
    return expected


def _validate_publication_manifest(data: bytes, policy: dict[str, Any]) -> None:
    expected = _publication_manifest_lines(policy)
    try:
        actual = tuple(data.decode("utf-8").splitlines())
    except UnicodeDecodeError as exc:
        raise ArtifactPolicyError("sdist MANIFEST.in is not UTF-8") from exc
    if actual != expected:
        raise ArtifactPolicyError("sdist MANIFEST.in differs from exact publication inventory")


def _require_exact_allowlist(names: list[str], expected: set[str], *, artifact: str) -> None:
    actual = set(names)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        raise ArtifactPolicyError(
            f"{artifact} allowlist mismatch; unexpected={unexpected}, missing={missing}"
        )


def _scan_paths(names: list[str], policy: dict[str, Any]) -> None:
    fragments = tuple(item.casefold() for item in _string_list(policy, "forbidden_path_fragments"))
    suffixes = tuple(item.casefold() for item in _string_list(policy, "forbidden_path_suffixes"))
    approved_generated = tuple(
        name
        for name in _string_list(policy, "sdist_documentation_files")
        if name.startswith("docs/reference/python/")
    )
    for name in names:
        lowered = name.casefold()
        generated_reference = any(
            name == approved or name.endswith(f"/{approved}") for approved in approved_generated
        )
        if not generated_reference and any(fragment in lowered for fragment in fragments):
            raise ArtifactPolicyError(f"prohibited path fragment in artifact: {name}")
        if lowered.endswith(suffixes):
            raise ArtifactPolicyError(f"prohibited credential-like file in artifact: {name}")


def _scan_retired_document_references(
    name: str,
    data: bytes,
    policy: dict[str, Any],
) -> None:
    if not name.casefold().endswith((".md", ".mdx")):
        return
    lowered = data.lower()
    for retired_name in _string_list(policy, "retired_document_references"):
        if retired_name.encode("utf-8").lower() in lowered:
            raise ArtifactPolicyError(f"retired document {retired_name} referenced by {name}")


def _scan_content(name: str, data: bytes, policy: dict[str, Any]) -> None:
    path = PurePosixPath(name)
    if _NONPUBLIC_SDK_IMPORT.search(data):
        raise ArtifactPolicyError(f"non-public SDK reference found in {name}")
    patterns = {
        "unapproved Picogrid repository URL": _UNAPPROVED_PICOGRID_REPOSITORY,
        "private package index": _PRIVATE_INDEX,
        "private API path": _PRIVATE_API_PATH,
    }
    for label, pattern in patterns.items():
        if pattern.search(data):
            raise ArtifactPolicyError(f"{label} found in {name}")

    policy_member = path.parts[-2:] == ("scripts", "release-policy.json")
    scan_secret_and_address_content(
        name,
        data,
        policy,
        allowed_exact_urls=(
            frozenset(_string_list(policy, "generated_site_placeholder_urls"))
            if policy_member
            else frozenset()
        ),
    )

    if IMPORT_NAME in path.parts and path.suffix.casefold() == ".py":
        lowered = data.lower()
        if any(marker in lowered for marker in _RETIRED_RUNTIME_MARKERS):
            raise ArtifactPolicyError(f"retired runtime marker found in {name}")


def scan_secret_and_address_content(
    name: str,
    data: bytes,
    policy: dict[str, Any],
    *,
    allow_synthetic_hosts: bool = False,
    allowed_exact_urls: frozenset[str] = frozenset(),
) -> None:
    """Reject publication secrets and network addresses from one candidate file."""

    patterns = {
        "private key": _PRIVATE_KEY,
        "JWT": _JWT,
        "GitHub token": _GITHUB_TOKEN,
        "cloud access key": _AWS_KEY,
        "provider token": _PROVIDER_TOKEN,
        "provider webhook credential": _SLACK_WEBHOOK,
    }
    for label, pattern in patterns.items():
        if pattern.search(data):
            raise ArtifactPolicyError(f"{label} found in {name}")

    private_hostnames = {match.group().lower() for match in _PRIVATE_HOSTNAME.finditer(data)} - {
        b"google.protobuf.internal"
    }
    if private_hostnames:
        raise ArtifactPolicyError(f"private hostname found in {name}")

    if not _is_text_member(name):
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactPolicyError(f"text publication file is not UTF-8: {name}") from exc

    scan_texts = [text]
    for _ in range(2):
        decoded = unquote(scan_texts[-1])
        if decoded == scan_texts[-1]:
            break
        scan_texts.append(decoded)

    for candidate_text in scan_texts:
        for raw_address in _IPV4.findall(candidate_text.encode("utf-8")):
            try:
                ipv4_address = ipaddress.IPv4Address(raw_address.decode("ascii"))
            except ipaddress.AddressValueError:
                continue
            if not (ipv4_address.is_loopback or ipv4_address.is_unspecified):
                raise ArtifactPolicyError(f"non-loopback IPv4 address found in {name}")

        for candidate in _IPV6_CANDIDATE.findall(candidate_text):
            raw_address = candidate.removeprefix("[").removesuffix("]")
            if raw_address == "::" and not candidate.startswith("["):
                continue
            try:
                ipv6_address = ipaddress.IPv6Address(raw_address)
            except ipaddress.AddressValueError:
                continue
            if not ipv6_address.is_loopback:
                raise ArtifactPolicyError(f"non-loopback IPv6 address found in {name}")

    approved_hostnames = {
        hostname.casefold().rstrip(".")
        for hostname in _string_list(policy, "approved_public_hostnames")
    }
    synthetic_hostnames = (
        {
            hostname.casefold().rstrip(".")
            for hostname in _string_list(policy, "worktree_synthetic_hostnames")
        }
        if allow_synthetic_hosts
        else set()
    )
    allowed_hostnames = approved_hostnames | synthetic_hostnames
    for candidate_text in scan_texts:
        for match in _NETWORK_URL.finditer(candidate_text):
            raw_url = match.group()
            try:
                parsed = urlsplit(raw_url)
                hostname = parsed.hostname
            except ValueError as exc:
                raise ArtifactPolicyError(f"malformed network URL found in {name}") from exc
            if hostname is None:
                raise ArtifactPolicyError(f"malformed network URL found in {name}")
            normalized_hostname = hostname.casefold().rstrip(".")
            if (parsed.username is not None or parsed.password is not None) and not (
                allow_synthetic_hosts and normalized_hostname in synthetic_hostnames
            ):
                raise ArtifactPolicyError(f"credential-bearing URL found in {name}")
            if raw_url in allowed_exact_urls:
                continue
            if not _is_allowed_hostname(normalized_hostname, allowed_hostnames):
                raise ArtifactPolicyError(f"unapproved operational hostname found in {name}")

        for match in _BARE_OPERATIONAL_FQDN.finditer(candidate_text):
            hostname = match.group().casefold().rstrip(".")
            if hostname not in allowed_hostnames:
                raise ArtifactPolicyError(f"unapproved operational hostname found in {name}")


def _is_allowed_hostname(hostname: str, allowed_hostnames: set[str]) -> bool:
    if hostname == "localhost" or hostname in allowed_hostnames:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _is_text_member(name: str) -> bool:
    path = PurePosixPath(name)
    return path.suffix.casefold() in _TEXT_SUFFIXES or path.name in {
        ".dockerignore",
        ".env.example",
        ".gitignore",
        "Containerfile",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "METADATA",
        "PKG-INFO",
        "RECORD",
        "WHEEL",
    }


def _validate_long_description_links(metadata: Message, *, label: str) -> None:
    description = metadata.get_payload()
    if not isinstance(description, str):
        raise ArtifactPolicyError(f"{label} long description is malformed")
    for match in _MARKDOWN_LINK.finditer(description):
        target = match.group("target").strip("<>")
        if target.startswith("#"):
            continue
        if urlsplit(target).scheme not in {"https", "mailto"}:
            raise ArtifactPolicyError(f"{label} long description contains a relative link")


def _validate_metadata(data: bytes, policy: dict[str, Any]) -> None:
    metadata = BytesParser().parsebytes(data)
    if metadata.get("Name") != PACKAGE_NAME:
        raise ArtifactPolicyError("wheel metadata has an unexpected project name")
    policy_version = _string_value(policy, "project_version")
    metadata_version = metadata.get("Version")
    if metadata_version is None or _normalize_version(metadata_version) != _normalize_version(
        policy_version
    ):
        raise ArtifactPolicyError("wheel metadata version changed")
    if metadata.get("Requires-Python") != _string_value(policy, "requires_python"):
        raise ArtifactPolicyError("wheel Python requirement changed")
    expected_license_expression = _string_value(policy, "license_expression")
    actual_license_expression = metadata.get("License-Expression")
    if actual_license_expression != expected_license_expression:
        raise ArtifactPolicyError(
            "wheel License-Expression mismatch: "
            f"expected {expected_license_expression!r}, got {actual_license_expression!r}"
        )
    requirements = metadata.get_all("Requires-Dist", failobj=[])
    expected_requirements = set(_string_list(policy, "runtime_requirements"))
    if len(requirements) != len(set(requirements)) or set(requirements) != expected_requirements:
        raise ArtifactPolicyError(
            "wheel runtime requirements changed: "
            f"{sorted(requirements)} != {sorted(expected_requirements)}"
        )
    dependency_names = {
        re.split(r"[\s(<=>;\[]", requirement, maxsplit=1)[0].replace("_", "-").casefold()
        for requirement in requirements
    }
    direct_policy = policy.get("direct_runtime_dependencies")
    if not isinstance(direct_policy, dict):
        raise ArtifactPolicyError("direct runtime dependency policy must be an object")
    expected = {name.replace("_", "-").casefold() for name in direct_policy}
    if dependency_names != expected:
        raise ArtifactPolicyError(
            f"wheel runtime dependency set changed: {sorted(dependency_names)} != {sorted(expected)}"
        )
    if any(
        "@" in requirement or "picogrid" in requirement.casefold() for requirement in requirements
    ):
        raise ArtifactPolicyError("wheel metadata contains a private or direct-URL dependency")
    expected_urls = policy.get("project_urls")
    if not isinstance(expected_urls, dict) or not all(
        isinstance(label, str) and isinstance(url, str) for label, url in expected_urls.items()
    ):
        raise ArtifactPolicyError("project URL policy must be a string mapping")
    actual_urls: dict[str, str] = {}
    for value in metadata.get_all("Project-URL", failobj=[]):
        label, separator, url = value.partition(", ")
        if not separator or not label or label in actual_urls:
            raise ArtifactPolicyError("wheel metadata contains a malformed project URL")
        actual_urls[label] = url
    if actual_urls != expected_urls:
        raise ArtifactPolicyError("wheel project URLs differ from the publication policy")
    _validate_long_description_links(metadata, label="wheel")


def _validate_operator_metadata(data: bytes, policy: dict[str, Any]) -> None:
    metadata = BytesParser().parsebytes(data)
    if metadata.get("Name") != OPERATOR_PACKAGE_NAME:
        raise ArtifactPolicyError("operator wheel metadata has an unexpected project name")
    metadata_version = metadata.get("Version")
    if metadata_version is None or _normalize_version(metadata_version) != _normalize_version(
        _string_value(policy, "project_version")
    ):
        raise ArtifactPolicyError("operator wheel metadata version changed")
    if metadata.get("Requires-Python") != _string_value(policy, "requires_python"):
        raise ArtifactPolicyError("operator wheel Python requirement changed")
    if metadata.get("License-Expression") != _string_value(policy, "license_expression"):
        raise ArtifactPolicyError("operator wheel legal-review license marker changed")
    requirements = metadata.get_all("Requires-Dist", failobj=[])
    expected_requirements = {
        *_string_list(policy, "operator_runtime_requirements"),
        f"{PACKAGE_NAME}=={_normalize_version(_string_value(policy, 'project_version'))}",
    }
    if len(requirements) != len(set(requirements)) or set(requirements) != expected_requirements:
        raise ArtifactPolicyError(
            "operator wheel runtime requirements changed: "
            f"{sorted(requirements)} != {sorted(expected_requirements)}"
        )
    if any("@" in requirement for requirement in requirements):
        raise ArtifactPolicyError("operator wheel metadata contains a direct-URL dependency")
    if metadata.get_all("Project-URL", failobj=[]):
        raise ArtifactPolicyError("operator wheel metadata contains unreviewed project URLs")
    _validate_long_description_links(metadata, label="operator wheel")


def _validate_wheel_metadata(data: bytes) -> None:
    text = data.decode("utf-8")
    if "Root-Is-Purelib: true" not in text:
        raise ArtifactPolicyError("initial distribution must remain a pure-Python wheel")
    if "Tag: py3-none-any" not in text:
        raise ArtifactPolicyError("initial distribution must retain its universal Python tag")


def _validate_topic_filters(data: bytes, policy: dict[str, Any]) -> None:
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ArtifactPolicyError("could not parse the retained topic policy") from exc
    discovered: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.endswith("_SUBSCRIPTION"):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            raise ArtifactPolicyError(f"topic filter {target.id} must be a literal string")
        discovered.add(node.value.value)
    approved = set(_string_list(policy, "approved_topic_filters"))
    fixed_depth_filters = {
        "entity/+/+/+",
        "entity_pb/+/+",
        "entity_location/+/+",
        "entity_location_pb/+/+",
    }
    if approved != fixed_depth_filters:
        raise ArtifactPolicyError("release policy MQTT filters are not the fixed-depth allowlist")
    if discovered != approved:
        raise ArtifactPolicyError(
            f"fixed MQTT subscription set changed: {sorted(discovered)} != {sorted(approved)}"
        )
    docstring_nodes = {
        id(owner.body[0].value)
        for owner in ast.walk(tree)
        if isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
        and isinstance(owner.body[0].value.value, str)
    }
    hash_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
        and "#" in node.value
    }
    if hash_literals:
        raise ArtifactPolicyError(
            f"unapproved MQTT wildcard literals found: {sorted(hash_literals)}"
        )


def _validate_mqtt_v5_runtime(contents: dict[str, bytes], *, package_prefix: str) -> None:
    transport_path = f"{package_prefix}_transport/mqtt.py"
    mock_path = f"{package_prefix}testing/_mqtt.py"
    transport = contents.get(transport_path)
    mock = contents.get(mock_path)
    if transport is None or mock is None:
        raise ArtifactPolicyError("MQTT v5 runtime files are missing")
    if b"paho.MQTTv5" not in transport:
        raise ArtifactPolicyError("MQTT transport does not require protocol v5")
    if re.search(rb"protocol_level\s*!=\s*5\b", mock) is None:
        raise ArtifactPolicyError("offline mock does not require MQTT protocol level 5")
