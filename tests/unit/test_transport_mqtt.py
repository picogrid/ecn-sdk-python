# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import threading
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, Self, cast
from uuid import UUID

import aiomqtt
import pytest
from aiomqtt.exceptions import MqttConnectError
from paho.mqtt import client as paho
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from pydantic import SecretStr

from picogrid_ecn_client._legion_auth import legion_system_auth_provider
from picogrid_ecn_client._protocol import (
    ENTITY_JSON_SUBSCRIPTION,
    build_entity_topic,
)
from picogrid_ecn_client._transport import MQTTTransport, SubscriptionHandle
from picogrid_ecn_client._transport import credentials as credential_module
from picogrid_ecn_client._transport.mqtt import (
    _decode_resolver_response,
    _DNSResolutionFailure,
    _ensure_publication_accepted,
    _ensure_subscription_accepted,
    _ensure_unsubscription_accepted,
    _is_authentication_rejection,
    _MQTTV5Client,
    _open_prepared_socket,
    _PublishCompletion,
    _resolve_tcp_addresses,
    _ResolvedTCPAddress,
    _resolver_process_main,
    _TCPConnectionFailure,
    _TLSPeerVerificationFailure,
    _TLSTransportFailure,
)
from picogrid_ecn_client.auth import (
    BearerTokenAuth,
    CertificateMaterial,
    MTLSAuth,
    NoAuth,
    PrivateKeyMaterial,
    TLSConfig,
)
from picogrid_ecn_client.config import ECNConfig, ReconnectPolicy, ReviewedContainerNetwork
from picogrid_ecn_client.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConnectionError,
    DeliveryError,
    ECNClientError,
    OutcomeUnknownError,
    ProtocolError,
    ResourceLimitError,
    TransportBoundaryError,
    ValidationError,
)
from picogrid_ecn_client.exceptions import TimeoutError as ECNTimeoutError
from picogrid_ecn_client.models import EntityCategory
from picogrid_ecn_client.models.common import (
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    DeliveryPhase,
)
from picogrid_ecn_client.testing import MockECN

ENTITY_ID = UUID("12345678-1234-5678-9234-567812345678")


def _config() -> ECNConfig:
    return ECNConfig(
        host="localhost",
        integration_name="vendor-a",
        mqtt_port=1883,
        auth=BearerTokenAuth(token=SecretStr("synthetic-token")),
        tls=TLSConfig(enabled=False),
        allow_insecure=True,
        connection_timeout=1,
        operation_timeout=1,
        shutdown_timeout=1,
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.05,
            maximum_delay_seconds=0.05,
        ),
        maximum_payload_size=1024,
    )


def _no_auth_config() -> ECNConfig:
    return ECNConfig(
        host="mqtt-container.example",
        integration_name="vendor-a",
        mqtt_port=1883,
        auth=NoAuth(),
        tls=TLSConfig(enabled=False),
        plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        connection_timeout=1,
        operation_timeout=1,
        shutdown_timeout=1,
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.05,
            maximum_delay_seconds=0.05,
        ),
        maximum_payload_size=1024,
    )


def test_authentication_rejection_uses_only_mqtt_v5_reason_codes() -> None:
    assert _is_authentication_rejection(MqttConnectError(134))
    assert not _is_authentication_rejection(MqttConnectError(135))
    assert not _is_authentication_rejection(MqttConnectError(4))
    assert not _is_authentication_rejection(MqttConnectError(5))


@pytest.mark.parametrize(
    ("reason_code", "failure_code", "terminal"),
    [
        (0x86, ConnectionFailureCode.AUTHENTICATION_REJECTED, True),
        (0x87, ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED, True),
        (0x8A, ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED, True),
        (0x85, ConnectionFailureCode.CONFIGURATION_INVALID, True),
        (0x8C, ConnectionFailureCode.CONFIGURATION_INVALID, True),
        (0x80, ConnectionFailureCode.CONNECTION_REJECTED, True),
        (0x83, ConnectionFailureCode.CONNECTION_REJECTED, True),
        (0x88, ConnectionFailureCode.BROKER_UNAVAILABLE, False),
        (0x89, ConnectionFailureCode.SERVER_BUSY, False),
        (0x9F, ConnectionFailureCode.SERVER_BUSY, False),
        (0x97, ConnectionFailureCode.CONNECTION_RESOURCE_LIMIT, True),
        (0x81, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x82, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x84, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x90, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x95, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x99, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x9A, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x9B, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x9C, ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW, True),
        (0x9D, ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW, True),
        (0x00, ConnectionFailureCode.PROTOCOL_FAILURE, True),
        (0x98, ConnectionFailureCode.PROTOCOL_FAILURE, True),
    ],
)
def test_connack_reason_codes_have_exhaustive_retry_classification(
    reason_code: int,
    failure_code: ConnectionFailureCode,
    terminal: bool,
) -> None:
    transport = MQTTTransport(_config())

    classification = transport._classify_failure(
        MqttConnectError(reason_code),
        client=None,
        was_ready=False,
    )

    assert classification.code is failure_code
    assert classification.terminal is terminal


@pytest.mark.parametrize("reason_code", [None, object(), [0x80, 0x81]])
def test_missing_nonnumeric_or_wrong_cardinality_connack_is_protocol_terminal(
    reason_code: object,
) -> None:
    transport = MQTTTransport(_config())
    error = MqttConnectError(0x80)
    error.rc = reason_code  # type: ignore[assignment]

    classification = transport._classify_failure(
        error,
        client=None,
        was_ready=False,
    )

    assert classification.code is ConnectionFailureCode.PROTOCOL_FAILURE
    assert classification.terminal is True


@pytest.mark.asyncio
async def test_pinned_paho_connack_gap_preserves_raw_payload_format_invalid() -> None:
    with pytest.raises(ValueError):
        ReasonCode(PacketTypes.CONNACK, identifier=0x99)
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._client._in_packet = {  # type: ignore[attr-defined]
        "command": 0x20,
        "have_remaining": 1,
        "remaining_count": [3],
        "remaining_mult": 128,
        "remaining_length": 3,
        "packet": bytearray(b"\x00\x99\x00"),
        "to_process": 0,
        "pos": 0,
    }

    result = adapter._client._handle_connack()  # type: ignore[attr-defined]
    error = adapter._connected.exception()

    assert result == paho.MQTT_ERR_PROTOCOL
    assert isinstance(error, MqttConnectError)
    assert error.rc == 0x99
    assert "localhost" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining_length", "packet", "classifier"),
    [
        (1, b"\x00", "missing_reason_code"),
        (2, b"\x00\x00", "malformed_connack"),
        (4, b"\x00\x00\x00", "wrong_cardinality"),
        (3, b"\x02\x00\x00", "malformed_connack"),
        (3, b"\x01\x00\x00", "session_present"),
        (3, b"\x00\x98\x00", "unmapped_failure"),
    ],
)
async def test_connack_debug_guard_fails_closed_without_raw_packet_details(
    remaining_length: int,
    packet: bytes,
    classifier: str,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._client._in_packet = {  # type: ignore[attr-defined]
        "command": 0x20,
        "have_remaining": 1,
        "remaining_count": [remaining_length],
        "remaining_mult": 128,
        "remaining_length": remaining_length,
        "packet": bytearray(packet),
        "to_process": 0,
        "pos": 0,
    }

    result = adapter._client._handle_connack()  # type: ignore[attr-defined]
    error = adapter._connected.exception()

    assert result == paho.MQTT_ERR_PROTOCOL
    assert isinstance(error, aiomqtt.MqttError)
    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.details["classifier"] == classifier
    classification = MQTTTransport(_config())._classify_failure(
        adapter.protocol_failure_error,
        client=adapter,
        was_ready=False,
    )
    assert classification.code is ConnectionFailureCode.PROTOCOL_FAILURE
    assert classification.terminal is True
    assert "localhost" not in str(error)
    assert packet.hex() not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", [0x9C, 0x9D])
async def test_disconnect_redirect_reason_is_retained_without_endpoint(
    reason_code: int,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._connected.set_result(None)

    adapter._on_disconnect(
        adapter._client,
        None,
        paho.DisconnectFlags(is_disconnect_packet_from_server=True),
        ReasonCode(PacketTypes.DISCONNECT, identifier=reason_code),  # type: ignore[no-untyped-call]
        None,
    )

    assert adapter._server_reference_present is True
    adapter._disconnected.exception()


@pytest.mark.asyncio
async def test_disconnect_server_reference_property_is_terminal_evidence_without_value() -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._connected.set_result(None)
    redirect = "redirect-property-canary"
    properties = Properties(PacketTypes.DISCONNECT)  # type: ignore[no-untyped-call]
    properties.ServerReference = redirect

    adapter._on_disconnect(
        adapter._client,
        None,
        paho.DisconnectFlags(is_disconnect_packet_from_server=True),
        ReasonCode(PacketTypes.DISCONNECT, identifier=0x00),  # type: ignore[no-untyped-call]
        properties,
    )

    classification = MQTTTransport(_config())._classify_failure(
        ConnectionError("synthetic disconnect", operation="mqtt.receive"),
        client=adapter,
        was_ready=True,
    )
    assert classification.code is ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW
    assert classification.terminal is True
    assert redirect not in repr(adapter.__dict__)


@pytest.mark.asyncio
async def test_success_connack_server_reference_is_rejected_before_readiness() -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    redirect = "successful-connack-redirect-canary"
    properties = Properties(PacketTypes.CONNACK)  # type: ignore[no-untyped-call]
    properties.ServerReference = redirect

    adapter._on_connect(
        adapter._client,
        None,
        paho.ConnectFlags(session_present=False),
        ReasonCode(PacketTypes.CONNACK, identifier=0x00),  # type: ignore[no-untyped-call]
        properties,
    )

    error = adapter._connected.exception()
    assert isinstance(error, MqttConnectError)
    classification = MQTTTransport(_config())._classify_failure(
        error,
        client=adapter,
        was_ready=False,
    )
    assert adapter._server_reference_present is True
    assert classification.code is ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW
    assert classification.terminal is True
    assert redirect not in repr(adapter.__dict__)


@pytest.mark.asyncio
async def test_transport_close_kills_and_reaps_owned_dns_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_started = asyncio.Event()
    resolver_released = asyncio.Event()
    resolver_reaped = asyncio.Event()

    class StalledResolver:
        returncode: int | None = None

        async def communicate(self, _request: bytes) -> tuple[bytes, None]:
            resolver_started.set()
            await resolver_released.wait()
            return b'{"status":"error"}', None

        def kill(self) -> None:
            self.returncode = -9
            resolver_released.set()

        async def wait(self) -> int:
            await resolver_released.wait()
            resolver_reaped.set()
            assert self.returncode is not None
            return self.returncode

    resolver = StalledResolver()

    async def create_resolver(*_args: object, **_kwargs: object) -> StalledResolver:
        return resolver

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_resolver)
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5, timeout=1)
    transport = MQTTTransport(_config())
    connect_supervisor = asyncio.create_task(adapter.__aenter__())
    transport._supervisor = connect_supervisor  # type: ignore[assignment]
    await asyncio.wait_for(resolver_started.wait(), timeout=1)

    await asyncio.wait_for(transport.close(), timeout=1)

    assert connect_supervisor.done()
    assert connect_supervisor.cancelled()
    assert resolver.returncode == -9
    assert resolver_reaped.is_set()
    assert not adapter._lock.locked()
    assert not any(
        task.get_name().startswith("picogrid-ecn-dns-resolver")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_dns_timeout_kills_and_reaps_owned_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_started = asyncio.Event()
    resolver_released = asyncio.Event()
    resolver_reaped = asyncio.Event()

    class StalledResolver:
        returncode: int | None = None

        async def communicate(self, _request: bytes) -> tuple[bytes, None]:
            resolver_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await resolver_released.wait()
                return b'{"status":"error"}', None

        def kill(self) -> None:
            self.returncode = -9
            resolver_released.set()

        async def wait(self) -> int:
            await resolver_released.wait()
            resolver_reaped.set()
            assert self.returncode is not None
            return self.returncode

    resolver = StalledResolver()

    async def create_resolver(*_args: object, **_kwargs: object) -> StalledResolver:
        return resolver

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_resolver)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await _resolve_tcp_addresses("localhost", 1883)

    assert resolver_started.is_set()
    assert resolver.returncode == -9
    assert resolver_reaped.is_set()


@pytest.mark.asyncio
async def test_dns_resolver_cleanup_is_bounded_and_preserves_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class CancellationResistantResolver:
        returncode: int | None = None

        async def communicate(self, _request: bytes) -> tuple[bytes, None]:
            resolver_started.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
            return b'{"status":"error"}', None

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            cleanup_started.set()
            await release_cleanup.wait()
            assert self.returncode is not None
            return self.returncode

    resolver = CancellationResistantResolver()

    async def create_resolver(*_args: object, **_kwargs: object) -> CancellationResistantResolver:
        return resolver

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_resolver)
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._RESOLVER_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )

    resolving = asyncio.create_task(_resolve_tcp_addresses("localhost", 1883))
    await resolver_started.wait()
    resolving.cancel()
    await cleanup_started.wait()
    resolving.cancel()

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(0.2):
            await resolving

    release_cleanup.set()
    for _ in range(100):
        if not any(
            task.get_name().startswith("picogrid-ecn-dns-resolver")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ):
            break
        await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("picogrid-ecn-dns-resolver")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


def test_resolver_response_accepts_bounded_ipv4_and_ipv6_addresses() -> None:
    response = json.dumps(
        {
            "status": "ok",
            "addresses": [
                [socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "127.0.0.1", 1883, 0, 0],
                [socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "::1", 1883, 0, 0],
            ],
        },
        separators=(",", ":"),
    ).encode()

    resolved = _decode_resolver_response(response, expected_port=1883)

    assert resolved == (
        _ResolvedTCPAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("127.0.0.1", 1883),
        ),
        _ResolvedTCPAddress(
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("::1", 1883, 0, 0),
        ),
    )


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        [[socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "not-an-ip", 1883, 0, 0]],
        [[socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "127.0.0.1", 8883, 0, 0]],
        [[socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "127.0.0.1", 1883, 0, 0]],
        [[socket.AF_INET, True, socket.IPPROTO_TCP, "127.0.0.1", 1883, 0, 0]],
        [[socket.AF_INET, socket.SOCK_STREAM, False, "127.0.0.1", 1883, 0, 0]],
    ],
)
def test_resolver_response_rejects_invalid_or_mismatched_addresses(
    addresses: list[list[object]],
) -> None:
    response = json.dumps(
        {"status": "ok", "addresses": addresses},
        separators=(",", ":"),
    ).encode()

    with pytest.raises(_DNSResolutionFailure):
        _decode_resolver_response(response, expected_port=1883)


def test_resolver_process_rejects_address_sets_larger_than_validation_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = io.BytesIO(b'{"host":"localhost","port":1883}')
    response = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": request})())
    monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": response})())
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (f"127.0.0.{index}", 1883),
            )
            for index in range(1, 34)
        ],
    )

    _resolver_process_main()

    assert json.loads(response.getvalue()) == {"status": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "family", "address"),
    [
        ("127.0.0.1", socket.AF_INET, ("127.0.0.1", 1883)),
        ("::1", socket.AF_INET6, ("::1", 1883, 0, 0)),
    ],
)
async def test_literal_ip_resolution_uses_no_process(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    family: int,
    address: tuple[str, int] | tuple[str, int, int, int],
) -> None:
    async def reject_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("literal IP resolution must not start a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_process)

    assert await _resolve_tcp_addresses(host, 1883) == (
        _ResolvedTCPAddress(
            family,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            address,
        ),
    )


@pytest.mark.asyncio
async def test_hostname_resolution_runs_in_the_isolated_resolver_process() -> None:
    resolved = await asyncio.wait_for(_resolve_tcp_addresses("localhost", 1883), timeout=2)

    assert resolved
    assert len(resolved) <= 32
    assert all(ipaddress.ip_address(address.address[0]).is_loopback for address in resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "family"),
    [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)],
)
async def test_prepared_tcp_socket_connects_to_ipv4_and_ipv6_loopback(
    host: str,
    family: int,
) -> None:
    accepted: list[asyncio.StreamWriter] = []
    accepted_event = asyncio.Event()

    def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        accepted_event.set()

    try:
        server = await asyncio.start_server(accept, host=host, port=0, family=family)
    except OSError:
        if family == socket.AF_INET6:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    listening_socket = server.sockets[0]
    port = listening_socket.getsockname()[1]
    prepared_socket: socket.socket | None = None
    try:
        prepared_socket = await _open_prepared_socket(host, port, None)
        assert prepared_socket.family == family
        await asyncio.wait_for(accepted_event.wait(), timeout=1)
        assert len(accepted) == 1
    finally:
        if prepared_socket is not None:
            prepared_socket.close()
        for writer in accepted:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tcp_connection_cancellation_closes_prepared_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_started = asyncio.Event()
    observed_socket: socket.socket | None = None

    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def stall_connect(
        sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        nonlocal observed_socket
        observed_socket = sock
        connect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        stall_connect,
    )
    opening = asyncio.create_task(_open_prepared_socket("localhost", 1883, None))
    await asyncio.wait_for(connect_started.wait(), timeout=1)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert observed_socket is not None
    assert observed_socket.fileno() == -1


@pytest.mark.asyncio
async def test_tcp_connection_failure_is_classified_without_dependency_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def reject_connect(
        _sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        raise ConnectionRefusedError("synthetic dependency detail")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        reject_connect,
    )

    with pytest.raises(_TCPConnectionFailure) as caught:
        await _open_prepared_socket("localhost", 1883, None)

    assert "synthetic" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_reviewed_network_rejects_mixed_answer_before_socket_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_address = str(ipaddress.IPv4Address(bytes((10, 0, 0, 2))))
    public_address = str(ipaddress.IPv4Address(bytes((8, 8, 8, 8))))

    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                (private_address, port),
            ),
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                (public_address, port),
            ),
        )

    def unexpected_socket(*_args: object, **_kwargs: object) -> socket.socket:
        raise AssertionError("a refused answer set must not create a socket")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt.socket.socket",
        unexpected_socket,
    )

    with pytest.raises(TransportBoundaryError) as caught:
        await _open_prepared_socket(
            "mqtt-container.example",
            1883,
            None,
            require_reviewed_network=True,
            attempt_timeout=1,
        )

    rendered = str(caught.value)
    assert "mqtt-container.example" not in rendered
    assert public_address not in rendered


