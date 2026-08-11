# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Bounded ECN-relative NTPv4/SNTP clock diagnostics."""

from __future__ import annotations

import asyncio
import builtins
import math
import socket
import statistics
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeAlias

from ..config import ECNConfig
from ..exceptions import (
    ClockError,
    ClockProtocolError,
    ClockToleranceError,
    NotReadyError,
    ResourceLimitError,
    TimeoutError,
    ValidationError,
)
from ..models.clock import ClockEndpoint, ClockLeapState, ClockReport

# Public NTPv4 wire values come from RFC 5905.
_NTP_PACKET_SIZE = 48
_MAX_NTP_DATAGRAM_SIZE = 1024
_NTP_MAC_SIZES = frozenset({20, 24})
_NTP_CRYPTO_NAK = bytes(4)
_NTP_EXTENSION_HEADER_SIZE = 4
_MIN_NTP_EXTENSION_SIZE = 16
_MIN_FINAL_NTP_EXTENSION_SIZE = 28
_NTP_UNIX_EPOCH_OFFSET = 2_208_988_800
_NTP_ERA_SECONDS = 1 << 32
_MAX_LOCAL_CLOCK_STEP_SECONDS = 0.01
_MAX_CLOCK_CAPTURE_SPAN_SECONDS = 0.01
_MINIMUM_DELAY_SECONDS = max(time.get_clock_info("time").resolution, 1 / _NTP_ERA_SECONDS)
_MAX_TIMESTAMP_DISTANCE_SECONDS = 10 * 366 * 24 * 60 * 60
_MAX_REQUEST_ATTEMPTS = 10
_CLIENT_HEADER = (0 << 6) | (4 << 3) | 3
_LEAP_STATES = (
    ClockLeapState.NO_WARNING,
    ClockLeapState.LAST_MINUTE_HAS_61_SECONDS,
    ClockLeapState.LAST_MINUTE_HAS_59_SECONDS,
    ClockLeapState.UNSYNCHRONIZED,
)
# Closed allowlist drawn from the public IANA NTP Kiss-o'-Death registry. Reference IDs in
# stratum-zero responses are untrusted wire bytes; only registered values cross
# the public error boundary.
_KNOWN_NTP_KISS_CODES = frozenset(
    {
        b"ACST",
        b"AUTH",
        b"AUTO",
        b"BCST",
        b"CRYP",
        b"DENY",
        b"DROP",
        b"INIT",
        b"MCST",
        b"NKEY",
        b"NTSN",
        b"RATE",
        b"RMOT",
        b"RSTR",
        b"STEP",
    }
)
_UNKNOWN_NTP_KISS_CODE = "UNSPECIFIED"

WallClock: TypeAlias = Callable[[], float]
MonotonicClock: TypeAlias = Callable[[], float]
ResolvedEndpoint: TypeAlias = tuple[int, int, int, tuple[object, ...]]
ResolvedEndpoints: TypeAlias = tuple[ResolvedEndpoint, ...]
Resolver: TypeAlias = Callable[[str, int], Awaitable[ResolvedEndpoints]]


@dataclass(frozen=True, slots=True)
class _ClockSample:
    offset: float
    delay: float
    local_capture_uncertainty: float
    version: int
    stratum: int
    leap_state: ClockLeapState
    received_at: float


@dataclass(frozen=True, slots=True)
class _ClockReading:
    wall: float
    monotonic: float
    uncertainty: float
    capture_span: float


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    packet: bytes
    sent: _ClockReading


_RequestFactory: TypeAlias = Callable[[], _PreparedRequest]
_ClockCapture: TypeAlias = Callable[[], _ClockReading]


@dataclass(frozen=True, slots=True)
class _CompletedExchange:
    response: bytes
    request: _PreparedRequest
    received: _ClockReading


