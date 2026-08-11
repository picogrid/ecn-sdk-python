# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Prove that every place the repository writes the SDK version agrees.

The released version is written down in many places: distribution metadata, the
documentation site's own package, the release policy the verifier enforces, the
public `__version__`, the dependency lock, and the artifact filenames printed in
the install and verification instructions. A reader who follows any one of them
must arrive at the same release.

Release automation is what keeps them together, and the failure this module
exists to prevent is silent: a version that automation does not bump does not
announce itself, it simply goes stale, and every other gate keeps passing because
each one reads a different copy. So agreement alone is not enough to check. Three
further properties are enforced, each of which has to hold for agreement to
survive the next release:

- Every file holding a version is one release automation is configured to update,
  so no copy depends on a strategy's undocumented file discovery.
- Every version written in prose sits on a line carrying the release-please
  annotation, because an unannotated line is one automation will silently skip.
- No prose document quotes the version except on a line declared here, so a copy
  added to the guide later cannot hide among the ones already known.

That last scan reads documentation only. It was briefly applied to the whole
repository and withdrawn: a version number carries no evidence of whose software
it belongs to, so every dependency pin, workflow tool pin, action label, and
comparator that might one day equal this release needed its own exemption, and
the gate's failure mode was to block a legitimate release. Structured files state
their versions where this module can read them directly instead.

`pyproject.toml` is canonical: it is what the build reads, so it is the version
the published wheel actually carries.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from scripts.release_checks import DOCUMENTATION_WORKSPACE_DIRECTORIES

PACKAGE_NAME = "picogrid-ecn-client"
IMPORT_NAME = "picogrid_ecn_client"
ANNOTATION = "x-release-please-version"
CANONICAL_PATH = "pyproject.toml"
RELEASE_PLEASE_CONFIG = ".github/release-please-config.json"

_STRUCTURED_UPDATERS = {
    ("docs/package.json", "docs site"): ("json", "$.version"),
    ("docs/package-lock.json", "docs lockfile"): ("json", "$.version"),
    (
        "docs/package-lock.json",
        "docs lockfile root package",
    ): ("json", "$.packages[''].version"),
    ("scripts/release-policy.json", "release policy"): ("json", "$.project_version"),
    (
        "uv.lock",
        "dependency lock",
    ): ("toml", "$.package[?(@.name=='picogrid-ecn-client')].version"),
}

_VERSION = r"\d+\.\d+\.\d+(?:-?(?:a|b|rc)\d+)?"
# The version as it appears inside a built artifact's name, which is how the
# install and verification instructions quote it.
_ARTIFACT = re.compile(rf"{IMPORT_NAME}-({_VERSION})(?:-py3-none-any\.whl|\.tar\.gz)")
# The version as it appears pinning this SDK as a dependency.
_REQUIREMENT = re.compile(rf"(?<![\w.-]){re.escape(PACKAGE_NAME)}==([^,\s;\"'\])]+)")
# The version as a release-facing document quotes it when stating which release
# is current.
_CLAIM = re.compile(rf"`({_VERSION})`")


def _normalize_version(version: str) -> str:
    """Normalize a version string to canonical PEP 440 form.

    Converts dashed pre-release spelling (0.1.0-rc1) to canonical (0.1.0rc1).
    """
    try:
        return str(Version(version))
    except InvalidVersion as exc:
        raise VersionSyncError(f"invalid version string: {version}") from exc


class VersionSyncError(RuntimeError):
    """Raised when the repository states more than one released version."""


@dataclass(frozen=True, slots=True)
class Statement:
    """One place the repository writes the released version down."""

    path: str
    line: int
    version: str
    description: str


def _read(repository: Path, relative: str) -> str:
    path = repository / relative
    if path.is_symlink() or not path.is_file():
        raise VersionSyncError(f"{relative} is missing or is not a regular file")
    return path.read_text(encoding="utf-8")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _pyproject_version(text: str, relative: str) -> Statement:
    """The `[project]` version, which is what the build publishes."""

    document = tomllib.loads(text)
    version = document.get("project", {}).get("version")
    if not isinstance(version, str):
        raise VersionSyncError(f"{relative} has no [project] version")
    match = re.search(rf'^version = "{re.escape(version)}"$', text, re.MULTILINE)
    if match is None:
        raise VersionSyncError(f"{relative} does not declare its version on one plain line")
    return Statement(relative, _line_of(text, match.start()), version, "distribution metadata")