@pytest.mark.asyncio
async def test_reviewed_network_falls_back_across_exact_validated_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_address = str(ipaddress.IPv4Address(bytes((10, 0, 0, 2))))
    second_address = str(ipaddress.IPv4Address(bytes((10, 0, 0, 3))))
    addresses = (
        _ResolvedTCPAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            (first_address, 1883),
        ),
        _ResolvedTCPAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            (second_address, 1883),
        ),
    )
    attempted: list[tuple[str, int] | tuple[str, int, int, int]] = []
    observed_sockets: list[socket.socket] = []

    async def resolve(_host: str, _port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return addresses

    async def connect(
        sock: socket.socket,
        address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        observed_sockets.append(sock)
        attempted.append(address)
        if len(attempted) == 1:
            raise ConnectionRefusedError("synthetic refused address")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        connect,
    )

    prepared = await _open_prepared_socket(
        "mqtt-container.example",
        1883,
        None,
        require_reviewed_network=True,
        attempt_timeout=1,
    )
    try:
        assert attempted == [addresses[0].address, addresses[1].address]
        assert observed_sockets[0].fileno() == -1
        assert prepared is observed_sockets[1]
    finally:
        prepared.close()


@pytest.mark.asyncio
async def test_tls_handshake_cancellation_closes_prepared_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handshake_started = asyncio.Event()
    observed_socket: ssl.SSLSocket | None = None

    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def accept_connect(
        _sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        return None

    async def stall_handshake(sock: ssl.SSLSocket) -> None:
        nonlocal observed_socket
        observed_socket = sock
        handshake_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        accept_connect,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._perform_tls_handshake",
        stall_handshake,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opening = asyncio.create_task(_open_prepared_socket("localhost", 1883, context))
    await asyncio.wait_for(handshake_started.wait(), timeout=1)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert observed_socket is not None
    assert observed_socket.fileno() == -1
    assert observed_socket.server_hostname == "localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency_error", "marker_type"),
    [
        (ssl.SSLError("synthetic transport detail"), _TLSTransportFailure),
        (
            ssl.SSLCertVerificationError(1, "synthetic peer detail"),
            _TLSPeerVerificationFailure,
        ),
    ],
)
async def test_tls_handshake_failure_is_classified_without_dependency_detail(
    monkeypatch: pytest.MonkeyPatch,
    dependency_error: ssl.SSLError,
    marker_type: type[aiomqtt.MqttError],
) -> None:
    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def accept_connect(
        _sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        return None

    async def reject_handshake(_sock: ssl.SSLSocket) -> None:
        raise dependency_error

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        accept_connect,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._perform_tls_handshake",
        reject_handshake,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with pytest.raises(marker_type) as caught:
        await _open_prepared_socket("localhost", 1883, context)

    assert "synthetic" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency_error_type", "expect_fallback"),
    [
        (ssl.SSLError, True),
        (OSError, True),
        (ssl.SSLCertVerificationError, False),
    ],
)
async def test_tls_address_fallback_distinguishes_transport_and_peer_verification(
    monkeypatch: pytest.MonkeyPatch,
    dependency_error_type: type[OSError],
    expect_fallback: bool,
) -> None:
    addresses = (
        _ResolvedTCPAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("127.0.0.1", 1883),
        ),
        _ResolvedTCPAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("127.0.0.2", 1883),
        ),
    )
    attempted_addresses: list[tuple[str, int] | tuple[str, int, int, int]] = []
    handshake_sockets: list[ssl.SSLSocket] = []

    async def resolve(_host: str, _port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return addresses

    async def accept_connect(
        _sock: socket.socket,
        address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        attempted_addresses.append(address)

    async def handshake(sock: ssl.SSLSocket) -> None:
        handshake_sockets.append(sock)
        if len(handshake_sockets) == 1:
            if issubclass(dependency_error_type, ssl.SSLCertVerificationError):
                raise dependency_error_type(1, "synthetic peer detail")
            raise dependency_error_type("synthetic transport detail")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        accept_connect,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._perform_tls_handshake",
        handshake,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    if not expect_fallback:
        with pytest.raises(_TLSPeerVerificationFailure) as caught:
            await _open_prepared_socket("localhost", 1883, context)

        assert attempted_addresses == [addresses[0].address]
        assert len(handshake_sockets) == 1
        assert handshake_sockets[0].fileno() == -1
        assert "synthetic" not in "".join(traceback.format_exception(caught.value))
        return

    prepared = await _open_prepared_socket("localhost", 1883, context)
    try:
        assert attempted_addresses == [addresses[0].address, addresses[1].address]
        assert len(handshake_sockets) == 2
        assert handshake_sockets[0].fileno() == -1
        assert prepared is handshake_sockets[1]
        assert prepared.fileno() >= 0
    finally:
        prepared.close()

    assert handshake_sockets[1].fileno() == -1


@pytest.mark.asyncio
async def test_prepared_socket_rejects_tls_below_1_2_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_started = False

    async def unexpected_resolution(
        _host: str,
        _port: int,
    ) -> tuple[_ResolvedTCPAddress, ...]:
        nonlocal resolution_started
        resolution_started = True
        raise AssertionError("insecure TLS context reached resolution")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        unexpected_resolution,
    )

    class InsecureTLSContext:
        minimum_version = ssl.TLSVersion.TLSv1_1
        maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED

    context = cast("ssl.SSLContext", InsecureTLSContext())

    with pytest.raises(ConfigurationError, match=r"TLS 1\.2-or-newer") as caught:
        await _open_prepared_socket("localhost", 1883, context)

    assert caught.value.operation == "mqtt.connect"
    assert resolution_started is False


@pytest.mark.asyncio
async def test_prepared_socket_rejects_contradictory_tls_range_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_started = False

    async def unexpected_resolution(
        _host: str,
        _port: int,
    ) -> tuple[_ResolvedTCPAddress, ...]:
        nonlocal resolution_started
        resolution_started = True
        raise AssertionError("contradictory TLS context reached resolution")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        unexpected_resolution,
    )

    class ContradictoryTLSContext:
        minimum_version = ssl.TLSVersion.TLSv1_2
        maximum_version = ssl.TLSVersion.TLSv1_1

    context = cast("ssl.SSLContext", ContradictoryTLSContext())

    with pytest.raises(ConfigurationError, match=r"TLS 1\.2-or-newer") as caught:
        await _open_prepared_socket("localhost", 1883, context)

    classification = MQTTTransport(_config())._classify_failure(
        caught.value,
        client=None,
        was_ready=False,
    )
    assert caught.value.operation == "mqtt.connect"
    assert resolution_started is False
    assert classification.code is ConnectionFailureCode.CONFIGURATION_INVALID
    assert classification.operation is ConnectionFailureOperation.CONFIGURE
    assert classification.terminal is True


def test_stalled_dns_resolver_does_not_delay_interpreter_shutdown() -> None:
    child = r"""
import asyncio, contextlib
from paho.mqtt import client as paho
from picogrid_ecn_client._transport import mqtt

async def main():
    mqtt._RESOLVER_PROCESS_CODE = "import time; time.sleep(30)"
    client = mqtt._MQTTV5Client(
        hostname="localhost",
        port=1883,
        identifier="dns-shutdown-check",
        protocol=paho.MQTTv5,
        timeout=30,
    )
    opening = asyncio.create_task(client.__aenter__())
    for _ in range(100):
        if any(
            task.get_name() == "picogrid-ecn-dns-resolver-io"
            for task in asyncio.all_tasks()
        ):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("resolver child did not start")
    opening.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await opening
    current = asyncio.current_task()
    assert not [
        task for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]

asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", child],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_stalled_tls_handshake_does_not_delay_interpreter_shutdown() -> None:
    child = r"""
import asyncio, contextlib, ssl
from paho.mqtt import client as paho
from picogrid_ecn_client._transport.mqtt import _MQTTV5Client

async def main():
    accepted = asyncio.Event()
    writers = []
    def accept(_reader, writer):
        writers.append(writer)
        accepted.set()
    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    client = _MQTTV5Client(
        hostname="127.0.0.1",
        port=port,
        identifier="shutdown-check",
        protocol=paho.MQTTv5,
        tls_context=context,
        timeout=30,
    )
    opening = asyncio.create_task(client.__aenter__())
    await asyncio.wait_for(accepted.wait(), 1)
    opening.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await opening
    for writer in writers:
        writer.close()
        await writer.wait_closed()
    server.close()
    await server.wait_closed()
    await asyncio.sleep(0)
    current = asyncio.current_task()
    assert not [
        task for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]

asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", child],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_queued_socket_registration_is_invalidated_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    client_socket, peer_socket = socket.socketpair()
    installed: list[int] = []
    loop = asyncio.get_running_loop()
    original_add_reader = loop.add_reader

    def observe_reader(descriptor: int, callback: object, *args: object) -> None:
        installed.append(descriptor)
        original_add_reader(descriptor, callback, *args)

    monkeypatch.setattr(loop, "add_reader", observe_reader)
    adapter._client._sock = client_socket
    adapter._on_socket_open(adapter._client, None, client_socket)
    adapter._force_disconnect()
    await asyncio.sleep(0)

    assert installed == []
    assert client_socket.fileno() == -1
    peer_socket.close()


@pytest.mark.asyncio
async def test_queued_socket_writer_is_invalidated_before_unregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    client_socket, peer_socket = socket.socketpair()
    installed: list[int] = []
    loop = asyncio.get_running_loop()
    original_add_writer = loop.add_writer

    def observe_writer(descriptor: int, callback: object, *args: object) -> None:
        installed.append(descriptor)
        original_add_writer(descriptor, callback, *args)

    monkeypatch.setattr(loop, "add_writer", observe_writer)
    adapter._client._sock = client_socket
    adapter._on_socket_register_write(adapter._client, None, client_socket)
    adapter._on_socket_unregister_write(adapter._client, None, client_socket)
    await asyncio.sleep(0)

    assert installed == []
    adapter._force_disconnect()
    assert client_socket.fileno() == -1
    peer_socket.close()


@pytest.mark.asyncio
async def test_prepared_mqtt_connect_uses_no_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test")
        assert isinstance(config.auth, BearerTokenAuth)
        assert config.auth.token is not None
        adapter = _MQTTV5Client(
            hostname=mock.host,
            port=mock.mqtt_port,
            username="transport-test",
            password=config.auth.token.get_secret_value(),
            identifier="prepared-connect-test",
            protocol=paho.MQTTv5,
            clean_start=True,
        )
        loop = asyncio.get_running_loop()

        def reject_executor(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("MQTT socket setup must not use the default executor")

        monkeypatch.setattr(loop, "run_in_executor", reject_executor)

        async with adapter:
            assert mock.active_connection_count == 1

        for _ in range(10):
            if mock.active_connection_count == 0:
                break
            await asyncio.sleep(0)
        assert mock.active_connection_count == 0


def test_mqtt_v5_publish_acknowledgement_reason_mapping() -> None:
    _ensure_publication_accepted(None)
    _ensure_publication_accepted(0x00)
    _ensure_publication_accepted(0x10)

    with pytest.raises(AuthorizationError) as authorization:
        _ensure_publication_accepted(0x87)
    assert authorization.value.details == {"reason_code": "135"}

    with pytest.raises(ResourceLimitError) as resource_limit:
        _ensure_publication_accepted(0x97)
    assert resource_limit.value.details == {"reason_code": "151"}

    malformed = (
        (object(), "non_numeric_reason_code", None),
        (0x01, "unlisted_success", "1"),
        (0x80, "unmapped_failure", "128"),
    )
    for reason_code, classifier, numeric_code in malformed:
        with pytest.raises(ProtocolError) as caught:
            _ensure_publication_accepted(reason_code)
        expected = {"packet_type": "PUBACK", "classifier": classifier}
        if numeric_code is not None:
            expected["reason_code"] = numeric_code
        assert caught.value.details == expected


def test_mqtt_v5_unsubscribe_acknowledgement_reason_mapping() -> None:
    _ensure_unsubscription_accepted([0x00])
    _ensure_unsubscription_accepted([0x11])

    with pytest.raises(AuthorizationError) as authorization:
        _ensure_unsubscription_accepted([0x87])
    assert authorization.value.details == {"reason_code": "135"}

    malformed = (
        (None, "wrong_cardinality", None),
        ([], "wrong_cardinality", None),
        ([0x00, 0x00], "wrong_cardinality", None),
        ([None], "missing_reason_code", None),
        ([object()], "non_numeric_reason_code", None),
        ([0x01], "unlisted_success", "1"),
        ([0x80], "unmapped_failure", "128"),
        ([0x97], "unmapped_failure", "151"),
    )
    for reason_codes, classifier, numeric_code in malformed:
        with pytest.raises(ProtocolError) as caught:
            _ensure_unsubscription_accepted(reason_codes)
        expected = {"packet_type": "UNSUBACK", "classifier": classifier}
        if numeric_code is not None:
            expected["reason_code"] = numeric_code
        assert caught.value.details == expected


def test_mqtt_v5_subscribe_acknowledgement_reason_mapping() -> None:
    _ensure_subscription_accepted([0x00])
    _ensure_subscription_accepted([0x01])

    with pytest.raises(AuthorizationError):
        _ensure_subscription_accepted([0x87])
    with pytest.raises(ResourceLimitError):
        _ensure_subscription_accepted([0x97])

    malformed = (
        (None, "wrong_cardinality", None),
        ([], "wrong_cardinality", None),
        ([0x00, 0x01], "wrong_cardinality", None),
        ([None], "missing_reason_code", None),
        ([object()], "non_numeric_reason_code", None),
        ([0x02], "unlisted_success", "2"),
        ([0x80], "unmapped_failure", "128"),
    )
    for reason_codes, classifier, numeric_code in malformed:
        with pytest.raises(ProtocolError) as caught:
            _ensure_subscription_accepted(reason_codes)
        expected = {"packet_type": "SUBACK", "classifier": classifier}
        if numeric_code is not None:
            expected["reason_code"] = numeric_code
        assert caught.value.details == expected


class _ForceClosePahoClient:
    def __init__(self, disconnect_result: int) -> None:
        self.disconnect_result = disconnect_result
        self.force_close_calls = 0

    def disconnect(self) -> int:
        return self.disconnect_result

    def _sock_close(self) -> None:
        self.force_close_calls += 1


def _bare_ack_adapter(underlying: object) -> _MQTTV5Client:
    adapter = object.__new__(_MQTTV5Client)
    adapter._acknowledgement_lock = threading.RLock()
    adapter._pending_publish_acknowledgements = {}
    adapter._pending_unsubscribe_acknowledgements = {}
    adapter._retired_publish_mids = set()
    adapter._retired_subscribe_mids = set()
    adapter._retired_unsubscribe_mids = set()
    adapter._early_publish_acknowledgements = {}
    adapter._early_subscribe_acknowledgements = {}
    adapter._early_unsubscribe_acknowledgements = {}
    adapter._starting_publish = 0
    adapter._starting_subscribe = 0
    adapter._starting_unsubscribe = 0
    adapter._protocol_failure_error = None
    adapter._pending_subscribes = {}
    adapter._outgoing_calls_sem = None
    adapter._client = underlying  # type: ignore[assignment]
    adapter._loop = asyncio.get_running_loop()
    adapter._disconnected = adapter._loop.create_future()
    return adapter


def _set_incoming_packet(adapter: _MQTTV5Client, packet: bytes) -> None:
    adapter._client._in_packet = {  # type: ignore[attr-defined]
        "command": 0,
        "have_remaining": 1,
        "remaining_count": [len(packet)],
        "remaining_mult": 128,
        "remaining_length": len(packet),
        "packet": bytearray(packet),
        "to_process": 0,
        "pos": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disconnect_result",
    [paho.MQTT_ERR_SUCCESS, paho.MQTT_ERR_NO_CONN],
)
async def test_session_invalidation_force_closes_after_failure_or_timeout(
    disconnect_result: int,
) -> None:
    underlying = _ForceClosePahoClient(disconnect_result)
    adapter = _bare_ack_adapter(underlying)
    adapter._retired_subscribe_mids.add(3)
    adapter._retired_unsubscribe_mids.add(4)

    await adapter.invalidate_session(timeout=0)

    assert underlying.force_close_calls == 1
    assert adapter._disconnected.done()
    assert adapter._retired_subscribe_mids == set()
    assert adapter._retired_unsubscribe_mids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["exception", "cancelled"])
async def test_session_invalidation_force_closes_abnormal_disconnect_future(
    completion: str,
) -> None:
    underlying = _ForceClosePahoClient(paho.MQTT_ERR_SUCCESS)
    adapter = _bare_ack_adapter(underlying)
    if completion == "exception":
        adapter._disconnected.set_exception(RuntimeError("synthetic reader failure"))
    else:
        adapter._disconnected.cancel()

    await adapter.invalidate_session(timeout=1)

    assert underlying.force_close_calls == 1
    assert adapter._disconnected.done()
    assert not adapter._disconnected.cancelled()


class _SyntheticPublishInfo:
    rc = paho.MQTT_ERR_SUCCESS

    def __init__(self, mid: int) -> None:
        self.mid = mid

    def is_published(self) -> bool:
        return False


class _ReusedAcknowledgementClient:
    def __init__(self) -> None:
        self.adapter: _MQTTV5Client | None = None
        self.publish_calls = 0
        self.unsubscribe_calls = 0

    def publish(self, *_args: Any, **_kwargs: Any) -> _SyntheticPublishInfo:
        assert self.adapter is not None
        self.publish_calls += 1
        if self.publish_calls == 1:
            self.adapter._on_publish(self, None, 7, 0x00, None)  # type: ignore[arg-type]
            mid, reason_code = 8, 0x00
        else:
            mid, reason_code = 7, 0x87
        self.adapter._loop.call_soon(
            self.adapter._on_publish,
            self,
            None,
            mid,
            reason_code,
            None,
        )
        return _SyntheticPublishInfo(mid)

    def unsubscribe(self, *_args: Any, **_kwargs: Any) -> tuple[int, int]:
        assert self.adapter is not None
        self.unsubscribe_calls += 1
        if self.unsubscribe_calls == 1:
            self.adapter._on_unsubscribe(self, None, 7, [0x00])  # type: ignore[arg-type]
            mid, reason_codes = 8, [0x00]
        else:
            mid, reason_codes = 7, [0x87]
        self.adapter._loop.call_soon(
            self.adapter._on_unsubscribe,
            self,
            None,
            mid,
            reason_codes,
        )
        return paho.MQTT_ERR_SUCCESS, mid


class _NeverAcknowledgesClient:
    def publish(self, *_args: Any, **_kwargs: Any) -> _SyntheticPublishInfo:
        return _SyntheticPublishInfo(11)


class _AcknowledgementFormClient:
    def __init__(self, reason_code: object, properties: object) -> None:
        self.adapter: _MQTTV5Client | None = None
        self.reason_code = reason_code
        self.properties = properties

    def publish(self, *_args: Any, **_kwargs: Any) -> _SyntheticPublishInfo:
        assert self.adapter is not None
        self.adapter._on_publish(  # type: ignore[arg-type]
            self,
            None,
            23,
            self.reason_code,
            self.properties,
        )
        return _SyntheticPublishInfo(23)


