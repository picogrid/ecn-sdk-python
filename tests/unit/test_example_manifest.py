# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from scripts.release_checks import ArtifactPolicyError, inspect_example_manifest  # noqa: E402


def _entry() -> dict[str, Any]:
    return {
        "id": "demo",
        "source_path": "examples/demo.py",
        "title": "Demo",
        "summary": "Synthetic manifest entry.",
        "workflow": {
            "module": "picogrid_ecn_client.workflows.diagnostics",
            "function": "preflight",
        },
        "required_inputs": [],
        "safety_class": "read",
        "modes": ["offline-check"],
        "documentation": ["docs/demo.md"],
        "notebook_eligible": True,
        "exclusion_reason": None,
    }


def _repository(tmp_path: Path, *, entries: list[dict[str, Any]] | None = None) -> Path:
    repository = tmp_path / "repository"
    examples = repository / "examples"
    docs = repository / "docs"
    examples.mkdir(parents=True)
    docs.mkdir()
    for name in ("__init__.py", "_common.py", "demo.py"):
        (examples / name).write_text("\n", encoding="utf-8")
    (docs / "demo.md").write_text("# Demo\n", encoding="utf-8")
    _write_manifest(repository, entries or [_entry()])
    return repository


def _write_manifest(repository: Path, entries: list[dict[str, Any]]) -> None:
    manifest = {"schema_version": 1, "examples": entries}
    (repository / "examples" / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _assert_valid(repository: Path) -> None:
    assert inspect_example_manifest(repository) == ("examples/demo.py",)


def test_manifest_accepts_existing_source_paths(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


def test_manifest_rejects_missing_source_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["source_path"] = "examples/missing.py"
    entry["id"] = "missing"
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=r"source_path.*missing"):
        inspect_example_manifest(repository)


def test_manifest_accepts_exact_example_tree_inventory(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


def test_manifest_rejects_undeclared_example_file(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "examples" / "extra.py").write_text("\n", encoding="utf-8")

    with pytest.raises(ArtifactPolicyError, match="example inventory mismatch"):
        inspect_example_manifest(repository)


def test_manifest_accepts_importable_exported_workflow(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("module", "picogrid_ecn_client.workflows.missing", "cannot be imported"),
        ("function", "dataclass", "not exported"),
    ],
)
def test_manifest_rejects_invalid_public_workflow(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["workflow"][field] = value
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=message):
        inspect_example_manifest(repository)


def test_manifest_rejects_extra_workflow_metadata(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["workflow"]["extra"] = "unexpected"
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="workflow"):
        inspect_example_manifest(repository)


def test_manifest_accepts_existing_documentation_reference(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


def test_manifest_rejects_missing_documentation_reference(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["documentation"] = ["docs/missing.md"]
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=r"documentation.*missing"):
        inspect_example_manifest(repository)


def test_manifest_rejects_duplicate_documentation_reference(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["documentation"] *= 2
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=r"documentation.*duplicated"):
        inspect_example_manifest(repository)


def test_manifest_accepts_reason_for_ineligible_notebook(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["notebook_eligible"] = False
    entry["exclusion_reason"] = "Requires an interactive terminal."
    _write_manifest(repository, [entry])

    _assert_valid(repository)


def test_manifest_rejects_ineligible_notebook_without_reason(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["notebook_eligible"] = False
    entry["exclusion_reason"] = ""
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="exclusion_reason"):
        inspect_example_manifest(repository)


def test_manifest_rejects_reason_for_eligible_notebook(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["exclusion_reason"] = "Unexpected reason."
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="exclusion_reason"):
        inspect_example_manifest(repository)


def test_manifest_rejects_workflow_routed_through_a_private_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = importlib.import_module("picogrid_ecn_client.workflows")
    retention = importlib.import_module("picogrid_ecn_client.workflows._retention")
    # Re-export the genuine public callable from a private module, so the route passes
    # every non-boundary condition: the name is exported, callable, and identical.
    monkeypatch.setattr(retention, "preflight", workflows.preflight, raising=False)
    repository = _repository(tmp_path)
    entry = _entry()
    entry["workflow"] = {
        "module": "picogrid_ecn_client.workflows._retention",
        "function": "preflight",
    }
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="workflow is invalid"):
        inspect_example_manifest(repository)


def test_manifest_rejects_workflow_not_owned_by_the_declared_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    geodesy = importlib.import_module("picogrid_ecn_client.workflows.geodesy")

    def _impostor() -> None:  # pragma: no cover - never invoked
        raise AssertionError("the impostor must never be routed")

    # A same-named callable on another public workflow module: exported and callable,
    # but not the object `picogrid_ecn_client.workflows.preflight` resolves to.
    monkeypatch.setattr(geodesy, "preflight", _impostor, raising=False)
    repository = _repository(tmp_path)
    entry = _entry()
    entry["workflow"] = {
        "module": "picogrid_ecn_client.workflows.geodesy",
        "function": "preflight",
    }
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="not exported"):
        inspect_example_manifest(repository)


def test_manifest_accepts_unique_sorted_filename_derived_ids(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


@pytest.mark.parametrize("violation", ["duplicate", "unsorted", "filename"])
def test_manifest_rejects_invalid_id_contract(tmp_path: Path, violation: str) -> None:
    repository = _repository(tmp_path)
    second = _entry()
    second["id"] = "second"
    second["source_path"] = "examples/second.py"
    (repository / "examples" / "second.py").write_text("\n", encoding="utf-8")
    entries = [_entry(), second]
    if violation == "duplicate":
        second["id"] = "demo"
    elif violation == "unsorted":
        entries.reverse()
    else:
        second["id"] = "wrong"
    _write_manifest(repository, entries)

    with pytest.raises(ArtifactPolicyError, match="id"):
        inspect_example_manifest(repository)


def test_manifest_accepts_allowed_safety_class_and_modes(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("safety_class", "task-dispatch"),
        ("modes", ["offline-check", "future-mode"]),
        ("modes", ["offline-check", "offline-check"]),
    ],
)
def test_manifest_rejects_unknown_safety_or_mode(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry[field] = value
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=field):
        inspect_example_manifest(repository)


def test_manifest_accepts_deterministic_formatting(tmp_path: Path) -> None:
    _assert_valid(_repository(tmp_path))


def test_manifest_rejects_nondeterministic_formatting(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manifest_path = repository / "examples" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactPolicyError, match="deterministic formatting"):
        inspect_example_manifest(repository)


def test_manifest_rejects_unadvertised_entry_field(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["extra"] = "unexpected"
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match="entry fields"):
        inspect_example_manifest(repository)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("summary", None),
        ("required_inputs", {}),
    ],
)
def test_manifest_rejects_malformed_entry_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry[field] = value
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=field):
        inspect_example_manifest(repository)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("kind", "argument"),
        ("type", "mapping"),
        ("required", "yes"),
        ("default", {}),
        ("description", ""),
    ],
)
def test_manifest_rejects_malformed_required_input_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    required_input: dict[str, object] = {
        "name": "ECN_SAMPLE",
        "kind": "env",
        "type": "integer",
        "required": False,
        "default": 1,
        "description": "Number of samples.",
    }
    required_input[field] = value
    if field == "required":
        required_input.pop("default")
    entry["required_inputs"] = [required_input]
    _write_manifest(repository, [entry])

    with pytest.raises(ArtifactPolicyError, match=field):
        inspect_example_manifest(repository)


def test_manifest_canonical_form_preserves_utf8_text(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entry = _entry()
    entry["summary"] = "Measure a 90° turn."
    manifest = {"schema_version": 1, "examples": [entry]}
    (repository / "examples" / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _assert_valid(repository)
