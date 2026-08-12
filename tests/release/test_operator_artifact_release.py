# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from scripts import release_checks
from scripts import verify_release as release_workflow
from scripts.release_checks import ArtifactPolicyError, load_policy
from scripts.sync_dep_locks import canonical_root_requirement

POLICY_PATH = Path(__file__).parents[2] / "scripts" / "release-policy.json"
_OPERATOR_DISTRIBUTION_NAME = "picogrid_ecn_operator_app"
_OPERATOR_IMPORT_NAME = "operator_app"
_OPERATOR_PROJECT_NAME = "picogrid-ecn-operator-app"
_PRIVATE_KEY_CANARY = b"-----BEGIN " + b"PRIVATE KEY-----\n"
_IPV4_CANARY = b"connect to 192.0.2." + b"10\n"


def _operator_build_backend(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = POLICY_PATH.parents[1] / "operator-app" / "build_backend.py"
    setuptools = ModuleType("setuptools")
    setuptools.build_meta = ModuleType("setuptools.build_meta")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "setuptools", setuptools)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("operator_build_backend_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operator_requirements(policy: dict[str, Any]) -> list[str]:
    return [
        *policy["operator_runtime_requirements"],
        f"picogrid-ecn-client=={policy['project_version']}",
    ]


def _operator_metadata(
    policy: dict[str, Any],
    *,
    requirements: list[str] | None = None,
) -> bytes:
    requires_dist = "".join(
        f"Requires-Dist: {requirement}\n"
        for requirement in requirements or _operator_requirements(policy)
    )
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {_OPERATOR_PROJECT_NAME}\n"
        f"Version: {policy['project_version']}\n"
        f"License-Expression: {policy['license_expression']}\n"
        f"Requires-Python: {policy['requires_python']}\n"
        f"{requires_dist}"
        "\n"
        "Synthetic installed operator application fixture.\n"
    ).encode()


def _generated_frontend_contents(policy: dict[str, Any]) -> dict[str, bytes]:
    static_prefix = "operator_app/static/"
    files = {
        name.removeprefix(static_prefix): name.encode("utf-8")
        for name in policy["operator_wheel_package_files"]
        if name.startswith(static_prefix)
    }
    scripts = sorted(name for name in files if name.startswith("assets/") and name.endswith(".js"))
    styles = sorted(name for name in files if name.startswith("assets/") and name.endswith(".css"))
    assert scripts and styles
    files["index.html"] = (
        "<!doctype html><html><head>"
        + "".join(f'<link rel="stylesheet" href="./{name}">' for name in styles)
        + '</head><body><main id="app"></main>'
        + "".join(f'<script type="module" src="./{name}"></script>' for name in scripts)
        + "</body></html>\n"
    ).encode()
    for name in scripts:
        files[name] = b'const mode = "read-only";\n'
    for name in styles:
        files[name] = b"body { color: CanvasText; background: Canvas; }\n"
    for name in files:
        if name.startswith("brand/"):
            files[name] = (
                POLICY_PATH.parents[1] / "operator-app" / "frontend" / "public" / name
            ).read_bytes()
    return files


def _operator_wheel_contents(
    policy: dict[str, Any],
    *,
    requirements: list[str] | None = None,
) -> dict[str, bytes]:
    contents = dict.fromkeys(policy["operator_wheel_package_files"], b"")
    for name, data in _generated_frontend_contents(policy).items():
        contents[f"operator_app/static/{name}"] = data
    prefix = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/"
    for name in policy["operator_wheel_dist_info_files"]:
        contents[f"{prefix}{name}"] = b""
    contents[f"{prefix}METADATA"] = _operator_metadata(policy, requirements=requirements)
    contents[f"{prefix}WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    contents[f"{prefix}entry_points.txt"] = (
        f"[console_scripts]\npicogrid-ecn-operator = {_OPERATOR_IMPORT_NAME}.__main__:main\n"
    ).encode()
    contents[f"{prefix}top_level.txt"] = f"{_OPERATOR_IMPORT_NAME}\n".encode()
    contents[f"{prefix}licenses/LICENSE"] = (POLICY_PATH.parents[1] / "LICENSE").read_bytes()
    contents[f"{prefix}licenses/THIRD_PARTY_LICENSES.md"] = (
        POLICY_PATH.parents[1] / "operator-app" / "THIRD_PARTY_LICENSES.md"
    ).read_bytes()
    return contents