class _PendingAcknowledgementClient:
    def __init__(self) -> None:
        self.next_mid = 40

    def publish(self, *_args: Any, **_kwargs: Any) -> _SyntheticPublishInfo:
        self.next_mid += 1
        return _SyntheticPublishInfo(self.next_mid)


class _RejectedPublishInfo(_SyntheticPublishInfo):
    rc = paho.MQTT_ERR_NO_CONN


class _RejectsBeforeSendClient:
    def publish(self, *_args: Any, **_kwargs: Any) -> _RejectedPublishInfo:
        return _RejectedPublishInfo(12)


@pytest.mark.asyncio
async def test_stale_publish_callback_cannot_satisfy_reused_mid() -> None:
    underlying = _ReusedAcknowledgementClient()
    adapter = _bare_ack_adapter(underlying)
    underlying.adapter = adapter
    with pytest.raises(aiomqtt.MqttError):
        await adapter.publish("entity/vendor/id/detection", b"first", qos=1, timeout=1)
    await asyncio.sleep(0)

    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.details == {
        "packet_type": "PUBACK",
        "classifier": "unmatched_packet_identifier",
    }
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "handler_name", "operation"),
    [
        (PacketTypes.SUBACK, "_handle_suback", "mqtt.subscribe"),
        (PacketTypes.UNSUBACK, "_handle_unsuback", "mqtt.unsubscribe"),
    ],
)
async def test_raw_subscription_ack_guards_delegate_valid_property_packets(
    packet_type: int,
    handler_name: str,
    operation: str,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 29
    pending: asyncio.Future[object] = asyncio.Future()
    if packet_type == PacketTypes.SUBACK:
        adapter._pending_subscribes[packet_identifier] = pending
        reason_code = 0x01
    else:
        adapter._pending_unsubscribe_acknowledgements[packet_identifier] = pending
        reason_code = 0x11
    properties = Properties(packet_type)  # type: ignore[no-untyped-call]
    properties.ReasonString = "ignored broker detail"
    properties.UserProperty = [("ignored", "value")]
    packet = (
        packet_identifier.to_bytes(2, "big")
        + bytes(properties.pack())  # type: ignore[no-untyped-call]
        + bytes((reason_code,))
    )
    _set_incoming_packet(adapter, packet)

    result = getattr(adapter._client, handler_name)()
    await asyncio.sleep(0)

    assert result in {None, paho.MQTT_ERR_SUCCESS}
    assert adapter.protocol_failure_error is None
    reason_codes = pending.result()
    if operation == "mqtt.subscribe":
        _ensure_subscription_accepted(reason_codes)
    else:
        _ensure_unsubscription_accepted(reason_codes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "handler_name", "retired_attribute", "reason_code"),
    [
        (PacketTypes.SUBACK, "_handle_suback", "_retired_subscribe_mids", b"\x01"),
        (PacketTypes.UNSUBACK, "_handle_unsuback", "_retired_unsubscribe_mids", b"\x00"),
    ],
)
async def test_raw_subscription_ack_guard_consumes_valid_retired_acknowledgement(
    packet_type: int,
    handler_name: str,
    retired_attribute: str,
    reason_code: bytes,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 30
    retired_identifiers: set[int] = getattr(adapter, retired_attribute)
    retired_identifiers.add(packet_identifier)
    packet = packet_identifier.to_bytes(2, "big") + b"\x00" + reason_code
    _set_incoming_packet(adapter, packet)

    result = getattr(adapter._client, handler_name)()
    await asyncio.sleep(0)

    assert result in {None, paho.MQTT_ERR_SUCCESS}
    assert adapter.protocol_failure_error is None
    assert retired_identifiers == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["cancelled", "timeout"])
async def test_unobserved_suback_is_retired_until_late_callback(
    monkeypatch: pytest.MonkeyPatch,
    completion: str,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 71
    operation_started = asyncio.Event()

    def leave_unacknowledged(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        operation_started.set()
        return paho.MQTT_ERR_SUCCESS, packet_identifier

    monkeypatch.setattr(adapter._client, "subscribe", leave_unacknowledged)

    async def subscribe(timeout: float) -> object:
        return await adapter.subscribe(
            "entity/vendor-a/+/track",
            qos=1,
            timeout=timeout,
        )

    if completion == "cancelled":
        operation = asyncio.create_task(subscribe(10))
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        with pytest.raises(aiomqtt.MqttError):
            await subscribe(0)

    assert adapter._retired_subscribe_mids == {packet_identifier}
    packet = packet_identifier.to_bytes(2, "big") + b"\x00\x01"
    _set_incoming_packet(adapter, packet)

    result = adapter._client._handle_suback()
    await asyncio.sleep(0)

    assert result in {None, paho.MQTT_ERR_SUCCESS}
    assert adapter._retired_subscribe_mids == set()
    assert adapter.protocol_failure_error is None
    adapter._force_disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "operation_name", "retired_attribute", "packet_name"),
    [
        (PacketTypes.SUBACK, "subscribe", "_retired_subscribe_mids", "SUBACK"),
        (PacketTypes.UNSUBACK, "unsubscribe", "_retired_unsubscribe_mids", "UNSUBACK"),
    ],
)
async def test_retired_subscription_identifier_cannot_be_reused_ambiguously(
    monkeypatch: pytest.MonkeyPatch,
    packet_type: int,
    operation_name: str,
    retired_attribute: str,
    packet_name: str,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 73
    retired_identifiers: set[int] = getattr(adapter, retired_attribute)
    retired_identifiers.add(packet_identifier)
    monkeypatch.setattr(
        adapter._client,
        operation_name,
        lambda *_args, **_kwargs: (paho.MQTT_ERR_SUCCESS, packet_identifier),
    )

    with pytest.raises(ProtocolError) as caught:
        if packet_type == PacketTypes.SUBACK:
            await adapter.subscribe("entity/vendor-a/+/track", qos=1, timeout=1)
        else:
            await adapter.unsubscribe("entity/vendor-a/+/track", timeout=1)
    await asyncio.sleep(0)

    assert caught.value.operation == f"mqtt.{operation_name}"
    assert caught.value.details == {
        "packet_type": packet_name,
        "classifier": "unmatched_packet_identifier",
    }
    assert adapter.protocol_failure_error is caught.value
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "handler_name", "operation"),
    [
        (PacketTypes.SUBACK, "_handle_suback", "mqtt.subscribe"),
        (PacketTypes.UNSUBACK, "_handle_unsuback", "mqtt.unsubscribe"),
    ],
)
@pytest.mark.parametrize(
    ("acknowledgement_tail", "classifier"),
    [
        (b"", "malformed_properties"),
        (b"\x80", "malformed_properties"),
        (b"\x04\x1f", "malformed_properties"),
        (b"\x04\x1f\x00\x01\xff", "malformed_properties"),
        (b"\x02\x01\x00", "malformed_properties"),
        (b"\x00", "missing_reason_code"),
        (b"\x00\x7f", "unlisted_success"),
        (b"\x00\xff", "unmapped_failure"),
    ],
    ids=[
        "missing-properties",
        "truncated-property-length",
        "declared-length-overrun",
        "invalid-utf8",
        "forbidden-property",
        "missing-reason-code",
        "unknown-success",
        "unknown-failure",
    ],
)
async def test_raw_subscription_ack_guards_fail_pending_operation_as_protocol(
    packet_type: int,
    handler_name: str,
    operation: str,
    acknowledgement_tail: bytes,
    classifier: str,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 31
    pending: asyncio.Future[object] = asyncio.Future()
    if packet_type == PacketTypes.SUBACK:
        adapter._pending_subscribes[packet_identifier] = pending
    else:
        adapter._pending_unsubscribe_acknowledgements[packet_identifier] = pending
    packet = packet_identifier.to_bytes(2, "big") + acknowledgement_tail
    _set_incoming_packet(adapter, packet)

    result = getattr(adapter._client, handler_name)()
    await asyncio.sleep(0)

    assert result == paho.MQTT_ERR_PROTOCOL
    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.operation == operation
    assert adapter.protocol_failure_error.details["classifier"] == classifier
    assert isinstance(pending.exception(), ProtocolError)
    assert packet.hex() not in str(adapter.protocol_failure_error)
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "handler_name", "operation", "reason_codes"),
    [
        (PacketTypes.SUBACK, "_handle_suback", "mqtt.subscribe", b"\x00\x01"),
        (PacketTypes.UNSUBACK, "_handle_unsuback", "mqtt.unsubscribe", b"\x00\x11"),
    ],
)
async def test_raw_subscription_ack_guard_rejects_multiple_reason_codes(
    packet_type: int,
    handler_name: str,
    operation: str,
    reason_codes: bytes,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 37
    pending: asyncio.Future[object] = asyncio.Future()
    if packet_type == PacketTypes.SUBACK:
        adapter._pending_subscribes[packet_identifier] = pending
    else:
        adapter._pending_unsubscribe_acknowledgements[packet_identifier] = pending
    packet = packet_identifier.to_bytes(2, "big") + b"\x00" + reason_codes
    _set_incoming_packet(adapter, packet)

    result = getattr(adapter._client, handler_name)()
    await asyncio.sleep(0)

    assert result == paho.MQTT_ERR_PROTOCOL
    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.operation == operation
    assert adapter.protocol_failure_error.details == {
        "packet_type": "SUBACK" if packet_type == PacketTypes.SUBACK else "UNSUBACK",
        "classifier": "wrong_cardinality",
    }
    assert isinstance(pending.exception(), ProtocolError)
    assert packet.hex() not in str(adapter.protocol_failure_error)
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("packet_type", "handler_name", "operation", "reason_code"),
    [
        (PacketTypes.SUBACK, "_handle_suback", "mqtt.subscribe", b"\x00"),
        (PacketTypes.UNSUBACK, "_handle_unsuback", "mqtt.unsubscribe", b"\x11"),
    ],
)
async def test_raw_subscription_ack_guard_rejects_unmatched_packet_identifier(
    packet_type: int,
    handler_name: str,
    operation: str,
    reason_code: bytes,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet = b"\x00\x2b\x00" + reason_code
    _set_incoming_packet(adapter, packet)

    result = getattr(adapter._client, handler_name)()
    await asyncio.sleep(0)

    assert result == paho.MQTT_ERR_PROTOCOL
    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.operation == operation
    assert adapter.protocol_failure_error.details == {
        "packet_type": "SUBACK" if packet_type == PacketTypes.SUBACK else "UNSUBACK",
        "classifier": "unmatched_packet_identifier",
    }
    assert packet.hex() not in str(adapter.protocol_failure_error)
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("packet_type", [PacketTypes.SUBACK, PacketTypes.UNSUBACK])
async def test_subscription_ack_during_mid_registration_is_correlated(
    monkeypatch: pytest.MonkeyPatch,
    packet_type: int,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 47
    handler_results: list[object] = []

    def acknowledge_during_registration(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        reason_code = b"\x01" if packet_type == PacketTypes.SUBACK else b"\x00"
        packet = packet_identifier.to_bytes(2, "big") + b"\x00" + reason_code
        _set_incoming_packet(adapter, packet)
        handler_name = "_handle_suback" if packet_type == PacketTypes.SUBACK else "_handle_unsuback"
        handler_results.append(getattr(adapter._client, handler_name)())
        return paho.MQTT_ERR_SUCCESS, packet_identifier

    if packet_type == PacketTypes.SUBACK:
        monkeypatch.setattr(adapter._client, "subscribe", acknowledge_during_registration)
        reason_codes = await adapter.subscribe("entity/vendor-a/+/track", qos=1, timeout=1)
        _ensure_subscription_accepted(reason_codes)
    else:
        monkeypatch.setattr(adapter._client, "unsubscribe", acknowledge_during_registration)
        await adapter.unsubscribe("entity/vendor-a/+/track", timeout=1)

    assert len(handler_results) == 1
    assert handler_results[0] != paho.MQTT_ERR_PROTOCOL
    assert adapter.protocol_failure_error is None
    assert adapter._early_subscribe_acknowledgements == {}
    assert adapter._early_unsubscribe_acknowledgements == {}
    assert adapter._pending_subscribes == {}
    assert adapter._pending_unsubscribe_acknowledgements == {}


@pytest.mark.asyncio
async def test_disconnect_fails_pending_unsuback_without_waiting_for_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._connected.set_result(None)
    unsubscribe_started = asyncio.Event()

    def leave_unacknowledged(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        unsubscribe_started.set()
        return paho.MQTT_ERR_SUCCESS, 53

    monkeypatch.setattr(adapter._client, "unsubscribe", leave_unacknowledged)
    unsubscribing: asyncio.Task[None] | None = None
    try:
        unsubscribing = asyncio.create_task(
            adapter.unsubscribe("entity/vendor-a/+/track", timeout=10)
        )
        await asyncio.wait_for(unsubscribe_started.wait(), timeout=1)
        assert 53 in adapter._pending_unsubscribe_acknowledgements

        adapter._on_disconnect(
            adapter._client,
            None,
            paho.DisconnectFlags(is_disconnect_packet_from_server=True),
            ReasonCode(PacketTypes.DISCONNECT, identifier=0x00),  # type: ignore[no-untyped-call]
            None,
        )

        with pytest.raises(aiomqtt.MqttError) as caught:
            await asyncio.wait_for(asyncio.shield(unsubscribing), timeout=0.1)
        assert "localhost" not in str(caught.value)
        assert adapter._pending_unsubscribe_acknowledgements == {}
        assert adapter._retired_unsubscribe_mids == set()
        assert adapter._disconnected.done()
    finally:
        if unsubscribing is not None and not unsubscribing.done():
            unsubscribing.cancel()
        if unsubscribing is not None:
            await asyncio.gather(unsubscribing, return_exceptions=True)
        if not adapter._disconnected.done():
            adapter._force_disconnect()


@pytest.mark.asyncio
async def test_raw_suback_guard_rejects_granted_qos_2_for_qos_1_request() -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    packet_identifier = 41
    pending: asyncio.Future[object] = asyncio.Future()
    adapter._pending_subscribes[packet_identifier] = pending
    packet = packet_identifier.to_bytes(2, "big") + b"\x00\x02"
    _set_incoming_packet(adapter, packet)

    result = adapter._client._handle_suback()
    await asyncio.sleep(0)

    assert result == paho.MQTT_ERR_PROTOCOL
    assert adapter.protocol_failure_error is not None
    assert adapter.protocol_failure_error.operation == "mqtt.subscribe"
    assert adapter.protocol_failure_error.details == {
        "packet_type": "SUBACK",
        "classifier": "unlisted_success",
        "reason_code": "2",
    }
    assert isinstance(pending.exception(), ProtocolError)
    assert packet.hex() not in str(adapter.protocol_failure_error)
    assert adapter._disconnected.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "operation", "packet_type"),
    [
        ("suback", "mqtt.subscribe", "SUBACK"),
        ("unsuback", "mqtt.unsubscribe", "UNSUBACK"),
    ],
)
async def test_malformed_subscription_ack_surfaces_typed_protocol_failure(
    response: str,
    operation: str,
    packet_type: str,
) -> None:
    async with MockECN() as mock:
        config = mock.client_config("transport-test").model_copy(
            update={
                "connection_timeout": 1.0,
                "operation_timeout": 1.0,
                "reconnect_policy": ReconnectPolicy(
                    initial_delay_seconds=30,
                    maximum_delay_seconds=30,
                ),
            }
        )
        transport = MQTTTransport(config, random_source=lambda: 1.0)

        async def callback(_topic: str, _payload: bytes) -> None:
            return None

        try:
            await transport.start()
            handle = None
            if response == "unsuback":
                handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
            mock.malform_next_protocol_responses(response)
            with pytest.raises(ProtocolError) as caught:
                if handle is None:
                    await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
                else:
                    await transport.unsubscribe(handle)
        finally:
            await transport.close()

        assert caught.value.operation == operation
        assert caught.value.details == {
            "packet_type": packet_type,
            "classifier": "malformed_properties",
        }
        assert mock.events.malformed_protocol_response.is_set()


@pytest.mark.asyncio
async def test_stale_unsubscribe_callback_cannot_satisfy_reused_mid() -> None:
    underlying = _ReusedAcknowledgementClient()
    adapter = _bare_ack_adapter(underlying)
    underlying.adapter = adapter
    adapter._retired_unsubscribe_mids.add(7)

    await adapter.unsubscribe("entity/+/+/detection", timeout=1)
    with pytest.raises(AuthorizationError):
        await adapter.unsubscribe("entity/+/+/detection", timeout=1)


@pytest.mark.asyncio
async def test_token_provider_failure_has_no_raw_cause_or_traceback_secret() -> None:
    canary = "do-not-echo-token-provider-secret"

    async def provider() -> str:
        raise RuntimeError(canary)

    config = _config().model_copy(update={"auth": BearerTokenAuth(token_provider=provider)})
    transport = MQTTTransport(config)

    with pytest.raises(AuthenticationError) as caught:
        await transport._authentication()

    assert canary not in str(caught.value)
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_legion_provider_failure_retains_only_safe_actionable_guidance(
    tmp_path: Path,
) -> None:
    provider = legion_system_auth_provider(tmp_path / "missing")
    config = _config().model_copy(update={"auth": BearerTokenAuth(credentials_provider=provider)})
    transport = MQTTTransport(config)

    with pytest.raises(AuthenticationError, match=r"legion-auth setup") as caught:
        await transport._authentication()

    assert caught.value.code == "legion_credentials_missing"
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_start_failure_does_not_trust_spoofed_legion_error_codes() -> None:
    canary = "caller-provider-secret-canary"
    transport = MQTTTransport(_config())
    transport._last_error = AuthenticationError(canary, code="legion_credentials_missing")

    with pytest.raises(AuthenticationError) as caught:
        await transport._raise_start_failure(timed_out=False)

    assert canary not in str(caught.value)
    assert "legion-auth setup" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_bearer_authentication_uses_explicit_username_and_current_provider_token() -> None:
    async def provider() -> str:
        return "current-synthetic-token"

    config = _config().model_copy(
        update={
            "auth": BearerTokenAuth(
                username="deployment-identity",
                token_provider=provider,
            )
        }
    )
    transport = MQTTTransport(config)

    assert await transport._authentication() == (
        "deployment-identity",
        "current-synthetic-token",
    )


@pytest.mark.asyncio
async def test_bearer_credentials_provider_resolves_one_consistent_generation() -> None:
    calls = 0

    async def provider() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return f"integration-{calls}", f"token-{calls}"

    config = _config().model_copy(update={"auth": BearerTokenAuth(credentials_provider=provider)})
    transport = MQTTTransport(config)

    assert await transport._authentication() == ("integration-1", "token-1")
    assert await transport._authentication() == ("integration-2", "token-2")
    assert calls == 2


@pytest.mark.asyncio
async def test_async_callable_credential_provider_is_accepted() -> None:
    class Provider:
        async def __call__(self) -> tuple[str, str]:
            return "integration-1", "token-1"

    config = _config().model_copy(update={"auth": BearerTokenAuth(credentials_provider=Provider())})

    assert await MQTTTransport(config)._authentication() == ("integration-1", "token-1")


@pytest.mark.asyncio
async def test_bearer_authentication_uses_integration_name_for_loopback_mock() -> None:
    transport = MQTTTransport(_config())

    assert await transport._authentication() == ("vendor-a", "synthetic-token")


class _EndMessages:
    pass


_END = _EndMessages()


class _MessageStream:
    def __init__(self, queue: asyncio.Queue[object]) -> None:
        self._queue = queue
        self.dequeued = asyncio.Event()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        message = await self._queue.get()
        self.dequeued.set()
        if message is _END:
            raise StopAsyncIteration
        return message


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = aiomqtt.Topic(topic)
        self.payload = payload


class _FakeClient:
    instances: ClassVar[list[_FakeClient]] = []
    second_connected: ClassVar[asyncio.Event]

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.messages = _MessageStream(self.queue)
        self.subscriptions: list[tuple[str, int]] = []
        self.unsubscriptions: list[str] = []
        self.publications: list[tuple[str, bytes, int]] = []
        type(self).instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        if len(type(self).instances) >= 2:
            type(self).second_connected.set()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int, **_kwargs: Any) -> list[int]:
        self.subscriptions.append((topic, qos))
        return [qos]

    async def unsubscribe(self, topic: str, **_kwargs: Any) -> None:
        self.unsubscriptions.append(topic)

    async def publish(self, topic: str, payload: bytes, qos: int, **_kwargs: Any) -> None:
        self.publications.append((topic, payload, qos))

    async def publish_with_completion(
        self, topic: str, payload: bytes, qos: int, **kwargs: Any
    ) -> _PublishCompletion:
        await self.publish(topic, payload, qos, **kwargs)
        return _PublishCompletion.COMPLETED


@pytest.mark.asyncio
async def test_no_auth_attempt_uses_original_host_without_credentials_and_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    transport = MQTTTransport(_no_auth_config())

    await transport.start()

    kwargs = _FakeClient.instances[0].kwargs
    assert kwargs["hostname"] == "mqtt-container.example"
    assert kwargs["username"] is None
    assert kwargs["password"] is None
    assert kwargs["require_reviewed_network"] is True
    assert kwargs["clean_start"] is True
    assert kwargs["properties"].SessionExpiryInterval == 0
    await transport.close()


@pytest.mark.asyncio
async def test_start_preserves_initial_transport_boundary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary_error = TransportBoundaryError(
        "reviewed container-network boundary check refused configured endpoint",
        operation="mqtt.resolve",
    )

    class _BoundaryClient(_FakeClient):
        async def __aenter__(self) -> Self:
            raise boundary_error

    _BoundaryClient.instances = []
    _BoundaryClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _BoundaryClient)
    transport = MQTTTransport(_no_auth_config())

    with pytest.raises(TransportBoundaryError) as caught:
        await asyncio.wait_for(transport.start(), timeout=1)

    assert caught.value is boundary_error


