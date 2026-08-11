# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.sync_dep_locks import (
    REPOSITORY,
    SyncError,
    _build_requirement_conflicts,
    _declaration_conflicts,
    _dependency_group_declarations,
    _direct_declarations,
    _exact_pin,
    _manifest_pin_conflicts,
    _optional_dependency_declarations,
    _pinned_version_sets,
    _report_unsupported_declarations,
    _shared_pin_conflicts,
    _source_declarations,
    _source_inputs,
    _stale_lock_pins,
    _synchronized_policy_text,
    _unsupported_declarations,
    canonical_root_requirement,
    requirement_name,
)


def _policy_text(direct_dependencies: list[str]) -> str:
    direct = ",\n".join(f'    "{name}": {{}}' for name in direct_dependencies)
    return (
        "{\n"
        '  "project_version": "0.1.0",\n'
        '  "direct_runtime_dependencies": {\n'
        f"{direct}\n"
        "  },\n"
        '  "runtime_requirements": [\n'
        '    "aiomqtt==2.5.1",\n'
        '    "protobuf<8,>=7.35.1"\n'
        "  ],\n"
        '  "operator_runtime_requirements": [\n'
        '    "uvicorn==0.52.0"\n'
        "  ],\n"
        '  "operator_optional_runtime_requirements": [\n'
        '    "httpx==0.28.0; extra == \\"test\\""\n'
        "  ]\n"
        "}\n"
    )


def _policy_array(text: str, key: str) -> list[str]:
    loaded = json.loads(text)
    values = loaded[key]
    assert isinstance(values, list)
    return values


_OPERATOR_VERSIONS = {"uvicorn": {"0.52.1"}, "httpx": {"0.28.1"}}


def test_marker_forked_declarations_each_appear_exactly_once() -> None:
    dependencies = [
        "aiomqtt>=2.5,<3",
        'protobuf>=7.35.1,<8 ; python_version >= "3.12"',
        'protobuf>=6.31,<7 ; python_version < "3.12"',
    ]

    synchronized = _synchronized_policy_text(
        _policy_text(["aiomqtt", "protobuf"]),
        dependencies,
        _OPERATOR_VERSIONS,
        ["uvicorn==0.52.1"],
    )

    assert _policy_array(synchronized, "runtime_requirements") == [
        "aiomqtt<3,>=2.5",
        'protobuf<7,>=6.31; python_version < "3.12"',
        'protobuf<8,>=7.35.1; python_version >= "3.12"',
    ]


def test_forked_declarations_change_policy_array_cardinality() -> None:
    dependencies = [
        'protobuf>=7.35.1,<8 ; python_version >= "3.12"',
        'protobuf>=6.31,<7 ; python_version < "3.12"',
    ]

    synchronized = _synchronized_policy_text(
        _policy_text(["protobuf"]),
        dependencies,
        _OPERATOR_VERSIONS,
        ["uvicorn==0.52.1"],
    )

    requirements = _policy_array(synchronized, "runtime_requirements")
    assert len(requirements) == 2
    assert len(set(requirements)) == 2
    assert {requirement_name(requirement) for requirement in requirements} == {"protobuf"}


def test_operator_array_is_rebuilt_from_manifest_declarations() -> None:
    dependencies = ["aiomqtt>=2.5,<3", "protobuf>=7.35.1,<8"]
    operator = [
        "wsproto==1.3.2",
        'uvicorn==0.52.2 ; sys_platform != "win32"',
        'uvicorn==0.52.1 ; sys_platform == "win32"',
    ]

    synchronized = _synchronized_policy_text(
        _policy_text(["aiomqtt", "protobuf"]), dependencies, _OPERATOR_VERSIONS, operator
    )

    assert _policy_array(synchronized, "operator_runtime_requirements") == [
        'uvicorn==0.52.1; sys_platform == "win32"',
        'uvicorn==0.52.2; sys_platform != "win32"',
        "wsproto==1.3.2",
    ]


def test_operator_version_change_in_manifest_reaches_the_policy() -> None:
    dependencies = ["aiomqtt>=2.5,<3", "protobuf>=7.35.1,<8"]

    synchronized = _synchronized_policy_text(
        _policy_text(["aiomqtt", "protobuf"]),
        dependencies,
        _OPERATOR_VERSIONS,
        ["uvicorn==0.52.2"],
    )

    assert _policy_array(synchronized, "operator_runtime_requirements") == ["uvicorn==0.52.2"]


def test_synchronization_is_idempotent_and_preserves_surrounding_formatting() -> None:
    dependencies = ["aiomqtt>=2.5,<3", "protobuf>=7.35.1,<8"]
    policy = _policy_text(["aiomqtt", "protobuf"])
    operator = ["uvicorn==0.52.1"]

    first = _synchronized_policy_text(policy, dependencies, _OPERATOR_VERSIONS, operator)
    second = _synchronized_policy_text(first, dependencies, _OPERATOR_VERSIONS, operator)

    assert first == second
    assert first.startswith('{\n  "project_version": "0.1.0",\n')
    assert '    "aiomqtt<3,>=2.5",\n    "protobuf<8,>=7.35.1"\n  ]' in first
    assert '"uvicorn==0.52.1"' in first
    assert '"httpx==0.28.1; extra == \\"test\\""' in first


def test_unreviewed_dependency_addition_fails_before_rewriting() -> None:
    policy = _policy_text(["aiomqtt", "protobuf"])
    dependencies = ["aiomqtt>=2.5,<3", "protobuf>=7.35.1,<8", "httpx>=0.28,<1"]

    with pytest.raises(SyncError, match=r"added: httpx.*removed: none"):
        _synchronized_policy_text(policy, dependencies, _OPERATOR_VERSIONS, ["uvicorn==0.52.1"])


