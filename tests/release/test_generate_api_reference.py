# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ast
import importlib
import itertools
import json
import re
import shutil
import socket
import sys
from pathlib import Path

import griffe
import pytest
from scripts import generate_api_reference as generator
from scripts import release_checks

REPOSITORY_ROOT = Path(__file__).parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "scripts" / "public-api-manifest.json"
EXAMPLE_MANIFEST_PATH = REPOSITORY_ROOT / "examples" / "manifest.json"
RELEASE_POLICY_PATH = REPOSITORY_ROOT / "scripts" / "release-policy.json"
PACKAGE_ROOT = REPOSITORY_ROOT / "src"
GOLDEN_ROOT = Path(__file__).parent / "fixtures" / "api_reference"


REPRESENTATIVE_ROUTES = (
    "reference/python/client/ecn-client",
    "reference/python/entities/entity",
    "reference/python/configuration/wire-format",
    "reference/python/configuration/auth-config",
    "reference/python/exceptions/configuration-error",
    "reference/python/testing/mock-ecn",
    "reference/python/locations/location",
    "reference/python/tasks/tasks",
    "reference/python",
)


def _build() -> generator.ReferenceBuild:
    return generator.build_reference(
        manifest_path=MANIFEST_PATH,
        package_root=PACKAGE_ROOT,
        repository_root=REPOSITORY_ROOT,
    )


def _manifest_symbols(manifest: dict[str, object]) -> list[dict[str, object]]:
    symbols: list[dict[str, object]] = []
    for key in ("symbols", "testing_symbols", "workflow_symbols"):
        value = manifest[key]
        assert isinstance(value, list)
        symbols.extend(value)
    return symbols


def test_generation_is_byte_deterministic() -> None:
    first = _build()
    second = _build()

    assert first.pages == second.pages
    assert first.inventory == second.inventory


def test_site_routes_are_mount_neutral() -> None:
    # The documentation mount (Astro ``base``) is deployment configuration; the
    # build-time link rewriter prefixes it, so generated routes never embed it.
    assert generator._site_route("") == "/"
    assert generator._site_route("reference/python/tasks") == "/reference/python/tasks/"
    assert generator._site_route("/reference/python/") == "/reference/python/"


def test_generated_pages_never_embed_the_documentation_mount() -> None:
    build = _build()

    # The generator is mount-neutral, so the mount must not appear as a complete
    # path segment in generated output. Repository names may share the mount prefix
    # (for example, ``/ecn-sdk-python``) without embedding the documentation mount.
    # The mount is read from the same single source the build uses, so this test
    # follows a reconfigured mount instead of pinning one spelling.
    mount = release_checks.load_documentation_base_path(
        REPOSITORY_ROOT / "docs" / "site" / "site-config.mjs"
    )
    mount_reference = re.compile(rf"{re.escape(mount)}(?=$|[^A-Za-z0-9_-])")
    for route, page in build.pages.items():
        assert mount_reference.search(page) is None, route


def test_installed_package_root_maps_sources_back_to_repository_paths(tmp_path: Path) -> None:
    installed_root = tmp_path / "site-packages"
    shutil.copytree(PACKAGE_ROOT / "picogrid_ecn_client", installed_root / "picogrid_ecn_client")

    installed = generator.build_reference(
        manifest_path=MANIFEST_PATH,
        package_root=installed_root,
        repository_root=REPOSITORY_ROOT,
    )

    assert installed.pages == _build().pages
    assert all(
        path.startswith("src/picogrid_ecn_client/") for path in installed.inventory["source_paths"]
    )


def test_async_signatures_and_usage_notes_are_rendered() -> None:
    build = _build()

    client_page = build.pages["reference/python/client/ecn-client"]
    tasks_page = build.pages["reference/python/tasks/tasks"]
    stream_page = build.pages["reference/python/streams/event-stream"]
    assert "async def start(" in client_page
    assert "async with" in client_page
    assert "async def send(" in tasks_page
    assert "async for" in stream_page