@pytest.mark.asyncio
async def test_reviewed_boundary_failure_after_readiness_uses_policy_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary_error = TransportBoundaryError(
        "reviewed container-network boundary check refused configured endpoint",
        operation="mqtt.resolve",
    )

    class _RecoveringBoundaryClient(_FakeClient):
        entries = 0
        recovered: ClassVar[asyncio.Event]

        async def __aenter__(self) -> Self:
            type(self).entries += 1
            if type(self).entries == 2:
                raise boundary_error
            if type(self).entries == 3:
                type(self).recovered.set()
            return self

    _RecoveringBoundaryClient.instances = []
    _RecoveringBoundaryClient.second_connected = asyncio.Event()
    _RecoveringBoundaryClient.entries = 0
    _RecoveringBoundaryClient.recovered = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _RecoveringBoundaryClient,
    )
    transport = MQTTTransport(_no_auth_config())
    await transport.start()

    await _RecoveringBoundaryClient.instances[0].queue.put(_END)
    await asyncio.wait_for(_RecoveringBoundaryClient.recovered.wait(), timeout=1)

    assert _RecoveringBoundaryClient.entries == 3
    assert transport.connected
    assert transport._last_error is None
    assert all(
        client.kwargs["require_reviewed_network"] is True
        for client in _RecoveringBoundaryClient.instances
    )
    await transport.close()


class _StalledAttemptPhaseClient(_FakeClient):
    phase = ConnectionFailureOperation.CONNECT
    phase_started: ClassVar[asyncio.Event]

    async def __aenter__(self) -> _StalledAttemptPhaseClient:
        callback = self.kwargs["on_connection_phase"]
        assert callable(callback)
        callback(type(self).phase)
        type(self).phase_started.set()
        await asyncio.Event().wait()
        raise AssertionError("stalled connection phase unexpectedly resumed")


class _RejectedClient(_FakeClient):
    async def __aenter__(self) -> _RejectedClient:
        raise MqttConnectError(0x86)


class _ReasonRejectClient(_FakeClient):
    reason_code = 0x80

    async def __aenter__(self) -> _ReasonRejectClient:
        raise MqttConnectError(type(self).reason_code)


class _UnavailableClient(_FakeClient):
    async def __aenter__(self) -> _UnavailableClient:
        raise MqttConnectError(0x88)


class _ExhaustThenRecoverClient(_FakeClient):
    entries = 0
    recovered: ClassVar[asyncio.Event]

    async def __aenter__(self) -> _ExhaustThenRecoverClient:
        type(self).entries += 1
        if type(self).entries == 2:
            raise MqttConnectError(0x88)
        if type(self).entries == 3:
            type(self).recovered.set()
        return self


class _ServerReferenceClient(_FakeClient):
    async def __aenter__(self) -> _ServerReferenceClient:
        raise MqttConnectError(0x9C)


class _PostReadyServerReferenceClient(_FakeClient):
    pass


class _TLSVerificationRejectClient(_FakeClient):
    async def __aenter__(self) -> _TLSVerificationRejectClient:
        raise ssl.SSLCertVerificationError(1, "synthetic peer verification detail")


class _TLSVerificationAfterReadyClient(_FakeClient):
    entries = 0

    async def __aenter__(self) -> _TLSVerificationAfterReadyClient:
        type(self).entries += 1
        if type(self).entries > 1:
            raise ssl.SSLCertVerificationError(1, "synthetic rotated peer detail")
        return self


class _ProtocolFailureClient(_FakeClient):
    async def __aenter__(self) -> _ProtocolFailureClient:
        raise ProtocolError(
            "synthetic connection-local protocol failure",
            operation="mqtt.receive",
            details={"packet_type": "DISCONNECT", "classifier": "synthetic"},
        )


class _PostReadyProtocolFailureClient(_FakeClient):
    connected_clients: ClassVar[asyncio.Queue[_PostReadyProtocolFailureClient]]
    protocol_failure_error: ProtocolError | None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.protocol_failure_error = None

    async def __aenter__(self) -> _PostReadyProtocolFailureClient:
        await super().__aenter__()
        type(self).connected_clients.put_nowait(self)
        return self


class _AlternatingProtocolFailureClient(_FakeClient):
    entries = 0

    async def __aenter__(self) -> _AlternatingProtocolFailureClient:
        type(self).entries += 1
        raise ProtocolError(
            "synthetic alternating protocol failure",
            operation="mqtt.receive",
            details={
                "packet_type": "DISCONNECT",
                "classifier": "first" if type(self).entries % 2 else "second",
            },
        )


class _BlockedRestoreClient(_FakeClient):
    restore_started: ClassVar[asyncio.Event]
    allow_restore: ClassVar[asyncio.Event]

    async def subscribe(self, topic: str, qos: int, **_kwargs: Any) -> list[int]:
        if len(type(self).instances) >= 2:
            type(self).restore_started.set()
            await type(self).allow_restore.wait()
        return await super().subscribe(topic, qos)


class _CancellationResistantRestoreClient(_FakeClient):
    restore_started: ClassVar[asyncio.Event]
    restore_cancelled: ClassVar[asyncio.Event]
    allow_restore: ClassVar[asyncio.Event]

    async def subscribe(self, topic: str, qos: int, **_kwargs: Any) -> list[int]:
        if len(type(self).instances) >= 2:
            type(self).restore_started.set()
            try:
                await type(self).allow_restore.wait()
            except asyncio.CancelledError:
                type(self).restore_cancelled.set()
                await type(self).allow_restore.wait()
        return await super().subscribe(topic, qos)


class _CredentialRecoveryClient(_FakeClient):
    async def __aenter__(self) -> _CredentialRecoveryClient:
        await super().__aenter__()
        return self


class _AuthenticationBudgetClient(_FakeClient):
    entries = 0
    accept_credentials = False

    async def __aenter__(self) -> _AuthenticationBudgetClient:
        type(self).entries += 1
        if type(self).entries == 1:
            return self
        if type(self).accept_credentials:
            await super().__aenter__()
            return self
        raise MqttConnectError(0x86)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class _ControlledSleeper:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_initial_negative_connack_fails_immediately_without_reconnect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _RejectedClient)
    transport = MQTTTransport(_config())

    with pytest.raises(AuthenticationError, match="rejected authentication") as caught:
        await asyncio.wait_for(transport.start(), timeout=0.5)

    assert caught.value.operation == "mqtt.start"
    assert not transport.connected


@pytest.mark.parametrize(
    "verification_detail",
    [
        "untrusted issuer",
        "certificate expired",
        "certificate not yet valid",
        "hostname mismatch",
    ],
)
def test_tls_peer_verification_is_terminal_but_transport_tls_errors_are_transient(
    verification_detail: str,
) -> None:
    transport = MQTTTransport(_config())

    peer_failure = transport._classify_failure(
        ssl.SSLCertVerificationError(1, verification_detail),
        client=None,
        was_ready=False,
    )
    transport_failure = transport._classify_failure(
        ssl.SSLError("synthetic transport failure"),
        client=None,
        was_ready=False,
    )

    assert peer_failure.code is ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED
    assert peer_failure.terminal is True
    assert transport_failure.code is ConnectionFailureCode.TLS_UNAVAILABLE
    assert transport_failure.terminal is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "failure_code", "operation", "terminal"),
    [
        (
            _DNSResolutionFailure(),
            ConnectionFailureCode.DNS_UNAVAILABLE,
            ConnectionFailureOperation.DNS,
            False,
        ),
        (
            _TCPConnectionFailure(),
            ConnectionFailureCode.TCP_UNAVAILABLE,
            ConnectionFailureOperation.TCP,
            False,
        ),
        (
            _TLSTransportFailure(),
            ConnectionFailureCode.TLS_UNAVAILABLE,
            ConnectionFailureOperation.TLS,
            False,
        ),
        (
            _TLSPeerVerificationFailure(),
            ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED,
            ConnectionFailureOperation.TLS,
            True,
        ),
    ],
)
async def test_real_aiomqtt_wrapper_preserves_connect_failure_classification(
    monkeypatch: pytest.MonkeyPatch,
    marker: aiomqtt.MqttError,
    failure_code: ConnectionFailureCode,
    operation: ConnectionFailureOperation,
    terminal: bool,
) -> None:
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)

    async def reject_connect(*_args: object, **_kwargs: object) -> socket.socket:
        raise marker

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._open_prepared_socket",
        reject_connect,
    )

    with pytest.raises(type(marker)) as caught:
        await adapter.__aenter__()

    assert not adapter._lock.locked()
    rendered = "".join(traceback.format_exception(caught.value))
    assert "localhost" not in rendered
    classification = MQTTTransport(_config())._classify_failure(
        caught.value,
        client=adapter,
        was_ready=False,
    )
    assert classification.code is failure_code
    assert classification.operation is operation
    assert classification.terminal is terminal


async def _assert_start_timeout_phase(
    transport: MQTTTransport,
    *,
    failure_code: ConnectionFailureCode,
    operation: ConnectionFailureOperation,
) -> None:
    with pytest.raises(ConnectionError) as caught:
        await asyncio.wait_for(transport.start(), timeout=1)

    assert caught.value.details == {"failure_code": failure_code.value}
    rendered = "".join(traceback.format_exception(caught.value))
    assert transport._config.host not in rendered
    assert caught.value.__cause__ is None
    assert transport.recovery_snapshot.state is ConnectionRetryState.TERMINAL
    assert transport.recovery_snapshot.failure_code is failure_code
    assert transport.recovery_snapshot.failure_operation is operation
    assert transport._supervisor is None


@pytest.mark.asyncio
async def test_outer_startup_timeout_retains_dns_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_started = asyncio.Event()
    phase_cancelled = asyncio.Event()

    async def stall_resolution(_host: str, _port: int) -> tuple[_ResolvedTCPAddress, ...]:
        phase_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            phase_cancelled.set()
        raise AssertionError("stalled DNS resolution unexpectedly resumed")

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        stall_resolution,
    )
    transport = MQTTTransport(_config().model_copy(update={"connection_timeout": 0.1}))

    await _assert_start_timeout_phase(
        transport,
        failure_code=ConnectionFailureCode.DNS_UNAVAILABLE,
        operation=ConnectionFailureOperation.DNS,
    )

    assert phase_started.is_set()
    assert phase_cancelled.is_set()


@pytest.mark.asyncio
async def test_outer_startup_timeout_retains_tcp_phase_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_started = asyncio.Event()
    observed_socket: socket.socket | None = None

    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def stall_connect(
        sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        nonlocal observed_socket
        observed_socket = sock
        phase_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        stall_connect,
    )
    transport = MQTTTransport(_config().model_copy(update={"connection_timeout": 0.1}))

    await _assert_start_timeout_phase(
        transport,
        failure_code=ConnectionFailureCode.TCP_UNAVAILABLE,
        operation=ConnectionFailureOperation.TCP,
    )

    assert phase_started.is_set()
    assert observed_socket is not None
    assert observed_socket.fileno() == -1


@pytest.mark.asyncio
async def test_outer_startup_timeout_retains_tls_handshake_phase_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_started = asyncio.Event()
    observed_socket: ssl.SSLSocket | None = None

    async def resolve(_host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("127.0.0.1", port),
            ),
        )

    async def accept_connect(
        _sock: socket.socket,
        _address: tuple[str, int] | tuple[str, int, int, int],
    ) -> None:
        return None

    async def stall_handshake(sock: ssl.SSLSocket) -> None:
        nonlocal observed_socket
        observed_socket = sock
        phase_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._resolve_tcp_addresses",
        resolve,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._connect_tcp_socket",
        accept_connect,
    )
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._perform_tls_handshake",
        stall_handshake,
    )
    config = _config().model_copy(
        update={
            "connection_timeout": 0.1,
            "tls": TLSConfig(enabled=True, verify=False),
        }
    )
    transport = MQTTTransport(config)

    await _assert_start_timeout_phase(
        transport,
        failure_code=ConnectionFailureCode.TLS_UNAVAILABLE,
        operation=ConnectionFailureOperation.TLS,
    )

    assert phase_started.is_set()
    assert observed_socket is not None
    assert observed_socket.fileno() == -1
    assert observed_socket.server_hostname == "localhost"


@pytest.mark.asyncio
async def test_outer_startup_timeout_retains_mqtt_connack_phase() -> None:
    accepted = asyncio.Event()
    writers: list[asyncio.StreamWriter] = []

    def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        accepted.set()

    server = await asyncio.start_server(accept, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    config = _config().model_copy(
        update={
            "host": "127.0.0.1",
            "mqtt_port": port,
            "connection_timeout": 0.1,
        }
    )
    transport = MQTTTransport(config)
    try:
        await _assert_start_timeout_phase(
            transport,
            failure_code=ConnectionFailureCode.BROKER_UNAVAILABLE,
            operation=ConnectionFailureOperation.CONNECT,
        )
        assert accepted.is_set()
    finally:
        await transport.close()
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_outer_startup_timeout_retains_last_scheduled_phase_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DNSUnavailableClient(_FakeClient):
        async def __aenter__(self) -> DNSUnavailableClient:
            raise _DNSResolutionFailure()

    DNSUnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        DNSUnavailableClient,
    )
    sleeper = _ControlledSleeper()
    config = _config().model_copy(
        update={
            "connection_timeout": 0.1,
            "reconnect_policy": ReconnectPolicy(
                initial_delay_seconds=1,
                maximum_delay_seconds=1,
            ),
        }
    )
    transport = MQTTTransport(config, sleeper=sleeper, random_source=lambda: 1.0)

    await _assert_start_timeout_phase(
        transport,
        failure_code=ConnectionFailureCode.DNS_UNAVAILABLE,
        operation=ConnectionFailureOperation.DNS,
    )

    assert sleeper.started.is_set()
    assert len(DNSUnavailableClient.instances) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "failure_code"),
    [
        (ConnectionFailureOperation.DNS, ConnectionFailureCode.DNS_UNAVAILABLE),
        (ConnectionFailureOperation.TCP, ConnectionFailureCode.TCP_UNAVAILABLE),
        (ConnectionFailureOperation.TLS, ConnectionFailureCode.TLS_UNAVAILABLE),
        (ConnectionFailureOperation.CONNECT, ConnectionFailureCode.BROKER_UNAVAILABLE),
    ],
)
async def test_reconnect_supervisor_retains_timed_out_connection_phase(
    monkeypatch: pytest.MonkeyPatch,
    phase: ConnectionFailureOperation,
    failure_code: ConnectionFailureCode,
) -> None:
    _StalledAttemptPhaseClient.instances = []
    _StalledAttemptPhaseClient.phase = phase
    _StalledAttemptPhaseClient.phase_started = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _StalledAttemptPhaseClient,
    )
    scheduled = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.SCHEDULED:
            scheduled.set()

    transport = MQTTTransport(
        _config().model_copy(update={"connection_timeout": 0.05}),
        on_recovery_change=observe,
    )
    transport._ever_ready = True
    supervisor = asyncio.create_task(transport._run())
    transport._supervisor = supervisor
    try:
        await asyncio.wait_for(_StalledAttemptPhaseClient.phase_started.wait(), timeout=1)
        await asyncio.wait_for(scheduled.wait(), timeout=1)

        assert transport.recovery_snapshot.failure_code is failure_code
        assert transport.recovery_snapshot.failure_operation is phase
        assert transport.recovery_snapshot.state is ConnectionRetryState.SCHEDULED
    finally:
        await transport.close()

    assert supervisor.done()
    assert not any(
        task.get_name() == "picogrid-ecn-mqtt-transport"
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_initial_tls_peer_verification_failure_is_safe_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TLSVerificationRejectClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _TLSVerificationRejectClient,
    )
    transport = MQTTTransport(_config())

    with pytest.raises(ConnectionError, match="peer certificate verification failed") as caught:
        await transport.start()

    assert "synthetic" not in str(caught.value)
    assert len(_TLSVerificationRejectClient.instances) == 1
    assert (
        transport.recovery_snapshot.failure_code
        is ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED
    )
    assert caught.value.details == {
        "failure_code": ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED.value
    }
    transport.request_retry()
    transport.notify_credentials_changed()
    await asyncio.sleep(0)
    assert len(_TLSVerificationRejectClient.instances) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "error_type"),
    [
        (0x87, AuthorizationError),
        (0x8A, AuthorizationError),
        (0x85, ConfigurationError),
        (0x8C, ConfigurationError),
        (0x80, ConnectionError),
        (0x83, ConnectionError),
        (0x97, ResourceLimitError),
        (0x81, ProtocolError),
        (0x99, ProtocolError),
    ],
)
async def test_initial_connack_rejection_raises_its_typed_public_failure(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: int,
    error_type: type[ECNClientError],
) -> None:
    _ReasonRejectClient.instances = []
    _ReasonRejectClient.reason_code = reason_code
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _ReasonRejectClient,
    )
    transport = MQTTTransport(_config())

    with pytest.raises(error_type) as caught:
        await transport.start()

    assert len(_ReasonRejectClient.instances) == 1
    if reason_code in {0x80, 0x83}:
        assert caught.value.details == {
            "failure_code": ConnectionFailureCode.CONNECTION_REJECTED.value
        }