class DatagramExchange(Protocol):
    async def exchange(
        self,
        endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange: ...

    async def close(self) -> None: ...


class _NtpDatagramExchange:
    """One-request-per-sample UDP exchange with deterministic socket cleanup."""

    def __init__(self) -> None:
        self._sockets: set[socket.socket] = set()
        self._closed = False

    @property
    def active_socket_count(self) -> int:
        return len(self._sockets)

    async def exchange(
        self,
        endpoint: ResolvedEndpoint,
        request_factory: _RequestFactory,
        receive_clock: _ClockCapture,
    ) -> _CompletedExchange:
        if self._closed:
            raise NotReadyError("clock diagnostic is closed", operation="clock.measure")
        family, socktype, protocol, address = endpoint
        datagram = socket.socket(family, socktype, protocol)
        datagram.setblocking(False)
        self._sockets.add(datagram)
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_connect(datagram, address)
            prepared = request_factory()
            await loop.sock_sendall(datagram, prepared.packet)
            response = await loop.sock_recv(datagram, _MAX_NTP_DATAGRAM_SIZE + 1)
            received = receive_clock()
            return _CompletedExchange(
                response=response,
                request=prepared,
                received=received,
            )
        finally:
            self._sockets.discard(datagram)
            datagram.close()

    async def close(self) -> None:
        self._closed = True
        sockets, self._sockets = tuple(self._sockets), set()
        for datagram in sockets:
            datagram.close()


async def _resolve_endpoints(host: str, port: int) -> ResolvedEndpoints:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_DGRAM,
        proto=socket.IPPROTO_UDP,
    )
    endpoints: list[ResolvedEndpoint] = []
    for family, socktype, protocol, _canonical_name, address in results:
        if family in {socket.AF_INET, socket.AF_INET6} and socktype == socket.SOCK_DGRAM:
            endpoint = (family, socktype, protocol, address)
            if endpoint not in endpoints:
                endpoints.append(endpoint)
            if len(endpoints) == _MAX_REQUEST_ATTEMPTS:
                break
    if not endpoints:
        raise OSError("no UDP address was resolved")
    return tuple(endpoints)


def _unix_to_ntp(value: float) -> bytes:
    ntp = value + _NTP_UNIX_EPOCH_OFFSET
    seconds = math.floor(ntp)
    fraction = round((ntp - seconds) * _NTP_ERA_SECONDS)
    if fraction == _NTP_ERA_SECONDS:
        seconds += 1
        fraction = 0
    return struct.pack("!II", seconds % _NTP_ERA_SECONDS, fraction)


def _ntp_to_unix(value: bytes, *, reference: float) -> float:
    raw_seconds, raw_fraction = struct.unpack("!II", value)
    seconds = int(raw_seconds)
    fraction = int(raw_fraction)
    base = seconds - _NTP_UNIX_EPOCH_OFFSET + fraction / _NTP_ERA_SECONDS
    era = round((reference - base) / _NTP_ERA_SECONDS)
    return base + era * _NTP_ERA_SECONDS


def _request_packet(transmit_timestamp: bytes) -> bytes:
    packet = bytearray(_NTP_PACKET_SIZE)
    packet[0] = _CLIENT_HEADER
    packet[40:48] = transmit_timestamp
    return bytes(packet)


def _kiss_code(packet: bytes) -> str:
    raw = packet[12:16]
    if raw in _KNOWN_NTP_KISS_CODES:
        return raw.decode("ascii")
    return _UNKNOWN_NTP_KISS_CODE


def _is_standard_mac_or_crypto_nak_tail(tail: memoryview) -> bool:
    # The 20- and 24-octet MAC contents are opaque without negotiated key context.
    return len(tail) in _NTP_MAC_SIZES or (
        len(tail) == len(_NTP_CRYPTO_NAK) and tail.tobytes() == _NTP_CRYPTO_NAK
    )


def _validate_response_tail(packet: bytes) -> None:
    tail = memoryview(packet)[_NTP_PACKET_SIZE:]
    if not tail or _is_standard_mac_or_crypto_nak_tail(tail):
        return

    offset = 0
    final_extension_size = 0
    while offset < len(tail):
        remaining = len(tail) - offset
        if offset and _is_standard_mac_or_crypto_nak_tail(tail[offset:]):
            return
        if remaining < _NTP_EXTENSION_HEADER_SIZE:
            break
        _field_type, field_size = struct.unpack_from("!HH", tail, offset)
        if field_size < _MIN_NTP_EXTENSION_SIZE or field_size % 4 != 0 or field_size > remaining:
            break
        final_extension_size = field_size
        offset += field_size
    else:
        if final_extension_size >= _MIN_FINAL_NTP_EXTENSION_SIZE:
            return

    raise ClockProtocolError(
        "ECN clock service returned malformed trailing fields",
        code="clock_packet_size_invalid",
        operation="clock.measure",
    )


