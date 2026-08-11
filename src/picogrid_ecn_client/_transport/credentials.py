# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""TLS material snapshots, context construction, and attempt lifecycle."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import ssl
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, cast

from ..auth import CertificateMaterial, MTLSAuth, PrivateKeyMaterial, TLSConfig
from ..exceptions import AuthenticationError, ConfigurationError

_MAXIMUM_TLS_MATERIAL_BYTES: Final = 1024 * 1024
_MAXIMUM_PRIVATE_KEY_PASSWORD_BYTES: Final = 64 * 1024
_MAXIMUM_TLS_MATERIAL_PATH_BYTES: Final = 16 * 1024
_MAXIMUM_TLS_MATERIAL_REQUEST_BYTES: Final = 32 * 1024
_MAXIMUM_TLS_MATERIAL_RESPONSE_BYTES: Final = 5 * 1024 * 1024
_TLS_MATERIAL_CLEANUP_TIMEOUT_SECONDS: Final = 1.0
_TLS_MATERIAL_READER_CODE: Final = (
    "from picogrid_ecn_client._transport.credentials import "
    "_tls_material_reader_process_main;_tls_material_reader_process_main()"
)
_TLS_MATERIAL_IO_TASK_NAME: Final = "picogrid-ecn-tls-material-io"
_TLS_MATERIAL_CLEANUP_TASK_NAME: Final = "picogrid-ecn-tls-material-cleanup"

_TLSMaterialRole = Literal["ca", "client_certificate", "client_key"]
_TLS_MATERIAL_ROLE_PRECEDENCE: Final[tuple[_TLSMaterialRole, ...]] = (
    "ca",
    "client_certificate",
    "client_key",
)


@dataclass(frozen=True, slots=True)
class _PreparedTLSMaterial:
    ca: bytes | None = field(default=None, repr=False)
    client_certificate: bytes | None = field(default=None, repr=False)
    client_key: bytes | None = field(default=None, repr=False)


class _TLSMaterialReadFailure(Exception):
    """Secret-safe marker for one unusable TLS material role."""

    def __init__(self, role: _TLSMaterialRole) -> None:
        self.role = role
        super().__init__("TLS material could not be read safely")


class TemporaryCertificateFiles:
    """Materialize secret PEM text in private files and remove it deterministically."""

    def __init__(self) -> None:
        self._directory: Path | None = None
        self._paths: dict[str, Path] = {}
        self._closed = False

    @property
    def directory(self) -> Path | None:
        return self._directory

    def _ensure_directory(self) -> Path:
        if self._closed:
            raise RuntimeError("temporary certificate storage is closed")
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="picogrid-ecn-tls-"))
            self._directory.chmod(0o700)
        return self._directory

    def _write_bytes(self, name: str, data: bytes) -> Path:
        existing = self._paths.get(name)
        if existing is not None:
            return existing
        path = self._ensure_directory() / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        path.chmod(0o600)
        self._paths[name] = path
        return path

    def _write(self, name: str, data: str) -> Path:
        return self._write_bytes(name, data.encode("utf-8"))

    def certificate_path(self, material: CertificateMaterial, *, name: str) -> Path:
        if material.path is not None:
            return material.path
        assert material.data is not None
        return self._write(name, material.data.get_secret_value())

    def private_key_path(self, material: PrivateKeyMaterial, *, name: str) -> Path:
        if material.path is not None:
            return material.path
        assert material.data is not None
        return self._write(name, material.data.get_secret_value())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        directory = self._directory
        self._paths.clear()
        self._directory = None
        if directory is not None:
            # The target is the exact unique directory returned by mkdtemp above.
            with suppress(FileNotFoundError):
                shutil.rmtree(directory)

    def __enter__(self) -> TemporaryCertificateFiles:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_client_ssl_context(
    tls: TLSConfig,
    auth: object,
    temporary_files: TemporaryCertificateFiles,
    *,
    wall_time: Callable[[], float] = time.time,
    _prepared_material: _PreparedTLSMaterial | None = None,
) -> ssl.SSLContext | None:
    """Create one server-auth TLS context without persisting in-memory PEMs."""

    if not tls.enabled:
        return None
    try:
        context = _build_server_auth_context(
            tls,
            temporary_files,
            prepared_material=_prepared_material,
        )
    except (OSError, ssl.SSLError, ValueError):
        temporary_files.close()
        raise ConfigurationError(
            "unable to configure TLS verification material",
            operation="configure_tls",
        ) from None
    if isinstance(auth, MTLSAuth):
        try:
            if _prepared_material is None:
                certificate = temporary_files.certificate_path(
                    auth.client_certificate,
                    name="client-certificate.pem",
                )
                key = temporary_files.private_key_path(
                    auth.client_key,
                    name="client-key.pem",
                )
            else:
                if (
                    _prepared_material.client_certificate is None
                    or _prepared_material.client_key is None
                ):
                    raise ValueError("prepared mutual-TLS material is incomplete")
                certificate = temporary_files._write_bytes(
                    "client-certificate.pem",
                    _prepared_material.client_certificate,
                )
                key = temporary_files._write_bytes(
                    "client-key.pem",
                    _prepared_material.client_key,
                )
            _validate_client_certificate_window(certificate, wall_time=wall_time)
            context.load_cert_chain(
                certfile=str(certificate),
                keyfile=str(key),
                password=(
                    auth.client_key.password.get_secret_value()
                    if auth.client_key.password is not None
                    else None
                ),
            )
        except AuthenticationError:
            temporary_files.close()
            raise
        except (OSError, ssl.SSLError, ValueError):
            temporary_files.close()
            raise AuthenticationError(
                "unable to load mutual-TLS credential material",
                operation="mqtt.authenticate",
            ) from None
    return context


