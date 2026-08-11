# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

_IMPORT_POLICY_PATH = REPOSITORY_ROOT / "scripts" / "installed_examples.py"
if not _IMPORT_POLICY_PATH.is_file():
    _IMPORT_POLICY_PATH = REPOSITORY_ROOT / "installed_examples.py"
_IMPORT_POLICY_SPEC = importlib.util.spec_from_file_location(
    "_installed_examples_import_policy",
    _IMPORT_POLICY_PATH,
)
if _IMPORT_POLICY_SPEC is None or _IMPORT_POLICY_SPEC.loader is None:
    raise RuntimeError("installed example import policy could not be loaded")
_IMPORT_POLICY_MODULE = importlib.util.module_from_spec(_IMPORT_POLICY_SPEC)
_IMPORT_POLICY_SPEC.loader.exec_module(_IMPORT_POLICY_MODULE)
_public_import_violation = cast(
    Callable[[ast.AST], str | None],
    _IMPORT_POLICY_MODULE.__dict__["_public_import_violation"],
)

_MANIFEST_NAME = cast(str, _IMPORT_POLICY_MODULE.__dict__["MANIFEST_NAME"])
_MANIFEST_PATH = REPOSITORY_ROOT / "examples" / "manifest.json"
if not _MANIFEST_PATH.is_file():
    # The release verifier stages the committed manifest at the staging root.
    _MANIFEST_PATH = REPOSITORY_ROOT / _MANIFEST_NAME
_MANIFEST: Any = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

# Derived, never hand-listed: the committed manifest is the declared inventory, so an
# example added there cannot escape the gates below by being omitted from a tuple.
MANIFEST_EXAMPLES: tuple[str, ...] = tuple(
    PurePosixPath(entry["source_path"]).name for entry in _MANIFEST["examples"]
)
if not MANIFEST_EXAMPLES:
    raise RuntimeError("committed example manifest declares no runnable examples")


def _assert_only_public_sdk_imports(tree: ast.AST) -> None:
    sdk_imports = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules = (node.module,)
        else:
            continue
        sdk_imports = sdk_imports or any(
            module == package or module.startswith(f"{package}.")
            for module in modules
            for package in ("picogrid_ecn_client", "picogrid_edge_sdk")
        )
    assert sdk_imports
    assert _public_import_violation(tree) is None


@pytest.fixture
def assert_only_public_sdk_imports() -> Callable[[ast.AST], None]:
    return _assert_only_public_sdk_imports


@pytest.fixture
def manifest_examples() -> tuple[str, ...]:
    return MANIFEST_EXAMPLES
