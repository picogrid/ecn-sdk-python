# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import statistics
import struct
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from picogrid_ecn_client import (
    BearerTokenAuth,
    ClientState,
    Clock,
    ClockEndpoint,
    ClockLeapState,
    ClockReport,
    ECNClient,
    ECNConfig,
    TLSConfig,
    load_config,
)
from picogrid_ecn_client import _cli as clock_cli
from picogrid_ecn_client._profiles import load_profile, save_profile
from picogrid_ecn_client._services.clock import (
    _MAX_NTP_DATAGRAM_SIZE,
    _MINIMUM_DELAY_SECONDS,
    ClockService,
    ResolvedEndpoint,
    ResolvedEndpoints,
    _ClockCapture,
    _ClockReading,
    _CompletedExchange,
    _ntp_to_unix,
    _NtpDatagramExchange,
    _PreparedRequest,
    _RequestFactory,
    _unix_to_ntp,
)
from picogrid_ecn_client.exceptions import (
    ClockError,
    ClockProtocolError,
    ClockToleranceError,
    ConfigurationError,
    NotReadyError,
    ResourceLimitError,
    TimeoutError,
    ValidationError,
)
from picogrid_ecn_client.exceptions import ConnectionError as ECNConnectionError

_BASE_TIME = 1_700_000_000.0
_NTP_PACKET_SIZE = 48
_NTP_FIXED_POINT_TOLERANCE = 1e-6


def _config(*, ntp_host: str = "localhost", ntp_port: int = 123) -> ECNConfig:
    return ECNConfig(
        host="127.0.0.1",
        mqtt_port=1883,
        ntp_host=ntp_host,
        ntp_port=ntp_port,
        integration_name="clock-test",
        auth=BearerTokenAuth(token=SecretStr("synthetic")),
        tls=TLSConfig(enabled=False, verify=False),
        operation_timeout=1.0,
        allow_insecure=True,
    )


class _SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


@dataclass
class _MutableClock:
    value: float

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _bracketed(values: Iterable[float]) -> Iterable[float]:
    return (value for value in values for _ in range(2))


@dataclass
class _StaticResolver:
    endpoint: ResolvedEndpoint
    calls: int = 0
    last_host: str | None = None
    last_port: int | None = None

    async def __call__(self, host: str, port: int) -> ResolvedEndpoints:
        self.calls += 1
        self.last_host = host
        self.last_port = port
        return (self.endpoint,)


@dataclass(frozen=True)
class _SampleSpec:
    offset: float
    delay: float
    stratum: int = 2
    leap: int = 0


def _server_packet(
    request: bytes,
    *,
    server_received: float,
    server_transmitted: float,
    leap: int = 0,
    version: int = 4,
    mode: int = 4,
    stratum: int = 2,
    reference_id: bytes = b"LOCL",
) -> bytes:
    packet = bytearray(_NTP_PACKET_SIZE)
    packet[0] = (leap << 6) | (version << 3) | mode
    packet[1] = stratum
    packet[12:16] = reference_id
    packet[24:32] = request[40:48]
    packet[32:40] = _unix_to_ntp(server_received)
    packet[40:48] = _unix_to_ntp(server_transmitted)
    return bytes(packet)


def _response_for_spec(request: bytes, spec: _SampleSpec) -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    server_received = sent + spec.delay / 2 + spec.offset
    return _server_packet(
        request,
        server_received=server_received,
        server_transmitted=server_received,
        leap=spec.leap,
        stratum=spec.stratum,
    )