def _json_version(text: str, relative: str, field: str, description: str) -> Statement:
    document = json.loads(text)
    version = document.get(field)
    if not isinstance(version, str):
        raise VersionSyncError(f"{relative} has no {field}")
    match = re.search(rf'^\s*"{re.escape(field)}": "{re.escape(version)}",?$', text, re.MULTILINE)
    if match is None:
        raise VersionSyncError(f"{relative} does not declare {field} on one plain line")
    return Statement(relative, _line_of(text, match.start()), version, description)


def _package_lock_versions(text: str, relative: str) -> list[Statement]:
    """The documentation package versions npm records at the lockfile root."""

    document = json.loads(text)
    root_version = document.get("version")
    package_version = document.get("packages", {}).get("", {}).get("version")
    if not isinstance(root_version, str) or not isinstance(package_version, str):
        raise VersionSyncError(f"{relative} has no root package versions")

    packages_index = text.find('"packages"')
    root_package_index = text.find('"": {', packages_index)
    if packages_index < 0 or root_package_index < 0:
        raise VersionSyncError(f"{relative} does not declare its root package on plain lines")

    def locate(version: str, start: int, end: int, description: str) -> Statement:
        match = re.search(
            rf'^\s*"version":\s*"{re.escape(version)}",?$',
            text[start:end],
            re.MULTILINE,
        )
        if match is None:
            raise VersionSyncError(f"{relative} does not declare {description} on one plain line")
        return Statement(
            relative,
            _line_of(text, start + match.start()),
            version,
            description,
        )

    return [
        locate(root_version, 0, packages_index, "docs lockfile"),
        locate(package_version, root_package_index, len(text), "docs lockfile root package"),
    ]


def _dunder_version(text: str, relative: str) -> Statement:
    """The public `__version__`, read as an assignment rather than as a pattern."""

    for node in ast.parse(text).body:
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "__version__" not in names:
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            raise VersionSyncError(f"{relative} does not assign __version__ a plain string")
        return Statement(relative, node.lineno, node.value.value, "public __version__")
    raise VersionSyncError(f"{relative} does not assign __version__")


def _locked_version(text: str, relative: str) -> Statement:
    """This SDK's own entry in the dependency lock."""

    for package in tomllib.loads(text).get("package", []):
        if package.get("name") != PACKAGE_NAME:
            continue
        version = package.get("version")
        if not isinstance(version, str):
            raise VersionSyncError(f"{relative} locks {PACKAGE_NAME} without a version")
        # Located from this package's own entry rather than by searching the
        # whole lock, where other packages may sit at the same version.
        entry = re.search(rf'^name = "{re.escape(PACKAGE_NAME)}"$', text, re.MULTILINE)
        after = text.index(f'version = "{version}"', entry.start()) if entry else None
        return Statement(
            relative,
            _line_of(text, after) if after is not None else 0,
            version,
            "dependency lock",
        )
    raise VersionSyncError(f"{relative} does not lock {PACKAGE_NAME}")


def _quoted_versions(text: str, relative: str, description: str) -> list[Statement]:
    """Versions quoted in prose, as artifact names or as a dependency pin."""

    found = [
        Statement(relative, _line_of(text, match.start()), match.group(1), description)
        for pattern in (_ARTIFACT, _REQUIREMENT)
        for match in pattern.finditer(text)
    ]
    if not found:
        raise VersionSyncError(f"{relative} no longer quotes a released version")
    return sorted(found, key=lambda statement: statement.line)


def _claimed_versions(text: str, relative: str, description: str) -> list[Statement]:
    """Versions a release-facing document claims as the current one.

    A claim is marked by the release-please annotation on its own line, which is
    both what automation updates and what distinguishes a claim about this
    release from prose about some other version, such as a future milestone.
    """

    found = [
        Statement(relative, number, match.group(1), description)
        for number, line in enumerate(text.splitlines(), start=1)
        if ANNOTATION in line
        for match in [_CLAIM.search(line)]
        if match is not None
    ]
    if not found:
        raise VersionSyncError(
            f"{relative} no longer claims a released version on an annotated line"
        )
    return found


# Every file that writes the released version down, and how it writes it. A file
# added here must also be added to release automation, which `check_version_sync`
# enforces rather than assumes.
_PROSE = (
    ("README.md", "install instructions"),
    ("docs/getting-started/installation.md", "install guide"),
    ("VERIFYING_ARTIFACTS.md", "verification instructions"),
    ("operator-app/pyproject.toml", "operator application dependency pin"),
)

