# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""FastAPI surface for the local operator browser application.

These HTTP and WebSocket routes are local application APIs. ECN traffic remains MQTT
v5 through the installed public client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from picogrid_ecn_client import __version__ as sdk_version
from starlette.types import Message

from .api_models import (
    ConfirmTaskRequest,
    DiscardPreparedTaskRequest,
    PreparedTaskResponse,
    PrepareTaskRequest,
    RetireBrowserViewRequest,
    SafeConfigurationView,
    TaskConfirmationResponse,
)
from .http_security import (
    BoundedRequestBodyMiddleware,
    BrowserSecurityHeadersMiddleware,
    RuntimeTrustedHostMiddleware,
)
from .hub import BrowserHubClosedError, BrowserLimitError
from .runtime import OperatorActionError, OperatorRuntime
from .settings import OperatorSettings
from .state import EntityView, OperatorSnapshot


def _packaged_frontend() -> Path:
    """Return the frontend embedded in the installed operator package."""

    return Path(str(resources.files("operator_app").joinpath("static")))


def _frontend_root(application_root: Path | None) -> Path:
    """Prefer installed assets, with a source-build fallback for local development."""

    packaged = _packaged_frontend()
    if (packaged / "index.html").is_file():
        return packaged
    source_root = (
        application_root.resolve()
        if application_root is not None
        else Path(__file__).resolve().parents[2]
    )
    source_build = source_root / "frontend" / "dist"
    if (source_build / "index.html").is_file():
        return source_build
    return packaged


