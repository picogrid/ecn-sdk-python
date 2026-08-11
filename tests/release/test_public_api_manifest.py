# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Release checks for the reviewed public API manifest (FDE-694).

The manifest at ``scripts/public-api-manifest.json`` is the authoritative,
reviewed inventory of the supported public Python surface and the generated
reference page contract. These checks prove:

* every supported export is accounted for exactly once;
* every excluded namespace stays excluded (negative assertions); and
* the page contract stays deterministic and environment-free.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import json
import re
import textwrap
import types
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY / "scripts" / "public-api-manifest.json"
PACKAGE_INIT = REPOSITORY / "src" / "picogrid_ecn_client" / "__init__.py"
TESTING_INIT = REPOSITORY / "src" / "picogrid_ecn_client" / "testing" / "__init__.py"
WORKFLOWS_INIT = REPOSITORY / "src" / "picogrid_ecn_client" / "workflows" / "__init__.py"
CHANGELOG = REPOSITORY / "CHANGELOG.md"

# The one deliberately shared page: the three synthetic token constants.
SHARED_ROUTES = {"reference/python/testing/synthetic-tokens"}
_HTTPS = "https" + "://"


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _symbols(manifest: dict[str, object]) -> list[dict[str, object]]:
    symbols = manifest["symbols"]
    assert isinstance(symbols, list)
    return symbols


def _testing_symbols(manifest: dict[str, object]) -> list[dict[str, object]]:
    symbols = manifest["testing_symbols"]
    assert isinstance(symbols, list)
    return symbols


def _workflow_symbols(manifest: dict[str, object]) -> list[dict[str, object]]:
    symbols = manifest["workflow_symbols"]
    assert isinstance(symbols, list)
    return symbols


def _declared_all(source: Path) -> set[str]:
    text = source.read_text(encoding="utf-8")
    match = re.search(r"__all__\s*(?::[^=]+)?=\s*\[(.*?)\]", text, re.DOTALL)
    assert match is not None, f"{source} must declare __all__"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _assert_workflow_namespace_bindings(manifest: dict[str, object]) -> None:
    workflows = import_module("picogrid_ecn_client.workflows")
    for symbol in _workflow_symbols(manifest):
        name = str(symbol["name"])
        module = import_module(str(symbol["module"]))
        assert getattr(workflows, name, None) is getattr(module, name), (
            f"workflow namespace binding {name} does not resolve to its manifest target"
        )


def test_every_top_level_export_is_accounted_exactly_once() -> None:
    manifest = _manifest()
    names = [str(symbol["name"]) for symbol in _symbols(manifest)]
    assert len(names) == len(set(names)), "duplicate symbol entries in manifest"

    version_attribute = manifest["version_attribute"]
    assert isinstance(version_attribute, dict)
    accounted = set(names) | {str(version_attribute["name"])}

    exported = _declared_all(PACKAGE_INIT)
    missing = exported - accounted
    extra = accounted - exported
    assert not missing, f"exported names missing from the manifest: {sorted(missing)}"
    assert not extra, f"manifest names not exported by the package: {sorted(extra)}"


def test_testing_surface_is_accounted_exactly_once() -> None:
    manifest = _manifest()
    names = [str(symbol["name"]) for symbol in _testing_symbols(manifest)]
    assert len(names) == len(set(names))
    exported = _declared_all(TESTING_INIT)
    assert set(names) == exported, (
        f"testing manifest {sorted(names)} != testing exports {sorted(exported)}"
    )


def test_workflows_surface_is_accounted_exactly_once() -> None:
    manifest = _manifest()
    names = [str(symbol["name"]) for symbol in _workflow_symbols(manifest)]
    assert len(names) == len(set(names))
    exported = _declared_all(WORKFLOWS_INIT)
    assert set(names) == exported, (
        f"workflow manifest {sorted(names)} != workflow exports {sorted(exported)}"
    )
    _assert_workflow_namespace_bindings(manifest)


def test_workflow_namespace_binding_rejects_wrong_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = import_module("picogrid_ecn_client.workflows")
    monkeypatch.setattr(workflows, "preflight", object())
    manifest: dict[str, object] = {
        "workflow_symbols": [
            {
                "name": "preflight",
                "module": "picogrid_ecn_client.workflows.diagnostics",
            }
        ]
    }

    with pytest.raises(AssertionError, match="does not resolve"):
        _assert_workflow_namespace_bindings(manifest)


def test_changelog_workflow_export_count_matches_manifest() -> None:
    match = re.search(
        r"workflows` subpackage with (?P<count>\d+) typed reusable",
        CHANGELOG.read_text(encoding="utf-8"),
    )
    assert match is not None, "changelog must document the workflow export count"
    assert int(match.group("count")) == len(_workflow_symbols(_manifest()))