@pytest.mark.asyncio
async def test_connack_is_reported_before_strict_restored_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _BlockedRestoreClient.instances = []
    _BlockedRestoreClient.second_connected = asyncio.Event()
    _BlockedRestoreClient.restore_started = asyncio.Event()
    _BlockedRestoreClient.allow_restore = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _BlockedRestoreClient,
    )
    state_changes: list[bool] = []
    transport = MQTTTransport(_config(), state_changes.append, random_source=lambda: 0.0)
    await transport.start()

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    await _BlockedRestoreClient.instances[0].queue.put(_END)
    await asyncio.wait_for(_BlockedRestoreClient.restore_started.wait(), timeout=1)

    assert transport.connected
    assert transport.ready is False
    assert state_changes[-2:] == [False, True]
    assert transport.recovery_snapshot.state is ConnectionRetryState.CONNECTING

    _BlockedRestoreClient.allow_restore.set()
    await asyncio.wait_for(transport._ready_event.wait(), timeout=1)
    assert transport.ready is True
    await transport.unsubscribe(handle)
    await transport.close()


@pytest.mark.asyncio
async def test_unregister_during_restore_suback_retires_filter_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _BlockedRestoreClient.instances = []
    _BlockedRestoreClient.second_connected = asyncio.Event()
    _BlockedRestoreClient.restore_started = asyncio.Event()
    _BlockedRestoreClient.allow_restore = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _BlockedRestoreClient,
    )
    transport = MQTTTransport(_config(), random_source=lambda: 0.0)
    received: list[bytes] = []

    async def callback(_topic: str, _payload: bytes) -> None:
        received.append(_payload)

    try:
        await transport.start()
        handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
        await _BlockedRestoreClient.instances[0].queue.put(_END)
        await asyncio.wait_for(_BlockedRestoreClient.restore_started.wait(), timeout=1)

        removing = asyncio.create_task(transport.unsubscribe(handle))
        await asyncio.wait_for(removing, timeout=1)
        assert handle not in transport._subscriptions
        assert transport.ready is False

        restored = _BlockedRestoreClient.instances[1]
        topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
        await restored.queue.put(_Message(topic, b"retired-owner"))
        _BlockedRestoreClient.allow_restore.set()
        await asyncio.wait_for(transport._ready_event.wait(), timeout=1)
        await asyncio.wait_for(restored.messages.dequeued.wait(), timeout=1)
        assert restored.subscriptions == [(ENTITY_JSON_SUBSCRIPTION, 1)]
        assert restored.unsubscriptions == [ENTITY_JSON_SUBSCRIPTION]
        assert transport._subscriptions == {}
        assert received == []
    finally:
        _BlockedRestoreClient.allow_restore.set()
        await transport.close()


@pytest.mark.asyncio
async def test_close_during_restore_suback_cannot_publish_restored_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CancellationResistantRestoreClient.instances = []
    _CancellationResistantRestoreClient.second_connected = asyncio.Event()
    _CancellationResistantRestoreClient.restore_started = asyncio.Event()
    _CancellationResistantRestoreClient.restore_cancelled = asyncio.Event()
    _CancellationResistantRestoreClient.allow_restore = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _CancellationResistantRestoreClient,
    )
    snapshots: list[Any] = []
    transport = MQTTTransport(
        _config(),
        on_recovery_change=snapshots.append,
        random_source=lambda: 0.0,
    )
    closing: asyncio.Task[None] | None = None

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    try:
        await transport.start()
        await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
        await _CancellationResistantRestoreClient.instances[0].queue.put(_END)
        await asyncio.wait_for(
            _CancellationResistantRestoreClient.restore_started.wait(),
            timeout=1,
        )
        restore_snapshot_index = len(snapshots)

        closing = asyncio.create_task(transport.close())
        await asyncio.wait_for(
            _CancellationResistantRestoreClient.restore_cancelled.wait(),
            timeout=1,
        )
        _CancellationResistantRestoreClient.allow_restore.set()
        await _CancellationResistantRestoreClient.instances[1].queue.put(_END)
        await asyncio.wait_for(asyncio.shield(closing), timeout=1)

        assert transport.ready is False
        assert transport._subscriptions == {}
        assert all(
            snapshot.state is not ConnectionRetryState.INACTIVE
            for snapshot in snapshots[restore_snapshot_index:]
        )
    finally:
        _CancellationResistantRestoreClient.allow_restore.set()
        if len(_CancellationResistantRestoreClient.instances) >= 2:
            _CancellationResistantRestoreClient.instances[1].queue.put_nowait(_END)
        if closing is None:
            await transport.close()
        else:
            await closing


@pytest.mark.asyncio
async def test_post_connack_restore_runs_outside_connection_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _BlockedRestoreClient.instances = []
    _BlockedRestoreClient.second_connected = asyncio.Event()
    _BlockedRestoreClient.restore_started = asyncio.Event()
    _BlockedRestoreClient.allow_restore = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _BlockedRestoreClient,
    )
    config = _config().model_copy(update={"connection_timeout": 0.01, "operation_timeout": 0.2})
    transport = MQTTTransport(config, random_source=lambda: 0.0)
    await transport.start()

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    await _BlockedRestoreClient.instances[0].queue.put(_END)
    await asyncio.wait_for(_BlockedRestoreClient.restore_started.wait(), timeout=1)
    await asyncio.sleep(0.03)

    assert transport.connected
    assert transport.ready is False
    assert transport._supervisor is not None
    assert not transport._supervisor.done()

    _BlockedRestoreClient.allow_restore.set()
    await asyncio.wait_for(transport._ready_event.wait(), timeout=1)
    await transport.unsubscribe(handle)
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), aiomqtt.MqttError("synthetic dependency timeout detail")],
)
async def test_restore_timeout_is_clipped_and_classified_at_restore_operation(
    failure: BaseException,
) -> None:
    clock = _FakeClock()
    policy = ReconnectPolicy(maximum_elapsed_seconds=1)
    config = _config().model_copy(
        update={
            "operation_timeout": 0.5,
            "reconnect_policy": policy,
        }
    )
    transport = MQTTTransport(config, monotonic=clock.monotonic)
    original = _RacingClient()
    transport._client = original  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    transport._recovery_started_at = 0.0
    clock.now = 0.75

    class TimedOutRestoreClient:
        timeout: float | None = None

        async def subscribe(
            self,
            _topic: str,
            *,
            qos: int,
            timeout: float,
        ) -> list[int]:
            del qos
            self.timeout = timeout
            raise failure

    restored = TimedOutRestoreClient()
    with pytest.raises(ConnectionError) as caught:
        await transport._restore_subscriptions(restored)  # type: ignore[arg-type]

    assert restored.timeout == pytest.approx(0.25)
    assert caught.value.operation == "mqtt.restore_subscription"
    assert "synthetic" not in str(caught.value)
    classification = transport._classify_failure(
        caught.value,
        client=None,
        was_ready=True,
    )
    assert classification.code is ConnectionFailureCode.BROKER_UNAVAILABLE
    assert classification.operation is ConnectionFailureOperation.RESTORE_SUBSCRIPTION
    assert classification.terminal is False
    await transport.close()


@pytest.mark.asyncio
async def test_no_restore_subscription_is_bound_to_one_connection_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    transport = MQTTTransport(_config(), random_source=lambda: 0.0)
    await transport.start()

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(
        ENTITY_JSON_SUBSCRIPTION,
        callback,
        restore_on_reconnect=False,
    )
    first_generation = handle.connection_generation
    generation_lost = asyncio.create_task(transport.wait_for_connection_loss(first_generation))

    assert first_generation == transport.connection_generation == 1
    await _FakeClient.instances[0].queue.put(_END)
    await asyncio.wait_for(generation_lost, timeout=1)
    await asyncio.wait_for(_FakeClient.second_connected.wait(), timeout=1)

    assert transport.connection_generation == 2
    assert handle not in transport._subscriptions
    assert _FakeClient.instances[1].subscriptions == []
    await asyncio.wait_for(
        transport.wait_for_connection_loss(first_generation),
        timeout=1,
    )
    current_generation_lost = asyncio.create_task(
        transport.wait_for_connection_loss(transport.connection_generation)
    )
    await transport.unsubscribe(handle)
    await transport.close()
    await asyncio.wait_for(current_generation_lost, timeout=1)


@pytest.mark.asyncio
async def test_locally_malformed_static_bearer_is_configuration_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    config = _config().model_copy(update={"auth": BearerTokenAuth(token=SecretStr(""))})
    transport = MQTTTransport(config)

    with pytest.raises(ConfigurationError):
        await transport.start()

    assert len(_FakeClient.instances) == 0
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.CONFIGURATION_INVALID


@pytest.mark.asyncio
async def test_hanging_dynamic_credential_provider_is_terminal_before_start_returns() -> None:
    never = asyncio.Event()

    async def credentials() -> tuple[str, str]:
        await never.wait()
        return "vendor-a", "unreachable-token"

    config = _config().model_copy(
        update={
            "auth": BearerTokenAuth(credentials_provider=credentials),
            "connection_timeout": 0.02,
        }
    )
    transport = MQTTTransport(config)

    with pytest.raises(AuthenticationError, match="unavailable or unusable"):
        await asyncio.wait_for(transport.start(), timeout=0.5)

    assert transport.recovery_snapshot.state is ConnectionRetryState.TERMINAL
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE
    assert (
        transport.recovery_snapshot.failure_operation
        is ConnectionFailureOperation.RESOLVE_CREDENTIALS
    )


@pytest.mark.asyncio
async def test_dynamic_credential_failure_is_terminal_until_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CredentialRecoveryClient.instances = []
    _CredentialRecoveryClient.second_connected = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _CredentialRecoveryClient,
    )
    credential_calls = 0

    async def credentials() -> tuple[str, str]:
        nonlocal credential_calls
        credential_calls += 1
        if credential_calls == 2:
            raise ValueError("synthetic unavailable credential")
        return "vendor-a", f"token-{credential_calls}"

    terminal = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    config = _config().model_copy(
        update={"auth": BearerTokenAuth(credentials_provider=credentials)}
    )
    transport = MQTTTransport(
        config,
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    await transport.start()
    await _CredentialRecoveryClient.instances[0].queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert credential_calls == 2
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE
    assert transport.recovery_snapshot.state is ConnectionRetryState.TERMINAL
    assert transport.recovery_snapshot.next_retry_delay_seconds is None

    transport.notify_credentials_changed()
    await asyncio.wait_for(_CredentialRecoveryClient.second_connected.wait(), timeout=1)
    assert credential_calls == 3
    assert transport.connected
    await transport.close()


@pytest.mark.asyncio
async def test_authentication_budget_exhaustion_retains_authentication_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _AuthenticationBudgetClient.instances = []
    _AuthenticationBudgetClient.entries = 0
    _AuthenticationBudgetClient.accept_credentials = False
    _AuthenticationBudgetClient.second_connected = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _AuthenticationBudgetClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.05,
        maximum_delay_seconds=0.05,
        stable_reset_seconds=60,
        maximum_attempts=3,
    )
    terminal = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    await transport.start()
    await _AuthenticationBudgetClient.instances[0].queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert _AuthenticationBudgetClient.entries == 3
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.AUTHENTICATION_REJECTED
    exhausted_supervisor = transport._supervisor
    transport.request_retry()
    await asyncio.sleep(0)
    assert transport._supervisor is exhausted_supervisor
    assert _AuthenticationBudgetClient.entries == 3

    _AuthenticationBudgetClient.accept_credentials = True
    transport.notify_credentials_changed()
    await asyncio.wait_for(_AuthenticationBudgetClient.second_connected.wait(), timeout=1)
    assert _AuthenticationBudgetClient.entries == 4
    assert transport.connected
    assert transport._supervisor is not exhausted_supervisor
    await transport.close()


@pytest.mark.asyncio
async def test_reconnect_uses_deterministic_full_jitter_until_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _UnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _UnavailableClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        multiplier=2,
        maximum_delay_seconds=4,
        stable_reset_seconds=60,
        maximum_attempts=4,
    )
    clock = _FakeClock()
    snapshots: list[Any] = []
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=snapshots.append,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_source=lambda: 0.5,
    )

    with pytest.raises(ConnectionError, match="did not become ready") as caught:
        await transport.start()

    assert len(_UnavailableClient.instances) == 4
    assert clock.delays == [0.25, 0.5, 1.0]
    assert snapshots[-1].state is ConnectionRetryState.TERMINAL
    assert snapshots[-1].failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
    assert snapshots[-1].consecutive_attempt_count == 4
    assert caught.value.details == {"failure_code": ConnectionFailureCode.RETRY_EXHAUSTED.value}


@pytest.mark.asyncio
async def test_request_retry_revives_initial_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _UnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _UnavailableClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.05,
        maximum_delay_seconds=0.05,
        stable_reset_seconds=60,
        maximum_attempts=1,
    )
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        random_source=lambda: 0.0,
    )

    try:
        with pytest.raises(ConnectionError, match="did not become ready"):
            await transport.start()

        assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
        monkeypatch.setattr(
            "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
            _FakeClient,
        )

        transport.request_retry()
        await asyncio.wait_for(transport._ready_event.wait(), timeout=1)

        assert transport.ready
        assert transport.connected
        assert transport._attempt_count == 1
    finally:
        await transport.close()


def test_reconnect_full_jitter_advances_the_retained_cap_once_per_attempt() -> None:
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        multiplier=1.000001,
        maximum_delay_seconds=30,
    )
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        random_source=lambda: 1.0,
    )

    assert transport._begin_attempt()
    assert transport._full_jitter_delay() == 0.5
    assert transport._begin_attempt()
    assert transport._full_jitter_delay() == pytest.approx(0.5000005)
    assert transport._retry_delay_cap_seconds == pytest.approx(0.5000005)


@pytest.mark.asyncio
async def test_request_retry_starts_new_episode_only_after_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ExhaustThenRecoverClient.instances = []
    _ExhaustThenRecoverClient.entries = 0
    _ExhaustThenRecoverClient.recovered = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _ExhaustThenRecoverClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.05,
        maximum_delay_seconds=0.05,
        stable_reset_seconds=60,
        maximum_attempts=2,
    )
    terminal = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED:
            terminal.set()

    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    await transport.start()
    await _ExhaustThenRecoverClient.instances[0].queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)
    exhausted_supervisor = transport._supervisor
    assert exhausted_supervisor is not None
    await asyncio.wait_for(asyncio.shield(exhausted_supervisor), timeout=1)

    transport.request_retry()
    await asyncio.wait_for(_ExhaustThenRecoverClient.recovered.wait(), timeout=1)

    assert transport.connected
    assert transport._supervisor is not exhausted_supervisor
    assert transport._attempt_count == 1
    assert transport._retry_delay_cap_seconds == policy.initial_delay_seconds
    await transport.close()


@pytest.mark.asyncio
async def test_unusable_tls_credentials_are_classified_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_credentials(*_args: Any, **_kwargs: Any) -> object:
        raise AuthenticationError(
            "synthetic unusable credential material",
            operation="mqtt.authenticate",
        )

    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt.build_lifecycle_owned_client_ssl_context",
        reject_credentials,
    )
    transport = MQTTTransport(_config())

    with pytest.raises(AuthenticationError, match="unavailable or unusable"):
        await transport.start()

    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE


@pytest.mark.asyncio
async def test_server_reference_is_terminal_and_never_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ServerReferenceClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _ServerReferenceClient,
    )
    snapshots: list[Any] = []
    transport = MQTTTransport(_config(), on_recovery_change=snapshots.append)

    with pytest.raises(ConnectionError, match="did not become ready") as caught:
        await transport.start()

    assert len(_ServerReferenceClient.instances) == 1
    assert snapshots[-1].state is ConnectionRetryState.TERMINAL
    assert snapshots[-1].failure_code is ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW
    assert caught.value.details == {
        "failure_code": ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW.value
    }


@pytest.mark.asyncio
async def test_post_ready_server_reference_is_terminal_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PostReadyServerReferenceClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _PostReadyServerReferenceClient,
    )
    terminal = asyncio.Event()
    snapshots: list[Any] = []

    def observe(snapshot: Any) -> None:
        snapshots.append(snapshot)
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    transport = MQTTTransport(_config(), on_recovery_change=observe)
    await transport.start()
    client = _PostReadyServerReferenceClient.instances[0]
    client._server_reference_present = True
    await client.queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert len(_PostReadyServerReferenceClient.instances) == 1
    assert snapshots[-1].failure_code is ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW
    assert snapshots[-1].failure_operation is ConnectionFailureOperation.CONNECT
    await transport.close()


@pytest.mark.asyncio
async def test_three_identical_protocol_failures_are_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ProtocolFailureClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _ProtocolFailureClient,
    )
    snapshots: list[Any] = []
    clock = _FakeClock()
    transport = MQTTTransport(
        _config(),
        on_recovery_change=snapshots.append,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_source=lambda: 0.0,
    )

    with pytest.raises(ProtocolError, match="protocol validation"):
        await transport.start()

    assert len(_ProtocolFailureClient.instances) == 3
    assert snapshots[-1].failure_code is ConnectionFailureCode.PROTOCOL_FAILURE
    assert snapshots[-1].state is ConnectionRetryState.TERMINAL


