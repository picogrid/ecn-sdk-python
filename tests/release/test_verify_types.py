# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts.verify_types import (
    Diagnostic,
    _export_worktree,
    _load_allowlist,
    _parse_mypy,
    _parse_pyright,
    _pydantic_model_owners,
    expected_error_markers,
    normalize_report,
    reconcile_diagnostics,
    strict_pyright_project,
)


def test_expected_error_markers_require_trailing_exact_marker(tmp_path: Path) -> None:
    fixture = tmp_path / "negative" / "example.py"
    fixture.parent.mkdir()
    fixture.write_text(
        "bad()  # expect-type-error\n"
        "also_bad()  # expect-type-error trailing-text\n"
        "# expect-type-error\n",
        encoding="utf-8",
    )

    assert expected_error_markers(tmp_path) == {
        ("negative/example.py", 1),
        ("negative/example.py", 3),
    }


def test_reconcile_diagnostics_accepts_multiple_errors_on_marker_line() -> None:
    markers = {("negative/example.py", 4)}
    reconciliation = reconcile_diagnostics(
        markers,
        [
            Diagnostic("negative/example.py", 4, "first"),
            Diagnostic("negative/example.py", 4, "second"),
        ],
    )

    assert reconciliation.matches
    assert reconciliation.missing == ()
    assert reconciliation.unexpected == ()


def test_reconcile_diagnostics_reports_missing_and_unexpected() -> None:
    reconciliation = reconcile_diagnostics(
        {("negative/example.py", 4), ("negative/example.py", 8)},
        [
            Diagnostic("negative/example.py", 8, "expected"),
            Diagnostic("positive/clean.py", 2, "bad"),
        ],
    )

    assert reconciliation.missing == (("negative/example.py", 4),)
    assert reconciliation.unexpected == (Diagnostic("positive/clean.py", 2, "bad"),)


def test_synthetic_checker_outputs_reconcile_to_same_markers(tmp_path: Path) -> None:
    markers = {("negative/example.py", 7)}
    fixture = tmp_path / "negative" / "example.py"
    fixture.parent.mkdir()
    fixture.write_text("\n" * 7, encoding="utf-8")
    mypy = _parse_mypy(
        f"{fixture}:7:5: error: incompatible assignment  [assignment]\n",
        tmp_path,
    )
    pyright_output = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": str(fixture),
                    "severity": "error",
                    "message": "Type is not assignable",
                    "range": {
                        "start": {"line": 6, "character": 4},
                        "end": {"line": 6, "character": 10},
                    },
                },
                {
                    "file": str(fixture),
                    "severity": "warning",
                    "message": "warning is not an error",
                    "range": {
                        "start": {"line": 1, "character": 0},
                        "end": {"line": 1, "character": 1},
                    },
                },
            ]
        }
    )
    pyright, _ = _parse_pyright(pyright_output, tmp_path)

    assert reconcile_diagnostics(markers, mypy).matches
    assert reconcile_diagnostics(markers, pyright).matches


def test_normalize_report_removes_paths_and_timing_recursively(tmp_path: Path) -> None:
    report = {
        "time": "unstable",
        "python": {
            "3.11": {
                "completeness_score": 1.0,
                "moduleRootDirectory": str(tmp_path / "package"),
                "nested": {"timeInSec": 0.5, "file": str(tmp_path / "fixture.py")},
            }
        },
    }

    assert normalize_report(report) == {
        "python": {
            "3.11": {
                "completeness_score": 1.0,
                "nested": {"file": "fixture.py"},
            }
        }
    }


def test_strict_pyright_project_forces_strict_consumer_checking(tmp_path: Path) -> None:
    project = strict_pyright_project(tmp_path, "3.12")

    assert project.name == "pyrightconfig.json"
    settings = json.loads(project.read_text(encoding="utf-8"))
    assert settings == {"typeCheckingMode": "strict", "pythonVersion": "3.12"}


def test_export_worktree_skips_paths_deleted_from_the_working_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "kept.py").write_text("kept = True\n", encoding="utf-8")
    (repository / "removed.py").write_text("removed = True\n", encoding="utf-8")
    (repository / "valid-link.py").symlink_to("src/kept.py")
    (repository / "broken-link.py").symlink_to("src/never-created.py")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=repository,
        check=True,
    )
    # Tracked in the index, absent from the working tree: tar cannot stat it.
    (repository / "removed.py").unlink()
    (repository / "added.py").write_text("added = True\n", encoding="utf-8")

    destination = tmp_path / "candidate"
    destination.mkdir()
    _export_worktree(repository, destination)

    assert (destination / "src" / "kept.py").is_file()
    assert (destination / "added.py").is_file()
    assert not (destination / "removed.py").exists()
    # lexists, not exists: a tracked symlink must survive even when dangling.
    assert (destination / "valid-link.py").is_symlink()
    assert (destination / "broken-link.py").is_symlink()
    assert not (destination / "broken-link.py").exists()


def test_allowlist_rejects_sdk_authored_entries(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps([{"name": "picogrid_ecn_client.ECNClient.start", "reason": "why"}]),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="not framework-generated"):
        _load_allowlist(allowlist)


@pytest.mark.parametrize(
    "name",
    [
        "picogrid_ecn_client.models.entity.Entity.model_custom",
        "picogrid_ecn_client.models.entity.Entity.__custom__",
        "picogrid_ecn_client.models.entity.Entity.model_export",
    ],
)
def test_allowlist_rejects_lookalike_framework_names(tmp_path: Path, name: str) -> None:
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps([{"name": name, "reason": "looks generated"}]), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="not framework-generated"):
        _load_allowlist(allowlist)


def test_allowlist_rejects_framework_names_on_non_model_owners(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps([{"name": "picogrid_ecn_client.client.ECNClient.model_fields", "reason": "no"}]),
        encoding="utf-8",
    )
    owners = _pydantic_model_owners(Path(__file__).parents[2])

    with pytest.raises(RuntimeError, match="approved Pydantic model"):
        _load_allowlist(allowlist, owners)


def test_allowlist_accepts_framework_generated_entries(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            [
                {
                    "name": "picogrid_ecn_client.models.entity.Entity.model_config",
                    "reason": "pydantic",
                }
            ]
        ),
        encoding="utf-8",
    )

    owners = _pydantic_model_owners(Path(__file__).parents[2])

    assert _load_allowlist(allowlist, owners) == {
        "picogrid_ecn_client.models.entity.Entity.model_config": "pydantic"
    }