async def build_lifecycle_owned_client_ssl_context(
    tls: TLSConfig,
    auth: object,
) -> ssl.SSLContext | None:
    """Build an attempt context with deadline-bounded cancellation cleanup.

    Caller-referenced files are first copied through a disposable subprocess. On
    cancellation, the process is killed and its reader is drained up to a fixed
    cleanup deadline. An uncooperative asyncio transport may finish later in a
    detached task whose result is consumed. ``SSLContext`` itself cannot be
    transferred between processes, so bounded local snapshots are parsed on the
    event-loop thread and temporary PEM files remain scope-owned.
    """

    if not tls.enabled:
        return None
    prepared_material = await _prepare_tls_material(tls, auth)
    with TemporaryCertificateFiles() as temporary_files:
        return build_client_ssl_context(
            tls,
            auth,
            temporary_files,
            _prepared_material=prepared_material,
        )


async def _prepare_tls_material(
    tls: TLSConfig,
    auth: object,
) -> _PreparedTLSMaterial:
    inline: dict[_TLSMaterialRole, bytes] = {}
    paths: dict[_TLSMaterialRole, str] = {}

    if tls.verify and tls.ca_certificate is not None:
        _select_material(tls.ca_certificate, "ca", inline=inline, paths=paths)
    if isinstance(auth, MTLSAuth):
        _validate_private_key_password(auth.client_key)
        _select_material(
            auth.client_certificate,
            "client_certificate",
            inline=inline,
            paths=paths,
        )
        _select_material(auth.client_key, "client_key", inline=inline, paths=paths)

    if paths:
        try:
            inline.update(await _read_tls_paths_in_subprocess(paths))
        except _TLSMaterialReadFailure as error:
            _raise_tls_material_error(error.role)

    return _PreparedTLSMaterial(
        ca=inline.get("ca"),
        client_certificate=inline.get("client_certificate"),
        client_key=inline.get("client_key"),
    )


def _select_material(
    material: CertificateMaterial | PrivateKeyMaterial,
    role: _TLSMaterialRole,
    *,
    inline: dict[_TLSMaterialRole, bytes],
    paths: dict[_TLSMaterialRole, str],
) -> None:
    if material.path is not None:
        path = os.fspath(material.path)
        if not path or len(path) > _MAXIMUM_TLS_MATERIAL_PATH_BYTES:
            _raise_tls_material_error(role)
        try:
            encoded_path = os.fsencode(path)
        except (UnicodeEncodeError, ValueError):
            _raise_tls_material_error(role)
        if (
            not encoded_path
            or len(encoded_path) > _MAXIMUM_TLS_MATERIAL_PATH_BYTES
            or b"\x00" in encoded_path
        ):
            _raise_tls_material_error(role)
        paths[role] = path
        return
    assert material.data is not None
    secret_value = material.data.get_secret_value()
    if not secret_value or len(secret_value) > _MAXIMUM_TLS_MATERIAL_BYTES:
        _raise_tls_material_error(role)
    try:
        value = secret_value.encode("utf-8")
    except UnicodeEncodeError:
        _raise_tls_material_error(role)
    if not value or len(value) > _MAXIMUM_TLS_MATERIAL_BYTES:
        _raise_tls_material_error(role)
    inline[role] = value


def _validate_private_key_password(material: PrivateKeyMaterial) -> None:
    if material.password is None:
        return
    value = material.password.get_secret_value()
    if len(value) > _MAXIMUM_PRIVATE_KEY_PASSWORD_BYTES:
        _raise_tls_material_error("client_key")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _raise_tls_material_error("client_key")
    if len(encoded) > _MAXIMUM_PRIVATE_KEY_PASSWORD_BYTES:
        _raise_tls_material_error("client_key")


