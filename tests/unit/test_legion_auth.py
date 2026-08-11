# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import picogrid_ecn_client._legion_auth as legion_auth
from picogrid_ecn_client import BearerTokenAuth, ECNClient, ECNConfig, TLSConfig
from picogrid_ecn_client._legion_auth import legion_system_auth_provider
from picogrid_ecn_client.auth import CredentialsProvider
from picogrid_ecn_client.exceptions import AuthenticationError, ConfigurationError

_BLOCKED_OPEN_CHILD_CODE = (
    "import json,os,socket,sys;"
    "request=json.loads(sys.stdin.buffer.read());"
    "storage=request['storage_path'];"
    "signal=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);"
    "signal.sendto(b'1',os.path.join(storage,'ready.sock'));"
    "signal.close();"
    "os.open(os.path.join(storage,'blocked'),os.O_RDONLY)"
)
_STALLED_CHILD_CODE = "import time;time.sleep(60)"


def _jwt(expires_at: datetime, *, algorithm: str = "RS256", marker: str = "one") -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    signature = base64.urlsafe_b64encode(b"synthetic-signature").decode().rstrip("=")
    return f"{encode({'alg': algorithm})}.{encode({'exp': expires_at.timestamp(), 'marker': marker})}.{signature}"


def _write_credentials(
    directory: Path,
    *,
    integration_id: str = "a125dab0-fbc8-4e4b-a251-803792548e10",
    token: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    directory.mkdir(mode=0o755, exist_ok=True)
    expiry = expires_at or datetime.now(UTC) + timedelta(hours=1)
    access_token = token or _jwt(expiry)
    configuration = {
        "integrationId": integration_id,
        "clientId": "ignored-client-id",
        "clientSecret": "ignored-client-secret-canary",
    }
    token_document = {
        "access_token": access_token,
        "refresh_token": "ignored-refresh-token-canary",
        "expires_at": expiry.isoformat(),
    }
    for name, document in (
        ("oauth_config.json", configuration),
        ("access_token.json", token_document),
    ):
        path = directory / name
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o640)
    return access_token


@pytest.mark.asyncio
async def test_provider_uses_only_integration_id_and_current_access_token(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "auth"
    token = _write_credentials(directory)
    provider = legion_system_auth_provider(directory)

    assert await provider() == ("a125dab0-fbc8-4e4b-a251-803792548e10", token)
    assert repr(provider) == "LegionSystemAuthProvider()"


@pytest.mark.asyncio
async def test_provider_reopens_current_files_on_every_call(tmp_path: Path) -> None:
    directory = tmp_path / "auth"
    first = _write_credentials(directory, integration_id="first-integration")
    provider = legion_system_auth_provider(directory)
    assert await provider() == ("first-integration", first)

    for path in directory.iterdir():
        path.unlink()
    second = _write_credentials(directory, integration_id="second-integration", token=None)
    assert await provider() == ("second-integration", second)


def _record_credential_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[asyncio.subprocess.Process]:
    created: list[asyncio.subprocess.Process] = []
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def recording_create_subprocess_exec(
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.subprocess.Process:
        process = await create_subprocess_exec(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_create_subprocess_exec)
    return created


def _assert_process_reaped(process: asyncio.subprocess.Process) -> None:
    assert process.returncode is not None
    assert process.stdin is not None and process.stdin.is_closing()
    assert process.stdout is not None and process.stdout.at_eof()
    if hasattr(os, "waitpid"):
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_provider_exchange_timeout_kills_and_reaps_child_secret_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legion_auth, "_CREDENTIAL_READER_CODE", _STALLED_CHILD_CODE)
    monkeypatch.setattr(legion_auth, "_CREDENTIAL_EXCHANGE_TIMEOUT_SECONDS", 0.05)
    processes = _record_credential_processes(monkeypatch)
    storage = tmp_path / "credential-path-canary"

    async with asyncio.timeout(1):
        with pytest.raises(AuthenticationError) as raised:
            await legion_system_auth_provider(storage)()

    assert raised.value.code == "legion_credentials_unsafe"
    assert str(storage) not in str(raised.value)
    assert len(processes) == 1
    _assert_process_reaped(processes[0])


@pytest.mark.asyncio
async def test_client_close_keeps_process_creation_cleanup_in_startup_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_created = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def delayed_create_subprocess_exec(
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.subprocess.Process:
        process = await create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        child_created.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable process-creation release")
        except asyncio.CancelledError:
            assert process.stdin is not None
            process.stdin.close()
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create_subprocess_exec)
    storage = tmp_path / "credential-path-canary"
    client = ECNClient(
        ECNConfig(
            host="127.0.0.1",
            mqtt_port=1883,
            integration_name="credential-spawn-close-test",
            auth=BearerTokenAuth(credentials_provider=legion_system_auth_provider(storage)),
            tls=TLSConfig(enabled=False, verify=False),
            allow_insecure=True,
            connection_timeout=2,
            shutdown_timeout=2,
        )
    )
    starting = asyncio.create_task(client.start())
    async with asyncio.timeout(1):
        await child_created.wait()

    async with asyncio.timeout(5):
        await client.close()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert len(processes) == 1
    _assert_process_reaped(processes[0])


@pytest.mark.asyncio
async def test_cleanup_deadline_bounds_stubborn_open_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legion_auth, "_CREDENTIAL_CLEANUP_TIMEOUT_SECONDS", 0.05)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _STALLED_CHILD_CODE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        close_fds=True,
        env=legion_auth._credential_reader_environment(),
    )
    assert process.stdin is not None
    process.stdin.close()
    assert process.returncode is None

    release_exchange = asyncio.Event()

    async def inherited_pipe_exchange() -> tuple[bytes | None, bytes | None]:
        while not release_exchange.is_set():
            try:
                await release_exchange.wait()
            except asyncio.CancelledError:
                continue
        return b"", None

    exchange = asyncio.create_task(
        inherited_pipe_exchange(),
        name=legion_auth._CREDENTIAL_IO_TASK_NAME,
    )
    cleanup = asyncio.create_task(legion_auth._terminate_and_reap(process, exchange))
    await asyncio.sleep(0)
    for _ in range(5):
        cleanup.cancel()
        await asyncio.sleep(0)

    async with asyncio.timeout(1):
        await cleanup

    assert cleanup.cancelling() == 0
    assert not exchange.done()
    _assert_process_reaped(process)
    release_exchange.set()
    async with asyncio.timeout(1):
        assert await exchange == (b"", None)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires a POSIX FIFO")
