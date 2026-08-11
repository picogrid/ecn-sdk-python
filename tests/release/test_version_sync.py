# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""The version-agreement gate, exercised against a repository built to be broken.

Each test starts from a synthetic repository that passes, then breaks exactly one
property, so a failure names the property rather than the fixture. The real
repository is checked too, which is what keeps the synthetic one honest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.release_checks import load_policy
from scripts.version_sync import (
    VersionSyncError,
    _normalize_version,
    check_version_sync,
    collect_statements,
)

REPOSITORY = Path(__file__).parents[2]
POLICY_PATH = REPOSITORY / "scripts" / "release-policy.json"


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def replace(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{relative} does not contain {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def set_fixture_version(root: Path, version: str) -> None:
    for relative in (
        "pyproject.toml",
        "docs/package.json",
        "docs/package-lock.json",
        "scripts/release-policy.json",
        "src/picogrid_ecn_client/__init__.py",
        "uv.lock",
        "README.md",
        "docs/getting-started/installation.md",
        "VERIFYING_ARTIFACTS.md",
        "operator-app/pyproject.toml",
        "VERSIONING.md",
    ):
        path = root / relative
        path.write_text(
            path.read_text(encoding="utf-8").replace("1.2.3", version),
            encoding="utf-8",
        )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A minimal repository that satisfies every property the gate enforces."""

    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "picogrid-ecn-client"\nversion = "1.2.3"\n',
    )
    write(tmp_path, "docs/package.json", '{\n  "name": "docs",\n  "version": "1.2.3"\n}\n')
    write(
        tmp_path,
        "docs/package-lock.json",
        '{\n  "name": "docs",\n  "version": "1.2.3",\n  "lockfileVersion": 3,\n'
        '  "packages": {\n    "": {\n      "name": "docs",\n      "version": "1.2.3"\n'
        "    }\n  }\n}\n",
    )
    write(
        tmp_path,
        "scripts/release-policy.json",
        '{\n  "project_version": "1.2.3",\n'
        '  "sdist_auxiliary_files": [\n'
        '    "NOTICE.md",\n'
        '    "operator-app/README.md"\n'
        "  ]\n}\n",
    )
    write(
        tmp_path,
        "src/picogrid_ecn_client/__init__.py",
        '__version__ = "1.2.3"  # x-release-please-version\n',
    )
    write(
        tmp_path,
        "uv.lock",
        '[[package]]\nname = "anyio"\nversion = "1.2.3"\n\n'
        '[[package]]\nname = "picogrid-ecn-client"\nversion = "1.2.3"\n',
    )
    for relative in ("README.md", "docs/getting-started/installation.md"):
        write(
            tmp_path,
            relative,
            "Install it:\n\n"
            "python -m pip install ./picogrid_ecn_client-1.2.3-py3-none-any.whl"
            " # x-release-please-version\n",
        )
    write(
        tmp_path,
        "VERIFYING_ARTIFACTS.md",
        "gh attestation verify picogrid_ecn_client-1.2.3.tar.gz # x-release-please-version\n",
    )
    write(
        tmp_path,
        "operator-app/pyproject.toml",
        '[project]\nname = "operator-app"\nversion = "9.9.9"\ndependencies = [\n'
        '  "picogrid-ecn-client==1.2.3", # x-release-please-version\n]\n',
    )
    write(tmp_path, "operator-app/README.md", "Operator application.\n")
    write(
        tmp_path,
        "VERSIONING.md",
        "`1.2.3` identifies the current release candidate. <!-- x-release-please-version -->\n",
    )
    write(
        tmp_path,
        "PUBLIC_API.md",
        "Version `1.2.3` is alpha.\n"
        "After `1.0.0`, an incompatible change requires a major release.\n",
    )
    write(
        tmp_path,
        ".github/release-please-config.json",
        """{
  "packages": {
    ".": {
      "extra-files": [
        {"type": "generic", "path": "README.md"},
        {"type": "generic", "path": "docs/getting-started/installation.md"},
        {"type": "generic", "path": "VERIFYING_ARTIFACTS.md"},
        {"type": "generic", "path": "operator-app/pyproject.toml"},
        {"type": "generic", "path": "VERSIONING.md"},
        {"type": "generic", "path": "src/picogrid_ecn_client/__init__.py"},
        {"type": "json", "path": "docs/package.json", "jsonpath": "$.version"},
        {"type": "json", "path": "docs/package-lock.json", "jsonpath": "$.version"},
        {"type": "json", "path": "docs/package-lock.json",
         "jsonpath": "$.packages[''].version"},
        {"type": "json", "path": "scripts/release-policy.json",
         "jsonpath": "$.project_version"},
        {"type": "toml", "path": "uv.lock",
         "jsonpath": "$.package[?(@.name=='picogrid-ecn-client')].version"}
      ]
    }
  }
}
""",
    )
    return tmp_path


def test_the_fixture_repository_agrees_with_itself(repository: Path) -> None:
    assert check_version_sync(repository) == "1.2.3"


def test_the_real_repository_agrees_with_its_release_policy() -> None:
    policy = load_policy(POLICY_PATH)

    assert check_version_sync(REPOSITORY) == str(policy["project_version"])


def test_every_statement_the_real_repository_makes_is_located() -> None:
    statements = collect_statements(REPOSITORY)
    paths = {statement.path for statement in statements}

    assert "pyproject.toml" in paths
    assert "docs/package.json" in paths
    assert "src/picogrid_ecn_client/__init__.py" in paths
    # A located statement can be pointed at, which is what makes a failure
    # actionable rather than merely true.
    assert all(statement.line > 0 for statement in statements)


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("docs/package.json", '"version": "1.2.3"', '"version": "1.2.4"'),
        ("scripts/release-policy.json", '"1.2.3"', '"1.2.4"'),
        ("src/picogrid_ecn_client/__init__.py", '"1.2.3"', '"1.2.4"'),
        (
            "uv.lock",
            'name = "picogrid-ecn-client"\nversion = "1.2.3"',
            'name = "picogrid-ecn-client"\nversion = "1.2.4"',
        ),
        ("README.md", "client-1.2.3-py3", "client-1.2.4-py3"),
        ("docs/getting-started/installation.md", "client-1.2.3-py3", "client-1.2.4-py3"),
        ("VERIFYING_ARTIFACTS.md", "client-1.2.3.tar", "client-1.2.4.tar"),
        ("operator-app/pyproject.toml", "client==1.2.3", "client==1.2.4"),
    ],
)
def test_drift_at_any_single_location_is_reported(
    repository: Path, relative: str, old: str, new: str
) -> None:
    replace(repository, relative, old, new)

    with pytest.raises(VersionSyncError) as raised:
        check_version_sync(repository)

    assert relative in str(raised.value)
    assert "1.2.4" in str(raised.value)


def test_a_version_on_an_unannotated_line_is_reported(repository: Path) -> None:
    replace(repository, "README.md", " # x-release-please-version", "")

    with pytest.raises(VersionSyncError, match="annotation"):
        check_version_sync(repository)


def test_a_file_release_automation_does_not_update_is_reported(repository: Path) -> None:
    replace(
        repository,
        ".github/release-please-config.json",
        '{"type": "generic", "path": "src/picogrid_ecn_client/__init__.py"},',
        "",
    )

    with pytest.raises(VersionSyncError, match="release automation does not update"):
        check_version_sync(repository)


@pytest.mark.parametrize(
    ("entry", "selector", "relative"),
    [
        pytest.param(
            '{"type": "json", "path": "docs/package.json", "jsonpath": "$.version"}',
            "$.version",
            "docs/package.json",
            id="package-json-version",
        ),
        pytest.param(
            '{"type": "json", "path": "docs/package-lock.json", "jsonpath": "$.version"}',
            "$.version",
            "docs/package-lock.json",
            id="package-lock-version",
        ),
        pytest.param(
            '{"type": "json", "path": "docs/package-lock.json",\n'
            '         "jsonpath": "$.packages[\'\'].version"}',
            "$.packages[''].version",
            "docs/package-lock.json",
            id="package-lock-root-package-version",
        ),
        pytest.param(
            '{"type": "json", "path": "scripts/release-policy.json",\n'
            '         "jsonpath": "$.project_version"}',
            "$.project_version",
            "scripts/release-policy.json",
            id="release-policy-project-version",
        ),
        pytest.param(
            '{"type": "toml", "path": "uv.lock",\n'
            '         "jsonpath": "$.package[?(@.name==\'picogrid-ecn-client\')].version"}',
            "$.package[?(@.name=='picogrid-ecn-client')].version",
            "uv.lock",
            id="uv-lock-package-version",
        ),
    ],
)
def test_a_structured_version_without_its_exact_updater_is_reported(
    repository: Path, entry: str, selector: str, relative: str
) -> None:
    replace(
        repository,
        ".github/release-please-config.json",
        entry,
        entry.replace(selector, f"{selector}.stale"),
    )

    with pytest.raises(VersionSyncError) as raised:
        check_version_sync(repository)

    failure = str(raised.value)
    assert relative in failure
    assert selector in failure
    assert "extra-files" in failure


def append(root: Path, relative: str, line: str) -> None:
    path = root / relative
    path.write_text(f"{path.read_text(encoding='utf-8')}\n{line}\n", encoding="utf-8")


def test_an_extra_copy_quoted_in_prose_is_reported(repository: Path) -> None:
    """The failure the gate exists for: a copy that agrees now and goes stale."""

    append(repository, "README.md", "Release 1.2.3 is the current line.")

    with pytest.raises(VersionSyncError, match="does not know about"):
        check_version_sync(repository)


def test_a_canonical_version_in_a_new_documentation_file_is_reported(
    repository: Path,
) -> None:
    write(repository, "docs/how-to/new-workflow.md", "Use SDK release 1.2.3.\n")

    with pytest.raises(VersionSyncError, match=re.escape("docs/how-to/new-workflow.md")):
        check_version_sync(repository)


def test_a_canonical_version_in_a_new_mdx_documentation_file_is_reported(
    repository: Path,
) -> None:
    write(repository, "docs/how-to/new-workflow.mdx", "Use SDK release 1.2.3.\n")

    with pytest.raises(VersionSyncError, match=re.escape("docs/how-to/new-workflow.mdx")):
        check_version_sync(repository)


def test_a_canonical_version_in_a_shipped_non_root_document_is_reported(
    repository: Path,
) -> None:
    append(repository, "operator-app/README.md", "Current SDK release 1.2.3")

    with pytest.raises(VersionSyncError, match=re.escape("operator-app/README.md:3")):
        check_version_sync(repository)


def test_a_canonical_version_in_a_fixture_document_is_not_reported(
    repository: Path,
) -> None:
    write(
        repository,
        "tests/release/fixtures/generated-reference.md",
        "Generated against SDK release 1.2.3.\n",
    )

    assert check_version_sync(repository) == "1.2.3"


@pytest.mark.parametrize("form", ["absolute", "traversal"])
def test_an_auxiliary_path_outside_the_repository_is_refused(
    repository: Path, tmp_path: Path, form: str
) -> None:
    """A declared auxiliary path is repository-relative; one that escapes is refused.

    Admitted, an absolute entry crashes the scan on `relative_to` and a traversal
    entry reads prose from outside the checkout. Skipping it quietly would leave the
    policy naming a document this gate never reads, so the policy error is reported
    rather than tolerated.
    """
    outside = tmp_path.parent / "outside-the-checkout.md"
    outside.write_text("Use SDK release 1.2.3.\n", encoding="utf-8")
    escaping = str(outside) if form == "absolute" else "../outside-the-checkout.md"
    write(
        repository,
        "scripts/release-policy.json",
        '{\n  "project_version": "1.2.3",\n'
        '  "sdist_auxiliary_files": [\n'
        '    "NOTICE.md",\n'
        f'    "{escaping}"\n'
        "  ]\n}\n",
    )

    with pytest.raises(VersionSyncError, match="outside the repository"):
        check_version_sync(repository)


def test_longer_versions_containing_the_canonical_version_are_not_reported(
    repository: Path,
) -> None:
    write(
        repository,
        "docs/how-to/version-examples.md",
        "Older examples include 11.2.3 and 1.2.30.\n",
    )

    assert check_version_sync(repository) == "1.2.3"


@pytest.mark.parametrize(
    "suffix",
    (".1", "rc1", "-rc.1", "+local", ".post1", ".post", ".dev1", ".dev"),
)
def test_a_version_continuation_is_not_the_released_version(repository: Path, suffix: str) -> None:
    """The released version is a whole token, not the head of a longer one."""

    write(repository, "docs/how-to/version-examples.md", f"Try 1.2.3{suffix} instead.\n")

    assert check_version_sync(repository) == "1.2.3"


def test_a_dependency_pin_continuation_is_not_the_released_version(repository: Path) -> None:
    """`==1.2.3.post1` pins a different artifact, and must not read as agreement."""

    replace(
        repository,
        "operator-app/pyproject.toml",
        "picogrid-ecn-client==1.2.3",
        "picogrid-ecn-client==1.2.3.post1",
    )

    with pytest.raises(VersionSyncError, match=r"1\.2\.3\.post1"):
        check_version_sync(repository)


def test_a_third_party_version_in_the_notice_does_not_block_the_release(
    repository: Path,
) -> None:
    """`NOTICE.md` records other projects' versions, never a release of this SDK."""

    write(
        repository,
        "NOTICE.md",
        "| `@fontsource-variable/chivo-mono@1.2.3` | vendored font package |\n",
    )

    assert check_version_sync(repository) == "1.2.3"


def test_internal_api_contract_is_not_a_release_claim(repository: Path) -> None:
    replace(repository, "PUBLIC_API.md", "Version `1.2.3`", "Version `9.9.9`")

    assert check_version_sync(repository) == "1.2.3"


def test_milestone_mentions_do_not_hide_an_undeclared_current_claim(
    repository: Path,
) -> None:
    set_fixture_version(repository, "1.0.0")
    replace(
        repository,
        "PUBLIC_API.md",
        "After `1.0.0`, an incompatible change requires a major release.",
        "After `2.0.0`, an incompatible change requires a major release.",
    )
    write(
        repository,
        "VERSIONING.md",
        "`1.0.0` identifies the current release candidate."
        " <!-- x-release-please-version -->\n"
        "- After `1.0.0`, a breaking change to a public import, model, method signature,\n"
        "release, but the release notes must call out the break explicitly."
        " After `1.0.0`, a\n",
    )

    assert check_version_sync(repository) == "1.0.0"

    append(repository, "VERSIONING.md", "Version `1.0.0` is the current supported release.")
    with pytest.raises(VersionSyncError, match=r"VERSIONING\.md:5"):
        check_version_sync(repository)


@pytest.mark.parametrize("relative", ("VERSIONING.md",))
def test_a_shipped_policy_left_behind_by_a_bump_is_reported(
    repository: Path, relative: str
) -> None:
    """The failure Codex reproduced: every automated source advances, a policy does not."""

    for path, old, new in (
        ("pyproject.toml", 'version = "1.2.3"', 'version = "1.2.4"'),
        ("docs/package.json", "1.2.3", "1.2.4"),
        ("scripts/release-policy.json", "1.2.3", "1.2.4"),
        ("src/picogrid_ecn_client/__init__.py", "1.2.3", "1.2.4"),
        ("README.md", "1.2.3", "1.2.4"),
        ("docs/getting-started/installation.md", "1.2.3", "1.2.4"),
        ("VERIFYING_ARTIFACTS.md", "1.2.3", "1.2.4"),
        ("operator-app/pyproject.toml", "1.2.3", "1.2.4"),
        ("VERSIONING.md", "1.2.3", "1.2.4"),
    ):
        if path != relative:
            replace(repository, path, old, new)
    for old in ('"version": "1.2.3"', '"name": "docs",\n      "version": "1.2.3"'):
        replace(repository, "docs/package-lock.json", old, old.replace("1.2.3", "1.2.4"))
    replace(
        repository,
        "uv.lock",
        'name = "picogrid-ecn-client"\nversion = "1.2.3"',
        'name = "picogrid-ecn-client"\nversion = "1.2.4"',
    )

    with pytest.raises(VersionSyncError) as raised:
        check_version_sync(repository)
    assert relative in str(raised.value)


@pytest.mark.parametrize("relative", ("VERSIONING.md",))
def test_a_policy_claim_without_the_annotation_is_reported(repository: Path, relative: str) -> None:
    replace(repository, relative, " <!-- x-release-please-version -->", "")

    with pytest.raises(VersionSyncError) as raised:
        check_version_sync(repository)
    assert relative in str(raised.value)


@pytest.mark.parametrize(
    "jsonpath",
    ("top-level", "root package"),
)
def test_package_lock_version_disagreement_is_reported(repository: Path, jsonpath: str) -> None:
    old = (
        '"version": "1.2.3"'
        if jsonpath == "top-level"
        else '"name": "docs",\n      "version": "1.2.3"'
    )
    new = old.replace("1.2.3", "1.2.4")
    replace(repository, "docs/package-lock.json", old, new)

    with pytest.raises(VersionSyncError) as raised:
        check_version_sync(repository)

    assert "docs/package-lock.json" in str(raised.value)
    assert "1.2.4" in str(raised.value)


def test_an_extra_artifact_name_on_an_unannotated_line_is_reported(repository: Path) -> None:
    append(repository, "README.md", "See picogrid_ecn_client-1.2.3.tar.gz for the sdist.")

    with pytest.raises(VersionSyncError, match="annotation"):
        check_version_sync(repository)


def test_the_operator_application_may_carry_its_own_version(repository: Path) -> None:
    """Its pin tracks this release; its own version is its own business."""

    replace(repository, "operator-app/pyproject.toml", 'version = "9.9.9"', 'version = "1.2.3"')

    assert check_version_sync(repository) == "1.2.3"


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("pyproject.toml", 'version = "1.2.3"', ""),
        ("src/picogrid_ecn_client/__init__.py", '__version__ = "1.2.3"', "x = 1"),
        ("uv.lock", 'name = "picogrid-ecn-client"', 'name = "other"'),
        ("README.md", "picogrid_ecn_client-1.2.3-py3-none-any.whl", "the wheel"),
    ],
)
def test_a_missing_statement_is_reported_rather_than_ignored(
    repository: Path, relative: str, old: str, new: str
) -> None:
    replace(repository, relative, old, new)

    with pytest.raises(VersionSyncError):
        check_version_sync(repository)