# Release-facing policy documents that ship in the source distribution and state
# which version is current. They are claims automation must advance, not history.
_CLAIMS = (("VERSIONING.md", "versioning policy"),)

# Prose documents that deliberately quote a historical release, a candidate
# baseline, a generated reference bound to one, or another project's version.
# They are not claims release automation should advance, so the documentation
# scan skips them. `NOTICE.md` is the third case: it records the upstream
# versions of third-party material — a font package, a rasterizer — and states
# no release of this SDK, so a coincidence between one of those numbers and
# this release must not block the release.
_BASELINE_DOCUMENTS = frozenset(
    {
        "CHANGELOG.md",
        "DECISIONS.md",
        "NOTICE.md",
        "PUBLICATION_REVIEW.md",
        # Internal upstream contract record absent from the distribution.
        "PUBLIC_API.md",
        "RELEASING.md",
        "docs/concepts/mesh-routing.md",
        "docs/concepts/tasks.md",
        "docs/getting-started/authentication.md",
        "docs/how-to/dispatch-local-tasks.md",
        "docs/how-to/receive-local-tasks.md",
        "docs/integrations/effectors.md",
    }
)
_GENERATED_DOCUMENTATION = "docs/reference/python/"
# The documentation workspace holds its own tooling, dependencies and build
# caches beside the published prose. Only the prose states a version, and which
# trees are tooling is stated once, by the release inventory.
_DOCUMENTATION_TOOLING_PREFIXES = tuple(
    f"docs/{name}/" for name in sorted(DOCUMENTATION_WORKSPACE_DIRECTORIES)
)

# These exact lines name a compatibility milestone rather than the current
# release, and only need exempting in the release that reaches the milestone
# they name. They carry the version as a slot, so the exemption holds no version
# of its own and any prose edit drops it, failing the gate until a maintainer
# re-audits the sentence.
_MILESTONE_MENTION_LINES = {
    "VERSIONING.md": (
        "- After `{version}`, a breaking change to a public import, model, method signature,",
        "release, but the release notes must call out the break explicitly. After `{version}`, a",
    ),
}


def _documentation_texts(repository: Path) -> Iterator[tuple[str, str]]:
    """Yield the prose documents a reader is asked to trust, in path order."""

    patterns = ("*.md", "*.mdx", "docs/**/*.md", "docs/**/*.mdx")
    candidates = {path for pattern in patterns for path in repository.glob(pattern)}
    policy = json.loads(_read(repository, "scripts/release-policy.json"))
    root = repository.resolve()
    for declared in policy.get("sdist_auxiliary_files", ()):
        if not isinstance(declared, str) or Path(declared).suffix not in {".md", ".mdx"}:
            continue
        # A declared auxiliary path is repository-relative. One that escapes the
        # checkout — absolute, or reaching upward — is a policy error, not a
        # document to scan. Skipping it silently would let the policy name a
        # document this gate never reads, so it is refused. The candidate itself
        # stays unresolved so the symlink refusal below still sees a symlink.
        candidate = repository / declared
        if root not in candidate.resolve().parents:
            raise VersionSyncError(
                "scripts/release-policy.json declares an auxiliary document outside"
                f" the repository: {declared}"
            )
        candidates.add(candidate)

    for path in sorted(candidates):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(repository).as_posix()
        if (
            relative in _BASELINE_DOCUMENTS
            or relative.startswith(_GENERATED_DOCUMENTATION)
            or relative.startswith(_DOCUMENTATION_TOOLING_PREFIXES)
        ):
            continue
        try:
            yield relative, path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def collect_statements(repository: Path) -> list[Statement]:
    """Every statement of the released version, in a stable order."""

    statements = [
        _pyproject_version(_read(repository, CANONICAL_PATH), CANONICAL_PATH),
        _json_version(
            _read(repository, "docs/package.json"),
            "docs/package.json",
            "version",
            "docs site",
        ),
        *_package_lock_versions(
            _read(repository, "docs/package-lock.json"), "docs/package-lock.json"
        ),
        _json_version(
            _read(repository, "scripts/release-policy.json"),
            "scripts/release-policy.json",
            "project_version",
            "release policy",
        ),
        _dunder_version(
            _read(repository, f"src/{IMPORT_NAME}/__init__.py"),
            f"src/{IMPORT_NAME}/__init__.py",
        ),
        _locked_version(_read(repository, "uv.lock"), "uv.lock"),
    ]
    for relative, description in _PROSE:
        statements.extend(_quoted_versions(_read(repository, relative), relative, description))
    for relative, description in _CLAIMS:
        statements.extend(_claimed_versions(_read(repository, relative), relative, description))
    return statements


