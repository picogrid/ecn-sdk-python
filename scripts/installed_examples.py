# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Execute every staged example's deterministic check mode with the installed wheel."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "example-manifest.json"
SUPPORT_FILES = ("__init__.py", "_common.py")
_EXAMPLE_NAME = re.compile(r"[a-z][a-z0-9_]*\.py")
_PUBLIC_PACKAGE = "picogrid_ecn_client"
_PERMITTED_PUBLIC_MODULES = frozenset(
    {
        _PUBLIC_PACKAGE,
        f"{_PUBLIC_PACKAGE}.workflows",
    }
)
_PUBLIC_SUBPACKAGE_IMPORTS = {
    _PUBLIC_PACKAGE: frozenset({"workflows"}),
}


def _public_exports(module_name: str) -> frozenset[str] | None:
    """Resolve a module's declared exports, failing closed when unavailable."""
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    exports = vars(module).get("__all__")
    if not isinstance(exports, (list, tuple)) or not all(isinstance(name, str) for name in exports):
        return None
    return frozenset(exports)


def _public_import_violation(tree: ast.AST) -> str | None:
    """Return why an SDK import is non-public, or ``None`` when all are public."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
            imported_names: tuple[str, ...] = ()
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules = (node.module,)
            imported_names = tuple(alias.name for alias in node.names)
        else:
            continue
        imports_public_sdk = False

        for module in modules:
            if (
                module.startswith("picogrid_")
                and module != _PUBLIC_PACKAGE
                and not module.startswith(f"{_PUBLIC_PACKAGE}.")
            ):
                return "non-public client module"
            if module != _PUBLIC_PACKAGE and not module.startswith(f"{_PUBLIC_PACKAGE}."):
                continue
            imports_public_sdk = True
            if (
                any(part.startswith("_") for part in module.split(".")[1:])
                or module not in _PERMITTED_PUBLIC_MODULES
            ):
                return "non-public client module"
        if imports_public_sdk and imported_names:
            module = modules[0]
            exports = _public_exports(module)
            allowed_subpackages = _PUBLIC_SUBPACKAGE_IMPORTS.get(module, ())
            if exports is None or any(
                name.startswith("_") or (name not in exports and name not in allowed_subpackages)
                for name in imported_names
            ):
                return "non-public client name"
    return None


def _load_example_manifest(staging: Path) -> tuple[tuple[str, str, str], ...]:
    value: Any = json.loads((staging / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("installed example manifest must use schema_version 1")
    entries = value.get("examples")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("installed example manifest has an invalid public inventory")
    declared: list[tuple[str, str, str]] = []
    identifiers: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("installed example manifest contains an invalid entry")
        identifier = entry.get("id")
        source_path = entry.get("source_path")
        workflow = entry.get("workflow")
        if not isinstance(identifier, str) or not isinstance(source_path, str):
            raise RuntimeError("installed example manifest contains an invalid source path")
        relative = PurePosixPath(source_path)
        if (
            len(relative.parts) != 2
            or relative.parts[0] != "examples"
            or source_path != relative.as_posix()
            or _EXAMPLE_NAME.fullmatch(relative.name) is None
            or identifier != relative.stem.replace("_", "-")
        ):
            raise RuntimeError("installed example manifest contains an invalid source path")
        if not isinstance(workflow, dict):
            raise RuntimeError("installed example manifest contains invalid workflow metadata")
        module_name = workflow.get("module")
        function_name = workflow.get("function")
        if (
            not isinstance(module_name, str)
            or not module_name.startswith(f"{_PUBLIC_PACKAGE}.workflows.")
            or not isinstance(function_name, str)
            or not function_name
        ):
            raise RuntimeError("installed example manifest contains invalid workflow metadata")
        identifiers.append(identifier)
        declared.append((relative.name, module_name, function_name))
    if identifiers != sorted(set(identifiers)):
        raise RuntimeError("installed example manifest contains duplicate or unsorted examples")
    return tuple(declared)


def _validate_public_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    if violation := _public_import_violation(tree):
        raise RuntimeError(f"{path.name} imports a {violation}")


def _installed_package_root() -> Path:
    spec = importlib.util.find_spec(_PUBLIC_PACKAGE)
    if spec is None or spec.origin is None:
        raise RuntimeError("installed public package could not be resolved")
    origin = Path(spec.origin).resolve()
    site_roots = {
        Path(value).resolve()
        for key in ("purelib", "platlib")
        if (value := sysconfig.get_path(key)) is not None
    }
    if not any(origin.is_relative_to(root) for root in site_roots):
        raise RuntimeError("public package did not resolve from the isolated installation")
    return origin.parent


def main() -> int:
    staging = Path(__file__).resolve().parent
    examples_directory = staging / "examples"
    declared = _load_example_manifest(staging)
    examples = tuple(name for name, _module, _function in declared)
    expected_files = {*examples, *SUPPORT_FILES}
    actual_files = {path.name for path in examples_directory.glob("*.py")}
    if actual_files != expected_files:
        raise RuntimeError("staged example inventory differs from its exact manifest")
    shadow_paths = (
        examples_directory / _PUBLIC_PACKAGE,
        examples_directory / f"{_PUBLIC_PACKAGE}.py",
    )
    if any(path.exists() for path in shadow_paths):
        raise RuntimeError("staged examples contain a package shadow path")
    package_root = _installed_package_root()
    for name in (*SUPPORT_FILES, *examples):
        _validate_public_imports(examples_directory / name)
    workflows = importlib.import_module(f"{_PUBLIC_PACKAGE}.workflows")
    exported = getattr(workflows, "__all__", ())
    for _name, module_name, function_name in declared:
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            raise RuntimeError(f"installed workflow module has no source: {module_name}")
        source = Path(module_file).resolve()
        # Identity, not just presence: a manifest that routes an exported name to a
        # different module carrying a same-named callable must not pass this gate.
        routed = getattr(module, function_name, None)
        if (
            not source.is_file()
            or not source.is_relative_to(package_root / "workflows")
            or function_name not in exported
            or not callable(routed)
            or getattr(workflows, function_name, None) is not routed
        ):
            raise RuntimeError(f"installed workflow is not public: {module_name}.{function_name}")

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    for name in examples:
        path = examples_directory / name
        bootstrap = (
            "import runpy, sys; "
            f"sys.path.insert(0, {str(examples_directory)!r}); "
            f"sys.argv = [{str(path)!r}, '--check']; "
            f"runpy.run_path({str(path)!r}, run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", bootstrap],
            cwd=examples_directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{name} check failed with {result.returncode}: {result.stderr.strip()[:500]}"
            )
        output = result.stdout.strip()
        if "\n" in output or not output.endswith(": offline check passed"):
            raise RuntimeError(f"{name} returned an unexpected check result")
    print(f"{len(examples)} installed-wheel examples passed offline checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
