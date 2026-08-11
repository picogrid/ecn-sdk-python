# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Read-only credentials sourced from a local legion-system-auth installation.

The selected files and field names are grounded in
``picogrid/legion-system-auth@9f618b7ce1648789d816a49b8fd0ec0ab21ea24a``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from .exceptions import AuthenticationError, ConfigurationError

_CONFIGURATION_FILE: Final = "oauth_config.json"
_ACCESS_TOKEN_FILE: Final = "access_token.json"
_MAXIMUM_CREDENTIAL_FILE_SIZE: Final = 1024 * 1024
_SETUP_COMMAND: Final = "legion-auth setup"
_CONFIGURATION_FIELDS: Final = frozenset({"integrationId"})
_TOKEN_FIELDS: Final = frozenset({"access_token", "expires_at"})
_MAXIMUM_CREDENTIAL_IPC_SIZE: Final = 512 * 1024
_CREDENTIAL_EXCHANGE_TIMEOUT_SECONDS: Final = 5.0
_CREDENTIAL_CLEANUP_TIMEOUT_SECONDS: Final = 1.0
_CREDENTIAL_READER_CODE: Final = (
    "from picogrid_ecn_client._legion_auth import _credential_reader_process_main;"
    "_credential_reader_process_main()"
)
_CREDENTIAL_IO_TASK_NAME: Final = "picogrid-ecn-legion-credential-io"
_CREDENTIAL_WAIT_TASK_NAME: Final = "picogrid-ecn-legion-credential-wait"

_LegionFailure = Literal[
    "expired",
    "malformed",
    "missing_custom",
    "missing_default",
    "unsafe",
]
_FAILURE_MESSAGES: Final[dict[_LegionFailure, str]] = {
    "expired": (
        f"legion-system-auth access token is expired; wait for refresh or run `{_SETUP_COMMAND}`"
    ),
    "malformed": "legion-system-auth credential material is malformed",
    "missing_custom": (
        "legion-system-auth credentials are not configured at the selected storage; set "
        "ECN_LEGION_AUTH_STORAGE to that same profile reference and run "
        '`legion-auth setup --storage-path "$ECN_LEGION_AUTH_STORAGE"`'
    ),
    "missing_default": (
        f"legion-system-auth credentials are not configured; run `{_SETUP_COMMAND}`"
    ),
    "unsafe": "legion-system-auth credential storage failed local safety checks",
}


class _LegionCredentialError(AuthenticationError):
    """A locally authored, secret-safe Legion credential resolution failure."""

    def __init__(self, reason: _LegionFailure) -> None:
        self.reason = reason
        super().__init__(
            _FAILURE_MESSAGES[reason],
            code=(
                "legion_credentials_missing"
                if reason in {"missing_custom", "missing_default"}
                else f"legion_credentials_{reason}"
            ),
            operation="legion_auth.resolve",
        )


