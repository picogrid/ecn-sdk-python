# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
from base64 import b64decode
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from examples import (
    decode_public_protobuf,
    dispatch_task,
    get_ecn_location,
    observe_mesh_data,
    preflight,
    publish_entity,
    publish_location,
    receive_task,
    watch_detections,
    watch_tracks,
)
from picogrid_ecn_client import ECNClient, Entity, EntityCategory, Location, WireFormat
from picogrid_ecn_client.testing import FULL_ACCESS_TOKEN, MockECN

ROOT = Path(__file__).parents[2]
# The runnable inventory is derived from the committed manifest by the
# ``manifest_examples`` fixture, so a newly declared example cannot slip past the
# offline-check and import-boundary gates by being missing from a hand-written tuple.
ENVIRONMENT_KEYS = (
    "ECN_ACCURACY",
    "ECN_ALLOW_INSECURE",
    "ECN_ALTITUDE",
    "ECN_BEARER_TOKEN",
    "ECN_BEARING",
    "ECN_CA_CERT",
    "ECN_CLIENT_CERT",
    "ECN_CLIENT_KEY",
    "ECN_CLIENT_KEY_PASSWORD",
    "ECN_CONFIDENCE",
    "ECN_DISPLAY_NAME",
    "ECN_ENTITY_CATEGORY",
    "ECN_ENTITY_ID",
    "ECN_ENTITY_NAME",
    "ECN_ENTITY_TYPE",
    "ECN_HOST",
    "ECN_INTEGRATION_NAME",
    "ECN_LATITUDE",
    "ECN_LOCATION_SOURCE",
    "ECN_LONGITUDE",
    "ECN_MAX_EVENTS",
    "ECN_MAXIMUM_PAYLOAD_SIZE",
    "ECN_MQTT_PORT",
    "ECN_MQTT_USERNAME",
    "ECN_OBSERVED_INTEGRATIONS",
    "ECN_OBSERVATION_TIMEOUT",
    "ECN_PROTOBUF_PAYLOAD_FILE",
    "ECN_TARGET_ENTITY_ID",
    "ECN_TARGET_INTEGRATION",
    "ECN_TARGET_TERMINAL_ID",
    "ECN_TASK_COMMAND",
    "ECN_TASK_LIMIT",
    "ECN_TASK_MESSAGE",
    "ECN_TASK_MODE",
    "ECN_TASK_TIMEOUT",
    "ECN_TLS_VERIFY",
    "ECN_TERMINAL_ID",
    "ECN_WIRE_FORMAT",
)


def _configure_mock(
    monkeypatch: pytest.MonkeyPatch,
    mock: MockECN,
    integration: str,
    *,
    wire_format: WireFormat = WireFormat.JSON,
) -> None:
    for name in ENVIRONMENT_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ECN_HOST", mock.host)
    monkeypatch.setenv("ECN_MQTT_PORT", str(mock.mqtt_port))
    monkeypatch.setenv("ECN_INTEGRATION_NAME", integration)
    monkeypatch.setenv("ECN_BEARER_TOKEN", FULL_ACCESS_TOKEN)
    monkeypatch.setenv("ECN_ALLOW_INSECURE", "1")
    monkeypatch.setenv("ECN_WIRE_FORMAT", wire_format.value)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2) -> None:
    reached = asyncio.Event()
    loop = asyncio.get_running_loop()

    def check() -> None:
        if predicate():
            reached.set()
        else:
            loop.call_soon(check)

    loop.call_soon(check)
    await asyncio.wait_for(reached.wait(), timeout=timeout)


async def _publish_until_done(
    task: asyncio.Task[None],
    publish: Callable[[], Awaitable[object]],
) -> None:
    async with asyncio.timeout(2):
        while not task.done():
            await publish()
            await asyncio.sleep(0.01)
    await task


def test_every_required_example_has_a_no_network_check(
    tmp_path: Path,
    manifest_examples: tuple[str, ...],
) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ECN_") and key != "PYTHONPATH"
    }
    for filename in manifest_examples:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "examples" / filename), "--check"],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, (filename, completed.stderr)
        assert "offline check passed" in completed.stdout


def test_examples_import_only_public_sdk_modules(
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
    manifest_examples: tuple[str, ...],
) -> None:
    for filename in ("_common.py", *manifest_examples):
        tree = ast.parse((ROOT / "examples" / filename).read_text())
        assert_only_public_sdk_imports(tree)