def test_symbols_never_come_from_excluded_modules() -> None:
    manifest = _manifest()
    excluded = manifest["excluded"]
    assert isinstance(excluded, dict)
    excluded_modules = set(map(str, excluded["modules"]))
    for symbol in (*_symbols(manifest), *_testing_symbols(manifest), *_workflow_symbols(manifest)):
        module = str(symbol["module"])
        assert module not in excluded_modules, f"{symbol['name']} defined in excluded {module}"
        for prefix in excluded_modules:
            assert not module.startswith(f"{prefix}."), (
                f"{symbol['name']} defined below excluded {prefix}"
            )
        assert "._" not in f".{module}", (
            f"{symbol['name']} module {module} crosses a private boundary"
        )


def test_private_and_unsupported_surfaces_have_negative_assertions() -> None:
    manifest = _manifest()
    excluded = manifest["excluded"]
    assert isinstance(excluded, dict)
    excluded_modules = set(map(str, excluded["modules"]))

    # Every private module that exists in the source tree must be excluded.
    package_root = REPOSITORY / "src" / "picogrid_ecn_client"
    private_modules = set()
    for path in package_root.rglob("*"):
        if "__pycache__" in path.parts or "egg-info" in path.name:
            continue
        parts = path.relative_to(package_root).parts
        private_index = next(
            (index for index, part in enumerate(parts) if part.startswith("_")), None
        )
        if private_index is None:
            continue
        head = parts[: private_index + 1]
        module = ".".join(("picogrid_ecn_client", *head)).removesuffix(".py")
        if module.endswith(("__init__", "__main__")):
            continue
        private_modules.add(module)
    missing = {
        module
        for module in private_modules
        if module not in excluded_modules
        and not any(module.startswith(f"{prefix}.") for prefix in excluded_modules)
    }
    assert not missing, f"private modules without a negative assertion: {sorted(missing)}"

    surfaces = " ".join(map(str, excluded["surfaces"])).lower()
    for required in (
        "transport",
        "raw mqtt topics",
        "protobuf classes",
        "rest, cloud, or legion",
        "server-wide search",
        "routing administration",
        "mock",
    ):
        assert required in surfaces, f"missing negative surface assertion: {required}"


def test_mock_broker_callbacks_stay_excluded() -> None:
    manifest = _manifest()
    excluded = manifest["excluded"]
    assert isinstance(excluded, dict)
    members = excluded["members"]
    assert isinstance(members, dict)
    mock_excluded = set(map(str, members["MockECN"]["names"]))
    assert {
        "mqtt_authenticate",
        "mqtt_can_subscribe",
        "mqtt_can_publish",
        "mqtt_published",
        "mqtt_connected",
        "mqtt_disconnected",
        "mqtt_forwarded",
    } <= mock_excluded

    for symbol in _testing_symbols(manifest):
        if symbol["name"] != "MockECN":
            continue
        approved = set(map(str, symbol["members"]))
        overlap = approved & mock_excluded
        assert not overlap, f"MockECN members both approved and excluded: {sorted(overlap)}"


def test_routes_are_unique_deterministic_and_prefixed() -> None:
    manifest = _manifest()
    prefix = str(manifest["reference_route_prefix"])
    routes: list[str] = []
    for symbol in (*_symbols(manifest), *_testing_symbols(manifest), *_workflow_symbols(manifest)):
        route = str(symbol["route"])
        assert route.startswith(f"{prefix}/"), route
        assert re.fullmatch(r"[a-z0-9/-]+", route), f"non-deterministic route: {route}"
        routes.append(route)
    duplicated = {route for route in routes if routes.count(route) > 1}
    assert duplicated <= SHARED_ROUTES, f"unexpected shared routes: {sorted(duplicated)}"

    group_ids = {str(group["id"]) for group in manifest["groups"]}
    for symbol in (*_symbols(manifest), *_testing_symbols(manifest), *_workflow_symbols(manifest)):
        assert str(symbol["group"]) in group_ids, f"unknown group for {symbol['name']}"


def test_manifest_is_environment_free() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    for forbidden in ("/Users/", "/home/", "/tmp/", "C:\\\\", "http://", "@"):
        assert forbidden not in text, f"forbidden fragment in manifest: {forbidden}"
    parsed = json.loads(text)
    # Built at runtime so the literal pattern is not mistaken for a malformed
    # URL by the release secret/address scanner.
    hostnames = re.findall(_HTTPS + r"([a-z0-9.-]+)/", text)
    assert set(hostnames) <= {"github.com"}, f"unexpected hostnames: {sorted(set(hostnames))}"
    assert parsed["source_link"]["template"].startswith(
        "https://github.com/picogrid/ecn-sdk-python/"
    )


def test_related_guides_reference_existing_curated_pages() -> None:
    manifest = _manifest()
    for group in manifest["groups"]:
        for route in group["related_guides"]:
            page = REPOSITORY / "docs" / f"{route}.md"
            index_page = REPOSITORY / "docs" / route / "index.md"
            assert page.exists() or index_page.exists(), (
                f"related guide {route} for group {group['id']} does not exist"
            )