def test_unreviewed_dependency_removal_fails_before_rewriting() -> None:
    policy = _policy_text(["aiomqtt", "protobuf"])
    dependencies = ["protobuf>=7.35.1,<8"]

    with pytest.raises(SyncError, match=r"added: none.*removed: aiomqtt"):
        _synchronized_policy_text(policy, dependencies, _OPERATOR_VERSIONS, ["uvicorn==0.52.1"])


def test_optional_pin_ambiguity_is_scoped_to_referenced_packages() -> None:
    dependencies = ["aiomqtt>=2.5,<3", "protobuf>=7.35.1,<8"]
    versions = {"uvicorn": {"0.52.1"}, "httpx": {"0.28.1"}, "tomli": {"1.0.0", "2.0.0"}}
    policy = _policy_text(["aiomqtt", "protobuf"])

    synchronized = _synchronized_policy_text(policy, dependencies, versions, ["uvicorn==0.52.1"])
    assert '"httpx==0.28.1' in synchronized

    with pytest.raises(SyncError, match="ambiguous resolved versions"):
        _synchronized_policy_text(
            synchronized.replace('"httpx==0.28.1', '"tomli==1.0.0'),
            dependencies,
            versions,
            ["uvicorn==0.52.1"],
        )


def test_missing_policy_arrays_fail_loudly() -> None:
    with pytest.raises(SyncError, match="direct_runtime_dependencies"):
        _synchronized_policy_text('{"project_version": "0.1.0"}', ["aiomqtt>=2.5,<3"], {}, [])

    incomplete = '{\n  "direct_runtime_dependencies": {\n    "aiomqtt": {}\n  }\n}\n'
    with pytest.raises(SyncError, match="runtime_requirements"):
        _synchronized_policy_text(incomplete, ["aiomqtt>=2.5,<3"], {}, [])


@pytest.mark.parametrize(
    "policy",
    [
        "[]",
        "null",
        '"policy"',
        '{"direct_runtime_dependencies": ["aiomqtt"]}',
    ],
)
def test_non_object_policy_document_fails_loudly(policy: str) -> None:
    with pytest.raises(SyncError, match="direct_runtime_dependencies"):
        _synchronized_policy_text(policy, ["aiomqtt>=2.5,<3"], {}, [])


def test_canonical_root_requirement_sorts_clauses_and_keeps_extras_and_marker() -> None:
    dependency = 'uvicorn[standard]>=0.52.1,<1 ; python_version >= "3.11"'
    assert canonical_root_requirement(dependency) == (
        'uvicorn[standard]<1,>=0.52.1; python_version >= "3.11"'
    )
    assert requirement_name(dependency) == "uvicorn"


def _write_lock_pair(root: Path, in_body: str, lock_body: str) -> Path:
    (root / "requirements.in").write_text(in_body, encoding="utf-8")
    lock = root / "requirements.txt"
    lock.write_text(
        "# This file was autogenerated by uv via the following command:\n"
        "#    uv pip compile requirements.in --generate-hashes --universal -o requirements.txt\n"
        f"{lock_body}",
        encoding="utf-8",
    )
    return lock


def test_marker_change_in_source_pin_marks_lock_stale(tmp_path: Path) -> None:
    lock = _write_lock_pair(
        tmp_path,
        'foo==1.0 ; sys_platform == "win32"\n',
        "foo==1.0 ; sys_platform == 'win32' \\\n    --hash=sha256:0\n",
    )
    assert _stale_lock_pins(lock) is False

    (tmp_path / "requirements.in").write_text(
        'foo==1.0 ; sys_platform != "win32"\n', encoding="utf-8"
    )
    assert _stale_lock_pins(lock) is True