def _parse_response(
    packet: bytes,
    *,
    request_transmit: bytes,
    sent: _ClockReading,
    received: _ClockReading,
) -> _ClockSample:
    if not _NTP_PACKET_SIZE <= len(packet) <= _MAX_NTP_DATAGRAM_SIZE or len(packet) % 4 != 0:
        raise ClockProtocolError(
            "ECN clock service returned an invalid packet size",
            code="clock_packet_size_invalid",
            operation="clock.measure",
        )
    _validate_response_tail(packet)

    leap = (packet[0] >> 6) & 0b11
    version = (packet[0] >> 3) & 0b111
    mode = packet[0] & 0b111
    stratum = packet[1]
    if version != 4:
        raise ClockProtocolError(
            "ECN clock service returned an unsupported NTP version",
            code="clock_version_invalid",
            operation="clock.measure",
        )
    if mode != 4:
        raise ClockProtocolError(
            "ECN clock service did not return a server-mode response",
            code="clock_mode_invalid",
            operation="clock.measure",
        )
    if packet[24:32] != request_transmit:
        raise ClockProtocolError(
            "ECN clock response does not match the request",
            code="clock_originate_mismatch",
            operation="clock.measure",
        )
    if stratum == 0:
        raise ClockProtocolError(
            "ECN clock service rejected the request",
            code="clock_kiss_of_death",
            operation="clock.measure",
            details={"classification": _kiss_code(packet)},
        )
    if not 1 <= stratum <= 15:
        raise ClockProtocolError(
            "ECN clock service returned an invalid stratum",
            code="clock_stratum_invalid",
            operation="clock.measure",
        )
    leap_state = _LEAP_STATES[leap]
    if leap_state is ClockLeapState.UNSYNCHRONIZED:
        raise ClockProtocolError(
            "ECN clock service reports an unsynchronized clock",
            code="clock_unsynchronized",
            operation="clock.measure",
        )
    receive_raw = packet[32:40]
    transmit_raw = packet[40:48]
    if receive_raw == bytes(8) or transmit_raw == bytes(8):
        raise ClockProtocolError(
            "ECN clock response omitted a required timestamp",
            code="clock_timestamp_missing",
            operation="clock.measure",
        )

    wall_elapsed = received.wall - sent.wall
    monotonic_elapsed = received.monotonic - sent.monotonic
    elapsed_uncertainty = sent.uncertainty + received.uncertainty
    elapsed_divergence = abs(wall_elapsed - monotonic_elapsed)
    if (
        monotonic_elapsed < 0
        or elapsed_divergence > _MAX_LOCAL_CLOCK_STEP_SECONDS + elapsed_uncertainty
    ):
        raise ClockProtocolError(
            "local clock changed during the ECN clock measurement",
            code="clock_local_step_detected",
            operation="clock.measure",
        )

    reference = sent.wall + monotonic_elapsed / 2
    server_received = _ntp_to_unix(receive_raw, reference=reference)
    server_transmitted = _ntp_to_unix(transmit_raw, reference=reference)
    if server_transmitted < server_received:
        raise ClockProtocolError(
            "ECN clock response timestamps are inconsistent",
            code="clock_timestamp_order_invalid",
            operation="clock.measure",
        )
    if (
        max(
            abs(server_received - sent.wall),
            abs(server_transmitted - received.wall),
        )
        > _MAX_TIMESTAMP_DISTANCE_SECONDS
    ):
        raise ClockProtocolError(
            "ECN clock response timestamp is outside the supported era window",
            code="clock_timestamp_invalid",
            operation="clock.measure",
        )

    raw_delay = monotonic_elapsed - (server_transmitted - server_received)
    offset = ((server_received - sent.wall) + (server_transmitted - received.wall)) / 2
    if not math.isfinite(raw_delay) or not math.isfinite(offset):
        raise ClockProtocolError(
            "ECN clock response produced an invalid delay or offset",
            code="clock_derived_value_invalid",
            operation="clock.measure",
        )
    return _ClockSample(
        offset=offset,
        delay=max(raw_delay, _MINIMUM_DELAY_SECONDS),
        local_capture_uncertainty=max(
            elapsed_uncertainty,
            (elapsed_divergence + elapsed_uncertainty) / 2,
        ),
        version=version,
        stratum=stratum,
        leap_state=leap_state,
        received_at=received.wall,
    )