# Dunders that are Python object machinery rather than a consumer-facing API.
# Detection is inverted on purpose: any dunder a testing class implements that
# is not on this reviewed list must be documented or explicitly excluded, so a
# newly implemented protocol fails the gate instead of slipping through.
# Bases whose surface the manifest deliberately does not enumerate: Enum,
# BaseModel and Exception each contribute a large framework API that is not
# SDK-authored. Only members the SDK itself defines are reconciled.
_FRAMEWORK_BASE_MODULES = (
    "builtins",
    "abc",
    "collections",
    "_collections_abc",
    "contextlib",
    "dataclasses",
    "enum",
    "pydantic",
    "typing",
)


def _is_framework_base(base: type) -> bool:
    """Return whether a base class comes from Python or a vendored framework."""

    module = str(getattr(base, "__module__", ""))
    return any(module == root or module.startswith(f"{root}.") for root in _FRAMEWORK_BASE_MODULES)


_NON_CONSUMER_DUNDERS = frozenset(
    {
        "__annotations__",
        # Python 3.14 deferred-annotation machinery (PEP 649/749).
        "__annotate_func__",
        "__annotations_cache__",
        "__class_getitem__",
        # Dataclass-generated machinery.
        "__dataclass_fields__",
        "__dataclass_params__",
        "__match_args__",
        "__replace__",
        "__dict__",
        "__doc__",
        "__eq__",
        "__firstlineno__",
        "__hash__",
        "__init_subclass__",
        "__module__",
        "__ne__",
        "__orig_bases__",
        "__parameters__",
        "__post_init__",
        "__repr__",
        "__slots__",
        "__static_attributes__",
        "__str__",
        "__subclasshook__",
        "__weakref__",
    }
)


def _redefines(cls: type, member: str) -> bool:
    """Return whether this exact class redefines `member` in its own source.

    Distinct from `_defines_authored`, which answers the same question across
    the SDK bases. Redefinition is what makes an inherited approval stale: the
    subclass is new behavior and must be reviewed where it is written.
    """

    try:
        source = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return False
    for element in ast.parse(source).body:
        if not isinstance(element, ast.ClassDef) or element.name != cls.__name__:
            continue
        for statement in element.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name == member:
                    return True
            elif _assigns_member(statement, member):
                # An assigned implementation is a redefinition, and so is
                # replacing an inherited implementation with something that is
                # not one -- `shared = None` removes behavior. Only rebinding a
                # constant to another constant leaves the documented contract
                # intact, because the base already describes it.
                replacement = vars(cls).get(member)
                inherited = next(
                    (vars(base)[member] for base in cls.__mro__[1:] if member in vars(base)),
                    None,
                )
                return _is_implementation(replacement) or _is_implementation(inherited)
        return False
    return False


def _is_implementation(value: object) -> bool:
    """Return whether an assigned class attribute replaces behavior.

    A descriptor counts even when it is not callable: `shared = property(...)`
    changes what a consumer sees on attribute access just as much as a `def`.
    """

    return callable(value) or hasattr(type(value), "__get__")


def _overrides_before(target: type, base: type, member: str) -> bool:
    """Return whether `member` is redefined between `target` and `base`.

    An approval recorded on `base` only describes `base`'s implementation. Any
    class ahead of it in the MRO that rebinds the member supersedes that
    description, including an unmanifested intermediate the exported class
    merely inherits from.
    """

    for ancestor in target.__mro__:
        if ancestor is base:
            return False
        if _is_framework_base(ancestor):
            continue
        if _redefines(ancestor, member):
            return True
    return False


def _assigns_member(statement: ast.stmt, member: str) -> bool:
    """Return whether a class-body assignment binds `member`."""

    if isinstance(statement, ast.AnnAssign):
        # A bare `member: int` only records an annotation; it binds nothing and
        # leaves the inherited implementation in place.
        return (
            statement.value is not None
            and isinstance(statement.target, ast.Name)
            and statement.target.id == member
        )
    if isinstance(statement, ast.Assign):
        return any(
            isinstance(target, ast.Name) and target.id == member for target in statement.targets
        )
    return False


def _defines_authored(cls: type, member: str) -> bool:
    """Return whether the SDK hand-writes `member` anywhere in this class's bases.

    Read from the source rather than the class dictionary: `dataclass`
    generates a constructor but preserves an explicit one, `typing` injects its
    own onto a Protocol, and `Enum` supplies `__format__`. None of those appear
    as a definition in the SDK's own source, so the source is what separates
    authored surface from framework machinery.
    """

    for base in cls.__mro__:
        if _is_framework_base(base):
            continue
        try:
            source = textwrap.dedent(inspect.getsource(base))
        except (OSError, TypeError):
            continue
        for element in ast.parse(source).body:
            if not isinstance(element, ast.ClassDef):
                continue
            if any(
                isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == member
                for statement in element.body
            ):
                return True
    return False


def _defines_authored_init(cls: type) -> bool:
    """Return whether the SDK hand-writes a constructor for this class."""

    return _defines_authored(cls, "__init__")