def _automated_paths(repository: Path) -> list[dict[str, object]]:
    """The complete updater entries configured for release automation."""

    document = json.loads(_read(repository, RELEASE_PLEASE_CONFIG))
    packages = document.get("packages", {})
    if list(packages) != ["."]:
        raise VersionSyncError("release automation no longer configures exactly one package")
    entries = [
        entry
        for entry in packages["."].get("extra-files", [])
        if isinstance(entry, dict) and "path" in entry
    ]
    # The release strategy owns the distribution metadata itself.
    return [*entries, {"path": CANONICAL_PATH, "type": "release-type"}]


def check_version_sync(repository: Path) -> str:
    """Return the released version, or raise with every disagreement found."""

    statements = collect_statements(repository)
    canonical = next(
        statement.version for statement in statements if statement.path == CANONICAL_PATH
    )
    canonical_normalized = _normalize_version(canonical)
    automated = _automated_paths(repository)
    failures: list[str] = []

    for statement in statements:
        where = f"{statement.path}:{statement.line}"
        if _normalize_version(statement.version) != canonical_normalized:
            failures.append(
                f"{where} states {statement.description} version {statement.version},"
                f" but {CANONICAL_PATH} states {canonical}"
            )
        expected_type, expected_jsonpath = _STRUCTURED_UPDATERS.get(
            (statement.path, statement.description),
            ("release-type", None) if statement.path == CANONICAL_PATH else ("generic", None),
        )
        updater = next(
            (
                entry
                for entry in automated
                if entry.get("path") == statement.path
                and entry.get("type") == expected_type
                and (expected_jsonpath is None or entry.get("jsonpath") == expected_jsonpath)
            ),
            None,
        )
        if updater is None:
            requirement = f"type {expected_type}"
            if expected_jsonpath is not None:
                requirement += f" and jsonpath {expected_jsonpath}"
            failures.append(
                f"{where} holds the version but release automation does not update"
                f" {statement.path} with {requirement}; add that updater to extra-files"
                f" in {RELEASE_PLEASE_CONFIG}"
            )

    # The generic updater only rewrites lines it is told to, so a version it is
    # responsible for that is not annotated is one it will silently skip.
    for statement in statements:
        if not any(
            entry.get("path") == statement.path and entry.get("type") == "generic"
            for entry in automated
        ):
            continue
        source = _read(repository, statement.path).splitlines()[statement.line - 1]
        if ANNOTATION not in source:
            failures.append(
                f"{statement.path}:{statement.line} states the version on a line without the"
                f" {ANNOTATION} annotation, so release automation will not update it"
            )

    # A version quoted in a document nobody declared agrees today and goes stale
    # at the next release, because automation will not rewrite a file it was
    # never told about. Only prose is scanned: a structured file states other
    # software's versions too, and cannot say which of them is this release.
    declared = {(statement.path, statement.line) for statement in statements}
    version_token = re.compile(rf"(?<![\d.])({_VERSION})(?!\.[0-9A-Za-z])(?![\w+-])")
    spellings = {canonical, canonical_normalized}
    for relative, text in _documentation_texts(repository):
        milestones = {
            template.format(version=spelling)
            for template in _MILESTONE_MENTION_LINES.get(relative, ())
            for spelling in spellings
        }
        lines = text.splitlines()
        for match in version_token.finditer(text):
            if _normalize_version(match.group(1)) != canonical_normalized:
                continue
            line_number = _line_of(text, match.start())
            if lines[line_number - 1] in milestones:
                continue
            if (relative, line_number) not in declared:
                failures.append(
                    f"{relative}:{line_number} states the released version somewhere"
                    " scripts/version_sync.py does not know about"
                )

    if failures:
        raise VersionSyncError("\n".join(sorted(set(failures))))
    return canonical


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    try:
        version = check_version_sync(repository)
    except VersionSyncError as error:
        print(f"version sync failed:\n{error}")
        return 1
    statements = collect_statements(repository)
    print(f"version sync passed: {len(statements)} statements agree on {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