def _write_operator_wheel(
    path: Path,
    contents: dict[str, bytes],
    *,
    symlink: str | None = None,
) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, data in sorted(contents.items()):
            member = zipfile.ZipInfo(name)
            member.create_system = 3
            member.external_attr = (0o120777 if name == symlink else 0o100644) << 16
            archive.writestr(member, data)


def _operator_wheel_path(tmp_path: Path, policy: dict[str, Any]) -> Path:
    return tmp_path / (
        f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}-py3-none-any.whl"
    )


def _inspect_operator_wheel(path: Path, policy: dict[str, Any]) -> Any:
    return release_checks.inspect_operator_wheel(path, policy)


def test_operator_policy_matches_runtime_manifest_and_fixed_frontend_inventory() -> None:
    policy = load_policy(POLICY_PATH)
    repository = POLICY_PATH.parents[1]
    configuration = tomllib.loads(
        (repository / "operator-app" / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = {
        canonical_root_requirement(requirement)
        for requirement in configuration["project"]["dependencies"]
    }

    assert set(_operator_requirements(policy)) == declared
    assert f"picogrid-ecn-client=={policy['project_version']}" in declared
    assert configuration["project"]["license-files"] == [
        "LICENSE",
        "THIRD_PARTY_LICENSES.md",
    ]
    assert "licenses/LICENSE" in policy["operator_wheel_dist_info_files"]
    assert "licenses/THIRD_PARTY_LICENSES.md" in policy["operator_wheel_dist_info_files"]
    third_party = repository / "operator-app" / "THIRD_PARTY_LICENSES.md"
    assert (
        hashlib.sha256(third_party.read_bytes()).hexdigest()
        == policy["operator_third_party_licenses_sha256"]
    )
    static = {
        name.removeprefix("operator_app/static/")
        for name in policy["operator_wheel_package_files"]
        if name.startswith("operator_app/static/")
    }
    assert "index.html" in static
    assert any(name.startswith("assets/") and name.endswith(".js") for name in static)
    assert any(name.startswith("assets/") and name.endswith(".css") for name in static)
    assert all("*" not in name for name in static)


@pytest.mark.parametrize(
    "package_json",
    [
        b'{"packageManager":"npm@11.17.0","engines":{"node":"24.19.0",'
        b'"node":"24.19.0","npm":"11.17.0"}}',
        b'{"packageManager":"npm@11.17.0","engines":{"node":"24.19.0",'
        b'"npm":"11.17.0"},"value":NaN}',
        b'{"packageManager":"npm@11.17.0","engines":{"node":"24.19.0",'
        b'"npm":"11.17.0"},"value":Infinity}',
        json.dumps(
            {
                "packageManager": "npm@11.17.0",
                "engines": {"node": "24.19.0", "npm": "11.17.0"},
                "padding": "x" * (64 * 1024),
            }
        ).encode(),
    ],
    ids=("duplicate-key", "nan", "infinity", "oversized"),
)
def test_operator_build_backend_rejects_ambiguous_package_metadata(
    tmp_path: Path,
    package_json: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_bytes(package_json)
    backend = _operator_build_backend(monkeypatch)

    with pytest.raises(RuntimeError, match="operator package metadata could not be read"):
        backend._required_frontend_tool_versions(tmp_path)


@pytest.mark.parametrize(
    "symlink_name",
    ("README.md", "backend/external.py", "frontend/external.ts"),
)
def test_operator_build_backend_rejects_symlinked_source_members(
    tmp_path: Path,
    symlink_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    backend = _operator_build_backend(monkeypatch)
    for name in backend._SOURCE_FILES:
        (source / name).write_text(name, encoding="utf-8")
    (source / "LICENSE").write_text("license", encoding="utf-8")
    (source / "backend").mkdir()
    (source / "frontend").mkdir()
    external = tmp_path / "external-canary"
    canary = b"must not enter the operator artifact"
    external.write_bytes(canary)
    symlink = source / symlink_name
    if symlink.exists():
        symlink.unlink()
    symlink.symlink_to(external)
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.setattr(backend, "_ROOT", source)

    with pytest.raises(
        RuntimeError,
        match=rf"operator source member is not a regular file: {symlink}",
    ):
        backend._stage_source(staged)

    assert all(path.read_bytes() != canary for path in staged.rglob("*") if path.is_file())


def test_operator_wheel_matches_exact_inventory_metadata_and_dependencies(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = _operator_wheel_path(tmp_path, policy)
    contents = _operator_wheel_contents(policy)
    _write_operator_wheel(wheel, contents)

    inspection = _inspect_operator_wheel(wheel, policy)

    assert inspection.artifact_type == "operator-wheel"
    assert inspection.file_count == len(contents)
    assert inspection.files == tuple(sorted(contents))
    assert inspection.sha256 == hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_operator_wheel_with_rc_policy_version_accepts_canonical_artifact(
    tmp_path: Path,
) -> None:
    """A dashed policy pre-release matches the canonical artifact the build emits."""
    policy = dict(load_policy(POLICY_PATH))
    policy["project_version"] = "1.2.3-rc1"
    canonical = dict(policy)
    canonical["project_version"] = "1.2.3rc1"
    wheel = _operator_wheel_path(tmp_path, canonical)
    _write_operator_wheel(wheel, _operator_wheel_contents(canonical))

    inspection = _inspect_operator_wheel(wheel, policy)

    assert inspection.artifact_type == "operator-wheel"


def test_operator_browser_gate_accepts_the_full_supported_python_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrowserPhaseReached(Exception):
        pass

    def reach_browser_phase(_port: int) -> None:
        raise BrowserPhaseReached

    wheel = tmp_path / "operator.whl"
    wheel.write_bytes(b"operator")
    state = SimpleNamespace(
        operator_wheel=wheel,
        operator_reproducible_digest=hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )
    probes = {minor: {"python": f"{minor}.0"} for minor in ("3.11", "3.12", "3.13", "3.14")}
    monkeypatch.setattr(release_workflow, "_await_local_port_released", reach_browser_phase)

    with pytest.raises(BrowserPhaseReached):
        release_workflow._run_operator_browser_gate(
            state,
            environment={},
            policy={},
            supported_python_probes=probes,
        )


def test_operator_wheel_rejects_changed_license_text(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    prefix = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/"
    contents[f"{prefix}licenses/LICENSE"] = b"license replaced\n"
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="license text digest mismatch"):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_rejects_changed_third_party_license_notice(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    prefix = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/"
    contents[f"{prefix}licenses/THIRD_PARTY_LICENSES.md"] = b"notice removed\n"
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="third-party license notice changed"):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_rejects_retired_document_reference(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    prefix = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/"
    notice = f"{prefix}licenses/THIRD_PARTY_LICENSES.md"
    retired_name = policy["retired_document_references"][0]
    contents[notice] += f"\nSee {retired_name.swapcase()} for details.\n".encode()
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(
        ArtifactPolicyError,
        match=rf"retired document {retired_name} referenced by {notice}",
    ):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_rejects_changed_public_brand_asset(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    brand = "operator_app/static/brand/picogrid-app-icon-192.png"
    contents[brand] += b"changed"
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="public brand asset hash changed"):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_rejects_changed_public_brand_dimensions(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    policy["public_brand_assets"]["brand/picogrid-app-icon-192.png"]["width"] = 193
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="public brand asset dimensions changed"):
        _inspect_operator_wheel(wheel, policy)


@pytest.mark.parametrize("size", [192, 512])
def test_public_app_icons_are_opaque_and_identical_between_products(size: int) -> None:
    policy = load_policy(POLICY_PATH)
    relative = f"brand/picogrid-app-icon-{size}.png"
    repository = POLICY_PATH.parents[1]
    docs_icon = (repository / "docs" / "site" / "public" / relative).read_bytes()
    operator_icon = (repository / "operator-app" / "frontend" / "public" / relative).read_bytes()

    assert docs_icon == operator_icon
    assert (
        hashlib.sha256(docs_icon).hexdigest() == policy["public_brand_assets"][relative]["sha256"]
    )
    assert docs_icon[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    metadata = policy["public_brand_assets"][relative]
    assert int.from_bytes(docs_icon[16:20], "big") == metadata["width"] == size
    assert int.from_bytes(docs_icon[20:24], "big") == metadata["height"] == size
    assert docs_icon[24:26] == b"\x08\x02"  # 8-bit opaque truecolor

    offset = 8
    chunk_types: list[bytes] = []
    while offset < len(docs_icon):
        length = int.from_bytes(docs_icon[offset : offset + 4], "big")
        end = offset + length + 12
        assert end <= len(docs_icon)
        chunk_types.append(docs_icon[offset + 4 : offset + 8])
        offset = end
    assert offset == len(docs_icon)
    assert chunk_types[-1] == b"IEND"
    assert b"tRNS" not in chunk_types


def test_generated_operator_frontend_rejects_changed_public_brand_asset(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY_PATH)
    frontend = tmp_path / "frontend"
    source = POLICY_PATH.parents[1] / "operator-app" / "frontend" / "public"
    expected = {
        name
        for name, spec in policy["public_brand_assets"].items()
        if "operator" in spec.get("surfaces", ["documentation", "operator"])
    }
    for name in expected:
        destination = frontend / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())

    inspection = release_workflow._inspect_generated_web_tree(
        frontend,
        policy,
        label="operator frontend fixture",
    )
    assert expected <= set(inspection["files"])

    changed = frontend / "brand" / "picogrid-wordmark-dark.png"
    changed.write_bytes(changed.read_bytes() + b"changed")
    with pytest.raises(release_workflow.VerificationError, match="public brand asset hash changed"):
        release_workflow._inspect_generated_web_tree(
            frontend,
            policy,
            label="operator frontend fixture",
        )


@pytest.mark.parametrize(
    "header_name, replacement, expected",
    [
        ("name", "Name: another-app", "project name"),
        ("version", "Version: 9.9.9", "version changed"),
        ("python", "Requires-Python: >=3.13", "Python requirement"),
        ("license", "License-Expression: MIT", "license marker"),
    ],
)
def test_operator_wheel_rejects_metadata_identity_drift(
    tmp_path: Path,
    header_name: str,
    replacement: str,
    expected: str,
) -> None:
    policy = load_policy(POLICY_PATH)
    header = {
        "license": f"License-Expression: {policy['license_expression']}",
        "name": f"Name: {_OPERATOR_PROJECT_NAME}",
        "python": f"Requires-Python: {policy['requires_python']}",
        "version": f"Version: {policy['project_version']}",
    }[header_name]
    contents = _operator_wheel_contents(policy)
    metadata = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/METADATA"
    contents[metadata] = contents[metadata].replace(header.encode(), replacement.encode())
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match=expected):
        _inspect_operator_wheel(wheel, policy)


@pytest.mark.parametrize(
    "member, replacement, expected",
    [
        (
            "entry_points.txt",
            b"[console_scripts]\npicogrid-ecn-operator = operator_app.other:main\n",
            "console entry point changed",
        ),
        ("top_level.txt", b"operator_application\n", "top-level package changed"),
    ],
)
def test_operator_wheel_rejects_installed_surface_drift(
    tmp_path: Path,
    member: str,
    replacement: bytes,
    expected: str,
) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    prefix = f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}.dist-info/"
    contents[f"{prefix}{member}"] = replacement
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match=expected):
        _inspect_operator_wheel(wheel, policy)


@pytest.mark.parametrize("change", ["missing", "unexpected"])
def test_operator_wheel_rejects_inventory_drift(tmp_path: Path, change: str) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    if change == "missing":
        contents.pop("operator_app/state.py")
    else:
        contents["operator_app/unreviewed.py"] = b""
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match="allowlist mismatch"):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_rejects_symbolic_link_member(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    wheel = _operator_wheel_path(tmp_path, policy)
    symlink = "operator_app/state.py"
    contents[symlink] = b"runtime.py"
    _write_operator_wheel(wheel, contents, symlink=symlink)

    with pytest.raises(ArtifactPolicyError, match="symbolic link"):
        _inspect_operator_wheel(wheel, policy)


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_PRIVATE_KEY_CANARY, "private key"),
        (_IPV4_CANARY, "non-loopback IPv4"),
    ],
)
def test_operator_wheel_scans_generated_frontend_for_secrets_and_addresses(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    script = next(
        name
        for name in contents
        if name.startswith("operator_app/static/assets/") and name.endswith(".js")
    )
    contents[script] = payload
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)

    with pytest.raises(ArtifactPolicyError, match=expected):
        _inspect_operator_wheel(wheel, policy)


