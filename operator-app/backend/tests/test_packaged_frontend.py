# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import re
from importlib import import_module, resources
from pathlib import Path

import httpx
import pytest

from operator_app.app import create_app
from operator_app.settings import OperatorSettings

app_module = import_module("operator_app.app")


def _read_only_demo_settings() -> OperatorSettings:
    return OperatorSettings.from_env(
        {
            "OPERATOR_MODE": "mock",
            "OPERATOR_ECN_CLIENT_INTEGRATION": "operator-console",
            "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "mock-sensor,mock-target",
            "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK,DETECTION,DEVICE",
            "OPERATOR_ECN_WIRE_FORMAT": "json",
            "OPERATOR_TASKING_ENABLED": "false",
            "OPERATOR_SYNTHETIC_PERIOD_SECONDS": "0.1",
        }
    )


def test_installed_package_contains_the_compiled_frontend() -> None:
    frontend = resources.files("operator_app").joinpath("static")
    index = frontend.joinpath("index.html")
    assets = frontend.joinpath("assets")
    brand = frontend.joinpath("brand")

    assert index.is_file()
    assert assets.is_dir()
    assert brand.is_dir()
    assert any(item.is_file() and item.name.endswith(".js") for item in assets.iterdir())
    assert any(item.is_file() and item.name.endswith(".css") for item in assets.iterdir())
    assert any(item.is_file() and item.name.endswith(".png") for item in brand.iterdir())


@pytest.mark.asyncio
async def test_server_uses_packaged_frontend_from_an_empty_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    application = create_app(_read_only_demo_settings())

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert index.headers["content-type"].startswith("text/html")
            assert "installed operator artifact is incomplete" not in index.text
            asset_match = re.search(r'href="(/assets/[^"]+\.css)"', index.text)
            assert asset_match is not None
            asset = await client.get(asset_match.group(1))
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith("text/css")

    runtime = application.state.operator_runtime
    assert runtime.client is None
    assert runtime._mock is None
    assert runtime._tasks == []


@pytest.mark.asyncio
async def test_server_reports_an_incomplete_artifact_when_the_frontend_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    monkeypatch.setattr(app_module, "_packaged_frontend", lambda: tmp_path / "absent")
    application = create_app(_read_only_demo_settings(), application_root=tmp_path)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            index = await client.get("/")

    assert index.status_code == 200
    assert index.json() == {
        "application": "operator backend",
        "frontend": "the installed operator artifact is incomplete",
    }


@pytest.mark.asyncio
async def test_source_server_uses_the_built_frontend_when_packaged_assets_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    source_frontend = tmp_path / "frontend" / "dist"
    assets = source_frontend / "assets"
    assets.mkdir(parents=True)
    (source_frontend / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/assets/operator.css">',
        encoding="utf-8",
    )
    (assets / "operator.css").write_text("body {}", encoding="utf-8")
    monkeypatch.setattr(app_module, "_packaged_frontend", lambda: tmp_path / "absent")
    application = create_app(_read_only_demo_settings(), application_root=tmp_path)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            index = await client.get("/")
            asset = await client.get("/assets/operator.css")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/css")