def test_example_import_check_rejects_plain_private_import(
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
) -> None:
    tree = ast.parse("import picogrid_ecn_client._internal")

    with pytest.raises(AssertionError):
        assert_only_public_sdk_imports(tree)


@pytest.mark.parametrize(
    "source",
    [
        "from picogrid_ecn_client.workflows import _retention",
        "from picogrid_ecn_client import client",
        "from picogrid_ecn_client.workflows import observe",
        "from picogrid_ecn_client import _internal",
        "import picogrid_ecn_client.workflows._retention",
        "import picogrid_" + "example_sdk",
        "from picogrid_" + "example_sdk import x",
    ],
)
def test_example_import_check_rejects_non_public_sdk_names(
    source: str,
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
) -> None:
    with pytest.raises(AssertionError):
        assert_only_public_sdk_imports(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "from picogrid_ecn_client import ECNClient",
        "from picogrid_ecn_client.workflows import preflight",
        "from picogrid_ecn_client import workflows",
        "import picogrid_ecn_client",
        "from helpers import _private\nimport picogrid_ecn_client",
    ],
)
def test_example_import_check_accepts_public_sdk_names(
    source: str,
    assert_only_public_sdk_imports: Callable[[ast.AST], None],
) -> None:
    assert_only_public_sdk_imports(ast.parse(source))


@pytest.mark.asyncio
async def test_diagnostics_and_publication_examples(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entity_id = UUID("00000000-0000-4000-8000-000000000401")
    location_id = UUID("00000000-0000-4000-8000-000000000402")
    async with MockECN() as mock:
        _configure_mock(monkeypatch, mock, "example-publisher")
        await preflight.main()
        assert '"successful": true' in capsys.readouterr().out

        monkeypatch.setenv("ECN_ENTITY_ID", str(entity_id))
        monkeypatch.setenv("ECN_ENTITY_CATEGORY", "detection")
        monkeypatch.setenv("ECN_ENTITY_TYPE", "synthetic-detection")
        monkeypatch.setenv("ECN_ENTITY_NAME", "Example detection")
        await publish_entity.main()
        assert str(entity_id) in mock.entity_state
        assert '"kind": "entity"' in capsys.readouterr().out

        monkeypatch.setenv("ECN_ENTITY_ID", str(location_id))
        monkeypatch.setenv("ECN_LATITUDE", "34.0")
        monkeypatch.setenv("ECN_LONGITUDE", "-118.0")
        monkeypatch.setenv("ECN_ALTITUDE", "125")
        await publish_location.main()
        assert mock.location_state[str(location_id)]["latitude"] == 34.0
        assert '"kind": "location"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_get_location_example_observes_terminal_geolocation_without_an_entity_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entity_id = UUID("00000000-0000-4000-8000-000000000406")
    async with MockECN() as mock:
        _configure_mock(monkeypatch, mock, "example-observer")
        monkeypatch.setenv("ECN_OBSERVATION_TIMEOUT", "2")
        observer = asyncio.create_task(get_ecn_location.main())
        try:
            await _wait_until(lambda: mock.active_connection_count >= 1)
            async with ECNClient(mock.client_config("terminal-geolocation")) as publisher:
                location = Location(
                    latitude=34.0,
                    longitude=-118.0,
                    recorded_at=datetime.now(UTC),
                    source="terminal-geolocation",
                )
                await _publish_until_done(
                    observer,
                    lambda: publisher.locations.publish(
                        entity_id=entity_id,
                        location=location,
                    ),
                )
        finally:
            if not observer.done():
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
        output = capsys.readouterr().out
        assert str(entity_id) in output
        assert '"integration": "terminal-geolocation"' in output
        assert '"latitude": 34.0' in output
        assert '"longitude": -118.0' in output


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["entity", "location"])
async def test_observe_mesh_data_example_receives_each_mock_event_family(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event_kind: str,
) -> None:
    entity_id = UUID("00000000-0000-4000-8000-000000000407")
    async with MockECN() as mock:
        _configure_mock(monkeypatch, mock, "example-observer")
        monkeypatch.setenv("ECN_OBSERVED_INTEGRATIONS", "example-publisher")
        monkeypatch.setenv("ECN_MAX_EVENTS", "1")
        async with ECNClient(mock.client_config("example-publisher")) as publisher:
            observer = asyncio.create_task(observe_mesh_data.main())
            try:
                await _wait_until(lambda: mock.active_connection_count >= 2)
                if event_kind == "entity":
                    entity = Entity(
                        id=entity_id,
                        category=EntityCategory.TRACK,
                        integration="example-publisher",
                        recorded_at=datetime.now(UTC),
                        type="synthetic-event",
                    )
                    await _publish_until_done(observer, lambda: publisher.entities.publish(entity))
                else:
                    location = Location(
                        latitude=34.0,
                        longitude=-118.0,
                        recorded_at=datetime.now(UTC),
                        source="synthetic-sensor",
                    )
                    await _publish_until_done(
                        observer,
                        lambda: publisher.locations.publish(
                            entity_id=entity_id,
                            location=location,
                        ),
                    )
            finally:
                if not observer.done():
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
        output = capsys.readouterr().out
        assert str(entity_id) in output
        assert '"integration": "example-publisher"' in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_main", "category"),
    [
        (watch_tracks.main, EntityCategory.TRACK),
        (watch_detections.main, EntityCategory.DETECTION),
    ],
)
async def test_watch_examples_receive_mock_events(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module_main: Callable[[], Coroutine[Any, Any, None]],
    category: EntityCategory,
) -> None:
    async with MockECN() as mock:
        _configure_mock(monkeypatch, mock, "example-watcher")
        monkeypatch.setenv("ECN_MAX_EVENTS", "1")
        async with ECNClient(mock.client_config("example-publisher")) as publisher:
            task: asyncio.Task[None] = asyncio.create_task(module_main())
            await _wait_until(lambda: mock.active_connection_count >= 2)
            entity = Entity(
                id=UUID("00000000-0000-4000-8000-000000000404"),
                category=category,
                integration="example-publisher",
                recorded_at=datetime.now(UTC),
                type="synthetic-event",
            )
            try:
                await _publish_until_done(task, lambda: publisher.entities.publish(entity))
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        output = capsys.readouterr().out
        assert str(entity.id) in output
        assert f'"category": "{category.value}"' in output