@pytest.mark.parametrize("change", ["missing-client", "unexpected-dependency"])
def test_operator_wheel_requires_exact_runtime_dependencies(tmp_path: Path, change: str) -> None:
    policy = load_policy(POLICY_PATH)
    requirements = _operator_requirements(policy)
    if change == "missing-client":
        requirements.remove(f"picogrid-ecn-client=={policy['project_version']}")
    else:
        requirements.append("requests==2.32.5")
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(
        wheel,
        _operator_wheel_contents(policy, requirements=requirements),
    )

    with pytest.raises(ArtifactPolicyError, match="runtime requirements changed"):
        _inspect_operator_wheel(wheel, policy)


def test_operator_wheel_embeds_the_exact_generated_frontend(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    contents = _operator_wheel_contents(policy)
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, contents)
    frontend = tmp_path / "frontend"
    for name, data in _generated_frontend_contents(policy).items():
        target = frontend / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    inspection = release_workflow._require_operator_frontend_matches(
        wheel,
        frontend,
        policy,
    )

    expected = tuple(sorted(_generated_frontend_contents(policy)))
    assert tuple(inspection["files"]) == expected
    assert inspection["total_bytes"] == sum(
        len(data) for data in _generated_frontend_contents(policy).values()
    )
    assert len(inspection["sha256"]) == 64