class _ScriptedExchange:
    def __init__(self, specs: Iterable[_SampleSpec]) -> None:
        self._specs = iter(specs)
        self.calls: list[tuple[ResolvedEndpoint, bytes]] = []
        self.closed = False

    async def exchange(
        self,
        endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        request = request_factory()
        self.calls.append((endpoint, request.packet))
        return _CompletedExchange(
            response=_response_for_spec(request.packet, next(self._specs)),
            request=request,
            received=receive_clock(),
        )

    async def close(self) -> None:
        self.closed = True


class _ResponseExchange:
    def __init__(self, response: Callable[[bytes], bytes]) -> None:
        self._response = response
        self.calls = 0
        self.closed = False

    async def exchange(
        self,
        _endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        self.calls += 1
        request = request_factory()
        return _CompletedExchange(
            response=self._response(request.packet),
            request=request,
            received=receive_clock(),
        )

    async def close(self) -> None:
        self.closed = True


class _SequencedExchange:
    def __init__(self, outcomes: Iterable[_SampleSpec | bytes | OSError | None]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[ResolvedEndpoint] = []
        self.closed = False

    async def exchange(
        self,
        endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        self.calls.append(endpoint)
        request = request_factory()
        outcome = next(self._outcomes)
        if outcome is None:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        if isinstance(outcome, OSError):
            raise outcome
        response = (
            outcome if isinstance(outcome, bytes) else _response_for_spec(request.packet, outcome)
        )
        return _CompletedExchange(
            response=response,
            request=request,
            received=receive_clock(),
        )

    async def close(self) -> None:
        self.closed = True


class _FallbackExchange:
    def __init__(self, *, failures: int) -> None:
        self._failures = failures
        self.calls: list[ResolvedEndpoint] = []
        self.closed = False

    async def exchange(
        self,
        endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        self.calls.append(endpoint)
        if len(self.calls) <= self._failures:
            raise OSError("synthetic address failure")
        request = request_factory()
        return _CompletedExchange(
            response=_response_for_spec(
                request.packet,
                _SampleSpec(offset=0.0, delay=0.02),
            ),
            request=request,
            received=receive_clock(),
        )

    async def close(self) -> None:
        self.closed = True


class _BlockingExchange:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def exchange(
        self,
        _endpoint: ResolvedEndpoint,
        _request_factory: _RequestFactory,
        _receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class _BoundaryDelayExchange:
    def __init__(
        self,
        *,
        wall: _MutableClock,
        monotonic: _MutableClock,
        setup_delay: float,
        network_delay: float,
        return_delay: float = 0.0,
    ) -> None:
        self._wall = wall
        self._monotonic = monotonic
        self._setup_delay = setup_delay
        self._network_delay = network_delay
        self._return_delay = return_delay
        self.request: bytes | None = None
        self.closed = False

    async def exchange(
        self,
        _endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        self._wall.advance(self._setup_delay)
        self._monotonic.advance(self._setup_delay)
        request = request_factory()
        self.request = request.packet
        server_timestamp = self._wall.value + self._network_delay / 2
        self._wall.advance(self._network_delay)
        self._monotonic.advance(self._network_delay)
        received = receive_clock()
        self._wall.advance(self._return_delay)
        self._monotonic.advance(self._return_delay)
        return _CompletedExchange(
            response=_server_packet(
                request.packet,
                server_received=server_timestamp,
                server_transmitted=server_timestamp,
            ),
            request=request,
            received=received,
        )

    async def close(self) -> None:
        self.closed = True


class _BlockingResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, _host: str, _port: int) -> ResolvedEndpoints:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _LocalNtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, *, respond: bool, offset: float = 0.125) -> None:
        self.respond = respond
        self.offset = offset
        self.requests: list[bytes] = []
        self.received = asyncio.Event()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast("asyncio.DatagramTransport", transport)

    def datagram_received(self, data: bytes, address: tuple[Any, ...]) -> None:
        self.requests.append(data)
        self.received.set()
        if not self.respond or self.transport is None:
            return
        sent = _ntp_to_unix(data[40:48], reference=_BASE_TIME)
        server_received = sent + 0.05 + self.offset
        self.transport.sendto(
            _server_packet(
                data,
                server_received=server_received,
                server_transmitted=server_received,
            ),
            address,
        )


@asynccontextmanager
async def _local_ntp_server(
    family: socket.AddressFamily,
    *,
    respond: bool,
) -> AsyncIterator[tuple[_LocalNtpProtocol, ResolvedEndpoint, int]]:
    if family == socket.AF_INET6 and not socket.has_ipv6:
        pytest.skip("IPv6 loopback is unavailable")
    loop = asyncio.get_running_loop()
    local_address: tuple[Any, ...] = ("::1", 0) if family == socket.AF_INET6 else ("127.0.0.1", 0)
    protocol = _LocalNtpProtocol(respond=respond)
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=local_address,
            family=family,
        )
    except OSError:
        if family == socket.AF_INET6:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    datagram_transport = transport
    socket_address = datagram_transport.get_extra_info("sockname")
    assert isinstance(socket_address, tuple)
    port = cast("int", socket_address[1])
    endpoint_address: tuple[object, ...] = (
        ("::1", port, 0, 0) if family == socket.AF_INET6 else ("127.0.0.1", port)
    )
    endpoint: ResolvedEndpoint = (
        family,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        endpoint_address,
    )
    try:
        yield protocol, endpoint, port
    finally:
        datagram_transport.close()
        await asyncio.sleep(0)


def _service(
    exchange: _ScriptedExchange | _ResponseExchange | _SequencedExchange,
    *,
    wall_values: Iterable[float],
    monotonic_values: Iterable[float],
    resolver: _StaticResolver | None = None,
) -> ClockService:
    selected_resolver = resolver or _StaticResolver(
        (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
    )
    return ClockService(
        _config(),
        wall_clock=_SequenceClock(wall_values),
        monotonic_clock=_SequenceClock(_bracketed(monotonic_values)),
        resolver=selected_resolver,
        exchange=exchange,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
async def test_clock_valid_ntpv4_response_over_ipv4_and_ipv6(
    family: socket.AddressFamily,
) -> None:
    async with _local_ntp_server(family, respond=True) as (server, endpoint, port):
        exchange = _NtpDatagramExchange()
        resolver = _StaticResolver(endpoint)
        service = ClockService(
            _config(ntp_port=port),
            wall_clock=_SequenceClock([_BASE_TIME, _BASE_TIME + 0.1]),
            monotonic_clock=_SequenceClock(_bracketed([10.0, 10.1])),
            resolver=resolver,
            exchange=exchange,
        )
        try:
            report = await service.measure(samples=1, timeout=1.0)
            assert report.offset_seconds == pytest.approx(0.125, abs=_NTP_FIXED_POINT_TOLERANCE)
            assert report.round_trip_delay_seconds == pytest.approx(
                0.1, abs=_NTP_FIXED_POINT_TOLERANCE
            )
            assert report.server_version == 4
            assert report.server_stratum == 2
            assert report.leap_state is ClockLeapState.NO_WARNING
            assert report.endpoint.host == "localhost"
            assert report.endpoint.port == port
            assert resolver.calls == 1
            assert len(server.requests) == 1
            assert server.requests[0][0] == (4 << 3) | 3
            assert exchange.active_socket_count == 0
        finally:
            await service.close()
        assert exchange.active_socket_count == 0


@pytest.mark.asyncio
async def test_ntp_exchange_captures_at_send_and_receive_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    events: list[str] = []
    response = b"synthetic-response"
    request = _PreparedRequest(
        packet=bytes(_NTP_PACKET_SIZE),
        sent=_ClockReading(
            wall=_BASE_TIME,
            monotonic=10.0,
            uncertainty=0.0,
            capture_span=0.0,
        ),
    )
    received_reading = _ClockReading(
        wall=_BASE_TIME + 0.02,
        monotonic=10.02,
        uncertainty=0.0,
        capture_span=0.0,
    )

    class _RecordingSocket:
        def setblocking(self, flag: bool) -> None:
            assert flag is False

        def close(self) -> None:
            events.append("close")

    recording_socket = _RecordingSocket()

    async def connect(_socket: socket.socket, _address: tuple[object, ...]) -> None:
        events.append("connect-start")
        await asyncio.sleep(0)
        events.append("connect-complete")

    async def send(_socket: socket.socket, packet: bytes) -> None:
        events.append("send")
        assert packet is request.packet

    async def receive(_socket: socket.socket, _size: int) -> bytes:
        events.append("receive")
        return response

    monkeypatch.setattr(loop, "sock_connect", connect)
    monkeypatch.setattr(loop, "sock_sendall", send)
    monkeypatch.setattr(loop, "sock_recv", receive)
    monkeypatch.setattr(socket, "socket", lambda *_args: recording_socket)

    def prepare() -> _PreparedRequest:
        events.append("prepare")
        return request

    def capture_received() -> _ClockReading:
        events.append("capture-received")
        return received_reading

    exchange = _NtpDatagramExchange()
    endpoint: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )
    completed = await exchange.exchange(endpoint, prepare, capture_received)
    events.append("returned")
    assert events == [
        "connect-start",
        "connect-complete",
        "prepare",
        "send",
        "receive",
        "capture-received",
        "close",
        "returned",
    ]
    assert completed.response is response
    assert completed.request is request
    assert completed.received is received_reading
    assert exchange.active_socket_count == 0
    await exchange.close()


@pytest.mark.asyncio
async def test_clock_captures_transmit_time_after_endpoint_setup() -> None:
    setup_delay = 0.2
    network_delay = 0.02
    wall = _MutableClock(_BASE_TIME)
    monotonic = _MutableClock(10.0)
    exchange = _BoundaryDelayExchange(
        wall=wall,
        monotonic=monotonic,
        setup_delay=setup_delay,
        network_delay=network_delay,
    )
    service = ClockService(
        _config(),
        wall_clock=wall,
        monotonic_clock=monotonic,
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        report = await service.require_within(
            max_offset_seconds=0.001,
            samples=1,
            timeout=1.0,
        )
        assert exchange.request is not None
        wire_transmit = _ntp_to_unix(
            exchange.request[40:48],
            reference=_BASE_TIME + setup_delay,
        )
        assert wire_transmit == pytest.approx(
            _BASE_TIME + setup_delay,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.offset_seconds == pytest.approx(0.0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.round_trip_delay_seconds == pytest.approx(network_delay, abs=1e-6)
        assert report.within_tolerance is True
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_uses_receive_time_before_exchange_return_delay() -> None:
    network_delay = 0.02
    return_delay = 0.2
    wall = _MutableClock(_BASE_TIME)
    monotonic = _MutableClock(10.0)
    exchange = _BoundaryDelayExchange(
        wall=wall,
        monotonic=monotonic,
        setup_delay=0.0,
        network_delay=network_delay,
        return_delay=return_delay,
    )
    service = ClockService(
        _config(),
        wall_clock=wall,
        monotonic_clock=monotonic,
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        report = await service.require_within(
            max_offset_seconds=0.001,
            samples=1,
            timeout=1.0,
        )
        assert report.offset_seconds == pytest.approx(0.0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.round_trip_delay_seconds == pytest.approx(network_delay, abs=1e-6)
        assert report.measured_at == datetime.fromtimestamp(
            _BASE_TIME + network_delay,
            tz=UTC,
        )
        assert report.within_tolerance is True
    finally:
        await service.close()
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_timeout_closes_the_udp_socket_and_finishes_the_task() -> None:
    async with _local_ntp_server(socket.AF_INET, respond=False) as (server, endpoint, port):
        exchange = _NtpDatagramExchange()
        service = ClockService(
            _config(ntp_port=port),
            wall_clock=lambda: _BASE_TIME,
            monotonic_clock=lambda: 10.0,
            resolver=_StaticResolver(endpoint),
            exchange=exchange,
        )
        measurement = asyncio.create_task(service.measure(samples=1, timeout=0.05))
        await asyncio.wait_for(server.received.wait(), timeout=1.0)
        with pytest.raises(TimeoutError) as caught:
            await measurement
        assert caught.value.code == "clock_timeout"
        assert server.received.is_set()
        assert measurement.done()
        assert exchange.active_socket_count == 0
        await service.close()


@pytest.mark.asyncio
async def test_clock_connection_failure_does_not_expose_resolver_diagnostics() -> None:
    canary = "sensitive-endpoint-canary"

    async def fail_resolver(_host: str, _port: int) -> ResolvedEndpoints:
        raise OSError(f"resolver failed for {canary}")

    service = ClockService(
        _config(),
        resolver=fail_resolver,
        exchange=_ScriptedExchange([]),
    )
    try:
        with pytest.raises(ClockError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_connection_failed"
        assert str(caught.value) == "ECN clock endpoint could not be measured"
        assert canary not in str(caught.value)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_caller_cancellation_closes_the_udp_socket_without_translation() -> None:
    async with _local_ntp_server(socket.AF_INET, respond=False) as (server, endpoint, port):
        exchange = _NtpDatagramExchange()
        service = ClockService(
            _config(ntp_port=port),
            wall_clock=_SequenceClock([_BASE_TIME]),
            monotonic_clock=_SequenceClock(_bracketed([10.0])),
            resolver=_StaticResolver(endpoint),
            exchange=exchange,
        )
        measurement = asyncio.create_task(service.measure(samples=1, timeout=1.0))
        await asyncio.wait_for(server.received.wait(), timeout=1.0)
        measurement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await measurement
        assert exchange.active_socket_count == 0
        await service.close()


@pytest.mark.asyncio
async def test_clock_close_interrupts_response_wait_and_is_idempotent() -> None:
    async with _local_ntp_server(socket.AF_INET, respond=False) as (server, endpoint, port):
        exchange = _NtpDatagramExchange()
        service = ClockService(
            _config(ntp_port=port),
            wall_clock=_SequenceClock([_BASE_TIME]),
            monotonic_clock=_SequenceClock(_bracketed([10.0])),
            resolver=_StaticResolver(endpoint),
            exchange=exchange,
        )
        measurement = asyncio.create_task(service.measure(samples=1, timeout=1.0))
        await asyncio.wait_for(server.received.wait(), timeout=1.0)

        await service.close()
        await service.close()

        with pytest.raises(asyncio.CancelledError):
            await measurement
        assert measurement.done()
        assert exchange.active_socket_count == 0


@pytest.mark.asyncio
async def test_close_ignores_caller_tail() -> None:
    exchange = _BlockingExchange()
    service = ClockService(
        _config(),
        wall_clock=lambda: _BASE_TIME,
        monotonic_clock=lambda: 10.0,
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    cancellation_observed = asyncio.Event()
    release_caller = asyncio.Event()

    async def caller() -> None:
        try:
            await service.measure(samples=1, timeout=1.0)
        except asyncio.CancelledError:
            cancellation_observed.set()
            await release_caller.wait()

    caller_task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(exchange.started.wait(), timeout=1.0)
        await asyncio.wait_for(service.close(), timeout=0.1)
        await asyncio.wait_for(cancellation_observed.wait(), timeout=0.1)
        assert exchange.closed
        assert not caller_task.done()
    finally:
        release_caller.set()
        await caller_task


def _valid_response(request: bytes) -> bytes:
    return _response_for_spec(request, _SampleSpec(offset=0.01, delay=0.1))


def _truncated_response(request: bytes) -> bytes:
    return _valid_response(request)[:-1]


def _misaligned_response(request: bytes) -> bytes:
    return _valid_response(request) + b"x"


def _oversized_response(request: bytes) -> bytes:
    packet = _valid_response(request)
    return packet + bytes(_MAX_NTP_DATAGRAM_SIZE - len(packet) + 4)


def _originate_mismatch(request: bytes) -> bytes:
    response = bytearray(_valid_response(request))
    response[24:32] = bytes(8)
    return bytes(response)


def _unsynchronized_response(request: bytes) -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    return _server_packet(
        request,
        server_received=sent + 0.06,
        server_transmitted=sent + 0.06,
        leap=3,
    )


def _kiss_of_death_response(request: bytes, *, reference_id: bytes = b"RATE") -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    return _server_packet(
        request,
        server_received=sent + 0.06,
        server_transmitted=sent + 0.06,
        stratum=0,
        reference_id=reference_id,
    )


def _mismatched_kiss_of_death_response(request: bytes) -> bytes:
    response = bytearray(_kiss_of_death_response(request))
    response[24:32] = bytes(8)
    return bytes(response)


def _invalid_stratum_response(request: bytes) -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    return _server_packet(
        request,
        server_received=sent + 0.06,
        server_transmitted=sent + 0.06,
        stratum=16,
    )


def _wrong_version_response(request: bytes) -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    return _server_packet(
        request,
        server_received=sent + 0.06,
        server_transmitted=sent + 0.06,
        version=3,
    )


def _wrong_mode_response(request: bytes) -> bytes:
    sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
    return _server_packet(
        request,
        server_received=sent + 0.06,
        server_transmitted=sent + 0.06,
        mode=3,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_truncated_response, "clock_packet_size_invalid"),
        (_misaligned_response, "clock_packet_size_invalid"),
        (_oversized_response, "clock_packet_size_invalid"),
        (_originate_mismatch, "clock_originate_mismatch"),
        (_mismatched_kiss_of_death_response, "clock_originate_mismatch"),
        (_unsynchronized_response, "clock_unsynchronized"),
        (_kiss_of_death_response, "clock_kiss_of_death"),
        (_invalid_stratum_response, "clock_stratum_invalid"),
        (_wrong_version_response, "clock_version_invalid"),
        (_wrong_mode_response, "clock_mode_invalid"),
    ],
)
async def test_clock_rejects_malformed_or_unusable_ntp_responses(
    response: Callable[[bytes], bytes],
    code: str,
) -> None:
    exchange = _ResponseExchange(response)
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.1],
        monotonic_values=[10.0, 10.1],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == code
        if code == "clock_kiss_of_death":
            assert caught.value.details == {"classification": "RATE"}
        assert exchange.calls == 1
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference_id", "classification"),
    [
        pytest.param(b"RATE", "RATE", id="registered-rfc-5905"),
        pytest.param(b"NTSN", "NTSN", id="registered-rfc-8915"),
        pytest.param(b"LEAK", "UNSPECIFIED", id="unregistered-printable"),
        pytest.param(b"\xff\x00\x01\x02", "UNSPECIFIED", id="unregistered-binary"),
    ],
)
async def test_clock_kiss_of_death_classification_uses_closed_allowlist(
    reference_id: bytes,
    classification: str,
) -> None:
    exchange = _ResponseExchange(
        lambda request: _kiss_of_death_response(request, reference_id=reference_id)
    )
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.1],
        monotonic_values=[10.0, 10.1],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_kiss_of_death"
        assert caught.value.details == {"classification": classification}
        assert exchange.calls == 1
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailing_data",
    [
        pytest.param(bytes(4), id="crypto-nak"),
        pytest.param(struct.pack("!I", 1) + bytes(16), id="mac-20-octet"),
        pytest.param(struct.pack("!I", 1) + bytes(20), id="mac-24-octet"),
        pytest.param(
            struct.pack("!HH", 0x0104, 28) + bytes(24),
            id="single-extension-without-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12) + struct.pack("!HH", 0x0204, 28) + bytes(24),
            id="multiple-extensions-without-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12) + struct.pack("!I", 1) + bytes(16),
            id="extension-with-20-octet-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12) + struct.pack("!I", 1) + bytes(20),
            id="extension-with-24-octet-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 28) + bytes(24) + bytes(4),
            id="extension-with-crypto-nak",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, _MAX_NTP_DATAGRAM_SIZE - _NTP_PACKET_SIZE)
            + bytes(_MAX_NTP_DATAGRAM_SIZE - _NTP_PACKET_SIZE - 4),
            id="maximum-extension",
        ),
    ],
)
async def test_clock_accepts_structurally_valid_ntp_extension_crypto_nak_or_mac_tail(
    trailing_data: bytes,
) -> None:
    exchange = _ResponseExchange(lambda request: _valid_response(request) + trailing_data)
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.1],
        monotonic_values=[10.0, 10.1],
    )
    try:
        report = await service.measure(samples=1, timeout=1.0)
        assert report.samples_completed == 1
        assert report.server_version == 4
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailing_data",
    [
        pytest.param(bytes(8), id="impossible-eight-octet-tail"),
        pytest.param(bytes(12), id="impossible-twelve-octet-tail"),
        pytest.param(b"\x00\x00\x00\x01", id="nonzero-crypto-nak"),
        pytest.param(
            struct.pack("!HH", 0x0104, 12) + bytes(24),
            id="extension-shorter-than-minimum",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 18) + bytes(24),
            id="extension-length-not-word-aligned",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 32) + bytes(24),
            id="extension-length-exceeds-tail",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12),
            id="short-final-extension-without-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12) + struct.pack("!HH", 0x0204, 16) + bytes(12),
            id="short-last-of-multiple-extensions-without-mac",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 16) + bytes(12) + bytes(12),
            id="invalid-tail-after-extension",
        ),
        pytest.param(
            struct.pack("!HH", 0x0104, 28) + bytes(24) + b"\x00\x00\x00\x01",
            id="nonzero-crypto-nak-after-extension",
        ),
    ],
)
async def test_clock_rejects_malformed_ntp_extension_crypto_nak_or_mac_tail(
    trailing_data: bytes,
) -> None:
    exchange = _ResponseExchange(lambda request: _valid_response(request) + trailing_data)
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.1],
        monotonic_values=[10.0, 10.1],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_packet_size_invalid"
        assert exchange.calls == 1
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_wall_step_detected() -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.1)])
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.6],
        monotonic_values=[10.0, 10.1],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_local_step_detected"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_read_bracketing_tolerates_bounded_capture_latency() -> None:
    capture_span = 0.005
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.1)])
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock(
            [_BASE_TIME + capture_span / 2, _BASE_TIME + 0.1 + capture_span / 2]
        ),
        monotonic_clock=_SequenceClock([10.0, 10.0 + capture_span, 10.1, 10.1 + capture_span]),
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=1, timeout=1.0)
        assert report.offset_seconds == pytest.approx(0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.round_trip_delay_seconds == pytest.approx(0.1, abs=1e-6)
        assert report.local_capture_uncertainty_seconds == pytest.approx(capture_span)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("received_wall", "expected_offset"),
    [
        (_BASE_TIME + 0.013, -0.004),
        (_BASE_TIME - 0.003, 0.004),
    ],
    ids=["forward", "backward"],
)
async def test_clock_tolerance_accounts_for_tolerated_wall_clock_movement(
    received_wall: float,
    expected_offset: float,
) -> None:
    def response(request: bytes) -> bytes:
        sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
        return _server_packet(
            request,
            server_received=sent + 0.002,
            server_transmitted=sent + 0.003,
        )

    exchange = _ResponseExchange(response)
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, received_wall],
        monotonic_values=[10.0, 10.005],
    )
    try:
        with pytest.raises(ClockToleranceError) as caught:
            await service.require_within(
                max_offset_seconds=0.005,
                samples=1,
                timeout=1.0,
            )
        report = caught.value.report
        assert report.offset_seconds == pytest.approx(
            expected_offset,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.local_capture_uncertainty_seconds == pytest.approx(
            0.004,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.round_trip_delay_seconds == pytest.approx(
            0.004,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.within_tolerance is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_selects_by_monotonic_delay_after_a_tolerated_wall_step() -> None:
    exchange = _ScriptedExchange(
        [
            _SampleSpec(offset=0.0, delay=0.005, stratum=2),
            _SampleSpec(offset=0.0, delay=0.002, stratum=3),
        ]
    )
    service = _service(
        exchange,
        wall_values=[
            _BASE_TIME,
            _BASE_TIME - 0.003,
            _BASE_TIME + 0.092,
            _BASE_TIME + 0.094,
        ],
        monotonic_values=[10.0, 10.005, 10.1, 10.102],
    )
    try:
        report = await service.measure(samples=2, timeout=1.0)
        assert report.round_trip_delay_seconds == pytest.approx(
            0.002,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.offset_seconds == pytest.approx(0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.server_stratum == 3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_uncertainty_combines_capture_and_elapsed_divergence() -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.1)])
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock([_BASE_TIME, _BASE_TIME + 0.111]),
        monotonic_clock=_SequenceClock([10.0, 10.006, 10.106, 10.106]),
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=1, timeout=1.0)
        assert report.local_capture_uncertainty_seconds == pytest.approx(
            0.0055,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wall_values", "monotonic_values", "server_timestamp", "expected_offset"),
    [
        (
            [_BASE_TIME, _BASE_TIME + 0.109],
            [10.0, 10.009, 10.109, 10.109],
            _BASE_TIME + 0.059,
            0.0045,
        ),
        (
            [_BASE_TIME, _BASE_TIME + 0.109],
            [10.0, 10.0, 10.1, 10.109],
            _BASE_TIME + 0.05,
            -0.0045,
        ),
    ],
    ids=["send-read-leading-edge", "receive-read-trailing-edge"],
)
async def test_clock_tolerance_accounts_for_local_capture_uncertainty(
    wall_values: list[float],
    monotonic_values: list[float],
    server_timestamp: float,
    expected_offset: float,
) -> None:
    exchange = _ResponseExchange(
        lambda request: _server_packet(
            request,
            server_received=server_timestamp,
            server_transmitted=server_timestamp,
        )
    )
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock(wall_values),
        monotonic_clock=_SequenceClock(monotonic_values),
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        with pytest.raises(ClockToleranceError) as caught:
            await service.require_within(
                max_offset_seconds=0.006,
                samples=1,
                timeout=1.0,
            )
        report = caught.value.report
        assert abs(report.offset_seconds) < 0.006
        assert report.offset_seconds == pytest.approx(
            expected_offset,
            abs=_NTP_FIXED_POINT_TOLERANCE,
        )
        assert report.local_capture_uncertainty_seconds == pytest.approx(0.0045)
        assert report.within_tolerance is False
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wall_values", "monotonic_values", "expected_calls"),
    [
        (
            [_BASE_TIME],
            [10.0, 10.2],
            0,
        ),
        (
            [_BASE_TIME, _BASE_TIME + 0.1],
            [10.0, 10.0, 10.1, 10.3],
            1,
        ),
    ],
    ids=["before-send", "after-receive"],
)
async def test_clock_rejects_an_overlong_local_capture(
    wall_values: list[float],
    monotonic_values: list[float],
    expected_calls: int,
) -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.1)])
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock(wall_values),
        monotonic_clock=_SequenceClock(monotonic_values),
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_local_read_uncertain"
        assert len(exchange.calls) == expected_calls
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_detects_local_wall_clock_step_between_samples() -> None:
    exchange = _ScriptedExchange(
        [
            _SampleSpec(offset=0.0, delay=0.01),
            _SampleSpec(offset=0.0, delay=0.02),
        ]
    )
    service = _service(
        exchange,
        wall_values=[
            _BASE_TIME,
            _BASE_TIME + 0.01,
            _BASE_TIME + 100.02,
        ],
        monotonic_values=[10.0, 10.01, 10.02],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=2, timeout=1.0)
        assert caught.value.code == "clock_local_step_detected"
        assert len(exchange.calls) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_clamps_a_finite_negative_apparent_delay_to_system_precision() -> None:
    def response(request: bytes) -> bytes:
        sent = _ntp_to_unix(request[40:48], reference=_BASE_TIME)
        return _server_packet(
            request,
            server_received=sent + 0.02,
            server_transmitted=sent + 0.12,
        )

    exchange = _ResponseExchange(response)
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.05],
        monotonic_values=[10.0, 10.05],
    )
    try:
        report = await service.measure(samples=1, timeout=1.0)
        assert report.round_trip_delay_seconds == _MINIMUM_DELAY_SECONDS
        assert report.offset_seconds == pytest.approx(0.045, abs=_NTP_FIXED_POINT_TOLERANCE)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_clock_rejects_non_finite_local_clock_values(value: float) -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.1)])
    service = _service(
        exchange,
        wall_values=[value],
        monotonic_values=[10.0],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_local_value_invalid"
        assert exchange.calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_rejects_timestamp_outside_supported_offset_window() -> None:
    excessive_offset = 11 * 366 * 24 * 60 * 60
    exchange = _ScriptedExchange([_SampleSpec(offset=excessive_offset, delay=0.1)])
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.1],
        monotonic_values=[10.0, 10.1],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_timestamp_invalid"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_require_within_preserves_report_for_excessive_offset() -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=2.0, delay=0.02)])
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.02],
        monotonic_values=[10.0, 10.02],
    )
    try:
        with pytest.raises(ClockToleranceError) as caught:
            await service.require_within(
                max_offset_seconds=0.5,
                samples=1,
                timeout=1.0,
            )
        assert caught.value.report.offset_seconds == pytest.approx(
            2.0, abs=_NTP_FIXED_POINT_TOLERANCE
        )
        assert caught.value.report.max_offset_seconds == 0.5
        assert caught.value.report.within_tolerance is False
        assert caught.value.details["max_offset_seconds"] == "0.5"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_zero_tolerance_is_valid_and_strict() -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.0)])
    service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME],
        monotonic_values=[10.0, 10.0],
    )
    try:
        report = await service.require_within(
            max_offset_seconds=0,
            samples=1,
            timeout=1.0,
        )
        assert report.max_offset_seconds == 0
        assert report.within_tolerance is True
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "maximum",
    [-0.1, 86_401, float("nan"), float("inf"), True, "1"],
)
async def test_tolerance_bounds(maximum: object) -> None:
    service = _service(
        _ScriptedExchange([]),
        wall_values=[],
        monotonic_values=[],
    )
    try:
        with pytest.raises(ValidationError, match="finite non-negative"):
            await service.require_within(
                max_offset_seconds=cast("float", maximum),
                samples=1,
                timeout=1.0,
            )
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [0, 11, True, 1.0, "3", None])
async def test_clock_sample_count_is_strictly_bounded(samples: object) -> None:
    resolver = _StaticResolver(
        (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
    )
    exchange = _ScriptedExchange([])
    service = _service(
        exchange,
        wall_values=[],
        monotonic_values=[],
        resolver=resolver,
    )
    try:
        with pytest.raises(ValidationError, match="between 1 and 10"):
            await service.measure(samples=cast("int", samples), timeout=1.0)
        assert resolver.calls == 0
        assert exchange.calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [1, 10])