async def _read_tls_paths_in_subprocess(
    paths: Mapping[_TLSMaterialRole, str],
) -> dict[_TLSMaterialRole, bytes]:
    request = json.dumps({"paths": paths}, separators=(",", ":")).encode("utf-8")
    first_role = _preferred_tls_material_role(paths)
    if len(request) > _MAXIMUM_TLS_MATERIAL_REQUEST_BYTES:
        raise _TLSMaterialReadFailure(first_role)

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _TLS_MATERIAL_READER_CODE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
            env=_tls_material_reader_environment(),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _TLSMaterialReadFailure(first_role) from None

    exchange = asyncio.create_task(
        process.communicate(request),
        name=_TLS_MATERIAL_IO_TASK_NAME,
    )
    try:
        stdout, _stderr = await asyncio.shield(exchange)
    except asyncio.CancelledError as original_cancellation:
        await _terminate_tls_material_reader(process, exchange)
        raise original_cancellation
    except Exception:
        await _terminate_tls_material_reader(process, exchange)
        raise _TLSMaterialReadFailure(first_role) from None

    if process.returncode != 0 or stdout is None:
        raise _TLSMaterialReadFailure(first_role)
    return _decode_tls_material_response(stdout, expected_roles=frozenset(paths))


async def _terminate_tls_material_reader(
    process: asyncio.subprocess.Process,
    exchange: asyncio.Task[tuple[bytes | None, bytes | None]],
) -> None:
    deadline = asyncio.get_running_loop().time() + _TLS_MATERIAL_CLEANUP_TIMEOUT_SECONDS
    wait: asyncio.Task[int] | None = None
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        wait = asyncio.create_task(
            process.wait(),
            name=_TLS_MATERIAL_CLEANUP_TASK_NAME,
        )
    exchange.cancel()
    if wait is not None:
        await _drain_tls_material_task(wait, deadline=deadline)
    await _drain_tls_material_task(exchange, deadline=deadline)