def test_unmarked_source_pin_requires_unmarked_lock_entry(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    lock = _write_lock_pair(
        clean,
        "foo==1.0\n",
        "foo==1.0 \\\n    --hash=sha256:0\n",
    )
    assert _stale_lock_pins(lock) is False

    # A pin that loses its marker must not be satisfied by a stale
    # platform-qualified entry: the dependency would be absent elsewhere.
    marked = tmp_path / "marked"
    marked.mkdir()
    marker_only = _write_lock_pair(
        marked,
        "foo==1.0\n",
        "foo==1.0 ; sys_platform == 'win32' \\\n    --hash=sha256:0\n",
    )
    assert _stale_lock_pins(marker_only) is True

    (clean / "requirements.in").write_text("foo==1.1\n", encoding="utf-8")
    assert _stale_lock_pins(lock) is True


def test_manifest_pin_conflicts_name_the_lock_and_versions(tmp_path: Path) -> None:
    lock = _write_lock_pair(tmp_path, "httpx==0.28.1\n", "httpx==0.28.1\n")
    assert _manifest_pin_conflicts(["httpx==0.28.1"], lock) == []

    conflicts = _manifest_pin_conflicts(["httpx==0.29.0", "ruff>=0.16"], lock)
    assert len(conflicts) == 2
    assert "httpx==0.29.0" in conflicts[0]
    assert "0.28.1" in conflicts[0]
    assert conflicts[1] == "'ruff>=0.16' (operator declarations must use exact pins)"

    extras_conflicts = _manifest_pin_conflicts(["httpx[http2]==0.28.1"], lock)
    assert len(extras_conflicts) == 1
    assert extras_conflicts[0].startswith("httpx[http2]==0.28.1")


def test_manifest_pin_conflicts_compare_markers_and_ranges(tmp_path: Path) -> None:
    lock = _write_lock_pair(
        tmp_path,
        'foo==1.0 ; sys_platform != "win32"\n',
        "foo==1.0 ; sys_platform != 'win32' \\\n    --hash=sha256:0\n",
    )

    matching = ['foo==1.0 ; sys_platform != "win32"']
    assert _manifest_pin_conflicts(matching, lock) == []

    flipped = ['foo==1.0 ; sys_platform == "win32"']
    conflicts = _manifest_pin_conflicts(flipped, lock)
    assert len(conflicts) == 1
    assert "sys_platform == 'win32'" in conflicts[0]
    assert "sys_platform != 'win32'" in conflicts[0]

    ranged = _manifest_pin_conflicts(["foo>=1,<2"], lock)
    assert ranged == ["'foo>=1,<2' (operator declarations must use exact pins)"]


def test_build_requirement_conflicts_check_pins_and_presence(tmp_path: Path) -> None:
    lock = _write_lock_pair(tmp_path, "wheel==0.47.0\n", "wheel==0.47.0\nsetuptools==83.0.0\n")

    assert _build_requirement_conflicts(["wheel==0.47.0", "setuptools>=83,<84"], lock) == []

    conflicts = _build_requirement_conflicts(["wheel==0.48.0", "flit-core>=3,<4"], lock)
    assert len(conflicts) == 2
    assert "wheel==0.48.0" in conflicts[0]
    assert "0.47.0" in conflicts[0]
    assert conflicts[1] == f"'flit-core>=3,<4' (absent from {lock.as_posix()})"

    ranged = _build_requirement_conflicts(["setuptools>=84,<85"], lock)
    assert len(ranged) == 1
    assert "setuptools>=84,<85" in ranged[0]
    assert "83.0.0" in ranged[0]


def test_shared_pin_conflicts_only_where_lock_resolves_the_name(tmp_path: Path) -> None:
    lock = _write_lock_pair(tmp_path, "aiomqtt==2.5.1\n", "aiomqtt==2.5.1\n")

    assert (
        _shared_pin_conflicts(["aiomqtt==2.5.1", "protobuf>=7,<8", "paho-mqtt==2.1.0"], lock) == []
    )

    conflicts = _shared_pin_conflicts(["aiomqtt==2.6.0"], lock)
    assert len(conflicts) == 1
    assert "aiomqtt==2.6.0" in conflicts[0]
    assert "2.5.1" in conflicts[0]

    ranged = _shared_pin_conflicts(["aiomqtt>=2.6,<3"], lock)
    assert len(ranged) == 1
    assert "aiomqtt>=2.6,<3" in ranged[0]
    assert _shared_pin_conflicts(["aiomqtt>=2.5,<3"], lock) == []


def test_ranged_declarations_cover_every_applicable_lock_branch(tmp_path: Path) -> None:
    lock = _write_lock_pair(
        tmp_path,
        "foo==2.5\n",
        "foo==1.5 ; python_version < '3.12' \\\n    --hash=sha256:0\n"
        "foo==2.5 ; python_version >= '3.12' \\\n    --hash=sha256:0\n",
    )

    # An unmarked range applies everywhere: the 1.5 branch violates it.
    partial = _shared_pin_conflicts(["foo>=2,<3"], lock)
    assert len(partial) == 1
    assert "foo>=2,<3" in partial[0]
    assert "1.5" in partial[0]

    # A range covering both branches passes.
    assert _shared_pin_conflicts(["foo>=1,<3"], lock) == []

    # A marker-qualified range governs only the matching branch.
    # These helper paths sit beneath the gate; real manifests now reject markers outright.
    assert _shared_pin_conflicts(['foo>=2,<3 ; python_version >= "3.12"'], lock) == []
    disjoint = _shared_pin_conflicts(['foo>=2,<3 ; python_version == "3.99"'], lock)
    assert len(disjoint) == 1
    assert "no applicable locked entry" in disjoint[0]


def test_ranged_declarations_require_declared_extras_in_lock(tmp_path: Path) -> None:
    base_only = tmp_path / "base"
    base_only.mkdir()
    lock = _write_lock_pair(base_only, "foo==1.5\n", "foo==1.5\n")

    conflicts = _shared_pin_conflicts(["foo[feature]>=1,<2"], lock)
    assert len(conflicts) == 1
    assert "foo[feature]>=1,<2" in conflicts[0]

    with_extra = tmp_path / "extra"
    with_extra.mkdir()
    extra_lock = _write_lock_pair(with_extra, "foo[feature]==1.5\n", "foo[feature]==1.5\n")
    assert _shared_pin_conflicts(["foo[feature]>=1,<2"], extra_lock) == []
    # A plain range is satisfied by an extras-bearing entry.
    assert _shared_pin_conflicts(["foo>=1,<2"], extra_lock) == []


def test_pin_removed_from_input_marks_retained_lock_stale(tmp_path: Path) -> None:
    lock = _write_lock_pair(
        tmp_path,
        "bar==2.0\nfoo==1.0\n",
        "bar==2.0 \\\n    --hash=sha256:0\n    # via -r requirements.in\n"
        "foo==1.0 \\\n    --hash=sha256:0\n"
        "    # via\n    #   -r requirements.in\n    #   bar\n",
    )
    assert _stale_lock_pins(lock) is False

    # Removing a pin from the input must not leave the compiled lock accepted:
    # the retained entry would ship an obsolete package.
    (tmp_path / "requirements.in").write_text("bar==2.0\n", encoding="utf-8")
    assert _stale_lock_pins(lock) is True

    # A transitive entry (annotated only via another package) is untouched.
    transitive = tmp_path / "transitive"
    transitive.mkdir()
    lock_two = _write_lock_pair(
        transitive,
        "bar==2.0\n",
        "bar==2.0 \\\n    --hash=sha256:0\n    # via -r requirements.in\n"
        "foo==1.0 \\\n    --hash=sha256:0\n    # via bar\n",
    )
    assert _stale_lock_pins(lock_two) is False


def test_removed_direct_branch_of_declared_package_marks_lock_stale(tmp_path: Path) -> None:
    both = (
        "foo==1.0 ; sys_platform == 'win32' \\\n    --hash=sha256:0\n"
        "    # via -r requirements.in\n"
        "foo==2.0 ; sys_platform != 'win32' \\\n    --hash=sha256:0\n"
        "    # via -r requirements.in\n"
    )
    lock = _write_lock_pair(
        tmp_path,
        'foo==1.0 ; sys_platform == "win32"\nfoo==2.0 ; sys_platform != "win32"\n',
        both,
    )
    assert _stale_lock_pins(lock) is False

    # Removing only the Windows branch keeps the name declared, but the
    # retained direct win32 entry no longer matches any source pin tuple.
    (tmp_path / "requirements.in").write_text(
        'foo==2.0 ; sys_platform != "win32"\n', encoding="utf-8"
    )
    assert _stale_lock_pins(lock) is True

    # A ranged declaration keeps its resolver-chosen direct entry acceptable.
    ranged = tmp_path / "ranged"
    ranged.mkdir()
    ranged_lock = _write_lock_pair(
        ranged,
        "bar>=1,<3\n",
        "bar==1.5 \\\n    --hash=sha256:0\n    # via -r requirements.in\n",
    )
    assert _stale_lock_pins(ranged_lock) is False


def test_reversed_marker_operands_match_canonicalized_lock(tmp_path: Path) -> None:
    # uv writes the variable-first canonical form into compiled locks; a source
    # pin written constant-first must still converge after `make sync-deps`.
    lock = _write_lock_pair(
        tmp_path,
        "foo==1.0 ; 'win32' == sys_platform\nbar==2.0 ; \"3.12\" > python_version\n",
        "foo==1.0 ; sys_platform == 'win32' \\\n    --hash=sha256:0\n"
        "    # via -r requirements.in\n"
        "bar==2.0 ; python_version < '3.12' \\\n    --hash=sha256:0\n"
        "    # via -r requirements.in\n",
    )
    assert _stale_lock_pins(lock) is False

    # A genuinely different reversed marker still conflicts.
    (tmp_path / "requirements.in").write_text(
        "foo==1.0 ; 'linux' == sys_platform\nbar==2.0 ; \"3.12\" > python_version\n",
        encoding="utf-8",
    )
    assert _stale_lock_pins(lock) is True


def test_markerless_declarations_are_supported() -> None:
    assert (
        _declaration_conflicts(
            Path("operator-app/requirements.in"),
            ["aiomqtt==2.5.1", "protobuf>=7,<8", "uvicorn[standard]==0.52.1"],
        )
        == []
    )


def test_marker_qualified_declaration_is_rejected() -> None:
    manifest = Path("operator-app/requirements.in")
    declaration = 'My_Package==1.0 ; python_version < "3.12"'

    conflicts = _declaration_conflicts(manifest, [declaration])

    assert len(conflicts) == 1
    assert manifest.as_posix() in conflicts[0]
    assert "my-package" in conflicts[0]
    assert declaration in conflicts[0]


# This is deliberately representative, not an exhaustive PEP 508 grammar matrix.
@pytest.mark.parametrize(
    "declaration",
    [
        'tomli==2.0.1 ; python_version < "3.12"',
        'tomli==2.0.1 ; python_full_version >= "3.11.4"',
        'colorama==0.4.6 ; sys_platform == "win32"',
        'foo==1.0 ; platform_machine in "x86_64 aarch64"',
        'foo==1.0 ; platform_machine not in "s390x"',
        "foo==1.0 ; 'win32' == sys_platform",
        'foo==1.0 ; implementation_name == "pypy"',
        'foo==1.0 ; python_version >= "3.11" and sys_platform != "win32"',
    ],
)
def test_every_marker_shape_requiring_interpretation_fails_closed(declaration: str) -> None:
    conflicts = _declaration_conflicts(Path("requirements.in"), [declaration])
    assert len(conflicts) == 1
    # Rejected *as a marker*, not incidentally by a spelling or reference rule.
    assert "environment markers are not compared" in conflicts[0]


def test_marker_qualified_input_declarations_survive_nested_includes(tmp_path: Path) -> None:
    outer = tmp_path / "outer.in"
    inner = tmp_path / "inner.in"
    declaration = 'tomli==2.0.1 ; python_version < "3.12"'
    outer.write_text("-r inner.in\n", encoding="utf-8")
    inner.write_text(f"{declaration}\n", encoding="utf-8")

    surfaced = _source_declarations(outer)
    assert (inner.resolve(), declaration) in surfaced

    inner_declarations = [
        requirement for manifest, requirement in surfaced if manifest == inner.resolve()
    ]
    conflicts = _declaration_conflicts(inner.resolve(), inner_declarations)
    assert len(conflicts) == 1
    assert declaration in conflicts[0]


def test_unsupported_marker_report_is_actionable_and_deterministic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    conflicts = [
        "operator-app/pyproject.toml: zulu ('zulu==1 ; sys_platform == \"win32\"')",
        "pyproject.toml: alpha ('alpha==1 ; python_version < \"3.12\"')",
    ]

    _report_unsupported_declarations(conflicts)

    stderr = capsys.readouterr().err
    assert "validates only markerless PEP 508 direct dependency declarations" in stderr
    assert all(f"  - {conflict}\n" in stderr for conflict in conflicts)
    assert stderr.endswith(
        "Express the dependency as a markerless requirement and run `make sync-deps`, "
        "or add reviewed checker support to scripts/sync_dep_locks.py before "
        "introducing this declaration.\n"
    )

    declarations = [
        'Zulu==1 ; sys_platform == "win32"',
        'alpha==1 ; python_version < "3.12"',
    ]
    reported = _declaration_conflicts(Path("pyproject.toml"), declarations)
    assert [conflict.split(" (")[0] for conflict in reported] == [
        "pyproject.toml: zulu",
        "pyproject.toml: alpha",
    ]
    assert all("environment markers are not compared" in conflict for conflict in reported)
    # The same declaration twice yields one conflict per occurrence here; the
    # deduplication and ordering live in `_unsupported_declarations()`, which
    # reports each distinct conflict once, sorted.
    repeated = _declaration_conflicts(Path("pyproject.toml"), [declarations[0]] * 2)
    assert len(repeated) == 2 and repeated[0] == repeated[1]
    assert _unsupported_declarations() == []


def test_repository_manifests_declare_nothing_unsupported() -> None:
    assert _unsupported_declarations() == []


def test_dependency_groups_are_part_of_the_declaration_inventory() -> None:
    # PEP 735 groups live outside [project], so they are easy to miss; the root
    # dev group is a real editable surface and must reach the marker gate.
    root = REPOSITORY / "pyproject.toml"
    group_declarations = _dependency_group_declarations(root)

    assert group_declarations, "root pyproject declares a dev dependency group"
    assert set(group_declarations) <= set(_direct_declarations()[root])


def test_marker_qualified_dependency_group_member_is_rejected(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[dependency-groups]\n"
        'dev = ["mypy>=1.17,<2", { include-group = "test" }]\n'
        "test = [\"tomli==2.0.1 ; python_version < '3.12'\"]\n",
        encoding="utf-8",
    )

    declarations = _dependency_group_declarations(pyproject)

    # The include-group table names a group, not a requirement, and is skipped.
    assert declarations == ["mypy>=1.17,<2", "tomli==2.0.1 ; python_version < '3.12'"]
    conflicts = _declaration_conflicts(pyproject, declarations)
    assert len(conflicts) == 1
    assert "tomli" in conflicts[0]


def _declared(source: Path) -> list[str]:
    return [text for _path, text in _source_declarations(source)]


def test_inline_comment_semicolon_is_not_an_environment_marker(tmp_path: Path) -> None:
    source = tmp_path / "requirements.in"
    source.write_text(
        "# leading comment; with a semicolon\n"
        "foo==1.0 # compatibility; markerless\n"
        'bar==2.0 ; python_version < "3.12" # genuinely marked\n',
        encoding="utf-8",
    )

    assert _declared(source) == ["foo==1.0", 'bar==2.0 ; python_version < "3.12"']
    conflicts = _declaration_conflicts(source, _declared(source))
    assert len(conflicts) == 1
    assert "bar" in conflicts[0]


def test_comment_ending_in_a_backslash_does_not_swallow_the_next_line(tmp_path: Path) -> None:
    # The backslash belongs to a comment, not to a continuation. Joining before
    # stripping would merge both lines and then delete everything after the `#`,
    # erasing the marker-qualified declaration entirely.
    source = tmp_path / "requirements.in"
    source.write_text('foo==1 # note \\\nbar==2 ; sys_platform == "win32"\n', encoding="utf-8")

    assert _declared(source) == ["foo==1", 'bar==2 ; sys_platform == "win32"']
    conflicts = _declaration_conflicts(source, _declared(source))
    assert len(conflicts) == 1
    assert "bar" in conflicts[0]


@pytest.mark.parametrize("include", ["-r inner.in", "  -r inner.in", "--requirement=inner.in"])
def test_every_uv_include_spelling_is_followed(tmp_path: Path, include: str) -> None:
    # uv follows indented and equals-separated includes; a form this collector
    # drops would hide the nested file's declarations from both gates.
    outer = tmp_path / "requirements.in"
    outer.write_text(f"{include}\n", encoding="utf-8")
    (tmp_path / "inner.in").write_text('tomli==2.0.1 ; python_version < "3.12"\n', encoding="utf-8")

    assert _declared(outer) == ['tomli==2.0.1 ; python_version < "3.12"']
    assert len(_declaration_conflicts(outer, _declared(outer))) == 1


@pytest.mark.parametrize(
    "line",
    [
        '-e ./pkg ; sys_platform == "win32"',
        '-e ./pkg; sys_platform == "win32"',
        '--editable=./pkg ; sys_platform == "win32"',
        './pkg ; sys_platform == "win32"',
        '../pkg ; sys_platform == "win32"',
        "-e ./pkg",
        "./pkg",
    ],
)
def test_declarations_the_checker_cannot_parse_fail_closed(tmp_path: Path, line: str) -> None:
    # Local paths and editables are not PEP 508 requirements, so the checker
    # cannot compare them to a lock at all. Rejecting the whole class removes
    # any need to sniff a marker out of a form it does not understand — with or
    # without whitespace before the separator.
    source = tmp_path / "requirements.in"
    source.write_text(f"{line}\n", encoding="utf-8")

    conflicts = _declaration_conflicts(source, _declared(source))
    assert len(conflicts) == 1
    assert "not a PEP 508 requirement" in conflicts[0]


def test_direct_references_are_rejected_and_classified_correctly(tmp_path: Path) -> None:
    # `_source_pin_sets()` records only `==` pins, so a URL or revision change in a
    # direct reference would pass `_stale_lock_pins()` as resolver-chosen. Reject it.
    manifest = tmp_path / "requirements.in"

    conflicts = _declaration_conflicts(manifest, ["demo @ file:///tmp/demo;v1"])
    assert len(conflicts) == 1
    # PEP 508 lets the URL itself contain `;`, so this is reported as a direct
    # reference rather than misclassified as marker-qualified.
    assert "direct references are not comparable" in conflicts[0]

    marked = 'demo @ file:///tmp/demo;v1 ; sys_platform == "win32"'
    marked_conflicts = _declaration_conflicts(manifest, [marked])
    assert len(marked_conflicts) == 1
    # Still classified by what it is, not by the marker it also carries.
    assert "direct references are not comparable" in marked_conflicts[0]

    assert _declaration_conflicts(manifest, ["demo==1.0"]) == []


def test_optional_dependencies_of_both_manifests_are_inventoried(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'x'\nversion = '0'\ndependencies = []\n"
        "[project.optional-dependencies]\n"
        'test = ["httpx==0.28.1", "tomli==2.0.1 ; python_version < \'3.12\'"]\n',
        encoding="utf-8",
    )

    declarations = _optional_dependency_declarations(pyproject)
    assert len(_declaration_conflicts(pyproject, declarations)) == 1

    inventory = _direct_declarations()
    operator = REPOSITORY / "operator-app" / "pyproject.toml"
    assert set(_optional_dependency_declarations(operator)) <= set(inventory[operator])
    root = REPOSITORY / "pyproject.toml"
    assert set(_optional_dependency_declarations(root)) <= set(inventory[root])


def test_named_editable_direct_reference_is_rejected(tmp_path: Path) -> None:
    # The target parses as a valid PEP 508 reference, so the editable flag must be
    # kept: an editable is never comparable to a lock, whatever it points at.
    source = tmp_path / "requirements.in"
    source.write_text("-e demo @ file:///tmp/demo\n", encoding="utf-8")

    conflicts = _declaration_conflicts(source, _declared(source))
    assert len(conflicts) == 1
    assert "not a PEP 508 requirement" in conflicts[0]


@pytest.mark.parametrize("declaration", ["foo == 1.0", "foo=== 1.0"])
def test_exact_pins_the_staleness_parser_cannot_record_are_rejected(
    tmp_path: Path, declaration: str
) -> None:
    # `Requirement` accepts these and uv treats them as exact, but `PIN_RE` does
    # not, so `_source_pin_sets()` records nothing while the name still counts as
    # declared — the lock would be treated as resolver-chosen and never checked.
    conflicts = _declaration_conflicts(tmp_path / "requirements.in", [declaration])
    assert len(conflicts) == 1
    assert "spell an exact pin" in conflicts[0]

    assert _declaration_conflicts(tmp_path / "requirements.in", ["foo==1.0"]) == []


@pytest.mark.parametrize(
    ("argument", "reason"),
    [
        ("/dev/zero", "not a requirements file"),
        ("/dev/zero.in", "escapes"),
        ("../outside.in", "escapes"),
        ("missing.in", "not a regular file"),
        ("pyproject.toml", "not a requirements file"),
    ],
)
def test_compile_header_inputs_are_held_to_the_include_contract(
    tmp_path: Path, argument: str, reason: str
) -> None:
    # `_header_command()` validates only the output path, so a header could point
    # the gate at a device node before any include safeguard runs.
    command = ["uv", "pip", "compile", argument, "-o", "requirements.txt"]
    with pytest.raises(SyncError, match=reason):
        _source_inputs(command, tmp_path)


def test_wildcard_equality_is_treated_as_a_range(tmp_path: Path) -> None:
    # No lock ever contains the literal `1.*`, so recording it as a pin would make
    # the staleness check permanently unsatisfiable: sync would succeed and the
    # check immediately after it would still call the fresh lock stale.
    manifest = tmp_path / "requirements.in"
    assert _declaration_conflicts(manifest, ["foo==1.*", "foo==1.0.*"]) == []

    lock = _write_lock_pair(
        tmp_path, "foo==1.*\n", "foo==1.9 \\\n    --hash=sha256:0\n    # via -r requirements.in\n"
    )
    assert _stale_lock_pins(lock) is False
    assert _shared_pin_conflicts(["foo==1.*"], lock) == []

    # A resolved version outside the wildcard is still a genuine conflict.
    assert _shared_pin_conflicts(["foo==2.*"], lock) != []


@pytest.mark.parametrize("declaration", ["foo==2.*", "foo>=2", "foo<1.5"])
def test_ranged_source_declarations_are_checked_against_the_lock(
    tmp_path: Path, declaration: str
) -> None:
    # A range has no pin tuple to compare, so without an explicit specifier check
    # `_stale_lock_pins()` waves the locked version through as resolver-chosen.
    lock = _write_lock_pair(
        tmp_path, "foo==1.*\n", "foo==1.9 \\\n    --hash=sha256:0\n    # via -r requirements.in\n"
    )
    assert _stale_lock_pins(lock) is False

    (tmp_path / "requirements.in").write_text(f"{declaration}\n", encoding="utf-8")
    assert _stale_lock_pins(lock) is True


@pytest.mark.parametrize("spelling", ["demo==v1.0", "demo==01.0", "demo==1.0.0", "demo==1.0"])
def test_exact_pins_converge_under_pep_440_equivalence(tmp_path: Path, spelling: str) -> None:
    # uv writes the distribution's metadata version, which need not match the
    # input's spelling. Comparing raw text would leave the pin permanently stale:
    # sync succeeds, and the check immediately after it fails.
    lock = _write_lock_pair(
        tmp_path,
        f"{spelling}\n",
        "demo==1.0 \\\n    --hash=sha256:0\n    # via -r requirements.in\n",
    )
    assert _stale_lock_pins(lock) is False
    assert _manifest_pin_conflicts([spelling], lock) == []

    # A genuinely different release is still caught.
    (tmp_path / "requirements.in").write_text("demo==1.1\n", encoding="utf-8")
    assert _stale_lock_pins(lock) is True


def test_pep_440_equivalence_does_not_rewrite_diagnostics_or_policy(tmp_path: Path) -> None:
    # Versions are compared per PEP 440 but reported and synchronized verbatim,
    # so the policy rebuild still reproduces committed text byte for byte.
    lock = _write_lock_pair(tmp_path, "demo==83.0.0\n", "demo==83.0.0\n")
    assert _pinned_version_sets(lock) == {"demo": {"83.0.0"}}

    conflicts = _manifest_pin_conflicts(["demo==83.1"], lock)
    assert len(conflicts) == 1
    assert "83.0.0" in conflicts[0]


@pytest.mark.parametrize("declaration", ["foo==1.0,>=1", "foo==1.0, >=1", "foo>=1,==1.0"])
def test_compound_specifiers_are_ranges_not_pins(tmp_path: Path, declaration: str) -> None:
    # `PIN_RE` is greedy enough to capture `1.0,>=1` as a version, and drops a
    # clause that follows whitespace. Either way the recorded pin matches no lock
    # entry, so the input could never converge.
    assert _exact_pin(declaration) is None

    lock = _write_lock_pair(
        tmp_path,
        f"{declaration}\n",
        "foo==1.0 \\\n    --hash=sha256:0\n    # via -r requirements.in\n",
    )
    assert _stale_lock_pins(lock) is False

    # Routed through the range comparison, so a lock outside the range is caught.
    (tmp_path / "requirements.txt").write_text(
        (tmp_path / "requirements.txt").read_text(encoding="utf-8").replace("foo==1.0", "foo==0.5"),
        encoding="utf-8",
    )
    assert _stale_lock_pins(lock) is True


@pytest.mark.parametrize("directive", ["-r", "  -r  ", "--requirement="])
def test_include_directives_without_a_target_are_rejected(tmp_path: Path, directive: str) -> None:
    source = tmp_path / "requirements.in"
    source.write_text(f"{directive}\n", encoding="utf-8")

    with pytest.raises(SyncError, match="names no file"):
        _source_declarations(source)


@pytest.mark.parametrize("directive", ["-e", "--editable="])
def test_editable_directives_without_a_target_are_unsupported(
    tmp_path: Path, directive: str
) -> None:
    # Dropped as a generic option before, so `check-deps` could pass while
    # `sync-deps` went on to hand uv an input it refuses.
    source = tmp_path / "requirements.in"
    source.write_text(f"{directive}\n", encoding="utf-8")

    conflicts = _declaration_conflicts(source, _declared(source))
    assert len(conflicts) == 1
    assert "not a PEP 508 requirement" in conflicts[0]


@pytest.mark.parametrize("declaration", ["demo [x]>=1", "demo(>=1)", "foo [extra]==1.0"])
def test_spellings_the_two_parsers_read_differently_are_rejected(
    tmp_path: Path, declaration: str
) -> None:
    # `packaging` accepts these and uv compiles them, but `_range_conflict()`
    # replays the raw text with the regex parser, which reads different extras or
    # a specifier it cannot parse at all. Disagreement is rejected rather than
    # allowed to crash or to compare the wrong thing.
    conflicts = _declaration_conflicts(tmp_path / "requirements.in", [declaration])
    assert len(conflicts) == 1
    assert "spell the requirement" in conflicts[0]

    assert _declaration_conflicts(tmp_path / "requirements.in", ["demo[x]>=1"]) == []


def test_nested_include_may_reach_a_shared_parent_file(tmp_path: Path) -> None:
    # uv follows `-r ../common.in`; containment is anchored at the top-level
    # input's directory, not the including file's, so a shared parent-level file
    # stays reachable while an escape from the tree does not.
    (tmp_path / "sub").mkdir()
    outer = tmp_path / "sub" / "requirements.in"
    outer.write_text("-r ../common.in\n", encoding="utf-8")
    (tmp_path / "common.in").write_text("foo==1.0\n", encoding="utf-8")

    top = tmp_path / "requirements.in"
    top.write_text("-r sub/requirements.in\n", encoding="utf-8")
    assert _declared(top) == ["foo==1.0"]

    (tmp_path.parent / "escaped.in").write_text("foo==1.0\n", encoding="utf-8")
    outer.write_text("-r ../../escaped.in\n", encoding="utf-8")
    with pytest.raises(SyncError, match="escapes"):
        _source_declarations(top)


@pytest.mark.parametrize("directive", ["-rinner.in", "--requirementinner.in"])
def test_include_directives_must_use_a_separator(tmp_path: Path, directive: str) -> None:
    # uv: "Expected '=' or whitespace". Matching the directive anyway keeps the
    # attached form an actionable error instead of a silently skipped option.
    source = tmp_path / "requirements.in"
    source.write_text(f"{directive}\n", encoding="utf-8")
    (tmp_path / "inner.in").write_text("foo==1.0\n", encoding="utf-8")

    with pytest.raises(SyncError, match="needs a separator"):
        _source_declarations(source)


@pytest.mark.parametrize("declaration", ["foo[x,x]==1.0", "foo[X,x]==1.0", "foo[b,a]==1.0"])
def test_extras_are_deduplicated_like_the_lock(tmp_path: Path, declaration: str) -> None:
    # uv canonicalizes extras in the lock, so a repeated or differently cased
    # spelling would leave the pin unmatchable: sync succeeds, check calls the
    # fresh lock stale.
    assert _declaration_conflicts(tmp_path / "requirements.in", [declaration]) == []

    pin = _exact_pin(declaration)
    assert pin is not None
    assert pin.extras in ("[x]", "[a,b]")


def test_unmodelled_compile_options_are_rejected(tmp_path: Path) -> None:
    # `--override overrides.txt` would leave its value looking like an input, so
    # the whole declarations set could be compared against the wrong file.
    (tmp_path / "requirements.in").write_text("foo==1.0\n", encoding="utf-8")
    (tmp_path / "overrides.txt").write_text("foo==2.0\n", encoding="utf-8")

    base = ["uv", "pip", "compile", "requirements.in"]
    assert _source_inputs([*base, "-o", "requirements.txt"], tmp_path) == [
        (tmp_path / "requirements.in").resolve()
    ]
    with pytest.raises(SyncError, match="unmodeled uv compile option"):
        _source_inputs([*base, "--override", "overrides.txt"], tmp_path)

    # `--flag=value` must not be mistaken for a flag that swallows the next token,
    # so a further input has to follow it for the case to mean anything.
    (tmp_path / "extra.in").write_text("bar==1.0\n", encoding="utf-8")
    assert _source_inputs([*base, "--output-file=requirements.txt", "extra.in"], tmp_path) == [
        (tmp_path / "requirements.in").resolve(),
        (tmp_path / "extra.in").resolve(),
    ]


def test_malformed_dependency_group_members_are_rejected(tmp_path: Path) -> None:
    # PEP 735 defines exactly two member forms; anything else is input uv rejects,
    # so the gate must not pass it through to `_compile()`.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[dependency-groups]\ndev = [{ unknown-key = 'x' }]\n", encoding="utf-8")
    with pytest.raises(SyncError, match="neither a requirement nor an include-group"):
        _dependency_group_declarations(pyproject)

    pyproject.write_text(
        "[dependency-groups]\ndev = ['mypy>=1.17,<2', { include-group = 'test' }]\n",
        encoding="utf-8",
    )
    assert _dependency_group_declarations(pyproject) == ["mypy>=1.17,<2"]


@pytest.mark.parametrize("value", ["1", "['test']", "{ a = 'b' }"])
def test_include_group_values_must_be_strings(tmp_path: Path, value: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f"[dependency-groups]\ndev = [{{ include-group = {value} }}]\n", encoding="utf-8"
    )
    with pytest.raises(SyncError, match="neither a requirement nor an include-group"):
        _dependency_group_declarations(pyproject)


@pytest.mark.parametrize("option", ["--override overrides.txt", "--no-index", "-c c.txt"])
def test_unmodelled_options_in_requirements_inputs_are_rejected(
    tmp_path: Path, option: str
) -> None:
    # Held to the same standard as a lock header: an option that changes what uv
    # resolves must not be dropped, or the gate audits a different input set.
    source = tmp_path / "requirements.in"
    source.write_text(f"foo==1.0\n{option}\n", encoding="utf-8")

    with pytest.raises(SyncError, match="unmodeled option"):
        _source_declarations(source)


@pytest.mark.parametrize(
    "body",
    [
        'foo==1.0 \\\n; python_version >= "3.11"\n',  # marker split
        "foo >=\\\n  25\n",  # operator split
        "foo==25.\\\n4.0\n",  # version split
        "-r \\\ninner.in\n",  # include split
        "foo==1\\\n",  # dangling at end of file
        "\\\n",  # a lone backslash, which continues nothing
    ],
)
def test_line_continuations_are_rejected(tmp_path: Path, body: str) -> None:
    # Verified against `uv pip compile`: it rejects a backslash continuation
    # everywhere in a requirements input, at every one of these positions.
    # Joining them would assemble a declaration uv never sees, and certify a
    # source `make sync-deps` cannot regenerate.
    source = tmp_path / "requirements.in"
    source.write_text(body, encoding="utf-8")
    (tmp_path / "inner.in").write_text("foo==1.0\n", encoding="utf-8")

    with pytest.raises(SyncError, match="line continuation"):
        _source_declarations(source)


def test_backslash_before_trailing_whitespace_is_not_a_continuation(tmp_path: Path) -> None:
    # The backslash is not last, so this is not a continuation. It leaves a
    # declaration uv cannot parse either, which fails closed on its own.
    source = tmp_path / "requirements.in"
    source.write_text("packaging==\\   \n26.3\n", encoding="utf-8")

    conflicts = _declaration_conflicts(source, _declared(source))
    assert any("not a PEP 508 requirement" in conflict for conflict in conflicts)


@pytest.mark.parametrize(
    "target",
    [
        '"inner".in',  # uv strips quotes anywhere in the value
        '"inner file".in',
        "inner\\ file.in",  # backslash escapes a space outside quotes
        "inner#comment",  # uv cuts at the fragment
        "${REQ_FILE}",  # uv expands the braced form from the environment
        "$REQ_FILE",  # literal to uv, but not worth telling apart
        "inner.in extra",  # the whole remainder is one filename
        "= inner.in",
    ],
)
def test_include_targets_must_be_plain_relative_paths(tmp_path: Path, target: str) -> None:
    # Each of these means something specific to uv, and each is a way for the
    # gate to open a different file than uv compiles. Rather than reimplement
    # that language, the whole class is refused with one message.
    source = tmp_path / "requirements.in"
    source.write_text(f"-r {target}\n", encoding="utf-8")

    with pytest.raises(SyncError, match="plain relative path"):
        _source_declarations(source)


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("/dev/zero", "plain relative path"),
        ("../outside.in", "escapes"),
        ("subdir", "not a regular file"),
        ("missing.in", "not a regular file"),
    ],
)
def test_unsafe_includes_are_rejected(tmp_path: Path, target: str, reason: str) -> None:
    (tmp_path.parent / "outside.in").write_text("foo==1.0\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "requirements.in"
    source.write_text(f"-r {target}\n", encoding="utf-8")
    (nested / "subdir").mkdir()

    with pytest.raises(SyncError, match=reason):
        _source_declarations(source)


def test_plain_include_paths_are_followed(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    source = tmp_path / "requirements.in"
    source.write_text("-r sub/inner.in\n", encoding="utf-8")
    (tmp_path / "sub" / "inner.in").write_text("foo==1.0\n", encoding="utf-8")

    assert _declared(source) == ["foo==1.0"]