def _real_public_members(cls: type) -> set[str]:
    """Return the public members a consumer can reach on a testing class."""

    # Walk the SDK's own bases so inherited public members count, and keep
    # class-valued attributes: a nested helper class is reachable like a method.
    # Framework bases (Enum, BaseModel, Exception) contribute their own large
    # surfaces, which the manifest deliberately does not enumerate.
    bases = [base for base in cls.__mro__ if not _is_framework_base(base)]
    names = {name for base in bases for name in vars(base) if not name.startswith("_")}
    tree = ast.parse(textwrap.dedent("\n\n".join(inspect.getsource(base) for base in bases)))

    def _flatten(target: ast.expr) -> list[ast.expr]:
        # `self.left, self.right = values` nests the attributes inside a Tuple.
        if isinstance(target, (ast.Tuple, ast.List)):
            return [nested for item in target.elts for nested in _flatten(item)]
        if isinstance(target, ast.Starred):
            return _flatten(target.value)
        return [target]

    # A dataclass field declared at class level is a public instance attribute
    # even when `vars(cls)` has no entry, which is the case for required and
    # `default_factory` fields. Only class-body annotations count: a local
    # annotation inside a method is not reachable from an instance.
    declared_fields = {
        statement.target.id
        # Only the parsed classes themselves: a nested or method-local class
        # carries its own annotations that consumers cannot reach here.
        for element in tree.body
        if isinstance(element, ast.ClassDef)
        for statement in element.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and not statement.target.id.startswith("_")
    }
    # Only methods of the parsed classes: a nested or method-local class
    # assigning `self.x` binds its own instances, not the exported class.
    own_methods = [
        statement
        for element in tree.body
        if isinstance(element, ast.ClassDef)
        for statement in element.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def _walk_outside_nested_classes(node: ast.AST) -> list[ast.AST]:
        # A nested class owns its own `self`, so its bodies are pruned entirely
        # rather than skipping only the ClassDef node itself.
        collected: list[ast.AST] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                continue
            collected.append(child)
            collected.extend(_walk_outside_nested_classes(child))
        return collected

    assigned: list[ast.expr] = []
    for method in own_methods:
        for node in _walk_outside_nested_classes(method):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    assigned.extend(_flatten(target))
            elif isinstance(node, ast.AnnAssign):
                assigned.extend(_flatten(node.target))
    instance_attributes = {
        target.attr
        for target in assigned
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
        and not target.attr.startswith("_")
    }
    dunders = {
        name
        for base in bases
        for name in vars(base)
        if name.startswith("__") and name.endswith("__") and name not in _NON_CONSUMER_DUNDERS
    }
    # A hand-written `__repr__` or `__str__` is consumer-visible behavior even
    # though the generated default is machinery, so authored ones always count.
    dunders |= {
        statement.name
        for element in tree.body
        if isinstance(element, ast.ClassDef)
        for statement in element.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name.startswith("__")
        and statement.name.endswith("__")
    }
    return names | instance_attributes | declared_fields | dunders


def _assert_lifecycle_flags(symbol: Mapping[str, Any], target: type) -> set[str]:
    """Check advertised lifecycle protocols against what the class implements.

    Returns the dunders the manifest advertises, so the caller can treat them
    as accounted without listing them as members.
    """

    advertised: set[str] = set()
    for flag, methods in (
        ("async_context_manager", ("__aenter__", "__aexit__")),
        ("async_iterator", ("__aiter__", "__anext__")),
    ):
        claimed = set(methods) if symbol.get(flag) else set()
        implemented = set()
        for method in methods:
            attribute = getattr(target, method, None)
            if method == "__aiter__":
                # `async for` does not await `__aiter__`; a coroutine here
                # yields a coroutine object instead of an async iterator.
                if callable(attribute) and not inspect.iscoroutinefunction(attribute):
                    implemented.add(method)
            elif inspect.iscoroutinefunction(attribute):
                implemented.add(method)
        assert claimed == (implemented if claimed or implemented else set()), (
            f"{symbol['name']} advertises {flag}={bool(claimed)} but implements "
            f"{sorted(implemented)}"
        )
        advertised |= claimed
    return advertised


def test_testing_members_are_approved_or_excluded() -> None:
    """Every reachable testing member is either documented or explicitly excluded."""

    manifest = _manifest()
    excluded_members = manifest["excluded"]["members"]
    testing = import_module("picogrid_ecn_client.testing")
    for symbol in _testing_symbols(manifest):
        name = str(symbol["name"])
        target = getattr(testing, name)
        if not isinstance(target, type):
            continue
        approved = set(map(str, symbol.get("members", ())))
        excluded = set(map(str, excluded_members.get(name, {}).get("names", ())))
        overlap = approved & excluded
        assert not overlap, f"{name} members are both approved and excluded: {sorted(overlap)}"
        advertised = _assert_lifecycle_flags(symbol, target)
        context_manager = advertised
        if not _defines_authored_init(target):
            context_manager = context_manager | {"__init__"}
        unaccounted = _real_public_members(target) - approved - excluded - context_manager
        assert not unaccounted, (
            f"{name} exposes members that are neither documented nor excluded: "
            f"{sorted(unaccounted)}"
        )


def test_testing_module_exposes_no_incidental_globals() -> None:
    """The testing package exposes its approved exports and no stray imports."""

    manifest = _manifest()
    approved = {str(symbol["name"]) for symbol in _testing_symbols(manifest)}
    testing = import_module("picogrid_ecn_client.testing")
    # Maps the attribute name a submodule is bound under to its full declared
    # path, so an unrelated module bound under an approved leaf name (for
    # example `import os as mock_ecn`) is not exempted by its name alone.
    declared_modules = {
        str(symbol["module"]).rsplit(".", 1)[-1]: str(symbol["module"])
        for symbol in _testing_symbols(manifest)
    }
    reachable = {
        name
        for name, value in vars(testing).items()
        if not name.startswith("_")
        # Submodules that declare approved symbols are bound by import machinery,
        # and `__future__` flags plus the typing guard are language mechanics.
        and not isinstance(value, types.ModuleType)
        and not isinstance(value, __import__("__future__")._Feature)
        and name != "TYPE_CHECKING"
    }
    stray_modules = {
        name
        for name, value in vars(testing).items()
        if isinstance(value, types.ModuleType)
        and not name.startswith("_")
        and getattr(value, "__name__", None) != declared_modules.get(name)
    }
    unaccounted = (reachable - approved) | stray_modules
    assert not unaccounted, f"testing package exposes incidental globals: {sorted(unaccounted)}"


def test_consumer_magic_methods_are_enumerated() -> None:
    """A newly implemented consumer protocol is accounted for, not silently skipped."""

    class Gadget:
        def __call__(self) -> None: ...

    assert "__call__" in _real_public_members(Gadget)
    assert "__repr__" not in _real_public_members(Gadget)

    class Awaitable:
        def __await__(self) -> None: ...

    # An unlisted protocol must still be accounted for, not silently dropped.
    assert "__await__" in _real_public_members(Awaitable)

    class Derived(Gadget): ...

    # Inherited protocols count: a consumer can reach them on the subclass.
    assert "__call__" in _real_public_members(Derived)


def test_lazy_testing_exports_match_the_manifest() -> None:
    """The lazy-export allowlist cannot reach past the approved testing surface."""

    manifest = _manifest()
    declared = {str(symbol["name"]): str(symbol["module"]) for symbol in _testing_symbols(manifest)}
    approved = set(declared)
    testing = import_module("picogrid_ecn_client.testing")
    # `__getattr__` resolves these on demand, so they never appear in `vars()`
    # and are invisible to the incidental-globals check.
    lazy_exports = set(testing._EXPORTS)
    assert lazy_exports == approved, (
        "lazy testing exports disagree with the manifest: "
        f"unapproved={sorted(lazy_exports - approved)} missing={sorted(approved - lazy_exports)}"
    )
    for name in sorted(lazy_exports):
        expected = getattr(import_module(declared[name]), name)
        assert getattr(testing, name) is expected, (
            f"picogrid_ecn_client.testing.{name} does not resolve to the manifest target "
            f"{declared[name]}.{name}"
        )


class _Destructured:
    """Fixture whose public attributes are created by destructuring."""

    def __init__(self, values: tuple[int, int, int]) -> None:
        self.left, self.right = values[0], values[1]
        self.head, *self.rest = values
        [self.first, self.second] = values[0], values[1]


def test_destructured_attributes_are_enumerated() -> None:
    """Attributes bound through tuple and starred targets are accounted for."""

    members = _real_public_members(_Destructured)
    assert {"left", "right", "head", "rest", "first", "second"} <= members


# Pydantic attaches its own machinery to every model class.
# The exact Pydantic-generated member set, not a `model_*` pattern: a pattern
# would also credit an SDK-authored method such as `model_summary`.
_PYDANTIC_MACHINERY = frozenset(
    {
        "model_computed_fields",
        "model_config",
        "model_extra",
        "model_fields",
        "model_fields_set",
        "model_post_init",
        "__abstractmethods__",
        "__class_vars__",
        "__private_attributes__",
        "__pydantic_complete__",
        "__pydantic_computed_fields__",
        "__pydantic_core_schema__",
        "__pydantic_custom_init__",
        "__pydantic_decorators__",
        "__pydantic_extra__",
        "__pydantic_extra_info__",
        "__pydantic_fields__",
        "__pydantic_fields_set__",
        "__pydantic_generic_metadata__",
        "__pydantic_parent_namespace__",
        "__pydantic_post_init__",
        "__pydantic_private__",
        "__pydantic_root_model__",
        "__pydantic_serializer__",
        "__pydantic_setattr_handlers__",
        "__pydantic_validator__",
        "__signature__",
    }
)


def _conventionally_documented(symbol: Mapping[str, Any], target: type) -> set[str]:
    """Return members a page documents through a convention, not a member entry.

    Enum values are rendered as a value table, Pydantic attaches machinery to
    every model, and an exception subclass inherits attributes documented once
    on its base rather than repeated on each subclass.
    """

    credited: set[str] = set()
    # typing replaces a Protocol's constructor with its own guard.
    initializer = vars(target).get("__init__")
    if getattr(initializer, "__module__", "") == "typing":
        credited.add("__init__")
    # typing.Protocol attaches these to every protocol class.
    credited.update(
        name for name in ("__abstractmethods__", "__protocol_attrs__") if hasattr(target, name)
    )
    kind = str(symbol["kind"])
    if kind == "enum":
        credited.update(member.name for member in target)
        # `Enum` supplies these; only credit the inherited versions, so an enum
        # that authors its own consumer-visible formatting must document it.
        credited.update(name for name in ("__format__", "__new__") if not _redefines(target, name))
    if kind == "pydantic-model":
        credited.update(name for name in vars(target) if name in _PYDANTIC_MACHINERY)
        # Declared fields render in the page's field table, which the docstring
        # gate separately requires to carry a description for every entry, so
        # they are documented without being listed as members.
        credited.update(getattr(target, "model_fields", {}) or {})
        # Validators and serializers are invoked by Pydantic, not by a consumer,
        # so the page renders fields rather than these callables. Read them from
        # Pydantic's own registry instead of guessing from names. Computed
        # fields are deliberately absent: they are consumer-reachable and
        # serialized, so they must be documented like any other member.
        decorators = getattr(target, "__pydantic_decorators__", None)
        for group in (
            "validators",
            "field_validators",
            "root_validators",
            "model_validators",
            "field_serializers",
            "model_serializers",
        ):
            credited.update(getattr(decorators, group, {}) or {})
    return credited


def test_public_class_members_are_approved_or_excluded() -> None:
    """Every reachable member of an approved class is documented or excluded."""

    manifest = _manifest()
    excluded_members = manifest["excluded"]["members"]
    package = import_module("picogrid_ecn_client")
    # Keyed by the resolved class object, not its name: two different modules
    # can define same-named classes, and a name-only lookup would union an
    # unrelated symbol's approved members into a subclass.
    ancestors: dict[type, set[str]] = {}
    for symbol in _symbols(manifest):
        resolved = getattr(import_module(str(symbol["module"])), str(symbol["name"]))
        if isinstance(resolved, type):
            ancestors[resolved] = set(map(str, symbol.get("members", ())))
    for symbol in _symbols(manifest):
        name = str(symbol["name"])
        target = getattr(package, name)
        # Identity is checked for every approved export, not only classes, so a
        # rebound function or alias cannot pair manifest documentation with a
        # different object.
        expected = getattr(import_module(str(symbol["module"])), name)
        assert target is expected, (
            f"picogrid_ecn_client.{name} does not resolve to the manifest target "
            f"{symbol['module']}.{name}"
        )
        if not isinstance(target, type):
            continue
        approved = set(map(str, symbol.get("members", ())))
        # Members documented once on an approved SDK base are not repeated.
        for base in target.__mro__[1:]:
            # Only members the subclass does not redefine are credited from an
            # ancestor: an override is new behavior and must be reviewed where
            # it is written.
            inherited = ancestors.get(base, set())
            approved |= {
                member for member in inherited if not _overrides_before(target, base, member)
            }
        excluded = set(map(str, excluded_members.get(name, {}).get("names", ())))
        overlap = approved & excluded
        assert not overlap, f"{name} members are both approved and excluded: {sorted(overlap)}"
        advertised = _assert_lifecycle_flags(symbol, target)
        unaccounted = (
            _real_public_members(target)
            - approved
            - excluded
            - advertised
            - _conventionally_documented(symbol, target)
        )
        # `__init__` is only free when the SDK does not define one; a custom
        # constructor renders on the page and must be a manifest member.
        if not _defines_authored_init(target):
            unaccounted -= {"__init__"}
        assert not unaccounted, (
            f"{name} exposes members that are neither documented nor excluded: "
            f"{sorted(unaccounted)}"
        )


@dataclasses.dataclass
class _GeneratedInit:
    """Fixture whose constructor the dataclass decorator generates."""

    value: int = 0


@dataclasses.dataclass
class _ExplicitInitOnDataclass:
    """Fixture whose explicit constructor survives ``@dataclass``."""

    value: int = 0

    def __init__(self) -> None:
        self.value = 99


class _SubclassOfGeneratedInit(_GeneratedInit):
    """Fixture adding an authored constructor below dataclass metadata."""

    def __init__(self) -> None:
        self.value = 2


@dataclasses.dataclass(init=False)
class _AuthoredInitOnDataclass:
    """Fixture that keeps its hand-written constructor via ``init=False``."""

    value: int = 0

    def __init__(self) -> None:
        self.value = 1


class _OuterWithNested:
    """Fixture whose method defines a nested class binding its own attributes."""

    def __init__(self) -> None:
        self.mine = 1

        class Nested:
            def __init__(self) -> None:
                self.theirs = 2

        self.nested = Nested


def test_authored_constructors_are_distinguished_from_generated() -> None:
    """Authored constructors are found in the source, generated ones are not."""

    assert not _defines_authored_init(_GeneratedInit)
    # `@dataclass` preserves an explicit constructor rather than replacing it,
    # so the `init` flag alone cannot distinguish these two cases.
    assert _defines_authored_init(_ExplicitInitOnDataclass)
    assert _defines_authored_init(_SubclassOfGeneratedInit)


def test_nested_class_attributes_are_not_attributed_to_the_outer_class() -> None:
    """A nested class binds its own instances, not the exported one."""

    members = _real_public_members(_OuterWithNested)
    assert {"mine", "nested"} <= members
    assert "theirs" not in members


_ALIAS_KINDS = frozenset({"type-alias", "callback-alias"})


def _actual_kind(target: object) -> str:
    """Return the manifest kind the resolved object actually is.

    The renderer branches on `kind`, so a mislabeled symbol is documented with
    the wrong template even though its identity is correct.
    """

    if isinstance(target, type):
        if issubclass(target, BaseException):
            return "exception"
        if issubclass(target, enum.Enum):
            return "enum"
        if issubclass(target, BaseModel):
            return "pydantic-model"
        if getattr(target, "_is_protocol", False):
            return "protocol"
        return "class"
    if target is None or isinstance(target, (str, int, float, bytes, bool, tuple, list)):
        return "constant"
    if inspect.isfunction(target) or inspect.isbuiltin(target):
        return "function"
    return "type-alias"


def _kind_matches(declared: str, target: object) -> bool:
    """Return whether a declared kind is consistent with the resolved object."""

    if declared in _ALIAS_KINDS:
        # An alias is a declaration, not a runtime shape: `UserId = str` resolves
        # to a class, `Handler = Callable[..., None]` to a typing construct, and
        # neither is distinguishable from the thing it aliases. A plain function
        # is distinguishable, though, and has its own kind, so declaring one as
        # an alias is still a mislabel.
        return _actual_kind(target) != "function"
    return declared == _actual_kind(target)


def test_manifest_kinds_match_their_runtime_targets() -> None:
    """A mislabeled kind renders the wrong template for a correct object."""

    manifest = _manifest()
    package = import_module("picogrid_ecn_client")
    testing = import_module("picogrid_ecn_client.testing")
    workflows = import_module("picogrid_ecn_client.workflows")
    for symbol, namespace in (
        *((s, package) for s in _symbols(manifest)),
        *((s, testing) for s in _testing_symbols(manifest)),
        *((s, workflows) for s in _workflow_symbols(manifest)),
    ):
        name = str(symbol["name"])
        declared = str(symbol["kind"])
        target = getattr(namespace, name)
        assert _kind_matches(declared, target), (
            f"{name} is declared {declared!r} but resolves to {_actual_kind(target)!r}"
        )


def test_manifest_member_lists_have_no_duplicates() -> None:
    """A repeated member would render duplicate headings and anchors."""

    manifest = _manifest()
    for symbol in (*_symbols(manifest), *_testing_symbols(manifest)):
        members = [str(member) for member in symbol.get("members", ())]
        duplicates = sorted({member for member in members if members.count(member) > 1})
        assert not duplicates, f"{symbol['name']} repeats members: {duplicates}"


def test_main_package_exposes_no_incidental_globals() -> None:
    """The package exposes its exports and the modules that declare them."""

    manifest = _manifest()
    package = import_module("picogrid_ecn_client")
    approved = set(map(str, package.__all__))

    # A declared symbol module and every package on the way to it are reachable
    # through ordinary import machinery, so both are legitimate.
    allowed_modules: set[str] = set()
    for symbol in (
        *_symbols(manifest),
        *_testing_symbols(manifest),
        *_workflow_symbols(manifest),
    ):
        parts = str(symbol["module"]).split(".")
        for index in range(1, len(parts) + 1):
            allowed_modules.add(".".join(parts[:index]))

    unaccounted: set[str] = set()
    for name, value in vars(package).items():
        if name.startswith("_") or name in approved:
            continue
        if (
            isinstance(value, types.ModuleType)
            and value.__name__ in allowed_modules
            # The binding must be the one import machinery creates, so an
            # approved module re-bound under another public name still fails.
            and value.__name__.rsplit(".", 1)[-1] == name
        ):
            continue
        unaccounted.add(name)
    assert not unaccounted, f"package exposes incidental globals: {sorted(unaccounted)}"


class _CoroutineAiter:
    """Fixture whose ``__aiter__`` is a coroutine, which ``async for`` rejects."""

    async def __aiter__(self) -> _CoroutineAiter:  # pragma: no cover - never awaited
        return self

    async def __anext__(self) -> None:  # pragma: no cover - never awaited
        raise StopAsyncIteration


def test_coroutine_aiter_is_not_a_valid_async_iterator() -> None:
    """``async for`` does not await ``__aiter__``, so a coroutine fails the flag."""

    symbol = {"name": "_CoroutineAiter", "async_iterator": True}
    with pytest.raises(AssertionError, match="async_iterator"):
        _assert_lifecycle_flags(symbol, _CoroutineAiter)


class _AuthoredBase:
    """Fixture base whose members are approved on the base itself."""

    def shared(self) -> None: ...


class _OverridingSubclass(_AuthoredBase):
    """Fixture subclass that redefines an inherited member."""

    def shared(self) -> None: ...


class _InheritingSubclass(_AuthoredBase):
    """Fixture subclass that inherits without redefining."""


def test_redefinition_is_distinguished_from_inheritance() -> None:
    """An override is new behavior; inheritance is already documented."""

    assert _redefines(_OverridingSubclass, "shared")
    assert not _redefines(_InheritingSubclass, "shared")
    # `_defines_authored` answers the broader question across the SDK bases.
    assert _defines_authored(_InheritingSubclass, "shared")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (import_module("picogrid_ecn_client").ECNClient, "class"),
        (import_module("picogrid_ecn_client").Entity, "pydantic-model"),
        (import_module("picogrid_ecn_client").EntityCategory, "enum"),
        (import_module("picogrid_ecn_client").ECNClientError, "exception"),
        (import_module("picogrid_ecn_client").load_config, "function"),
        (import_module("picogrid_ecn_client").RequestTaskHandler, "protocol"),
    ],
)
def test_actual_kind_classifies_the_public_surface(target: object, expected: str) -> None:
    """Kind detection must agree with the manifest for each represented kind."""

    assert _actual_kind(target) == expected


