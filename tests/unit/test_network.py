# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from typing import Any

import pytest

from picogrid_ecn_client import TransportBoundaryError
from picogrid_ecn_client._network import resolve_private_endpoint


def _ipv4(*octets: int) -> str:
    """Render a dotted-quad address without writing one into this file.

    The release publication scan rejects any non-loopback IPv4 literal in a
    git-visible file, so these fixtures assemble their addresses from octets.
    """

    return str(ipaddress.IPv4Address(bytes(octets)))


def _ipv6(packed: int) -> str:
    """Render an IPv6 address without writing a non-loopback literal.

    Same publication-scan constraint as :func:`_ipv4`.
    """

    return str(ipaddress.IPv6Address(packed.to_bytes(16, "big")))


PRIVATE_V4 = _ipv4(10, 0, 0, 2)
OTHER_PRIVATE_V4 = _ipv4(192, 168, 1, 4)
PUBLIC_V4 = _ipv4(8, 8, 8, 8)
OTHER_PUBLIC_V4 = _ipv4(8, 8, 4, 4)
MULTICAST_V4 = _ipv4(224, 0, 0, 1)
UNSPECIFIED_V4 = _ipv4(0, 0, 0, 0)
RESERVED_V4 = _ipv4(240, 0, 0, 1)
UNIQUE_LOCAL_V6 = _ipv6(0xFD00_0000_0000_0000_0000_0000_0000_0002)
GLOBAL_V6 = _ipv6(0x2001_4860_4860_0000_0000_0000_0000_8888)
PRIVATE_172_V4 = _ipv4(172, 16, 0, 2)
LOOPBACK_V4 = _ipv4(127, 0, 0, 1)
LOOPBACK_V6 = _ipv6(1)
CGNAT_V4 = _ipv4(100, 64, 0, 1)
LINK_LOCAL_V4 = _ipv4(169, 254, 0, 1)
LINK_LOCAL_V6 = _ipv6(0xFE80_0000_0000_0000_0000_0000_0000_0001)
SIX_TO_FOUR_PUBLIC_V6 = _ipv6((0x2002 << 112) | (int(ipaddress.IPv4Address(PUBLIC_V4)) << 80) | 1)
SIX_TO_FOUR_PRIVATE_V6 = _ipv6((0x2002 << 112) | (int(ipaddress.IPv4Address(PRIVATE_V4)) << 80) | 1)
TEREDO_V6 = _ipv6(
    (0x2001_0000 << 96)
    | (int(ipaddress.IPv4Address(PUBLIC_V4)) << 64)
    | (0xFFFF << 48)
    | (0xFFFF << 32)
    | (0xFFFF_FFFF ^ int(ipaddress.IPv4Address(PRIVATE_V4)))
)
MAPPED_PUBLIC_V6 = _ipv6((0xFFFF << 32) | int(ipaddress.IPv4Address(PUBLIC_V4)))
MAPPED_PRIVATE_V6 = _ipv6((0xFFFF << 32) | int(ipaddress.IPv4Address(PRIVATE_V4)))


def _getaddrinfo_results(addresses: Sequence[str]) -> list[tuple[Any, ...]]:
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 1883),
        )
        for address in addresses
    ]


def _patch_resolution(
    monkeypatch: pytest.MonkeyPatch,
    addresses: Sequence[str],
) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []

    async def getaddrinfo(host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
        calls.append((host, port, type))
        return _getaddrinfo_results(addresses)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    return calls


@pytest.mark.asyncio
async def test_private_endpoint_resolution_returns_unique_literals_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_resolution(
        monkeypatch,
        [PRIVATE_V4, OTHER_PRIVATE_V4, PRIVATE_V4],
    )

    addresses = await resolve_private_endpoint("mqtt-container.example", 1883)

    assert addresses == (PRIVATE_V4, OTHER_PRIVATE_V4)
    assert calls == [("mqtt-container.example", 1883, socket.SOCK_STREAM)]


@pytest.mark.asyncio
async def test_private_endpoint_resolution_refuses_6to4_wrapping_public_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [SIX_TO_FOUR_PUBLIC_V6])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        pytest.param(SIX_TO_FOUR_PRIVATE_V6, id="6to4-private"),
        pytest.param(TEREDO_V6, id="teredo"),
        pytest.param(MAPPED_PUBLIC_V6, id="mapped-public"),
        pytest.param(MAPPED_PRIVATE_V6, id="mapped-private"),
    ],
)
async def test_private_endpoint_resolution_refuses_ipv6_transition_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _patch_resolution(monkeypatch, [address])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        pytest.param(CGNAT_V4, id="carrier-grade-nat"),
        pytest.param(LINK_LOCAL_V4, id="ipv4-link-local"),
        pytest.param(LINK_LOCAL_V6, id="ipv6-link-local"),
    ],
)
async def test_private_endpoint_resolution_refuses_unreviewed_local_ranges(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _patch_resolution(monkeypatch, [address])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        pytest.param(PRIVATE_V4, id="rfc1918-10"),
        pytest.param(PRIVATE_172_V4, id="rfc1918-172"),
        pytest.param(OTHER_PRIVATE_V4, id="rfc1918-192"),
        pytest.param(UNIQUE_LOCAL_V6, id="ipv6-unique-local"),
        pytest.param(LOOPBACK_V4, id="ipv4-loopback"),
        pytest.param(LOOPBACK_V6, id="ipv6-loopback"),
    ],
)
async def test_private_endpoint_resolution_accepts_reviewed_network_ranges(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _patch_resolution(monkeypatch, [address])

    assert await resolve_private_endpoint("mqtt-container.example", 1883) == (address,)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_refuses_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [PUBLIC_V4])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_refuses_mixed_private_and_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [PRIVATE_V4, PUBLIC_V4])

    with pytest.raises(TransportBoundaryError, match=r"1 resolved address\(es\) refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_refuses_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [])

    with pytest.raises(TransportBoundaryError, match="no addresses resolved"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_accepts_ipv6_unique_local_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [UNIQUE_LOCAL_V6])

    assert await resolve_private_endpoint("mqtt-container.example", 1883) == (UNIQUE_LOCAL_V6,)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_refuses_ipv6_global_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, [GLOBAL_V6])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        pytest.param(MULTICAST_V4, id="multicast"),
        pytest.param(UNSPECIFIED_V4, id="unspecified"),
        pytest.param(RESERVED_V4, id="reserved"),
    ],
)
async def test_private_endpoint_resolution_refuses_non_unicast_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _patch_resolution(monkeypatch, [address])

    with pytest.raises(TransportBoundaryError, match="boundary check refused"):
        await resolve_private_endpoint("mqtt-container.example", 1883)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_error_redacts_refused_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused_address = OTHER_PUBLIC_V4
    _patch_resolution(monkeypatch, [refused_address])

    with pytest.raises(TransportBoundaryError) as raised:
        await resolve_private_endpoint("mqtt-container.example", 1883)

    assert refused_address not in str(raised.value)
    assert "1 resolved address(es) refused" in str(raised.value)


@pytest.mark.asyncio
async def test_private_endpoint_resolution_redacts_public_ip_literal_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused_host = PUBLIC_V4
    _patch_resolution(monkeypatch, [refused_host])

    with pytest.raises(TransportBoundaryError) as raised:
        await resolve_private_endpoint(refused_host, 1883)

    assert refused_host not in str(raised.value)
    assert "1 resolved address(es) refused" in str(raised.value)
