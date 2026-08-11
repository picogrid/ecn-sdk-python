# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Small reconnecting MQTT adapter restricted to the supported topic subset."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import os
import random
import socket
import ssl
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from types import MethodType
from typing import Any, cast
from uuid import UUID, uuid4

import aiomqtt
from aiomqtt.client import _set_client_socket_defaults
from aiomqtt.exceptions import MqttCodeError, MqttConnectError
from aiomqtt.types import SubscribeTopic
from paho.mqtt import client as paho
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.subscribeoptions import SubscribeOptions

from .._legion_auth import _LegionCredentialError
from .._network import validate_private_endpoint_addresses
from .._protocol import validate_publish_topic, validate_subscription_filter
from ..auth import BearerTokenAuth
from ..config import ECNConfig
from ..exceptions import (
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
from ..exceptions import (
    TimeoutError as ECNTimeoutError,
)
from ..models.common import (
    ConnectionFailureCode,
    ConnectionFailureOperation,
    ConnectionRetryState,
    DeliveryPhase,
)
from .credentials import build_lifecycle_owned_client_ssl_context

logger = logging.getLogger(__name__)

_MQTT_SUCCESS = 0x00
_MQTT_NO_MATCHING_SUBSCRIBERS = 0x10
_MQTT_NO_SUBSCRIPTION_EXISTED = 0x11
_MQTT_NOT_AUTHORIZED = 0x87
_MQTT_QUOTA_EXCEEDED = 0x97
_MISSING = object()
_MAXIMUM_RESOLVER_IPC_SIZE = 16 * 1024
_MAXIMUM_RESOLVED_ADDRESSES = 32
_RESOLVER_CLEANUP_TIMEOUT_SECONDS = 1.0
_RESOLVER_PROCESS_CODE = (
    "from picogrid_ecn_client._transport.mqtt import _resolver_process_main;"
    "_resolver_process_main()"
)
# Public wire constants below are copied from the OASIS MQTT Version 5.0
# CONNACK and PUBACK packet definitions solely for interoperable, fail-closed
# classification. Paho 2.1.0 omits CONNACK 0x99, so the raw guard preserves
# that standards-defined value before decoding.
_CONNACK_SUCCESS = 0x00
_CONNACK_AUTHENTICATION_REJECTED = frozenset({0x86})
_CONNACK_AUTHORIZATION_DENIED = frozenset({0x87, 0x8A})
_CONNACK_CONFIGURATION_INVALID = frozenset({0x85, 0x8C})
_CONNACK_CONNECTION_REJECTED = frozenset({0x80, 0x83})
_CONNACK_BROKER_UNAVAILABLE = frozenset({0x88})
_CONNACK_SERVER_BUSY = frozenset({0x89, 0x9F})
_CONNACK_RESOURCE_LIMIT = frozenset({0x97})
_CONNACK_PROTOCOL_FAILURE = frozenset({0x81, 0x82, 0x84, 0x90, 0x95, 0x99, 0x9A, 0x9B})
_CONNACK_SERVER_REFERENCE = frozenset({0x9C, 0x9D})
_KNOWN_CONNACK_REASON_CODES = frozenset(
    {
        _CONNACK_SUCCESS,
        *_CONNACK_AUTHENTICATION_REJECTED,
        *_CONNACK_AUTHORIZATION_DENIED,
        *_CONNACK_CONFIGURATION_INVALID,
        *_CONNACK_CONNECTION_REJECTED,
        *_CONNACK_BROKER_UNAVAILABLE,
        *_CONNACK_SERVER_BUSY,
        *_CONNACK_RESOURCE_LIMIT,
        *_CONNACK_PROTOCOL_FAILURE,
        *_CONNACK_SERVER_REFERENCE,
    }
)
_KNOWN_PUBACK_REASON_CODES = frozenset({0x00, 0x10, 0x80, 0x83, 0x87, 0x90, 0x91, 0x97, 0x99})
_TRANSIENT_FAILURE_CODES = frozenset(
    {
        ConnectionFailureCode.DNS_UNAVAILABLE,
        ConnectionFailureCode.TCP_UNAVAILABLE,
        ConnectionFailureCode.TLS_UNAVAILABLE,
        ConnectionFailureCode.BROKER_UNAVAILABLE,
        ConnectionFailureCode.SERVER_BUSY,
        ConnectionFailureCode.CONNECTION_LOST,
    }
)


async def _build_attempt_ssl_context(config: ECNConfig) -> ssl.SSLContext | None:
    """Build one attempt's TLS context within the supervisor lifecycle."""

    return await build_lifecycle_owned_client_ssl_context(config.tls, config.auth)


MessageCallback = Callable[[str, bytes], Awaitable[None] | None]
ConnectionChangeCallback = Callable[[bool], Awaitable[None] | None]
RestoreFailure = AuthorizationError | ResourceLimitError
RestoreFailureCallback = Callable[[RestoreFailure], Awaitable[None] | None]
PublishStartedCallback = Callable[[], None]
ConnectionPhaseCallback = Callable[[ConnectionFailureOperation], None]


class _PublishCompletion(Enum):
    """Private completion signal for callers that wait for a response after send."""

    COMPLETED = "completed"
    COMPLETED_AFTER_CANCELLATION = "completed_after_cancellation"


class _PublicationAcknowledgementTimeout(aiomqtt.MqttError):
    """Private marker for a send whose PUBACK deadline expired."""


class _PublicationConnectionLost(aiomqtt.MqttError):
    """Private marker for a send interrupted before PUBACK."""


class _SubscriptionConnectionLost(aiomqtt.MqttError):
    """Private marker for a subscribe interrupted before SUBACK."""


class _UnsubscriptionConnectionLost(aiomqtt.MqttError):
    """Private marker for an unsubscribe interrupted before UNSUBACK."""


class _PublicationProtocolFailure(aiomqtt.MqttError):
    """Private marker for a sent publication on an invalid ACK generation."""

    def __init__(self, error: ProtocolError) -> None:
        self.error = error
        super().__init__("MQTT publication acknowledgment failed protocol validation")


class _ConnackProtocolFailure(aiomqtt.MqttError):
    """Private marker for one malformed or impossible MQTT v5 CONNACK."""

    def __init__(self, error: ProtocolError) -> None:
        self.error = error
        super().__init__("MQTT CONNACK failed protocol validation")


class _DNSResolutionFailure(aiomqtt.MqttError):
    """Detail-free marker for an isolated name-resolution failure."""

    def __init__(self) -> None:
        super().__init__("MQTT host resolution failed")


class _TCPConnectionFailure(aiomqtt.MqttError):
    """Detail-free marker for a nonblocking TCP connection failure."""

    def __init__(self) -> None:
        super().__init__("MQTT TCP connection failed")


class _TLSTransportFailure(aiomqtt.MqttError):
    """Detail-free marker for a transient nonblocking TLS failure."""

    def __init__(self) -> None:
        super().__init__("MQTT TLS transport failed")


class _TLSPeerVerificationFailure(aiomqtt.MqttError):
    """Detail-free marker for TLS peer verification failure."""

    def __init__(self) -> None:
        super().__init__("MQTT TLS peer certificate verification failed")


class _CredentialResolutionTimeout(AuthenticationError):
    """Marker for the attempt deadline expiring while resolving credentials."""

    def __init__(self) -> None:
        super().__init__(
            "MQTT credential resolution exceeded the connection timeout",
            operation="mqtt.authenticate",
        )


class _TLSMaterialLoadTimeout(ConfigurationError):
    """Marker for the attempt deadline expiring while loading TLS material."""

    def __init__(self) -> None:
        super().__init__(
            "MQTT TLS material loading exceeded the connection timeout",
            operation="configure_tls",
        )


class _ConnectionPhaseTimeout(TimeoutError):
    """Detail-free marker retaining the lifecycle phase that reached its deadline."""

    def __init__(self, operation: ConnectionFailureOperation) -> None:
        self.operation = operation
        super().__init__()


@dataclass(frozen=True, slots=True)
class _ResolvedTCPAddress:
    family: int
    socket_type: int
    protocol: int
    address: tuple[str, int] | tuple[str, int, int, int]


def _resolver_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _resolver_process_main() -> None:
    """Resolve one bounded TCP endpoint in disposable process isolation."""

    try:
        request_raw = sys.stdin.buffer.read(_MAXIMUM_RESOLVER_IPC_SIZE + 1)
        if not request_raw or len(request_raw) > _MAXIMUM_RESOLVER_IPC_SIZE:
            raise ValueError
        request = json.loads(request_raw, object_pairs_hook=_resolver_json_object)
        if not isinstance(request, dict) or set(request) != {"host", "port"}:
            raise ValueError
        host = request["host"]
        port = request["port"]
        if (
            not isinstance(host, str)
            or not 1 <= len(host) <= 1024
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise ValueError

        addresses: list[list[object]] = []
        seen: set[tuple[object, ...]] = set()
        for family, socket_type, protocol, _canonical_name, address in socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        ):
            if family == socket.AF_INET:
                ipv4_address = cast("tuple[str, int]", address)
                normalized: list[object] = [
                    family,
                    socket_type,
                    protocol,
                    ipv4_address[0],
                    ipv4_address[1],
                    0,
                    0,
                ]
            elif family == socket.AF_INET6:
                ipv6_address = cast("tuple[str, int, int, int]", address)
                normalized = [
                    family,
                    socket_type,
                    protocol,
                    ipv6_address[0],
                    ipv6_address[1],
                    ipv6_address[2],
                    ipv6_address[3],
                ]
            else:
                continue
            identity = tuple(normalized)
            if identity in seen:
                continue
            if len(addresses) >= _MAXIMUM_RESOLVED_ADDRESSES:
                raise ValueError
            seen.add(identity)
            addresses.append(normalized)
        if not addresses:
            raise ValueError
        response: dict[str, object] = {"status": "ok", "addresses": addresses}
    except BaseException:
        response = {"status": "error"}

    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAXIMUM_RESOLVER_IPC_SIZE:
        encoded = b'{"status":"error"}'
    with contextlib.suppress(BrokenPipeError, OSError):
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def _resolver_environment() -> dict[str, str]:
    """Return only platform values needed to start the isolated interpreter."""

    return {
        name: value
        for name in ("SystemRoot", "SYSTEMROOT", "WINDIR")
        if (value := os.environ.get(name))
    }


async def _terminate_resolver_process(
    process: asyncio.subprocess.Process,
    exchange: asyncio.Task[tuple[bytes | None, bytes | None]],
) -> None:
    """Kill, reap, and drain one resolver after cancellation or failure."""

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    exchange.cancel()

    async def finish_cleanup() -> None:
        await asyncio.gather(process.wait(), exchange, return_exceptions=True)

    cleanup = asyncio.create_task(finish_cleanup(), name="picogrid-ecn-dns-resolver-cleanup")
    repeated_cancellation: asyncio.CancelledError | None = None
    deadline = asyncio.get_running_loop().time() + _RESOLVER_CLEANUP_TIMEOUT_SECONDS
    while not cleanup.done() and (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        try:
            await asyncio.wait({cleanup}, timeout=remaining)
        except asyncio.CancelledError as cancellation:
            # The caller already preserves the cancellation that entered cleanup.
            # Consume only additional requests while allowing a bounded drain, then
            # re-raise the latest request so cancellation is never lost.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            repeated_cancellation = cancellation
    if cleanup.done():
        cleanup.result()
    else:
        cleanup.cancel()
        cleanup.add_done_callback(_consume_background_task_result)
    if repeated_cancellation is not None:
        raise repeated_cancellation


def _consume_background_task_result(task: asyncio.Task[Any]) -> None:
    """Consume a detached bounded-cleanup task when a dependency resists cancellation."""

    if not task.cancelled():
        task.exception()


def _decode_resolver_response(raw: bytes, *, expected_port: int) -> tuple[_ResolvedTCPAddress, ...]:
    if not raw or len(raw) > _MAXIMUM_RESOLVER_IPC_SIZE:
        raise _DNSResolutionFailure()
    try:
        response = json.loads(raw, object_pairs_hook=_resolver_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _DNSResolutionFailure() from None
    if not isinstance(response, dict):
        raise _DNSResolutionFailure()
    if response == {"status": "error"}:
        raise _DNSResolutionFailure()
    if set(response) != {"status", "addresses"} or response.get("status") != "ok":
        raise _DNSResolutionFailure()
    raw_addresses = response.get("addresses")
    if (
        not isinstance(raw_addresses, list)
        or not raw_addresses
        or len(raw_addresses) > _MAXIMUM_RESOLVED_ADDRESSES
    ):
        raise _DNSResolutionFailure()

    addresses: list[_ResolvedTCPAddress] = []
    for item in raw_addresses:
        if not isinstance(item, list) or len(item) != 7:
            raise _DNSResolutionFailure()
        family, socket_type, protocol, host, port, flow_info, scope_id = item
        if (
            not isinstance(family, int)
            or isinstance(family, bool)
            or family not in {socket.AF_INET, socket.AF_INET6}
            or not isinstance(socket_type, int)
            or isinstance(socket_type, bool)
            or socket_type != socket.SOCK_STREAM
            or not isinstance(protocol, int)
            or isinstance(protocol, bool)
            or protocol not in {0, socket.IPPROTO_TCP}
            or not isinstance(host, str)
            or "%" in host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or port != expected_port
            or not isinstance(flow_info, int)
            or isinstance(flow_info, bool)
            or not 0 <= flow_info <= 0xFFFFFFFF
            or not isinstance(scope_id, int)
            or isinstance(scope_id, bool)
            or not 0 <= scope_id <= 0xFFFFFFFF
        ):
            raise _DNSResolutionFailure()
        try:
            parsed_host = ipaddress.ip_address(host)
        except ValueError:
            raise _DNSResolutionFailure() from None
        if family == socket.AF_INET and not isinstance(parsed_host, ipaddress.IPv4Address):
            raise _DNSResolutionFailure()
        if family == socket.AF_INET6 and not isinstance(parsed_host, ipaddress.IPv6Address):
            raise _DNSResolutionFailure()
        if family == socket.AF_INET:
            if flow_info != 0 or scope_id != 0:
                raise _DNSResolutionFailure()
            address: tuple[str, int] | tuple[str, int, int, int] = (host, port)
        else:
            address = (host, port, flow_info, scope_id)
        addresses.append(_ResolvedTCPAddress(family, socket_type, protocol, address))
    return tuple(addresses)


async def _resolve_tcp_addresses(host: str, port: int) -> tuple[_ResolvedTCPAddress, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if isinstance(literal, ipaddress.IPv4Address):
        return (
            _ResolvedTCPAddress(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                (literal.compressed, port),
            ),
        )
    if isinstance(literal, ipaddress.IPv6Address) and literal.scope_id is None:
        return (
            _ResolvedTCPAddress(
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                (literal.compressed, port, 0, 0),
            ),
        )

    request = json.dumps({"host": host, "port": port}, separators=(",", ":")).encode("utf-8")
    if len(request) > _MAXIMUM_RESOLVER_IPC_SIZE:
        raise _DNSResolutionFailure()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _RESOLVER_PROCESS_CODE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
            env=_resolver_environment(),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _DNSResolutionFailure() from None

    exchange = asyncio.create_task(
        process.communicate(request),
        name="picogrid-ecn-dns-resolver-io",
    )
    try:
        stdout, _stderr = await asyncio.shield(exchange)
    except asyncio.CancelledError as cancellation:
        with contextlib.suppress(asyncio.CancelledError):
            await _terminate_resolver_process(process, exchange)
        raise cancellation
    except Exception:
        await _terminate_resolver_process(process, exchange)
        raise _DNSResolutionFailure() from None
    if process.returncode != 0 or stdout is None:
        raise _DNSResolutionFailure()
    addresses = _decode_resolver_response(stdout, expected_port=port)
    if host.casefold() == "localhost":
        addresses = tuple(
            address for address in addresses if ipaddress.ip_address(address.address[0]).is_loopback
        )
        if not addresses:
            raise _DNSResolutionFailure()
    return addresses


async def _wait_for_socket_readiness(sock: socket.socket, *, writable: bool) -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def set_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    registration = loop.add_writer if writable else loop.add_reader
    removal = loop.remove_writer if writable else loop.remove_reader
    registration(sock.fileno(), set_ready)
    try:
        await ready
    finally:
        removal(sock.fileno())


async def _perform_tls_handshake(sock: ssl.SSLSocket) -> None:
    while True:
        try:
            sock.do_handshake()
            return
        except ssl.SSLWantReadError:
            await _wait_for_socket_readiness(sock, writable=False)
        except ssl.SSLWantWriteError:
            await _wait_for_socket_readiness(sock, writable=True)


async def _connect_tcp_socket(
    sock: socket.socket,
    address: tuple[str, int] | tuple[str, int, int, int],
) -> None:
    await asyncio.get_running_loop().sock_connect(sock, address)


async def _open_prepared_socket(
    host: str,
    port: int,
    tls_context: ssl.SSLContext | None,
    *,
    on_connection_phase: ConnectionPhaseCallback | None = None,
    require_reviewed_network: bool = False,
    attempt_timeout: float | None = None,
) -> socket.socket:
    if tls_context is not None:
        minimum_version = tls_context.minimum_version
        maximum_version = tls_context.maximum_version
        if minimum_version < ssl.TLSVersion.TLSv1_2 or (
            maximum_version != ssl.TLSVersion.MAXIMUM_SUPPORTED
            and maximum_version < minimum_version
        ):
            raise ConfigurationError(
                "MQTT TLS requires a usable TLS 1.2-or-newer version range",
                operation="mqtt.connect",
            )
    if on_connection_phase is not None:
        on_connection_phase(ConnectionFailureOperation.DNS)
    addresses = await _resolve_tcp_addresses(host, port)
    if require_reviewed_network:
        validate_private_endpoint_addresses(
            host,
            tuple(dict.fromkeys(str(item.address[0]) for item in addresses)),
        )
    loop = asyncio.get_running_loop()
    deadline = None if attempt_timeout is None else loop.time() + attempt_timeout
    for index, resolved in enumerate(addresses):
        if on_connection_phase is not None:
            on_connection_phase(ConnectionFailureOperation.TCP)
        tcp_socket = socket.socket(resolved.family, resolved.socket_type, resolved.protocol)
        tcp_socket.setblocking(False)
        tls_socket: ssl.SSLSocket | None = None
        try:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            address_timeout = None if remaining is None else remaining / (len(addresses) - index)
            async with asyncio.timeout(address_timeout):
                await _connect_tcp_socket(tcp_socket, resolved.address)
                if tls_context is None:
                    return tcp_socket
                if on_connection_phase is not None:
                    on_connection_phase(ConnectionFailureOperation.TLS)
                tls_socket = tls_context.wrap_socket(
                    tcp_socket,
                    server_hostname=host,
                    do_handshake_on_connect=False,
                )
                tls_socket.setblocking(False)
                await _perform_tls_handshake(tls_socket)
                return tls_socket
        except asyncio.CancelledError:
            (tls_socket or tcp_socket).close()
            raise
        except TimeoutError:
            (tls_socket or tcp_socket).close()
            if index + 1 == len(addresses):
                raise
            continue
        except ssl.SSLCertVerificationError:
            (tls_socket or tcp_socket).close()
            raise _TLSPeerVerificationFailure() from None
        except ssl.SSLError:
            (tls_socket or tcp_socket).close()
            if index + 1 == len(addresses):
                raise _TLSTransportFailure() from None
            continue
        except OSError:
            (tls_socket or tcp_socket).close()
            if index + 1 == len(addresses):
                if tls_socket is None:
                    raise _TCPConnectionFailure() from None
                raise _TLSTransportFailure() from None
            continue
    raise _TCPConnectionFailure()


@dataclass(frozen=True, slots=True)
class _FailureClassification:
    code: ConnectionFailureCode
    operation: ConnectionFailureOperation
    terminal: bool = False
    waiting_for_credentials: bool = False
    immediate_retry: bool = False
    protocol_fingerprint: tuple[str, ...] | None = None


def _connection_phase_timeout_classification(
    operation: ConnectionFailureOperation,
    *,
    terminal: bool = False,
) -> _FailureClassification:
    if operation is ConnectionFailureOperation.RESOLVE_CREDENTIALS:
        return _FailureClassification(
            ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
            operation,
            terminal=True,
        )
    if operation is ConnectionFailureOperation.CONFIGURE:
        return _FailureClassification(
            ConnectionFailureCode.CONFIGURATION_INVALID,
            operation,
            terminal=True,
        )
    failure_code = {
        ConnectionFailureOperation.DNS: ConnectionFailureCode.DNS_UNAVAILABLE,
        ConnectionFailureOperation.TCP: ConnectionFailureCode.TCP_UNAVAILABLE,
        ConnectionFailureOperation.TLS: ConnectionFailureCode.TLS_UNAVAILABLE,
        ConnectionFailureOperation.CONNECT: ConnectionFailureCode.BROKER_UNAVAILABLE,
    }.get(operation, ConnectionFailureCode.BROKER_UNAVAILABLE)
    return _FailureClassification(failure_code, operation, terminal=terminal)


@dataclass(frozen=True, slots=True)
class _RecoverySnapshot:
    state: ConnectionRetryState
    consecutive_attempt_count: int
    next_retry_delay_seconds: float | None = None
    failure_code: ConnectionFailureCode | None = None
    failure_operation: ConnectionFailureOperation | None = None


RecoveryChangeCallback = Callable[[_RecoverySnapshot], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class SubscriptionHandle:
    """Opaque identity for one adapter-private callback registration."""

    token: UUID
    connection_generation: int


@dataclass(frozen=True, slots=True)
class _Subscription:
    topic_filter: str
    callback: MessageCallback
    on_restore_failure: RestoreFailureCallback | None = None
    restore_on_reconnect: bool = True


class _MQTTV5Client(aiomqtt.Client):
    """Preserve MQTT v5 PUBACK and UNSUBACK outcomes discarded by aiomqtt."""

    def __init__(
        self,
        *args: Any,
        on_connection_phase: ConnectionPhaseCallback | None = None,
        require_reviewed_network: bool = False,
        **kwargs: Any,
    ) -> None:
        tls_context = kwargs.get("tls_context")
        attempt_timeout = kwargs.get("timeout")
        self._attempt_tls_context = tls_context if isinstance(tls_context, ssl.SSLContext) else None
        self._on_connection_phase = on_connection_phase
        self._require_reviewed_network = require_reviewed_network
        self._attempt_timeout = (
            float(attempt_timeout)
            if isinstance(attempt_timeout, (int, float)) and not isinstance(attempt_timeout, bool)
            else None
        )
        self._acknowledgement_lock = threading.RLock()
        self._pending_publish_acknowledgements: dict[int, asyncio.Future[object]] = {}
        self._pending_unsubscribe_acknowledgements: dict[int, asyncio.Future[object]] = {}
        self._retired_publish_mids: set[int] = set()
        self._retired_subscribe_mids: set[int] = set()
        self._retired_unsubscribe_mids: set[int] = set()
        self._early_publish_acknowledgements: dict[int, object] = {}
        self._early_subscribe_acknowledgements: dict[int, object] = {}
        self._early_unsubscribe_acknowledgements: dict[int, object] = {}
        self._starting_publish = 0
        self._starting_subscribe = 0
        self._starting_unsubscribe = 0
        self._server_reference_present = False
        self._protocol_failure_error: ProtocolError | None = None
        self._socket_registration_generation = 0
        self._socket_write_registration_generation = 0
        super().__init__(*args, **kwargs)
        self._install_packet_guards()

    def _client_connect_with_socket(self, prepared_socket: socket.socket) -> None:
        """Inject one lifecycle-owned socket into pinned Paho's CONNECT path."""

        client = self._client
        previous_override = client.__dict__.get("_create_socket", _MISSING)
        socket_supplied = False

        def supply_prepared_socket(_client: paho.Client) -> socket.socket:
            nonlocal socket_supplied
            if socket_supplied:
                raise OSError
            socket_supplied = True
            return prepared_socket

        try:
            self._set_tls_params()
            client.connect_async(
                self._hostname,
                self._port,
                self._keepalive,
                self._bind_address,
                self._bind_port,
                self._clean_start,
                self._properties,
            )
            # Paho 2.1.0's public reconnect() performs its MQTT-v5 state reset,
            # socket callbacks, and CONNECT serialization after one private
            # socket-factory call. The exact-pinned one-shot seam preserves that
            # behavior while moving DNS, TCP, and TLS outside its blocking path.
            client.__dict__["_create_socket"] = MethodType(supply_prepared_socket, client)
            client.reconnect()
        except ssl.SSLCertVerificationError:
            raise _TLSPeerVerificationFailure() from None
        except ssl.SSLError:
            raise _TLSTransportFailure() from None
        except OSError:
            raise _TCPConnectionFailure() from None
        finally:
            if previous_override is _MISSING:
                with contextlib.suppress(AttributeError):
                    del client.__dict__["_create_socket"]
            else:
                client.__dict__["_create_socket"] = previous_override
        if not socket_supplied:
            raise _TCPConnectionFailure()

    def _on_socket_open(
        self,
        client: paho.Client,
        userdata: Any,
        sock: Any,
    ) -> None:
        """Install aiomqtt's reader only while this exact socket remains owned."""

        opened_socket = cast("socket.socket", sock)
        self._socket_registration_generation += 1
        registration_generation = self._socket_registration_generation
        descriptor = opened_socket.fileno()

        def read_ready() -> None:
            try:
                while True:
                    client.loop_read()
                    current_socket = client.socket()
                    if (
                        current_socket is not None
                        and hasattr(current_socket, "pending")
                        and current_socket.pending() > 0
                    ):
                        continue
                    break
            except Exception as error:
                if not self._disconnected.done():
                    self._disconnected.set_exception(error)

        def install_reader() -> None:
            if (
                registration_generation != self._socket_registration_generation
                or client.socket() is not opened_socket
                or opened_socket.fileno() != descriptor
            ):
                return
            # This mirrors aiomqtt 2.5.1's event-loop integration after the
            # generation guard above closes its queued-registration race.
            self._loop.add_reader(descriptor, read_ready)
            self._misc_task = self._loop.create_task(self._misc_loop())

        self._loop.call_soon_threadsafe(install_reader)

    def _on_socket_close(
        self,
        client: paho.Client,
        userdata: Any,
        sock: Any,
    ) -> None:
        """Invalidate a queued reader install before closing its descriptor."""

        self._socket_registration_generation += 1
        self._socket_write_registration_generation += 1
        super()._on_socket_close(client, userdata, sock)

    def _on_socket_register_write(
        self,
        client: paho.Client,
        userdata: Any,
        sock: Any,
    ) -> None:
        """Install one generation-bound Paho write callback."""

        opened_socket = cast("socket.socket", sock)
        self._socket_write_registration_generation += 1
        registration_generation = self._socket_write_registration_generation
        descriptor = opened_socket.fileno()

        def write_ready() -> None:
            try:
                client.loop_write()
            except Exception as error:
                if not self._disconnected.done():
                    self._disconnected.set_exception(error)

        def install_writer() -> None:
            if (
                registration_generation != self._socket_write_registration_generation
                or client.socket() is not opened_socket
                or opened_socket.fileno() != descriptor
            ):
                return
            self._loop.add_writer(descriptor, write_ready)

        self._loop.call_soon_threadsafe(install_writer)

    def _on_socket_unregister_write(
        self,
        client: paho.Client,
        userdata: Any,
        sock: Any,
    ) -> None:
        """Invalidate a queued writer install before removing it."""

        self._socket_write_registration_generation += 1
        descriptor = sock.fileno()
        if descriptor >= 0:
            self._loop.remove_writer(descriptor)

    async def __aenter__(self) -> _MQTTV5Client:
        """Connect through lifecycle-owned, cancellation-safe socket setup."""

        if self._lock.locked():
            raise aiomqtt.MqttReentrantError(
                "The client context manager is reusable, but not reentrant"
            )
        await self._lock.acquire()
        prepared_socket: socket.socket | None = None
        try:
            prepared_socket = await _open_prepared_socket(
                self._hostname,
                self._port,
                self._attempt_tls_context,
                on_connection_phase=self._on_connection_phase,
                require_reviewed_network=self._require_reviewed_network,
                attempt_timeout=self._attempt_timeout,
            )
            _set_client_socket_defaults(prepared_socket, self._socket_options)
            if self._on_connection_phase is not None:
                self._on_connection_phase(ConnectionFailureOperation.CONNECT)
            self._client_connect_with_socket(prepared_socket)
            prepared_socket = None
        except BaseException:
            # Once Paho consumed the one-shot factory result, _sock_close owns
            # callback removal and descriptor closure. If adoption did not
            # happen, the still-local close below handles the prepared socket.
            self._force_disconnect()
            if prepared_socket is not None:
                prepared_socket.close()
            if self._lock.locked():
                self._lock.release()
            raise

        try:
            await self._wait_for(self._connected, timeout=None)
        except asyncio.CancelledError:
            self._force_disconnect()
            if self._lock.locked():
                self._lock.release()
            raise
        except aiomqtt.MqttError:
            if self._lock.locked():
                self._lock.release()
            self._connected = asyncio.Future()
            raise
        if self._disconnected.done():
            self._disconnected = asyncio.Future()
        return self

    @property
    def protocol_failure_error(self) -> ProtocolError | None:
        """Return the first safe protocol failure that invalidated this session."""

        return self._protocol_failure_error

    def _install_packet_guards(self) -> None:
        """Preserve ACK evidence before pinned Paho normalizes or discards it."""

        original_handler = self._client._handle_connack

        def guarded_handler(_client: paho.Client) -> object:
            guarded_result = self._guard_connack_packet()
            if guarded_result is not None:
                return guarded_result
            return original_handler()

        self._client._handle_connack = MethodType(  # type: ignore[method-assign]
            guarded_handler,
            self._client,
        )
        original_puback_handler = self._client._handle_pubackcomp

        def guarded_puback_handler(_client: paho.Client, command: str) -> object:
            if command == "PUBACK":
                guarded_result = self._guard_puback_packet()
                if guarded_result is not None:
                    return guarded_result
            return original_puback_handler(command)  # type: ignore[arg-type]

        self._client._handle_pubackcomp = MethodType(  # type: ignore[method-assign]
            guarded_puback_handler,
            self._client,
        )
        original_suback_handler = self._client._handle_suback

        def guarded_suback_handler(_client: paho.Client) -> object:
            guarded_result = self._guard_subscription_ack_packet(
                operation="mqtt.subscribe",
                packet_type="SUBACK",
                properties_packet_type=PacketTypes.SUBACK,
            )
            if guarded_result is not None:
                return guarded_result
            return original_suback_handler()

        self._client._handle_suback = MethodType(  # type: ignore[method-assign]
            guarded_suback_handler,
            self._client,
        )
        original_unsuback_handler = self._client._handle_unsuback

        def guarded_unsuback_handler(_client: paho.Client) -> object:
            guarded_result = self._guard_subscription_ack_packet(
                operation="mqtt.unsubscribe",
                packet_type="UNSUBACK",
                properties_packet_type=PacketTypes.UNSUBACK,
            )
            if guarded_result is not None:
                return guarded_result
            return original_unsuback_handler()

        self._client._handle_unsuback = MethodType(  # type: ignore[method-assign]
            guarded_unsuback_handler,
            self._client,
        )

    def _guard_connack_packet(self) -> object | None:
        """Reject malformed CONNACK framing and retain Paho's raw 0x99 gap."""

        try:
            incoming = self._client._in_packet
            remaining_length = incoming["remaining_length"]
            packet = incoming["packet"]
            if (
                not isinstance(remaining_length, int)
                or isinstance(remaining_length, bool)
                or not isinstance(packet, (bytes, bytearray))
                or len(packet) != remaining_length
            ):
                return self._reject_connack_protocol("wrong_cardinality")
            if remaining_length < 2:
                return self._reject_connack_protocol("missing_reason_code")
            if remaining_length < 3:
                return self._reject_connack_protocol("malformed_connack")
            flags = packet[0]
            reason_code = packet[1]
            if flags & 0xFE:
                return self._reject_connack_protocol("malformed_connack")
            if flags & 0x01:
                return self._reject_connack_protocol("session_present")
            properties = Properties(PacketTypes.CONNACK)  # type: ignore[no-untyped-call]
            _, consumed = properties.unpack(packet[2:])  # type: ignore[no-untyped-call]
            if consumed != remaining_length - 2:
                return self._reject_connack_protocol("malformed_connack")
        except Exception:
            return self._reject_connack_protocol("malformed_connack")
        if reason_code not in _KNOWN_CONNACK_REASON_CODES:
            return self._reject_connack_protocol(
                "unmapped_failure",
                reason_code=reason_code,
            )
        if reason_code == 0x99:
            if not self._connected.done():
                self._connected.set_exception(MqttConnectError(reason_code))
            return paho.MQTT_ERR_PROTOCOL
        return None

    def _reject_connack_protocol(
        self,
        classifier: str,
        *,
        reason_code: int | None = None,
    ) -> object:
        error = _ack_protocol_error(
            operation="mqtt.connect",
            packet_type="CONNACK",
            classifier=classifier,
            reason_code=reason_code,
        )
        self._protocol_failure_error = error
        if not self._connected.done():
            self._connected.set_exception(_ConnackProtocolFailure(error))
        return paho.MQTT_ERR_PROTOCOL

    def _guard_puback_packet(self) -> object | None:
        """Validate PUBACK framing and identity before pinned Paho can discard it."""

        try:
            incoming = self._client._in_packet
            remaining_length = incoming["remaining_length"]
            packet = incoming["packet"]
            if (
                not isinstance(remaining_length, int)
                or isinstance(remaining_length, bool)
                or not isinstance(packet, (bytes, bytearray))
                or len(packet) != remaining_length
            ):
                return self._reject_puback_protocol("wrong_cardinality")
            if remaining_length < 2:
                return self._reject_puback_protocol("missing_packet_identifier")
            packet_identifier = int.from_bytes(packet[:2], "big")
            packet_identifier_error = _packet_identifier_error(
                packet_identifier,
                operation="mqtt.publish",
                packet_type="PUBACK",
            )
            if packet_identifier_error is not None:
                self.invalidate_protocol(packet_identifier_error)
                return paho.MQTT_ERR_PROTOCOL
            with self._client._out_message_mutex:
                matched = packet_identifier in self._client._out_messages
            if not matched:
                return self._reject_puback_protocol("unmatched_packet_identifier")
            if remaining_length == 2:
                return None
            reason_code = packet[2]
            if reason_code not in _KNOWN_PUBACK_REASON_CODES:
                return self._reject_puback_protocol(
                    "unmapped_failure" if reason_code >= 0x80 else "unlisted_success",
                    reason_code=reason_code,
                )
            if remaining_length == 3:
                return None
            try:
                properties = Properties(PacketTypes.PUBACK)  # type: ignore[no-untyped-call]
                _, consumed = properties.unpack(packet[3:])  # type: ignore[no-untyped-call]
            except Exception:
                return self._reject_puback_protocol(
                    "malformed_properties",
                    reason_code=reason_code,
                )
            if consumed != remaining_length - 3:
                return self._reject_puback_protocol(
                    "malformed_properties",
                    reason_code=reason_code,
                )
        except Exception:
            return self._reject_puback_protocol("wrong_cardinality")
        return None

    def _reject_puback_protocol(
        self,
        classifier: str,
        *,
        reason_code: int | None = None,
    ) -> object:
        error = _ack_protocol_error(
            operation="mqtt.publish",
            packet_type="PUBACK",
            classifier=classifier,
            reason_code=reason_code,
        )
        self.invalidate_protocol(error)
        return paho.MQTT_ERR_PROTOCOL

    def _guard_subscription_ack_packet(
        self,
        *,
        operation: str,
        packet_type: str,
        properties_packet_type: int,
    ) -> object | None:
        """Validate SUBACK/UNSUBACK framing before delegating to pinned Paho."""

        try:
            incoming = self._client._in_packet
            remaining_length = incoming["remaining_length"]
            packet = incoming["packet"]
            if (
                not isinstance(remaining_length, int)
                or isinstance(remaining_length, bool)
                or not isinstance(packet, (bytes, bytearray))
                or len(packet) != remaining_length
            ):
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="wrong_cardinality",
                )
            if remaining_length < 2:
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="missing_packet_identifier",
                )
            packet_identifier = int.from_bytes(packet[:2], "big")
            packet_identifier_error = _packet_identifier_error(
                packet_identifier,
                operation=operation,
                packet_type=packet_type,
            )
            if packet_identifier_error is not None:
                self.invalidate_protocol(packet_identifier_error)
                return paho.MQTT_ERR_PROTOCOL
            with self._acknowledgement_lock:
                if properties_packet_type == PacketTypes.SUBACK:
                    matched = (
                        packet_identifier in self._pending_subscribes
                        or packet_identifier in self._retired_subscribe_mids
                    )
                    registration_in_progress = self._starting_subscribe > 0
                else:
                    matched = (
                        packet_identifier in self._pending_unsubscribe_acknowledgements
                        or packet_identifier in self._retired_unsubscribe_mids
                    )
                    registration_in_progress = self._starting_unsubscribe > 0
            if not matched and not registration_in_progress:
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="unmatched_packet_identifier",
                )
            properties = Properties(properties_packet_type)  # type: ignore[no-untyped-call]
            _, consumed = properties.unpack(packet[2:])  # type: ignore[no-untyped-call]
            if consumed <= 0 or consumed > remaining_length - 2:
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="malformed_properties",
                )
            reason_codes = packet[2 + consumed :]
            if not reason_codes:
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="missing_reason_code",
                )
            if len(reason_codes) != 1:
                return self._reject_subscription_ack_protocol(
                    operation=operation,
                    packet_type=packet_type,
                    classifier="wrong_cardinality",
                )
            for reason_code in reason_codes:
                if properties_packet_type == PacketTypes.SUBACK and reason_code == 0x02:
                    return self._reject_subscription_ack_protocol(
                        operation=operation,
                        packet_type=packet_type,
                        classifier="unlisted_success",
                        reason_code=reason_code,
                    )
                try:
                    ReasonCode(properties_packet_type, identifier=reason_code)
                except Exception:
                    return self._reject_subscription_ack_protocol(
                        operation=operation,
                        packet_type=packet_type,
                        classifier=(
                            "unmapped_failure" if reason_code >= 0x80 else "unlisted_success"
                        ),
                        reason_code=reason_code,
                    )
        except Exception:
            return self._reject_subscription_ack_protocol(
                operation=operation,
                packet_type=packet_type,
                classifier="malformed_properties",
            )
        return None

    def _reject_subscription_ack_protocol(
        self,
        *,
        operation: str,
        packet_type: str,
        classifier: str,
        reason_code: int | None = None,
    ) -> object:
        error = _ack_protocol_error(
            operation=operation,
            packet_type=packet_type,
            classifier=classifier,
            reason_code=reason_code,
        )
        self.invalidate_protocol(error)
        return paho.MQTT_ERR_PROTOCOL

    async def subscribe(
        self,
        /,
        topic: SubscribeTopic,
        qos: int = 0,
        options: SubscribeOptions | None = None,
        properties: Properties | None = None,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> tuple[int, ...] | list[ReasonCode]:
        """Subscribe while preserving an ACK received during MID registration."""

        semaphore = self._outgoing_calls_sem
        if semaphore is None:
            return await self._subscribe_with_acknowledgement(
                topic,
                qos,
                options,
                properties,
                *args,
                timeout=timeout,
                **kwargs,
            )
        async with semaphore:
            return await self._subscribe_with_acknowledgement(
                topic,
                qos,
                options,
                properties,
                *args,
                timeout=timeout,
                **kwargs,
            )

    async def _subscribe_with_acknowledgement(
        self,
        topic: SubscribeTopic,
        qos: int,
        options: SubscribeOptions | None,
        properties: Properties | None,
        *args: Any,
        timeout: float | None,
        **kwargs: Any,
    ) -> tuple[int, ...] | list[ReasonCode]:
        acknowledgement: asyncio.Future[tuple[int, ...] | list[ReasonCode]] = asyncio.Future()
        with self._acknowledgement_lock:
            self._starting_subscribe += 1
            try:
                result, mid = self._client.subscribe(
                    topic,
                    qos,
                    options,
                    properties,
                    *args,
                    **kwargs,
                )
            finally:
                self._starting_subscribe -= 1
            if result != paho.MQTT_ERR_SUCCESS or mid is None:
                self._early_subscribe_acknowledgements.clear()
                raise MqttCodeError(result, "Could not subscribe to topic")
            early_acknowledgement = self._early_subscribe_acknowledgements.pop(mid, _MISSING)
            if self._early_subscribe_acknowledgements:
                self._early_subscribe_acknowledgements.clear()
                unmatched = _ack_protocol_error(
                    operation="mqtt.subscribe",
                    packet_type="SUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(unmatched)
                raise unmatched
            protocol_failure = self._protocol_failure_error
            if protocol_failure is not None:
                raise protocol_failure
            if mid in self._retired_subscribe_mids:
                reused = _ack_protocol_error(
                    operation="mqtt.subscribe",
                    packet_type="SUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(reused)
                raise reused
            if early_acknowledgement is not _MISSING:
                return cast("tuple[int, ...] | list[ReasonCode]", early_acknowledgement)
            if mid in self._pending_calls:
                raise RuntimeError("there is already a pending MQTT call for this identifier")
            self._pending_subscribes[mid] = acknowledgement
        acknowledgement_observed = False
        try:
            acknowledgement_result = await self._wait_for(acknowledgement, timeout=timeout)
            acknowledgement_observed = True
            return acknowledgement_result
        finally:
            with self._acknowledgement_lock:
                self._pending_subscribes.pop(mid, None)
                if not acknowledgement_observed and not self._disconnected.done():
                    self._retired_subscribe_mids.add(mid)

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None = None,
    ) -> None:
        if bool(getattr(flags, "session_present", False)):
            self._reject_connack_protocol("session_present")
            self._force_disconnect()
            return
        server_reference_present = bool(
            properties is not None and getattr(properties, "ServerReference", None) is not None
        )
        if server_reference_present:
            # A Server Reference may contain an operational endpoint. Retain
            # only its presence and reject even an otherwise successful
            # CONNACK before aiomqtt can expose connection readiness.
            self._server_reference_present = True
            if not self._connected.done():
                self._connected.set_exception(MqttConnectError(0x9C))
            self._force_disconnect()
            return
        super()._on_connect(client, userdata, flags, reason_code, properties)

    async def publish(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
        *args: Any,
        timeout: float | None = None,
        on_send_started: PublishStartedCallback | None = None,
        **kwargs: Any,
    ) -> None:
        await self.publish_with_completion(
            topic,
            payload,
            qos,
            retain,
            properties,
            *args,
            timeout=timeout,
            on_send_started=on_send_started,
            **kwargs,
        )

    async def publish_with_completion(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
        *args: Any,
        timeout: float | None = None,
        on_send_started: PublishStartedCallback | None = None,
        **kwargs: Any,
    ) -> _PublishCompletion:
        semaphore = self._outgoing_calls_sem
        if semaphore is None:
            acknowledgement, completion = await self._publish_with_acknowledgement(
                topic,
                payload,
                qos,
                retain,
                properties,
                *args,
                timeout=timeout,
                on_send_started=on_send_started,
                **kwargs,
            )
        else:
            async with semaphore:
                acknowledgement, completion = await self._publish_with_acknowledgement(
                    topic,
                    payload,
                    qos,
                    retain,
                    properties,
                    *args,
                    timeout=timeout,
                    on_send_started=on_send_started,
                    **kwargs,
                )
        try:
            _ensure_publication_accepted(acknowledgement)
        except ProtocolError as error:
            if error.details.get("classifier") == "non_numeric_reason_code":
                self.invalidate_protocol(error)
                raise _PublicationProtocolFailure(error) from None
            raise
        protocol_failure = self._protocol_failure_error
        if protocol_failure is not None:
            raise _PublicationProtocolFailure(protocol_failure)
        return completion

    async def _publish_with_acknowledgement(
        self,
        topic: str,
        payload: Any,
        qos: int,
        retain: bool,
        properties: Properties | None,
        *args: Any,
        timeout: float | None,
        on_send_started: PublishStartedCallback | None,
        **kwargs: Any,
    ) -> tuple[object, _PublishCompletion]:
        publish_task = asyncio.current_task()
        cancellation_baseline = publish_task.cancelling() if publish_task is not None else 0
        acknowledgement: asyncio.Future[object] = asyncio.Future()
        with self._acknowledgement_lock:
            self._starting_publish += 1
            try:
                info = self._client.publish(
                    topic,
                    payload,
                    qos,
                    retain,
                    properties,
                    *args,
                    **kwargs,
                )
            finally:
                self._starting_publish -= 1
            if info.rc != paho.MQTT_ERR_SUCCESS:
                raise MqttCodeError(info.rc, "Could not publish message")
            if on_send_started is not None:
                on_send_started()
            mid = info.mid
            packet_identifier_error = _packet_identifier_error(
                mid,
                operation="mqtt.publish",
                packet_type="PUBACK",
            )
            if packet_identifier_error is not None:
                self.invalidate_protocol(packet_identifier_error)
                raise _PublicationProtocolFailure(packet_identifier_error)
            if mid in self._retired_publish_mids:
                reused = _ack_protocol_error(
                    operation="mqtt.publish",
                    packet_type="PUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(reused)
                raise _PublicationProtocolFailure(reused)
            protocol_failure = self._protocol_failure_error
            if protocol_failure is not None:
                raise _PublicationProtocolFailure(protocol_failure)
            early_acknowledgement = self._early_publish_acknowledgements.pop(mid, _MISSING)
            if self._early_publish_acknowledgements:
                unmatched = _ack_protocol_error(
                    operation="mqtt.publish",
                    packet_type="PUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(unmatched)
                raise _PublicationProtocolFailure(unmatched)
            if early_acknowledgement is not _MISSING:
                return early_acknowledgement, _PublishCompletion.COMPLETED
            if info.is_published():
                return _MQTT_SUCCESS, _PublishCompletion.COMPLETED
            self._pending_publish_acknowledgements[mid] = acknowledgement
        acknowledgement_observed = False
        try:
            try:
                async with asyncio.timeout(timeout):
                    result = await asyncio.shield(acknowledgement)
                    acknowledgement_observed = True
                    return result, _PublishCompletion.COMPLETED
            except asyncio.CancelledError:
                if acknowledgement.done() and not acknowledgement.cancelled():
                    # A recorded PUBACK is stronger delivery evidence than a
                    # simultaneous caller cancellation.  Because this branch
                    # deliberately suppresses one CancelledError, balance
                    # exactly one request added at this await boundary.  Any
                    # additional cancellation debt remains owned by its scope.
                    if publish_task is None or publish_task.cancelling() <= cancellation_baseline:
                        raise
                    publish_task.uncancel()
                    result = acknowledgement.result()
                    acknowledgement_observed = True
                    return result, _PublishCompletion.COMPLETED_AFTER_CANCELLATION
                raise
            except TimeoutError:
                if acknowledgement.done() and not acknowledgement.cancelled():
                    result = acknowledgement.result()
                    acknowledgement_observed = True
                    return result, _PublishCompletion.COMPLETED
                raise _PublicationAcknowledgementTimeout() from None
        finally:
            with self._acknowledgement_lock:
                self._pending_publish_acknowledgements.pop(mid, None)
                if not acknowledgement_observed:
                    self._retired_publish_mids.add(mid)

    def _on_disconnect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None = None,
    ) -> None:
        # Retain only the presence of an operator-reviewed redirect. The
        # Server Reference value can contain an operational endpoint and must
        # never be stored or surfaced by the public client.
        if _reason_code_value(reason_code) in _CONNACK_SERVER_REFERENCE or (
            properties is not None and getattr(properties, "ServerReference", None) is not None
        ):
            self._server_reference_present = True
        super()._on_disconnect(client, userdata, flags, reason_code, properties)
        with self._acknowledgement_lock:
            pending_publications = tuple(self._pending_publish_acknowledgements.values())
            pending_subscriptions = tuple(self._pending_subscribes.values())
            pending_unsubscriptions = tuple(self._pending_unsubscribe_acknowledgements.values())
            self._retired_subscribe_mids.clear()
            self._retired_unsubscribe_mids.clear()
        for publication_acknowledgement in pending_publications:
            self._loop.call_soon_threadsafe(
                _fail_acknowledgement,
                publication_acknowledgement,
                _PublicationConnectionLost(),
            )
        for subscription_acknowledgement in pending_subscriptions:
            self._loop.call_soon_threadsafe(
                _fail_acknowledgement,
                cast("asyncio.Future[object]", subscription_acknowledgement),
                _SubscriptionConnectionLost(),
            )
        for unsubscription_acknowledgement in pending_unsubscriptions:
            self._loop.call_soon_threadsafe(
                _fail_acknowledgement,
                unsubscription_acknowledgement,
                _UnsubscriptionConnectionLost(),
            )

    async def unsubscribe(
        self,
        topic: str | list[str],
        properties: Properties | None = None,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        semaphore = self._outgoing_calls_sem
        if semaphore is None:
            acknowledgement = await self._unsubscribe_with_acknowledgement(
                topic,
                properties,
                *args,
                timeout=timeout,
                **kwargs,
            )
        else:
            async with semaphore:
                acknowledgement = await self._unsubscribe_with_acknowledgement(
                    topic,
                    properties,
                    *args,
                    timeout=timeout,
                    **kwargs,
                )
        _ensure_unsubscription_accepted(acknowledgement)

    async def _unsubscribe_with_acknowledgement(
        self,
        topic: str | list[str],
        properties: Properties | None,
        *args: Any,
        timeout: float | None,
        **kwargs: Any,
    ) -> object:
        acknowledgement: asyncio.Future[object] = asyncio.Future()
        with self._acknowledgement_lock:
            self._starting_unsubscribe += 1
            try:
                result, mid = self._client.unsubscribe(topic, properties, *args, **kwargs)
            finally:
                self._starting_unsubscribe -= 1
            if result != paho.MQTT_ERR_SUCCESS or mid is None:
                if mid is not None:
                    self._retired_unsubscribe_mids.add(mid)
                self._early_unsubscribe_acknowledgements.clear()
                raise MqttCodeError(result, "Could not unsubscribe from topic")
            early_acknowledgement = self._early_unsubscribe_acknowledgements.pop(mid, _MISSING)
            unexpected_early_identifiers = set(self._early_unsubscribe_acknowledgements).difference(
                self._retired_unsubscribe_mids
            )
            self._early_unsubscribe_acknowledgements.clear()
            if unexpected_early_identifiers:
                unmatched = _ack_protocol_error(
                    operation="mqtt.unsubscribe",
                    packet_type="UNSUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(unmatched)
                raise unmatched
            protocol_failure = self._protocol_failure_error
            if protocol_failure is not None:
                raise protocol_failure
            if mid in self._retired_unsubscribe_mids:
                reused = _ack_protocol_error(
                    operation="mqtt.unsubscribe",
                    packet_type="UNSUBACK",
                    classifier="unmatched_packet_identifier",
                )
                self.invalidate_protocol(reused)
                raise reused
            if early_acknowledgement is not _MISSING:
                return early_acknowledgement
            self._pending_unsubscribe_acknowledgements[mid] = acknowledgement
        acknowledgement_observed = False
        try:
            acknowledgement_result = await self._wait_for(acknowledgement, timeout=timeout)
            acknowledgement_observed = True
            return acknowledgement_result
        finally:
            with self._acknowledgement_lock:
                self._pending_unsubscribe_acknowledgements.pop(mid, None)
                if not acknowledgement_observed and not self._disconnected.done():
                    self._retired_unsubscribe_mids.add(mid)

    def _on_publish(
        self,
        client: paho.Client,
        userdata: Any,
        mid: int,
        reason_code: ReasonCode | None,
        properties: Properties | None,
    ) -> None:
        del client, userdata, properties
        packet_identifier_error = _packet_identifier_error(
            mid,
            operation="mqtt.publish",
            packet_type="PUBACK",
        )
        if packet_identifier_error is not None:
            self.invalidate_protocol(packet_identifier_error)
            return
        with self._acknowledgement_lock:
            if self._protocol_failure_error is not None:
                return
            if mid in self._retired_publish_mids:
                self._retired_publish_mids.remove(mid)
                return
            pending = self._pending_publish_acknowledgements.get(mid)
            if pending is not None:
                self._loop.call_soon_threadsafe(_complete_acknowledgement, pending, reason_code)
                return
            if self._starting_publish:
                if mid in self._early_publish_acknowledgements:
                    self.invalidate_protocol(
                        _ack_protocol_error(
                            operation="mqtt.publish",
                            packet_type="PUBACK",
                            classifier="unmatched_packet_identifier",
                        )
                    )
                    return
                self._early_publish_acknowledgements[mid] = reason_code
                return
        self.invalidate_protocol(
            _ack_protocol_error(
                operation="mqtt.publish",
                packet_type="PUBACK",
                classifier="unmatched_packet_identifier",
            )
        )

    def invalidate_protocol(self, error: ProtocolError) -> None:
        """Invalidate this clean-session generation after a safe parser failure."""

        with self._acknowledgement_lock:
            if self._protocol_failure_error is not None:
                return
            self._protocol_failure_error = error
        self._loop.call_soon_threadsafe(self._apply_protocol_failure, error)

    def _apply_protocol_failure(self, error: ProtocolError) -> None:
        with self._acknowledgement_lock:
            publish_pending = tuple(self._pending_publish_acknowledgements.values())
            unsubscribe_pending = tuple(self._pending_unsubscribe_acknowledgements.values())
            subscribe_pending = tuple(self._pending_subscribes.values())
        for publish_acknowledgement in publish_pending:
            _fail_acknowledgement(
                publish_acknowledgement,
                _PublicationProtocolFailure(error),
            )
        for unsubscribe_acknowledgement in unsubscribe_pending:
            _fail_acknowledgement(unsubscribe_acknowledgement, error)
        for subscribe_acknowledgement in subscribe_pending:
            _fail_acknowledgement(subscribe_acknowledgement, error)
        self._force_disconnect()

    def _on_unsubscribe(
        self,
        client: paho.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None = None,
    ) -> None:
        del client, userdata, properties
        with self._acknowledgement_lock:
            if mid in self._retired_unsubscribe_mids:
                self._retired_unsubscribe_mids.remove(mid)
                return
            pending = self._pending_unsubscribe_acknowledgements.get(mid)
            if pending is not None:
                self._loop.call_soon_threadsafe(
                    _complete_acknowledgement,
                    pending,
                    reason_codes,
                )
                return
            if self._starting_unsubscribe:
                if mid in self._early_unsubscribe_acknowledgements:
                    self.invalidate_protocol(
                        _ack_protocol_error(
                            operation="mqtt.unsubscribe",
                            packet_type="UNSUBACK",
                            classifier="unmatched_packet_identifier",
                        )
                    )
                    return
                self._early_unsubscribe_acknowledgements[mid] = list(reason_codes)
                return
        self.invalidate_protocol(
            _ack_protocol_error(
                operation="mqtt.unsubscribe",
                packet_type="UNSUBACK",
                classifier="unmatched_packet_identifier",
            )
        )

    def _on_subscribe(
        self,
        client: paho.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None = None,
    ) -> None:
        del client, userdata, properties
        with self._acknowledgement_lock:
            if mid in self._retired_subscribe_mids:
                self._retired_subscribe_mids.remove(mid)
                return
            pending = self._pending_subscribes.get(mid)
            if pending is not None:
                self._loop.call_soon_threadsafe(
                    _complete_acknowledgement,
                    cast("asyncio.Future[object]", pending),
                    reason_codes,
                )
                return
            if self._starting_subscribe:
                if mid in self._early_subscribe_acknowledgements:
                    self.invalidate_protocol(
                        _ack_protocol_error(
                            operation="mqtt.subscribe",
                            packet_type="SUBACK",
                            classifier="unmatched_packet_identifier",
                        )
                    )
                    return
                self._early_subscribe_acknowledgements[mid] = list(reason_codes)
                return
        self.invalidate_protocol(
            _ack_protocol_error(
                operation="mqtt.subscribe",
                packet_type="SUBACK",
                classifier="unmatched_packet_identifier",
            )
        )

    async def invalidate_session(self, *, timeout: float) -> None:
        """Disconnect so a refused unsubscribe cannot leave clean-session state."""

        if self._disconnected.done():
            if self._disconnected.cancelled() or self._disconnected.exception() is not None:
                self._force_disconnect()
            return
        try:
            result = self._client.disconnect()
        except Exception:
            self._force_disconnect()
            return
        if result == paho.MQTT_ERR_SUCCESS:
            try:
                await self._wait_for(asyncio.shield(self._disconnected), timeout=timeout)
                return
            except asyncio.CancelledError:
                self._force_disconnect()
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                return
            except Exception:
                pass
        self._force_disconnect()

    def _force_disconnect(self) -> None:
        """Close the exact-pinned Paho socket and wake aiomqtt's iterator."""

        with self._acknowledgement_lock:
            self._retired_subscribe_mids.clear()
            self._retired_unsubscribe_mids.clear()
        with contextlib.suppress(Exception):
            # paho-mqtt is a reviewed direct dependency pinned to 2.1.0 because
            # its private socket-close hook is the only synchronous way to both
            # unregister aiomqtt's socket callbacks and close the descriptor.
            self._client._sock_close()
            # Paho closes the descriptor in its own finally block.  Callback
            # failures must not prevent the local disconnect barrier below.
        if self._disconnected.cancelled():
            self._disconnected = self._loop.create_future()
            self._disconnected.set_result(None)
        elif not self._disconnected.done():
            self._disconnected.set_result(None)


class MQTTTransport:
    """Reconnect aiomqtt while exposing only bounded, fixed-family operations."""

    def __init__(
        self,
        config: ECNConfig,
        on_connection_change: ConnectionChangeCallback | None = None,
        *,
        on_recovery_change: RecoveryChangeCallback | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stable_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._reconnect_policy = config.reconnect_policy
        self._on_connection_change = on_connection_change
        self._on_recovery_change = on_recovery_change
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._stable_sleeper = stable_sleeper
        self._random_source = random_source
        # Match the pinned clean-session client-ID form: integration plus the
        # first eight hexadecimal characters of a random UUID.
        self._identifier = f"{config.integration_name}-{uuid4().hex[:8]}"
        self._subscriptions: dict[SubscriptionHandle, _Subscription] = {}
        self._subscription_setup_tasks: set[asyncio.Task[Any]] = set()
        self._broker_operation_tasks: set[asyncio.Task[Any]] = set()
        self._publish_tasks: set[asyncio.Task[Any]] = set()
        self._subscription_lock = asyncio.Lock()
        self._client: _MQTTV5Client | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._connected_event = asyncio.Event()
        self._connection_lost_event = asyncio.Event()
        self._connection_lost_event.set()
        self._ready_event = asyncio.Event()
        self._startup_failure_event = asyncio.Event()
        self._retry_wakeup = asyncio.Event()
        self._connected = False
        self._strict_ready = False
        self._connection_generation = 0
        self._stable_reset_task: asyncio.Task[None] | None = None
        self._closing = False
        self._last_error: BaseException | None = None
        self._ever_ready = False
        self._ready_since: float | None = None
        self._recovery_started_at: float | None = None
        self._attempt_count = 0
        self._retry_delay_cap_seconds: float | None = None
        self._wakeup_generation = 0
        self._retry_waiting = False
        self._waiting_for_credentials = False
        self._auth_immediate_retry_used = False
        self._episode_restart_requested = False
        self._protocol_failure_signature: tuple[str, ...] | None = None
        self._protocol_failure_count = 0
        self._attempt_operation: ConnectionFailureOperation | None = None
        self._loading_tls_material = False
        self._recovery_snapshot = _RecoverySnapshot(ConnectionRetryState.INACTIVE, 0)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        """Return whether CONNACK and all active subscription restores completed."""

        return self._strict_ready

    @property
    def connection_generation(self) -> int:
        """Return the current transport connection generation."""

        return self._connection_generation

    @property
    def recovery_snapshot(self) -> _RecoverySnapshot:
        """Return the private sanitized state used by the public client facade."""

        return self._recovery_snapshot

    async def start(self) -> None:
        """Start the reconnect supervisor and wait for initial broker readiness."""

        if self._supervisor is not None and not self._supervisor.done():
            await self._wait_until_ready()
            return
        self._closing = False
        self._last_error = None
        self._startup_failure_event.clear()
        self._connected_event.clear()
        self._ready_event.clear()
        self._retry_wakeup.clear()
        self._ever_ready = False
        self._ready_since = None
        self._recovery_started_at = None
        self._attempt_count = 0
        self._retry_delay_cap_seconds = None
        self._retry_waiting = False
        self._waiting_for_credentials = False
        self._auth_immediate_retry_used = False
        self._episode_restart_requested = False
        self._protocol_failure_signature = None
        self._protocol_failure_count = 0
        self._attempt_operation = None
        self._loading_tls_material = False
        self._recovery_snapshot = _RecoverySnapshot(ConnectionRetryState.INACTIVE, 0)
        self._supervisor = asyncio.create_task(
            self._run(),
            name="picogrid-ecn-mqtt-transport",
        )
        await self._wait_until_ready()

    def notify_credentials_changed(self) -> None:
        """Wake one credential-blocked retry without creating another supervisor."""

        if self._closing or self._connected:
            return
        if self._recovery_snapshot.state is ConnectionRetryState.WAITING_FOR_CREDENTIALS:
            self._signal_retry_wakeup()
            return
        self._schedule_new_recovery_episode(
            frozenset(
                {
                    ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
                    ConnectionFailureCode.AUTHENTICATION_REJECTED,
                }
            )
        )

    def request_retry(self) -> None:
        """Wake scheduled recovery or restart one exhausted recovery episode."""

        if self._closing or self._connected:
            return
        if self._recovery_snapshot.state is ConnectionRetryState.SCHEDULED:
            self._signal_retry_wakeup()
            return
        if not (
            self._recovery_snapshot.state is ConnectionRetryState.TERMINAL
            and self._recovery_snapshot.failure_code is ConnectionFailureCode.RETRY_EXHAUSTED
        ):
            return
        self._schedule_new_recovery_episode(frozenset({ConnectionFailureCode.RETRY_EXHAUSTED}))

    def _schedule_new_recovery_episode(
        self,
        allowed_codes: frozenset[ConnectionFailureCode],
    ) -> None:
        if not (
            self._recovery_snapshot.state is ConnectionRetryState.TERMINAL
            and self._recovery_snapshot.failure_code in allowed_codes
        ):
            return
        supervisor = self._supervisor
        if supervisor is not None and not supervisor.done():
            if not self._episode_restart_requested:
                self._episode_restart_requested = True
                supervisor.add_done_callback(
                    lambda _supervisor: self._start_new_recovery_episode(allowed_codes)
                )
            return
        self._start_new_recovery_episode(allowed_codes)

    def _start_new_recovery_episode(
        self,
        allowed_codes: frozenset[ConnectionFailureCode],
    ) -> None:
        self._episode_restart_requested = False
        if self._closing or self._connected:
            return
        if not (
            self._recovery_snapshot.state is ConnectionRetryState.TERMINAL
            and self._recovery_snapshot.failure_code in allowed_codes
        ):
            return
        self._last_error = None
        self._startup_failure_event.clear()
        self._recovery_started_at = None
        self._attempt_count = 0
        self._retry_delay_cap_seconds = None
        self._auth_immediate_retry_used = False
        self._protocol_failure_signature = None
        self._protocol_failure_count = 0
        self._supervisor = asyncio.create_task(
            self._run(),
            name="picogrid-ecn-mqtt-transport",
        )

    def _signal_retry_wakeup(self) -> None:
        self._wakeup_generation += 1
        self._retry_wakeup.set()

    async def _wait_until_ready(self) -> None:
        try:
            async with asyncio.timeout(self._config.connection_timeout):
                while not self._strict_ready and not self._startup_failure_event.is_set():
                    ready_waiter = asyncio.create_task(self._ready_event.wait())
                    failure_waiter = asyncio.create_task(self._startup_failure_event.wait())
                    try:
                        await asyncio.wait(
                            (ready_waiter, failure_waiter),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for waiter in (ready_waiter, failure_waiter):
                            if not waiter.done():
                                waiter.cancel()
                        await asyncio.gather(
                            ready_waiter,
                            failure_waiter,
                            return_exceptions=True,
                        )
        except TimeoutError:
            failure: BaseException
            operation = self._attempt_operation
            snapshot = self._recovery_snapshot
            if self._loading_tls_material:
                failure = _TLSMaterialLoadTimeout()
                classification = _FailureClassification(
                    ConnectionFailureCode.CONFIGURATION_INVALID,
                    ConnectionFailureOperation.RESOLVE_CREDENTIALS,
                    terminal=True,
                )
            elif operation is ConnectionFailureOperation.RESOLVE_CREDENTIALS:
                failure = _CredentialResolutionTimeout()
                classification = _FailureClassification(
                    ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
                    ConnectionFailureOperation.RESOLVE_CREDENTIALS,
                    terminal=True,
                )
            elif operation is not None:
                failure = _ConnectionPhaseTimeout(operation)
                classification = _connection_phase_timeout_classification(
                    operation,
                    terminal=True,
                )
            elif snapshot.failure_code is not None and snapshot.failure_operation is not None:
                failure = self._last_error or TimeoutError()
                classification = _FailureClassification(
                    snapshot.failure_code,
                    snapshot.failure_operation,
                    terminal=True,
                )
            else:
                operation = ConnectionFailureOperation.CONNECT
                failure = _ConnectionPhaseTimeout(operation)
                classification = _connection_phase_timeout_classification(
                    operation,
                    terminal=True,
                )
            self._last_error = failure
            await self._set_terminal_recovery(classification, failure)
            await self._raise_start_failure(timed_out=True)
        if not self._strict_ready:
            await self._raise_start_failure(timed_out=False)

    async def _raise_start_failure(self, *, timed_out: bool) -> None:
        cause = self._last_error
        failure_code = self._recovery_snapshot.failure_code
        failure_details = {"failure_code": failure_code.value} if failure_code is not None else None
        if failure_code is not ConnectionFailureCode.RETRY_EXHAUSTED:
            await self.close()
        if isinstance(cause, TransportBoundaryError):
            raise cause
        if isinstance(cause, _LegionCredentialError):
            raise AuthenticationError(
                cause.message,
                code=cause.code,
                operation=cause.operation,
            ) from None
        if failure_code is ConnectionFailureCode.CONFIGURATION_INVALID or isinstance(
            cause, ConfigurationError
        ):
            raise ConfigurationError(
                "MQTT local connection material is unusable",
                operation="mqtt.start",
            ) from None
        if failure_code is ConnectionFailureCode.CREDENTIALS_UNAVAILABLE or isinstance(
            cause, AuthenticationError
        ):
            raise AuthenticationError(
                "MQTT credentials are unavailable or unusable",
                operation="mqtt.start",
            ) from None
        if failure_code is ConnectionFailureCode.AUTHENTICATION_REJECTED or (
            cause is not None and _is_authentication_rejection(cause)
        ):
            message = (
                "MQTT authentication did not become ready before the connection timeout"
                if timed_out
                else "MQTT broker rejected authentication"
            )
            raise AuthenticationError(message, operation="mqtt.start") from None
        if failure_code in {
            ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED,
            ConnectionFailureCode.SUBSCRIPTION_DENIED,
        } or isinstance(cause, AuthorizationError):
            raise AuthorizationError(
                "MQTT connection or subscription authorization was rejected",
                operation="mqtt.start",
            ) from None
        if failure_code in {
            ConnectionFailureCode.CONNECTION_RESOURCE_LIMIT,
            ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT,
        } or isinstance(cause, ResourceLimitError):
            raise ResourceLimitError(
                "MQTT connection or subscription exceeded a broker resource limit",
                operation="mqtt.start",
            ) from None
        if failure_code is ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED:
            raise ConnectionError(
                "MQTT TLS peer certificate verification failed",
                operation="mqtt.start",
                details=failure_details,
            ) from None
        if failure_code is ConnectionFailureCode.PROTOCOL_FAILURE or isinstance(
            cause, ProtocolError
        ):
            raise ProtocolError(
                "MQTT connection failed protocol validation",
                operation="mqtt.start",
            ) from None
        raise ConnectionError(
            "MQTT did not become ready before the connection timeout",
            operation="mqtt.start",
            details=failure_details,
        ) from None

    async def close(self) -> None:
        """Stop reconnecting, disconnect aiomqtt, and remove temporary credentials."""

        self._closing = True
        self._wakeup_generation += 1
        self._retry_wakeup.set()
        if not self._connected:
            self._startup_failure_event.set()
        task, self._supervisor = self._supervisor, None
        stable_reset_task = self._stable_reset_task
        current = asyncio.current_task()
        setup_tasks = tuple(task for task in self._subscription_setup_tasks if task is not current)
        broker_tasks = tuple(task for task in self._broker_operation_tasks if task is not current)
        publish_tasks = tuple(task for task in self._publish_tasks if task is not current)
        pending = set(setup_tasks) | set(broker_tasks) | set(publish_tasks)
        if task is not None and not task.done():
            pending.add(task)
        if (
            stable_reset_task is not None
            and stable_reset_task is not current
            and not stable_reset_task.done()
        ):
            pending.add(stable_reset_task)
        for pending_task in pending:
            pending_task.cancel()
        try:
            if task is not None and task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
            if pending:
                try:
                    async with asyncio.timeout(self._config.shutdown_timeout):
                        await asyncio.gather(*pending, return_exceptions=True)
                except TimeoutError:
                    for pending_task in pending:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.sleep(0)
                    raise ECNTimeoutError(
                        "MQTT transport shutdown exceeded shutdown_timeout",
                        operation="mqtt.close",
                    ) from None
        finally:
            self._client = None
            self._subscriptions.clear()
            self._subscription_setup_tasks.clear()
            self._broker_operation_tasks.clear()
            self._publish_tasks.clear()
            self._stable_reset_task = None
            self._retry_waiting = False
            self._waiting_for_credentials = False
            self._episode_restart_requested = False
            self._mark_not_ready()
            await self._set_connected(False)

    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 1,
        *,
        on_send_started: PublishStartedCallback | None = None,
        expected_connection_generation: int | None = None,
    ) -> _PublishCompletion:
        """Publish bytes to one exact supported topic; this adapter remains private."""

        publish_task = asyncio.current_task()
        assert publish_task is not None
        cancellation_baseline = publish_task.cancelling()
        validate_publish_topic(topic)
        if not isinstance(payload, bytes):
            raise ValidationError("MQTT payload must be bytes", operation="mqtt.publish")
        if qos not in {0, 1}:
            raise ValidationError("MQTT QoS must be 0 or 1", operation="mqtt.publish")
        if len(payload) > self._config.maximum_payload_size:
            raise ResourceLimitError(
                "MQTT payload exceeds maximum_payload_size",
                operation="mqtt.publish",
                details={
                    "payload_size": len(payload),
                    "maximum_payload_size": self._config.maximum_payload_size,
                },
            )
        if (
            expected_connection_generation is not None
            and expected_connection_generation != self._connection_generation
        ):
            raise DeliveryError(
                "MQTT publication was not sent on a stale connection generation",
                delivery_phase=DeliveryPhase.NOT_SENT,
                operation="mqtt.publish",
            )
        client = self._client
        if self._closing or not self._connected or client is None:
            raise DeliveryError(
                "MQTT publication was not sent",
                delivery_phase=DeliveryPhase.NOT_SENT,
                operation="mqtt.publish",
            )
        is_supervisor_publish = publish_task is self._supervisor
        self._publish_tasks.add(publish_task)
        send_started = False

        def mark_send_started() -> None:
            nonlocal send_started
            send_started = True
            if on_send_started is not None:
                try:
                    on_send_started()
                except Exception:
                    logger.warning("MQTT publication progress callback failed")

        try:
            completion = await client.publish_with_completion(
                topic,
                payload,
                qos=qos,
                timeout=self._config.operation_timeout,
                on_send_started=mark_send_started,
            )
        except _PublicationProtocolFailure as exc:
            raise OutcomeUnknownError(
                "MQTT publication acknowledgment could not be associated safely",
                delivery_phase=(
                    DeliveryPhase.LOCAL_SEND_UNCERTAIN
                    if qos == 0
                    else DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
                ),
                operation="mqtt.publish",
                details=exc.error.details,
            ) from None
        except asyncio.CancelledError:
            if send_started and not is_supervisor_publish:
                # This layer replaces one caller cancellation with delivery
                # uncertainty.  Only a single request over a zero baseline is
                # unambiguously ours to consume; structured-concurrency scopes
                # retain ownership of pre-existing cancellation debt.
                if cancellation_baseline == 0 and publish_task.cancelling() == 1:
                    publish_task.uncancel()
                raise OutcomeUnknownError(
                    (
                        "MQTT publication outcome is unknown after transport shutdown"
                        if self._closing
                        else "MQTT publication outcome is unknown after caller cancellation"
                    ),
                    delivery_phase=(
                        DeliveryPhase.LOCAL_SEND_UNCERTAIN
                        if qos == 0
                        else DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
                    ),
                    operation="mqtt.publish",
                ) from None
            raise
        except (aiomqtt.MqttError, OSError):
            if send_started:
                raise OutcomeUnknownError(
                    "MQTT publication outcome is unknown after local send began",
                    delivery_phase=(
                        DeliveryPhase.LOCAL_SEND_UNCERTAIN
                        if qos == 0
                        else DeliveryPhase.BROKER_ACKNOWLEDGMENT_PENDING
                    ),
                    operation="mqtt.publish",
                ) from None
            raise DeliveryError(
                "MQTT publication was not sent",
                delivery_phase=DeliveryPhase.NOT_SENT,
                operation="mqtt.publish",
            ) from None
        finally:
            self._publish_tasks.discard(publish_task)
        return completion

    async def subscribe(
        self,
        topic_filter: str,
        callback: MessageCallback,
        *,
        on_restore_failure: RestoreFailureCallback | None = None,
        restore_on_reconnect: bool = True,
        expected_connection_generation: int | None = None,
    ) -> SubscriptionHandle:
        """Register a callback for one fixed family or exact task path."""

        validate_subscription_filter(topic_filter)
        if not callable(callback):
            raise ValidationError("MQTT callback must be callable", operation="mqtt.subscribe")
        handle_token = uuid4()
        setup_task = asyncio.current_task()
        assert setup_task is not None
        self._subscription_setup_tasks.add(setup_task)
        try:
            async with self._subscription_lock:
                if (
                    expected_connection_generation is not None
                    and expected_connection_generation != self._connection_generation
                ):
                    raise ConnectionError(
                        "MQTT subscription did not open on a stale connection generation",
                        operation="mqtt.subscribe",
                    )
                opening_generation = self._connection_generation
                client = self._client
                if client is None or self._closing or not self._connected:
                    raise ConnectionError(
                        "MQTT transport is not connected",
                        operation="mqtt.subscribe",
                    )
                should_subscribe = not any(
                    subscription.topic_filter == topic_filter
                    for subscription in self._subscriptions.values()
                )
                if should_subscribe:
                    readiness_suspended = restore_on_reconnect and self._strict_ready
                    # Strict readiness includes every broker subscription that
                    # is currently being established.  Clear it before the
                    # SUBSCRIBE leaves this process so concurrent readiness
                    # waiters cannot complete before the matching SUBACK.
                    if readiness_suspended:
                        self._mark_not_ready()
                    broker_subscribe = asyncio.create_task(
                        client.subscribe(
                            topic_filter,
                            qos=1,
                            timeout=self._config.operation_timeout,
                        ),
                        name="picogrid-ecn-mqtt-subscribe",
                    )
                    self._track_broker_operation(broker_subscribe)
                    try:
                        if readiness_suspended:
                            await self._emit_recovery(self._recovery_snapshot)
                        reason_codes = await asyncio.shield(broker_subscribe)
                        _ensure_subscription_accepted(reason_codes)
                    except asyncio.CancelledError:
                        current_task = asyncio.current_task()
                        if current_task is None or current_task.cancelling() == 0:
                            raise
                        if self._closing:
                            raise
                        try:
                            reason_codes = await asyncio.shield(broker_subscribe)
                            _ensure_subscription_accepted(reason_codes)
                        except (aiomqtt.MqttError, OSError):
                            await self._invalidate_subscription_session(client)
                        except (AuthorizationError, ResourceLimitError):
                            await self._restore_live_subscription_readiness(
                                client,
                                opening_generation,
                                restore_readiness=readiness_suspended,
                            )
                        except ProtocolError:
                            await self._invalidate_subscription_session(client)
                        else:
                            with contextlib.suppress(
                                asyncio.CancelledError,
                                ECNClientError,
                                aiomqtt.MqttError,
                                OSError,
                            ):
                                await self._broker_unsubscribe(client, topic_filter)
                            await self._restore_live_subscription_readiness(
                                client,
                                opening_generation,
                                restore_readiness=readiness_suspended,
                            )
                        raise
                    except (AuthorizationError, ResourceLimitError):
                        await self._restore_live_subscription_readiness(
                            client,
                            opening_generation,
                            restore_readiness=readiness_suspended,
                        )
                        raise
                    except ProtocolError:
                        await self._invalidate_subscription_session(client)
                        raise
                    except (aiomqtt.MqttError, OSError):
                        await self._invalidate_subscription_session(client)
                        raise ConnectionError(
                            "MQTT subscription failed",
                            operation="mqtt.subscribe",
                        ) from None
                    if (
                        self._closing
                        or self._client is not client
                        or not self._connected
                        or self._connection_generation != opening_generation
                    ):
                        with contextlib.suppress(ECNClientError, aiomqtt.MqttError, OSError):
                            await self._broker_unsubscribe(client, topic_filter)
                        raise ConnectionError(
                            "MQTT transport closed while opening a subscription",
                            operation="mqtt.subscribe",
                        )
                    await self._restore_live_subscription_readiness(
                        client,
                        opening_generation,
                        restore_readiness=readiness_suspended,
                    )
                handle = SubscriptionHandle(handle_token, opening_generation)
                self._subscriptions[handle] = _Subscription(
                    topic_filter,
                    callback,
                    on_restore_failure,
                    restore_on_reconnect,
                )
        finally:
            self._subscription_setup_tasks.discard(setup_task)
        return handle

    async def wait_for_connection_loss(self, generation: int) -> None:
        """Wait until one observed transport generation is no longer connected."""

        if (
            not self._connected
            or generation != self._connection_generation
            or self._connection_lost_event.is_set()
        ):
            return
        await self._connection_lost_event.wait()

    async def unsubscribe(self, handle: object) -> None:
        """Remove one registration and unsubscribe when its filter has no consumers."""

        if not isinstance(handle, SubscriptionHandle):
            raise ValidationError(
                "invalid MQTT subscription handle",
                operation="mqtt.unsubscribe",
            )
        cleanup_task = asyncio.create_task(
            self._unsubscribe_handle(handle),
            name="picogrid-ecn-mqtt-subscription-cleanup",
        )
        self._subscription_setup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._subscription_cleanup_done)
        cleanup_waiter = asyncio.create_task(
            self._consume_expected_cleanup_error(cleanup_task),
            name="picogrid-ecn-mqtt-subscription-cleanup-waiter",
        )
        try:
            await asyncio.shield(cleanup_waiter)
            cleanup_task.result()
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() and not self._closing:
                with contextlib.suppress(
                    asyncio.CancelledError,
                    ECNClientError,
                    aiomqtt.MqttError,
                    OSError,
                ):
                    await asyncio.shield(cleanup_waiter)
                    cleanup_task.result()
            raise

    @staticmethod
    async def _consume_expected_cleanup_error(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(ECNClientError, aiomqtt.MqttError, OSError):
            await task

    def _subscription_cleanup_done(self, task: asyncio.Task[Any]) -> None:
        self._subscription_setup_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _unsubscribe_handle(self, handle: SubscriptionHandle) -> None:
        async with self._subscription_lock:
            removed = self._subscriptions.pop(handle, None)
            topic_filter = removed.topic_filter if removed is not None else None
            should_unsubscribe = topic_filter is not None and not any(
                subscription.topic_filter == topic_filter
                for subscription in self._subscriptions.values()
            )
            client = self._client
            if should_unsubscribe and topic_filter is not None and client is not None:
                broker_unsubscribe = asyncio.create_task(
                    self._unsubscribe_with_session_fallback(client, topic_filter),
                    name="picogrid-ecn-mqtt-unsubscribe",
                )
                self._track_broker_operation(broker_unsubscribe)
                try:
                    await asyncio.shield(broker_unsubscribe)
                except asyncio.CancelledError:
                    if not self._closing:
                        with contextlib.suppress(
                            asyncio.CancelledError,
                            ECNClientError,
                            aiomqtt.MqttError,
                            OSError,
                        ):
                            await asyncio.shield(broker_unsubscribe)
                    raise
                except (aiomqtt.MqttError, OSError):
                    raise ConnectionError(
                        "MQTT unsubscribe failed",
                        operation="mqtt.unsubscribe",
                    ) from None

    async def _broker_unsubscribe(self, client: _MQTTV5Client, topic_filter: str) -> None:
        broker_unsubscribe = asyncio.create_task(
            self._unsubscribe_with_session_fallback(client, topic_filter),
            name="picogrid-ecn-mqtt-unsubscribe",
        )
        self._track_broker_operation(broker_unsubscribe)
        await asyncio.shield(broker_unsubscribe)

    async def _invalidate_subscription_session(self, client: _MQTTV5Client) -> None:
        """Leave no ready session after an ambiguous SUBSCRIBE outcome."""

        self._mark_not_ready()
        try:
            await client.invalidate_session(timeout=self._config.operation_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if self._client is client:
                self._client = None
            await self._set_connected(False)

    async def _restore_live_subscription_readiness(
        self,
        client: _MQTTV5Client,
        generation: int,
        *,
        restore_readiness: bool,
    ) -> None:
        """Restore readiness after one live SUBSCRIBE reaches a definite outcome."""

        if (
            not restore_readiness
            or self._closing
            or self._client is not client
            or not self._connected
            or self._connection_generation != generation
        ):
            return
        self._mark_ready()
        await self._emit_recovery(self._recovery_snapshot)

    async def _unsubscribe_with_session_fallback(
        self,
        client: _MQTTV5Client,
        topic_filter: str,
    ) -> None:
        try:
            await client.unsubscribe(
                topic_filter,
                timeout=self._config.operation_timeout,
            )
        except asyncio.CancelledError:
            raise
        except (ECNClientError, aiomqtt.MqttError, OSError):
            try:
                await client.invalidate_session(timeout=self._config.operation_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._client is client:
                self._client = None
            await self._set_connected(False)
            raise

    def _track_broker_operation(self, task: asyncio.Task[Any]) -> None:
        self._broker_operation_tasks.add(task)
        task.add_done_callback(self._broker_operation_done)

    def _broker_operation_done(self, task: asyncio.Task[Any]) -> None:
        self._broker_operation_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _authentication(self) -> tuple[str | None, str | None]:
        auth = self._config.auth
        if not isinstance(auth, BearerTokenAuth):
            return None, None
        try:
            return await auth._resolve_credentials(self._config.integration_name)
        except asyncio.CancelledError:
            raise
        except _LegionCredentialError:
            raise
        except ValueError:
            if auth.token is not None:
                raise ConfigurationError(
                    "static bearer credential is invalid",
                    operation="mqtt.authenticate",
                ) from None
            raise AuthenticationError(
                "bearer credential provider returned unusable credentials",
                operation="mqtt.authenticate",
            ) from None
        except Exception:
            raise AuthenticationError(
                "bearer token provider failed",
                operation="mqtt.authenticate",
            ) from None

    def _new_client(
        self,
        hostname: str,
        username: str | None,
        password: str | None,
        timeout: float,
        *,
        ssl_context: ssl.SSLContext | None,
        connect_properties: Properties,
    ) -> _MQTTV5Client:
        return _MQTTV5Client(
            hostname=hostname,
            port=self._config.mqtt_port,
            username=username,
            password=password,
            identifier=self._identifier,
            protocol=cast("Any", paho.MQTTv5),
            timeout=timeout,
            max_queued_incoming_messages=self._config.watcher_buffer_size,
            max_concurrent_outgoing_calls=(self._config.maximum_outstanding_operations),
            tls_context=ssl_context,
            on_connection_phase=self._set_attempt_operation,
            require_reviewed_network=(self._config.plaintext_container_network is not None),
            clean_start=True,
            properties=connect_properties,
        )

    async def _run(self) -> None:
        try:
            while not self._closing:
                if not self._begin_attempt():
                    classification = self._budget_exhaustion_classification()
                    await self._set_terminal_recovery(
                        classification,
                        self._last_error
                        or ConnectionError(
                            "MQTT reconnect retry budget was exhausted",
                            operation="mqtt.connect",
                        ),
                    )
                    return

                client: _MQTTV5Client | None = None
                was_ready = False
                recovery_reset = False
                failure: BaseException | None = None
                await self._emit_recovery(
                    _RecoverySnapshot(ConnectionRetryState.CONNECTING, self._attempt_count)
                )
                try:
                    async with contextlib.AsyncExitStack() as client_stack:
                        try:
                            async with asyncio.timeout(self._connection_phase_timeout()):
                                self._attempt_operation = (
                                    ConnectionFailureOperation.RESOLVE_CREDENTIALS
                                )
                                username, password = await self._authentication()
                                self._attempt_operation = (
                                    ConnectionFailureOperation.RESOLVE_CREDENTIALS
                                )
                                self._loading_tls_material = True
                                ssl_context = await _build_attempt_ssl_context(self._config)
                                self._loading_tls_material = False
                                connect_properties = Properties(  # type: ignore[no-untyped-call]
                                    PacketTypes.CONNECT
                                )
                                connect_properties.SessionExpiryInterval = 0
                                self._attempt_operation = ConnectionFailureOperation.CONNECT
                                client = self._new_client(
                                    self._config.host,
                                    username,
                                    password,
                                    self._attempt_timeout(),
                                    ssl_context=ssl_context,
                                    connect_properties=connect_properties,
                                )
                                await client_stack.enter_async_context(client)
                        except TimeoutError:
                            if self._loading_tls_material:
                                raise _TLSMaterialLoadTimeout() from None
                            if (
                                self._attempt_operation
                                is ConnectionFailureOperation.RESOLVE_CREDENTIALS
                            ):
                                raise _CredentialResolutionTimeout() from None
                            raise _ConnectionPhaseTimeout(
                                self._attempt_operation or ConnectionFailureOperation.CONNECT
                            ) from None
                        finally:
                            self._attempt_operation = None
                            self._loading_tls_material = False
                        # CONNACK establishes transport connectivity. Strict
                        # readiness follows only after every still-owned
                        # subscription has completed restoration.
                        # A ready connection supersedes any earlier failure. Clear the
                        # cause and the flag BEFORE announcing the connection:
                        # `_set_connected` awaits a caller-supplied callback, and a
                        # startup waiter observing the stale failure during that await
                        # would report a boundary refusal this attempt already
                        # recovered from.
                        self._last_error = None
                        self._startup_failure_event.clear()
                        await self._set_connected(True)
                        await self._restore_subscriptions(client)
                        self._mark_ready()
                        was_ready = True
                        await self._emit_recovery(
                            _RecoverySnapshot(
                                ConnectionRetryState.INACTIVE,
                                self._attempt_count,
                            )
                        )
                        async for message in client.messages:
                            if self._closing:
                                break
                            await self._dispatch(str(message.topic), bytes(message.payload or b""))
                        if not self._closing:
                            protocol_failure = getattr(
                                client,
                                "protocol_failure_error",
                                None,
                            )
                            if isinstance(protocol_failure, ProtocolError):
                                raise protocol_failure
                            raise ConnectionError(
                                "MQTT connection ended",
                                operation="mqtt.receive",
                            )
                except asyncio.CancelledError:
                    if client is not None:
                        with contextlib.suppress(Exception):
                            client._force_disconnect()
                    raise
                except (
                    ECNClientError,
                    aiomqtt.MqttError,
                    OSError,
                    # Named explicitly although OSError already covers it: the bounded
                    # resolution wait raises the built-in TimeoutError, and that must
                    # remain a retryable attempt failure rather than ending the loop.
                    TimeoutError,
                    ValueError,
                ) as exc:
                    protocol_failure = (
                        getattr(client, "protocol_failure_error", None)
                        if client is not None
                        else None
                    )
                    failure = (
                        protocol_failure if isinstance(protocol_failure, ProtocolError) else exc
                    )
                    if client is not None:
                        with contextlib.suppress(Exception):
                            client._force_disconnect()
                finally:
                    self._attempt_operation = None
                    self._loading_tls_material = False
                    self._mark_not_ready()
                    if client is not None:
                        await self._clear_client(client)
                    if was_ready:
                        recovery_reset = self._finish_ready_period()
                    await self._set_connected(False)

                if self._closing:
                    continue
                if failure is None:
                    failure = ConnectionError(
                        "MQTT connection was lost",
                        operation="mqtt.receive",
                    )
                classification = self._classify_failure(
                    failure,
                    client=client,
                    was_ready=was_ready,
                )
                classification = self._apply_protocol_failure_limit(classification)
                self._last_error = failure
                if classification.terminal:
                    await self._set_terminal_recovery(classification, failure)
                    return

                retry_delay = (
                    0.0
                    if classification.immediate_retry or recovery_reset
                    else self._full_jitter_delay()
                )
                remaining_elapsed = self._remaining_elapsed_budget()
                if remaining_elapsed is not None:
                    if remaining_elapsed <= 0:
                        await self._set_terminal_recovery(
                            self._budget_exhaustion_classification(classification),
                            failure,
                        )
                        return
                    retry_delay = min(retry_delay, remaining_elapsed)
                if not self._has_remaining_attempt_budget():
                    await self._set_terminal_recovery(
                        self._budget_exhaustion_classification(classification),
                        failure,
                    )
                    return

                retry_state = (
                    ConnectionRetryState.WAITING_FOR_CREDENTIALS
                    if classification.waiting_for_credentials
                    else ConnectionRetryState.SCHEDULED
                )
                observed_generation = self._wakeup_generation
                await self._emit_recovery(
                    _RecoverySnapshot(
                        retry_state,
                        self._attempt_count,
                        next_retry_delay_seconds=retry_delay,
                        failure_code=classification.code,
                        failure_operation=classification.operation,
                    )
                )
                await self._wait_for_retry(
                    retry_delay,
                    waiting_for_credentials=classification.waiting_for_credentials,
                    observed_generation=observed_generation,
                )
        finally:
            async with self._subscription_lock:
                self._client = None
            await self._set_connected(False)

    async def _restore_subscriptions(self, client: _MQTTV5Client) -> None:
        restore_failures: list[tuple[RestoreFailureCallback | None, RestoreFailure]] = []
        restored_filters: set[str] = set()
        while True:
            async with self._subscription_lock:
                if self._closing:
                    raise asyncio.CancelledError
                required_filters = {
                    subscription.topic_filter
                    for subscription in self._subscriptions.values()
                    if subscription.restore_on_reconnect
                }

            for topic_filter in sorted(restored_filters - required_filters):
                await self._broker_unsubscribe(client, topic_filter)
                restored_filters.remove(topic_filter)
                if self._closing:
                    raise asyncio.CancelledError

            for topic_filter in sorted(required_filters - restored_filters):
                try:
                    restore_timeout = self._restore_timeout()
                    async with asyncio.timeout(restore_timeout):
                        reason_codes = await client.subscribe(
                            topic_filter,
                            qos=1,
                            timeout=restore_timeout,
                        )
                    _ensure_subscription_accepted(reason_codes)
                except TimeoutError:
                    raise ConnectionError(
                        "MQTT restored subscription timed out",
                        operation="mqtt.restore_subscription",
                    ) from None
                except (aiomqtt.MqttError, OSError):
                    raise ConnectionError(
                        "MQTT restored subscription failed",
                        operation="mqtt.restore_subscription",
                    ) from None
                except (AuthorizationError, ResourceLimitError) as rejection:
                    failure: RestoreFailure
                    if isinstance(rejection, AuthorizationError):
                        failure = AuthorizationError(
                            "MQTT restored subscription was rejected by the broker",
                            operation="mqtt.restore_subscription",
                        )
                    else:
                        failure = ResourceLimitError(
                            "MQTT restored subscription exceeded a broker resource limit",
                            operation="mqtt.restore_subscription",
                        )
                    async with self._subscription_lock:
                        denied_handles = [
                            handle
                            for handle, subscription in self._subscriptions.items()
                            if subscription.topic_filter == topic_filter
                        ]
                        for handle in denied_handles:
                            subscription = self._subscriptions.pop(handle)
                            restore_failures.append((subscription.on_restore_failure, failure))
                else:
                    restored_filters.add(topic_filter)
                if self._closing:
                    raise asyncio.CancelledError

            unhandled_failure: RestoreFailure | None = None
            pending_failures, restore_failures = restore_failures, []
            for callback, failure in pending_failures:
                if callback is None:
                    if unhandled_failure is None:
                        unhandled_failure = failure
                    continue
                try:
                    result = callback(failure)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("MQTT restored-subscription failure callback failed")
                    if unhandled_failure is None:
                        unhandled_failure = failure
            if isinstance(unhandled_failure, AuthorizationError):
                raise AuthorizationError(
                    "MQTT restored subscription rejection could not be isolated",
                    operation="mqtt.restore_subscription",
                )
            if isinstance(unhandled_failure, ResourceLimitError):
                raise ResourceLimitError(
                    "MQTT restored subscription resource limit could not be isolated",
                    operation="mqtt.restore_subscription",
                )

            async with self._subscription_lock:
                if self._closing:
                    raise asyncio.CancelledError
                current_filters = {
                    subscription.topic_filter
                    for subscription in self._subscriptions.values()
                    if subscription.restore_on_reconnect
                }
                if current_filters == restored_filters:
                    self._client = client
                    return

    def _begin_attempt(self) -> bool:
        now = self._monotonic()
        if self._recovery_started_at is None:
            self._recovery_started_at = now
        if not self._has_remaining_attempt_budget():
            return False
        remaining_elapsed = self._remaining_elapsed_budget(now=now)
        if remaining_elapsed is not None and remaining_elapsed <= 0 and self._attempt_count:
            return False
        self._attempt_count += 1
        policy = self._reconnect_policy
        previous_cap = self._retry_delay_cap_seconds
        if previous_cap is None:
            cap = policy.initial_delay_seconds
        elif previous_cap >= policy.maximum_delay_seconds / policy.multiplier:
            cap = policy.maximum_delay_seconds
        else:
            cap = previous_cap * policy.multiplier
        self._retry_delay_cap_seconds = min(cap, policy.maximum_delay_seconds)
        return True

    def _has_remaining_attempt_budget(self) -> bool:
        maximum_attempts = self._reconnect_policy.maximum_attempts
        return maximum_attempts is None or self._attempt_count < maximum_attempts

    def _remaining_elapsed_budget(self, *, now: float | None = None) -> float | None:
        maximum_elapsed = self._reconnect_policy.maximum_elapsed_seconds
        if maximum_elapsed is None or self._recovery_started_at is None:
            return None
        observed_at = self._monotonic() if now is None else now
        return maximum_elapsed - (observed_at - self._recovery_started_at)

    def _attempt_timeout(self) -> float:
        remaining_elapsed = self._remaining_elapsed_budget()
        if remaining_elapsed is None:
            return self._config.connection_timeout
        return min(self._config.connection_timeout, max(remaining_elapsed, 1e-9))

    def _connection_phase_timeout(self) -> float | None:
        """Give initial startup one outer deadline; bound later attempts locally."""

        if self._ever_ready:
            return self._attempt_timeout()
        remaining_elapsed = self._remaining_elapsed_budget()
        if remaining_elapsed is None or remaining_elapsed >= self._config.connection_timeout:
            return None
        return max(remaining_elapsed, 1e-9)

    def _restore_timeout(self) -> float:
        remaining_elapsed = self._remaining_elapsed_budget()
        if remaining_elapsed is None:
            return self._config.operation_timeout
        if remaining_elapsed <= 0:
            raise ConnectionError(
                "MQTT restored subscription exceeded the recovery deadline",
                operation="mqtt.restore_subscription",
            )
        return min(self._config.operation_timeout, remaining_elapsed)

    def _full_jitter_delay(self) -> float:
        cap = self._retry_delay_cap_seconds
        if cap is None:
            raise RuntimeError("retry delay requested before a connection attempt")
        sample = self._random_source()
        # The production source is random.random. Clamping keeps deliberately
        # injected test sources from bypassing the configured maximum.
        sample = min(1.0, max(0.0, sample))
        return cap * sample

    def _mark_ready(self) -> None:
        self._ever_ready = True
        self._strict_ready = True
        self._ready_event.set()
        self._ready_since = self._monotonic()
        self._protocol_failure_signature = None
        self._protocol_failure_count = 0
        self._schedule_stable_reset()

    def _mark_not_ready(self) -> None:
        self._strict_ready = False
        self._ready_event.clear()
        self._cancel_stable_reset()

    def _schedule_stable_reset(self) -> None:
        self._cancel_stable_reset()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        ready_since = self._ready_since
        if ready_since is None:
            return
        task = asyncio.create_task(
            self._reset_after_stable_ready(
                self._connection_generation,
                ready_since,
            ),
            name="picogrid-ecn-mqtt-stable-ready-reset",
        )
        self._stable_reset_task = task
        task.add_done_callback(self._stable_reset_done)

    def _cancel_stable_reset(self) -> None:
        task = self._stable_reset_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _stable_reset_done(self, task: asyncio.Task[None]) -> None:
        if self._stable_reset_task is task:
            self._stable_reset_task = None
        if not task.cancelled():
            task.exception()

    async def _reset_after_stable_ready(
        self,
        generation: int,
        ready_since: float,
    ) -> None:
        deadline = ready_since + self._reconnect_policy.stable_reset_seconds
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            await self._stable_sleeper(remaining)
        if (
            self._closing
            or not self._strict_ready
            or self._connection_generation != generation
            or self._ready_since != ready_since
        ):
            return
        self._recovery_started_at = None
        self._attempt_count = 0
        self._retry_delay_cap_seconds = None
        self._auth_immediate_retry_used = False
        self._protocol_failure_signature = None
        self._protocol_failure_count = 0
        await self._emit_recovery(_RecoverySnapshot(ConnectionRetryState.INACTIVE, 0))

    def _finish_ready_period(self) -> bool:
        ready_since, self._ready_since = self._ready_since, None
        if ready_since is None:
            return False
        if self._monotonic() - ready_since < self._reconnect_policy.stable_reset_seconds:
            return False
        self._recovery_started_at = None
        self._attempt_count = 0
        self._retry_delay_cap_seconds = None
        self._auth_immediate_retry_used = False
        self._protocol_failure_signature = None
        self._protocol_failure_count = 0
        return True

    def _set_attempt_operation(self, operation: ConnectionFailureOperation) -> None:
        """Record one safe lifecycle phase for timeout and recovery diagnostics."""

        self._attempt_operation = operation

    def _classify_failure(
        self,
        error: BaseException,
        *,
        client: _MQTTV5Client | None,
        was_ready: bool,
    ) -> _FailureClassification:
        if (
            client is not None and getattr(client, "_server_reference_present", False)
        ) or _is_server_reference(error):
            return _FailureClassification(
                ConnectionFailureCode.SERVER_REFERENCE_REQUIRES_REVIEW,
                ConnectionFailureOperation.CONNECT,
                terminal=True,
            )
        if isinstance(error, TransportBoundaryError):
            return _FailureClassification(
                ConnectionFailureCode.DNS_UNAVAILABLE,
                ConnectionFailureOperation.DNS,
                terminal=not self._ever_ready,
            )
        if isinstance(error, _DNSResolutionFailure):
            return _FailureClassification(
                ConnectionFailureCode.DNS_UNAVAILABLE,
                ConnectionFailureOperation.DNS,
            )
        if isinstance(error, _TCPConnectionFailure):
            return _FailureClassification(
                ConnectionFailureCode.TCP_UNAVAILABLE,
                ConnectionFailureOperation.TCP,
            )
        if isinstance(error, _TLSTransportFailure):
            return _FailureClassification(
                ConnectionFailureCode.TLS_UNAVAILABLE,
                ConnectionFailureOperation.TLS,
            )
        if isinstance(error, (ssl.SSLCertVerificationError, _TLSPeerVerificationFailure)):
            return _FailureClassification(
                ConnectionFailureCode.TLS_PEER_VERIFICATION_FAILED,
                ConnectionFailureOperation.TLS,
                terminal=True,
            )
        if isinstance(error, _ConnectionPhaseTimeout):
            return _connection_phase_timeout_classification(error.operation)
        if isinstance(error, _TLSMaterialLoadTimeout):
            return _FailureClassification(
                ConnectionFailureCode.CONFIGURATION_INVALID,
                ConnectionFailureOperation.RESOLVE_CREDENTIALS,
                terminal=True,
            )
        if isinstance(error, ConfigurationError) or (
            isinstance(error, ValueError) and not isinstance(error, AuthenticationError)
        ):
            return _FailureClassification(
                ConnectionFailureCode.CONFIGURATION_INVALID,
                ConnectionFailureOperation.CONFIGURE,
                terminal=True,
            )
        if isinstance(error, AuthenticationError):
            return _FailureClassification(
                ConnectionFailureCode.CREDENTIALS_UNAVAILABLE,
                ConnectionFailureOperation.RESOLVE_CREDENTIALS,
                terminal=True,
            )
        if isinstance(error, ConnectionError) and error.operation == "mqtt.restore_subscription":
            return _FailureClassification(
                ConnectionFailureCode.BROKER_UNAVAILABLE,
                ConnectionFailureOperation.RESTORE_SUBSCRIPTION,
            )
        if isinstance(error, AuthorizationError):
            return _FailureClassification(
                ConnectionFailureCode.SUBSCRIPTION_DENIED,
                ConnectionFailureOperation.RESTORE_SUBSCRIPTION,
                terminal=True,
            )
        if isinstance(error, ResourceLimitError) and error.operation in {
            "mqtt.subscribe",
            "mqtt.restore_subscription",
        }:
            return _FailureClassification(
                ConnectionFailureCode.SUBSCRIPTION_RESOURCE_LIMIT,
                ConnectionFailureOperation.RESTORE_SUBSCRIPTION,
                terminal=True,
            )
        if isinstance(error, ProtocolError):
            connack_failure = (
                error.operation == "mqtt.connect" and error.details.get("packet_type") == "CONNACK"
            )
            operation = (
                ConnectionFailureOperation.RESTORE_SUBSCRIPTION
                if error.operation in {"mqtt.subscribe", "mqtt.restore_subscription"}
                else (
                    ConnectionFailureOperation.CONNECT
                    if connack_failure
                    else ConnectionFailureOperation.RECEIVE
                )
            )
            return _FailureClassification(
                ConnectionFailureCode.PROTOCOL_FAILURE,
                operation,
                terminal=connack_failure,
                protocol_fingerprint=(
                    "protocol_error",
                    error.operation or "unknown",
                    error.details.get("packet_type", "unknown"),
                    error.details.get("classifier", "unknown"),
                    error.details.get("reason_code", "unknown"),
                ),
            )
        if isinstance(error, _ConnackProtocolFailure):
            return _FailureClassification(
                ConnectionFailureCode.PROTOCOL_FAILURE,
                ConnectionFailureOperation.CONNECT,
                terminal=True,
                protocol_fingerprint=(
                    "connack",
                    error.error.details.get("classifier", "unknown"),
                    error.error.details.get("reason_code", "unknown"),
                ),
            )
        if isinstance(error, MqttConnectError):
            reason_code = _connect_reason_code(error)
            if _is_authentication_rejection(error):
                if not self._ever_ready:
                    return _FailureClassification(
                        ConnectionFailureCode.AUTHENTICATION_REJECTED,
                        ConnectionFailureOperation.CONNECT,
                        terminal=True,
                    )
                if not self._auth_immediate_retry_used:
                    self._auth_immediate_retry_used = True
                    return _FailureClassification(
                        ConnectionFailureCode.AUTHENTICATION_REJECTED,
                        ConnectionFailureOperation.CONNECT,
                        waiting_for_credentials=True,
                        immediate_retry=True,
                    )
                return _FailureClassification(
                    ConnectionFailureCode.AUTHENTICATION_REJECTED,
                    ConnectionFailureOperation.CONNECT,
                    waiting_for_credentials=True,
                )
            if reason_code in _CONNACK_AUTHORIZATION_DENIED:
                return _FailureClassification(
                    ConnectionFailureCode.CONNECTION_AUTHORIZATION_DENIED,
                    ConnectionFailureOperation.CONNECT,
                    terminal=True,
                )
            if reason_code in _CONNACK_CONFIGURATION_INVALID:
                return _FailureClassification(
                    ConnectionFailureCode.CONFIGURATION_INVALID,
                    ConnectionFailureOperation.CONFIGURE,
                    terminal=True,
                )
            if reason_code in _CONNACK_CONNECTION_REJECTED:
                return _FailureClassification(
                    ConnectionFailureCode.CONNECTION_REJECTED,
                    ConnectionFailureOperation.CONNECT,
                    terminal=True,
                )
            if reason_code in _CONNACK_SERVER_BUSY:
                return _FailureClassification(
                    ConnectionFailureCode.SERVER_BUSY,
                    ConnectionFailureOperation.CONNECT,
                )
            if reason_code in _CONNACK_BROKER_UNAVAILABLE:
                return _FailureClassification(
                    ConnectionFailureCode.BROKER_UNAVAILABLE,
                    ConnectionFailureOperation.CONNECT,
                )
            if reason_code in _CONNACK_RESOURCE_LIMIT:
                return _FailureClassification(
                    ConnectionFailureCode.CONNECTION_RESOURCE_LIMIT,
                    ConnectionFailureOperation.CONNECT,
                    terminal=True,
                )
            return _FailureClassification(
                ConnectionFailureCode.PROTOCOL_FAILURE,
                ConnectionFailureOperation.CONNECT,
                terminal=True,
                protocol_fingerprint=(
                    "connack",
                    str(reason_code) if reason_code is not None else "unknown",
                ),
            )
        if isinstance(error, socket.gaierror):
            return _FailureClassification(
                ConnectionFailureCode.DNS_UNAVAILABLE,
                ConnectionFailureOperation.DNS,
            )
        if isinstance(error, ssl.SSLError):
            return _FailureClassification(
                ConnectionFailureCode.TLS_UNAVAILABLE,
                ConnectionFailureOperation.TLS,
            )
        if isinstance(error, TimeoutError):
            return _FailureClassification(
                ConnectionFailureCode.BROKER_UNAVAILABLE,
                ConnectionFailureOperation.CONNECT,
            )
        if isinstance(error, OSError):
            return _FailureClassification(
                ConnectionFailureCode.TCP_UNAVAILABLE,
                ConnectionFailureOperation.TCP,
            )
        if isinstance(error, ConnectionError) and error.operation == "mqtt.receive":
            return _FailureClassification(
                ConnectionFailureCode.CONNECTION_LOST,
                ConnectionFailureOperation.RECEIVE,
            )
        if isinstance(error, aiomqtt.MqttError):
            return _FailureClassification(
                (
                    ConnectionFailureCode.CONNECTION_LOST
                    if was_ready
                    else ConnectionFailureCode.BROKER_UNAVAILABLE
                ),
                (
                    ConnectionFailureOperation.RECEIVE
                    if was_ready
                    else ConnectionFailureOperation.CONNECT
                ),
            )
        return _FailureClassification(
            ConnectionFailureCode.BROKER_UNAVAILABLE,
            ConnectionFailureOperation.CONNECT,
        )

    def _budget_exhaustion_classification(
        self,
        classification: _FailureClassification | None = None,
    ) -> _FailureClassification:
        code: ConnectionFailureCode | None
        if classification is not None:
            code = classification.code
            operation = classification.operation
        else:
            code = self._recovery_snapshot.failure_code
            operation = (
                self._recovery_snapshot.failure_operation or ConnectionFailureOperation.CONNECT
            )
        terminal_code = (
            ConnectionFailureCode.RETRY_EXHAUSTED
            if code in _TRANSIENT_FAILURE_CODES or code is None
            else code
        )
        return _FailureClassification(terminal_code, operation, terminal=True)

    def _apply_protocol_failure_limit(
        self,
        classification: _FailureClassification,
    ) -> _FailureClassification:
        if classification.code is not ConnectionFailureCode.PROTOCOL_FAILURE:
            self._protocol_failure_signature = None
            self._protocol_failure_count = 0
            return classification
        signature = (
            classification.code.value,
            classification.operation.value,
            *(classification.protocol_fingerprint or ("unspecified",)),
        )
        if signature == self._protocol_failure_signature:
            self._protocol_failure_count += 1
        else:
            self._protocol_failure_signature = signature
            self._protocol_failure_count = 1
        if self._protocol_failure_count < 3:
            return classification
        return _FailureClassification(
            classification.code,
            classification.operation,
            terminal=True,
        )

    async def _set_terminal_recovery(
        self,
        classification: _FailureClassification,
        failure: BaseException,
    ) -> None:
        if classification.code is ConnectionFailureCode.PROTOCOL_FAILURE and not isinstance(
            failure, ProtocolError
        ):
            self._last_error = ProtocolError(
                "MQTT connection repeatedly failed protocol validation",
                operation="mqtt.start",
            )
        else:
            self._last_error = failure
        await self._emit_recovery(
            _RecoverySnapshot(
                ConnectionRetryState.TERMINAL,
                self._attempt_count,
                failure_code=classification.code,
                failure_operation=classification.operation,
            )
        )
        self._startup_failure_event.set()

    async def _emit_recovery(self, snapshot: _RecoverySnapshot) -> None:
        self._recovery_snapshot = snapshot
        callback = self._on_recovery_change
        if callback is None:
            return
        try:
            result = callback(snapshot)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("MQTT recovery-state callback failed")

    async def _wait_for_retry(
        self,
        delay: float,
        *,
        waiting_for_credentials: bool,
        observed_generation: int,
    ) -> None:
        if delay <= 0:
            return
        self._retry_wakeup.clear()
        if observed_generation != self._wakeup_generation:
            return
        self._retry_waiting = True
        self._waiting_for_credentials = waiting_for_credentials
        sleep_task: asyncio.Future[None] = asyncio.ensure_future(self._sleeper(delay))
        if isinstance(sleep_task, asyncio.Task):
            sleep_task.set_name("picogrid-ecn-mqtt-retry-sleep")
        wake_task = asyncio.create_task(
            self._retry_wakeup.wait(),
            name="picogrid-ecn-mqtt-retry-wakeup",
        )
        try:
            await asyncio.wait(
                (sleep_task, wake_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._retry_waiting = False
            self._waiting_for_credentials = False
            for task in (sleep_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, wake_task, return_exceptions=True)

    async def _clear_client(self, client: _MQTTV5Client) -> None:
        async with self._subscription_lock:
            if self._client is client:
                self._client = None
            self._subscriptions = {
                handle: subscription
                for handle, subscription in self._subscriptions.items()
                if subscription.restore_on_reconnect
            }

    async def _dispatch(self, topic: str, payload: bytes) -> None:
        if len(payload) > self._config.maximum_payload_size:
            logger.warning("dropping oversized MQTT protocol payload")
            return
        mqtt_topic = aiomqtt.Topic(topic)
        async with self._subscription_lock:
            callbacks: list[MessageCallback] = []
            seen_callbacks: set[int] = set()
            for subscription in self._subscriptions.values():
                # EntityLocationService reuses one cached callback across overlapping filters.
                callback_id = id(subscription.callback)
                if callback_id not in seen_callbacks and mqtt_topic.matches(
                    subscription.topic_filter
                ):
                    callbacks.append(subscription.callback)
                    seen_callbacks.add(callback_id)
        for callback in callbacks:
            try:
                result = callback(topic, payload)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("MQTT protocol callback failed")

    async def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        if connected:
            self._connection_generation += 1
            self._connection_lost_event = asyncio.Event()
        else:
            self._connected_event.clear()
            self._connection_lost_event.set()
        callback = self._on_connection_change
        if callback is not None:
            try:
                result = callback(connected)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("MQTT connection-state callback failed")
        if connected:
            self._connected_event.set()


def _complete_acknowledgement(future: asyncio.Future[object], value: object) -> None:
    if not future.done():
        future.set_result(value)


def _fail_acknowledgement(
    future: asyncio.Future[Any],
    error: BaseException,
) -> None:
    if not future.done():
        future.set_exception(error)


def _reason_code_value(reason_code: object) -> int | None:
    value = getattr(reason_code, "value", reason_code)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _packet_identifier_error(
    packet_identifier: object,
    *,
    operation: str,
    packet_type: str,
) -> ProtocolError | None:
    if packet_identifier is None:
        classifier = "missing_packet_identifier"
    elif (
        not isinstance(packet_identifier, int)
        or isinstance(packet_identifier, bool)
        or not 1 <= packet_identifier <= 0xFFFF
    ):
        classifier = "invalid_packet_identifier"
    else:
        return None
    return _ack_protocol_error(
        operation=operation,
        packet_type=packet_type,
        classifier=classifier,
    )


def _ensure_publication_accepted(reason_code: object) -> None:
    # MQTT v5 permits PUBACK with only the packet identifier; an omitted reason
    # code is the abbreviated Success form defined by the protocol.
    if reason_code is None:
        return
    code = _reason_code_value(reason_code)
    if code is None:
        raise _ack_protocol_error(
            operation="mqtt.publish",
            packet_type="PUBACK",
            classifier="non_numeric_reason_code",
        )
    if code in {_MQTT_SUCCESS, _MQTT_NO_MATCHING_SUBSCRIBERS}:
        return
    if code == _MQTT_NOT_AUTHORIZED:
        raise AuthorizationError(
            "MQTT publication was rejected by the broker",
            operation="mqtt.publish",
            details={"reason_code": code},
        )
    if code == _MQTT_QUOTA_EXCEEDED:
        raise ResourceLimitError(
            "MQTT publication was rejected by a broker resource limit",
            operation="mqtt.publish",
            details={"reason_code": code},
        )
    raise _ack_protocol_error(
        operation="mqtt.publish",
        packet_type="PUBACK",
        classifier="unmapped_failure" if code >= 0x80 else "unlisted_success",
        reason_code=code,
    )


def _ensure_unsubscription_accepted(reason_codes: object) -> None:
    if not isinstance(reason_codes, (list, tuple)) or len(reason_codes) != 1:
        raise _ack_protocol_error(
            operation="mqtt.unsubscribe",
            packet_type="UNSUBACK",
            classifier="wrong_cardinality",
        )
    reason_code = reason_codes[0]
    code = _reason_code_value(reason_code)
    if code is None:
        raise _ack_protocol_error(
            operation="mqtt.unsubscribe",
            packet_type="UNSUBACK",
            classifier=(
                "missing_reason_code" if reason_code is None else "non_numeric_reason_code"
            ),
        )
    if code in {_MQTT_SUCCESS, _MQTT_NO_SUBSCRIPTION_EXISTED}:
        return
    if code == _MQTT_NOT_AUTHORIZED:
        raise AuthorizationError(
            "MQTT unsubscription was rejected by the broker",
            operation="mqtt.unsubscribe",
            details={"reason_code": code},
        )
    raise _ack_protocol_error(
        operation="mqtt.unsubscribe",
        packet_type="UNSUBACK",
        classifier="unmapped_failure" if code >= 0x80 else "unlisted_success",
        reason_code=code,
    )


def _is_authentication_rejection(error: BaseException) -> bool:
    if not isinstance(error, MqttConnectError):
        return False
    return _connect_reason_code(error) in _CONNACK_AUTHENTICATION_REJECTED


def _is_server_reference(error: BaseException) -> bool:
    return (
        isinstance(error, MqttConnectError)
        and _connect_reason_code(error) in _CONNACK_SERVER_REFERENCE
    )


def _connect_reason_code(error: MqttConnectError) -> int | None:
    return _reason_code_value(error.rc)


def _ensure_subscription_accepted(reason_codes: object) -> None:
    if not isinstance(reason_codes, (list, tuple)) or len(reason_codes) != 1:
        raise _ack_protocol_error(
            operation="mqtt.subscribe",
            packet_type="SUBACK",
            classifier="wrong_cardinality",
        )
    reason_code = reason_codes[0]
    code = _reason_code_value(reason_code)
    if code is None:
        raise _ack_protocol_error(
            operation="mqtt.subscribe",
            packet_type="SUBACK",
            classifier=(
                "missing_reason_code" if reason_code is None else "non_numeric_reason_code"
            ),
        )
    if code in {0, 1}:
        return
    if code == _MQTT_NOT_AUTHORIZED:
        raise AuthorizationError(
            "MQTT subscription was rejected by the broker",
            operation="mqtt.subscribe",
            details={"reason_code": code},
        )
    if code == _MQTT_QUOTA_EXCEEDED:
        raise ResourceLimitError(
            "MQTT subscription was rejected by a broker resource limit",
            operation="mqtt.subscribe",
            details={"reason_code": code},
        )
    raise _ack_protocol_error(
        operation="mqtt.subscribe",
        packet_type="SUBACK",
        classifier="unmapped_failure" if code >= 0x80 else "unlisted_success",
        reason_code=code,
    )


def _ack_protocol_error(
    *,
    operation: str,
    packet_type: str,
    classifier: str,
    reason_code: int | None = None,
) -> ProtocolError:
    details: dict[str, object] = {
        "packet_type": packet_type,
        "classifier": classifier,
    }
    if reason_code is not None:
        details["reason_code"] = reason_code
    return ProtocolError(
        "MQTT acknowledgement failed protocol validation",
        operation=operation,
        details=details,
    )


__all__ = [
    "ConnectionChangeCallback",
    "MQTTTransport",
    "MessageCallback",
    "RecoveryChangeCallback",
    "RestoreFailureCallback",
    "SubscriptionHandle",
]