async def test_clock_sample_count_accepts_inclusive_bounds(samples: int) -> None:
    exchange = _ScriptedExchange(_SampleSpec(offset=0.01, delay=0.02) for _ in range(samples))
    wall_values: list[float] = []
    monotonic_values: list[float] = []
    for index in range(samples):
        wall_values.extend([_BASE_TIME + index, _BASE_TIME + index + 0.02])
        monotonic_values.extend([10.0 + index, 10.0 + index + 0.02])
    service = _service(
        exchange,
        wall_values=wall_values,
        monotonic_values=monotonic_values,
    )
    try:
        report = await service.measure(samples=samples, timeout=1.0)
        assert report.samples_requested == samples
        assert report.samples_completed == samples
        assert len(exchange.calls) == samples
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_selects_lowest_delay_and_reports_offset_distribution() -> None:
    specs = [
        _SampleSpec(offset=0.3, delay=0.2, stratum=2, leap=0),
        _SampleSpec(offset=-0.1, delay=0.05, stratum=3, leap=1),
        _SampleSpec(offset=0.2, delay=0.1, stratum=4, leap=2),
    ]
    exchange = _ScriptedExchange(specs)
    service = _service(
        exchange,
        wall_values=[
            _BASE_TIME,
            _BASE_TIME + 0.2,
            _BASE_TIME + 1,
            _BASE_TIME + 1.05,
            _BASE_TIME + 2,
            _BASE_TIME + 2.1,
        ],
        monotonic_values=[10.0, 10.2, 11.0, 11.05, 12.0, 12.1],
    )
    try:
        report = await service.measure(samples=3, timeout=1.0)
        expected_offsets = [spec.offset for spec in specs]
        assert report.offset_seconds == pytest.approx(-0.1, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.round_trip_delay_seconds == pytest.approx(
            0.05, abs=_NTP_FIXED_POINT_TOLERANCE
        )
        assert report.jitter_seconds == pytest.approx(
            statistics.pstdev(expected_offsets), abs=_NTP_FIXED_POINT_TOLERANCE
        )
        assert report.spread_seconds == pytest.approx(0.4, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.server_stratum == 3
        assert report.leap_state is ClockLeapState.LAST_MINUTE_HAS_61_SECONDS
        assert report.measured_at.timestamp() == pytest.approx(_BASE_TIME + 1.05)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_measurement_is_available_before_client_start() -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.05, delay=0.02)])
    clock_service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.02],
        monotonic_values=[10.0, 10.02],
    )
    client = ECNClient(_config())
    original_clock_service = client._clock_service
    client._clock_service = clock_service
    try:
        assert not client.is_ready
        report = await client.clock.measure(samples=1, timeout=1.0)
        assert report.offset_seconds == pytest.approx(0.05, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert not client.is_ready
    finally:
        await client.close()
        if original_clock_service is not None:
            await original_clock_service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_measurement_remains_available_after_mqtt_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = _ScriptedExchange([_SampleSpec(offset=0.05, delay=0.02)])
    clock_service = _service(
        exchange,
        wall_values=[_BASE_TIME, _BASE_TIME + 0.02],
        monotonic_values=[10.0, 10.02],
    )
    client = ECNClient(_config())
    original_clock_service = client._clock_service
    client._clock_service = clock_service

    async def fail_mqtt_start() -> None:
        raise ECNConnectionError("MQTT startup failed", operation="client.start")

    monkeypatch.setattr(client._mqtt_transport, "start", fail_mqtt_start)
    try:
        with pytest.raises(ECNConnectionError, match="MQTT startup failed"):
            await client.start()
        assert not client.is_ready
        assert client.status.state is ClientState.FAILED
        assert not exchange.closed

        report = await client.clock.measure(samples=1, timeout=1.0)
        assert report.offset_seconds == pytest.approx(0.05, abs=_NTP_FIXED_POINT_TOLERANCE)
        await client.close()
        await client.close()
        with pytest.raises(NotReadyError, match="clock diagnostic is closed"):
            await client.clock.measure(samples=1, timeout=1.0)
    finally:
        await client.close()
        if original_clock_service is not None:
            await original_clock_service.close()
    assert exchange.closed


def test_clock_public_methods_do_not_accept_a_per_call_endpoint() -> None:
    assert tuple(inspect.signature(Clock.measure).parameters) == (
        "self",
        "samples",
        "timeout",
    )
    assert tuple(inspect.signature(Clock.require_within).parameters) == (
        "self",
        "max_offset_seconds",
        "samples",
        "timeout",
    )


@pytest.mark.asyncio
async def test_clock_uses_configured_ecn_host_by_default_and_an_issued_override() -> None:
    endpoint = (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
    for ntp_host, expected_host in (
        (None, "127.0.0.1"),
        ("example.invalid", "example.invalid"),
    ):
        resolver = _StaticResolver(endpoint)
        exchange = _ScriptedExchange([_SampleSpec(offset=0.0, delay=0.02)])
        config = _config(ntp_host="localhost").model_copy(
            update={"ntp_host": ntp_host, "ntp_port": 321}
        )
        service = ClockService(
            config,
            wall_clock=_SequenceClock([_BASE_TIME, _BASE_TIME + 0.02]),
            monotonic_clock=_SequenceClock(_bracketed([10.0, 10.02])),
            resolver=resolver,
            exchange=exchange,
        )
        try:
            report = await service.measure(samples=1, timeout=1.0)
            assert resolver.last_host == expected_host
            assert resolver.last_port == 321
            assert report.endpoint == ClockEndpoint(host=expected_host, port=321)
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_dns_address_fallback() -> None:
    first: ResolvedEndpoint = (
        socket.AF_INET6,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("::1", 123, 0, 0),
    )
    second: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )

    async def resolve(_host: str, _port: int) -> ResolvedEndpoints:
        return first, second

    exchange = _FallbackExchange(failures=1)
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock([_BASE_TIME, _BASE_TIME + 0.01, _BASE_TIME + 0.03]),
        monotonic_clock=_SequenceClock(_bracketed([10.0, 10.01, 10.03])),
        resolver=resolve,
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=1, timeout=1.0)
        assert report.samples_completed == 1
        assert exchange.calls == [first, second]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_address_attempt_cap() -> None:
    endpoint: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )

    async def resolve(_host: str, _port: int) -> ResolvedEndpoints:
        return (endpoint,)

    exchange = _FallbackExchange(failures=10)
    service = ClockService(
        _config(),
        wall_clock=lambda: _BASE_TIME,
        monotonic_clock=lambda: 10.0,
        resolver=resolve,
        exchange=exchange,
    )
    try:
        with pytest.raises(ClockError) as caught:
            await service.measure(samples=1, timeout=1.0)
        assert caught.value.code == "clock_connection_failed"
        assert exchange.calls == [endpoint] * 10
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_address_attempt_cap_preserves_completed_samples() -> None:
    first: ResolvedEndpoint = (
        socket.AF_INET6,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("::1", 123, 0, 0),
    )
    second: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )

    async def resolve(_host: str, _port: int) -> ResolvedEndpoints:
        return first, second

    completed_samples = 9
    wall_values: list[float] = []
    monotonic_values: list[float] = []
    for index in range(completed_samples):
        wall_values.extend([_BASE_TIME + index, _BASE_TIME + index + 0.02])
        monotonic_values.extend([10.0 + index, 10.0 + index + 0.02])

    exchange = _FallbackExchange(failures=1)
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock(wall_values),
        monotonic_clock=_SequenceClock(_bracketed(monotonic_values)),
        resolver=resolve,
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=10, timeout=1.0)
        assert report.samples_requested == 10
        assert report.samples_completed == completed_samples
        assert report.offset_seconds == pytest.approx(0.0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.jitter_seconds == pytest.approx(0.0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert report.spread_seconds == pytest.approx(0.0, abs=_NTP_FIXED_POINT_TOLERANCE)
        assert len(exchange.calls) == 10
        assert exchange.calls == [first, second, *([second] * 8)]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_retries_transient_failure_while_attempt_budget_remains() -> None:
    endpoint: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )

    valid = _SampleSpec(offset=0.0, delay=0.02)
    exchange = _SequencedExchange([valid, None, valid, valid])
    service = ClockService(
        _config(),
        wall_clock=_SequenceClock(
            [
                _BASE_TIME,
                _BASE_TIME + 0.02,
                _BASE_TIME + 1,
                _BASE_TIME + 2,
                _BASE_TIME + 2.02,
                _BASE_TIME + 3,
                _BASE_TIME + 3.02,
            ]
        ),
        monotonic_clock=_SequenceClock(_bracketed([10.0, 10.02, 11.0, 12.0, 12.02, 13.0, 13.02])),
        resolver=_StaticResolver(endpoint),
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=3, timeout=1.0)
        assert report.samples_requested == 3
        assert report.samples_completed == 3
        assert exchange.calls == [endpoint] * 4
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_does_not_retry_protocol_failure_after_valid_sample() -> None:
    exchange = _SequencedExchange(
        [
            _SampleSpec(offset=0.0, delay=0.02),
            bytes(_NTP_PACKET_SIZE - 1),
        ]
    )
    service = _service(
        exchange,
        wall_values=[
            _BASE_TIME,
            _BASE_TIME + 0.02,
            _BASE_TIME + 1,
            _BASE_TIME + 1.02,
        ],
        monotonic_values=[10.0, 10.02, 11.0, 11.02],
    )
    try:
        with pytest.raises(ClockProtocolError) as caught:
            await service.measure(samples=3, timeout=1.0)
        assert caught.value.code == "clock_packet_size_invalid"
        assert len(exchange.calls) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_clock_returns_partial_sample_only_after_transient_attempt_cap() -> None:
    endpoint: ResolvedEndpoint = (
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP,
        ("127.0.0.1", 123),
    )
    exchange = _SequencedExchange(
        [
            _SampleSpec(offset=0.0, delay=0.02),
            *[OSError("synthetic endpoint failure") for _ in range(9)],
        ]
    )
    service = ClockService(
        _config(),
        wall_clock=lambda: _BASE_TIME,
        monotonic_clock=lambda: 10.0,
        resolver=_StaticResolver(endpoint),
        exchange=exchange,
    )
    try:
        report = await service.measure(samples=3, timeout=1.0)
        assert report.samples_requested == 3
        assert report.samples_completed == 1
        assert exchange.calls == [endpoint] * 10
    finally:
        await service.close()
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_allows_only_one_measurement_and_close_interrupts_it() -> None:
    exchange = _BlockingExchange()
    service = ClockService(
        _config(),
        wall_clock=lambda: _BASE_TIME,
        monotonic_clock=lambda: 10.0,
        resolver=_StaticResolver(
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, ("127.0.0.1", 123))
        ),
        exchange=exchange,
    )
    first = asyncio.create_task(service.measure(samples=1, timeout=1.0))
    await asyncio.wait_for(exchange.started.wait(), timeout=1.0)
    with pytest.raises(ResourceLimitError):
        await service.measure(samples=1, timeout=1.0)
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert exchange.closed