@pytest.mark.asyncio
async def test_receive_and_dispatch_task_examples_interoperate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = UUID("00000000-0000-4000-8000-000000000405")
    async with MockECN() as mock:
        _configure_mock(monkeypatch, mock, "example-receiver")
        monkeypatch.setenv("ECN_ENTITY_ID", str(target))
        monkeypatch.setenv("ECN_TASK_COMMAND", "echo")
        monkeypatch.setenv("ECN_TASK_LIMIT", "1")
        receiver = asyncio.create_task(receive_task.main())
        try:
            await _wait_until(lambda: mock.active_connection_count >= 1)
            await asyncio.sleep(0.05)

            _configure_mock(monkeypatch, mock, "example-sender")
            monkeypatch.setenv("ECN_TARGET_ENTITY_ID", str(target))
            monkeypatch.setenv("ECN_TARGET_INTEGRATION", "example-receiver")
            monkeypatch.setenv("ECN_TASK_COMMAND", "echo")
            monkeypatch.setenv("ECN_TASK_MESSAGE", "synthetic hello")
            monkeypatch.setenv("ECN_TASK_TIMEOUT", "2")
            await dispatch_task.main()
            await asyncio.wait_for(receiver, timeout=2)
        finally:
            if not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        output = capsys.readouterr().out
        assert "synthetic hello" in output
        assert '"status": "SUCCESS"' in output


@pytest.mark.asyncio
async def test_offline_public_protobuf_decode_example(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    payload = b64decode(
        "CgYIgPLWygYSTAoQAAAAAAAAQACAAAAAAAACBBoZU3ludGhldGljIHByb3RvYnVmIGVudGl0eSIT"
        "c3ludGhldGljLWRldGVjdGlvbigCMAE4BEoCe30="
    )
    payload_path = tmp_path / "synthetic-entity.pb"
    payload_path.write_bytes(payload)
    monkeypatch.setenv("ECN_PROTOBUF_PAYLOAD_FILE", str(payload_path))
    monkeypatch.setenv("ECN_INTEGRATION_NAME", "offline-decoder")
    monkeypatch.setenv("ECN_ENTITY_CATEGORY", "DETECTION")
    await decode_public_protobuf.main()
    output = capsys.readouterr().out
    assert "00000000-0000-4000-8000-000000000204" in output
    assert '"integration": "offline-decoder"' in output