def test_dashed_rc_version_spelling_is_normalized_to_canonical_form(repository: Path) -> None:
    """PEP 440 pre-release versions accept dashed spelling in policy files.

    release-please writes 0.1.0-rc1; canonical form is 0.1.0rc1.
    Policy file has dashed form; artifacts and comparisons normalize to canonical.
    """
    set_fixture_version(repository, "1.2.3-rc1")

    assert check_version_sync(repository) == "1.2.3-rc1"


def test_mismatched_rc_numbers_still_fail(repository: Path) -> None:
    """Different RC numbers represent different pre-releases and must not agree."""
    set_fixture_version(repository, "1.2.3-rc1")
    replace(repository, "docs/package.json", '"1.2.3-rc1"', '"1.2.3-rc2"')

    with pytest.raises(VersionSyncError, match=re.escape("1.2.3-rc2")):
        check_version_sync(repository)


def test_an_undeclared_alternate_spelling_in_prose_is_reported(repository: Path) -> None:
    """A canonical-spelling copy must not hide from the scan when files are dashed."""
    set_fixture_version(repository, "1.2.3-rc1")
    append(repository, "README.md", "Release 1.2.3rc1 is the current line.")

    with pytest.raises(VersionSyncError, match="does not know about"):
        check_version_sync(repository)


def test_mixed_declared_spellings_agree(repository: Path) -> None:
    """Declared statements may mix the dashed and canonical spellings."""
    set_fixture_version(repository, "1.2.3-rc1")
    replace(repository, "docs/package.json", '"1.2.3-rc1"', '"1.2.3rc1"')

    assert check_version_sync(repository) == "1.2.3-rc1"


def test_malformed_version_is_reported() -> None:
    """A version string packaging cannot parse fails the gate, not the parser."""
    with pytest.raises(VersionSyncError, match="invalid version string"):
        _normalize_version("not-a-version")


def test_final_version_behavior_unchanged_with_normalization(repository: Path) -> None:
    """Final versions without pre-release suffixes work as before."""
    set_fixture_version(repository, "1.2.3")

    assert check_version_sync(repository) == "1.2.3"