@pytest.mark.asyncio
async def test_strict_readiness_resets_post_ready_protocol_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PostReadyProtocolFailureClient.instances = []
    _PostReadyProtocolFailureClient.second_connected = asyncio.Event()
    _PostReadyProtocolFailureClient.connected_clients = asyncio.Queue()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _PostReadyProtocolFailureClient,
    )
    snapshots: list[Any] = []

    policy = ReconnectPolicy(
        initial_delay_seconds=0.05,
        maximum_delay_seconds=0.05,
        stable_reset_seconds=60,
        maximum_attempts=4,
    )
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=snapshots.append,
        random_source=lambda: 0.0,
    )

    await transport.start()
    for _ in range(3):
        client = await asyncio.wait_for(
            _PostReadyProtocolFailureClient.connected_clients.get(),
            timeout=1,
        )
        client.protocol_failure_error = ProtocolError(
            "synthetic post-ready protocol failure",
            operation="mqtt.publish",
            details={"packet_type": "PUBACK", "classifier": "malformed_properties"},
        )
        client.queue.put_nowait(_END)
    await asyncio.wait_for(
        _PostReadyProtocolFailureClient.connected_clients.get(),
        timeout=1,
    )

    assert len(_PostReadyProtocolFailureClient.instances) == 4
    assert transport.ready
    assert transport._protocol_failure_signature is None
    assert transport._protocol_failure_count == 0
    assert all(snapshot.state is not ConnectionRetryState.TERMINAL for snapshot in snapshots)
    await transport.close()


@pytest.mark.asyncio
async def test_different_protocol_failures_do_not_share_the_terminal_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _AlternatingProtocolFailureClient.instances = []
    _AlternatingProtocolFailureClient.entries = 0
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _AlternatingProtocolFailureClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.05,
        maximum_delay_seconds=0.05,
        stable_reset_seconds=60,
        maximum_attempts=4,
    )
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        random_source=lambda: 0.0,
    )

    with pytest.raises(ProtocolError):
        await transport.start()

    assert len(_AlternatingProtocolFailureClient.instances) == 4
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.PROTOCOL_FAILURE


@pytest.mark.asyncio
async def test_reconnect_elapsed_budget_caps_final_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _UnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _UnavailableClient,
    )
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        multiplier=2,
        maximum_delay_seconds=4,
        stable_reset_seconds=60,
        maximum_elapsed_seconds=0.6,
    )
    clock = _FakeClock()
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_source=lambda: 1.0,
    )

    with pytest.raises(ConnectionError, match="did not become ready"):
        await transport.start()

    assert len(_UnavailableClient.instances) == 2
    assert clock.delays == pytest.approx([0.5, 0.1])
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED


def test_reconnect_episode_resets_only_after_stable_ready_interval() -> None:
    clock = _FakeClock()
    policy = ReconnectPolicy(stable_reset_seconds=60)
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        monotonic=clock.monotonic,
    )

    assert transport._begin_attempt()
    transport._mark_ready()
    clock.now = 59.9
    transport._finish_ready_period()
    assert transport._attempt_count == 1

    transport._mark_ready()
    clock.now = 120.0
    transport._finish_ready_period()
    assert transport._attempt_count == 0
    assert transport._recovery_started_at is None
    assert transport._retry_delay_cap_seconds is None


@pytest.mark.asyncio
async def test_stable_ready_status_resets_without_waiting_for_a_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    clock = _FakeClock()
    stable_sleeper = _ControlledSleeper()
    reset = asyncio.Event()
    snapshots: list[Any] = []

    def observe(snapshot: Any) -> None:
        snapshots.append(snapshot)
        if (
            snapshot.state is ConnectionRetryState.INACTIVE
            and snapshot.consecutive_attempt_count == 0
        ):
            reset.set()

    policy = ReconnectPolicy(stable_reset_seconds=60)
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=observe,
        monotonic=clock.monotonic,
        stable_sleeper=stable_sleeper,
    )
    await transport.start()
    await asyncio.wait_for(stable_sleeper.started.wait(), timeout=1)

    assert transport.ready
    assert transport.recovery_snapshot.consecutive_attempt_count == 1
    clock.now = 60
    stable_sleeper.release.set()
    await asyncio.wait_for(reset.wait(), timeout=1)

    assert transport.ready
    assert transport.recovery_snapshot.consecutive_attempt_count == 0
    assert transport._attempt_count == 0
    assert transport._retry_delay_cap_seconds is None
    assert stable_sleeper.delays == [60]
    await transport.close()
    assert transport._stable_reset_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready_duration", "expected_delays"),
    [
        (59.9, [0.5]),
        (60.0, []),
    ],
)
async def test_stable_ready_reset_controls_whether_reconnect_attempt_one_is_immediate(
    monkeypatch: pytest.MonkeyPatch,
    ready_duration: float,
    expected_delays: list[float],
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    clock = _FakeClock()
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        maximum_delay_seconds=0.5,
        stable_reset_seconds=60,
    )
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_source=lambda: 1.0,
    )

    await transport.start()
    clock.now = ready_duration
    await _FakeClient.instances[0].queue.put(_END)
    await asyncio.wait_for(_FakeClient.second_connected.wait(), timeout=1)

    assert clock.delays == expected_delays
    assert transport._attempt_count == (2 if expected_delays else 1)
    await transport.close()


class _AuthenticationSequenceClient(_FakeClient):
    entries = 0
    fourth_connected: ClassVar[asyncio.Event]

    async def __aenter__(self) -> _AuthenticationSequenceClient:
        type(self).entries += 1
        entry = type(self).entries
        if entry == 1:
            await self.queue.put(_END)
            return self
        if entry in {2, 3}:
            raise MqttConnectError(0x86)
        type(self).fourth_connected.set()
        return self


@pytest.mark.asyncio
async def test_post_ready_authentication_gets_one_immediate_fresh_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _AuthenticationSequenceClient.instances = []
    _AuthenticationSequenceClient.entries = 0
    _AuthenticationSequenceClient.fourth_connected = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _AuthenticationSequenceClient,
    )
    credential_calls = 0

    async def credentials() -> tuple[str, str]:
        nonlocal credential_calls
        credential_calls += 1
        return f"integration-{credential_calls}", f"token-{credential_calls}"

    policy = ReconnectPolicy(
        initial_delay_seconds=0.1,
        multiplier=2,
        maximum_delay_seconds=1,
        stable_reset_seconds=60,
    )
    clock = _FakeClock()
    config = _config().model_copy(
        update={
            "auth": BearerTokenAuth(credentials_provider=credentials),
            "reconnect_policy": policy,
        }
    )
    transport = MQTTTransport(
        config,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        random_source=lambda: 1.0,
    )

    await transport.start()
    await asyncio.wait_for(
        _AuthenticationSequenceClient.fourth_connected.wait(),
        timeout=1,
    )

    assert credential_calls == 4
    assert [client.kwargs["password"] for client in _AuthenticationSequenceClient.instances] == [
        "token-1",
        "token-2",
        "token-3",
        "token-4",
    ]
    # Connection loss is delayed, the first rejected fresh credential is not,
    # and the second rejection enters jittered credential backoff.
    assert clock.delays == pytest.approx([0.1, 0.4])
    await transport.close()


@pytest.mark.asyncio
async def test_tls_material_is_rebuilt_and_removed_for_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    attempt_files: list[Any] = []
    attempt_directories: list[Path] = []

    event_loop_thread = threading.get_ident()

    def build_attempt_context(
        _tls: object,
        auth: object,
        storage: Any,
        *,
        _prepared_material: Any,
        **_kwargs: Any,
    ) -> object:
        assert threading.get_ident() == event_loop_thread
        assert isinstance(auth, MTLSAuth)
        storage._write_bytes(
            "client-certificate.pem",
            _prepared_material.client_certificate,
        )
        storage._write_bytes("client-key.pem", _prepared_material.client_key)
        assert storage.directory is not None
        attempt_files.append(storage)
        attempt_directories.append(storage.directory)
        return object()

    monkeypatch.setattr(
        credential_module,
        "build_client_ssl_context",
        build_attempt_context,
    )
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(data=SecretStr("synthetic certificate")),
        client_key=PrivateKeyMaterial(data=SecretStr("synthetic key")),
    )
    config = _config().model_copy(
        update={
            "auth": auth,
            "tls": TLSConfig(enabled=True, verify=False),
        }
    )
    transport = MQTTTransport(config, random_source=lambda: 0.0)

    try:
        await transport.start()
        await _FakeClient.instances[0].queue.put(_END)
        await asyncio.wait_for(_FakeClient.second_connected.wait(), timeout=1)

        assert len(attempt_files) == 2
        assert all(storage._closed for storage in attempt_files)
        assert not any(directory.exists() for directory in attempt_directories)
    finally:
        await transport.close()
    assert all(storage._closed for storage in attempt_files)


def _stall_tls_material_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[asyncio.subprocess.Process], asyncio.Event]:
    processes: list[asyncio.subprocess.Process] = []
    process_started = asyncio.Event()
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def recording_create_subprocess_exec(
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.subprocess.Process:
        process = await create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        process_started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_create_subprocess_exec)
    monkeypatch.setattr(
        credential_module,
        "_TLS_MATERIAL_READER_CODE",
        "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
    )
    return processes, process_started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("material_kind", "error_type", "failure_code", "failure_operation"),
    [
        (
            "mtls",
            ConfigurationError,
            ConnectionFailureCode.CONFIGURATION_INVALID,
            ConnectionFailureOperation.RESOLVE_CREDENTIALS,
        ),
        (
            "mtls_with_ca",
            ConfigurationError,
            ConnectionFailureCode.CONFIGURATION_INVALID,
            ConnectionFailureOperation.RESOLVE_CREDENTIALS,
        ),
        (
            "ca",
            ConfigurationError,
            ConnectionFailureCode.CONFIGURATION_INVALID,
            ConnectionFailureOperation.RESOLVE_CREDENTIALS,
        ),
    ],
)
async def test_tls_material_loading_timeout_is_terminal_cooperative_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    material_kind: str,
    error_type: type[ECNClientError],
    failure_code: ConnectionFailureCode,
    failure_operation: ConnectionFailureOperation,
) -> None:
    processes, process_started = _stall_tls_material_reader(monkeypatch)
    sensitive_paths: tuple[Path, ...]
    sensitive_values: tuple[str, ...] = ()
    updates: dict[str, Any] = {
        "connection_timeout": 1.0,
        "shutdown_timeout": 0.05,
    }
    if material_kind == "mtls":
        certificate = tmp_path / "sensitive-client-certificate-canary.pem"
        private_key = tmp_path / "sensitive-client-key-canary.pem"
        sensitive_paths = (certificate, private_key)
        updates.update(
            auth=MTLSAuth(
                client_certificate=CertificateMaterial(path=certificate),
                client_key=PrivateKeyMaterial(path=private_key),
            ),
            tls=TLSConfig(enabled=True, verify=False),
        )
    elif material_kind == "mtls_with_ca":
        ca_certificate = tmp_path / "sensitive-ca-path-canary.pem"
        client_certificate = "sensitive-inline-client-certificate-canary"
        client_key = "sensitive-inline-client-key-canary"
        sensitive_paths = (ca_certificate,)
        sensitive_values = (client_certificate, client_key)
        updates.update(
            auth=MTLSAuth(
                client_certificate=CertificateMaterial(data=SecretStr(client_certificate)),
                client_key=PrivateKeyMaterial(data=SecretStr(client_key)),
            ),
            tls=TLSConfig(
                enabled=True,
                verify=True,
                ca_certificate=CertificateMaterial(path=ca_certificate),
            ),
        )
    else:
        ca_certificate = tmp_path / "sensitive-ca-path-canary.pem"
        sensitive_paths = (ca_certificate,)
        updates["tls"] = TLSConfig(
            enabled=True,
            verify=True,
            ca_certificate=CertificateMaterial(path=ca_certificate),
        )
    config = _config().model_copy(update=updates)
    transport = MQTTTransport(config)
    starting = asyncio.create_task(transport.start())

    try:
        await asyncio.wait_for(process_started.wait(), timeout=2)
        loop_tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_tick.set)
        await asyncio.wait_for(loop_tick.wait(), timeout=0.1)
        with pytest.raises(error_type) as caught:
            await asyncio.wait_for(starting, timeout=2)
        assert transport.recovery_snapshot.failure_code is failure_code
        assert transport.recovery_snapshot.failure_operation is failure_operation
        assert transport.recovery_snapshot.consecutive_attempt_count == 1
        rendered = "".join(traceback.format_exception(caught.value))
        assert all(str(path) not in rendered for path in sensitive_paths)
        assert all(value not in rendered for value in sensitive_values)
    finally:
        await transport.close()

    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert not any(
        task.get_name().startswith("picogrid-ecn-tls-material")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_post_ready_mtls_material_timeout_is_terminal_without_automatic_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    reader_calls = 0
    second_read_started = asyncio.Event()
    second_read_cancelled = asyncio.Event()

    async def read_material(
        paths: Any,
    ) -> dict[str, bytes]:
        nonlocal reader_calls
        reader_calls += 1
        if reader_calls == 1:
            return {role: f"synthetic-{role}".encode() for role in paths}
        second_read_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            second_read_cancelled.set()
        raise AssertionError("cancelled material read unexpectedly resumed")

    def build_attempt_context(*_args: Any, **_kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(credential_module, "_read_tls_paths_in_subprocess", read_material)
    monkeypatch.setattr(credential_module, "build_client_ssl_context", build_attempt_context)
    certificate = tmp_path / "sensitive-client-certificate-canary.pem"
    private_key = tmp_path / "sensitive-client-key-canary.pem"
    config = _config().model_copy(
        update={
            "auth": MTLSAuth(
                client_certificate=CertificateMaterial(path=certificate),
                client_key=PrivateKeyMaterial(path=private_key),
            ),
            "tls": TLSConfig(enabled=True, verify=False),
            "connection_timeout": 0.05,
        }
    )
    terminal = asyncio.Event()
    scheduled_states = 0

    def observe(snapshot: Any) -> None:
        nonlocal scheduled_states
        if snapshot.state is ConnectionRetryState.SCHEDULED:
            scheduled_states += 1
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    transport = MQTTTransport(
        config,
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    try:
        await transport.start()
        await _FakeClient.instances[0].queue.put(_END)
        await asyncio.wait_for(second_read_started.wait(), timeout=1)
        scheduled_before_timeout = scheduled_states
        await asyncio.wait_for(terminal.wait(), timeout=1)
        await asyncio.sleep(0)

        assert reader_calls == 2
        assert len(_FakeClient.instances) == 1
        assert scheduled_states == scheduled_before_timeout
        assert second_read_cancelled.is_set()
        assert (
            transport.recovery_snapshot.failure_code is ConnectionFailureCode.CONFIGURATION_INVALID
        )
        assert (
            transport.recovery_snapshot.failure_operation
            is ConnectionFailureOperation.RESOLVE_CREDENTIALS
        )
        assert transport._last_error is not None
        rendered = "".join(traceback.format_exception(transport._last_error))
        assert str(certificate) not in rendered
        assert str(private_key) not in rendered

        transport.request_retry()
        await asyncio.sleep(0)
        assert reader_calls == 2
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_close_interrupts_stalled_tls_material_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes, process_started = _stall_tls_material_reader(monkeypatch)
    certificate = tmp_path / "credential-certificate.pem"
    private_key = tmp_path / "credential-key.pem"
    config = _config().model_copy(
        update={
            "auth": MTLSAuth(
                client_certificate=CertificateMaterial(path=certificate),
                client_key=PrivateKeyMaterial(path=private_key),
            ),
            "tls": TLSConfig(enabled=True, verify=False),
            "connection_timeout": 2.0,
            "shutdown_timeout": 0.05,
        }
    )
    transport = MQTTTransport(config)
    starting = asyncio.create_task(transport.start())

    await asyncio.wait_for(process_started.wait(), timeout=1)
    await asyncio.wait_for(transport.close(), timeout=0.5)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(starting, timeout=0.2)

    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert transport._supervisor is None
    assert not any(
        task.get_name().startswith("picogrid-ecn-tls-material")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_reconnect_reloads_rotated_ca_certificate_and_key_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    ca_certificate = tmp_path / "ca.pem"
    client_certificate = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    for path, value in (
        (ca_certificate, "ca-generation-1"),
        (client_certificate, "certificate-generation-1"),
        (client_key, "key-generation-1"),
    ):
        path.write_text(value, encoding="utf-8")
    observed_material: list[tuple[str, str, str]] = []
    attempt_files: list[Any] = []

    def capture_context(
        tls: object,
        auth: object,
        storage: Any,
        *,
        _prepared_material: Any,
        **_kwargs: Any,
    ) -> object:
        assert isinstance(tls, TLSConfig)
        assert isinstance(auth, MTLSAuth)
        attempt_files.append(storage)
        observed_material.append(
            (
                _prepared_material.ca.decode("utf-8"),
                _prepared_material.client_certificate.decode("utf-8"),
                _prepared_material.client_key.decode("utf-8"),
            )
        )
        return object()

    monkeypatch.setattr(
        credential_module,
        "build_client_ssl_context",
        capture_context,
    )
    config = _config().model_copy(
        update={
            "auth": MTLSAuth(
                client_certificate=CertificateMaterial(path=client_certificate),
                client_key=PrivateKeyMaterial(path=client_key),
            ),
            "tls": TLSConfig(
                enabled=True,
                verify=True,
                ca_certificate=CertificateMaterial(path=ca_certificate),
            ),
        }
    )
    transport = MQTTTransport(config, random_source=lambda: 0.0)

    try:
        await transport.start()
        for path, value in (
            (ca_certificate, "ca-generation-2"),
            (client_certificate, "certificate-generation-2"),
            (client_key, "key-generation-2"),
        ):
            path.write_text(value, encoding="utf-8")
        await _FakeClient.instances[0].queue.put(_END)
        await asyncio.wait_for(_FakeClient.second_connected.wait(), timeout=1)

        assert observed_material == [
            ("ca-generation-1", "certificate-generation-1", "key-generation-1"),
            ("ca-generation-2", "certificate-generation-2", "key-generation-2"),
        ]
        assert len(attempt_files) == 2
        assert all(storage._closed for storage in attempt_files)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_post_ready_tls_peer_verification_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TLSVerificationAfterReadyClient.instances = []
    _TLSVerificationAfterReadyClient.entries = 0
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _TLSVerificationAfterReadyClient,
    )
    terminal = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    transport = MQTTTransport(
        _config(),
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    await transport.start()
    await _TLSVerificationAfterReadyClient.instances[0].queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert len(_TLSVerificationAfterReadyClient.instances) == 2
    assert (
        transport.recovery_snapshot.failure_code
        is ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED
    )
    transport.request_retry()
    transport.notify_credentials_changed()
    await asyncio.sleep(0)
    assert len(_TLSVerificationAfterReadyClient.instances) == 2
    await transport.close()


@pytest.mark.asyncio
async def test_broken_mtls_rotation_is_terminal_until_atomic_repair_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    attempt_files: list[Any] = []
    build_calls = 0

    def build_attempt_context(
        _tls: object,
        _auth: object,
        storage: Any,
        **_kwargs: Any,
    ) -> object:
        nonlocal build_calls
        build_calls += 1
        attempt_files.append(storage)
        if build_calls == 2:
            raise AuthenticationError(
                "synthetic rotated certificate mismatch",
                operation="mqtt.authenticate",
            )
        return object()

    monkeypatch.setattr(
        credential_module,
        "build_client_ssl_context",
        build_attempt_context,
    )
    terminal = asyncio.Event()

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.TERMINAL:
            terminal.set()

    config = _config().model_copy(
        update={
            "auth": MTLSAuth(
                client_certificate=CertificateMaterial(data=SecretStr("synthetic certificate")),
                client_key=PrivateKeyMaterial(data=SecretStr("synthetic key")),
            ),
            "tls": TLSConfig(enabled=True, verify=False),
        }
    )
    transport = MQTTTransport(
        config,
        on_recovery_change=observe,
        random_source=lambda: 0.0,
    )
    await transport.start()
    await _FakeClient.instances[0].queue.put(_END)
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert build_calls == 2
    assert transport.recovery_snapshot.failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE
    assert all(storage._closed for storage in attempt_files)

    transport.notify_credentials_changed()
    await asyncio.wait_for(_FakeClient.second_connected.wait(), timeout=1)
    assert build_calls == 3
    assert transport.connected
    await transport.close()
    assert all(storage._closed for storage in attempt_files)


class _BlockingSleeper:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.calls = 0

    async def __call__(self, _delay: float) -> None:
        self.calls += 1
        self.started.set()
        if self.calls >= 2:
            self.second_started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_retry_request_from_scheduled_callback_wakes_the_same_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _UnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _UnavailableClient,
    )
    sleeper = _BlockingSleeper()
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        maximum_delay_seconds=0.5,
        stable_reset_seconds=60,
        maximum_attempts=2,
    )
    transport: MQTTTransport

    def observe(snapshot: Any) -> None:
        if snapshot.state is ConnectionRetryState.SCHEDULED:
            transport.request_retry()

    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        on_recovery_change=observe,
        sleeper=sleeper,
        random_source=lambda: 1.0,
    )

    with pytest.raises(ConnectionError):
        await transport.start()

    assert len(_UnavailableClient.instances) == 2
    assert sleeper.calls == 0


@pytest.mark.asyncio
async def test_generic_retry_request_does_not_wake_credential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _AuthenticationBudgetClient.instances = []
    _AuthenticationBudgetClient.entries = 0
    _AuthenticationBudgetClient.accept_credentials = False
    _AuthenticationBudgetClient.second_connected = asyncio.Event()
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _AuthenticationBudgetClient,
    )
    sleeper = _BlockingSleeper()
    policy = ReconnectPolicy(
        initial_delay_seconds=0.5,
        maximum_delay_seconds=0.5,
        stable_reset_seconds=60,
    )
    jitter_samples = iter((0.0, 1.0))
    transport = MQTTTransport(
        _config().model_copy(update={"reconnect_policy": policy}),
        sleeper=sleeper,
        random_source=lambda: next(jitter_samples),
    )
    await transport.start()
    await _AuthenticationBudgetClient.instances[0].queue.put(_END)
    await asyncio.wait_for(sleeper.started.wait(), timeout=1)

    assert transport.recovery_snapshot.state is ConnectionRetryState.WAITING_FOR_CREDENTIALS
    assert _AuthenticationBudgetClient.entries == 3
    transport.request_retry()
    await asyncio.sleep(0)
    assert _AuthenticationBudgetClient.entries == 3

    _AuthenticationBudgetClient.accept_credentials = True
    transport.notify_credentials_changed()
    await asyncio.wait_for(_AuthenticationBudgetClient.second_connected.wait(), timeout=1)
    assert _AuthenticationBudgetClient.entries == 4
    await transport.close()


