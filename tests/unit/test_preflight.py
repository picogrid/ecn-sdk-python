# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import traceback
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from picogrid_ecn_client import (
    AuthenticationError,
    AuthorizationError,
    BearerTokenAuth,
    CertificateMaterial,
    CheckStatus,
    ClientState,
    ConnectionError,
    ECNClient,
    ECNConfig,
    EntityCategory,
    NoAuth,
    PreflightCheckName,
    ReviewedContainerNetwork,
    SubscriptionProbe,
    SubscriptionProbeKind,
    TLSConfig,
    WireFormat,
)
from picogrid_ecn_client import (
    TimeoutError as ECNTimeoutError,
)
from picogrid_ecn_client._legion_auth import legion_system_auth_provider
from picogrid_ecn_client._preflight import PreflightRunner


def config_for_port(port: int) -> ECNConfig:
    return ECNConfig(
        host="127.0.0.1",
        mqtt_port=port,
        integration_name="synthetic-client",
        auth=BearerTokenAuth(token=SecretStr("synthetic-token")),
        tls=TLSConfig(enabled=False, verify=False),
        allow_insecure=True,
    )


def _ipv4(*octets: int) -> str:
    """Render a dotted-quad address without writing one into this file."""

    return str(ipaddress.IPv4Address(bytes(octets)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved_addresses",
    [
        (_ipv4(8, 8, 8, 8),),
        (_ipv4(10, 0, 0, 2), _ipv4(8, 8, 8, 8)),
    ],
    ids=["public", "mixed-private-public"],
)
async def test_attested_preflight_refuses_public_resolution_without_dialing(
    monkeypatch: pytest.MonkeyPatch,
    resolved_addresses: Sequence[str],
) -> None:
    async def getaddrinfo(
        host: str,
        port: int,
        *,
        family: int = 0,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert (host, port, type) == ("mqtt-container.example", 1883, socket.SOCK_STREAM)
        assert family in (0, socket.AF_UNSPEC)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
            for address in resolved_addresses
        ]

    dial_attempts: list[tuple[object, ...]] = []

    async def open_connection(*args: object, **kwargs: object) -> None:
        dial_attempts.append(args + tuple(kwargs.items()))
        raise AssertionError("attested preflight must not dial a refused resolution")

    async def mqtt_probe() -> None:
        return None

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    config = ECNConfig(
        host="mqtt-container.example",
        mqtt_port=1883,
        integration_name="synthetic-client",
        auth=NoAuth(),
        tls=TLSConfig(enabled=False),
        plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
    )
    runner = PreflightRunner(
        config,
        mqtt_probe=mqtt_probe,
        ssl_context_factory=ssl.create_default_context,
    )

    report = await runner.run()

    boundary_checks = [
        check
        for check in report.checks
        if check.name in (PreflightCheckName.DNS, PreflightCheckName.TCP)
    ]
    assert [(check.status, check.detail) for check in boundary_checks] == [
        (CheckStatus.FAIL, "public client failure: transport_boundary_error"),
        (CheckStatus.FAIL, "public client failure: transport_boundary_error"),
    ]
    assert not dial_attempts


@pytest.mark.asyncio
async def test_attested_preflight_tries_later_validated_address_like_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = (_ipv4(10, 0, 0, 3), _ipv4(10, 0, 0, 4))
    resolver_calls = 0
    dial_attempts: list[str] = []

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        assert (host, port) == ("mqtt-container.example", 1883)
        return addresses

    class _Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def open_connection(host: str, port: int) -> tuple[object, _Writer]:
        dial_attempts.append(host)
        assert port == 1883
        if host == addresses[0]:
            raise ConnectionRefusedError("synthetic refused endpoint")
        return object(), _Writer()

    async def mqtt_probe() -> None:
        return None

    monkeypatch.setattr("picogrid_ecn_client._preflight.resolve_private_endpoint", resolve)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    config = ECNConfig(
        host="mqtt-container.example",
        mqtt_port=1883,
        integration_name="synthetic-client",
        auth=NoAuth(),
        tls=TLSConfig(enabled=False),
        plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
    )

    report = await PreflightRunner(
        config,
        mqtt_probe=mqtt_probe,
        ssl_context_factory=ssl.create_default_context,
    ).run()

    tcp_check = next(check for check in report.checks if check.name is PreflightCheckName.TCP)
    assert tcp_check.status is CheckStatus.PASS
    assert dial_attempts == list(addresses)
    assert resolver_calls == 2


@pytest.mark.asyncio
async def test_preflight_is_mqtt_only_and_publish_authorization_is_unknown() -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    mqtt_calls = 0
    subscription_calls = 0

    async def mqtt_probe() -> None:
        nonlocal mqtt_calls
        mqtt_calls += 1

    async def subscription_probe() -> None:
        nonlocal subscription_calls
        subscription_calls += 1

    try:
        report = await PreflightRunner(
            config_for_port(port),
            mqtt_probe=mqtt_probe,
            subscription_probes=(subscription_probe,),
            ssl_context_factory=ssl.create_default_context,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    assert report.successful and report.ready
    assert mqtt_calls == 1
    assert subscription_calls == 1
    publish = next(
        check for check in report.checks if check.name is PreflightCheckName.PUBLISH_AUTHORIZATION
    )
    assert publish.status is CheckStatus.UNKNOWN
    assert not publish.required
    assert not hasattr(report, "authorized_scopes")
    assert not hasattr(report, "protocol_version")


@pytest.mark.asyncio
async def test_preflight_reports_subscription_rejection_without_exposing_token() -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def probe() -> None:
        return None

    async def rejected_subscription() -> None:
        raise AuthorizationError(
            "password=synthetic-token",
            operation="mqtt.subscribe",
            secrets=("synthetic-token",),
        )

    try:
        report = await PreflightRunner(
            config_for_port(port),
            mqtt_probe=probe,
            subscription_probes=(rejected_subscription,),
            ssl_context_factory=ssl.create_default_context,
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    subscription = next(
        check for check in report.checks if check.name is PreflightCheckName.SUBSCRIPTION
    )
    assert subscription.status is CheckStatus.FAIL
    assert not report.successful
    assert "synthetic-token" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_preflight_skips_subscription_probe_unless_explicitly_requested() -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def probe() -> None:
        return None

    try:
        report = await PreflightRunner(
            config_for_port(port),
            mqtt_probe=probe,
            ssl_context_factory=ssl.create_default_context,
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    subscription = next(
        check for check in report.checks if check.name is PreflightCheckName.SUBSCRIPTION
    )
    assert subscription.status is CheckStatus.SKIPPED
    assert not subscription.required


@pytest.mark.asyncio
async def test_preflight_preserves_safe_legion_setup_guidance(tmp_path: Path) -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def probe() -> None:
        return None

    config = config_for_port(port).model_copy(
        update={
            "auth": BearerTokenAuth(
                credentials_provider=legion_system_auth_provider(tmp_path / "missing")
            )
        }
    )
    try:
        report = await PreflightRunner(
            config,
            mqtt_probe=probe,
            ssl_context_factory=ssl.create_default_context,
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    authentication = next(
        check for check in report.checks if check.name is PreflightCheckName.AUTHENTICATION
    )
    assert authentication.status is CheckStatus.FAIL
    assert "legion-auth setup" in authentication.detail
    assert str(tmp_path) not in report.model_dump_json()


@pytest.mark.asyncio
async def test_preflight_does_not_trust_spoofed_legion_error_codes() -> None:
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    canary = "caller-provider-secret-canary"

    async def provider() -> tuple[str, str]:
        raise AuthenticationError(canary, code="legion_credentials_missing")

    async def probe() -> None:
        return None

    config = config_for_port(port).model_copy(
        update={"auth": BearerTokenAuth(credentials_provider=provider)}
    )
    try:
        report = await PreflightRunner(
            config,
            mqtt_probe=probe,
            ssl_context_factory=ssl.create_default_context,
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    authentication = next(
        check for check in report.checks if check.name is PreflightCheckName.AUTHENTICATION
    )
    assert authentication.status is CheckStatus.FAIL
    assert canary not in report.model_dump_json()
    assert "legion-auth setup" not in authentication.detail


def test_subscription_probe_builds_only_narrow_retained_filters() -> None:
    entity_probe = SubscriptionProbe(
        kind=SubscriptionProbeKind.ENTITY,
        integration="synthetic-client",
        category=EntityCategory.TRACK,
    )
    location_probe = SubscriptionProbe(
        kind=SubscriptionProbeKind.LOCATION,
        integration="synthetic-client",
        entity_id="00000000-0000-4000-8000-000000000001",
        wire_format=WireFormat.PROTOBUF,
    )
    assert ECNClient._subscription_probe_topic(entity_probe) == "entity/synthetic-client/+/track"
    assert ECNClient._subscription_probe_topic(location_probe) == (
        "entity_location_pb/synthetic-client/00000000-0000-4000-8000-000000000001"
    )


def test_subscription_probe_rejects_unbounded_or_unrepresentable_shapes() -> None:
    with pytest.raises(ValueError, match="require category"):
        SubscriptionProbe(
            kind=SubscriptionProbeKind.ENTITY,
            integration="synthetic-client",
        )
    with pytest.raises(ValueError, match="do not contain an entity ID"):
        SubscriptionProbe(
            kind=SubscriptionProbeKind.ENTITY,
            integration="synthetic-client",
            category=EntityCategory.TRACK,
            entity_id="00000000-0000-4000-8000-000000000001",
            wire_format=WireFormat.PROTOBUF,
        )
    with pytest.raises(ValueError, match="publishable category"):
        SubscriptionProbe(
            kind=SubscriptionProbeKind.ENTITY,
            integration="synthetic-client",
            category=EntityCategory.OTHER,
        )


@pytest.mark.asyncio
async def test_client_close_is_bounded_when_cleanup_suppresses_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(config_for_port(1).model_copy(update={"shutdown_timeout": 0.01}))
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cancellation_resistant_cleanup() -> None:
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            await release_cleanup.wait()
        finally:
            cleanup_finished.set()

    monkeypatch.setattr(client, "_close_components", cancellation_resistant_cleanup)

    with pytest.raises(ECNTimeoutError, match="shutdown exceeded"):
        await asyncio.wait_for(client.close(), timeout=0.5)
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)
    assert client.status.state is ClientState.CLOSED

    release_cleanup.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_client_close_does_not_wait_forever_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(config_for_port(1).model_copy(update={"shutdown_timeout": 0.5}))
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cancellation_resistant_cleanup() -> None:
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            await release_cleanup.wait()
        finally:
            cleanup_finished.set()

    monkeypatch.setattr(client, "_close_components", cancellation_resistant_cleanup)
    closing = asyncio.create_task(client.close())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=0.5)
    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=1)
    assert client.status.state is ClientState.CLOSED

    release_cleanup.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_client_close_does_not_retain_raw_cleanup_error_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(config_for_port(1))
    canary = "/tmp/synthetic-credential-path/client-key.pem"

    async def fail_with_sensitive_path() -> None:
        raise RuntimeError(canary)

    monkeypatch.setattr(client._mqtt_transport, "close", fail_with_sensitive_path)

    with pytest.raises(ConnectionError) as caught:
        await client.close()

    rendered = "".join(traceback.format_exception(caught.value))
    assert canary not in rendered
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_client_close_finishes_cleanup_after_connection_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ECNClient(config_for_port(1))
    failing_stream = client.connection_events()
    healthy_stream = client.connection_events()
    canary = "/tmp/synthetic-credential-path/stream-close.pem"
    closed: set[str] = set()

    async def fail_stream_release() -> None:
        raise RuntimeError(canary)

    failing_stream._on_close = fail_stream_release

    def record_component_close(
        name: str,
        close_component: Callable[[], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        async def record_close() -> None:
            closed.add(name)
            await close_component()

        return record_close

    components = {
        "tasks": client._task_service,
        "entities": client._entity_location_service,
        "clock": client._clock_service,
        "transport": client._mqtt_transport,
    }
    assert all(component is not None for component in components.values())
    for name, component in components.items():
        assert component is not None
        monkeypatch.setattr(component, "close", record_component_close(name, component.close))

    with pytest.raises(ConnectionError) as caught:
        await client.close()

    rendered = "".join(traceback.format_exception(caught.value))
    assert canary not in rendered
    assert caught.value.__cause__ is None
    assert failing_stream.closed
    assert healthy_stream.closed
    assert client._connection_streams == set()
    assert client._task_service is None
    assert client._entity_location_service is None
    assert client._clock_service is None
    assert closed == {"tasks", "entities", "clock", "transport"}
    assert client.status.state is ClientState.CLOSED


@pytest.mark.asyncio
async def test_preflight_rejects_oversized_tls_material_before_openssl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_path = tmp_path / "oversized-ca.pem"
    ca_path.write_bytes(b"x" * (1024 * 1024 + 1))
    client = ECNClient(
        ECNConfig(
            host="127.0.0.1",
            mqtt_port=8883,
            integration_name="synthetic-client",
            auth=BearerTokenAuth(token=SecretStr("synthetic-token")),
            tls=TLSConfig(ca_certificate=CertificateMaterial(path=ca_path)),
            connection_timeout=0.2,
        )
    )

    async def pass_check(_runner: PreflightRunner) -> None:
        return None

    async def pass_mqtt_probe(_transport: object) -> None:
        return None

    openssl_calls = 0

    def reject_openssl_parse(*_args: object, **_kwargs: object) -> ssl.SSLContext:
        nonlocal openssl_calls
        openssl_calls += 1
        raise AssertionError("oversized TLS material reached OpenSSL")

    monkeypatch.setattr(PreflightRunner, "_check_dns", pass_check)
    monkeypatch.setattr(PreflightRunner, "_check_tcp", pass_check)
    monkeypatch.setattr("picogrid_ecn_client.client.MQTTTransport.start", pass_mqtt_probe)
    monkeypatch.setattr(ssl, "create_default_context", reject_openssl_parse)

    report = await client.preflight()

    tls_check = next(check for check in report.checks if check.name is PreflightCheckName.TLS)
    assert tls_check.status is CheckStatus.FAIL
    assert openssl_calls == 0
