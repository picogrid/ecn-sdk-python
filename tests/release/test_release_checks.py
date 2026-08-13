# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from scripts import installed_examples, installed_mock_process, release_checks
from scripts import verify_release as release_workflow
from scripts.release_checks import (
    ArtifactInspection,
    ArtifactPolicyError,
    DocumentationInspection,
    _publication_manifest_lines,
    compare_rebuilt_artifacts,
    inspect_documentation,
    inspect_sdist,
    inspect_wheel,
    load_policy,
    scan_secret_and_address_content,
)
from scripts.sync_dep_locks import canonical_root_requirement, requirement_name

POLICY_PATH = Path(__file__).parents[2] / "scripts" / "release-policy.json"
_CANONICAL_LICENSE_TEXT = POLICY_PATH.parents[1].joinpath("LICENSE").read_bytes()
_NONPUBLIC_IMPORT_CANARY = b"import picogrid_" + b"example_sdk._internal\n"
_UNAPPROVED_REPOSITORY_CANARY = b"https://github.com/picogrid/" + b"unpublished-sdk\n"
_UNAPPROVED_SSH_REPOSITORY_CANARY = b"git@github.com:picogrid/" + b"unpublished-sdk.git\n"
_UNAPPROVED_PORT_REPOSITORY_CANARY = b"https://github.com:443/picogrid/" + b"unpublished-sdk.git\n"
_UNAPPROVED_SSH_PORT_REPOSITORY_CANARY = (
    b"ssh://git@github.com:22/picogrid/" + b"unpublished-sdk.git\n"
)
_PRIVATE_API_PATH_CANARY = b'path = "/' + b'internal/status"\n'
_PRIVATE_KEY_CANARY = b"-----BEGIN " + b"PRIVATE KEY-----\n"
_IPV4_CANARY = b"connect to 192.0.2." + b"10\n"
_IPV6_CANARY = b"connect to 2001:db8:" + b":1\n"
_CREDENTIAL_URL_CANARY = b"mqtts:" + b"//operator:password@broker.ops." + b"example." + b"com\n"
_OPERATIONAL_FQDN_CANARY = b"broker.ops." + b"example." + b"com\n"
_PAGEFIND_SENTINEL_URL = b"https:" + b"//p"
_SINGLE_LABEL_HOST_CANARY = b"https:" + b"//broker"
_ENCODED_OPERATIONAL_URL_CANARY = b"endpoint=https%3A" + b"%2F%2Fbroker.ops%2E" + b"example%2Ecom"
_SLACK_TOKEN_CANARY = b"xox" + b"b-1234567890-abcdefghijklmnop\n"
_SLACK_APP_TOKEN_CANARY = b"xapp" + b"-1234567890-abcdefghijklmnop\n"
_SLACK_WEBHOOK_CANARY = (
    b"https:" + b"//hooks.slack." + b"com/services/T12345678/B12345678/abcdefghijklmnop\n"
)
_MQTT_TRANSPORT_SOURCE = b"PROTOCOL = paho.MQTTv5\n"
_MQTT_MOCK_SOURCE = b"if protocol_level != 5:\n    raise RuntimeError\n"


def _metadata(policy: dict[str, Any]) -> bytes:
    requires_dist = "".join(
        f"Requires-Dist: {requirement}\n" for requirement in policy["runtime_requirements"]
    )
    return (
        "Metadata-Version: 2.4\n"
        "Name: picogrid-ecn-client\n"
        "Version: 0.1.0\n"
        f"License-Expression: {policy['license_expression']}\n"
        f"Requires-Python: {policy['requires_python']}\n"
        "Project-URL: Changelog, https://github.com/picogrid/ecn-sdk-python/blob/main/CHANGELOG.md\n"
        "Project-URL: Homepage, https://github.com/picogrid/ecn-sdk-python\n"
        "Project-URL: Issues, https://github.com/picogrid/ecn-sdk-python/issues\n"
        "Project-URL: Security, https://github.com/picogrid/ecn-sdk-python/security/policy\n"
        "Project-URL: Source, https://github.com/picogrid/ecn-sdk-python\n"
        "Project-URL: Support, https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md\n"
        f"{requires_dist}"
        "\n"
        "Synthetic public package fixture.\n"
    ).encode()


def _policy_requirement(policy: dict[str, Any], name: str) -> bytes:
    for requirement in policy["runtime_requirements"]:
        if requirement_name(requirement) == name:
            return str(requirement).encode()
    raise AssertionError(f"release policy has no runtime requirement for {name!r}")


def _topic_source() -> bytes:
    return b"""ENTITY_JSON_SUBSCRIPTION = "entity/+/+/+"
ENTITY_PROTOBUF_SUBSCRIPTION = "entity_pb/+/+"
LOCATION_JSON_SUBSCRIPTION = "entity_location/+/+"
LOCATION_PROTOBUF_SUBSCRIPTION = "entity_location_pb/+/+"
"""