@pytest.mark.asyncio
async def test_provider_cancellation_kills_and_reaps_blocked_os_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legion_auth, "_CREDENTIAL_READER_CODE", _BLOCKED_OPEN_CHILD_CODE)
    processes = _record_credential_processes(monkeypatch)

    with tempfile.TemporaryDirectory(prefix="ecn-credential-", dir="/tmp") as temporary:
        root = Path(temporary)
        for iteration in range(5):
            storage = root / f"b{iteration}"
            storage.mkdir()
            os.mkfifo(storage / "blocked", mode=0o600)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
                ready.bind(str(storage / "ready.sock"))
                ready.setblocking(False)
                resolving = asyncio.create_task(legion_system_auth_provider(storage)())
                async with asyncio.timeout(2):
                    assert await asyncio.get_running_loop().sock_recv(ready, 1) == b"1"
                process = processes[-1]

                resolving.cancel("original cancellation")
                await asyncio.sleep(0)
                resolving.cancel("later cancellation")
                async with asyncio.timeout(1):
                    with pytest.raises(asyncio.CancelledError) as raised:
                        await resolving

                assert raised.value.args == ("original cancellation",)
                assert resolving.cancelled()
                assert resolving.cancelling() == 1
                _assert_process_reaped(process)

    await asyncio.sleep(0)
    credential_tasks = {
        legion_auth._CREDENTIAL_IO_TASK_NAME,
        legion_auth._CREDENTIAL_WAIT_TASK_NAME,
    }
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name() in credential_tasks
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires a POSIX FIFO")
@pytest.mark.asyncio
async def test_client_close_reaps_credential_reader_blocked_in_os_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legion_auth, "_CREDENTIAL_READER_CODE", _BLOCKED_OPEN_CHILD_CODE)
    processes = _record_credential_processes(monkeypatch)
    with tempfile.TemporaryDirectory(prefix="ecn-credential-", dir="/tmp") as temporary:
        storage = Path(temporary) / "b"
        storage.mkdir()
        os.mkfifo(storage / "blocked", mode=0o600)
        provider = legion_system_auth_provider(storage)
        client = ECNClient(
            ECNConfig(
                host="127.0.0.1",
                mqtt_port=1883,
                integration_name="credential-close-test",
                auth=BearerTokenAuth(credentials_provider=provider),
                tls=TLSConfig(enabled=False, verify=False),
                allow_insecure=True,
                connection_timeout=2,
                shutdown_timeout=0.5,
            )
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
            ready.bind(str(storage / "ready.sock"))
            ready.setblocking(False)
            starting = asyncio.create_task(client.start())
            async with asyncio.timeout(2):
                assert await asyncio.get_running_loop().sock_recv(ready, 1) == b"1"

            async with asyncio.timeout(5):
                await client.close()
            with pytest.raises(asyncio.CancelledError):
                await starting

        assert len(processes) == 1
        _assert_process_reaped(processes[0])


@pytest.mark.asyncio
async def test_provider_autodetects_pinned_storage_environment(tmp_path: Path) -> None:
    directory = tmp_path / "auth"
    token = _write_credentials(directory)
    provider = legion_system_auth_provider(environ={"LEGION_AUTH_STORAGE_PATH": str(directory)})
    assert await provider() == ("a125dab0-fbc8-4e4b-a251-803792548e10", token)


@pytest.mark.asyncio
async def test_provider_does_not_open_refresh_token_file(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "auth"
    _write_credentials(directory)
    refresh_path = directory / "refresh_token.json"
    os.mkfifo(refresh_path, mode=0o000)

    # Opening the FIFO would block, so accidental access must fail promptly
    # instead of hanging.
    async with asyncio.timeout(2):
        await legion_system_auth_provider(directory)()


@pytest.mark.asyncio
async def test_provider_fails_closed_with_exact_setup_command_when_absent(
    tmp_path: Path,
) -> None:
    provider = legion_system_auth_provider(tmp_path / "missing")
    with pytest.raises(AuthenticationError, match=r"legion-auth setup") as raised:
        await provider()
    assert raised.value.code == "legion_credentials_missing"
    assert '--storage-path "$ECN_LEGION_AUTH_STORAGE"' in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.asyncio
async def test_default_storage_failure_uses_default_setup_command(
    tmp_path: Path,
) -> None:
    provider = legion_system_auth_provider(environ={})
    provider._storage_path = tmp_path / "missing-default"
    with pytest.raises(AuthenticationError) as raised:
        await provider()

    assert str(raised.value).endswith("run `legion-auth setup`")
    assert "--storage-path" not in str(raised.value)


@pytest.mark.parametrize(
    ("configuration", "token_document"),
    [
        ({}, {"access_token": "ignored", "expires_at": "ignored"}),
        ({"integrationId": "integration"}, {}),
        (
            {"integrationId": "integration"},
            {"access_token": "not-a-jwt", "expires_at": "2030-01-01T00:00:00Z"},
        ),
        (
            {"integrationId": "integration"},
            {"access_token": "a.e30.signature", "expires_at": "not-a-time"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_rejects_malformed_material_without_exposing_values(
    tmp_path: Path,
    configuration: dict[str, object],
    token_document: dict[str, object],
) -> None:
    directory = tmp_path / "auth"
    directory.mkdir(mode=0o755)
    for name, document in (
        ("oauth_config.json", configuration),
        ("access_token.json", token_document),
    ):
        path = directory / name
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o640)

    with pytest.raises(AuthenticationError, match="malformed") as raised:
        await legion_system_auth_provider(directory)()
    assert raised.value.code == "legion_credentials_malformed"
    rendered = str(raised.value)
    assert str(tmp_path) not in rendered
    assert "not-a-jwt" not in rendered


@pytest.mark.asyncio
async def test_provider_rejects_stored_or_jwt_expiry(tmp_path: Path) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(minutes=1)

    stored_expired = tmp_path / "stored-expired"
    _write_credentials(stored_expired, token=_jwt(future), expires_at=past)
    with pytest.raises(AuthenticationError, match="expired") as stored_error:
        await legion_system_auth_provider(stored_expired)()
    assert stored_error.value.code == "legion_credentials_expired"

    jwt_expired = tmp_path / "jwt-expired"
    _write_credentials(jwt_expired, token=_jwt(past), expires_at=future)
    with pytest.raises(AuthenticationError, match="expired"):
        await legion_system_auth_provider(jwt_expired)()


@pytest.mark.asyncio
async def test_provider_rejects_unsigned_jwt_algorithm(tmp_path: Path) -> None:
    expiry = datetime.now(UTC) + timedelta(hours=1)
    directory = tmp_path / "unsigned"
    _write_credentials(directory, token=_jwt(expiry, algorithm="none"), expires_at=expiry)
    with pytest.raises(AuthenticationError, match="malformed"):
        await legion_system_auth_provider(directory)()


@pytest.mark.asyncio
async def test_provider_rejects_symlinks_and_unsafe_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "auth"
    _write_credentials(directory)
    token_path = directory / "access_token.json"
    target = tmp_path / "token-target.json"
    token_path.replace(target)
    token_path.symlink_to(target)

    with pytest.raises(AuthenticationError, match="safety checks") as symlink_error:
        await legion_system_auth_provider(directory)()
    assert symlink_error.value.code == "legion_credentials_unsafe"

    token_path.unlink()
    target.replace(token_path)
    token_path.chmod(0o666)
    with pytest.raises(AuthenticationError, match="safety checks"):
        await legion_system_auth_provider(directory)()

    token_path.unlink()
    os.mkfifo(token_path, mode=0o600)
    # Leave room for an isolated interpreter to start on a loaded CI host while
    # remaining well below the credential exchange deadline; a blocking FIFO
    # open would still fail this bound.
    async with asyncio.timeout(2):
        with pytest.raises(AuthenticationError, match="safety checks"):
            await legion_system_auth_provider(directory)()


@pytest.mark.asyncio
async def test_provider_rejects_symlinked_or_writable_storage_directory(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real-auth"
    _write_credentials(real_directory)
    linked_directory = tmp_path / "linked-auth"
    linked_directory.symlink_to(real_directory)

    with pytest.raises(AuthenticationError, match="safety checks"):
        await legion_system_auth_provider(linked_directory)()

    real_directory.chmod(0o777)
    with pytest.raises(AuthenticationError, match="safety checks"):
        await legion_system_auth_provider(real_directory)()


def test_provider_requires_absolute_storage_path() -> None:
    with pytest.raises(ConfigurationError, match="absolute safe path"):
        legion_system_auth_provider("relative/auth")


def test_provider_redacts_unexpandable_storage_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "~missing-user-secret-canary/auth"
    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        if str(path) == canary:
            raise RuntimeError("unknown home for missing-user-secret-canary")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)

    with pytest.raises(ConfigurationError, match="absolute safe path") as raised:
        legion_system_auth_provider(canary)

    assert canary not in str(raised.value)
    assert "missing-user-secret-canary" not in str(raised.value)


@pytest.mark.asyncio
async def test_provider_never_returns_refresh_token_or_client_secret(tmp_path: Path) -> None:
    directory = tmp_path / "auth"
    access_token = _write_credentials(directory)
    credentials = await legion_system_auth_provider(directory)()
    assert credentials == ("a125dab0-fbc8-4e4b-a251-803792548e10", access_token)
    assert "ignored-refresh-token-canary" not in credentials
    assert "ignored-client-secret-canary" not in credentials


@pytest.mark.asyncio
async def test_bearer_credentials_provider_validates_dynamic_values() -> None:
    async def valid_provider() -> tuple[str, str]:
        return " integration-\U0001f6f0 ", "jwt-value"

    valid = BearerTokenAuth(credentials_provider=valid_provider)
    assert await valid._resolve_credentials("unused") == (
        "integration-\U0001f6f0",
        "jwt-value",
    )

    invalid_values: tuple[object, ...] = (
        ("integration-id", ""),
        ("integration-id", "line\nbreak"),
        ("integration-id", "x" * 65_536),
        ("integration-id", "\u00e9" * 32_768),
        ("integration-id", "token\ud800"),
        ("integration\x00id", "jwt-value"),
        ("integration\ud800id", "jwt-value"),
        ("integration\ufdd0id", "jwt-value"),
        ("integration\ufffeid", "jwt-value"),
        ("integration\U0001fffeid", "jwt-value"),
        ("integration\U0001ffffid", "jwt-value"),
        ("x" * 257, "jwt-value"),
        ("integration-id",),
    )
    for value in invalid_values:

        async def invalid_provider(value: object = value) -> object:
            return value

        provider = cast("CredentialsProvider", invalid_provider)
        auth = BearerTokenAuth(credentials_provider=provider)
        with pytest.raises(ValueError, match="credentials_provider returned"):
            await auth._resolve_credentials("unused")
