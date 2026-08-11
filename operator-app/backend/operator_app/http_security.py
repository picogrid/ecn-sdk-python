# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Small ASGI request-size boundary for the local browser API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_STATIC_SECURITY_HEADERS = (
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


def _security_headers(scope: Scope) -> tuple[tuple[bytes, bytes], ...]:
    """Build a fail-closed policy with at most one validated tile origin."""

    basemap_origin: str | None = None
    try:
        state = cast(Any, scope["app"]).state
        runtime = getattr(state, "operator_runtime", None)
        candidate = getattr(getattr(runtime, "settings", None), "basemap_origin", None)
        if isinstance(candidate, str) and candidate.startswith("https://"):
            basemap_origin = candidate
    except (AttributeError, TypeError, ValueError):
        pass
    image_sources = "'self' data:"
    if basemap_origin is not None:
        image_sources = f"{image_sources} {basemap_origin}"
    policy = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src {image_sources}; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    return (
        (b"content-security-policy", policy.encode("ascii")),
        *_STATIC_SECURITY_HEADERS,
    )


class BrowserSecurityHeadersMiddleware:
    """Apply fail-closed browser response headers to HTML and local APIs."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                security_headers = _security_headers(scope)
                protected = {name for name, _value in security_headers}
                existing = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() not in protected
                ]
                message["headers"] = [*existing, *security_headers]
            await send(message)

        await self._app(scope, receive, send_with_security_headers)


class BoundedRequestBodyMiddleware:
    """Buffer only mutation bodies up to a fixed small limit before routing."""

    def __init__(self, app: ASGIApp, *, maximum_bytes: int) -> None:
        self._app = app
        self._maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self._app(scope, receive, send)
            return

        declared = self._content_length(scope.get("headers", ()))
        if declared is None:
            await self._reject(scope, receive, send, 400, "a valid Content-Length is required")
            return
        if declared > self._maximum_bytes:
            await self._reject(scope, receive, send, 413, "request body exceeds the size limit")
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self._maximum_bytes:
                await self._reject(scope, receive, send, 413, "request body exceeds the size limit")
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)

    @staticmethod
    def _content_length(headers: Sequence[tuple[bytes, bytes]]) -> int | None:
        values = [value for name, value in headers if name.lower() == b"content-length"]
        if len(values) != 1:
            return None
        try:
            parsed = int(values[0])
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse({"detail": detail}, status_code=status_code)(scope, receive, send)


class RuntimeTrustedHostMiddleware:
    """Enforce runtime-configured exact hosts for both HTTP and WebSocket scopes."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        host = self._host(scope.get("headers", ()))
        state = cast(Any, scope["app"]).state
        runtime = getattr(state, "operator_runtime", None)
        allowed = getattr(getattr(runtime, "settings", None), "allowed_hosts", ())
        if host is None or host not in allowed:
            if scope["type"] == "websocket":
                await send(
                    {"type": "websocket.close", "code": 1008, "reason": "host is not allowed"}
                )
            else:
                await JSONResponse({"detail": "host is not allowed"}, status_code=400)(
                    scope, receive, send
                )
            return
        await self._app(scope, receive, send)

    @staticmethod
    def _host(headers: Sequence[tuple[bytes, bytes]]) -> str | None:
        values = [value for name, value in headers if name.lower() == b"host"]
        if len(values) != 1:
            return None
        try:
            raw = values[0].decode("ascii")
            if (
                not raw
                or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in raw)
                or any(delimiter in raw for delimiter in "/?#\\")
            ):
                return None
            parsed = urlsplit(f"//{raw}")
            port = parsed.port
        except (UnicodeDecodeError, ValueError):
            return None
        if (
            parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or (port is not None and not 0 < port <= 65535)
        ):
            return None
        return parsed.hostname


__all__ = [
    "BoundedRequestBodyMiddleware",
    "BrowserSecurityHeadersMiddleware",
    "RuntimeTrustedHostMiddleware",
]