@pytest.mark.asyncio
async def test_clock_close_interrupts_endpoint_resolution() -> None:
    resolver = _BlockingResolver()
    exchange = _ScriptedExchange([])
    service = ClockService(
        _config(),
        resolver=resolver,
        exchange=exchange,
    )
    measurement = asyncio.create_task(service.measure(samples=1, timeout=1.0))
    await asyncio.wait_for(resolver.started.wait(), timeout=1.0)
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await measurement
    assert exchange.closed


def test_clock_profile_and_environment_endpoint_configuration(tmp_path: Path) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path),
        "ECN_BEARER_TOKEN": "synthetic-token",
        "ECN_ALLOW_INSECURE": "true",
    }
    save_profile(
        "clock",
        {
            "host": "127.0.0.1",
            "integration_name": "clock-test",
            "auth": "bearer",
            "mqtt_port": 1883,
            "ntp_host": "example.invalid",
            "ntp_port": 124,
        },
        environment,
    )
    assert load_profile("clock", environment)["ntp_port"] == 124

    profile_config = load_config(profile="clock", environment=environment)
    assert profile_config.ntp_host == "example.invalid"
    assert profile_config.ntp_port == 124

    overridden = load_config(
        profile="clock",
        environment=environment
        | {
            "ECN_NTP_HOST": "tiles.example.invalid",
            "ECN_NTP_PORT": "125",
        },
    )
    assert overridden.ntp_host == "tiles.example.invalid"
    assert overridden.ntp_port == 125