@pytest.mark.asyncio
async def test_retry_wakeup_reuses_supervisor_and_close_interrupts_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _UnavailableClient.instances = []
    monkeypatch.setattr(
        "picogrid_ecn_client._transport.mqtt._MQTTV5Client",
        _UnavailableClient,
    )
    sleeper = _BlockingSleeper()
    transport = MQTTTransport(
        _config(),
        sleeper=sleeper,
        random_source=lambda: 1.0,
    )
    starting = asyncio.create_task(transport.start())
    await asyncio.wait_for(sleeper.started.wait(), timeout=1)
    supervisor = transport._supervisor

    transport.request_retry()
    await asyncio.wait_for(sleeper.second_started.wait(), timeout=1)
    assert len(_UnavailableClient.instances) == 2
    assert transport._supervisor is supervisor

    await asyncio.wait_for(transport.close(), timeout=1)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(starting, timeout=1)
    assert transport._supervisor is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("picogrid-ecn-mqtt-retry-")
    ]


class _RacingClient:
    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0
        self.session_invalidations = 0
        self.publish_calls = 0
        self.subscribe_started = asyncio.Event()
        self.unsubscribe_started = asyncio.Event()
        self.publish_started = asyncio.Event()
        self.allow_subscribe = asyncio.Event()
        self.allow_unsubscribe = asyncio.Event()
        self.allow_publish = asyncio.Event()
        self.fail_first_subscribe = False
        self.subscribe_exception: Exception | None = None
        self.fail_unsubscribe = False
        self.unsubscribe_exception: Exception | None = None
        self.block_subscribe = False
        self.block_unsubscribe = False
        self.block_publish = False
        self.resist_subscribe_cancellation = False
        self.subscribe_cancelled = asyncio.Event()
        self.subscribe_result: object = [1]

    async def subscribe(self, _topic: str, **_kwargs: Any) -> Any:
        self.subscribe_calls += 1
        call = self.subscribe_calls
        self.subscribe_started.set()
        if self.block_subscribe:
            try:
                await self.allow_subscribe.wait()
            except asyncio.CancelledError:
                self.subscribe_cancelled.set()
                if not self.resist_subscribe_cancellation:
                    raise
                await self.allow_subscribe.wait()
        if self.fail_first_subscribe and call == 1:
            raise OSError("synthetic first subscribe failure")
        if self.subscribe_exception is not None:
            raise self.subscribe_exception
        return self.subscribe_result

    async def unsubscribe(self, _topic: str, **_kwargs: Any) -> None:
        self.unsubscribe_calls += 1
        self.unsubscribe_started.set()
        if self.block_unsubscribe:
            await self.allow_unsubscribe.wait()
        if self.unsubscribe_exception is not None:
            raise self.unsubscribe_exception
        if self.fail_unsubscribe:
            raise OSError("synthetic unsubscribe failure")

    async def publish(self, _topic: str, _payload: bytes, **kwargs: Any) -> None:
        self.publish_calls += 1
        self.publish_started.set()
        on_send_started = kwargs.get("on_send_started")
        if on_send_started is not None:
            on_send_started()
        if self.block_publish:
            await self.allow_publish.wait()

    async def publish_with_completion(
        self, topic: str, payload: bytes, **kwargs: Any
    ) -> _PublishCompletion:
        await self.publish(topic, payload, **kwargs)
        return _PublishCompletion.COMPLETED

    async def invalidate_session(self, **_kwargs: Any) -> None:
        self.session_invalidations += 1


class _RestoreClient(_RacingClient):
    def __init__(self, outcomes: dict[str, object]) -> None:
        super().__init__()
        self.outcomes = outcomes
        self.filters: list[str] = []

    async def subscribe(self, topic: str, **_kwargs: Any) -> object:
        self.filters.append(topic)
        return self.outcomes[topic]


async def _noop_message_callback(_topic: str, _payload: bytes) -> None:
    return None


@pytest.mark.asyncio
async def test_mqtt_dynamic_subscriptions_publish_dispatch_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    state_changes: list[bool] = []
    received: list[tuple[str, bytes]] = []
    message_received = asyncio.Event()

    async def on_message(topic: str, payload: bytes) -> None:
        received.append((topic, payload))
        message_received.set()

    async def second_consumer(_topic: str, _payload: bytes) -> None:
        return None

    transport = MQTTTransport(_config(), state_changes.append)
    await transport.start()
    first = _FakeClient.instances[0]
    assert transport.connected
    assert first.kwargs["username"] == "vendor-a"
    assert first.kwargs["password"] == "synthetic-token"
    assert first.kwargs["protocol"] is paho.MQTTv5
    assert first.kwargs["clean_start"] is True
    assert first.kwargs["properties"].SessionExpiryInterval == 0
    assert first.kwargs["identifier"].startswith("vendor-a-")
    assert "clean_session" not in first.kwargs
    assert first.subscriptions == []
    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, on_message)
    second_handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, second_consumer)
    assert first.subscriptions == [(ENTITY_JSON_SUBSCRIPTION, 1)]

    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    await first.queue.put(_Message(topic, b"payload"))
    async with asyncio.timeout(1):
        await message_received.wait()
    assert received == [(topic, b"payload")]

    await transport.publish(topic, b"outbound", qos=1)
    assert first.publications == [(topic, b"outbound", 1)]

    await first.queue.put(_END)
    async with asyncio.timeout(1):
        await _FakeClient.second_connected.wait()
    second = _FakeClient.instances[1]
    assert second.kwargs["identifier"] == first.kwargs["identifier"]
    assert second.kwargs["clean_start"] is True
    assert second.kwargs["properties"].SessionExpiryInterval == 0
    assert second.subscriptions == [(ENTITY_JSON_SUBSCRIPTION, 1)]
    assert state_changes[:3] == [True, False, True]

    await transport.unsubscribe(handle)
    assert second.unsubscriptions == []
    await transport.unsubscribe(second_handle)
    assert second.unsubscriptions == [ENTITY_JSON_SUBSCRIPTION]
    await transport.close()
    assert not transport.connected
    assert state_changes[-1] is False


@pytest.mark.asyncio
async def test_retiring_one_shared_owner_during_restore_keeps_filter() -> None:
    transport = MQTTTransport(_config())
    restored = _RacingClient()
    restored.block_subscribe = True
    restoration: asyncio.Task[None] | None = None

    try:
        original = _RacingClient()
        transport._client = original  # type: ignore[assignment]
        transport._connected = True
        first = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
        )
        second = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
        )
        transport._client = None
        restoration = asyncio.create_task(
            transport._restore_subscriptions(restored)  # type: ignore[arg-type]
        )
        await asyncio.wait_for(restored.subscribe_started.wait(), timeout=1)
        await asyncio.wait_for(transport.unsubscribe(first), timeout=1)
        restored.allow_subscribe.set()
        await asyncio.wait_for(asyncio.shield(restoration), timeout=1)

        assert first not in transport._subscriptions
        assert second in transport._subscriptions
        assert restored.subscribe_calls == 1
        assert restored.unsubscribe_calls == 0
        assert transport._client is restored
        await transport.unsubscribe(second)
    finally:
        restored.allow_subscribe.set()
        if restoration is not None and not restoration.done():
            restoration.cancel()
        if restoration is not None:
            await asyncio.gather(restoration, return_exceptions=True)
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "failure_type"),
    [
        (0x87, AuthorizationError),
        (0x97, ResourceLimitError),
    ],
)
async def test_retired_owner_is_excluded_from_concurrent_restore_denial(
    reason_code: int,
    failure_type: type[ECNClientError],
) -> None:
    transport = MQTTTransport(_config())
    failures: list[ECNClientError] = []
    restored = _RacingClient()
    restored.block_subscribe = True
    restored.subscribe_result = [reason_code]
    restoration: asyncio.Task[None] | None = None

    try:
        original = _RacingClient()
        transport._client = original  # type: ignore[assignment]
        transport._connected = True
        retired = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
        )
        active = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
            on_restore_failure=failures.append,
        )
        transport._client = None
        restoration = asyncio.create_task(
            transport._restore_subscriptions(restored)  # type: ignore[arg-type]
        )
        await asyncio.wait_for(restored.subscribe_started.wait(), timeout=1)
        await asyncio.wait_for(transport.unsubscribe(retired), timeout=1)
        restored.allow_subscribe.set()
        await asyncio.wait_for(asyncio.shield(restoration), timeout=1)

        assert len(failures) == 1
        assert isinstance(failures[0], failure_type)
        assert retired not in transport._subscriptions
        assert active not in transport._subscriptions
        assert transport._client is restored
    finally:
        restored.allow_subscribe.set()
        if restoration is not None and not restoration.done():
            restoration.cancel()
        if restoration is not None:
            await asyncio.gather(restoration, return_exceptions=True)
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "failure_type", "failure_code", "failure_operation"),
    [
        (
            ProtocolError(
                "synthetic malformed compensating UNSUBACK",
                operation="mqtt.unsubscribe",
                details={"packet_type": "UNSUBACK", "classifier": "wrong_cardinality"},
            ),
            ProtocolError,
            ConnectionFailureCode.PROTOCOL_FAILURE,
            ConnectionFailureOperation.RECEIVE,
        ),
        (
            AuthorizationError(
                "synthetic denied compensating UNSUBACK",
                operation="mqtt.unsubscribe",
            ),
            AuthorizationError,
            ConnectionFailureCode.SUBSCRIPTION_DENIED,
            ConnectionFailureOperation.RESTORE_SUBSCRIPTION,
        ),
    ],
)
async def test_failed_compensating_unsubscribe_prevents_restored_readiness(
    failure: ECNClientError,
    failure_type: type[ECNClientError],
    failure_code: ConnectionFailureCode,
    failure_operation: ConnectionFailureOperation,
) -> None:
    transport = MQTTTransport(_config())
    restored = _RacingClient()
    restored.block_subscribe = True
    restored.unsubscribe_exception = failure
    restoration: asyncio.Task[None] | None = None

    try:
        original = _RacingClient()
        transport._client = original  # type: ignore[assignment]
        transport._connected = True
        handle = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
        )
        transport._client = None
        restoration = asyncio.create_task(
            transport._restore_subscriptions(restored)  # type: ignore[arg-type]
        )
        await asyncio.wait_for(restored.subscribe_started.wait(), timeout=1)
        await asyncio.wait_for(transport.unsubscribe(handle), timeout=1)
        restored.allow_subscribe.set()
        with pytest.raises(failure_type) as caught:
            await asyncio.wait_for(asyncio.shield(restoration), timeout=1)

        classification = transport._classify_failure(
            caught.value,
            client=None,
            was_ready=True,
        )
        assert classification.code is failure_code
        assert classification.operation is failure_operation
        assert transport.ready is False
        assert transport._client is None
        assert restored.session_invalidations == 1
    finally:
        restored.allow_subscribe.set()
        if restoration is not None and not restoration.done():
            restoration.cancel()
        if restoration is not None:
            await asyncio.gather(restoration, return_exceptions=True)
        await transport.close()


@pytest.mark.asyncio
async def test_disconnect_during_unsubscribe_releases_subscription_lock_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MQTTTransport(_config())
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._connected.set_result(None)
    unsubscribe_started = asyncio.Event()

    def leave_unacknowledged(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        unsubscribe_started.set()
        return paho.MQTT_ERR_SUCCESS, 59

    monkeypatch.setattr(adapter._client, "unsubscribe", leave_unacknowledged)
    removing: asyncio.Task[None] | None = None
    recovery: asyncio.Task[None] | None = None
    recovery_acquired_lock = asyncio.Event()

    async def wait_for_subscription_lock() -> None:
        async with transport._subscription_lock:
            recovery_acquired_lock.set()

    try:
        original = _RacingClient()
        transport._client = original  # type: ignore[assignment]
        transport._connected = True
        handle = await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            _noop_message_callback,
        )
        transport._client = adapter
        removing = asyncio.create_task(transport.unsubscribe(handle))
        await asyncio.wait_for(unsubscribe_started.wait(), timeout=1)
        assert 59 in adapter._pending_unsubscribe_acknowledgements
        recovery = asyncio.create_task(wait_for_subscription_lock())
        await asyncio.sleep(0)
        assert not recovery_acquired_lock.is_set()

        adapter._on_disconnect(
            adapter._client,
            None,
            paho.DisconnectFlags(is_disconnect_packet_from_server=True),
            ReasonCode(PacketTypes.DISCONNECT, identifier=0x00),  # type: ignore[no-untyped-call]
            None,
        )

        with pytest.raises(ConnectionError) as caught:
            await asyncio.wait_for(asyncio.shield(removing), timeout=0.1)
        assert caught.value.operation == "mqtt.unsubscribe"
        assert "localhost" not in str(caught.value)
        await asyncio.wait_for(asyncio.shield(recovery), timeout=0.1)
        assert recovery_acquired_lock.is_set()
        assert transport._subscription_setup_tasks == set()
        assert transport._broker_operation_tasks == set()
    finally:
        if not adapter._disconnected.done():
            adapter._force_disconnect()
        for task in (removing, recovery):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (removing, recovery) if task is not None),
            return_exceptions=True,
        )
        await transport.close()


@pytest.mark.asyncio
async def test_disconnect_during_subscribe_releases_subscription_lock_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MQTTTransport(_config())
    adapter = _MQTTV5Client(hostname="localhost", protocol=paho.MQTTv5)
    adapter._connected.set_result(None)
    subscribe_started = asyncio.Event()

    def leave_unacknowledged(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        subscribe_started.set()
        return paho.MQTT_ERR_SUCCESS, 61

    monkeypatch.setattr(adapter._client, "subscribe", leave_unacknowledged)
    opening: asyncio.Task[SubscriptionHandle] | None = None
    recovery: asyncio.Task[None] | None = None
    recovery_acquired_lock = asyncio.Event()

    async def wait_for_subscription_lock() -> None:
        async with transport._subscription_lock:
            recovery_acquired_lock.set()

    try:
        transport._client = adapter
        transport._connected = True
        opening = asyncio.create_task(
            transport.subscribe(ENTITY_JSON_SUBSCRIPTION, _noop_message_callback)
        )
        await asyncio.wait_for(subscribe_started.wait(), timeout=1)
        assert 61 in adapter._pending_subscribes
        recovery = asyncio.create_task(wait_for_subscription_lock())
        await asyncio.sleep(0)
        assert not recovery_acquired_lock.is_set()

        adapter._on_disconnect(
            adapter._client,
            None,
            paho.DisconnectFlags(is_disconnect_packet_from_server=True),
            ReasonCode(PacketTypes.DISCONNECT, identifier=0x00),  # type: ignore[no-untyped-call]
            None,
        )

        with pytest.raises(ConnectionError) as caught:
            await asyncio.wait_for(asyncio.shield(opening), timeout=0.1)
        assert caught.value.operation == "mqtt.subscribe"
        assert "localhost" not in str(caught.value)
        await asyncio.wait_for(asyncio.shield(recovery), timeout=0.1)
        assert recovery_acquired_lock.is_set()
        assert adapter._pending_subscribes == {}
        assert adapter._retired_subscribe_mids == set()
        assert transport._subscription_setup_tasks == set()
        assert transport._broker_operation_tasks == set()
    finally:
        if not adapter._disconnected.done():
            adapter._force_disconnect()
        for task in (opening, recovery):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (opening, recovery) if task is not None),
            return_exceptions=True,
        )
        await transport.close()