def test_only_exact_private_identifier_defaults_are_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "reference_default_fixture"
    (tmp_path / f"{module_name}.py").write_text(
        "_TOKEN = 'resolved'\n"
        "\n"
        "def exact(value: str = _TOKEN) -> None: ...\n"
        "def literal(value: str = '_TOKEN') -> None: ...\n"
        "def compound(value: str = _TOKEN + '-suffix') -> None: ...\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    static_module = griffe.load(module_name, search_paths=[tmp_path], allow_inspection=False)
    importlib.import_module(module_name)
    try:
        signatures = {
            name: generator._signatures(static_module[name])[0]
            for name in ("exact", "literal", "compound")
        }
    finally:
        sys.modules.pop(module_name, None)

    assert signatures["exact"] == "def exact(value: str = 'resolved') -> None"
    assert signatures["literal"] == "def literal(value: str = '_TOKEN') -> None"
    assert signatures["compound"] == "def compound(value: str = _TOKEN + '-suffix') -> None"
    for signature in signatures.values():
        ast.parse(f"{signature}: ...")


@pytest.mark.parametrize("route", REPRESENTATIVE_ROUTES)
def test_representative_page_matches_golden(route: str) -> None:
    build = _build()
    fixture = GOLDEN_ROOT / f"{route.removeprefix('reference/python/').replace('/', '__')}.md"
    if route == "reference/python":
        fixture = GOLDEN_ROOT / "index.md"

    assert build.pages[route] == fixture.read_text()


def test_every_manifest_route_produces_exactly_one_page() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    expected = {"reference/python"}
    expected.update(group["route"] for group in manifest["groups"])
    expected.update(symbol["route"] for symbol in _manifest_symbols(manifest))

    build = _build()

    assert set(build.pages) == expected
    assert len(build.pages) == len(expected)


def test_generated_output_excludes_private_surface_and_environment_data() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    build = _build()
    text = "\n".join(build.pages.values())
    mock_page = build.pages["reference/python/testing/mock-ecn"]

    for module in manifest["excluded"]["modules"]:
        assert module not in text
    for name in manifest["excluded"]["members"]["MockECN"]["names"]:
        assert f"## `{name}`" not in mock_page
    assert "## `main`" not in text
    assert re.search(r"20\d\d-", text) is None
    assert "/Users/" not in text
    assert "/tmp/" not in text
    assert socket.gethostname() not in text
    assert re.search(r"\b[0-9a-f]{40}\b", text) is None


def test_generated_output_uses_only_public_rendering_syntax() -> None:
    build = _build()
    text = "\n".join(build.pages.values())

    for fragment in (
        "_PydanticGeneralMetadata",
        "annotation=",
        "required=True",
        "FieldInfo",
        "AnyTaskHandler",
        ":meth:",
        ":class:",
        ":func:",
    ):
        assert fragment not in text


def test_pydantic_fields_render_clean_types_and_code_spanned_constraints() -> None:
    build = _build()
    location_page = build.pages["reference/python/locations/location"]
    config_page = build.pages["reference/python/configuration/ecn-config"]

    assert "| altitude | float \\| None | no | None | `finite` |" in location_page
    assert "| bearing | float \\| None | no | None | `ge=0`, `lt=360`, `finite` |" in location_page
    assert (
        "| source | str \\| None | no | None | `min_length=1`, `max_length=128` |" in location_page
    )
    assert "`pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]$'`" in config_page
    for line in config_page.splitlines():
        if "pattern=" not in line:
            continue
        constraints_cell = line.split(" | ")[4]
        assert "pattern=" not in re.sub(r"(`+).*?\1", "", constraints_cell)


def test_inline_code_uses_safe_delimiters_and_table_pipe_escaping() -> None:
    rendered = generator._table(("Value",), ((generator._inline_code("a`b|c"),),))

    assert "| ``a`b\\|c`` |" in rendered


def test_function_sections_start_at_h2_without_skipping_levels() -> None:
    page = _build().pages["reference/python/configuration/load-config"]
    heading_levels = [
        len(match.group(1)) for line in page.splitlines() if (match := re.match(r"^(#+) ", line))
    ]

    assert heading_levels
    assert heading_levels[0] == 2
    assert all(current <= previous + 1 for previous, current in itertools.pairwise(heading_levels))


def test_generic_class_and_protocol_declarations_use_static_bases() -> None:
    build = _build()

    assert (
        "class EventStream(AsyncIterator[EventT], Generic[EventT]): ..."
        in build.pages["reference/python/streams/event-stream"]
    )
    assert (
        "class RequestTaskHandler(Protocol[RequestT_contra, ResultT_co]): ..."
        in build.pages["reference/python/tasks/request-task-handler"]
    )
    assert (
        "class ContextTaskHandler(Protocol[RequestT_contra, ResultT_co]): ..."
        in build.pages["reference/python/tasks/context-task-handler"]
    )


def test_source_links_use_manifest_template(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    manifest["source_link"]["template"] = (
        "https://example.invalid/{repository_path}?version={project_version}"
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    build = generator.build_reference(
        manifest_path=manifest_path,
        package_root=PACKAGE_ROOT,
        repository_root=REPOSITORY_ROOT,
    )

    assert (
        "https://example.invalid/src/picogrid_ecn_client/config.py?version=0.1.0#L"
        in build.pages["reference/python/configuration/load-config"]
    )


def test_workflow_pages_link_the_manifest_runnable_example() -> None:
    public_manifest = json.loads(MANIFEST_PATH.read_text())
    examples_manifest = json.loads(EXAMPLE_MANIFEST_PATH.read_text())
    project_version = str(json.loads(RELEASE_POLICY_PATH.read_text())["project_version"])
    workflow_symbols = {
        (str(symbol["module"]), str(symbol["name"])): str(symbol["route"])
        for symbol in public_manifest["workflow_symbols"]
    }
    build = _build()

    for example in examples_manifest["examples"]:
        workflow = example["workflow"]
        if workflow is None:
            continue
        key = (str(workflow["module"]), str(workflow["function"]))
        source_path = str(example["source_path"])
        assert key in workflow_symbols, (
            f"{source_path} workflow is not registered in the public API manifest"
        )
        route = workflow_symbols[key]
        expected = (
            f"[{source_path.removeprefix('examples/')}]"
            f"(https://github.com/picogrid/ecn-sdk-python/blob/v{project_version}/{source_path})"
        )
        assert expected in build.pages[route], route


def test_workflow_model_pages_link_the_examples_that_use_them() -> None:
    project_version = str(json.loads(RELEASE_POLICY_PATH.read_text())["project_version"])
    build = _build()

    for route, filename in (
        ("reference/python/workflows/check-clock-result", "check_clock.py"),
        ("reference/python/workflows/receive-task-result", "receive_task.py"),
    ):
        assert (
            f"[{filename}](https://github.com/picogrid/ecn-sdk-python/blob/"
            f"v{project_version}/examples/{filename})"
        ) in build.pages[route]


def test_echo_models_link_direct_and_mesh_task_examples() -> None:
    project_version = str(json.loads(RELEASE_POLICY_PATH.read_text())["project_version"])
    build = _build()

    for route in (
        "reference/python/workflows/echo-request",
        "reference/python/workflows/echo-result",
    ):
        page = build.pages[route]
        for filename in ("dispatch_task.py", "dispatch_mesh_task.py"):
            assert (
                f"[{filename}](https://github.com/picogrid/ecn-sdk-python/blob/"
                f"v{project_version}/examples/{filename})"
            ) in page
        assert "preflight.py" not in page


def test_every_public_workflow_result_attribute_is_rendered() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    build = _build()

    for symbol in manifest["workflow_symbols"]:
        if symbol["kind"] != "class" or not str(symbol["name"]).endswith("Result"):
            continue
        canonical = f"{symbol['module']}.{symbol['name']}"
        public_attributes = [
            member
            for name, member in build.symbols[canonical].members.items()
            if not name.startswith("_") and type(member).__name__ == "Attribute"
        ]
        assert public_attributes, canonical
        page = build.pages[str(symbol["route"])]
        assert "## Attributes" in page
        for attribute in public_attributes:
            rendered_type = generator._expression_text(attribute.annotation).replace("|", "\\|")
            assert f"| {attribute.name} | {rendered_type} |" in page, (
                f"{canonical}.{attribute.name}"
            )


def test_overloaded_callable_tables_use_public_overload_types() -> None:
    page = _build().pages["reference/python/tasks/tasks"]

    assert (
        "| handler | RequestTaskHandler[RequestT, ResultT] "
        "\\| ContextTaskHandler[RequestT, ResultT] |"
    ) in page
    assert (
        "| TaskResult[ResultT] \\| TaskAcknowledgement \\| TaskResult[BaseModel] "
        "\\| DispatchReceipt | The acknowledgment, result, or dispatch receipt "
        "selected by `mode` and the responding status. |"
    ) in page


def test_pages_use_frontmatter_h1_and_canonical_imports() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    build = _build()

    assert "\n# Python API reference\n" not in build.pages["reference/python"]
    for symbol in _manifest_symbols(manifest):
        page = build.pages[str(symbol["route"])]
        assert f"\n# `{symbol['name']}`\n" not in page
        module = str(symbol["module"])
        if module.startswith("picogrid_ecn_client.testing"):
            namespace = "picogrid_ecn_client.testing"
        elif module.startswith("picogrid_ecn_client.workflows"):
            namespace = "picogrid_ecn_client.workflows"
        else:
            namespace = "picogrid_ecn_client"
        assert f"```python\nfrom {namespace} import {symbol['name']}\n```" in page
    assert (
        "```python\nfrom picogrid_ecn_client import __version__\n```"
        in build.pages["reference/python"]
    )


def test_class_source_precedes_members_and_is_not_duplicated_at_page_end() -> None:
    build = _build()
    page = build.pages["reference/python/tasks/tasks"]
    tasks = build.symbols["picogrid_ecn_client.interfaces.tasks.Tasks"]
    project_version = str(json.loads(RELEASE_POLICY_PATH.read_text())["project_version"])
    class_source = (
        "[Source · interfaces/tasks.py]"
        f"(https://github.com/picogrid/ecn-sdk-python/blob/v{project_version}/"
        f"src/picogrid_ecn_client/interfaces/tasks.py#L{tasks.lineno})"
    )

    assert page.count(class_source) == 1
    assert page.index(class_source) < page.index("## `register`")
    assert not page.rstrip().endswith(class_source)


def test_group_indexes_use_curated_group_summaries() -> None:
    build = _build()

    assert (
        "| [Tasks](/reference/python/tasks/) | Typed task registration and dispatch. |"
        in build.pages["reference/python"]
    )
    assert "Reference for Tasks." not in build.pages["reference/python/tasks"]


def test_inventory_contains_every_manifest_symbol_in_manifest_order() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    expected = [item["name"] for item in _manifest_symbols(manifest)]

    inventory = _build().inventory

    assert [item["name"] for item in inventory["symbols"]] == expected
    assert all(not Path(path).is_absolute() for path in inventory["source_paths"])


def test_docstring_gate_names_an_incomplete_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    build = _build()
    symbol = build.symbols["picogrid_ecn_client.config.load_config"]
    monkeypatch.setattr(symbol, "docstring", None)

    errors = generator.docstring_completeness_errors(build)

    assert "picogrid_ecn_client.config.load_config: missing docstring summary" in errors


def test_write_removes_orphans_and_check_detects_stale_files(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs" / "reference" / "python"
    docs_root.mkdir(parents=True)
    (docs_root / "orphan.md").write_text("orphan\n")
    build = _build()

    generator.write_reference(build, docs_root)

    assert not (docs_root / "orphan.md").exists()
    assert (docs_root / "diagnostics" / "index.md").is_file()
    assert not (docs_root / "diagnostics.md").exists()
    assert generator.compare_reference(build, docs_root) == []
    index = docs_root / "index.md"
    index.write_text(index.read_text() + "stale\n")
    assert generator.compare_reference(build, docs_root) == ["stale: index.md"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1024 * 1024", 1048576),
        ("2 ** 4", 16),
        ("-5", -5),
        ("'text'", "text"),
        ("None", None),
        ("(1, 2)", (1, 2)),
    ],
)
def test_static_literal_resolves_constant_expressions(source: str, expected: object) -> None:
    """Literals and numeric arithmetic resolve from source text."""

    assert generator._static_literal(ast.parse(source, mode="eval").body) == expected


@pytest.mark.parametrize(
    "source",
    [
        "object()",
        "float('nan')",
        "{1, 2}",
        "OTHER_NAME",
        "1 / 0",
        "helpers.value",
        "1e400 * 1e400",
        # Finite operands whose product overflows exercise the post-operation guard.
        "1e308 * 1e308",
        # An unbounded exponent must be refused rather than evaluated.
        "2 ** 10000000000",
        # Nested powers pass every individual exponent check while the
        # intermediate value explodes, so cumulative growth must be bounded.
        "((2 ** 64) ** 64) ** 64",
    ],
)
def test_static_literal_refuses_unresolvable_defaults(source: str) -> None:
    """Anything not a stable literal raises, leaving the private name visible."""

    with pytest.raises((ValueError, TypeError, ZeroDivisionError, OverflowError)):
        generator._static_literal(ast.parse(source, mode="eval").body)


def test_source_link_exemption_is_spent_once_per_emission() -> None:
    """A page reproducing its own source line does not get a second exemption."""

    build = generator.build_reference()
    route = "reference/python/probe"
    link = "[Source · _private/mod.py](https://example.invalid/_private/mod.py#L1)"
    build.source_references[route] = {link: 1}
    build.pages[route] = f"# Probe\n{link}\n{link}\n"

    errors = [error for error in generator.private_surface_errors(build) if route in error]
    assert len(errors) == 1
    assert "_private" in errors[0]

    build.source_references[route] = {link: 2}
    assert not [error for error in generator.private_surface_errors(build) if route in error]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("value: str = '_TOKEN'", []),
        ('value: str = "_TOKEN"', []),
        # An apostrophe in prose must not swallow the rest of the line.
        ("the client's _secret and the user's data", ["_secret"]),
        ("plain _leak here", ["_leak"]),
    ],
)
def test_quoted_literals_are_data_not_symbols(line: str, expected: list[str]) -> None:
    """A quoted value is not a symbol a reader could import."""

    scanned = generator._QUOTED_LITERAL.sub("''", line)
    assert sorted(set(generator._ANY_PRIVATE_NAME.findall(scanned))) == expected


def test_quoted_text_is_data_only_in_rendered_values() -> None:
    """Prose naming a private symbol is a leak; a rendered default is not."""

    build = generator.build_reference()
    route = "reference/python/probe"
    build.source_references[route] = {}
    build.pages[route] = "\n".join(
        [
            "# Probe",
            'Use "_secret" when configuring.',
            "| Name | Type | Default | Description |",
            "| --- | --- | --- | --- |",
            "| name | str | '_TOKEN' | A token. |",
            "```python",
            "def f(value: str = '_TOKEN') -> None",
            "```",
        ]
    )

    errors = [error for error in generator.private_surface_errors(build) if route in error]
    assert len(errors) == 1
    assert "_secret" in errors[0]


def test_a_table_without_a_default_header_is_scanned_whole() -> None:
    """With no Default column to identify, the scan fails closed."""

    build = generator.build_reference()
    route = "reference/python/probe"
    build.source_references[route] = {}
    build.pages[route] = "| name | str | '_TOKEN' | A token. |"

    errors = [error for error in generator.private_surface_errors(build) if route in error]
    assert len(errors) == 1
    assert "_TOKEN" in errors[0]


def test_table_descriptions_are_prose_not_rendered_values() -> None:
    """A Default cell holds data; the trailing Description holds docstring prose."""

    build = generator.build_reference()
    route = "reference/python/probe"
    build.source_references[route] = {}
    build.pages[route] = "\n".join(
        [
            "| Name | Type | Default | Description |",
            "| --- | --- | --- | --- |",
            "| token | str | '_TOKEN' | A token value. |",
            "| mode | str | 'fast' | Use \"_secret\" internally. |",
        ]
    )

    errors = [error for error in generator.private_surface_errors(build) if route in error]
    assert len(errors) == 1
    assert "_secret" in errors[0]


def test_only_default_expressions_are_treated_as_data() -> None:
    """A quoted annotation is a type reference; only a default renders a value."""

    build = generator.build_reference()
    route = "reference/python/probe"
    build.source_references[route] = {}
    build.pages[route] = "\n".join(
        [
            "```python",
            'def load(value: "_PrivateModel") -> None',
            "def f(value: str = '_TOKEN') -> None",
            "def g(value: \"_AlsoPrivate\" = '_TOKEN') -> None",
            "```",
            "| Name | Type | Default | Description |",
            "| --- | --- | --- | --- |",
            "| token | '_QuotedType' | '_TOKEN' | A token value. |",
            "| mode | str | 'fast' | Use \"_secret\" internally. |",
        ]
    )

    reported = {
        error.split("private symbol ")[1].split(" ")[0]
        for error in generator.private_surface_errors(build)
        if route in error
    }
    assert reported == {"_PrivateModel", "_AlsoPrivate", "_QuotedType", "_secret"}


def test_enum_value_column_and_escaped_pipes_are_handled() -> None:
    """An enum table ends in a value column; prose pipes are escaped, not boundaries."""

    build = generator.build_reference()
    route = "reference/python/probe"
    build.source_references[route] = {}
    build.pages[route] = "\n".join(
        [
            "| Name | Value |",
            "| --- | --- |",
            "| TOKEN | '_TOKEN' |",
            "",
            "| Name | Type | Default | Description |",
            "| --- | --- | --- | --- |",
            "| mode | str | 'fast' | Use \"_secret\" \\| only internally. |",
        ]
    )

    errors = [error for error in generator.private_surface_errors(build) if route in error]
    assert len(errors) == 1
    assert "_secret" in errors[0]