def test_clock_environment_configuration_uses_the_model_ntp_port_default() -> None:
    config = load_config(
        environment={
            "ECN_HOST": "127.0.0.1",
            "ECN_MQTT_PORT": "1883",
            "ECN_INTEGRATION_NAME": "clock-test",
            "ECN_BEARER_TOKEN": "synthetic-token",
            "ECN_ALLOW_INSECURE": "true",
        }
    )
    assert config.ntp_port == ECNConfig.model_fields["ntp_port"].default == 123


@pytest.mark.parametrize(
    ("environment_update", "message"),
    [
        ({"ECN_NTP_HOST": "https://example.invalid"}, "configuration is invalid"),
        ({"ECN_NTP_PORT": "not-secret-canary"}, "ECN NTP port must be an integer"),
        ({"ECN_NTP_PORT": "0"}, "ECN NTP port must be between 1 and 65535"),
    ],
)
def test_clock_environment_endpoint_validation_is_fail_closed_and_safe(
    environment_update: dict[str, str],
    message: str,
) -> None:
    environment = {
        "ECN_HOST": "127.0.0.1",
        "ECN_MQTT_PORT": "1883",
        "ECN_INTEGRATION_NAME": "clock-test",
        "ECN_BEARER_TOKEN": "synthetic-token",
        "ECN_ALLOW_INSECURE": "true",
    }
    with pytest.raises(ConfigurationError, match=message) as caught:
        load_config(environment=environment | environment_update)
    assert "example.invalid" not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-port", "ECN NTP port must be an integer"),
        ("0", "ECN NTP port must be between 1 and 65535"),
    ],
)
def test_clock_profile_environment_port_errors_match_environment_only_loading(
    tmp_path: Path,
    value: str,
    message: str,
) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path),
        "ECN_BEARER_TOKEN": "synthetic-token",
        "ECN_ALLOW_INSECURE": "true",
    }
    save_profile(
        "clock",
        {
            "host": "127.0.0.1",
            "integration_name": "clock-test",
            "auth": "bearer",
            "mqtt_port": 1883,
        },
        environment,
    )
    with pytest.raises(ConfigurationError, match=message):
        load_config(
            profile="clock",
            environment=environment | {"ECN_NTP_PORT": value},
        )


