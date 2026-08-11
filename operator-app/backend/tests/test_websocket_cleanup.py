# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from starlette.types import ASGIApp, Message, Scope
from starlette.websockets import WebSocket

from operator_app.api_models import PreparedTaskResponse, PrepareTaskRequest
from operator_app.app import _serve_state_websocket, create_app
from operator_app.runtime import OperatorActionError, OperatorRuntime
from operator_app.settings import OperatorSettings
from operator_app.state import EntityView

_APPLICATION_ROOT = Path(__file__).resolve().parents[2]
_VIEW_ID = UUID("00000000-0000-4000-8000-000000000101")
_MOCK_TARGET_ID = UUID("00000000-0000-4000-8000-000000000201")
_VIEW_GENERATION = UUID("00000000-0000-4000-8000-000000000301")
_OTHER_VIEW_GENERATION = UUID("00000000-0000-4000-8000-000000000302")


def _settings(*, tasking: bool = False) -> OperatorSettings:
    environment = {
        "OPERATOR_MODE": "mock",
        "OPERATOR_ECN_CLIENT_INTEGRATION": "operator-console",
        "OPERATOR_ECN_INTEGRATION_ALLOWLIST": "mock-sensor,mock-target",
        "OPERATOR_ECN_CATEGORY_ALLOWLIST": "TRACK,DETECTION,DEVICE",
        "OPERATOR_ECN_WIRE_FORMAT": "json",
        "OPERATOR_TASKING_ENABLED": str(tasking).lower(),
        "OPERATOR_COMMANDS_FILE": "config/commands.example.json",
        "OPERATOR_SYNTHETIC_PERIOD_SECONDS": "0.1",
        "OPERATOR_STALE_AFTER_SECONDS": "2",
    }
    if tasking:
        environment["OPERATOR_TASK_ENTITY_ALLOWLIST"] = str(_MOCK_TARGET_ID)
    return OperatorSettings.from_env(environment, application_root=_APPLICATION_ROOT)


async def _websocket_boundary_messages(
    application: ASGIApp,
    *,
    host: str,
    origin: str,
) -> list[Message]:
    incoming: list[Message] = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1000},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        "path": "/ws/state",
        "raw_path": b"/ws/state",
        "query_string": (f"view_id={_VIEW_ID}&view_generation={_VIEW_GENERATION}".encode()),
        "headers": [
            (b"host", host.encode()),
            (b"origin", origin.encode()),
        ],
        "subprotocols": [],
        "state": {},
        "extensions": {},
    }
    await application(scope, receive, send)
    return sent


async def _wait_for_target(runtime: OperatorRuntime) -> EntityView:
    for _attempt in range(100):
        target = next(
            (
                item
                for item in (await runtime.snapshot()).entities
                if item.type == "synthetic-task-target"
            ),
            None,
        )
        if target is not None:
            return target
        await asyncio.sleep(0.05)
    raise AssertionError("mock task target was not observed before the deadline")


class _FailingInitialSendWebSocket:
    def __init__(self) -> None:
        self.headers = {"origin": "http://127.0.0.1:4173"}

    async def accept(self) -> None:
        return None

    async def send_text(self, _message: str) -> None:
        raise RuntimeError("synthetic initial send failure")

    async def receive_text(self) -> str:
        raise AssertionError("receive must not run after initial send failure")

    async def close(self, *, code: int, reason: str) -> None:
        raise AssertionError(f"unexpected close {code}: {reason}")


class _ClosableWebSocket:
    def __init__(self) -> None:
        self.headers = {"origin": "http://127.0.0.1:4173"}
        self.accepted = asyncio.Event()
        self.initial_sent = asyncio.Event()
        self.closed: tuple[int, str] | None = None
        self._incoming = asyncio.Event()

    async def accept(self) -> None:
        self.accepted.set()

    async def send_text(self, _message: str) -> None:
        self.initial_sent.set()

    async def receive(self) -> dict[str, object]:
        await self._incoming.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def disconnect(self) -> None:
        self._incoming.set()


async def _start_websocket(
    runtime: OperatorRuntime,
    *,
    view_id: UUID = _VIEW_ID,
    view_generation: UUID = _VIEW_GENERATION,
) -> tuple[_ClosableWebSocket, asyncio.Task[None]]:
    raw_websocket = _ClosableWebSocket()
    websocket = cast(WebSocket, raw_websocket)
    serving = asyncio.create_task(
        _serve_state_websocket(
            websocket,
            runtime,
            view_id=view_id,
            view_generation=view_generation,
        )
    )
    await asyncio.wait_for(raw_websocket.initial_sent.wait(), timeout=1)
    return raw_websocket, serving


async def _disconnect_websocket(
    websocket: _ClosableWebSocket,
    serving: asyncio.Task[None],
) -> None:
    websocket.disconnect()
    await asyncio.wait_for(serving, timeout=1)