def _consume_tls_material_task_result(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _wait_tls_material_task_until(
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


async def _drain_tls_material_task(
    task: asyncio.Task[Any],
    *,
    deadline: float,
) -> bool:
    if not await _wait_tls_material_task_until(task, deadline=deadline):
        task.cancel()
        task.add_done_callback(_consume_tls_material_task_result)
        return False
    _consume_tls_material_task_result(task)
    return True


def _tls_material_reader_environment() -> dict[str, str]:
    return {
        name: value
        for name in ("SystemRoot", "SYSTEMROOT", "WINDIR")
        if (value := os.environ.get(name))
    }


def _tls_material_reader_process_main() -> None:
    """Read bounded regular files and return their bytes over anonymous pipes."""

    role: _TLSMaterialRole = "ca"
    try:
        request_raw = sys.stdin.buffer.read(_MAXIMUM_TLS_MATERIAL_REQUEST_BYTES + 1)
        if not request_raw or len(request_raw) > _MAXIMUM_TLS_MATERIAL_REQUEST_BYTES:
            raise ValueError
        request = json.loads(request_raw, object_pairs_hook=_json_object)
        if not isinstance(request, dict) or set(request) != {"paths"}:
            raise ValueError
        raw_paths = request["paths"]
        if (
            not isinstance(raw_paths, dict)
            or not raw_paths
            or not set(raw_paths).issubset({"ca", "client_certificate", "client_key"})
        ):
            raise ValueError

        encoded_material: dict[str, str] = {}
        for raw_role, path in raw_paths.items():
            if raw_role not in {"ca", "client_certificate", "client_key"}:
                raise ValueError
            role = cast("_TLSMaterialRole", raw_role)
            if not isinstance(path, str) or not path:
                raise _TLSMaterialReadFailure(role)
            encoded_material[role] = base64.b64encode(
                _read_tls_material_path(path, role=role)
            ).decode("ascii")
        response: dict[str, object] = {
            "status": "ok",
            "materials": encoded_material,
        }
    except _TLSMaterialReadFailure as error:
        response = {"status": "error", "role": error.role}
    except BaseException:
        response = {"status": "error", "role": role}

    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAXIMUM_TLS_MATERIAL_RESPONSE_BYTES:
        encoded = b'{"status":"error","role":"ca"}'
    with suppress(BrokenPipeError, OSError):
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def _read_tls_material_path(path: str, *, role: _TLSMaterialRole) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAXIMUM_TLS_MATERIAL_BYTES
        ):
            raise _TLSMaterialReadFailure(role)
        chunks: list[bytes] = []
        remaining = _MAXIMUM_TLS_MATERIAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if not value or len(value) > _MAXIMUM_TLS_MATERIAL_BYTES:
            raise _TLSMaterialReadFailure(role)
        return value
    except _TLSMaterialReadFailure:
        raise
    except (OSError, ValueError):
        raise _TLSMaterialReadFailure(role) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _decode_tls_material_response(
    raw: bytes,
    *,
    expected_roles: frozenset[_TLSMaterialRole],
) -> dict[_TLSMaterialRole, bytes]:
    first_role = _preferred_tls_material_role(expected_roles)
    if not raw or len(raw) > _MAXIMUM_TLS_MATERIAL_RESPONSE_BYTES:
        raise _TLSMaterialReadFailure(first_role)
    try:
        response = json.loads(raw, object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _TLSMaterialReadFailure(first_role) from None
    if not isinstance(response, dict):
        raise _TLSMaterialReadFailure(first_role)
    if set(response) == {"status", "role"} and response.get("status") == "error":
        role = response.get("role")
        if role not in expected_roles:
            raise _TLSMaterialReadFailure(first_role)
        raise _TLSMaterialReadFailure(cast("_TLSMaterialRole", role))
    if set(response) != {"status", "materials"} or response.get("status") != "ok":
        raise _TLSMaterialReadFailure(first_role)
    materials = response.get("materials")
    if not isinstance(materials, dict) or set(materials) != set(expected_roles):
        raise _TLSMaterialReadFailure(first_role)

    decoded: dict[_TLSMaterialRole, bytes] = {}
    for raw_role, value in materials.items():
        if raw_role not in expected_roles or not isinstance(value, str):
            raise _TLSMaterialReadFailure(first_role)
        role = cast("_TLSMaterialRole", raw_role)
        try:
            material = base64.b64decode(value, validate=True)
        except (ValueError, UnicodeEncodeError):
            raise _TLSMaterialReadFailure(role) from None
        if not material or len(material) > _MAXIMUM_TLS_MATERIAL_BYTES:
            raise _TLSMaterialReadFailure(role)
        decoded[role] = material
    return decoded


def _preferred_tls_material_role(
    roles: Collection[_TLSMaterialRole],
) -> _TLSMaterialRole:
    for role in _TLS_MATERIAL_ROLE_PRECEDENCE:
        if role in roles:
            return role
    raise ValueError("at least one TLS material role is required")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _raise_tls_material_error(role: _TLSMaterialRole) -> NoReturn:
    if role == "ca":
        raise ConfigurationError(
            "unable to configure TLS verification material",
            operation="configure_tls",
        ) from None
    raise AuthenticationError(
        "unable to load mutual-TLS credential material",
        operation="mqtt.authenticate",
    ) from None


def _validate_client_certificate_window(
    certificate: Path,
    *,
    wall_time: Callable[[], float],
) -> None:
    """Fail closed when client certificate validity cannot be established."""

    try:
        decoded = _decode_client_certificate(certificate)
        not_before = decoded.get("notBefore")
        not_after = decoded.get("notAfter")
        if not isinstance(not_before, str) or not isinstance(not_after, str):
            raise ValueError("certificate validity window is unavailable")
        valid_from = ssl.cert_time_to_seconds(not_before)
        valid_until = ssl.cert_time_to_seconds(not_after)
        observed_at = wall_time()
        if not valid_from <= observed_at <= valid_until:
            raise ValueError("certificate is outside its validity window")
    except (OSError, ssl.SSLError, ValueError):
        raise AuthenticationError(
            "mutual-TLS client certificate is unavailable or outside its validity window",
            operation="mqtt.authenticate",
        ) from None


def _decode_client_certificate(certificate: Path) -> Mapping[str, object]:
    # CPython's standard-library certificate decoder is the only bundled API
    # that exposes X.509 validity fields without adding a runtime dependency.
    # It parses the first certificate in a PEM chain and performs no network I/O.
    ssl_module = getattr(ssl, "_ssl", None)
    decoder = getattr(ssl_module, "_test_decode_cert", None)
    if not callable(decoder):
        raise ValueError("certificate decoder is unavailable")
    result = decoder(str(certificate))
    if not isinstance(result, Mapping):
        raise ValueError("certificate decoder returned an invalid result")
    return result


def _build_server_auth_context(
    tls: TLSConfig,
    temporary_files: TemporaryCertificateFiles,
    *,
    prepared_material: _PreparedTLSMaterial | None = None,
) -> ssl.SSLContext:
    if tls.verify:
        ca_file: Path | None = None
        if tls.ca_certificate is not None:
            if prepared_material is None:
                ca_file = temporary_files.certificate_path(
                    tls.ca_certificate,
                    name="ca-certificate.pem",
                )
            else:
                if prepared_material.ca is None:
                    raise ValueError("prepared CA material is unavailable")
                ca_file = temporary_files._write_bytes(
                    "ca-certificate.pem",
                    prepared_material.ca,
                )
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=str(ca_file) if ca_file is not None else None,
        )
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


__all__ = [
    "TemporaryCertificateFiles",
    "build_client_ssl_context",
    "build_lifecycle_owned_client_ssl_context",
]
