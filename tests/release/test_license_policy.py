# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts import license_policy
from scripts.license_policy import PolicyReport, evaluate_policy

LICENSE_EXPRESSION = "MPL-2.0"
COPYRIGHT_NOTICE = "Copyright (c) Picogrid, Inc."
SPDX_MARKER = "SPDX-License-" + "Identifier:"


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _base_repository(tmp_path: Path) -> tuple[Path, Path, list[str], dict[str, Any]]:
    repository = tmp_path / "repository"
    license_bytes = b"synthetic canonical license\n"
    license_digest = hashlib.sha256(license_bytes).hexdigest()
    files: dict[str, str | bytes] = {
        "LICENSE": license_bytes,
        "README.md": "Synthetic documentation.\n",
        "app.py": f"# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n\nVALUE = 1\n",
        "operator-app/package-lock.json": '{"lockfileVersion": 3, "license": "metadata"}\n',
        "operator-app/package.json": '{"license": "MPL-2.0"}\n',
        "operator-app/pyproject.toml": '[project]\nlicense = "MPL-2.0"\n',
        "docs/package-lock.json": '{"lockfileVersion": 3, "license": "metadata"}\n',
        "docs/package.json": '{"license": "MPL-2.0"}\n',
        "pyproject.toml": ('[project]\nlicense = "MPL-2.0"\nlicense-files = ["LICENSE"]\n'),
        "scripts/release-policy.json": json.dumps(
            {"license_expression": "MPL-2.0", "license_text_sha256": license_digest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    }
    for relative, content in files.items():
        _write(repository / relative, content)

    policy: dict[str, Any] = {
        "category_rules": [
            {
                "extensions": [".md", ".toml", ".txt"],
                "name": "documentation-and-config",
                "paths": [],
                "reason": "Text formats without a uniform notice syntax use the root license.",
            },
            {
                "extensions": [".json", ".lock"],
                "name": "structured-data",
                "paths": [],
                "reason": "Structured metadata is covered by the root license.",
            },
        ],
        "exception_categories": [
            "brand-asset",
            "generated",
            "license-text",
            "third-party-notice",
        ],
        "exceptions": [
            {
                "category": "license-text",
                "path": "LICENSE",
                "reason": "The unmodified license text cannot carry an added file notice.",
            }
        ],
        "license_expression": LICENSE_EXPRESSION,
        "license_text_sha256": hashlib.sha256(license_bytes).hexdigest(),
        "notice_copyright": COPYRIGHT_NOTICE,
        "notice_required_extensions": [".css", ".mjs", ".proto", ".py", ".ts"],
        "notice_required_paths": [],
        "third_party_notices": [],
        "third_party_scan_exclusions": [
            {
                "path": "LICENSE",
                "reason": "The authoritative license text is validated by its pinned digest.",
            },
            {
                "path": "docs/package-lock.json",
                "reason": "Dependency license metadata is not a source notice.",
            },
            {
                "path": "operator-app/package-lock.json",
                "reason": "Dependency license metadata is not a source notice.",
            },
            {
                "path": "scripts/license-policy.json",
                "reason": "The registry contains notice strings as policy data.",
            },
        ],
    }
    policy_path = repository / "scripts" / "license-policy.json"
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    tracked = sorted([*files, "scripts/license-policy.json"])
    return repository, policy_path, tracked, policy


def _evaluate(repository: Path, policy_path: Path, tracked: list[str]) -> PolicyReport:
    return evaluate_policy(repository, policy_path, tracked_files=tracked)


def _messages(report: PolicyReport) -> str:
    return "\n".join(f"{finding.path}: {finding.message}" for finding in report.findings)


def test_clean_tree_passes_with_deterministic_coverage_counts(tmp_path: Path) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == ()
    assert report.files_scanned == 11
    assert report.inline_notices == 1
    assert report.exceptions == 1
    assert report.category_rule_files == 9


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("VALUE = 1\n", "missing a valid inline notice"),
        (
            f"# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} replaced\n",
            "unregistered third-party notice",
        ),
    ],
)
def test_required_notice_failures_name_the_file(
    tmp_path: Path, content: str, expected: str
) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / "app.py", content)

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages
    assert expected in messages