class ClockService:
    """Measure ECN-relative offset without changing or correcting either clock."""

    def __init__(
        self,
        config: ECNConfig,
        *,
        wall_clock: WallClock = time.time,
        monotonic_clock: MonotonicClock = time.monotonic,
        resolver: Resolver = _resolve_endpoints,
        exchange: DatagramExchange | None = None,
    ) -> None:
        self._host = config.ntp_host or config.host
        self._port = config.ntp_port
        self._default_timeout = min(config.operation_timeout, 60.0)
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._resolver = resolver
        self._exchange = exchange or _NtpDatagramExchange()
        self._measurement_active = False
        self._active_tasks: set[asyncio.Task[ClockReport]] = set()
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def measure(self, *, samples: int, timeout: float | None) -> ClockReport:
        self._validate_measurement(samples=samples, timeout=timeout)
        if self._closed:
            raise NotReadyError("clock diagnostic is closed", operation="clock.measure")
        if self._measurement_active:
            raise ResourceLimitError(
                "one ECN clock measurement is already active",
                operation="clock.measure",
            )

        self._measurement_active = True
        operation = asyncio.create_task(
            self._run_measurement(samples=samples, timeout=timeout),
            name="picogrid-ecn-clock-measure",
        )
        self._active_tasks.add(operation)
        try:
            return await operation
        finally:
            self._measurement_active = False
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            self._active_tasks.discard(operation)

    async def _run_measurement(
        self,
        *,
        samples: int,
        timeout: float | None,
    ) -> ClockReport:
        measurement_timeout = timeout or self._default_timeout
        try:
            async with asyncio.timeout(measurement_timeout):
                return await self._measure(samples, measurement_timeout=measurement_timeout)
        except builtins.TimeoutError:
            raise TimeoutError(
                "ECN clock measurement timed out",
                code="clock_timeout",
                operation="clock.measure",
            ) from None
        except asyncio.CancelledError:
            raise
        except ClockError:
            raise
        except (OSError, OverflowError, ValueError):
            raise ClockError(
                "ECN clock endpoint could not be measured",
                code="clock_connection_failed",
                operation="clock.measure",
            ) from None

    async def require_within(
        self,
        *,
        max_offset_seconds: float,
        samples: int,
        timeout: float | None,
    ) -> ClockReport:
        if (
            isinstance(max_offset_seconds, bool)
            or not isinstance(max_offset_seconds, (int, float))
            or not math.isfinite(max_offset_seconds)
            or not 0 <= max_offset_seconds <= 86_400
        ):
            raise ValidationError(
                "max_offset_seconds must be a finite non-negative number of at most 86400",
                operation="clock.require_within",
            )
        measured = await self.measure(samples=samples, timeout=timeout)
        report = ClockReport.model_validate(
            {
                **measured.model_dump(),
                "max_offset_seconds": float(max_offset_seconds),
                "within_tolerance": (
                    abs(measured.offset_seconds) + measured.local_capture_uncertainty_seconds
                    <= max_offset_seconds
                ),
            }
        )
        if not report.within_tolerance:
            raise ClockToleranceError(report)
        return report

    async def _measure(self, samples: int, *, measurement_timeout: float) -> ClockReport:
        endpoints = await self._resolver(self._host, self._port)
        if not endpoints:
            raise OSError("no UDP address was resolved")
        attempt_timeout = measurement_timeout / _MAX_REQUEST_ATTEMPTS
        results: list[_ClockSample] = []
        initial_clock_reading: _ClockReading | None = None
        attempts = 0
        selected_endpoint: ResolvedEndpoint | None = None
        while len(results) < samples and attempts < _MAX_REQUEST_ATTEMPTS:
            candidates = (
                endpoints
                if selected_endpoint is None
                else (
                    selected_endpoint,
                    *(endpoint for endpoint in endpoints if endpoint != selected_endpoint),
                )
            )
            sample: _ClockSample | None = None
            for endpoint in candidates:
                if attempts == _MAX_REQUEST_ATTEMPTS:
                    break
                attempts += 1

                def prepare_request() -> _PreparedRequest:
                    nonlocal initial_clock_reading
                    sent = self._capture_clocks()
                    if initial_clock_reading is None:
                        initial_clock_reading = sent
                        baseline = sent
                    else:
                        baseline = initial_clock_reading
                    self._validate_clock_alignment(
                        initial=baseline,
                        current=sent,
                    )
                    transmit_timestamp = _unix_to_ntp(sent.wall)
                    return _PreparedRequest(
                        packet=_request_packet(transmit_timestamp),
                        sent=sent,
                    )

                try:
                    async with asyncio.timeout(attempt_timeout):
                        completed = await self._exchange.exchange(
                            endpoint,
                            prepare_request,
                            self._capture_clocks,
                        )
                except (builtins.TimeoutError, OSError):
                    continue
                alignment_baseline = initial_clock_reading
                if alignment_baseline is None:
                    alignment_baseline = completed.request.sent
                self._validate_clock_alignment(
                    initial=alignment_baseline,
                    current=completed.received,
                )
                sample = _parse_response(
                    completed.response,
                    request_transmit=completed.request.packet[40:48],
                    sent=completed.request.sent,
                    received=completed.received,
                )
                selected_endpoint = endpoint
                break
            if sample is None:
                continue
            results.append(sample)

        if not results:
            raise OSError("resolved UDP addresses did not answer")
        selected = min(results, key=lambda sample: sample.delay)
        offsets = [sample.offset for sample in results]
        return ClockReport(
            endpoint=ClockEndpoint(host=self._host, port=self._port),
            offset_seconds=selected.offset,
            round_trip_delay_seconds=selected.delay,
            local_capture_uncertainty_seconds=selected.local_capture_uncertainty,
            jitter_seconds=statistics.pstdev(offsets) if len(offsets) > 1 else 0.0,
            spread_seconds=max(offsets) - min(offsets),
            samples_requested=samples,
            samples_completed=len(results),
            server_version=selected.version,
            server_stratum=selected.stratum,
            leap_state=selected.leap_state,
            measured_at=datetime.fromtimestamp(selected.received_at, tz=UTC),
        )

    @staticmethod
    def _validate_measurement(*, samples: int, timeout: float | None) -> None:
        if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 10:
            raise ValidationError(
                "samples must be an integer between 1 and 10",
                operation="clock.measure",
            )
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValidationError(
                "timeout must be a finite positive number of at most 60 seconds",
                operation="clock.measure",
            )

    def _capture_clocks(self) -> _ClockReading:
        monotonic_before = self._monotonic_clock()
        wall = self._wall_clock()
        monotonic_after = self._monotonic_clock()
        if not all(math.isfinite(value) for value in (wall, monotonic_before, monotonic_after)):
            raise ClockProtocolError(
                "local clock returned an invalid value during measurement",
                code="clock_local_value_invalid",
                operation="clock.measure",
            )
        if monotonic_after < monotonic_before:
            raise ClockProtocolError(
                "local monotonic clock moved backwards during measurement",
                code="clock_local_step_detected",
                operation="clock.measure",
            )
        capture_span = monotonic_after - monotonic_before
        if capture_span > _MAX_CLOCK_CAPTURE_SPAN_SECONDS:
            raise ClockProtocolError(
                "local clock capture was too uncertain for an ECN clock measurement",
                code="clock_local_read_uncertain",
                operation="clock.measure",
            )
        return _ClockReading(
            wall=wall,
            monotonic=(monotonic_before + monotonic_after) / 2,
            uncertainty=capture_span / 2,
            capture_span=capture_span,
        )

    @staticmethod
    def _validate_clock_alignment(
        *,
        initial: _ClockReading,
        current: _ClockReading,
    ) -> None:
        initial_delta = initial.wall - initial.monotonic
        current_delta = current.wall - current.monotonic
        allowed_uncertainty = initial.uncertainty + current.uncertainty
        if abs(current_delta - initial_delta) > _MAX_LOCAL_CLOCK_STEP_SECONDS + allowed_uncertainty:
            raise ClockProtocolError(
                "local clock changed during the ECN clock measurement",
                code="clock_local_step_detected",
                operation="clock.measure",
            )

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active_tasks)
            for task in active:
                task.cancel()
            try:
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
            finally:
                self._active_tasks.difference_update(active)
                await self._exchange.close()


__all__ = ["ClockService"]