@pytest.mark.asyncio
async def test_mqtt_start_connects_without_installing_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.instances = []
    _FakeClient.second_connected = asyncio.Event()
    monkeypatch.setattr("picogrid_ecn_client._transport.mqtt._MQTTV5Client", _FakeClient)
    transport = MQTTTransport(_config())
    await transport.start()
    assert _FakeClient.instances[0].subscriptions == []
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "failure_type"),
    [
        (0x87, AuthorizationError),
        (0x97, ResourceLimitError),
    ],
)
async def test_restore_denial_closes_every_owner_and_continues_allowed_filter(
    reason_code: int,
    failure_type: type[ECNClientError],
) -> None:
    transport = MQTTTransport(_config())
    original = _RacingClient()
    transport._client = original  # type: ignore[assignment]
    transport._connected = True
    denied_filter = "entity/vendor-a/+/track"
    allowed_filter = "entity/vendor-a/+/detection"
    failures: list[ECNClientError] = []

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    first_denied = await transport.subscribe(
        denied_filter,
        callback,
        on_restore_failure=failures.append,
    )
    second_denied = await transport.subscribe(
        denied_filter,
        callback,
        on_restore_failure=failures.append,
    )
    allowed = await transport.subscribe(allowed_filter, callback)
    restored = _RestoreClient({denied_filter: [reason_code], allowed_filter: [1]})

    await transport._restore_subscriptions(restored)  # type: ignore[arg-type]

    assert restored.filters == [allowed_filter, denied_filter]
    assert len(failures) == 2
    assert all(isinstance(failure, failure_type) for failure in failures)
    assert all(failure.operation == "mqtt.restore_subscription" for failure in failures)
    assert first_denied not in transport._subscriptions
    assert second_denied not in transport._subscriptions
    assert allowed in transport._subscriptions
    assert transport._client is restored
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "failure_type"),
    [
        (0x87, AuthorizationError),
        (0x97, ResourceLimitError),
    ],
)
async def test_restore_denial_without_owner_callback_fails_terminally(
    reason_code: int,
    failure_type: type[ECNClientError],
) -> None:
    transport = MQTTTransport(_config())
    original = _RacingClient()
    transport._client = original  # type: ignore[assignment]
    transport._connected = True
    denied_filter = "entity/vendor-a/+/track"

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(denied_filter, callback)
    restored = _RestoreClient({denied_filter: [reason_code]})

    with pytest.raises(failure_type, match="could not be isolated"):
        await transport._restore_subscriptions(restored)  # type: ignore[arg-type]

    assert handle not in transport._subscriptions
    await transport.close()


def test_unisolated_restored_quota_denial_has_distinct_failure_code() -> None:
    transport = MQTTTransport(_config())
    classification = transport._classify_failure(
        ResourceLimitError(
            "synthetic restored subscription quota denial",
            operation="mqtt.restore_subscription",
        ),
        client=None,
        was_ready=False,
    )

    assert classification.terminal is True
    assert classification.code is ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "failure_type"),
    [
        (0x87, AuthorizationError),
        (0x97, ResourceLimitError),
    ],
)
async def test_restore_denial_callback_failure_preserves_denial_type(
    reason_code: int,
    failure_type: type[ECNClientError],
) -> None:
    transport = MQTTTransport(_config())
    original = _RacingClient()
    transport._client = original  # type: ignore[assignment]
    transport._connected = True
    denied_filter = "entity/vendor-a/+/track"

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    def reject_failure(_failure: ECNClientError) -> None:
        raise RuntimeError("synthetic callback failure")

    handle = await transport.subscribe(
        denied_filter,
        callback,
        on_restore_failure=reject_failure,
    )
    restored = _RestoreClient({denied_filter: [reason_code]})

    with pytest.raises(failure_type, match="could not be isolated"):
        await transport._restore_subscriptions(restored)  # type: ignore[arg-type]

    assert handle not in transport._subscriptions
    await transport.close()


@pytest.mark.asyncio
async def test_mqtt_close_clears_local_subscription_callbacks() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    assert transport._subscriptions

    await transport.close()

    assert transport._subscriptions == {}


@pytest.mark.asyncio
async def test_live_subscribe_clears_readiness_until_suback() -> None:
    readiness_notifications: list[bool] = []

    async def observe(_snapshot: object) -> None:
        readiness_notifications.append(transport.ready)

    transport = MQTTTransport(_config(), on_recovery_change=observe)
    client = _RacingClient()
    client.block_subscribe = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    transport._connection_generation = 1
    transport._mark_ready()
    opening: asyncio.Task[SubscriptionHandle] | None = None
    readiness: asyncio.Task[None] | None = None

    try:
        opening = asyncio.create_task(
            transport.subscribe(ENTITY_JSON_SUBSCRIPTION, _noop_message_callback)
        )
        await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)

        assert transport.ready is False
        assert readiness_notifications == [False]
        readiness = asyncio.create_task(transport._wait_until_ready())
        await asyncio.sleep(0)
        assert not readiness.done()

        client.allow_subscribe.set()
        handle = await asyncio.wait_for(asyncio.shield(opening), timeout=1)
        await asyncio.wait_for(asyncio.shield(readiness), timeout=1)

        assert transport.ready is True
        assert readiness_notifications == [False, True]
        assert handle in transport._subscriptions
        await transport.unsubscribe(handle)
    finally:
        client.allow_subscribe.set()
        for task in (opening, readiness):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (opening, readiness) if task is not None),
            return_exceptions=True,
        )
        await transport.close()


@pytest.mark.asyncio
async def test_transient_response_subscribe_does_not_suspend_global_readiness() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_subscribe = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    transport._connection_generation = 1
    transport._mark_ready()
    opening: asyncio.Task[SubscriptionHandle] | None = None

    try:
        opening = asyncio.create_task(
            transport.subscribe(
                "task/vendor-a/00000000-0000-4000-8000-000000000001/command/response",
                _noop_message_callback,
                restore_on_reconnect=False,
            )
        )
        await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)

        assert transport.ready is True

        client.allow_subscribe.set()
        handle = await asyncio.wait_for(asyncio.shield(opening), timeout=1)
        await transport.unsubscribe(handle)
    finally:
        client.allow_subscribe.set()
        if opening is not None and not opening.done():
            opening.cancel()
        if opening is not None:
            await asyncio.gather(opening, return_exceptions=True)
        await transport.close()


@pytest.mark.asyncio
async def test_concurrent_first_subscribe_failure_invalidates_ambiguous_session() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.fail_first_subscribe = True
    client.block_subscribe = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    first = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    await client.subscribe_started.wait()
    second = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    client.allow_subscribe.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(result, ConnectionError) for result in results)
    assert client.subscribe_calls == 1
    assert client.session_invalidations == 1
    assert transport._client is None
    assert not transport.connected
    assert not transport.ready
    assert transport._subscriptions == {}


@pytest.mark.asyncio
async def test_missing_suback_invalidates_ready_session_before_error() -> None:
    connection_changes: list[bool] = []
    transport = MQTTTransport(_config(), on_connection_change=connection_changes.append)
    client = _RacingClient()
    client.subscribe_exception = aiomqtt.MqttError("synthetic missing SUBACK detail")
    transport._client = client  # type: ignore[assignment]
    await transport._set_connected(True)
    transport._mark_ready()

    with pytest.raises(ConnectionError) as caught:
        await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, _noop_message_callback)

    assert caught.value.operation == "mqtt.subscribe"
    assert "synthetic" not in str(caught.value)
    assert client.session_invalidations == 1
    assert connection_changes == [True, False]
    assert transport._client is None
    assert not transport.connected
    assert not transport.ready
    assert transport._subscriptions == {}
    assert transport._broker_operation_tasks == set()
    await transport.close()


@pytest.mark.asyncio
async def test_cancelled_subscribe_with_missing_suback_invalidates_session() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_subscribe = True
    client.resist_subscribe_cancellation = True
    client.subscribe_exception = aiomqtt.MqttError("synthetic missing SUBACK detail")
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    transport._strict_ready = True

    opening = asyncio.create_task(
        transport.subscribe(ENTITY_JSON_SUBSCRIPTION, _noop_message_callback)
    )
    await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)
    opening.cancel()
    await asyncio.sleep(0)
    client.allow_subscribe.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, timeout=1)
    assert client.session_invalidations == 1
    assert transport._client is None
    assert not transport.connected
    assert not transport.ready
    assert transport._subscriptions == {}
    assert transport._broker_operation_tasks == set()
    await transport.close()


@pytest.mark.asyncio
async def test_cancelled_subscribe_waits_for_suback_and_compensates_at_broker() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_subscribe = True
    client.resist_subscribe_cancellation = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    opening = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)
    opening.cancel()
    await asyncio.sleep(0)
    client.allow_subscribe.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, timeout=1)
    assert client.unsubscribe_calls == 1
    assert transport._subscriptions == {}


@pytest.mark.asyncio
async def test_subscribe_does_not_migrate_across_connection_generation() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_subscribe = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    transport._connection_generation = 4

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    opening = asyncio.create_task(
        transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            callback,
            expected_connection_generation=4,
        )
    )
    await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)
    transport._connection_generation = 5
    client.allow_subscribe.set()

    with pytest.raises(ConnectionError):
        await opening
    assert client.unsubscribe_calls == 1
    assert transport._subscriptions == {}
    await transport.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_subscribe_and_waits_for_compensation() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_subscribe = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    opening = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)
    closing = asyncio.create_task(transport.close())
    await asyncio.sleep(0)
    assert not closing.done()

    client.allow_subscribe.set()
    await asyncio.wait_for(closing, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert client.unsubscribe_calls == 0
    assert transport._subscriptions == {}
    assert transport._subscription_setup_tasks == set()
    assert transport._broker_operation_tasks == set()


@pytest.mark.asyncio
async def test_close_cancels_inflight_publish_and_prevents_late_success() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_publish = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)

    publishing = asyncio.create_task(transport.publish(topic, b"payload", qos=1))
    await asyncio.wait_for(client.publish_started.wait(), timeout=1)
    await asyncio.wait_for(transport.close(), timeout=1)

    with pytest.raises(OutcomeUnknownError) as caught:
        await publishing
    assert caught.value.delivery_phase is DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
    assert client.publish_calls == 1
    assert transport._publish_tasks == set()


@pytest.mark.asyncio
async def test_subscribe_rejects_stale_expected_generation_before_send() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    transport._connection_generation = 4

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    with pytest.raises(ConnectionError):
        await transport.subscribe(
            ENTITY_JSON_SUBSCRIPTION,
            callback,
            expected_connection_generation=3,
        )

    assert client.subscribe_calls == 0
    assert transport._subscriptions == {}

    handle = await transport.subscribe(
        ENTITY_JSON_SUBSCRIPTION,
        callback,
        expected_connection_generation=4,
    )
    assert client.subscribe_calls == 1
    await transport.unsubscribe(handle)
    await transport.close()


@pytest.mark.asyncio
async def test_close_does_not_orphan_a_subscription_that_resists_one_cancellation() -> None:
    config = _config().model_copy(update={"shutdown_timeout": 0.01})
    transport = MQTTTransport(config)
    client = _RacingClient()
    client.block_subscribe = True
    client.resist_subscribe_cancellation = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    opening = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    await asyncio.wait_for(client.subscribe_started.wait(), timeout=1)

    with pytest.raises(ECNTimeoutError, match="shutdown_timeout"):
        await transport.close()
    await asyncio.sleep(0)

    assert opening.done()
    assert transport._broker_operation_tasks == set()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name() == "picogrid-ecn-mqtt-subscribe"
    ]


@pytest.mark.asyncio
async def test_supervisor_inline_publish_preserves_shutdown_cancellation() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.block_publish = True
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)

    async def callback(_topic: str, _payload: bytes) -> None:
        await transport.publish(topic, b"response", qos=1)

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    supervisor = asyncio.create_task(
        transport._dispatch(topic, b"request"),
        name="picogrid-ecn-mqtt-transport",
    )
    transport._supervisor = supervisor
    await asyncio.wait_for(client.publish_started.wait(), timeout=1)

    await asyncio.wait_for(transport.close(), timeout=1)

    assert supervisor.cancelled()
    assert transport._publish_tasks == set()
    assert handle not in transport._subscriptions


@pytest.mark.asyncio
async def test_cancelled_unsubscribe_finishes_broker_cleanup() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    client.block_unsubscribe = True
    removing = asyncio.create_task(transport.unsubscribe(handle))
    await asyncio.wait_for(client.unsubscribe_started.wait(), timeout=1)
    removing.cancel()
    await asyncio.sleep(0)
    assert not removing.done()
    client.allow_unsubscribe.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(removing, timeout=1)
    assert client.unsubscribe_calls == 1
    assert transport._subscriptions == {}
    assert transport._broker_operation_tasks == set()
    assert client.session_invalidations == 0


@pytest.mark.asyncio
async def test_cancelled_unsubscribe_waiting_for_lock_still_retires_callback() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    await transport._subscription_lock.acquire()
    removing = asyncio.create_task(transport.unsubscribe(handle))
    await asyncio.sleep(0)
    assert transport._subscription_setup_tasks

    removing.cancel()
    await asyncio.sleep(0)
    assert not removing.done()
    transport._subscription_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(removing, timeout=1)
    assert handle not in transport._subscriptions
    assert client.unsubscribe_calls == 1
    assert transport._subscription_setup_tasks == set()
    assert transport._broker_operation_tasks == set()


@pytest.mark.asyncio
async def test_cancelled_unsubscribe_preserves_cancellation_after_negative_unsuback() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    client.block_unsubscribe = True
    client.unsubscribe_exception = AuthorizationError(
        "synthetic negative acknowledgement",
        operation="mqtt.unsubscribe",
    )
    removing = asyncio.create_task(transport.unsubscribe(handle))
    await asyncio.wait_for(client.unsubscribe_started.wait(), timeout=1)
    removing.cancel()
    client.allow_unsubscribe.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(removing, timeout=1)
    assert transport._subscriptions == {}
    assert transport._broker_operation_tasks == set()
    assert client.session_invalidations == 1


@pytest.mark.asyncio
async def test_close_racing_unsubscribe_consumes_late_cleanup_error() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    client.block_unsubscribe = True
    client.unsubscribe_exception = AuthorizationError(
        "synthetic negative acknowledgement",
        operation="mqtt.unsubscribe",
    )
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        removing = asyncio.create_task(transport.unsubscribe(handle))
        await asyncio.wait_for(client.unsubscribe_started.wait(), timeout=1)
        cleanup_task = next(iter(transport._subscription_setup_tasks))
        cleanup_done = asyncio.Event()
        cleanup_task.add_done_callback(lambda _task: cleanup_done.set())
        transport._closing = True
        removing.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(removing, timeout=1)
        client.allow_unsubscribe.set()
        await asyncio.wait_for(cleanup_done.wait(), timeout=1)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert reported == []
    assert transport._subscription_setup_tasks == set()
    assert transport._broker_operation_tasks == set()
    assert client.session_invalidations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_codes", "expected_error"),
    [
        (None, ProtocolError),
        ([], ProtocolError),
        ([2], ProtocolError),
        ([128], ProtocolError),
        ([0x87], AuthorizationError),
        ([0x97], ResourceLimitError),
        ([0, 1], ProtocolError),
        (object(), ProtocolError),
    ],
)
async def test_subscribe_rejects_missing_or_malformed_suback(
    reason_codes: object,
    expected_error: type[ECNClientError],
) -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    client.subscribe_result = reason_codes
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    with pytest.raises(expected_error):
        await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    await transport.close()


@pytest.mark.asyncio
async def test_last_unsubscribe_serializes_with_new_subscriber() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    client.block_unsubscribe = True
    removing = asyncio.create_task(transport.unsubscribe(handle))
    await client.unsubscribe_started.wait()
    adding = asyncio.create_task(transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback))
    await asyncio.sleep(0)
    assert client.subscribe_calls == 1

    client.allow_unsubscribe.set()
    await removing
    new_handle = await adding
    assert client.unsubscribe_calls == 1
    assert client.subscribe_calls == 2
    await transport.unsubscribe(new_handle)


@pytest.mark.asyncio
async def test_failed_broker_unsubscribe_still_removes_local_callback() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    handle = await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)
    client.fail_unsubscribe = True

    with pytest.raises(ConnectionError, match="unsubscribe failed"):
        await transport.unsubscribe(handle)

    assert handle not in transport._subscriptions
    assert not transport.connected
    assert transport._client is None
    assert client.session_invalidations == 1


@pytest.mark.asyncio
async def test_mqtt_rejects_invalid_or_oversized_publications() -> None:
    transport = MQTTTransport(_config())
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)
    with pytest.raises(DeliveryError) as caught:
        await transport.publish(topic, b"payload")
    assert caught.value.delivery_phase is DeliveryPhase.NOT_SENT
    with pytest.raises(ValidationError):
        await transport.publish("#", b"payload")
    with pytest.raises(ResourceLimitError):
        await transport.publish(topic, b"x" * 1025)


@pytest.mark.asyncio
async def test_mqtt_rejects_subscription_before_connection() -> None:
    transport = MQTTTransport(_config())

    async def callback(_topic: str, _payload: bytes) -> None:
        return None

    with pytest.raises(ConnectionError, match="not connected"):
        await transport.subscribe(ENTITY_JSON_SUBSCRIPTION, callback)


@pytest.mark.asyncio
async def test_dispatch_invokes_shared_callback_once_for_overlapping_filters() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    calls = 0

    async def callback(_topic: str, _payload: bytes) -> None:
        nonlocal calls
        calls += 1

    await transport.subscribe("entity/vendor-a/+/track", callback)
    await transport.subscribe("entity/+/+/track", callback)
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)

    await transport._dispatch(topic, b"payload")

    assert calls == 1
    await transport.close()


@pytest.mark.asyncio
async def test_dispatch_invokes_distinct_callbacks_for_overlapping_filters() -> None:
    transport = MQTTTransport(_config())
    client = _RacingClient()
    transport._client = client  # type: ignore[assignment]
    transport._connected = True
    first_calls = 0
    second_calls = 0

    async def first(_topic: str, _payload: bytes) -> None:
        nonlocal first_calls
        first_calls += 1

    async def second(_topic: str, _payload: bytes) -> None:
        nonlocal second_calls
        second_calls += 1

    await transport.subscribe("entity/vendor-a/+/track", first)
    await transport.subscribe("entity/+/+/track", second)
    topic = build_entity_topic("vendor-a", ENTITY_ID, EntityCategory.TRACK)

    await transport._dispatch(topic, b"payload")

    assert first_calls == 1
    assert second_calls == 1
    await transport.close()