def _replacement_format(self: object, spec: str) -> str:
    """Stand-in implementation assigned over an inherited protocol method."""

    return "replaced"


class _ValueBase:
    """Fixture base declaring a documented constant and an implementation."""

    default_code: str = "base"

    def __format__(self, spec: str) -> str:
        return "base"


class _ValueOverride(_ValueBase):
    """Fixture rebinding only the inherited constant's value."""

    default_code: str = "override"


class _AssignedImplementation(_ValueBase):
    """Fixture replacing an inherited implementation by assignment."""

    __format__ = _replacement_format


def test_assigned_implementations_count_as_redefinition() -> None:
    """An assigned callable replaces behavior, so it must be reviewed."""

    assert _redefines(_AssignedImplementation, "__format__")


def test_rebinding_an_inherited_constant_is_not_redefinition() -> None:
    """Only the value differs; the documented contract comes from the base.

    This is what keeps every exception subclass from having to redeclare
    ``default_code`` merely because it names its own error code.
    """

    assert not _redefines(_ValueOverride, "default_code")
    assert _redefines(_ValueBase, "default_code") is False


@pytest.mark.parametrize("declared", ["type-alias", "callback-alias"])
def test_alias_kinds_accept_any_runtime_shape(declared: str) -> None:
    """`UserId = str` resolves to a class; an alias is a declaration, not a shape."""

    assert _kind_matches(declared, str)