def test_clock_stored_profile_ntp_port_error_uses_source_neutral_wording(
    tmp_path: Path,
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path)}
    with pytest.raises(
        ConfigurationError,
        match="ECN NTP port must be between 1 and 65535",
    ):
        save_profile(
            "clock",
            {
                "host": "127.0.0.1",
                "integration_name": "clock-test",
                "auth": "bearer",
                "mqtt_port": 1883,
                "ntp_port": 0,
            },
            environment,
        )


def test_clock_config_rejects_an_unresolvable_ntp_host_shape() -> None:
    with pytest.raises(PydanticValidationError, match="host must be a DNS name"):
        _config(ntp_host="https://example.invalid")


def test_clock_configure_cli_persists_the_optional_ntp_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in (
        "ECN_PROFILE",
        "ECN_MQTT_PORT",
        "ECN_NTP_HOST",
        "ECN_NTP_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert (
        clock_cli.main(
            [
                "configure",
                "--profile",
                "clock",
                "--host",
                "127.0.0.1",
                "--integration-name",
                "clock-test",
                "--auth",
                "bearer",
                "--mqtt-username",
                "synthetic-user",
                "--ntp-host",
                "example.invalid",
                "--ntp-port",
                "124",
                "--non-interactive",
            ]
        )
        == 0
    )
    configured = load_profile("clock", dict(os.environ))
    assert configured["ntp_host"] == "example.invalid"
    assert configured["ntp_port"] == 124
    assert "Configured ECN profile 'clock'" in capsys.readouterr().out