@pytest.mark.parametrize(
    ("host", "origin", "reason"),
    [
        ("invalid", "http://127.0.0.1:4173", "host is not allowed"),
        ("localhost", "https://example.invalid", "origin is not allowed"),
    ],
)
@pytest.mark.asyncio
async def test_websocket_asgi_boundary_rejects_untrusted_host_or_origin(
    host: str,
    origin: str,
    reason: str,
    fail_on_unhandled_loop_exception: None,
) -> None:
    application = create_app(_settings(), application_root=_APPLICATION_ROOT)
    mock = None
    async with application.router.lifespan_context(application):
        runtime = application.state.operator_runtime
        mock = runtime._mock
        assert mock is not None
        messages = await _websocket_boundary_messages(
            application,
            host=host,
            origin=origin,
        )
        assert messages == [{"type": "websocket.close", "code": 1008, "reason": reason}]
        assert runtime.hub.client_count == 0
        assert runtime._active_views == {}

    assert mock is not None
    assert mock.active_connection_count == 0
    assert mock.active_task_count == 0


async def _assert_view_released(runtime: OperatorRuntime) -> None:
    assert await runtime.activate_browser_view(_VIEW_ID, _OTHER_VIEW_GENERATION) is True
    assert await runtime.deactivate_browser_view(_VIEW_ID, _OTHER_VIEW_GENERATION) == 0


@pytest.mark.asyncio
async def test_initial_websocket_send_failure_unregisters_browser_queue(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings())
    await runtime.start()
    websocket = cast(WebSocket, _FailingInitialSendWebSocket())

    try:
        with pytest.raises(RuntimeError, match="initial send failure"):
            await _serve_state_websocket(
                websocket,
                runtime,
                view_id=_VIEW_ID,
                view_generation=_VIEW_GENERATION,
            )

        assert runtime.hub.client_count == 0
        await _assert_view_released(runtime)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_hub_close_unblocks_active_websocket_handler(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings())
    await runtime.start()
    raw_websocket, serving = await _start_websocket(runtime)

    try:
        await runtime.hub.close()
        await asyncio.wait_for(serving, timeout=1)

        assert raw_websocket.closed == (1001, "operator state stream closed")
        assert runtime.hub.client_count == 0
        await _assert_view_released(runtime)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_websocket_teardown_wins_before_late_preparation_insertion(
    monkeypatch: pytest.MonkeyPatch,
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(tasking=True))
    await runtime.start()
    websocket, serving = await _start_websocket(runtime)
    preparing: asyncio.Task[PreparedTaskResponse] | None = None
    release_target = asyncio.Event()
    try:
        target = await _wait_for_target(runtime)
        original_task_target = runtime.state.task_target
        target_read = asyncio.Event()
        first_read = True

        async def task_target_with_barrier(
            *, integration: str, entity_id: UUID
        ) -> EntityView | None:
            nonlocal first_read
            observed = await original_task_target(
                integration=integration,
                entity_id=entity_id,
            )
            if first_read:
                first_read = False
                target_read.set()
                await release_target.wait()
            return observed

        monkeypatch.setattr(runtime.state, "task_target", task_target_with_barrier)
        preparing = asyncio.create_task(
            runtime.prepare_task(
                PrepareTaskRequest(
                    entity_id=target.entity_id,
                    integration=target.integration,
                    command="echo",
                    payload={"message": "disconnect before token insertion"},
                ),
                view_id=_VIEW_ID,
                view_generation=_VIEW_GENERATION,
            )
        )
        await asyncio.wait_for(target_read.wait(), timeout=1)
        await _disconnect_websocket(websocket, serving)
        assert await runtime.activate_browser_view(_VIEW_ID, _OTHER_VIEW_GENERATION) is False
        release_target.set()

        with pytest.raises(OperatorActionError, match="browser view is disconnected") as error:
            await asyncio.wait_for(preparing, timeout=1)
        assert error.value.status_code == 409
        assert runtime._prepared == {}
        await _assert_view_released(runtime)
    finally:
        release_target.set()
        if preparing is not None and not preparing.done():
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        if not serving.done():
            websocket.disconnect()
            await asyncio.gather(serving, return_exceptions=True)
        await runtime.stop()


@pytest.mark.asyncio
async def test_duplicate_view_websocket_is_refused_without_extending_view_lifetime(
    fail_on_unhandled_loop_exception: None,
) -> None:
    runtime = OperatorRuntime(_settings(tasking=True))
    await runtime.start()
    first, first_serving = await _start_websocket(runtime)
    second = _ClosableWebSocket()
    second_serving = asyncio.create_task(
        _serve_state_websocket(
            cast(WebSocket, second),
            runtime,
            view_id=_VIEW_ID,
            view_generation=_OTHER_VIEW_GENERATION,
        )
    )
    try:
        await asyncio.wait_for(second_serving, timeout=1)
        assert second.closed == (1013, "operator view identity is already in use")

        await _disconnect_websocket(first, first_serving)
        assert runtime._prepared == {}
        await _assert_view_released(runtime)
    finally:
        for websocket, serving in (
            (first, first_serving),
            (second, second_serving),
        ):
            if not serving.done():
                websocket.disconnect()
        await asyncio.gather(first_serving, second_serving, return_exceptions=True)
        await runtime.stop()