async def _serve_state_websocket(
    websocket: WebSocket,
    service: OperatorRuntime,
    *,
    view_id: UUID,
    view_generation: UUID,
) -> None:
    origin = websocket.headers.get("origin")
    if origin not in service.settings.allowed_origins:
        await websocket.close(code=1008, reason="origin is not allowed")
        return
    try:
        identifier, queue = await service.hub.register()
    except (BrowserLimitError, BrowserHubClosedError):
        await websocket.close(code=1013, reason="browser client limit reached")
        return

    sender: asyncio.Task[None] | None = None
    receiver: asyncio.Task[Message] | None = None
    view_active = False
    try:
        await websocket.accept()
        view_active = await service.activate_browser_view(view_id, view_generation)
        if not view_active:
            # The runtime refuses activation either because it is not serving yet or
            # because the identity is already bound to a live connection. Report which,
            # so a refused duplicate is not misread as a readiness problem.
            await websocket.close(
                code=1013,
                reason=(
                    "operator runtime is not ready"
                    if not service.running
                    else "operator view identity is already in use"
                ),
            )
            return
        await websocket.send_text((await service.snapshot()).model_dump_json(serialize_as_any=True))

        async def send_updates() -> None:
            while True:
                message = await queue.get()
                if message is None:
                    return
                await websocket.send_text(message)

        sender = asyncio.create_task(send_updates(), name="operator-websocket-sender")
        receiver = asyncio.create_task(websocket.receive(), name="operator-websocket-receiver")
        completed, _pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        if receiver in completed:
            message = receiver.result()
            if message["type"] != "websocket.disconnect":
                await websocket.close(code=1008, reason="state WebSocket is push-only")
        else:
            sender.result()
            await websocket.close(code=1001, reason="operator state stream closed")
    except WebSocketDisconnect:
        pass
    finally:
        tasks = tuple(task for task in (sender, receiver) if task is not None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if view_active:
                await service.deactivate_browser_view(view_id, view_generation)
        finally:
            await service.hub.unregister(identifier)


def create_app(
    settings: OperatorSettings | None = None,
    *,
    application_root: Path | None = None,
) -> FastAPI:
    root = (application_root or Path.cwd()).resolve()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = OperatorRuntime(settings or OperatorSettings.from_env(application_root=root))
        application.state.operator_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    application = FastAPI(
        title="Picogrid ECN Operator Application",
        version=sdk_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(BoundedRequestBodyMiddleware, maximum_bytes=20 * 1024)
    application.add_middleware(RuntimeTrustedHostMiddleware)
    application.add_middleware(BrowserSecurityHeadersMiddleware)

    def runtime(request: Request) -> OperatorRuntime:
        return cast(OperatorRuntime, request.app.state.operator_runtime)

    def require_browser_origin(request: Request) -> None:
        if request.headers.get("origin") not in runtime(request).settings.allowed_origins:
            raise HTTPException(status_code=403, detail="operator browser origin is not allowed")

    @application.exception_handler(OperatorActionError)
    async def operator_error(_request: Request, error: OperatorActionError) -> JSONResponse:
        content = {"detail": str(error)}
        if error.outcome_status is not None:
            content["outcome_status"] = error.outcome_status
        return JSONResponse(status_code=error.status_code, content=content)

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "request validation failed"})

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, object]:
        service = runtime(request)
        snapshot = await service.snapshot()
        connected = bool(snapshot.connection and snapshot.connection.mqtt_connected)
        return {"status": "ready" if service.running else "stopped", "mqtt_connected": connected}

    @application.get("/api/config")
    async def configuration(request: Request) -> SafeConfigurationView:
        return runtime(request).safe_configuration()

    @application.get("/api/state")
    async def state(request: Request) -> OperatorSnapshot:
        return await runtime(request).snapshot()

    @application.get("/api/entities/{integration}/{entity_id}")
    async def selected_entity(integration: str, entity_id: UUID, request: Request) -> EntityView:
        selected = await runtime(request).state.task_target(
            integration=integration,
            entity_id=entity_id,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="observed entity was not found")
        return selected

    @application.post("/api/tasks/prepare")
    async def prepare_task(
        payload: PrepareTaskRequest,
        request: Request,
        x_operator_intent: Annotated[str | None, Header()] = None,
        x_operator_view: Annotated[UUID | None, Header()] = None,
        x_operator_view_generation: Annotated[UUID | None, Header()] = None,
    ) -> PreparedTaskResponse:
        require_browser_origin(request)
        if x_operator_intent != "prepare":
            raise HTTPException(
                status_code=400, detail="explicit prepare intent header is required"
            )
        if x_operator_view is None:
            raise HTTPException(status_code=400, detail="operator view identity is required")
        if x_operator_view_generation is None:
            raise HTTPException(status_code=400, detail="operator view generation is required")
        return await runtime(request).prepare_task(
            payload,
            view_id=x_operator_view,
            view_generation=x_operator_view_generation,
        )

    @application.post("/api/view/retire")
    async def retire_browser_view(
        _payload: RetireBrowserViewRequest,
        request: Request,
        x_operator_intent: Annotated[str | None, Header()] = None,
        x_operator_view: Annotated[UUID | None, Header()] = None,
        x_operator_view_generation: Annotated[UUID | None, Header()] = None,
    ) -> dict[str, bool]:
        require_browser_origin(request)
        if x_operator_intent != "retire-view":
            raise HTTPException(
                status_code=400, detail="explicit retire-view intent header is required"
            )
        if x_operator_view is None:
            raise HTTPException(status_code=400, detail="operator view identity is required")
        if x_operator_view_generation is None:
            raise HTTPException(status_code=400, detail="operator view generation is required")
        await runtime(request).retire_browser_view(
            x_operator_view,
            x_operator_view_generation,
        )
        return {"retired": True}

    @application.post("/api/tasks/confirm")
    async def confirm_task(
        payload: ConfirmTaskRequest,
        request: Request,
        x_operator_intent: Annotated[str | None, Header()] = None,
        x_operator_view: Annotated[UUID | None, Header()] = None,
        x_operator_view_generation: Annotated[UUID | None, Header()] = None,
    ) -> TaskConfirmationResponse:
        require_browser_origin(request)
        if x_operator_intent != "confirm":
            raise HTTPException(
                status_code=400, detail="explicit confirm intent header is required"
            )
        if x_operator_view is None:
            raise HTTPException(status_code=400, detail="operator view identity is required")
        if x_operator_view_generation is None:
            raise HTTPException(status_code=400, detail="operator view generation is required")
        return await runtime(request).confirm_task(
            payload.preparation_token,
            view_id=x_operator_view,
            view_generation=x_operator_view_generation,
        )

    @application.post("/api/tasks/discard")
    async def discard_prepared_task(
        payload: DiscardPreparedTaskRequest,
        request: Request,
        x_operator_intent: Annotated[str | None, Header()] = None,
        x_operator_view: Annotated[UUID | None, Header()] = None,
        x_operator_view_generation: Annotated[UUID | None, Header()] = None,
    ) -> dict[str, bool]:
        require_browser_origin(request)
        if x_operator_intent != "discard":
            raise HTTPException(
                status_code=400, detail="explicit discard intent header is required"
            )
        if x_operator_view is None:
            raise HTTPException(status_code=400, detail="operator view identity is required")
        if x_operator_view_generation is None:
            raise HTTPException(status_code=400, detail="operator view generation is required")
        discarded = await runtime(request).discard_prepared_task(
            payload.preparation_token,
            view_id=x_operator_view,
            view_generation=x_operator_view_generation,
        )
        if discarded != 1:
            raise HTTPException(status_code=409, detail="prepared task is no longer available")
        return {"discarded": True}

    @application.websocket("/ws/state")
    async def websocket_state(
        websocket: WebSocket,
        view_id: UUID,
        view_generation: UUID,
    ) -> None:
        service = cast(OperatorRuntime, websocket.app.state.operator_runtime)
        await _serve_state_websocket(
            websocket,
            service,
            view_id=view_id,
            view_generation=view_generation,
        )

    frontend = _frontend_root(application_root)
    assets = frontend / "assets"
    brand = frontend / "brand"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")
    if brand.is_dir():
        application.mount("/brand", StaticFiles(directory=brand), name="brand")

    @application.get("/", response_model=None)
    async def index() -> Response:
        page = frontend / "index.html"
        if page.is_file():
            return FileResponse(page)
        return JSONResponse(
            {
                "application": "operator backend",
                "frontend": "the installed operator artifact is incomplete",
            }
        )

    return application


app = create_app()

__all__ = ["app", "create_app"]