class LegionSystemAuthProvider:
    """Resolve the current integration username and JWT without refreshing it."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        values = os.environ if environ is None else environ
        self._storage_path = _storage_path(storage_path, values)
        self._missing_reason: Literal["missing_custom", "missing_default"] = (
            "missing_custom"
            if storage_path is not None
            or any(
                values.get(name, "").strip()
                for name in ("LEGION_AUTH_STORAGE_PATH", "STORAGE_PATH", "STORAGE_BASE_PATH")
            )
            else "missing_default"
        )

    def __repr__(self) -> str:
        return "LegionSystemAuthProvider()"

    async def __call__(self) -> tuple[str, str]:
        """Read and validate the current files on every invocation."""

        return await _read_credentials_in_subprocess(
            self._storage_path,
            self._missing_reason,
        )


def legion_system_auth_provider(
    storage_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LegionSystemAuthProvider:
    """Build a provider without invoking or managing ``legion-auth``."""

    return LegionSystemAuthProvider(storage_path, environ=environ)


async def _read_credentials_in_subprocess(
    storage_path: Path,
    missing_reason: Literal["missing_custom", "missing_default"],
) -> tuple[str, str]:
    request = json.dumps(
        {
            "storage_path": os.fspath(storage_path),
            "missing_reason": missing_reason,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > _MAXIMUM_CREDENTIAL_IPC_SIZE:
        raise _unsafe_credentials()

    # Keep creation inside the resolver task so asyncio owns partial transport
    # cleanup if cancellation lands before a Process handle is returned.
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _CREDENTIAL_READER_CODE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
            env=_credential_reader_environment(),
        )
    except Exception:
        raise _unsafe_credentials() from None

    exchange = asyncio.create_task(
        process.communicate(request),
        name=_CREDENTIAL_IO_TASK_NAME,
    )
    try:
        async with asyncio.timeout(_CREDENTIAL_EXCHANGE_TIMEOUT_SECONDS):
            stdout, _stderr = await asyncio.shield(exchange)
    except asyncio.CancelledError as original_cancellation:
        await _terminate_and_reap(process, exchange)
        raise original_cancellation
    except TimeoutError:
        await _terminate_and_reap(process, exchange)
        raise _unsafe_credentials() from None
    except Exception:
        await _terminate_and_reap(process, exchange)
        raise _unsafe_credentials() from None

    if process.returncode != 0 or stdout is None:
        raise _unsafe_credentials()
    return _decode_credential_response(stdout)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _wait_task_until(
    task: asyncio.Task[Any],
    *,
    deadline: float,
) -> bool:
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            continue
    return True


async def _drain_task(
    task: asyncio.Task[Any],
    *,
    deadline: float,
) -> bool:
    if not await _wait_task_until(task, deadline=deadline):
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return False
    _consume_task_result(task)
    return True


async def _terminate_and_reap(
    process: asyncio.subprocess.Process,
    exchange: asyncio.Task[tuple[bytes | None, bytes | None]] | None = None,
) -> None:
    deadline = asyncio.get_running_loop().time() + _CREDENTIAL_CLEANUP_TIMEOUT_SECONDS
    wait: asyncio.Task[int] | None = None
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        wait = asyncio.create_task(
            process.wait(),
            name=_CREDENTIAL_WAIT_TASK_NAME,
        )
    if exchange is not None:
        exchange.cancel()
    if wait is not None:
        await _drain_task(wait, deadline=deadline)
    if exchange is not None:
        await _drain_task(exchange, deadline=deadline)


def _credential_reader_environment() -> dict[str, str]:
    # An exact interpreter path plus isolated mode needs no caller environment.
    # Windows process creation can require its system-root variables, so retain
    # only those platform values rather than bearer tokens or application state.
    return {
        name: value
        for name in ("SystemRoot", "SYSTEMROOT", "WINDIR")
        if (value := os.environ.get(name))
    }


def _credential_reader_process_main() -> None:
    """Read one request and return one secret-safe response over anonymous pipes."""

    try:
        request_raw = sys.stdin.buffer.read(_MAXIMUM_CREDENTIAL_IPC_SIZE + 1)
        if len(request_raw) > _MAXIMUM_CREDENTIAL_IPC_SIZE:
            raise ValueError
        request = json.loads(request_raw, object_pairs_hook=_json_object)
        if not isinstance(request, dict) or set(request) != {"storage_path", "missing_reason"}:
            raise ValueError
        storage_path = request["storage_path"]
        missing_reason = request["missing_reason"]
        if not isinstance(storage_path, str) or missing_reason not in {
            "missing_custom",
            "missing_default",
        }:
            raise ValueError
        credentials = _read_current_credentials(
            Path(storage_path),
            cast('Literal["missing_custom", "missing_default"]', missing_reason),
        )
        response: dict[str, object] = {
            "status": "ok",
            "username": credentials[0],
            "token": credentials[1],
        }
    except _LegionCredentialError as error:
        response = {"status": "error", "reason": error.reason}
    except BaseException:
        response = {"status": "error", "reason": "unsafe"}

    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAXIMUM_CREDENTIAL_IPC_SIZE:
        encoded = b'{"status":"error","reason":"unsafe"}'
    with suppress(BrokenPipeError, OSError):
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def _decode_credential_response(raw: bytes) -> tuple[str, str]:
    if not raw or len(raw) > _MAXIMUM_CREDENTIAL_IPC_SIZE:
        raise _unsafe_credentials()
    try:
        response = json.loads(raw, object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _unsafe_credentials() from None
    if not isinstance(response, dict):
        raise _unsafe_credentials()
    if response.get("status") == "error" and set(response) == {"status", "reason"}:
        reason = response.get("reason")
        if reason not in _FAILURE_MESSAGES:
            raise _unsafe_credentials()
        raise _LegionCredentialError(cast("_LegionFailure", reason))
    if response.get("status") != "ok" or set(response) != {"status", "username", "token"}:
        raise _unsafe_credentials()
    username = _integration_id({"integrationId": response.get("username")})
    token = _access_token({"access_token": response.get("token")})
    return username, token


def _storage_path(
    explicit_path: str | Path | None,
    environ: Mapping[str, str],
) -> Path:
    if explicit_path is not None:
        raw = str(explicit_path).strip()
    else:
        raw = (
            environ.get("LEGION_AUTH_STORAGE_PATH", "").strip()
            or environ.get("STORAGE_PATH", "").strip()
        )
        if not raw:
            storage_base = environ.get("STORAGE_BASE_PATH", "").strip() or "/etc"
            raw = str(Path(storage_base) / "picogrid" / "auth")
    try:
        candidate = Path(raw).expanduser()
    except RuntimeError:
        raise ConfigurationError(
            "legion-system-auth storage must be an absolute safe path"
        ) from None
    if not raw or not candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigurationError("legion-system-auth storage must be an absolute safe path")
    return candidate


def _read_credential_json(
    storage_path: Path,
    name: str,
    *,
    selected_fields: frozenset[str],
    missing_reason: Literal["missing_custom", "missing_default"],
) -> dict[str, object]:
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_descriptor = os.open(storage_path, directory_flags)
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) & 0o022
        ):
            raise _unsafe_credentials()

        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_descriptor = os.open(name, file_flags, dir_fd=directory_descriptor)
        metadata = os.fstat(file_descriptor)
        # The pinned local service writes 0640 documents so its authorized service
        # group can read them. Preserve that contract while rejecting group write
        # or execute and every permission for other users.
        prohibited_permissions = stat.S_IMODE(metadata.st_mode) & 0o037
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or prohibited_permissions
            or metadata.st_size > _MAXIMUM_CREDENTIAL_FILE_SIZE
        ):
            raise _unsafe_credentials()
        raw = _read_descriptor(file_descriptor)
    except FileNotFoundError:
        raise _LegionCredentialError(missing_reason) from None
    except _LegionCredentialError:
        raise
    except OSError:
        raise _unsafe_credentials() from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    try:
        value = json.loads(
            raw,
            object_pairs_hook=lambda pairs: _selected_json_object(pairs, selected_fields),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _LegionCredentialError("malformed") from None
    if not isinstance(value, dict):
        raise _LegionCredentialError("malformed")
    return value


def _read_current_credentials(
    storage_path: Path,
    missing_reason: Literal["missing_custom", "missing_default"],
) -> tuple[str, str]:
    configuration = _read_credential_json(
        storage_path,
        _CONFIGURATION_FILE,
        selected_fields=_CONFIGURATION_FIELDS,
        missing_reason=missing_reason,
    )
    token_document = _read_credential_json(
        storage_path,
        _ACCESS_TOKEN_FILE,
        selected_fields=_TOKEN_FIELDS,
        missing_reason=missing_reason,
    )
    username = _integration_id(configuration)
    token = _access_token(token_document)
    _validate_expiry(token_document, token)
    return username, token


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAXIMUM_CREDENTIAL_FILE_SIZE + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > _MAXIMUM_CREDENTIAL_FILE_SIZE:
        raise _unsafe_credentials()
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _selected_json_object(
    pairs: list[tuple[str, object]],
    selected_fields: frozenset[str],
) -> dict[str, object]:
    value = _json_object(pairs)
    return {key: item for key, item in value.items() if key in selected_fields}


def _integration_id(configuration: Mapping[str, object]) -> str:
    value = configuration.get("integrationId")
    if not isinstance(value, str):
        raise _LegionCredentialError("malformed")
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or _has_control_character(normalized):
        raise _LegionCredentialError("malformed")
    return normalized


def _access_token(document: Mapping[str, object]) -> str:
    value = document.get("access_token")
    if not isinstance(value, str):
        raise _LegionCredentialError("malformed")
    token = value.strip()
    if not token or len(token) > 64 * 1024 or _has_control_character(token):
        raise _LegionCredentialError("malformed")
    return token


def _validate_expiry(document: Mapping[str, object], token: str) -> None:
    expires_at = document.get("expires_at")
    if not isinstance(expires_at, str):
        raise _LegionCredentialError("malformed")
    try:
        stored_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        raise _LegionCredentialError("malformed") from None
    if stored_expiry.tzinfo is None or stored_expiry.utcoffset() is None:
        raise _LegionCredentialError("malformed")

    jwt_expiry = _jwt_expiry(token)
    now = datetime.now(UTC)
    if stored_expiry.astimezone(UTC) <= now or jwt_expiry <= now:
        raise _LegionCredentialError("expired")


def _jwt_expiry(token: str) -> datetime:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise _LegionCredentialError("malformed")
    try:
        header = json.loads(_decode_base64url(parts[0]), object_pairs_hook=_json_object)
        payload_raw = _decode_base64url(parts[1])
        payload = json.loads(payload_raw, object_pairs_hook=_json_object)
        signature = _decode_base64url(parts[2])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise _LegionCredentialError("malformed") from None
    if not isinstance(header, dict) or not isinstance(payload, dict) or not signature:
        raise _LegionCredentialError("malformed")
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or not algorithm.strip() or algorithm.casefold() == "none":
        raise _LegionCredentialError("malformed")
    expires = payload.get("exp")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise _LegionCredentialError("malformed")
    try:
        return datetime.fromtimestamp(expires, UTC)
    except (OverflowError, OSError, ValueError):
        raise _LegionCredentialError("malformed") from None


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _unsafe_credentials() -> _LegionCredentialError:
    return _LegionCredentialError("unsafe")


__all__ = ["LegionSystemAuthProvider", "legion_system_auth_provider"]