def test_stale_exception_entry_fails(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["exceptions"].append(
        {
            "category": "generated",
            "path": "missing.py",
            "reason": "Synthetic stale entry.",
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "missing.py" in messages
    assert "stale exception" in messages


def test_redundant_exception_entry_fails(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["exceptions"].append(
        {
            "category": "generated",
            "path": "app.py",
            "reason": "Synthetic redundant entry.",
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages
    assert "redundant exception" in messages


def test_category_rule_matching_nothing_fails(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["category_rules"].append(
        {
            "extensions": [".unused"],
            "name": "unused-rule",
            "paths": [],
            "reason": "Synthetic stale category rule.",
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "unused-rule" in messages
    assert "matches no tracked files" in messages


def test_license_digest_drift_reports_expected_and_actual_digest(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    _write(repository / "LICENSE", b"replaced\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "LICENSE" in messages
    assert policy["license_text_sha256"] in messages
    assert hashlib.sha256(b"replaced\n").hexdigest() in messages


@pytest.mark.parametrize(
    ("relative", "old", "new", "field"),
    [
        ("pyproject.toml", 'license = "MPL-2.0"', 'license = "replaced"', "project.license"),
        (
            "operator-app/pyproject.toml",
            'license = "MPL-2.0"',
            'license = "replaced"',
            "project.license",
        ),
        ("docs/package.json", '"license": "MPL-2.0"', '"license": "replaced"', "license"),
        (
            "operator-app/package.json",
            '"license": "MPL-2.0"',
            '"license": "replaced"',
            "license",
        ),
        (
            "scripts/release-policy.json",
            '"license_expression": "MPL-2.0"',
            '"license_expression": "replaced"',
            "license_expression",
        ),
        (
            "scripts/release-policy.json",
            '"license_text_sha256"',
            '"license_text_sha256_disabled"',
            "license_text_sha256",
        ),
        (
            "pyproject.toml",
            'license-files = ["LICENSE"]',
            "license-files = []",
            "project.license-files",
        ),
    ],
)
def test_metadata_mismatch_names_file_and_field(
    tmp_path: Path, relative: str, old: str, new: str, field: str
) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    path = repository / relative
    _write(path, path.read_text(encoding="utf-8").replace(old, new))

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert field in messages


def test_unregistered_foreign_copyright_fails(tmp_path: Path) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "third-party.txt"
    _write(repository / relative, "Copy" + "right (c) Other Author\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_registered_third_party_notice_passes(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    relative = "third-party.txt"
    payload = (SPDX_MARKER + " OTHER\nCopy" + "right (c) Other Author\n").encode()
    _write(repository / relative, payload)
    tracked.append(relative)
    tracked.sort()
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    assert _evaluate(repository, policy_path, tracked).findings == ()


def test_registered_third_party_notice_with_changed_digest_fails(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    relative = "third-party.txt"
    reviewed = (SPDX_MARKER + " OTHER\nCopy" + "right (c) Other Author\n").encode()
    _write(repository / relative, reviewed + b"replaced\n")
    tracked.append(relative)
    tracked.sort()
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(reviewed).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "third-party notice digest changed" in messages


def test_control_file_rule_does_not_exempt_a_lookalike_source_file(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["category_rules"].append(
        {
            "extensions": [],
            "name": "repository-control-files",
            "paths": ["Makefile"],
            "reason": "Control files are covered by the root license.",
        }
    )
    policy["category_rules"].sort(key=lambda rule: str(rule["name"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    _write(repository / "Makefile", "all:\n\t@true\n")
    _write(repository / "Makefile.py", "VALUE = 1\n")
    tracked.extend(["Makefile", "Makefile.py"])
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "Makefile.py: " in messages
    assert "missing a valid inline notice" in messages
    assert "Makefile: " not in messages


def test_directory_rule_covers_only_paths_beneath_it(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["category_rules"].append(
        {
            "extensions": [],
            "name": "vendor-tree",
            "paths": ["vendor/"],
            "reason": "The reviewed vendor tree is covered by the root license.",
        }
    )
    policy["category_rules"].sort(key=lambda rule: str(rule["name"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    _write(repository / "vendor" / "note", "reviewed\n")
    _write(repository / "vendored.py", "VALUE = 1\n")
    tracked.extend(["vendor/note", "vendored.py"])
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "vendored.py: " in messages
    assert "vendor/note" not in messages


def test_xml_comment_terminator_is_stripped_from_a_registered_identifier(
    tmp_path: Path,
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    reviewed = (
        "<!-- " + SPDX_MARKER + " OTHER -->\n<!-- Copy" + "right (c) Other Author -->\n"
    ).encode()
    _write(repository / relative, reviewed)
    tracked.append(relative)
    tracked.sort()
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(reviewed).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_copyright_symbol_notice_requires_registration(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, "\u00a9 2026 Other Author\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    ("copy_bytes", "expected"),
    [
        (b"synthetic canonical license\n", ""),
        (b"synthetic canonical license\nwith a rider\n", "license digest drifted"),
    ],
)
def test_every_license_text_copy_is_pinned_to_the_authoritative_digest(
    tmp_path: Path, copy_bytes: bytes, expected: str
) -> None:
    relative = "operator-app/LICENSE"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    _write(repository / relative, copy_bytes)
    tracked.append(relative)
    tracked.sort()
    policy["exceptions"].append(
        {
            "category": "license-text",
            "path": relative,
            "reason": "The standalone operator build context needs its own copy of the terms.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    if expected:
        assert relative in messages
        assert expected in messages
    else:
        assert messages == ""


@pytest.mark.parametrize(
    ("break_notice", "expected_exit", "expected_ok"),
    [(False, 0, True), (True, 1, False)],
)
def test_cli_exit_status_and_json_payload_report_the_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    break_notice: bool,
    expected_exit: int,
    expected_ok: bool,
) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    if break_notice:
        _write(repository / "app.py", "VALUE = 1\n")
    monkeypatch.setattr(license_policy, "REPOSITORY", repository)
    monkeypatch.setattr(license_policy, "POLICY_PATH", policy_path)
    monkeypatch.setattr(license_policy, "_tracked_files", lambda _repository: tuple(tracked))
    monkeypatch.setattr("sys.argv", ["license_policy", "--json"])

    exit_code = license_policy.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == expected_exit
    assert payload["ok"] is expected_ok
    assert set(payload) == {"counts", "findings", "ok"}
    assert set(payload["counts"]) == {
        "category_rule_files",
        "exceptions",
        "files_scanned",
        "inline_notices",
    }
    if break_notice:
        assert [finding["path"] for finding in payload["findings"]] == ["app.py"]
    else:
        assert payload["findings"] == []


def test_cli_reports_a_malformed_policy_as_a_json_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    policy["license_text_sha256"] = "not-a-digest"
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    monkeypatch.setattr(license_policy, "REPOSITORY", repository)
    monkeypatch.setattr(license_policy, "POLICY_PATH", policy_path)
    monkeypatch.setattr(license_policy, "_tracked_files", lambda _repository: tuple(tracked))
    monkeypatch.setattr("sys.argv", ["license_policy", "--json"])

    exit_code = license_policy.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["findings"][0]["check"] == "policy"


def test_inline_notice_requires_the_exact_license_expression(tmp_path: Path) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / "app.py",
        f"# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}-or-later\n",
    )

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (f"# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n\nVALUE = 1\n", True),
        (
            f"#!/usr/bin/env python3\n# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n",
            True,
        ),
        (
            f'NOTICE = """\n# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n"""\n',
            False,
        ),
        (
            f"VALUE = 1\n# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n",
            False,
        ),
    ],
)
def test_notice_is_only_accepted_from_the_leading_comment_block(
    tmp_path: Path, body: str, accepted: bool
) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / "app.py", body)

    messages = _messages(_evaluate(repository, policy_path, tracked))

    if accepted:
        assert messages == ""
    else:
        assert "app.py" in messages
        assert "missing a valid inline notice" in messages


@pytest.mark.parametrize(
    ("line", "is_notice"),
    [
        ("Copy" + "right (c) Other Author", True),
        ("Copy" + "right 2026 Other Author", True),
        ("\u00a9 2026 Other Author", True),
        ("COPY" + "RIGHT (C) Other Author", True),
        ("copy" + "right is granted under section 2.1.", False),
        ("Copy" + "right is granted under section 2.1.", False),
        ("Their copy" + "right and patent grants are described below.", False),
        # A legal indicator is required, so title-cased prose is not a notice.
        ("Copy" + "right Notices Are Documented Below", False),
        ("Copy" + "right Policy", False),
        ("Copy" + "right notices are documented below", False),
        ("\u00a9 Other Author", True),
        # A symbol alone is notation, not a notice: holder or year must follow.
        ("Copy" + "right (c) syntax", False),
        ("Copy" + "right \u00a9 symbol", False),
        ("Copy" + "right (c) 2026 Other Author", True),
    ],
)
def test_only_a_conventional_notice_shape_requires_registration(
    tmp_path: Path, line: str, is_notice: bool
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, f"{line}\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert (relative in messages) is is_notice


def test_third_party_exception_without_a_registration_is_rejected(tmp_path: Path) -> None:
    relative = "vendor/notice-less.py"
    _repository, policy_path, _tracked, policy = _base_repository(tmp_path)
    policy["exceptions"].append(
        {
            "category": "third-party-notice",
            "path": relative,
            "reason": "Upstream file carries no recognizable header.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    with pytest.raises(license_policy.LicensePolicyError, match="third_party_notices"):
        license_policy.load_policy(policy_path)


def test_third_party_exception_with_a_registration_is_accepted(tmp_path: Path) -> None:
    relative = "vendor/notice-less.py"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    reviewed = b"UPSTREAM = 1\n"
    _write(repository / relative, reviewed)
    tracked.append(relative)
    tracked.sort()
    policy["exceptions"].append(
        {
            "category": "third-party-notice",
            "path": relative,
            "reason": "Upstream file carries no recognizable header.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(reviewed).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_combined_notice_naming_a_co_holder_requires_registration(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, f"{COPYRIGHT_NOTICE} and Other Author\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_picogrid_only_notice_does_not_require_registration(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, f"{COPYRIGHT_NOTICE} All rights reserved.\n")
    tracked.append(relative)
    tracked.sort()

    assert _evaluate(repository, policy_path, tracked).findings == ()


def test_registered_license_is_compared_against_an_mpl_upstream_identifier(
    tmp_path: Path,
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    reviewed = f"Copy{'right'} (c) Other Author\n{SPDX_MARKER} {LICENSE_EXPRESSION}\n".encode()
    _write(repository / relative, reviewed)
    tracked.append(relative)
    tracked.sort()
    policy["third_party_notices"].append(
        {
            "license": "MIT",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(reviewed).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "does not match declared identifiers" in messages


def test_a_category_rule_cannot_satisfy_a_notice_required_path(tmp_path: Path) -> None:
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    relative = "tool.md"
    policy["notice_required_paths"] = [relative]
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    _write(repository / relative, "Synthetic tool documentation.\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "notice-required file is missing a valid inline notice" in messages


@pytest.mark.parametrize(
    "suffix",
    ["#", " /", "*", " ;"],
)
def test_malformed_identifier_suffixes_do_not_normalize_to_the_policy_license(
    tmp_path: Path, suffix: str
) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / "app.py",
        f"# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}{suffix}\n",
    )

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages


@pytest.mark.parametrize(
    ("terminator", "accepted"),
    [("-->", True), ("--!>", True), ("*/", True)],
)
def test_complete_comment_terminators_are_removed(
    tmp_path: Path, terminator: str, accepted: bool
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    reviewed = (
        f"<!-- Copy{'right'} (c) Other Author {terminator}\n<!-- {SPDX_MARKER} OTHER {terminator}\n"
    ).encode()
    _write(repository / relative, reviewed)
    tracked.append(relative)
    tracked.sort()
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256(reviewed).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    report = _evaluate(repository, policy_path, tracked)

    assert (report.findings == ()) is accepted, _messages(report)


@pytest.mark.parametrize(
    "holder",
    [
        "2026 Other Author",
        "(C) 2026 Other Author",
        "(c) 2026 Other Author",
        "\u00a9 2026 Other Author",
        "\u00a9 Other Author",
    ],
)
def test_reuse_file_copyright_text_requires_registration(tmp_path: Path, holder: str) -> None:
    """The REUSE specification permits `(C)`, `(c)`, and the symbol after the tag."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        f"SPDX-File{'CopyrightText'}: {holder}\n{SPDX_MARKER} {LICENSE_EXPRESSION}\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "wrapper",
    [
        "> {notice}",
        "- {notice}",
        "| {notice} |",
        "<p>{notice}</p>",
        "  {notice}",
    ],
)
def test_markup_wrapped_notices_still_require_registration(tmp_path: Path, wrapper: str) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, wrapper.format(notice=notice) + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_negated_copyright_header_is_not_an_affirmative_notice(tmp_path: Path) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / "app.py",
        f"# Previous header: {COPYRIGHT_NOTICE} removed\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n",
    )

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages
    assert "missing a valid inline notice" in messages


def test_undecodable_category_file_fails_closed(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        ("Copy" + "right (c) 2026 Other Author\n").encode("latin-1") + b"\xff\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "is not UTF-8 text and cannot be scanned" in messages


@pytest.mark.parametrize(
    "wrapper",
    ["<!-- {notice} -->", "<!-- {notice}", "<!--{notice}--!>", "<li>{notice}</li>"],
)
def test_html_comment_notices_still_require_registration(tmp_path: Path, wrapper: str) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, wrapper.format(notice=notice) + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "wrapper",
    ["<p>{notice}</p>", "<!-- {notice} -->", "> {notice}", "| {notice} |"],
)
def test_wrapped_first_party_notice_is_not_reported_as_third_party(
    tmp_path: Path, wrapper: str
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, wrapper.format(notice=COPYRIGHT_NOTICE) + "\n")
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


@pytest.mark.parametrize("opener", ["/*", "/*!"])
def test_preserved_license_css_banner_is_a_valid_notice(tmp_path: Path, opener: str) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "theme.css"
    _write(
        repository / relative,
        f"{opener} {COPYRIGHT_NOTICE} */\n{opener} {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_undecodable_category_file_is_satisfied_by_a_documented_exception(
    tmp_path: Path,
) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        ("Copy" + "right (c) 2026 Other Author\n").encode("latin-1") + b"\xff\n",
    )
    tracked.append(relative)
    tracked.sort()
    policy["exceptions"].append(
        {
            "category": "third-party-notice",
            "path": relative,
            "reason": "Reviewed upstream document that is not UTF-8 text.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    policy["third_party_notices"].append(
        {
            "license": "OTHER",
            "origin": "Synthetic upstream fixture",
            "path": relative,
            "sha256": hashlib.sha256((repository / relative).read_bytes()).hexdigest(),
        }
    )
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_a_generated_exception_does_not_exempt_an_undecodable_file(tmp_path: Path) -> None:
    """Only `third-party-notice` exceptions carry a registration, so only they exempt."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        ("Copy" + "right (c) 2026 Other Author\n").encode("latin-1") + b"\xff\n",
    )
    tracked.append(relative)
    tracked.sort()
    policy["exceptions"].append(
        {
            "category": "generated",
            "path": relative,
            "reason": "Miscategorized exception that asserts nothing about provenance.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "is not UTF-8 text and cannot be scanned" in messages


@pytest.mark.parametrize(
    "line",
    [
        "Copy{}right (c) <b>2026</b> Other Author",
        "<p>Copy{}right (c) <em>2026</em> Other Author</p>",
        "Copy{}right (c) 2026 <strong>Other Author</strong>",
    ],
)
def test_interior_markup_does_not_hide_a_notice(tmp_path: Path, line: str) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, line.format("") + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "wrapped",
    [
        "<p>Copy{}right (c) <b>Picogrid, Inc.</b></p>",
        "Copy{}right (c) <b>Picogrid, Inc.</b>",
        "<b title='>'>Copy{}right (c) Picogrid, Inc.</b>",
    ],
)
def test_interior_markup_around_the_first_party_notice_is_not_third_party(
    tmp_path: Path, wrapped: str
) -> None:
    """Asserted end to end, not just through the normalizer.

    The ownership comparison has to run on the normalized line too; checking the raw
    line treats Picogrid's own notice as foreign as soon as markup sits inside it,
    which blocks the gate on first-party documentation.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, wrapped.format("") + "\n")
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)
    assert license_policy._strip_wrappers(wrapped.format("")) == COPYRIGHT_NOTICE


@pytest.mark.parametrize(
    "wrapped",
    [
        "| {notice} |",
        "> {notice}",
        "- {notice}",
        "<!-- {notice} -->",
        "<!-- {notice} --!>",
        "<p>{notice}</p>",
        "/* {notice} */",
        "/*! {notice} */",
        "// {notice}",
        "# {notice}",
        "1. {notice}",
        "10) {notice}",
    ],
)
def test_strip_wrappers_normalizes_every_supported_wrapper(wrapped: str) -> None:
    """Openers and closers must be symmetric, or exact notice matching breaks.

    Asserted on the normalizer directly: a table-wrapped notice cannot reach inline
    notice detection, because `|` is not a comment prefix and so never enters a
    file's leading comment block.
    """

    assert license_policy._strip_wrappers(wrapped.format(notice=COPYRIGHT_NOTICE)) == (
        COPYRIGHT_NOTICE
    )


def test_a_bare_year_is_not_mistaken_for_an_ordered_list_marker() -> None:
    assert license_policy._strip_wrappers("2026 Other Author") == "2026 Other Author"


@pytest.mark.parametrize("marker", ["+", "-"])
def test_fenced_diff_lines_are_not_normalized_into_notices(tmp_path: Path, marker: str) -> None:
    """`+`/`-` are diff syntax as often as list syntax, so they need a space.

    Documentation quotes diffs. Stripping a bare marker would turn a quoted line
    into an apparent file-level notice and block the gate on first-party content.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"```diff\n{marker}{notice}\n```\n")
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)
    assert license_policy._strip_wrappers(f"{marker}{notice}") == f"{marker}{notice}"


@pytest.mark.parametrize(
    "emphasis",
    ["**{}**", "*{}*", "__{}__", "`{}`", "[{}](https://example.invalid)"],
)
def test_emphasis_wrapped_header_notices_require_registration(
    tmp_path: Path, emphasis: str
) -> None:
    """Inline Markdown wraps holders the same way tags do."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    holder = emphasis.format("2026 Other Author")
    _write(repository / relative, f"Copy{'right'} (c) {holder}\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "example",
    [
        "<code>Copy{}right (c) 2026 Other Author</code>",
        "`Copy{}right (c) 2026 Other Author`",
        "Copy{}right (c) 2026 Other Author",
    ],
)
def test_a_notice_quoted_in_prose_is_not_a_provenance_event(tmp_path: Path, example: str) -> None:
    """Only the leading notice region can carry a file-level notice.

    Prose that quotes or explains a notice is not provenance, and no amount of
    markup normalization can tell the two apart — the reviewers asked for opposite
    treatments of the same syntax. The boundary is the file position, not the markup.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        "Licensing overview.\n\nA notice looks like " + example.format("") + ".\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_a_tagged_third_party_file_is_caught_wherever_it_declares_itself(
    tmp_path: Path,
) -> None:
    """The SPDX identifier is machine syntax, so it is matched over the whole file."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        f"Overview prose.\n\nSome body text.\n\n{SPDX_MARKER} OTHER\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "header",
    [
        "---\ntitle: Upstream\n---\nCopy{}right (c) 2026 Other Author",
        "+++\ntitle = 'Upstream'\n+++\nCopy{}right (c) 2026 Other Author",
        "<div>\nCopy{}right (c) 2026 Other Author\n</div>",
        "<div>\n<p>\nCopy{}right (c) 2026 Other Author\n</p>\n</div>",
    ],
)
def test_the_region_traverses_front_matter_and_markup_only_lines(
    tmp_path: Path, header: str
) -> None:
    """Neither front matter nor a standalone tag may end the region early.

    Both open a document *before* its header notice, so stopping at them would hide
    exactly the notice the region exists to find.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, header.format("") + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_unterminated_front_matter_does_not_swallow_the_document(tmp_path: Path) -> None:
    """A lone `---` is a horizontal rule, not front matter, so the boundary holds."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        "---\n\nLicensing overview prose.\n\nA notice looks like "
        + f"Copy{''}right (c) 2026 Other Author.\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


@pytest.mark.parametrize("declaration", ["<!DOCTYPE html>", "<!doctype html>"])
def test_document_declarations_do_not_end_the_region(tmp_path: Path, declaration: str) -> None:
    """A declaration opens a document; it is never a notice nor an example of one."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"{declaration}\n<!-- {notice} -->\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "example",
    ["`Copy{}right (c) 2026 Other Author`", "<code>Copy{}right (c) 2026 Other Author</code>"],
)
def test_a_code_marked_line_at_document_start_is_treated_as_a_notice(
    tmp_path: Path, example: str
) -> None:
    """Deliberate fail-closed behavior, pinned so it stays a decision.

    At the top of a document, code markers cannot distinguish a notice from an
    example of one: the same review asked for a `<!DOCTYPE>` declaration to be
    traversed and a backtick example at that position to be preserved. The gate
    reports it, which names the file and is cleared by registering or excluding it.
    Below the region the opposite holds, covered by
    `test_a_notice_quoted_in_prose_is_not_a_provenance_event`.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, example.format("") + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "opening",
    [
        "<?xml version='1.0'?>",
        "<?xml-stylesheet href='a.css'?>",
        "<!DOCTYPE html><!-- {notice} -->",
    ],
)
def test_declarations_and_processing_instructions_do_not_hide_a_notice(
    tmp_path: Path, opening: str
) -> None:
    """Including the minified single-line form, which needs repeated normalization."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    body = (
        opening.format(notice=notice) if "{notice}" in opening else f"{opening}\n<!-- {notice} -->"
    )
    _write(repository / relative, body + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "header",
    [
        "# <del>{notice}</del>",
        "# `{notice}`",
        "# <q>{notice}</q>",
    ],
)
def test_markup_cannot_manufacture_an_affirmative_notice(tmp_path: Path, header: str) -> None:
    """Asserting the notice and quoting it are different claims.

    The detection path normalizes markup on purpose, to find someone else's notice.
    The affirmative path must not, or `<del>`-marked text would satisfy a
    requirement the file explicitly denies.
    """

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / "app.py",
        f"{header.format(notice=COPYRIGHT_NOTICE)}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n",
    )

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert "app.py" in messages
    assert "missing a valid inline notice" in messages


def test_an_undecodable_notice_required_file_is_scanned(tmp_path: Path) -> None:
    """Notice-required formats are text too, so the fail-closed scan must cover them."""

    relative = "theme.css"
    repository, policy_path, tracked, policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        ("/* Copy" + "right (c) 2026 Other Author */\n").encode("latin-1") + b"\xff\n",
    )
    tracked.append(relative)
    tracked.sort()
    policy["exceptions"].append(
        {
            "category": "generated",
            "path": relative,
            "reason": "Generated stylesheet with a non-provenance exception.",
        }
    )
    policy["exceptions"].sort(key=lambda item: str(item["path"]))
    _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "is not UTF-8 text and cannot be scanned" in messages


@pytest.mark.parametrize(
    "header",
    [
        "<div\n  class='wrapper'>\n<!-- {notice} -->",
        "<div\n  class='a'\n  id='b'\n>\n<p>\n{notice}\n</p>",
        "<!--\n{notice}\n-->",
        "<?xml\n  version='1.0'\n?>\n<!-- {notice} -->",
        "/*\n * {notice}\n */",
    ],
)
def test_multiline_markup_cannot_hide_a_header_notice(tmp_path: Path, header: str) -> None:
    """A construct split across lines must not end the region on its own fragment."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, header.format(notice=notice) + "\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_an_unterminated_construct_does_not_swallow_the_document(tmp_path: Path) -> None:
    """The join is bounded, so an opener that never closes cannot consume the file."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    body = "<div\n" + "prose line\n" * 40 + f"A notice looks like Copy{''}right (c) 2026 X.\n"
    _write(repository / relative, body)
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


@pytest.mark.parametrize("expression", ["if a < b and c > d:", "x<y", "a<b and b>c"])
def test_comparison_operators_are_not_treated_as_markup(expression: str) -> None:
    """Spaced or compact, a comparison opens nothing, so joining must not run away."""

    assert not license_policy._has_unterminated_markup(expression)
    assert license_policy._logical_lines(f"{expression}\nsecond line\n") == [
        [expression],
        ["second line"],
    ]


def test_a_notice_after_comment_prose_is_still_found(tmp_path: Path) -> None:
    """Grouping decides the boundary; the physical lines are what get scanned.

    Flattening a multiline comment would put preceding prose ahead of the notice on
    one logical line, and the anchored pattern would never see it.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"/*\n * Upstream library.\n * {notice}\n */\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_byte_order_mark_does_not_end_the_region(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"\ufeff---\ntitle: Upstream\n---\n{notice}\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize("opener", ["/*", "/*!", "/**"])
def test_block_comment_openers_are_valid_notices(tmp_path: Path, opener: str) -> None:
    """Including the JSDoc `/**` form, which the exact comparison must accept."""

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "theme.css"
    _write(
        repository / relative,
        f"{opener} {COPYRIGHT_NOTICE} */\n{opener} {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_head_metadata_does_not_end_the_region(tmp_path: Path) -> None:
    """Document scaffolding precedes the notice; only prose ends the region."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        "<html>\n<head>\n<title>Upstream</title>\n"
        f'<meta name="generator" content="x">\n<!-- {notice} -->\n</head>\n',
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize("filler", [4, 30])
def test_a_long_but_complete_construct_is_followed_to_its_terminator(
    tmp_path: Path, filler: int
) -> None:
    """A cap that truncates a valid group hides the notice inside it."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    attributes = "\n".join(f'  data-x{n}="{n}"' for n in range(filler))
    _write(repository / relative, f"<div\n{attributes}\n>\n<!-- {notice} -->\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_bom_prefixed_first_party_header_is_still_a_valid_notice(tmp_path: Path) -> None:
    """A byte-order mark is an encoding artifact, not a missing notice."""

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / "app.py",
        f"\ufeff# {COPYRIGHT_NOTICE}\n# {SPDX_MARKER} {LICENSE_EXPRESSION}\n\nVALUE = 1\n",
    )

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


@pytest.mark.parametrize(
    "body",
    [
        "<p>Introduction</p>\n<p>Copy{}right (c) 2026 Other Author</p>",
        "<h1>Overview</h1>\n<pre>Copy{}right (c) 2026 Other Author</pre>",
        "**Introduction**\n\n`Copy{}right (c) 2026 Other Author`",
    ],
)
def test_body_content_ends_the_region_before_examples(tmp_path: Path, body: str) -> None:
    """Scaffolding is a closed set; formatted body content is not part of it.

    Accepting any markup-bearing line as scaffolding ran the region through the
    document body and reported quoted examples as file-level notices.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(repository / relative, body.format("") + "\n")
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


@pytest.mark.parametrize("attribute", ["copyright", "dcterms.rights"])
def test_a_notice_declared_in_head_metadata_requires_registration(
    tmp_path: Path, attribute: str
) -> None:
    """The metadata can be the notice, so its value is inspected before tags drop."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f'<head>\n<meta name="{attribute}" content="{notice}">\n</head>\n',
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_first_party_metadata_is_not_reported_as_third_party(tmp_path: Path) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        f'<head>\n<meta name="copyright" content="{COPYRIGHT_NOTICE}">\n</head>\n',
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_a_multiline_metadata_notice_requires_registration(tmp_path: Path) -> None:
    """An attribute value split across lines only exists in the joined form."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f'<head>\n<meta name="copyright"\n      content="{notice}">\n</head>\n',
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    ("directive", "relative"),
    [('@charset "UTF-8";', "theme.css"), ("'use strict';", "bundle.ts")],
)
def test_a_required_first_directive_does_not_hide_the_notice(
    tmp_path: Path, directive: str, relative: str
) -> None:
    """A stylesheet's `@charset` must come first, ahead of its license banner.

    Each directive is scoped to the formats that require it, so the pair is checked
    in its own format rather than interchangeably.
    """

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    _write(
        repository / relative,
        f"{directive}\n/* {COPYRIGHT_NOTICE} */\n/* {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_a_construct_longer_than_any_cap_is_still_followed(tmp_path: Path) -> None:
    """Traversal follows a complete construct to its terminator, without a cap."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    attributes = "\n".join(f'  data-x{n}="{n}"' for n in range(250))
    _write(repository / relative, f"<div\n{attributes}\n>\n<!-- {notice} -->\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize("marker", ["1.", "10)"])
def test_ordered_list_notices_still_require_registration(tmp_path: Path, marker: str) -> None:
    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"{marker} {notice}\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_quoted_delimiter_does_not_close_a_multiline_tag(tmp_path: Path) -> None:
    """A `>` inside a quoted attribute is content, so traversal must read past it."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"<div\n  title=\">\"\n  id='a'>\n<!-- {notice} -->\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_an_incomplete_charset_directive_is_ordinary_content(tmp_path: Path) -> None:
    """Only a complete `@charset` rule may precede the notice."""

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "theme.css"
    _write(
        repository / relative,
        f"@charset-custom value;\n/* {COPYRIGHT_NOTICE} */\n"
        f"/* {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "missing a valid inline notice" in messages


def test_head_state_needs_a_structural_head_tag(tmp_path: Path) -> None:
    """Prose mentioning a head tag is not a head container, so the region still ends."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f"Documents open with a <head> element.\n\nExample: `{notice}`.\n",
    )
    tracked.append(relative)
    tracked.sort()

    report = _evaluate(repository, policy_path, tracked)

    assert report.findings == (), _messages(report)


def test_a_quoted_value_spanning_lines_keeps_the_tag_open(tmp_path: Path) -> None:
    """Quote state rides along, because an attribute value may itself wrap."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"<div\n  title=\"a\n  > b\"\n  id='c'>\n<!-- {notice} -->\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_head_tag_after_leading_markup_opens_head_state(tmp_path: Path) -> None:
    """`<html><head>` on one line is still a head container."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f"<html><head>\n<title>Upstream\nlibrary</title>\n<!-- {notice} -->\n</head>\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_a_comment_containing_a_delimiter_is_tracked_as_a_comment(tmp_path: Path) -> None:
    """At the same position a block opener wins over the tag candidate.

    For `<!-- … > … -->` both match at zero. Choosing the tag let the `>` inside the
    comment close it, so a later line ended the region before the notice inside.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(repository / relative, f"<!-- a > b\n{notice}\n-->\nBody prose.\n")
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


@pytest.mark.parametrize(
    "directive",
    [
        '  @charset "UTF-8";',
        '@CHARSET "UTF-8";',
        "@charset 'UTF-8';",
        '@charset  "UTF-8";',
        '@charset "UTF-8"; ',
        '@charset "";',
        '@charset "not-a-real-charset";',
        '@charset "iso-8859-1";',
    ],
)
def test_only_the_exact_charset_form_may_precede_the_notice(tmp_path: Path, directive: str) -> None:
    """None of these is a CSS encoding declaration, so each is ordinary content."""

    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "theme.css"
    _write(
        repository / relative,
        f"{directive}\n/* {COPYRIGHT_NOTICE} */\n/* {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "missing a valid inline notice" in messages


def test_a_charset_directive_must_be_the_first_line(tmp_path: Path) -> None:
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    relative = "theme.css"
    _write(
        repository / relative,
        f'\n@charset "UTF-8";\n/* {COPYRIGHT_NOTICE} */\n'
        f"/* {SPDX_MARKER} {LICENSE_EXPRESSION} */\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "missing a valid inline notice" in messages


def test_a_head_close_inside_a_comment_does_not_close_head_state(tmp_path: Path) -> None:
    """A close written inside a comment is text, not structure."""

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f"<head>\n<!-- the </head> tag ends it -->\n<title>Upstream\nlibrary</title>\n"
        f"<!-- {notice} -->\n</head>\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages


def test_many_unterminated_openers_stay_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed forward scan is remembered, so the file is not rescanned per line.

    Asserted as a call bound rather than elapsed time: wall-clock in a required gate
    measures worker contention as well as the work, so correct linear code can fail it.
    """

    lines = 3000
    text = "".join(f"<div{n}\n" for n in range(lines))
    original = license_policy._advance_markup_state
    calls = 0

    def counted(line: str, state: tuple[str, ...] | None) -> tuple[str, ...] | None:
        nonlocal calls
        calls += 1
        return original(line, state)

    monkeypatch.setattr(license_policy, "_advance_markup_state", counted)
    groups = license_policy._logical_lines(text)

    assert len(groups) == lines
    assert all(len(group) == 1 for group in groups)
    # Linear: one classification per line, plus the single failed forward scan whose
    # result the memo retains. Quadratic behavior would be on the order of lines ** 2.
    assert calls <= 3 * lines


def test_an_unterminated_construct_does_not_suppress_another_kind(tmp_path: Path) -> None:
    """The memo is per construct kind, or one dead opener hides every later join.

    A stylesheet comment that never closes says nothing about whether a later tag
    closes, so caching by position alone would skip the tag's group and end the
    region before the notice inside it.
    """

    relative = "docs/upstream.md"
    repository, policy_path, tracked, _policy = _base_repository(tmp_path)
    notice = f"Copy{'right'} (c) 2026 Other Author"
    _write(
        repository / relative,
        f"/* never closed\n<div\n  class='a'>\n<!-- {notice} -->\n",
    )
    tracked.append(relative)
    tracked.sort()

    messages = _messages(_evaluate(repository, policy_path, tracked))

    assert relative in messages
    assert "unregistered third-party notice" in messages