def _clock_report(*, within_tolerance: bool) -> ClockReport:
    return ClockReport(
        endpoint=ClockEndpoint(host="example.invalid"),
        offset_seconds=0.01 if within_tolerance else 1.0,
        round_trip_delay_seconds=0.02,
        local_capture_uncertainty_seconds=0,
        jitter_seconds=0,
        spread_seconds=0,
        samples_requested=1,
        samples_completed=1,
        server_version=4,
        server_stratum=2,
        leap_state=ClockLeapState.NO_WARNING,
        measured_at=datetime(2026, 1, 1, tzinfo=UTC),
        max_offset_seconds=0.5,
        within_tolerance=within_tolerance,
    )


def test_clock_report_rejects_a_contradictory_tolerance_result() -> None:
    valid = _clock_report(within_tolerance=True)
    with pytest.raises(PydanticValidationError, match="does not match"):
        ClockReport.model_validate(
            {
                **valid.model_dump(),
                "offset_seconds": 1.0,
                "within_tolerance": True,
            }
        )


def test_clock_cli_check_reports_success_without_starting_mqtt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _clock_report(within_tolerance=True)
    calls: list[str] = []

    class _FakeClock:
        async def require_within(self, **_arguments: object) -> ClockReport:
            calls.append("measure")
            return report

    class _FakeClient:
        clock = _FakeClock()

        def __init__(self, _config: ECNConfig) -> None:
            calls.append("construct")

        async def start(self) -> None:
            raise AssertionError("clock diagnostics must not start MQTT")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(clock_cli, "_load_config", lambda *, profile: _config())
    monkeypatch.setattr(clock_cli, "ECNClient", _FakeClient)

    assert (
        clock_cli.main(
            ["clock", "check", "--profile", "clock", "--max-offset", "0.5", "--samples", "1"]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert '"within_tolerance": true' in rendered
    assert '"leap_state": "no_warning"' in rendered
    assert calls == ["construct", "measure", "close"]


def test_clock_cli_check_returns_three_with_the_valid_report_when_outside_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _clock_report(within_tolerance=False)

    class _FakeClock:
        async def require_within(self, **_arguments: object) -> ClockReport:
            raise ClockToleranceError(report)

    class _FakeClient:
        clock = _FakeClock()

        def __init__(self, _config: ECNConfig) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(clock_cli, "_load_config", lambda *, profile: _config())
    monkeypatch.setattr(clock_cli, "ECNClient", _FakeClient)

    assert clock_cli.main(["clock", "check", "--max-offset", "0.5"]) == 3
    assert '"within_tolerance": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--samples", "0"],
        ["--samples", "11"],
        ["--timeout", "0"],
        ["--timeout", "61"],
        ["--timeout", "nan"],
    ],
)
def test_clock_cli_rejects_invalid_bounds_before_loading_configuration(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        clock_cli,
        "_load_config",
        lambda **_arguments: pytest.fail("invalid CLI bounds must fail before configuration"),
    )
    with pytest.raises(SystemExit) as caught:
        clock_cli.main(["clock", "check", "--max-offset", "0.5", *arguments])
    assert caught.value.code == 2
    assert "invalid arguments" in capsys.readouterr().err


def test_clock_cli_check_redacts_operational_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeClock:
        async def require_within(self, **_arguments: object) -> ClockReport:
            raise ClockError("ECN clock endpoint could not be measured")

    class _FakeClient:
        clock = _FakeClock()

        def __init__(self, _config: ECNConfig) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(clock_cli, "_load_config", lambda *, profile: _config())
    monkeypatch.setattr(clock_cli, "ECNClient", _FakeClient)

    with pytest.raises(SystemExit) as caught:
        clock_cli.main(["clock", "check", "--max-offset", "0.5"])
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "clock failed: ECN clock endpoint could not be measured" in error
    assert "example.invalid" not in error