def test_operator_frontend_comparison_rejects_byte_drift(tmp_path: Path) -> None:
    policy = load_policy(POLICY_PATH)
    wheel = _operator_wheel_path(tmp_path, policy)
    _write_operator_wheel(wheel, _operator_wheel_contents(policy))
    frontend = tmp_path / "frontend"
    generated = _generated_frontend_contents(policy)
    for name, data in generated.items():
        target = frontend / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    script = next(name for name in generated if name.startswith("assets/") and name.endswith(".js"))
    (frontend / script).write_bytes(b"different generated bytes\n")

    with pytest.raises(release_workflow.VerificationError, match=r"frontend.*differ"):
        release_workflow._require_operator_frontend_matches(wheel, frontend, policy)


def test_release_environment_discards_untrusted_prebuilt_frontend_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PICOGRID_OPERATOR_PREBUILT_FRONTEND", "/tmp/unreviewed-frontend")

    environment = release_workflow._base_environment(1_735_689_600)

    assert "PICOGRID_OPERATOR_PREBUILT_FRONTEND" not in environment


def test_candidate_cleanup_inspects_operator_wheel_before_removing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy(POLICY_PATH)
    repository = tmp_path / "repository"
    distribution = repository / "dist"
    distribution.mkdir(parents=True)
    operator_wheel = distribution / (
        f"{_OPERATOR_DISTRIBUTION_NAME}-{policy['project_version']}-py3-none-any.whl"
    )
    _write_operator_wheel(operator_wheel, _operator_wheel_contents(policy))
    monkeypatch.setattr(release_workflow, "REPOSITORY", repository)
    monkeypatch.setattr(release_workflow, "DIST_DIRECTORY", distribution)

    contents = _operator_wheel_contents(policy)
    contents["operator_app/unreviewed.py"] = b""
    _write_operator_wheel(operator_wheel, contents)
    with pytest.raises(release_workflow.VerificationError, match="failed publication inspection"):
        release_workflow._reset_candidate_artifacts(policy)
    assert operator_wheel.is_file()

    _write_operator_wheel(operator_wheel, _operator_wheel_contents(policy))
    release_workflow._reset_candidate_artifacts(policy)

    assert not operator_wheel.exists()