@pytest.mark.parametrize("value", [None, (1, 2), [1], "token", 5, True])
def test_constants_cover_the_shapes_a_manifest_can_declare(value: object) -> None:
    """A constant is not only a string."""

    assert _kind_matches("constant", value)


def test_kind_mismatches_still_fail() -> None:
    """Runtime-distinguishable kinds remain enforced."""

    assert not _kind_matches("constant", _kind_matches)
    assert not _kind_matches("class", _kind_matches)


class _PropertyOverride(_ValueBase):
    """Fixture replacing an inherited method with a non-callable descriptor."""

    __format__ = property(lambda self: "replaced")


class _IntermediateOverride(_ValueBase):
    """Unmanifested intermediate that replaces an approved base implementation."""

    def __format__(self, spec: str) -> str:
        return "intermediate"


class _InheritsIntermediate(_IntermediateOverride):
    """Exported class that inherits the intermediate's override unchanged."""


def test_descriptor_replacements_count_as_redefinition() -> None:
    """A property changes attribute access even though it is not callable."""

    assert _redefines(_PropertyOverride, "__format__")


def test_intermediate_overrides_supersede_an_ancestor_approval() -> None:
    """An approval describes its own class, not a nearer implementation."""

    # The exported class redefines nothing itself...
    assert not _redefines(_InheritsIntermediate, "__format__")
    # ...but the implementation it inherits is not the approved base's.
    assert _overrides_before(_InheritsIntermediate, _ValueBase, "__format__")
    assert not _overrides_before(_ValueOverride, _ValueBase, "__format__")


class _AnnotationOnlyOverride(_ValueBase):
    """Fixture that re-annotates an inherited member without rebinding it."""

    __format__: object


def test_annotation_only_members_are_not_redefinitions() -> None:
    """A bare annotation binds nothing, so the inherited approval still holds."""

    assert not _redefines(_AnnotationOnlyOverride, "__format__")
    assert "__format__" not in vars(_AnnotationOnlyOverride)


class _RemovesInheritedBehavior(_ValueBase):
    """Fixture replacing an inherited implementation with a non-implementation."""

    __format__ = None


def test_removing_an_inherited_implementation_is_redefinition() -> None:
    """Replacing behavior with ``None`` changes the contract just as much."""

    assert _redefines(_RemovesInheritedBehavior, "__format__")


def test_alias_kinds_still_reject_plain_functions() -> None:
    """A function is runtime-distinguishable and has its own kind."""

    assert not _kind_matches("type-alias", _kind_matches)
    assert not _kind_matches("callback-alias", _kind_matches)
