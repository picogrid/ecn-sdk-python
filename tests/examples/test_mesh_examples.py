# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MESH_EXAMPLES = (
    "observe_mesh_data.py",
    "dispatch_mesh_task.py",
    "receive_mesh_task.py",
)


def _tree(filename: str) -> ast.Module:
    return ast.parse((ROOT / "examples" / filename).read_text())


def test_mesh_examples_import_only_public_sdk_modules(
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
) -> None:
    for filename in MESH_EXAMPLES:
        assert_only_public_sdk_imports(_tree(filename))


def test_mesh_examples_are_declared_in_the_committed_manifest(
    manifest_examples: tuple[str, ...],
) -> None:
    assert set(MESH_EXAMPLES) <= set(manifest_examples)


def test_mesh_example_import_check_rejects_plain_private_import(
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
) -> None:
    tree = ast.parse("import picogrid_ecn_client._internal")

    with pytest.raises(AssertionError):
        assert_only_public_sdk_imports(tree)


def test_mesh_examples_delegate_to_public_workflows() -> None:
    expected_workflows = {
        "observe_mesh_data.py": "observe_mesh_data",
        "dispatch_mesh_task.py": "dispatch_mesh_task",
        "receive_mesh_task.py": "receive_mesh_task",
    }
    for filename, workflow_name in expected_workflows.items():
        tree = _tree(filename)
        workflow_imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "picogrid_ecn_client.workflows"
            for alias in node.names
        }
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert workflow_name in workflow_imports
        assert workflow_name in called_names