def _wheel_contents(policy: dict[str, Any]) -> dict[str, bytes]:
    contents = dict.fromkeys(policy["wheel_package_files"], b"")
    contents["picogrid_ecn_client/_protocol/topics.py"] = _topic_source()
    contents["picogrid_ecn_client/_transport/mqtt.py"] = _MQTT_TRANSPORT_SOURCE
    contents["picogrid_ecn_client/testing/_mqtt.py"] = _MQTT_MOCK_SOURCE
    prefix = "picogrid_ecn_client-0.1.0.dist-info/"
    for name in policy["wheel_dist_info_files"]:
        contents[f"{prefix}{name}"] = b""
    contents[f"{prefix}METADATA"] = _metadata(policy)
    contents[f"{prefix}WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    contents[f"{prefix}licenses/LICENSE"] = _CANONICAL_LICENSE_TEXT
    return contents


def _write_wheel(path: Path, contents: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, data in sorted(contents.items()):
            archive.writestr(name, data)


def _sdist_contents(policy: dict[str, Any]) -> dict[str, bytes]:
    root = "picogrid_ecn_client-0.1.0"
    contents = {
        f"{root}/LICENSE": _CANONICAL_LICENSE_TEXT,
        f"{root}/MANIFEST.in": ("\n".join(_publication_manifest_lines(policy)) + "\n").encode(),
        f"{root}/PKG-INFO": _metadata(policy),
        f"{root}/README.md": b"Synthetic public fixture.\n",
        f"{root}/pyproject.toml": b"[project]\nname='picogrid-ecn-client'\n",
        f"{root}/setup.cfg": b"[egg_info]\n",
    }
    for key in (
        "sdist_documentation_files",
        "sdist_example_files",
        "sdist_auxiliary_files",
    ):
        for name in policy[key]:
            contents[f"{root}/{name}"] = b""
    contents[f"{root}/operator-app/LICENSE"] = _CANONICAL_LICENSE_TEXT
    repository = POLICY_PATH.parents[1]
    contents[f"{root}/scripts/release-policy.json"] = POLICY_PATH.read_bytes()
    for name, spec in policy["public_brand_assets"].items():
        surfaces = spec.get("surfaces", ["documentation", "operator"])
        if "documentation" in surfaces:
            contents[f"{root}/docs/site/public/{name}"] = (
                repository / "docs" / "site" / "public" / name
            ).read_bytes()
        if "operator" in surfaces:
            contents[f"{root}/operator-app/frontend/public/{name}"] = (
                repository / "operator-app" / "frontend" / "public" / name
            ).read_bytes()
    for name in policy["wheel_package_files"]:
        contents[f"{root}/src/{name}"] = b""
    contents[f"{root}/src/picogrid_ecn_client/_protocol/topics.py"] = _topic_source()
    contents[f"{root}/src/picogrid_ecn_client/_transport/mqtt.py"] = _MQTT_TRANSPORT_SOURCE
    contents[f"{root}/src/picogrid_ecn_client/testing/_mqtt.py"] = _MQTT_MOCK_SOURCE
    egg_info = f"{root}/src/picogrid_ecn_client.egg-info"
    contents.update(
        {
            f"{egg_info}/PKG-INFO": _metadata(policy),
            f"{egg_info}/SOURCES.txt": b"",
            f"{egg_info}/dependency_links.txt": b"",
            f"{egg_info}/entry_points.txt": b"",
            f"{egg_info}/requires.txt": b"",
            f"{egg_info}/top_level.txt": b"picogrid_ecn_client\n",
        }
    )
    return contents


_MINIMAL_BRANDED_README = """<div align="center">

<picture>
  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" media="(prefers-color-scheme: light)">
  <source srcset="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-dark.png" media="(prefers-color-scheme: dark)">
  <img src="https://docs.picogrid.com/ecn-sdk/brand/picogrid-wordmark-light.png" alt="Picogrid" width="576">
</picture>

<h1>ECN SDK</h1>

[ECN](https://picogrid.com/ecn) · [Documentation](https://docs.picogrid.com/ecn-sdk/) · [Examples](https://github.com/picogrid/ecn-sdk-python/tree/main/examples) · [Security](https://github.com/picogrid/ecn-sdk-python/security/policy) · [Support](https://github.com/picogrid/ecn-sdk-python/blob/main/SUPPORT.md) · [License](https://github.com/picogrid/ecn-sdk-python/blob/main/LICENSE)

</div>

```python
print("example")
```

```console
python examples/run.py
```
"""


def _write_sdist(path: Path, contents: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name, data in sorted(contents.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def _documentation_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    repository = tmp_path / "repository"
    (repository / "docs" / "how-to").mkdir(parents=True)
    (repository / "docs" / "getting-started").mkdir()
    (repository / "examples").mkdir()
    (repository / "README.md").write_text(
        _MINIMAL_BRANDED_README,
        encoding="utf-8",
    )
    (repository / "docs" / "index.md").write_text(
        "# Public guide\n\n[Run examples](how-to/run.md)\n",
        encoding="utf-8",
    )
    (repository / "docs" / "README.md").write_text(
        """# Maintainer documentation index

```python
await client.close()
```

```console
uv run python examples/alpha.py --check
```
""",
        encoding="utf-8",
    )
    (repository / "docs" / "how-to" / "run.md").write_text(
        """# Run examples

[Alpha](../../examples/alpha.py), [Beta](../../examples/beta.py), and [Run](../../examples/run.py) are runnable.
""",
        encoding="utf-8",
    )
    (repository / "docs" / "getting-started" / "installation.md").write_text(
        """# Installation

```console
python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl
python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl ./picogrid_ecn_operator_app-0.1.0-py3-none-any.whl
```
""",
        encoding="utf-8",
    )
    for name in ("__init__.py", "_common.py", "alpha.py", "beta.py", "run.py"):
        (repository / "examples" / name).write_text("\n", encoding="utf-8")
    manifest_entries = [
        {
            "id": Path(name).stem,
            "source_path": f"examples/{name}",
            "title": Path(name).stem.title(),
            "summary": "Synthetic example.",
            "workflow": {
                "module": "picogrid_ecn_client.workflows.diagnostics",
                "function": "preflight",
            },
            "required_inputs": [],
            "documentation": ["docs/how-to/run.md"],
            "notebook_eligible": True,
            "exclusion_reason": None,
            "safety_class": "read",
            "modes": ["offline-check"],
        }
        for name in ("alpha.py", "beta.py", "run.py")
    ]
    (repository / "examples" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "examples": manifest_entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    for name in ("LICENSE", "MANIFEST.in", "SUPPORT.md", "pyproject.toml"):
        (repository / name).write_text("synthetic\n", encoding="utf-8")

    policy = dict(load_policy(POLICY_PATH))
    policy.update(
        {
            "documentation_guide": "docs/index.md",
            "documentation_maintainer_index": "docs/README.md",
            "documentation_deferred_how_tos": [],
            "documentation_deferred_how_to_examples": {},
            "sdist_documentation_files": [
                "docs/README.md",
                "docs/getting-started/installation.md",
                "docs/how-to/run.md",
                "docs/index.md",
            ],
            "sdist_auxiliary_files": ["SUPPORT.md"],
            "sdist_example_files": [
                "examples/__init__.py",
                "examples/_common.py",
                "examples/alpha.py",
                "examples/beta.py",
                "examples/manifest.json",
                "examples/run.py",
            ],
        }
    )
    (repository / "MANIFEST.in").write_text(
        "\n".join(
            f"include {name}"
            for name in (
                *policy["sdist_documentation_files"],
                *policy["sdist_example_files"],
                *policy["sdist_auxiliary_files"],
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return repository, policy


def test_valid_wheel_and_sdist_match_exact_allowlists(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_wheel(wheel, _wheel_contents(policy))
    _write_sdist(sdist, _sdist_contents(policy))

    wheel_result = inspect_wheel(wheel, policy)
    sdist_result = inspect_sdist(sdist, policy)

    assert wheel_result.artifact_type == "wheel"
    assert sdist_result.artifact_type == "sdist"
    assert wheel_result.file_count == len(_wheel_contents(policy))


def test_load_policy_requires_license_text_sha256(tmp_path: Path) -> None:
    raw_policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw_policy.pop("license_text_sha256")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    policy_path = scripts / "release-policy.json"
    policy_path.write_text(json.dumps(raw_policy), encoding="utf-8")
    scripts.joinpath("public-api-manifest.json").write_bytes(
        POLICY_PATH.with_name("public-api-manifest.json").read_bytes()
    )

    with pytest.raises(ArtifactPolicyError, match="license_text_sha256"):
        load_policy(policy_path)


def test_load_policy_requires_retired_document_references(tmp_path: Path) -> None:
    raw_policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw_policy.pop("retired_document_references")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    policy_path = scripts / "release-policy.json"
    policy_path.write_text(json.dumps(raw_policy), encoding="utf-8")
    scripts.joinpath("public-api-manifest.json").write_bytes(
        POLICY_PATH.with_name("public-api-manifest.json").read_bytes()
    )

    with pytest.raises(ArtifactPolicyError, match="retired_document_references"):
        load_policy(policy_path)


def test_wheel_rejects_retired_document_reference_in_markdown(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    markdown_member = "picogrid_ecn_client/release-notes.MD"
    policy["wheel_package_files"].append(markdown_member)
    contents = _wheel_contents(policy)
    retired_name = policy["retired_document_references"][0]
    contents[markdown_member] = f"See {retired_name.swapcase()} for details.\n".encode()
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(
        ArtifactPolicyError,
        match=rf"retired document {re.escape(retired_name)} referenced by {re.escape(markdown_member)}",
    ):
        inspect_wheel(wheel, policy)


def test_sdist_rejects_retired_document_reference_in_markdown(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    markdown_member = "picogrid_ecn_client-0.1.0/README.md"
    retired_name = policy["retired_document_references"][1]
    contents[markdown_member] = f"See {retired_name.swapcase()} for details.\n".encode()
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(
        ArtifactPolicyError,
        match=rf"retired document {re.escape(retired_name)} referenced by {re.escape(markdown_member)}",
    ):
        inspect_sdist(sdist, policy)


def test_sdist_rejects_retired_document_reference_in_mdx(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    markdown_member = "picogrid_ecn_client-0.1.0/docs/index.mdx"
    policy["sdist_documentation_files"].append("docs/index.mdx")
    contents = _sdist_contents(policy)
    retired_name = policy["retired_document_references"][2]
    contents[markdown_member] = f"See {retired_name.swapcase()} for details.\n".encode()
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(
        ArtifactPolicyError,
        match=rf"retired document {re.escape(retired_name)} referenced by {re.escape(markdown_member)}",
    ):
        inspect_sdist(sdist, policy)


def test_wheel_rejects_license_text_digest_mismatch(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    expected_digest = policy["license_text_sha256"]
    contents = _wheel_contents(policy)
    license_member = "picogrid_ecn_client-0.1.0.dist-info/licenses/LICENSE"
    replacement = b"truncated license text\n"
    contents[license_member] = replacement
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="digest mismatch") as error:
        inspect_wheel(wheel, policy)

    message = str(error.value)
    assert expected_digest in message
    assert hashlib.sha256(replacement).hexdigest() in message


@pytest.mark.parametrize(
    "license_member",
    [
        "picogrid_ecn_client-0.1.0/LICENSE",
        "picogrid_ecn_client-0.1.0/operator-app/LICENSE",
    ],
)
def test_sdist_rejects_license_text_digest_mismatch(
    tmp_path: Path,
    license_member: str,
) -> None:
    policy = load_policy(POLICY_PATH)
    expected_digest = policy["license_text_sha256"]
    contents = _sdist_contents(policy)
    replacement = b"replacement license text\n"
    contents[license_member] = replacement
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(ArtifactPolicyError, match="digest mismatch") as error:
        inspect_sdist(sdist, policy)

    message = str(error.value)
    assert expected_digest in message
    assert hashlib.sha256(replacement).hexdigest() in message


def test_sdist_limits_generated_url_exception_to_release_policy(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    root = "picogrid_ecn_client-0.1.0"
    placeholder = policy["generated_site_placeholder_urls"][0].encode()
    contents[f"{root}/README.md"] += b"\n" + placeholder + b"\n"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(ArtifactPolicyError, match="unapproved operational hostname"):
        inspect_sdist(sdist, policy)


def test_sdist_rejects_changed_public_brand_asset(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    root = "picogrid_ecn_client-0.1.0"
    relative = f"{root}/docs/site/public/brand/picogrid-nav-texture.png"
    contents[relative] += b"changed"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(ArtifactPolicyError, match="public brand asset hash changed"):
        inspect_sdist(sdist, policy)


def test_wheel_metadata_rejects_relative_long_description_link(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    contents = _wheel_contents(policy)
    metadata = "picogrid_ecn_client-0.1.0.dist-info/METADATA"
    contents[metadata] += b"\n[Broken package link](docs/index.md)\n"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="relative link"):
        inspect_wheel(wheel, policy)


def test_release_please_bootstrap_targets_the_unpublished_candidate() -> None:
    repository = POLICY_PATH.parents[1]
    config = json.loads(
        (repository / ".github/release-please-config.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (repository / ".github/.release-please-manifest.json").read_text(encoding="utf-8")
    )
    policy = load_policy(POLICY_PATH)
    operator_manifest = tomllib.loads(
        (repository / "operator-app/pyproject.toml").read_text(encoding="utf-8")
    )
    operator_package = json.loads(
        (repository / "operator-app/package.json").read_text(encoding="utf-8")
    )
    operator_lock = json.loads(
        (repository / "operator-app/package-lock.json").read_text(encoding="utf-8")
    )

    assert manifest == {}
    assert config["packages"]["."]["initial-version"] == policy["project_version"]
    assert operator_manifest["project"]["version"] == policy["project_version"]
    assert operator_package["version"] == policy["project_version"]
    assert operator_lock["version"] == policy["project_version"]
    assert operator_lock["packages"][""]["version"] == policy["project_version"]
    extra_files = config["packages"]["."]["extra-files"]
    configured_updates = {(entry["path"], entry.get("jsonpath")) for entry in extra_files}
    assert {
        ("operator-app/README.md", None),
        ("operator-app/package-lock.json", '$.packages[""].version'),
        ("operator-app/package-lock.json", "$.version"),
        ("operator-app/package.json", "$.version"),
        ("operator-app/pyproject.toml", None),
    } <= configured_updates


def test_sdist_scan_allows_reviewed_public_web_and_dependency_syntax(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    root = "picogrid_ecn_client-0.1.0"
    contents[f"{root}/docs/package-lock.json"] = b"""{
  "path": "node_modules/@astrojs/internal-helpers",
  "resolved": "https://registry.npmjs.org/@astrojs/internal-helpers/-/internal-helpers.tgz"
}
"""
    contents[f"{root}/docs/site/public/brand/ecn-client-og.svg"] = (
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n'
    )
    contents[f"{root}/operator-app/frontend/src/styles.css"] = b"dialog::backdrop {}\n"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    inspect_sdist(sdist, policy)


def test_release_documentation_inventory_covers_every_published_extension(
    tmp_path: Path,
) -> None:
    # The workspace move narrowed this inventory once. A page the site can
    # publish must reach the exact artifact whatever its extension, so an
    # unlisted one fails the gate rather than being silently dropped.
    repository, policy = _documentation_fixture(tmp_path)
    (repository / "docs" / "how-to" / "run.mdx").write_text(
        "# Run it another way\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="documentation"):
        inspect_documentation(repository, policy)


def test_release_documentation_inventory_ignores_the_workspace_tooling(
    tmp_path: Path,
) -> None:
    # The same directory holds the toolchain that builds the prose. None of it
    # ships, so none of it belongs in the shipped inventory.
    repository, policy = _documentation_fixture(tmp_path)
    for directory, name in (
        ("site", "check-built-site.mjs"),
        ("src", "content.config.ts"),
        ("cloudflare", "README.md"),
        ("node_modules", "index.js"),
        (".astro", "types.d.ts"),
    ):
        (repository / "docs" / directory).mkdir(parents=True, exist_ok=True)
        (repository / "docs" / directory / name).write_text("tooling\n", encoding="utf-8")

    assert inspect_documentation(repository, policy).documentation_files == (
        "docs/README.md",
        "docs/getting-started/installation.md",
        "docs/how-to/run.md",
        "docs/index.md",
    )


def test_readme_surface_contract_allows_logo_without_badges(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)

    inspect_documentation(repository, policy)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("picogrid-wordmark-dark.png", "wrong-dark.png", "README wordmark contract"),
        ('alt="Picogrid"', 'alt=""', "README wordmark contract"),
        ('width="576"', 'width="577"', "README wordmark contract"),
        ("https://picogrid.com/ecn", "https://picogrid.com/", "README wordmark contract"),
    ],
)
def test_readme_surface_contract_fails_closed(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    readme = repository / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match=message):
        inspect_documentation(repository, policy)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("````html\n", "\n````\n"),
        ("<!--\n", "\n-->\n"),
        ("<details><summary>Navigation</summary>\n", "\n</details>\n"),
    ],
)
def test_readme_surface_contract_rejects_hidden_markup(
    tmp_path: Path, prefix: str, suffix: str
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    readme = repository / "README.md"
    text = readme.read_text(encoding="utf-8")
    readme.write_text(f"{prefix}{text}{suffix}", encoding="utf-8")

    with pytest.raises(ArtifactPolicyError, match="README wordmark contract"):
        inspect_documentation(repository, policy)


def test_readme_surface_contract_rejects_indented_code_block(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    readme = repository / "README.md"
    text = readme.read_text(encoding="utf-8")
    readme.write_text(
        "\n".join(f"    {line}" for line in text.splitlines()) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="README wordmark contract"):
        inspect_documentation(repository, policy)


def test_release_documentation_is_exact_linked_and_parseable(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)

    inspection = inspect_documentation(repository, policy)

    assert inspection.documentation_files == (
        "docs/README.md",
        "docs/getting-started/installation.md",
        "docs/how-to/run.md",
        "docs/index.md",
    )
    assert inspection.example_files == (
        "examples/__init__.py",
        "examples/_common.py",
        "examples/alpha.py",
        "examples/beta.py",
        "examples/manifest.json",
        "examples/run.py",
    )
    assert inspection.python_snippets == 2
    assert inspection.command_blocks == 3
    assert inspection.supported_how_tos == 1


def test_release_documentation_requires_exact_deferred_example_mapping(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    deferred_page = "docs/how-to/deferred.md"
    (repository / deferred_page).write_text(
        "# Deferred workflow\n\n[Typed failure](../../examples/beta.py)\n",
        encoding="utf-8",
    )
    policy["sdist_documentation_files"].insert(2, deferred_page)
    policy["documentation_deferred_how_tos"] = [deferred_page]
    policy["documentation_deferred_how_to_examples"] = {
        deferred_page: ["examples/alpha.py"],
    }
    (repository / "MANIFEST.in").write_text(
        "\n".join(_publication_manifest_lines(policy)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactPolicyError,
        match="deferred how-to runnable examples differ from policy",
    ):
        inspect_documentation(repository, policy)

    policy["documentation_deferred_how_to_examples"][deferred_page] = ["examples/beta.py"]
    assert inspect_documentation(repository, policy).supported_how_tos == 1


def test_release_documentation_requires_exact_operator_application_inventory(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    operator = repository / "operator-app"
    operator.mkdir()
    (operator / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (operator / "README.md").write_text(
        """# Operator

```bash
picogrid-ecn operator --demo
picogrid-ecn operator --profile NAME
picogrid-ecn-operator --demo
picogrid-ecn-operator --profile NAME
docker compose up --build operator-mock
npm ci --ignore-scripts
npm run build
```
""",
        encoding="utf-8",
    )
    policy["sdist_auxiliary_files"] = ["SUPPORT.md", "operator-app/README.md"]
    (repository / "MANIFEST.in").write_text(
        (repository / "MANIFEST.in").read_text(encoding="utf-8")
        + "include operator-app/README.md\n",
        encoding="utf-8",
    )

    assert inspect_documentation(repository, policy) is not None

    (operator / "unreviewed.ts").write_text("export {};\n", encoding="utf-8")
    with pytest.raises(ArtifactPolicyError, match="operator application inventory mismatch"):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_broken_relative_links(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    index = repository / "docs" / "index.md"
    index.write_text(
        index.read_text(encoding="utf-8") + "\n[Missing](concepts/missing.md)\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="broken released link"):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_invalid_python_snippets(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    maintainer_index = repository / "docs" / "README.md"
    maintainer_index.write_text(
        maintainer_index.read_text(encoding="utf-8") + "\n```python\nif )\n```\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="invalid Python snippet"):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_invalid_command_syntax(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    maintainer_index = repository / "docs" / "README.md"
    maintainer_index.write_text(
        maintainer_index.read_text(encoding="utf-8") + "\n```console\nif then\n```\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="invalid shell syntax"):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_nonresolving_offline_install_command(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8").replace(
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
            "python -m pip install --no-index --find-links dist picogrid-ecn-client",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactPolicyError,
        match="exact wheel with dependency resolution enabled",
    ):
        inspect_documentation(repository, policy)


def test_release_documentation_accepts_source_built_wheel_install_path(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8").replace(
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
            "python -m pip install build\n"
            "python -m build --wheel\n"
            "python -m pip install ./dist/picogrid_ecn_client-0.1.0-py3-none-any.whl\n"
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
            1,
        ),
        encoding="utf-8",
    )

    assert inspect_documentation(repository, policy) is not None


def test_release_documentation_rejects_unvetted_install_command(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8").replace(
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl\n"
            "python -m pip install requests",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactPolicyError,
        match="exact wheel with dependency resolution enabled",
    ):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_repeated_wheel_install_command(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8").replace(
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl\n"
            "python -m pip install ./picogrid_ecn_client-0.1.0-py3-none-any.whl",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactPolicyError,
        match="exact wheel with dependency resolution enabled",
    ):
        inspect_documentation(repository, policy)


def test_release_documentation_accepts_pypi_canonical_install_path(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8")
        + "\n```bash\npython -m pip install picogrid-ecn-client==0.1.0\n```\n",
        encoding="utf-8",
    )

    documentation = inspect_documentation(repository, policy)
    assert documentation is not None


def test_release_documentation_accepts_offline_verified_wheelhouse_install(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8")
        + "\n```bash\npython -m pip install --no-index --find-links ./wheelhouse --require-hashes -r wheelhouse/requirements.txt\n```\n",
        encoding="utf-8",
    )

    documentation = inspect_documentation(repository, policy)
    assert documentation is not None


def test_release_documentation_accepts_pinned_upgrade_command(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8")
        + "\n```bash\npython -m pip install --upgrade picogrid-ecn-client==0.1.0\n```\n",
        encoding="utf-8",
    )

    documentation = inspect_documentation(repository, policy)
    assert documentation is not None


def test_release_documentation_rejects_unpinned_upgrade_command(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    installation = repository / "docs" / "getting-started" / "installation.md"
    installation.write_text(
        installation.read_text(encoding="utf-8")
        + "\n```bash\npython -m pip install --upgrade picogrid-ecn-client\n```\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactPolicyError,
        match="exact wheel with dependency resolution enabled",
    ):
        inspect_documentation(repository, policy)


def test_release_documentation_requires_every_how_to_to_link_an_example(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    (repository / "docs" / "how-to" / "run.md").write_text(
        "# Run examples\n\nNo runnable link.\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="does not link a runnable example"):
        inspect_documentation(repository, policy)


def test_release_documentation_inventories_example_environment_variables(
    tmp_path: Path,
) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    (repository / "examples" / "alpha.py").write_text(
        'import os\n\nvalue = os.environ["ECN_SYNTHETIC_VALUE"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ArtifactPolicyError, match="every example environment variable"):
        inspect_documentation(repository, policy)

    guide = repository / "docs" / "how-to" / "run.md"
    guide.write_text(
        guide.read_text(encoding="utf-8")
        + "\n`ECN_SYNTHETIC_VALUE` supplies the synthetic value.\n",
        encoding="utf-8",
    )
    assert inspect_documentation(repository, policy).supported_how_tos == 1


def test_release_documentation_uses_publication_secret_scanner(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    maintainer_index = repository / "docs" / "README.md"
    maintainer_index.write_bytes(maintainer_index.read_bytes() + b"\n" + _SLACK_TOKEN_CANARY)

    with pytest.raises(ArtifactPolicyError, match="provider token"):
        inspect_documentation(repository, policy)


def test_release_documentation_rejects_unreviewed_inventory_file(tmp_path: Path) -> None:
    repository, policy = _documentation_fixture(tmp_path)
    (repository / "docs" / "unreviewed.md").write_text("# Surprise\n", encoding="utf-8")

    with pytest.raises(ArtifactPolicyError, match="documentation inventory mismatch"):
        inspect_documentation(repository, policy)


def test_wheel_package_allowlist_matches_current_source_tree() -> None:
    policy = load_policy(POLICY_PATH)
    source = Path(__file__).parents[2] / "src"
    repository = source.parent
    package_source = source / "picogrid_ecn_client"
    actual = {
        path.relative_to(source).as_posix()
        for path in package_source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not any(part.endswith(".egg-info") for part in path.parts)
    }

    assert set(policy["wheel_package_files"]) == actual
    configuration = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = configuration["project"]["dependencies"]
    assert set(policy["runtime_requirements"]) == {
        canonical_root_requirement(dependency) for dependency in dependencies
    }
    assert set(policy["direct_runtime_dependencies"]) == {
        requirement_name(dependency) for dependency in dependencies
    }
    assert policy["runtime_protocol"] == "mqtt-v5-only"
    assert configuration["tool"]["setuptools"]["package-data"]["picogrid_ecn_client"] == [
        "py.typed",
        "schemas/public/*.proto",
    ]
    # Spelled out here rather than read from the production set: the policy is
    # regenerated from that set, so borrowing it would let a published directory
    # wrongly named as workspace tooling drop out of both sides at once.
    workspace = {
        ".astro",
        ".wrangler",
        "astro.config.mjs",
        "cloudflare",
        "cspell.json",
        "node_modules",
        "package-lock.json",
        "package.json",
        "site",
        "src",
        "tsconfig.json",
        "wrangler.jsonc",
    }
    documentation_root = repository / "docs"
    assert policy["sdist_documentation_files"] == sorted(
        path.relative_to(repository).as_posix()
        for path in documentation_root.rglob("*")
        if path.is_file() and path.relative_to(documentation_root).parts[0] not in workspace
    )
    assert policy["sdist_example_files"] == sorted(
        path.relative_to(repository).as_posix()
        for path in (repository / "examples").glob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "manifest.json")
    )
    documentation = inspect_documentation(repository, policy)
    assert documentation.supported_how_tos == 11


def test_protobuf_decoder_documented_environment_matches_example() -> None:
    repository = Path(__file__).parents[2]
    documentation = (repository / "docs/how-to/protobuf-decode.md").read_text(encoding="utf-8")
    example = (repository / "examples/decode_public_protobuf.py").read_text(encoding="utf-8")

    assert "export ECN_INTEGRATION_NAME=" in documentation
    assert "export ECN_OBSERVED_INTEGRATION=" not in documentation
    assert 'required_env("ECN_INTEGRATION_NAME")' in example


def test_task_acknowledgement_documentation_does_not_claim_byte_identity() -> None:
    repository = Path(__file__).parents[2]
    wire_reference = (repository / "docs/reference/wire-formats.md").read_text(encoding="utf-8")

    assert "The bytes match" not in wire_reference
    assert "Byte identity is not claimed" in wire_reference


def test_make_verify_release_selects_isolated_python_311() -> None:
    repository = Path(__file__).parents[2]
    makefile = (repository / "Makefile").read_text(encoding="utf-8")

    assert "PYTHONDONTWRITEBYTECODE=1" in makefile
    assert (
        "$(UV) run --frozen --isolated --python 3.11 python -m scripts.verify_release" in makefile
    )


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_NONPUBLIC_IMPORT_CANARY, "non-public SDK reference"),
        (_UNAPPROVED_REPOSITORY_CANARY, "unapproved Picogrid repository URL"),
        (_UNAPPROVED_SSH_REPOSITORY_CANARY, "unapproved Picogrid repository URL"),
        (_UNAPPROVED_PORT_REPOSITORY_CANARY, "unapproved Picogrid repository URL"),
        (_UNAPPROVED_SSH_PORT_REPOSITORY_CANARY, "unapproved Picogrid repository URL"),
        (_PRIVATE_API_PATH_CANARY, "private API path"),
        (
            b"https%3A%2F%2Fgithub.com%2Fpicogrid%2F" + b"unpublished-sdk",
            "unapproved Picogrid repository URL",
        ),
        (b"%252F" + b"internal%252Fstatus", "private API path"),
        (_PRIVATE_KEY_CANARY, "private key"),
        (_IPV4_CANARY, "non-loopback IPv4"),
        (_IPV6_CANARY, "non-loopback IPv6"),
        (_CREDENTIAL_URL_CANARY, "credential-bearing URL"),
        (_OPERATIONAL_FQDN_CANARY, "unapproved operational hostname"),
        (_SLACK_TOKEN_CANARY, "provider token"),
        (_SLACK_APP_TOKEN_CANARY, "provider token"),
        (_SLACK_WEBHOOK_CANARY, "provider webhook credential"),
    ],
)
def test_wheel_content_scan_rejects_prohibited_material(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/client.py"] = payload
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match=expected):
        inspect_wheel(wheel, policy)


@pytest.mark.parametrize(
    "repository",
    [
        b"https://github.com/picogrid/ecn-sdk-python.git\n",
        b"git@github.com:picogrid/legion-system-auth.git\n",
        b"https://github.com:443/picogrid/ecn-sdk-python.git\n",
        b'https://github.com/picogrid/ecn-sdk-python\\n"',
        b"ssh://git@github.com:22/picogrid/legion-system-auth.git\n",
    ],
)
def test_wheel_content_scan_allows_approved_repository_git_urls(
    tmp_path: Path,
    repository: bytes,
) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client-0.1.0.dist-info/METADATA"] += repository
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    inspect_wheel(wheel, policy)


def test_content_scan_allows_the_public_documentation_package_name() -> None:
    policy = load_policy(POLICY_PATH)

    scan_secret_and_address_content(
        "package-lock.json",
        b'{"name":"picogrid-ecn-sdk-docs"}',
        policy,
    )


@pytest.mark.parametrize(
    "scheme",
    ["http", "https", "ws", "wss", "mqtt", "mqtts", "amqp", "amqps", "ssl", "tcp", "tls"],
)
def test_network_url_scan_rejects_userinfo_for_web_and_broker_schemes(scheme: str) -> None:
    policy = load_policy(POLICY_PATH)
    payload = (scheme + ":" + "//operator@broker.ops." + "example." + "com").encode()

    with pytest.raises(ArtifactPolicyError, match="credential-bearing URL"):
        scan_secret_and_address_content("client.py", payload, policy)


def test_network_scan_allows_classifier_separator_and_loopback_ipv6() -> None:
    policy = load_policy(POLICY_PATH)

    scan_secret_and_address_content(
        "METADATA",
        b"Classifier: Development Status :: 3 - Alpha\nlisten on [::1]\n",
        policy,
    )


def test_network_scan_allows_reviewed_generated_web_syntax() -> None:
    policy = load_policy(POLICY_PATH)

    scan_secret_and_address_content(
        "pagefind.js",
        b"translator <person@example.invalid>; new URL(`https://example.com${path}`)",
        policy,
    )
    with pytest.raises(ArtifactPolicyError, match="operational hostname"):
        scan_secret_and_address_content(
            "pagefind.js", b"new URL(path, `" + _PAGEFIND_SENTINEL_URL + b"`)", policy
        )
    scan_secret_and_address_content(
        "pagefind.js",
        b"new URL(path, `" + _PAGEFIND_SENTINEL_URL + b"`)",
        policy,
        allowed_exact_urls=frozenset(policy["generated_site_placeholder_urls"]),
    )
    with pytest.raises(ArtifactPolicyError, match="operational hostname"):
        scan_secret_and_address_content(
            "pagefind.js",
            b"new URL(path, `" + _SINGLE_LABEL_HOST_CANARY + b"`)",
            policy,
            allowed_exact_urls=frozenset(policy["generated_site_placeholder_urls"]),
        )


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"endpoint=192%2E0%2E" + b"2%2E10", "non-loopback IPv4 address"),
        (_ENCODED_OPERATIONAL_URL_CANARY, "operational hostname"),
    ],
)
def test_network_scan_decodes_percent_encoded_addresses(payload: bytes, expected: str) -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(ArtifactPolicyError, match=expected):
        scan_secret_and_address_content("generated.js", payload, policy)


def test_publication_scan_decodes_html_entities() -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(ArtifactPolicyError, match="private API path"):
        release_checks.scan_publication_content(
            "site-dist/page.html",
            b"&#x2f;internal&#x2f;status",
            policy,
        )

    release_checks.scan_publication_content(
        "example.py",
        b"&#x2f;internal&#x2f;status",
        policy,
    )


@pytest.mark.parametrize(
    "name",
    ("frontend/main.ts", "frontend/style.css", "Dockerfile", ".env.example"),
)
def test_network_scan_covers_public_web_and_container_text(name: str) -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(ArtifactPolicyError, match="non-loopback IPv4 address"):
        scan_secret_and_address_content(name, b"endpoint=192.0.2." + b"10", policy)


def test_worktree_scan_rejects_secret_content_without_echoing_it(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    canary_path = tmp_path / "README.md"
    canary_path.write_bytes(_SLACK_TOKEN_CANARY)

    with pytest.raises(release_workflow.VerificationError, match="provider token") as raised:
        release_workflow._scan_worktree_paths(tmp_path, [Path("README.md")], [], policy)

    assert _SLACK_TOKEN_CANARY.decode().strip() not in str(raised.value)
    assert "README.md" not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    (
        _NONPUBLIC_IMPORT_CANARY,
        _UNAPPROVED_REPOSITORY_CANARY,
        _PRIVATE_API_PATH_CANARY,
    ),
)
def test_worktree_source_scan_allows_nonpublic_reference_fixtures(
    tmp_path: Path,
    payload: bytes,
) -> None:
    policy = load_policy(POLICY_PATH)
    candidate = tmp_path / "fixture.py"
    candidate.write_bytes(payload)

    assert (
        release_workflow._scan_worktree_paths(
            tmp_path,
            [Path("fixture.py")],
            [],
            policy,
        )["git_visible_files_scanned"]
        == 1
    )


def test_operator_screenshot_reports_publication_content_rejection(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    screenshot = tmp_path / "operator.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 1_000 + _NONPUBLIC_IMPORT_CANARY)

    with pytest.raises(
        release_workflow.VerificationError,
        match="operator publication screenshot failed publication content scan",
    ):
        release_workflow._inspect_operator_screenshot(screenshot, policy)


def test_worktree_scan_rejects_unexpected_ignored_file_even_with_innocuous_name(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY_PATH)
    ignored = tmp_path / "ordinary.txt"
    ignored.write_bytes(_PRIVATE_KEY_CANARY)

    with pytest.raises(
        release_workflow.VerificationError,
        match="unexpected-ignored-file=1",
    ):
        release_workflow._scan_worktree_paths(
            tmp_path,
            [],
            [Path("ordinary.txt")],
            policy,
        )


@pytest.mark.parametrize(
    "path",
    [
        Path(".claude/settings.local.json"),
        Path("notes/CODEX.md"),
        Path(".github/copilot-instructions.md"),
    ],
)
def test_worktree_scan_rejects_assistant_artifact_names(path: Path) -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(
        release_workflow.VerificationError,
        match="assistant-artifact-name=1",
    ):
        release_workflow._scan_worktree_paths(Path("/unused"), [path], [], policy)


def test_worktree_scan_allows_withheld_root_agents_file() -> None:
    policy = load_policy(POLICY_PATH)

    result = release_workflow._scan_worktree_paths(
        Path("/unused"),
        [Path("AGENTS.md")],
        [],
        policy,
    )

    assert result["git_visible_files_scanned"] == 0


@pytest.mark.parametrize(
    "ca_bundle",
    [
        Path(".venv/lib/python3.11/site-packages/certifi/cacert.pem"),
        Path(".venv/lib/python3.11/site-packages/pip/_vendor/certifi/cacert.pem"),
    ],
)
def test_worktree_scan_allows_only_exact_virtualenv_ca_bundles(ca_bundle: Path) -> None:
    policy = load_policy(POLICY_PATH)

    assert release_workflow._scan_worktree_paths(Path("/unused"), [], [ca_bundle], policy) == {
        "git_visible_files_scanned": 0,
        "ignored_generated_files_scanned": 0,
        "ignored_files_reviewed": 1,
    }


@pytest.mark.parametrize(
    "module",
    [
        Path(".venv/lib/python3.11/token.py"),
        Path(".venv/lib/python3.11/secrets.py"),
        Path(".venv/lib/python3.11/__pycache__/token.cpython-311.pyc"),
    ],
)
def test_worktree_scan_allows_virtualenv_secret_token_code_modules(module: Path) -> None:
    policy = load_policy(POLICY_PATH)

    assert release_workflow._scan_worktree_paths(Path("/unused"), [], [module], policy) == {
        "git_visible_files_scanned": 0,
        "ignored_generated_files_scanned": 0,
        "ignored_files_reviewed": 1,
    }


@pytest.mark.parametrize(
    "path",
    [
        Path("docs/operator-secret.txt"),
        Path(".pytest_cache/id_rsa"),
        Path(".venv/client.key"),
        Path(".venv/copied.pem"),
        Path(".venv/session.jwt"),
    ],
)
def test_worktree_scan_rejects_suspicious_ignored_names(path: Path) -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(release_workflow.VerificationError, match="credential-like-name=1"):
        release_workflow._scan_worktree_paths(Path("/unused"), [], [path], policy)


def test_worktree_scan_rejects_risk_markers_inside_generated_roots() -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(
        release_workflow.VerificationError,
        match="validation-or-internal-capture-name=1",
    ):
        release_workflow._scan_worktree_paths(
            Path("/unused"),
            [],
            [Path(".venv/cache/prod_validation-capture.txt")],
            policy,
        )


@pytest.mark.parametrize(
    "path",
    [
        Path("build/lib/picogrid_ecn_client/_transport/rest.py"),
        Path("examples/__pycache__/search_entities.cpython-314.pyc"),
        Path("src/picogrid_ecn_client.egg-info/SOURCES.txt"),
    ],
)
def test_worktree_scan_rejects_stale_project_outputs(path: Path) -> None:
    policy = load_policy(POLICY_PATH)

    with pytest.raises(
        release_workflow.VerificationError,
        match="stale-project-output=1",
    ):
        release_workflow._scan_worktree_paths(Path("/unused"), [], [path], policy)


def test_quality_tool_output_cleanup_removes_only_exact_scanned_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in release_workflow._QUALITY_TOOL_CACHE_DIRECTORIES:
        cache = repository / name
        cache.mkdir()
        (cache / "data").write_text("generated\n", encoding="utf-8")
    coverage = repository / release_workflow._QUALITY_TOOL_CACHE_FILE
    coverage.write_text("generated\n", encoding="utf-8")
    unrelated = repository / ".unrelated-cache"
    unrelated.mkdir()
    sentinel = unrelated / "keep"
    sentinel.write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    release_workflow._reset_quality_tool_outputs()

    assert not coverage.exists()
    assert all(
        not (repository / name).exists()
        for name in release_workflow._QUALITY_TOOL_CACHE_DIRECTORIES
    )
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("symlink_kind", ["root", "nested", "coverage"])
def test_quality_tool_output_cleanup_rejects_symlinks_without_partial_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink_kind: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    preserved = repository / ".pytest_cache"
    preserved.mkdir()
    preserved_sentinel = preserved / "keep"
    preserved_sentinel.write_text("keep\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    external_sentinel = external / "keep"
    external_sentinel.write_text("outside\n", encoding="utf-8")
    if symlink_kind == "root":
        (repository / ".mypy_cache").symlink_to(external, target_is_directory=True)
    elif symlink_kind == "nested":
        cache = repository / ".mypy_cache"
        cache.mkdir()
        (cache / "outside").symlink_to(external, target_is_directory=True)
    else:
        (repository / ".coverage").symlink_to(external_sentinel)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    with pytest.raises(release_workflow.VerificationError, match="symbolic link"):
        release_workflow._reset_quality_tool_outputs()

    assert preserved_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert external_sentinel.read_text(encoding="utf-8") == "outside\n"


def test_quality_gate_environment_keeps_coverage_data_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    quality_root = tmp_path / "quality"
    quality_root.mkdir(mode=0o755)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    environment = release_workflow._quality_gate_environment(
        {"ORIGINAL": "preserved"}, quality_root
    )

    assert environment == {
        "COVERAGE_FILE": str(quality_root.resolve() / "coverage-data"),
        "MYPY_CACHE_DIR": str(quality_root.resolve() / "mypy-cache"),
        "ORIGINAL": "preserved",
    }
    assert quality_root.stat().st_mode & 0o777 == 0o700

    inside_repository = repository / "quality"
    inside_repository.mkdir()
    with pytest.raises(release_workflow.VerificationError, match="outside the repository"):
        release_workflow._quality_gate_environment({}, inside_repository)


def test_python_bytecode_cleanup_removes_only_disposable_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    cache = repository / "src" / "package" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.cpython-314.pyc").write_bytes(b"synthetic")
    loose = repository / "tests" / "stale.pyc"
    loose.parent.mkdir()
    loose.write_bytes(b"synthetic")
    retained = repository / ".venv" / "lib" / "python3.11" / "site-packages" / "keep.pyc"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained")
    source = repository / "src" / "package" / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    release_workflow._reset_python_bytecode_outputs()

    assert not cache.exists()
    assert not loose.exists()
    assert retained.read_bytes() == b"retained"
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_python_bytecode_cleanup_rejects_symlink_without_deleting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    package = repository / "src" / "package"
    package.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("outside\n", encoding="utf-8")
    (package / "__pycache__").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    with pytest.raises(release_workflow.VerificationError, match="safe directory"):
        release_workflow._reset_python_bytecode_outputs()

    assert sentinel.read_text(encoding="utf-8") == "outside\n"


def test_project_egg_info_cleanup_removes_only_exact_generated_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    source_parent = repository / "src"
    package = source_parent / "picogrid_ecn_client"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    egg_info = source_parent / "picogrid_ecn_client.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("synthetic\n", encoding="utf-8")
    unrelated = source_parent / "another_project.egg-info"
    unrelated.mkdir()
    (unrelated / "PKG-INFO").write_text("retain\n", encoding="utf-8")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "PROJECT_EGG_INFO_DIRECTORY", egg_info)

    release_workflow._reset_project_egg_info_output()

    assert not egg_info.exists()
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (unrelated / "PKG-INFO").read_text(encoding="utf-8") == "retain\n"


@pytest.mark.parametrize("symlink_location", ["root", "nested"])
def test_project_egg_info_cleanup_rejects_symlinks_without_deleting_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_location: str,
) -> None:
    repository = tmp_path / "repository"
    source_parent = repository / "src"
    source_parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("outside\n", encoding="utf-8")
    egg_info = source_parent / "picogrid_ecn_client.egg-info"
    if symlink_location == "root":
        egg_info.symlink_to(external, target_is_directory=True)
    else:
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("retain\n", encoding="utf-8")
        (egg_info / "outside").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "PROJECT_EGG_INFO_DIRECTORY", egg_info)

    with pytest.raises(release_workflow.VerificationError, match=r"symbolic link|unsupported"):
        release_workflow._reset_project_egg_info_output()

    assert sentinel.read_text(encoding="utf-8") == "outside\n"
    assert egg_info.exists() or egg_info.is_symlink()


def test_local_virtual_environment_root_must_be_a_real_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external-environment"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("outside\n", encoding="utf-8")
    virtual_environment = repository / ".venv"
    virtual_environment.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    with pytest.raises(release_workflow.VerificationError, match="symbolic link"):
        release_workflow._require_safe_local_virtual_environment()

    assert sentinel.read_text(encoding="utf-8") == "outside\n"

    virtual_environment.unlink()
    virtual_environment.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(release_workflow.VerificationError, match="not a directory"):
        release_workflow._require_safe_local_virtual_environment()

    virtual_environment.unlink()
    virtual_environment.mkdir()
    release_workflow._require_safe_local_virtual_environment()


def test_root_redirect_rejects_nested_path() -> None:
    html = """\
<meta http-equiv="refresh" content="0; url=/ecn-sdk/legacy/" />
<script>location.replace('/ecn-sdk/legacy/');</script>
"""

    with pytest.raises(
        release_workflow.VerificationError,
        match="configured content mount",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


@pytest.mark.parametrize(
    "unexpected_navigation",
    [
        "location.assign('/elsewhere/');",
        "window.location = '/elsewhere/';",
    ],
)
def test_root_redirect_rejects_additional_navigation_target(
    unexpected_navigation: str,
) -> None:
    html = f"""\
<meta http-equiv="refresh" content="0; url=/ecn-sdk/" />
<script>{unexpected_navigation}</script>
"""

    with pytest.raises(
        release_workflow.VerificationError,
        match="configured content mount",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


@pytest.mark.parametrize(
    "target",
    [
        "https://example.invalid/ecn-sdk/",
        "//example.invalid/ecn-sdk/",
    ],
)
def test_root_redirect_rejects_off_origin_target(target: str) -> None:
    html = f'<meta http-equiv="refresh" content="0; url={target}" />'

    with pytest.raises(
        release_workflow.VerificationError,
        match="origin-relative",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


def test_root_redirect_rejects_off_origin_unquoted_meta_despite_valid_script() -> None:
    html = """\
<meta http-equiv=refresh content="0; url=https://example.invalid/ecn-sdk/" />
<script>location.replace('/ecn-sdk/');</script>
"""

    with pytest.raises(
        release_workflow.VerificationError,
        match="origin-relative",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


def test_root_redirect_rejects_duplicate_meta_attributes() -> None:
    html = """\
<meta http-equiv=refresh
      content="0; url=https://example.invalid/ecn-sdk/"
      content="0; url=/ecn-sdk/" />
<script>location.replace('/ecn-sdk/');</script>
"""

    with pytest.raises(
        release_workflow.VerificationError,
        match="unparseable navigation target",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


def test_root_redirect_accepts_origin_relative_targets() -> None:
    html = """\
<meta http-equiv=refresh content="0; url=/ecn-sdk/" />
<script>location.replace('/ecn-sdk/');</script>
"""

    release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


@pytest.mark.parametrize(
    "html",
    [
        (
            '<meta http-equiv="refresh" content="0; url=/ecn-sdk/" />'
            "<script>location.assign(target);</script>"
        ),
        (
            '<meta http-equiv="refresh" content="0" />'
            "<script>location.replace('/ecn-sdk/');</script>"
        ),
        (
            '<meta http-equiv="refresh" content="0; url=/ecn-sdk/" />'
            "<script>location.replace('/ecn-sdk/' + '../elsewhere/');</script>"
        ),
        (
            '<meta http-equiv="refresh" content="0; url=/ecn-sdk/;legacy" />'
            "<script>location.replace('/ecn-sdk/');</script>"
        ),
        (
            '<meta http-equiv="refresh" content="0; url=/ecn-sdk/ legacy" />'
            "<script>location.replace('/ecn-sdk/');</script>"
        ),
    ],
)
def test_root_redirect_rejects_unparseable_navigation_target(html: str) -> None:
    with pytest.raises(
        release_workflow.VerificationError,
        match="unparseable navigation target",
    ):
        release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


def test_documentation_base_path_is_loaded_and_normalized(tmp_path: Path) -> None:
    config = tmp_path / "site-config.mjs"
    config.write_text(
        "export const documentationBasePath = '/docs-mount/';\n",
        encoding="utf-8",
    )

    assert release_checks.load_documentation_base_path(config) == "/docs-mount"


@pytest.mark.parametrize(
    "source",
    [
        "export const otherSetting = '/docs-mount';\n",
        "export const documentationBasePath = 'docs-mount';\n",
        "export const documentationBasePath = '/docs-mount//';\n",
        "export const documentationBasePath = '//docs-mount';\n",
        "export const documentationBasePath = '/docs//mount';\n",
    ],
)
def test_documentation_base_path_fails_closed(
    tmp_path: Path,
    source: str,
) -> None:
    config = tmp_path / "site-config.mjs"
    config.write_text(source, encoding="utf-8")

    with pytest.raises(ArtifactPolicyError, match="documentationBasePath"):
        release_checks.load_documentation_base_path(config)


def test_static_site_inspection_honors_nondefault_mount(tmp_path: Path) -> None:
    site = tmp_path / "site-dist"
    mount = "/docs/guide"
    required = {
        "index.html",
        "404.html",
        "docs/guide/404.html",
        "docs/guide/brand/ecn-client-og.png",
        "docs/guide/index.html",
        "docs/guide/operator-mock-light.png",
        "docs/guide/operator-mock-mobile-dark.png",
        "docs/guide/operator-mock-mobile-light.png",
        "docs/guide/operator-mock.png",
        "docs/guide/pagefind/pagefind.js",
        "docs/guide/sitemap-index.xml",
    }
    policy = load_policy(POLICY_PATH)
    required.update(f"docs/guide/{name}" for name in policy["public_brand_assets"])
    for relative in required:
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    repository = POLICY_PATH.parents[1]
    for name in policy["public_brand_assets"]:
        (site / "docs/guide" / name).write_bytes(
            (repository / "docs" / "site" / "public" / name).read_bytes()
        )
    (site / "docs/guide/pagefind/pagefind.js").write_bytes(
        b"new URL(path, `" + _PAGEFIND_SENTINEL_URL + b"`)"
    )
    (site / "index.html").write_text(
        '<meta http-equiv="refresh" content="0; url=/docs/guide/" />'
        "<script>location.replace('/docs/guide/');</script>",
        encoding="utf-8",
    )

    inspection = release_workflow._inspect_static_site(
        site,
        policy,
        mount,
    )

    assert set(inspection["files"]) == required


def test_root_redirect_accepts_generated_template() -> None:
    template = (Path(__file__).parents[2] / "docs" / "site" / "root-redirect.html").read_text(
        encoding="utf-8"
    )
    html = template.replace("__CANONICAL__", "https://example.invalid/ecn-sdk/").replace(
        "__MOUNT__", "/ecn-sdk/"
    )

    release_workflow._require_root_redirect_targets(html, "/ecn-sdk/")


def test_root_redirect_rejects_html_without_navigation_target() -> None:
    with pytest.raises(
        release_workflow.VerificationError,
        match="no extractable navigation target",
    ):
        release_workflow._require_root_redirect_targets(
            "<html><head><title>Documentation</title></head></html>",
            "/ecn-sdk/",
        )


def test_local_web_output_cleanup_clears_the_pre_move_root_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout that predates the docs-workspace move still holds the root
    # outputs the toolchain used to write. They belong to no tool now, so the
    # worktree scan would refuse the run until they are cleaned.
    repository = tmp_path / "repository"
    repository.mkdir()
    stale = []
    for relative in (".astro", "node_modules", "docs/.astro", "docs/node_modules", "site-dist"):
        directory = repository / relative
        directory.mkdir(parents=True)
        (directory / "generated").write_text("generated\n", encoding="utf-8")
        stale.append(directory)
    keep = repository / "docs" / "index.md"
    keep.write_text("# Guide\n", encoding="utf-8")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    release_workflow._reset_local_web_outputs()

    assert [directory for directory in stale if directory.exists()] == []
    assert keep.read_text(encoding="utf-8") == "# Guide\n"


def test_verify_release_clears_disposable_outputs_before_worktree_scan() -> None:
    source = Path(release_workflow.__file__).read_text(encoding="utf-8")
    entrypoint = source[source.index("def verify_release(") :]

    scan = entrypoint.index("worktree_scan = _scan_git_visible_worktree")
    for cleanup in (
        "_reset_generated_reports(policy)",
        "_reset_candidate_artifacts(policy)",
        "_reset_quality_tool_outputs()",
        "_reset_python_bytecode_outputs()",
        "_reset_project_egg_info_output()",
        "_reset_local_web_outputs()",
    ):
        assert entrypoint.index(cleanup) < scan


def test_verify_release_guards_quality_outputs_and_input_drift_through_success() -> None:
    source = Path(release_workflow.__file__).read_text(encoding="utf-8")
    entrypoint = source[source.index("def verify_release(") :]

    # The gates now run as a parallel DAG, so "after quality, before build" is no
    # longer expressible as source order. The guarantee that survives is that the
    # whole parallel region is bracketed by drift guards, and that quality's
    # repository side effects are cleaned before the final worktree scan.
    region = entrypoint.index("results = run_stages(")
    pre_region_drift = entrypoint.rindex("_require_verification_inputs_unchanged", 0, region)
    post_region_virtual_environment = entrypoint.index(
        "_require_safe_local_virtual_environment()", region
    )
    post_region_egg_info = entrypoint.index("_reset_project_egg_info_output()", region)
    post_region_drift = entrypoint.index("_require_verification_inputs_unchanged", region)
    final_scan = entrypoint.index("final_worktree_scan = _scan_git_visible_worktree")
    report_inspection = entrypoint.index("_inspect_generated_reports(policy)")
    final_drift = entrypoint.rindex("_require_verification_inputs_unchanged")

    assert pre_region_drift < region < post_region_drift
    assert region < post_region_virtual_environment < final_scan
    assert region < post_region_egg_info < post_region_drift < final_scan
    assert report_inspection < final_drift


def test_release_stage_graph_is_acyclic_and_fully_declared() -> None:
    dependencies = release_workflow._STAGE_DEPENDENCIES

    for stage, parents in dependencies.items():
        assert len(set(parents)) == len(parents), stage
        for parent in parents:
            assert parent in dependencies, (stage, parent)

    settled: set[str] = set()
    while len(settled) < len(dependencies):
        ready = {
            stage
            for stage, parents in dependencies.items()
            if stage not in settled and set(parents) <= settled
        }
        assert ready, f"cycle among {set(dependencies) - settled}"
        settled |= ready


def test_release_stage_graph_shares_browser_install_with_docs_and_operator() -> None:
    dependencies = release_workflow._STAGE_DEPENDENCIES

    assert dependencies["site_ci1"] == ("build_inputs",)
    assert dependencies["browsers"] == ("site_ci1",)
    assert {"site_ci1", "browsers"} <= set(dependencies["docs"])
    # The operator gate's browser-dependent phases live in `operator_web`, so the
    # single shared Chromium download must precede that stage. Keeping the
    # browser-independent `operator` work off `browsers` lets it run during the
    # download instead of queueing behind it.
    assert "browsers" in dependencies["operator_web"]
    assert "operator" in dependencies["operator_web"]
    assert "browsers" not in dependencies["operator"]


def test_operator_gate_covers_every_advertised_python_runtime() -> None:
    dependencies = release_workflow._STAGE_DEPENDENCIES
    runtime_stages = {"py312", "py313", "py314"}

    assert runtime_stages <= set(dependencies["operator_web"])
    for stage in runtime_stages:
        assert "operator" in dependencies[stage]


def test_playwright_browser_path_is_shared_by_all_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_workflow.shutil, "which", lambda executable: f"/bin/{executable}")
    calls: list[tuple[Sequence[str], object]] = []

    def capture(command: Sequence[str], **kwargs: object) -> str:
        calls.append((command, kwargs["cwd"]))
        return "v24.0.0" if command[1] == "--version" else ""

    monkeypatch.setattr(release_workflow, "_run", capture)
    monkeypatch.setattr(
        release_workflow,
        "_require_shared_playwright_versions",
        lambda _source_snapshot: None,
    )
    expected = release_workflow._playwright_browsers_path(tmp_path)

    _, site_environment = release_workflow._site_tooling(
        tmp_path,
        {},
        git_commit="0" * 40,
        git_tag="",
    )
    installed_path = release_workflow._install_docs_browsers(
        tmp_path,
        tmp_path / "snapshot",
        tmp_path / "context",
        environment={},
        git_commit="0" * 40,
        git_tag="",
    )

    assert site_environment["PLAYWRIGHT_BROWSERS_PATH"] == str(expected)
    assert installed_path == expected
    assert calls[-1] == (
        ["/bin/npm", "run", "docs:test:browser:install"],
        tmp_path / "context" / "docs",
    )
    assert (
        "_playwright_browsers_path"
        in release_workflow._run_operator_application_gate.__code__.co_names
    )


def test_site_tooling_propagates_legion_documentation_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_workflow.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(release_workflow, "_run", lambda *args, **kwargs: "v24.0.0")
    legion_inputs = {
        "LEGION_DOCS_URL": "https://docs.picogrid.com/reference/legion-2.4",
        "LEGION_DOCS_VERSION": "2.4",
    }

    _, site_environment = release_workflow._site_tooling(
        tmp_path,
        legion_inputs,
        git_commit="0" * 40,
        git_tag="v1.0.0",
    )

    assert {name: site_environment[name] for name in legion_inputs} == legion_inputs


@pytest.mark.parametrize("actual_expression", [None, "Apache-2.0"])
def test_operator_wheel_rejects_missing_or_changed_license_expression(
    tmp_path: Path, actual_expression: str | None
) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = tmp_path / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    metadata = "Metadata-Version: 2.4\nName: picogrid-ecn-operator-app\nVersion: 0.1.0\n"
    if actual_expression is not None:
        metadata += f"License-Expression: {actual_expression}\n"
    _write_wheel(
        wheel,
        {
            "picogrid_ecn_operator_app-0.1.0.dist-info/METADATA": metadata.encode(),
            "picogrid_ecn_operator_app-0.1.0.dist-info/licenses/LICENSE": (_CANONICAL_LICENSE_TEXT),
        },
    )

    with pytest.raises(
        release_workflow.VerificationError,
        match=(
            rf"{wheel.name}.*expected {policy['license_expression']!r}, "
            rf"got {actual_expression!r}"
        ),
    ):
        release_workflow._validate_operator_wheel_license(
            wheel, POLICY_PATH.parents[1] / "LICENSE", policy
        )


def test_operator_wheel_rejects_a_license_outside_its_own_dist_info(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = tmp_path / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.4\nName: picogrid-ecn-operator-app\nVersion: 0.1.0\n"
        f"License-Expression: {policy['license_expression']}\n"
    )
    _write_wheel(
        wheel,
        {
            "picogrid_ecn_operator_app-0.1.0.dist-info/METADATA": metadata.encode(),
            "other-0.1.0.dist-info/licenses/LICENSE": _CANONICAL_LICENSE_TEXT,
        },
    )

    with pytest.raises(
        release_workflow.VerificationError,
        match="must contain exactly one license member at",
    ):
        release_workflow._validate_operator_wheel_license(
            wheel, POLICY_PATH.parents[1] / "LICENSE", policy
        )


def test_installed_operator_console_health_check_uses_shared_port_constant() -> None:
    module = ast.parse(Path(release_workflow.__file__).read_text(encoding="utf-8"))
    smoke = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_run_installed_operator_console_smoke"
    )
    connections = [
        node
        for node in ast.walk(smoke)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "socket"
        and node.func.attr == "create_connection"
    ]

    assert len(connections) == 3
    for connection in connections:
        address = connection.args[0]
        assert isinstance(address, ast.Tuple)
        port = address.elts[1]
        assert isinstance(port, ast.Name)
        assert port.id == "OPERATOR_CONSOLE_PORT"


def test_installed_operator_pytest_disables_bytecode_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        release_workflow,
        "_probe_installed_operator_application",
        lambda *_args, **_kwargs: {"python": "3.12.1"},
    )
    monkeypatch.setattr(release_workflow, "_install_requirements", lambda *_args, **_kwargs: None)

    def capture(command: Sequence[str], **_: object) -> str:
        calls.append(tuple(command))
        return ""

    monkeypatch.setattr(release_workflow, "_run", capture)

    release_workflow._run_installed_operator_python_suite(
        Path("/isolated/bin/python"),
        tmp_path,
        tmp_path / "runtime",
        expected_python_minor="3.12",
        uv="uv",
        environment={},
    )

    assert calls == [
        (
            "/isolated/bin/python",
            "-I",
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            "pyproject.toml",
            "backend/tests",
        )
    ]


def test_shared_playwright_versions_must_match(tmp_path: Path) -> None:
    def write_lock(path: Path, test_version: str, core_version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "packages": {
                        "node_modules/@playwright/test": {"version": test_version},
                        "node_modules/playwright-core": {"version": core_version},
                    }
                }
            ),
            encoding="utf-8",
        )

    write_lock(tmp_path / "docs/package-lock.json", "1.62.1", "1.62.1")
    write_lock(tmp_path / "operator-app/package-lock.json", "1.62.1", "1.62.1")
    release_workflow._require_shared_playwright_versions(tmp_path)

    write_lock(tmp_path / "operator-app/package-lock.json", "1.63.0", "1.63.0")

    with pytest.raises(
        release_workflow.VerificationError,
        match=(
            r"shared Playwright install requires matching versions.*"
            r"root.*@playwright/test=1\.62\.1.*playwright-core=1\.62\.1.*"
            r"operator.*@playwright/test=1\.63\.0.*playwright-core=1\.63\.0"
        ),
    ):
        release_workflow._require_shared_playwright_versions(tmp_path)


def test_verify_release_jobs_defaults_when_unset_empty_or_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_workflow.os, "cpu_count", lambda: 6)

    for value in (None, "", "  ", "0", " 0 "):
        if value is None:
            monkeypatch.delenv("VERIFY_RELEASE_JOBS", raising=False)
        else:
            monkeypatch.setenv("VERIFY_RELEASE_JOBS", value)
        assert release_workflow._resolve_jobs() == 6


def test_verify_release_jobs_accepts_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIFY_RELEASE_JOBS", "1")

    assert release_workflow._resolve_jobs() == 1


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("two", r"VERIFY_RELEASE_JOBS must be an integer, got 'two'"),
        ("-1", r"VERIFY_RELEASE_JOBS must be at least 1"),
    ],
)
def test_verify_release_jobs_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str, message: str
) -> None:
    monkeypatch.setenv("VERIFY_RELEASE_JOBS", value)

    with pytest.raises(release_workflow.VerificationError, match=message):
        release_workflow._resolve_jobs()


@pytest.mark.parametrize(
    "relative",
    [
        Path("docs/guide.md"),
        Path("src/picogrid_ecn_client/client.py"),
        Path("tests/unit/test_client.py"),
        Path("examples/watch.py"),
        Path("scripts/release-policy.json"),
        Path("pyproject.toml"),
        Path("uv.lock"),
    ],
)
def test_verified_input_digest_detects_content_drift_across_release_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    repository = tmp_path / "repository"
    inventory = (
        Path("docs/guide.md"),
        Path("src/picogrid_ecn_client/client.py"),
        Path("tests/unit/test_client.py"),
        Path("examples/watch.py"),
        Path("scripts/release-policy.json"),
        Path("pyproject.toml"),
        Path("uv.lock"),
    )
    for path in inventory:
        candidate = repository / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(f"original {path.as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(
        release_workflow,
        "_git_paths",
        lambda _arguments, _environment: inventory,
    )
    expected = release_workflow._verification_input_digest({})
    release_workflow._require_verification_inputs_unchanged(expected, {})

    (repository / relative).write_text("changed\n", encoding="utf-8")

    with pytest.raises(release_workflow.VerificationError, match="inputs changed"):
        release_workflow._require_verification_inputs_unchanged(expected, {})


def test_verified_input_digest_detects_inventory_addition_and_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    original = Path("README.md")
    (repository / original).write_text("original\n", encoding="utf-8")
    inventory = [original]
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(
        release_workflow,
        "_git_paths",
        lambda _arguments, _environment: tuple(inventory),
    )
    expected = release_workflow._verification_input_digest({})

    added = Path("NEW.md")
    (repository / added).write_text("new\n", encoding="utf-8")
    inventory.append(added)
    with pytest.raises(release_workflow.VerificationError, match="inputs changed"):
        release_workflow._require_verification_inputs_unchanged(expected, {})

    inventory.remove(added)
    (repository / added).unlink()
    (repository / original).unlink()
    with pytest.raises(release_workflow.VerificationError, match="missing"):
        release_workflow._require_verification_inputs_unchanged(expected, {})


def test_verified_input_digest_rejects_symlinked_input_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "guide.md").write_text("outside\n", encoding="utf-8")
    (repository / "docs").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(
        release_workflow,
        "_git_paths",
        lambda _arguments, _environment: (Path("docs/guide.md"),),
    )

    with pytest.raises(release_workflow.VerificationError, match="not a regular file"):
        release_workflow._verification_input_digest({})


def test_worktree_scan_fails_closed_on_git_visible_symlink(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    (tmp_path / "outside.txt").write_text("synthetic\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(tmp_path / "outside.txt")

    with pytest.raises(release_workflow.VerificationError, match="symbolic link"):
        release_workflow._scan_worktree_paths(tmp_path, [Path("README.md")], [], policy)


@pytest.mark.parametrize(
    "payload",
    [
        b"retired endpoint /v" + b"1/status\n",
        b"incorrect PLI expansion: platform-" + b"location indicator\n",
        b"incorrect PLI expansion: platform " + b"location indicator\n",
    ],
)
def test_worktree_scan_rejects_retired_protocol_markers(
    tmp_path: Path,
    payload: bytes,
) -> None:
    policy = load_policy(POLICY_PATH)
    canary_path = tmp_path / "README.md"
    canary_path.write_bytes(payload)

    with pytest.raises(release_workflow.VerificationError, match="retired protocol marker"):
        release_workflow._scan_worktree_paths(tmp_path, [Path("README.md")], [], policy)


def test_worktree_scan_does_not_treat_lockfile_integrity_as_an_endpoint(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY_PATH)
    lockfile = tmp_path / "docs/package-lock.json"
    lockfile.parent.mkdir()
    lockfile.write_bytes(b'{"integrity":"sha512-AbCd/' + b'v3XyZ=="}\n')

    result = release_workflow._scan_worktree_paths(
        tmp_path, [Path("docs/package-lock.json")], [], policy
    )

    assert result["git_visible_files_scanned"] == 1


@pytest.mark.parametrize(
    ("relative", "payload"),
    [
        (Path(".pytest_cache/v/cache/nodeids"), b"tests/test_transport_" + b"rest.py"),
        (Path(".mypy_cache/3.11/module.meta.json"), b"import aio" + b"http"),
        (Path(".ruff_cache/content"), b"protocol-" + b"manifest.json"),
        (Path("reports/generated/old.json"), b'"path": "/v' + b'1/capabilities"'),
    ],
)
def test_worktree_scan_rejects_retired_content_inside_ignored_generated_files(
    tmp_path: Path, relative: Path, payload: bytes
) -> None:
    policy = load_policy(POLICY_PATH)
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(payload)

    with pytest.raises(
        release_workflow.VerificationError,
        match="ignored generated output failed retired protocol marker scan",
    ) as raised:
        release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy)

    assert relative.as_posix() not in str(raised.value)
    assert payload.decode() not in str(raised.value)


def test_ignored_generated_content_allows_non_sensitive_source_path(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    relative = Path(".coverage")
    (tmp_path / relative).write_bytes(b"examples/watch_tracks.py")

    assert (
        release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy)[
            "ignored_generated_files_scanned"
        ]
        == 1
    )


def test_ignored_generated_content_scan_allows_stdlib_http_references(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    relative = Path(".mypy_cache/3.11/http/client.meta.json")
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"import http" + b".client\n")

    assert release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy) == {
        "git_visible_files_scanned": 0,
        "ignored_generated_files_scanned": 1,
        "ignored_files_reviewed": 1,
    }


def test_ignored_generated_content_uses_publication_boundary_scanner(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY_PATH)
    relative = Path("reports/generated/coverage.json")
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(_NONPUBLIC_IMPORT_CANARY)

    with pytest.raises(
        release_workflow.VerificationError,
        match="ignored generated report failed non-public SDK reference scan",
    ) as raised:
        release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy)

    assert relative.as_posix() not in str(raised.value)
    assert _NONPUBLIC_IMPORT_CANARY.decode().strip() not in str(raised.value)


def test_ignored_generated_report_content_uses_the_secret_scanner(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    relative = Path("reports/generated/coverage.json")
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(_PRIVATE_KEY_CANARY)

    with pytest.raises(
        release_workflow.VerificationError,
        match="ignored generated report failed private key scan",
    ) as raised:
        release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy)

    assert relative.as_posix() not in str(raised.value)
    assert _PRIVATE_KEY_CANARY.decode().strip() not in str(raised.value)


def test_ignored_tool_cache_content_uses_the_secret_scanner(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    relative = Path(".pytest_cache/v/cache/nodeids")
    candidate = tmp_path / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(_PRIVATE_KEY_CANARY)

    with pytest.raises(
        release_workflow.VerificationError,
        match="ignored generated output failed private key scan",
    ) as raised:
        release_workflow._scan_worktree_paths(tmp_path, [], [relative], policy)

    assert relative.as_posix() not in str(raised.value)
    assert _PRIVATE_KEY_CANARY.decode().strip() not in str(raised.value)


def test_generated_report_scan_requires_exact_safe_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy(POLICY_PATH)
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(release_workflow, "REPORT_DIRECTORY", reports)
    for name in release_workflow._GENERATED_REPORT_FILES:
        (reports / name).write_text("{}\n", encoding="utf-8")

    release_workflow._inspect_generated_reports(policy)

    (reports / "unexpected.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(release_workflow.VerificationError, match="allowlist mismatch"):
        release_workflow._inspect_generated_reports(policy)
    (reports / "unexpected.json").unlink()

    secret_report = reports / "coverage.json"
    secret_report.write_bytes(_PRIVATE_KEY_CANARY)
    with pytest.raises(release_workflow.VerificationError, match="private key") as raised:
        release_workflow._inspect_generated_reports(policy)
    assert _PRIVATE_KEY_CANARY.decode().strip() not in str(raised.value)


def test_preexisting_generated_reports_are_inspected_before_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_policy(POLICY_PATH)
    reports = tmp_path / "reports" / "generated"
    reports.mkdir(parents=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", tmp_path)
    monkeypatch.setattr(release_workflow, "REPORT_DIRECTORY", reports)
    for name in ("coverage.json", "provenance.json"):
        (reports / name).write_text("{}\n", encoding="utf-8")

    release_workflow._reset_generated_reports(policy)

    assert reports.is_dir()
    assert not tuple(reports.iterdir())

    sensitive = reports / "coverage.json"
    sensitive.write_bytes(_PRIVATE_KEY_CANARY)
    with pytest.raises(release_workflow.VerificationError, match="private key") as raised:
        release_workflow._reset_generated_reports(policy)

    assert sensitive.read_bytes() == _PRIVATE_KEY_CANARY
    assert _PRIVATE_KEY_CANARY.decode().strip() not in str(raised.value)


def test_generated_report_reset_creates_missing_safe_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_policy(POLICY_PATH)
    repository = tmp_path / "repository"
    repository.mkdir()
    reports = repository / "reports"
    generated = reports / "generated"
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "REPORT_DIRECTORY", generated)

    release_workflow._reset_generated_reports(policy)

    assert reports.is_dir()
    assert generated.is_dir()
    assert not tuple(generated.iterdir())


def test_generated_report_reset_rejects_symlinked_parent_without_deleting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_policy(POLICY_PATH)
    repository = tmp_path / "repository"
    repository.mkdir()
    external_reports = tmp_path / "external-reports"
    generated = external_reports / "generated"
    generated.mkdir(parents=True)
    sentinel = generated / "coverage.json"
    sentinel.write_text("{}\n", encoding="utf-8")
    (repository / "reports").symlink_to(external_reports, target_is_directory=True)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "REPORT_DIRECTORY", repository / "reports/generated")

    with pytest.raises(release_workflow.VerificationError, match="unsafe report parent"):
        release_workflow._reset_generated_reports(policy)

    assert sentinel.read_text(encoding="utf-8") == "{}\n"


def test_release_boundary_refuses_a_dirty_worktree() -> None:
    with pytest.raises(
        release_workflow.VerificationError,
        match="release verification requires a clean Git worktree",
    ):
        release_workflow._require_clean_release_worktree(" M src/client.py")


def test_release_boundary_accepts_a_clean_worktree() -> None:
    assert release_workflow._require_clean_release_worktree("") is None


def test_release_tag_accepts_the_policy_version() -> None:
    assert release_workflow._release_tag({"RELEASE_TAG": "  v0.1.0  "}, "0.1.0") == "v0.1.0"


def test_release_tag_preserves_the_untagged_path() -> None:
    assert release_workflow._release_tag({}, "0.1.0") == ""
    assert release_workflow._release_tag({"RELEASE_TAG": ""}, "0.1.0") == ""


def test_release_tag_refuses_a_tag_for_another_version() -> None:
    with pytest.raises(
        release_workflow.VerificationError,
        match=r"release tag 'v0\.1\.1' does not match policy project version '0\.1\.0'",
    ):
        release_workflow._release_tag({"RELEASE_TAG": "v0.1.1"}, "0.1.0")


def test_release_tag_refuses_an_unusual_tag_name() -> None:
    with pytest.raises(release_workflow.VerificationError, match="ordinary Git tag name"):
        release_workflow._release_tag({"RELEASE_TAG": "-v0.1.0 rc"}, "0.1.0")


def test_release_tag_ignores_a_tag_the_checkout_happens_to_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built guide depends on the commit, not on when the build ran."""

    def refuse(*_arguments: object, **_keywords: object) -> str:
        raise AssertionError("the release tag must be injected, not read from the checkout")

    monkeypatch.setattr(release_workflow, "_git_value", refuse)

    assert release_workflow._release_tag({}, "0.1.0") == ""


def test_final_provenance_uses_captured_verified_input_materials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    operator_wheel = tmp_path / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    operator_wheel.write_bytes(b"operator wheel")
    inspections = (
        ArtifactInspection(
            artifact=wheel.name,
            artifact_type="wheel",
            file_count=1,
            files=("package.py",),
            sha256="a" * 64,
            checks=("synthetic",),
        ),
        ArtifactInspection(
            artifact=sdist.name,
            artifact_type="sdist",
            file_count=1,
            files=("package.py",),
            sha256="b" * 64,
            checks=("synthetic",),
        ),
        ArtifactInspection(
            artifact=operator_wheel.name,
            artifact_type="operator-wheel",
            file_count=1,
            files=("operator_app/__init__.py",),
            sha256="c" * 64,
            checks=("synthetic",),
        ),
    )
    documentation = DocumentationInspection(
        command_blocks=1,
        documentation_files=("docs/README.md",),
        example_files=("examples/demo.py",),
        link_count=1,
        python_snippets=1,
        supported_how_tos=1,
    )
    probe = {
        "import_origin": "/isolated/site-packages/picogrid_ecn_client/__init__.py",
        "project_name": "picogrid-ecn-client",
        "project_version": "0.1.0",
        "python": "3.11",
        "repository_on_sys_path": False,
    }
    verified_inputs_digest = "1" * 64
    build_requirements_digest = "2" * 64
    release_policy_digest = "3" * 64
    uv_lock_digest = "4" * 64
    source_digest = "5" * 64
    operator_inspection = {
        "operator_application_sha256": "8" * 64,
        "operator_build_requirements_sha256": "7" * 64,
        "operator_package_lock_sha256": "9" * 64,
        "operator_wheel_sha256": hashlib.sha256(b"operator wheel").hexdigest(),
    }
    monkeypatch.setattr(release_workflow, "REPORT_DIRECTORY", reports)
    monkeypatch.setattr(
        release_workflow,
        "_git_value",
        lambda *_arguments, **_keywords: pytest.fail("live Git metadata was reread"),
    )

    release_workflow._write_final_reports(
        wheel=wheel,
        sdist=sdist,
        operator_wheel=operator_wheel,
        source_digest=source_digest,
        reproducible_digests={
            wheel.name: "a" * 64,
            sdist.name: "b" * 64,
            operator_wheel.name: "c" * 64,
        },
        inspections=inspections,
        documentation=documentation,
        operator_inspection=operator_inspection,
        site_inspection={
            "byte_for_byte_rebuild": True,
            "file_count": 1,
            "files": ["index.html"],
            "package_lock_sha256": "6" * 64,
            "sha256": "7" * 64,
            "total_bytes": 1,
        },
        source_date_epoch=release_workflow.DEFAULT_SOURCE_DATE_EPOCH,
        probe=probe,
        python_312_probe={**probe, "python": "3.12"},
        python_313_probe={**probe, "python": "3.13"},
        python_314_probe={**probe, "python": "3.14"},
        worktree_scan={"git_visible_files_scanned": 1},
        verified_inputs_digest=verified_inputs_digest,
        build_requirements_digest=build_requirements_digest,
        release_policy_digest=release_policy_digest,
        uv_lock_digest=uv_lock_digest,
        git_commit="captured-commit",
        git_tag="v9.9.9",
        git_worktree_dirty=True,
    )

    provenance = json.loads((reports / "provenance.json").read_text(encoding="utf-8"))
    assert "generated_at" not in provenance
    assert provenance["reproducibility_epoch"] == "2025-01-01T00:00:00+00:00"
    assert provenance["invocation"] == {
        "build_requirements_sha256": build_requirements_digest,
        "release_policy_sha256": release_policy_digest,
        "source_date_epoch": release_workflow.DEFAULT_SOURCE_DATE_EPOCH,
        "uv_lock_sha256": uv_lock_digest,
        "verified_inputs_sha256": verified_inputs_digest,
    }
    assert provenance["materials"] == {
        "git_commit": "captured-commit",
        "git_tag": "v9.9.9",
        "git_worktree_dirty": True,
        "operator_application_sha256": "8" * 64,
        "operator_build_requirements_sha256": "7" * 64,
        "operator_package_lock_sha256": "9" * 64,
        "site_tree_sha256": "7" * 64,
        "source_snapshot_sha256": source_digest,
        "verified_inputs_sha256": verified_inputs_digest,
    }
    expected_subjects = {
        wheel.name: hashlib.sha256(b"wheel").hexdigest(),
        sdist.name: hashlib.sha256(b"sdist").hexdigest(),
        operator_wheel.name: hashlib.sha256(b"operator wheel").hexdigest(),
    }
    assert provenance["subjects"] == expected_subjects
    checksums = (reports / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    assert checksums == [f"{digest}  {name}" for name, digest in sorted(expected_subjects.items())]


def test_wheel_allowlist_rejects_unreviewed_file(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/new_escape_hatch.py"] = b""
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="allowlist mismatch"):
        inspect_wheel(wheel, policy)


def test_wheel_metadata_rejects_dependency_bound_drift(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    metadata = "picogrid_ecn_client-0.1.0.dist-info/METADATA"
    contents[metadata] = contents[metadata].replace(
        _policy_requirement(policy, "aiomqtt"), b"aiomqtt>=1"
    )
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="runtime requirements changed"):
        inspect_wheel(wheel, policy)


def test_sdist_metadata_rejects_dependency_bound_drift(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    metadata = "picogrid_ecn_client-0.1.0/PKG-INFO"
    contents[metadata] = contents[metadata].replace(
        _policy_requirement(policy, "protobuf"), b"protobuf>=1"
    )
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(ArtifactPolicyError, match="runtime requirements changed"):
        inspect_sdist(sdist, policy)


def test_sdist_rejects_manifest_that_drifts_from_exact_publication_inventory(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _sdist_contents(policy)
    root = "picogrid_ecn_client-0.1.0"
    contents[f"{root}/MANIFEST.in"] = b"include docs/README.md\n"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(sdist, contents)

    with pytest.raises(ArtifactPolicyError, match="exact publication inventory"):
        inspect_sdist(sdist, policy)


def test_topic_filter_change_requires_policy_review(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/_protocol/topics.py"] += b'BROAD_SUBSCRIPTION = "#"\n'
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="subscription set changed"):
        inspect_wheel(wheel, policy)


def test_unapproved_wildcard_literal_requires_policy_review(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/_protocol/topics.py"] += b'BROAD = "task/+/#"\n'
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="unapproved MQTT wildcard"):
        inspect_wheel(wheel, policy)


def test_runtime_http_remnant_is_rejected(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/client.py"] = b"import aio" + b"http\n"
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="retired runtime marker"):
        inspect_wheel(wheel, policy)


def test_mqtt_transport_must_require_protocol_v5(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/_transport/mqtt.py"] = b"PROTOCOL = object()\n"
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="does not require protocol v5"):
        inspect_wheel(wheel, policy)


def test_offline_mock_must_require_protocol_level_five(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/testing/_mqtt.py"] = b"protocol_level = packet[0]\n"
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="mock does not require MQTT protocol level 5"):
        inspect_wheel(wheel, policy)


def test_rebuild_comparison_requires_identical_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "candidate.whl").write_bytes(b"wheel")
        (directory / "candidate.tar.gz").write_bytes(b"sdist")

    assert compare_rebuilt_artifacts(first, second)
    (second / "candidate.whl").write_bytes(b"changed")
    with pytest.raises(ArtifactPolicyError, match="not byte-for-byte reproducible"):
        compare_rebuilt_artifacts(first, second)


def test_sbom_sanitizer_removes_local_archive_reference(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "$schema": "https://example.invalid/schema.json",
                "components": [
                    {
                        "name": "picogrid-ecn-client",
                        "externalReferences": [
                            {
                                "type": "distribution",
                                "url": "file:///tmp/candidate.whl",
                            },
                            {
                                "type": "website",
                                "url": "https://example.invalid/project",
                            },
                        ],
                    }
                ],
                "metadata": {
                    "tools": {
                        "components": [
                            {
                                "name": "synthetic-sbom-tool",
                                "externalReferences": [
                                    {
                                        "type": "website",
                                        "url": "https://example.invalid/tool-project",
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    release_workflow._sanitize_sbom(sbom)

    value = json.loads(sbom.read_text(encoding="utf-8"))
    assert "$schema" not in value
    assert "externalReferences" not in value["components"][0]
    assert "externalReferences" not in value["metadata"]["tools"]["components"][0]
    assert "file://" not in sbom.read_text(encoding="utf-8")


def test_sbom_sanitizer_derives_a_stable_identity_from_sanitized_content(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.cdx.json"

    def write_sbom(*, serial_number: str, timestamp: str) -> None:
        sbom.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "components": [{"name": "picogrid-ecn-client", "type": "library"}],
                    "metadata": {"timestamp": timestamp},
                    "serialNumber": serial_number,
                    "specVersion": "1.6",
                    "version": 1,
                }
            ),
            encoding="utf-8",
        )

    write_sbom(
        serial_number="urn:uuid:00000000-0000-4000-8000-000000000001",
        timestamp="2026-08-12T00:00:00Z",
    )
    release_workflow._sanitize_sbom(sbom)
    first = json.loads(sbom.read_text(encoding="utf-8"))

    assert re.fullmatch(
        r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        first["serialNumber"],
    )

    write_sbom(
        serial_number="urn:uuid:00000000-0000-4000-8000-000000000002",
        timestamp="2026-08-12T01:00:00Z",
    )
    release_workflow._sanitize_sbom(sbom)
    second = json.loads(sbom.read_text(encoding="utf-8"))

    assert second["serialNumber"] == first["serialNumber"]


def test_sbom_sanitizer_identity_changes_with_component_inventory(tmp_path: Path) -> None:
    serial_numbers: list[str] = []
    for component_name in ("picogrid-ecn-client", "picogrid-ecn-operator-app"):
        sbom = tmp_path / f"{component_name}.cdx.json"
        sbom.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "components": [{"name": component_name, "type": "library"}],
                    "specVersion": "1.6",
                    "version": 1,
                }
            ),
            encoding="utf-8",
        )
        release_workflow._sanitize_sbom(
            sbom,
            allowed_local_projects=(
                "picogrid-ecn-client",
                "picogrid-ecn-operator-app",
            ),
        )
        serial_numbers.append(json.loads(sbom.read_text(encoding="utf-8"))["serialNumber"])

    assert len(set(serial_numbers)) == 2


def test_sbom_sanitizer_rejects_local_dependency_reference(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "components": [
                    {
                        "name": "unexpected-local-dependency",
                        "externalReferences": [
                            {
                                "type": "distribution",
                                "url": "file:///tmp/dependency.whl",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        release_workflow.VerificationError, match="unexpected local dependency reference"
    ):
        release_workflow._sanitize_sbom(sbom)


def test_final_sbom_validation_rejects_invalid_document(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text(json.dumps({"components": []}), encoding="utf-8")

    with pytest.raises(release_workflow.VerificationError, match="SBOM is invalid"):
        release_workflow._validate_sbom(sbom)


def test_candidate_artifacts_are_promoted_only_from_verified_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_policy(POLICY_PATH)
    repository = tmp_path / "repository"
    distribution = repository / "dist"
    distribution.mkdir(parents=True)
    stale_wheel = distribution / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    _write_wheel(stale_wheel, _wheel_contents(policy))
    stale_sdist = distribution / "picogrid_ecn_client-0.1.0.tar.gz"
    _write_sdist(stale_sdist, _sdist_contents(policy))
    unrelated = distribution / "another_project-1.0-py3-none-any.whl"
    unrelated.write_bytes(b"preserve")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(release_workflow.VerificationError, match="unexpected pre-build entry"):
        release_workflow._reset_candidate_artifacts(policy)

    assert stale_wheel.is_file()
    assert stale_sdist.is_file()
    assert unrelated.read_bytes() == b"preserve"
    unrelated.unlink()
    release_workflow._reset_candidate_artifacts(policy)
    assert not stale_wheel.exists()
    assert not stale_sdist.exists()
    verified = tmp_path / "verified"
    verified.mkdir()
    wheel = verified / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    sdist = verified / "picogrid_ecn_client-0.1.0.tar.gz"
    operator_wheel = verified / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"verified wheel")
    sdist.write_bytes(b"verified sdist")
    operator_wheel.write_bytes(b"verified operator wheel")

    promoted_wheel, promoted_sdist, promoted_operator_wheel = (
        release_workflow._promote_verified_artifacts(wheel, sdist, operator_wheel)
    )

    assert promoted_wheel.read_bytes() == wheel.read_bytes()
    assert promoted_sdist.read_bytes() == sdist.read_bytes()
    assert promoted_operator_wheel.read_bytes() == operator_wheel.read_bytes()
    release_workflow._inspect_final_dist_inventory(
        promoted_wheel,
        promoted_sdist,
        promoted_operator_wheel,
    )

    unexpected = distribution / "unexpected.txt"
    unexpected.write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(
        release_workflow.VerificationError, match="does not match promoted artifacts"
    ):
        release_workflow._inspect_final_dist_inventory(
            promoted_wheel,
            promoted_sdist,
            promoted_operator_wheel,
        )


def test_artifact_promotion_rejects_symlinked_dist_without_writing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external-dist"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("outside\n", encoding="utf-8")
    distribution = repository / "dist"
    distribution.symlink_to(external, target_is_directory=True)
    verified = tmp_path / "verified"
    verified.mkdir()
    wheel = verified / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    sdist = verified / "picogrid_ecn_client-0.1.0.tar.gz"
    operator_wheel = verified / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"verified wheel")
    sdist.write_bytes(b"verified sdist")
    operator_wheel.write_bytes(b"verified operator wheel")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(release_workflow.VerificationError, match="symbolic-link"):
        release_workflow._promote_verified_artifacts(wheel, sdist, operator_wheel)

    assert sentinel.read_text(encoding="utf-8") == "outside\n"
    assert {path.name for path in external.iterdir()} == {"keep"}


def test_artifact_promotion_rejects_destination_entries_without_following_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    distribution = repository / "dist"
    distribution.mkdir(parents=True)
    external_wheel = tmp_path / "outside.whl"
    external_wheel.write_bytes(b"outside")
    wheel_name = "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    (distribution / wheel_name).symlink_to(external_wheel)
    verified = tmp_path / "verified"
    verified.mkdir()
    wheel = verified / wheel_name
    sdist = verified / "picogrid_ecn_client-0.1.0.tar.gz"
    operator_wheel = verified / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"verified wheel")
    sdist.write_bytes(b"verified sdist")
    operator_wheel.write_bytes(b"verified operator wheel")
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(release_workflow.VerificationError, match="changed after pre-build"):
        release_workflow._promote_verified_artifacts(wheel, sdist, operator_wheel)

    assert external_wheel.read_bytes() == b"outside"


@pytest.mark.parametrize("entry_kind", ["directory", "symlink"])
def test_candidate_artifact_reset_rejects_non_regular_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_kind: str
) -> None:
    repository = tmp_path / "repository"
    distribution = repository / "dist"
    distribution.mkdir(parents=True)
    candidate = distribution / "picogrid_ecn_client-0.0.9-py3-none-any.whl"
    if entry_kind == "directory":
        candidate.mkdir()
    else:
        target = tmp_path / "outside.whl"
        target.write_bytes(b"outside")
        candidate.symlink_to(target)
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(release_workflow.VerificationError, match="unexpected pre-build entry"):
        release_workflow._reset_candidate_artifacts(load_policy(POLICY_PATH))


def test_candidate_artifact_reset_inspects_archive_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_policy(POLICY_PATH)
    repository = tmp_path / "repository"
    distribution = repository / "dist"
    distribution.mkdir(parents=True)
    candidate = distribution / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    contents = _wheel_contents(policy)
    contents["picogrid_ecn_client/client.py"] = _PRIVATE_KEY_CANARY
    _write_wheel(candidate, contents)
    original = candidate.read_bytes()
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(
        release_workflow.VerificationError,
        match="pre-existing candidate artifact failed publication inspection",
    ) as raised:
        release_workflow._reset_candidate_artifacts(policy)

    assert candidate.read_bytes() == original
    assert candidate.name not in str(raised.value)
    assert _PRIVATE_KEY_CANARY.decode().strip() not in str(raised.value)


def test_final_dist_inventory_rejects_a_symlinked_promoted_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = tmp_path / "dist"
    distribution.mkdir()
    external_wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    external_wheel.write_bytes(b"wheel")
    wheel = distribution / external_wheel.name
    wheel.symlink_to(external_wheel)
    sdist = distribution / "picogrid_ecn_client-0.1.0.tar.gz"
    sdist.write_bytes(b"sdist")
    operator_wheel = distribution / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    operator_wheel.write_bytes(b"operator wheel")
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    with pytest.raises(release_workflow.VerificationError, match="unsupported entry"):
        release_workflow._inspect_final_dist_inventory(wheel, sdist, operator_wheel)


def test_installed_suite_includes_dedicated_example_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "verification-inputs"
    for name in ("unit", "contract", "e2e", "performance", "examples"):
        (staging / "tests" / name).mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def capture(command: Sequence[str], **_: object) -> str:
        calls.append(tuple(command))
        return ""

    monkeypatch.setattr(release_workflow, "_run", capture)

    release_workflow._run_installed_suite(Path("/isolated/bin/python"), staging, environment={})

    assert len(calls) == 4
    pytest_command = calls[0]
    assert pytest_command[:3] == ("/isolated/bin/python", "-I", "-m")
    assert str(staging / "tests" / "examples") in pytest_command
    assert pytest_command.index(str(staging / "tests" / "examples")) > pytest_command.index(
        str(staging / "tests" / "performance")
    )
    assert calls[2] == (
        "/isolated/bin/python",
        "-I",
        str(staging / "installed_cli_probe.py"),
    )


def test_installed_helpers_share_the_exact_mqtt_only_example_contract() -> None:
    assert installed_examples.SUPPORT_FILES == release_workflow.EXAMPLE_SUPPORT_FILES
    assert installed_mock_process._MQTT_LINE.fullmatch(
        "Mock ECN MQTT v5 listening on 127.0.0.1:1883"
    )


def test_run_preserves_timeout_error_when_partial_output_is_not_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []

    def time_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            ("synthetic-command",),
            7,
            output=b"stdout \xe2\x82",
            stderr=b"stderr \xf0\x9f\x92",
        )

    monkeypatch.setattr(release_workflow.subprocess, "run", time_out)
    monkeypatch.setattr(release_workflow, "emit", emitted.append)

    with pytest.raises(
        release_workflow.VerificationError,
        match=r"^command timed out after 7s: synthetic-command$",
    ):
        release_workflow._run(["synthetic-command"], cwd=Path.cwd(), environment={}, timeout=7)

    assert emitted == ["\n$ synthetic-command", "stdout \ufffd", "stderr \ufffd"]


def _innermost_functions_calling(source: str, callee: str) -> set[str]:
    """Names of the innermost functions containing a direct call to ``callee``."""

    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.enclosing: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.enclosing.append(node.name)
            self.generic_visit(node)
            self.enclosing.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.enclosing.append(node.name)
            self.generic_visit(node)
            self.enclosing.pop()

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            if isinstance(function, ast.Name) and function.id == callee and self.enclosing:
                found.add(self.enclosing[-1])
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def _stage_ancestors(dependencies: Mapping[str, tuple[str, ...]], stage: str) -> set[str]:
    seen: set[str] = set()
    pending = list(dependencies[stage])
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        pending.extend(dependencies[parent])
    return seen


# Every function that directly calls the wheel installer, mapped to its DAG stage.
# A new direct caller must be declared here so its dependency ordering is covered.
_WHEEL_INSTALLERS_BY_STAGE = {
    "runtime_stage": "runtime",
    "python_312_stage": "py312",
    "python_313_stage": "py313",
    "python_314_stage": "py314",
    "_run_operator_application_gate": "operator",
}


def test_no_installation_begins_before_documented_clean_install() -> None:
    """No stage that installs the release artifacts may start before `docsmoke`.

    The serial verifier guaranteed this by source order. Under the parallel DAG it
    has to be a real dependency edge on every installing stage, not just `runtime`.
    """

    source = Path(release_workflow.__file__).read_text(encoding="utf-8")
    dependencies = release_workflow._STAGE_DEPENDENCIES

    installers = _innermost_functions_calling(source, "_install_exact_wheel")
    assert installers == set(_WHEEL_INSTALLERS_BY_STAGE), (
        "a release stage installs the wheel without being ordered after the documented "
        f"clean install: {sorted(installers ^ set(_WHEEL_INSTALLERS_BY_STAGE))}"
    )

    for stage in release_workflow._INSTALLATION_STAGES:
        assert "docsmoke" in _stage_ancestors(dependencies, stage), (
            f"installation stage {stage!r} does not wait for the documented clean install"
        )

    # The smoke itself must stay ahead of them: it may not depend on any installer,
    # and it must be the stage that actually runs the documented install.
    assert "docsmoke" not in _stage_ancestors(dependencies, "docsmoke")
    assert not set(release_workflow._INSTALLATION_STAGES) & _stage_ancestors(
        dependencies, "docsmoke"
    )
    assert _innermost_functions_calling(source, "_run_documented_installation_smoke") == {
        "docsmoke_stage"
    }


def test_public_example_inventory_is_sorted_and_source_derived(tmp_path: Path) -> None:
    for name in (*release_workflow.EXAMPLE_SUPPORT_FILES, "z_last.py", "a_first.py"):
        (tmp_path / name).write_text("\n", encoding="utf-8")

    assert release_workflow._public_example_names(tmp_path) == (
        "a_first.py",
        "z_last.py",
    )

    (tmp_path / "_unstaged_helper.py").write_text("\n", encoding="utf-8")
    with pytest.raises(release_workflow.VerificationError, match="unstaged local helper"):
        release_workflow._public_example_names(tmp_path)


def test_stage_verification_inputs_copies_an_exact_example_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    (repository / "tests").mkdir(parents=True)
    examples = repository / "examples"
    examples.mkdir()
    for name in (*release_workflow.EXAMPLE_SUPPORT_FILES, "second.py", "first.py"):
        (examples / name).write_text("\n", encoding="utf-8")
    (examples / "notes.txt").write_text("not an executable example\n", encoding="utf-8")
    docs = repository / "docs"
    docs.mkdir()
    (docs / "example.md").write_text("# Example\n", encoding="utf-8")
    entries = []
    for name in ("first.py", "second.py"):
        entries.append(
            {
                "id": Path(name).stem,
                "source_path": f"examples/{name}",
                "workflow": {
                    "module": "picogrid_ecn_client.workflows.diagnostics",
                    "function": "preflight",
                },
                "documentation": ["docs/example.md"],
                "title": Path(name).stem.title(),
                "summary": "Synthetic example.",
                "notebook_eligible": True,
                "exclusion_reason": None,
                "safety_class": "read",
                "modes": ["offline-check"],
                "required_inputs": [],
            }
        )
    manifest = {"schema_version": 1, "examples": entries}
    (examples / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    scripts = repository / "scripts"
    scripts.mkdir()
    for name in (
        "__init__.py",
        "generate_api_reference.py",
        "installed_wheel_probe.py",
        "installed_mock_process.py",
        "installed_cli_probe.py",
        "installed_examples.py",
        "public-api-manifest.json",
        "release-policy.json",
        "release_checks.py",
    ):
        (scripts / name).write_text("\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    release_workflow._stage_verification_inputs(staging)

    assert {path.name for path in (staging / "examples").iterdir()} == {
        *release_workflow.EXAMPLE_SUPPORT_FILES,
        "first.py",
        "second.py",
    }
    # Byte identity, not parsed equality: the verifier copies the reviewed manifest, so
    # reformatting or key reordering in the staged copy is drift, not an equivalent.
    assert (staging / installed_examples.MANIFEST_NAME).read_bytes() == (
        repository / "examples" / "manifest.json"
    ).read_bytes()


def test_immutable_source_snapshot_copies_only_reviewed_docs_and_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    for name in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic\n", encoding="utf-8")
    package = repository / "src" / "picogrid_ecn_client"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (repository / "docs").mkdir()
    (repository / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repository / "docs" / "unreviewed.md").write_text("# Extra\n", encoding="utf-8")
    (repository / "examples").mkdir()
    (repository / "examples" / "demo.py").write_text("\n", encoding="utf-8")
    (repository / "examples" / "unreviewed.py").write_text("\n", encoding="utf-8")
    policy = {
        "sdist_documentation_files": ["docs/guide.md"],
        "sdist_example_files": ["examples/demo.py"],
        "sdist_auxiliary_files": [],
    }
    snapshot = tmp_path / "snapshot"
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    release_workflow._prepare_source_snapshot(snapshot, 1_735_689_600, policy)

    assert {
        path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()
    } == {
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "docs/guide.md",
        "examples/demo.py",
        "pyproject.toml",
        "src/picogrid_ecn_client/__init__.py",
    }


def _write_minimal_snapshot_repository(repository: Path) -> None:
    for name in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic\n", encoding="utf-8")
    package = repository / "src" / "picogrid_ecn_client"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")


@pytest.mark.parametrize("symlink_location", ["license", "operator_file"])
def test_immutable_source_snapshot_rejects_symlinked_build_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_location: str,
) -> None:
    repository = tmp_path / "repository"
    _write_minimal_snapshot_repository(repository)
    external = tmp_path / "external.txt"
    external.write_text("external-secret-marker\n", encoding="utf-8")
    operator_file = repository / "operator-app" / "README.md"
    operator_file.parent.mkdir(parents=True)
    operator_file.write_text("# Operator\n", encoding="utf-8")
    target = repository / "LICENSE" if symlink_location == "license" else operator_file
    target.unlink()
    target.symlink_to(external)
    policy = {
        "sdist_documentation_files": [],
        "sdist_example_files": [],
        "sdist_auxiliary_files": ["operator-app/README.md"],
    }
    snapshot = tmp_path / "snapshot"
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    with pytest.raises(release_workflow.VerificationError, match="symbolic link"):
        release_workflow._prepare_source_snapshot(snapshot, 1_735_689_600, policy)

    assert all(
        b"external-secret-marker" not in path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("escape_kind", ["absolute", "parent"])
def test_immutable_source_snapshot_rejects_policy_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escape_kind: str,
) -> None:
    source_parent = tmp_path / "source-parent"
    repository = source_parent / "repository"
    _write_minimal_snapshot_repository(repository)
    external_source = source_parent / "outside.txt"
    external_source.write_text("external-secret-marker\n", encoding="utf-8")
    destination_parent = tmp_path / "destination-parent"
    destination_parent.mkdir()
    escaped_destination = destination_parent / "outside.txt"
    policy_path = str(repository / "README.md") if escape_kind == "absolute" else "../outside.txt"
    policy = {
        "sdist_documentation_files": [],
        "sdist_example_files": [],
        "sdist_auxiliary_files": [policy_path],
    }
    snapshot = destination_parent / "snapshot"
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)

    with pytest.raises(release_workflow.VerificationError, match="outside the repository"):
        release_workflow._prepare_source_snapshot(snapshot, 1_735_689_600, policy)

    assert external_source.read_text(encoding="utf-8") == "external-secret-marker\n"
    assert (repository / "README.md").read_text(encoding="utf-8") == "synthetic\n"
    assert not escaped_destination.exists()


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "from picogrid_ecn_client._transport import MQTTTransport",
            "non-public client module",
        ),
        (
            "from picogrid_ecn_client.workflows import _retention",
            "non-public client name",
        ),
        ("from picogrid_ecn_client import _internal", "non-public client name"),
        ("from picogrid_ecn_client import client", "non-public client name"),
        (
            "from picogrid_ecn_client.workflows import observe",
            "non-public client name",
        ),
        (
            "import picogrid_ecn_client.workflows._retention",
            "non-public client module",
        ),
        ("import picogrid_" + "example_sdk", "non-public client module"),
        ("from picogrid_" + "example_sdk import x", "non-public client module"),
    ],
)
def test_installed_example_import_scan_rejects_non_public_sdk_names(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    example = tmp_path / "example.py"
    example.write_text(f"{source}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        installed_examples._validate_public_imports(example)


@pytest.mark.parametrize(
    "source",
    [
        "from picogrid_ecn_client import ECNClient",
        "from picogrid_ecn_client.workflows import preflight",
        "from picogrid_ecn_client import workflows",
        "import picogrid_ecn_client",
        "from helpers import _private\nimport picogrid_ecn_client",
        "from helpers import _private",
    ],
)
def test_installed_example_import_scan_accepts_public_sdk_names(
    tmp_path: Path,
    source: str,
) -> None:
    example = tmp_path / "example.py"
    example.write_text(f"{source}\n", encoding="utf-8")

    installed_examples._validate_public_imports(example)


def test_installed_example_import_scan_fails_closed_without_export_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = tmp_path / "example.py"
    example.write_text(
        "from picogrid_ecn_client import ECNClient\n",
        encoding="utf-8",
    )

    def fail_import(_module_name: str) -> None:
        raise ModuleNotFoundError

    monkeypatch.setattr(installed_examples.importlib, "import_module", fail_import)

    with pytest.raises(RuntimeError, match="non-public client name"):
        installed_examples._validate_public_imports(example)


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (None, "workflow metadata"),
        ({"module": "picogrid_ecn_client.client", "function": "preflight"}, "workflow metadata"),
        (
            {
                "module": "picogrid_ecn_client.workflows.diagnostics",
                "function": "",
            },
            "workflow metadata",
        ),
    ],
)
def test_installed_example_manifest_rejects_malformed_workflow_metadata(
    tmp_path: Path, workflow: object, message: str
) -> None:
    manifest = {
        "schema_version": 1,
        "examples": [
            {
                "id": "demo",
                "source_path": "examples/demo.py",
                "workflow": workflow,
            }
        ],
    }
    (tmp_path / installed_examples.MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=message):
        installed_examples._load_example_manifest(tmp_path)


def test_installed_example_manifest_uses_id_order(tmp_path: Path) -> None:
    entries = [
        {
            "id": identifier,
            "source_path": f"examples/{filename}",
            "workflow": {
                "module": "picogrid_ecn_client.workflows.diagnostics",
                "function": "preflight",
            },
        }
        for identifier, filename in (("a-b", "a_b.py"), ("ab", "ab.py"))
    ]
    (tmp_path / installed_examples.MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "examples": entries}),
        encoding="utf-8",
    )

    assert installed_examples._load_example_manifest(tmp_path) == (
        (
            "a_b.py",
            "picogrid_ecn_client.workflows.diagnostics",
            "preflight",
        ),
        (
            "ab.py",
            "picogrid_ecn_client.workflows.diagnostics",
            "preflight",
        ),
    )


def test_quality_gates_disable_repository_caches_and_strictly_type_check_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def capture(command: Sequence[str], **_: object) -> str:
        calls.append(tuple(command))
        return ""

    monkeypatch.setattr(release_workflow, "_run", capture)
    system_which = release_workflow.shutil.which
    monkeypatch.setattr(
        release_workflow.shutil,
        "which",
        lambda name: "/tools/uv" if name == "uv" else system_which(name),
    )

    release_workflow._quality_gates({}, load_policy(POLICY_PATH))

    assert (
        "/tools/uv",
        "run",
        "--frozen",
        "mypy",
        "--no-incremental",
        "--strict",
        "examples",
        "tests/examples",
    ) in calls
    assert all("--no-cache" in command for command in calls if "ruff" in command)
    assert all("--no-incremental" in command for command in calls if "mypy" in command)
    pytest_command = next(command for command in calls if "pytest" in command)
    assert pytest_command[pytest_command.index("-p") : pytest_command.index("-p") + 2] == (
        "-p",
        "no:cacheprovider",
    )


@pytest.mark.parametrize("minor", ("3.12", "3.13", "3.14"))
def test_python_discovery_verifies_the_selected_minor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minor: str
) -> None:
    interpreter = tmp_path / f"python{minor}"
    interpreter.write_bytes(b"")
    calls: list[tuple[str, ...]] = []

    def capture(command: Sequence[str], **_: object) -> str:
        calls.append(tuple(command))
        if command[0] == "uv":
            return f"{interpreter}\n"
        return f"{minor}\n"

    monkeypatch.setattr(release_workflow, "_run", capture)

    selected = release_workflow._find_python_minor(minor, "uv", {})

    assert selected == interpreter.resolve()
    assert calls[0][-1] == minor
    assert calls[1][:2] == (str(interpreter.resolve()), "-I")


def test_documented_installation_resolves_dependencies_from_an_empty_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    calls: list[tuple[str, ...]] = []
    environments: list[dict[str, str]] = []

    def capture(command: Sequence[str], **keywords: object) -> str:
        calls.append(tuple(command))
        environments.append(dict(keywords["environment"]))  # type: ignore[arg-type]
        return ""

    monkeypatch.setattr(release_workflow, "_run", capture)
    release_root = tmp_path / "release"
    release_root.mkdir()
    release_workflow._run_documented_installation_smoke(
        Path("/clean/bin/python"),
        wheel,
        release_root,
        environment={"PIP_NO_INDEX": "1", "PIP_INDEX_URL": "private"},
        version="0.1.0",
    )

    assert len(calls) == 3
    assert "metadata.distributions" in calls[0][-1]
    assert calls[1] == (
        "/clean/bin/python",
        "-m",
        "pip",
        "install",
        "./picogrid_ecn_client-0.1.0-py3-none-any.whl",
    )
    assert "site-packages" in calls[2][-1]
    assert all(
        "PIP_NO_INDEX" not in value and "PIP_INDEX_URL" not in value for value in environments
    )


def test_docker_inventory_probe_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout: int | None = None

    def inspect(*_: object, **keywords: object) -> subprocess.CompletedProcess[str]:
        nonlocal timeout
        timeout = int(keywords["timeout"])  # type: ignore[arg-type]
        raise subprocess.TimeoutExpired(("docker", "image", "inspect"), timeout)

    monkeypatch.setattr(release_workflow.subprocess, "run", inspect)

    with pytest.raises(
        release_workflow.VerificationError,
        match=r"^Docker image inventory check timed out$",
    ) as raised:
        release_workflow._docker_resource_exists(
            "/usr/bin/docker",
            "image",
            "sensitive-tag",
            cwd=tmp_path,
            environment={},
        )

    assert timeout == 30
    assert "sensitive-tag" not in str(raised.value)
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("body_fails", (False, True))
def test_operator_container_cleanup_does_not_mask_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
) -> None:
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    (operator_root / ".env.example").write_text("PLACEHOLDER=1\n", encoding="utf-8")
    wheel = tmp_path / "picogrid_ecn_client-0.1.0-py3-none-any.whl"
    operator_wheel = tmp_path / "picogrid_ecn_operator_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic wheel")
    operator_wheel.write_bytes(b"synthetic operator wheel")
    inventory_calls = 0

    def inventory(*_: object, **__: object) -> bool:
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 5:
            raise release_workflow.VerificationError("cleanup probe failed")
        return False

    def run(command: Sequence[str], **_: object) -> str:
        if body_fails and "build" in command:
            raise release_workflow.VerificationError("primary build failed")
        return ""

    monkeypatch.setattr(release_workflow.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(release_workflow, "_docker_resource_exists", inventory)
    monkeypatch.setattr(release_workflow, "_run", run)

    expected = "primary build failed" if body_fails else "cleanup probe failed"
    with pytest.raises(release_workflow.VerificationError, match=f"^{expected}$"):
        release_workflow._run_operator_container_gate(
            operator_root,
            wheel,
            operator_wheel,
            environment={},
        )


def test_pip_audit_does_not_retry_reported_vulnerabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "vulnerability-scan.json"
    calls = 0

    def audit(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        report.write_text(
            json.dumps({"dependencies": [{"name": "demo", "version": "1", "vulns": [{}]}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(("pip-audit",), 1, "", "")

    monkeypatch.setattr(release_workflow.subprocess, "run", audit)
    monkeypatch.setattr(
        release_workflow.time,
        "sleep",
        lambda _: pytest.fail("vulnerability failures must not be retried"),
    )

    with pytest.raises(release_workflow.VerificationError, match="known runtime vulnerabilities"):
        release_workflow._run_pip_audit_with_bounded_network_retry(
            ("pip-audit",), report=report, environment={}
        )
    assert calls == 1


def test_pip_audit_retries_only_a_bounded_transient_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "vulnerability-scan.json"
    calls = 0

    def audit(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                ("pip-audit",), 2, "", "temporary failure in name resolution"
            )
        report.write_text(
            json.dumps({"dependencies": [{"name": "demo", "version": "1", "vulns": []}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(("pip-audit",), 0, "", "")

    monkeypatch.setattr(release_workflow.subprocess, "run", audit)
    monkeypatch.setattr(release_workflow.time, "sleep", lambda _: None)

    release_workflow._run_pip_audit_with_bounded_network_retry(
        ("pip-audit",), report=report, environment={}
    )
    assert calls == 2


def test_wheel_with_rc_version_normalized_to_canonical_form(tmp_path: Path) -> None:
    """Pre-release versions in dashed policy form normalize to canonical artifact names.

    Policy: 0.1.0-rc1 (dashed form from release-please)
    Artifact: 0.1.0rc1-py3-none-any.whl (canonical PEP 440 form)
    """
    policy = load_policy(POLICY_PATH)
    policy["project_version"] = "0.1.0-rc1"
    wheel = tmp_path / "picogrid_ecn_client-0.1.0rc1-py3-none-any.whl"
    contents = _wheel_contents(policy)
    # Rename all dist-info files from 0.1.0 to canonical 0.1.0rc1
    old_prefix = "picogrid_ecn_client-0.1.0.dist-info/"
    new_prefix = "picogrid_ecn_client-0.1.0rc1.dist-info/"
    renamed = {}
    for key, value in contents.items():
        if key.startswith(old_prefix):
            new_key = key.replace(old_prefix, new_prefix)
            renamed[new_key] = value
        else:
            renamed[key] = value
    # Update METADATA with canonical version
    metadata_bytes = _metadata(policy)
    metadata_str = metadata_bytes.decode("utf-8")
    # Replace 0.1.0 with 0.1.0rc1 in the metadata
    metadata_str = metadata_str.replace("Version: 0.1.0\n", "Version: 0.1.0rc1\n")
    renamed[f"{new_prefix}METADATA"] = metadata_str.encode("utf-8")
    _write_wheel(wheel, renamed)

    wheel_result = inspect_wheel(wheel, policy)
    assert wheel_result.artifact_type == "wheel"


def test_sdist_with_rc_version_normalized_to_canonical_form(tmp_path: Path) -> None:
    """Pre-release versions in dashed policy form normalize to canonical artifact names.

    Policy: 0.1.0-rc1 (dashed form from release-please)
    Artifact: 0.1.0rc1.tar.gz (canonical PEP 440 form)
    """
    policy = load_policy(POLICY_PATH)
    policy["project_version"] = "0.1.0-rc1"
    sdist = tmp_path / "picogrid_ecn_client-0.1.0rc1.tar.gz"
    contents = _sdist_contents(policy)
    # Rename all paths from 0.1.0 to canonical 0.1.0rc1
    old_root = "picogrid_ecn_client-0.1.0"
    new_root = "picogrid_ecn_client-0.1.0rc1"
    renamed = {}
    for key, value in contents.items():
        new_key = key.replace(old_root, new_root)
        # Update PKG-INFO metadata version
        if key.endswith("/PKG-INFO") or key.endswith("-INFO"):
            value_str = value.decode("utf-8")
            value_str = value_str.replace("Version: 0.1.0\n", "Version: 0.1.0rc1\n")
            value = value_str.encode("utf-8")
        renamed[new_key] = value
    _write_sdist(sdist, renamed)

    sdist_result = inspect_sdist(sdist, policy)
    assert sdist_result.artifact_type == "sdist"


def test_mismatched_rc_numbers_fail_artifact_inspection(tmp_path: Path) -> None:
    """Different RC numbers represent different artifacts and fail inspection."""
    policy = load_policy(POLICY_PATH)
    policy["project_version"] = "0.1.0-rc1"
    # Artifact is rc2 but policy is rc1 - should fail
    wheel = tmp_path / "picogrid_ecn_client-0.1.0rc2-py3-none-any.whl"
    contents = _wheel_contents(policy)
    # Rename to the mismatched rc2 version
    old_prefix = "picogrid_ecn_client-0.1.0.dist-info/"
    new_prefix = "picogrid_ecn_client-0.1.0rc2.dist-info/"
    renamed = {}
    for key, value in contents.items():
        if key.startswith(old_prefix):
            new_key = key.replace(old_prefix, new_prefix)
            renamed[new_key] = value
        else:
            renamed[key] = value
    _write_wheel(wheel, renamed)

    with pytest.raises(ArtifactPolicyError, match="expected wheel"):
        inspect_wheel(wheel, policy)
