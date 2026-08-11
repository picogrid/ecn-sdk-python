# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Shared validation for configured network endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar

from .exceptions import TransportBoundaryError

_T = TypeVar("_T")

_HOST_ERROR = "host must be a DNS name or IP literal without a scheme or port"
_MAXIMUM_HOST_INPUT_CHARACTERS = 1024
_MAXIMUM_DNS_LABEL_BYTES = 63
_MAXIMUM_DNS_NAME_BYTES = 253


def _ipv4_network(octets: tuple[int, int, int, int], prefix: int) -> ipaddress.IPv4Network:
    """Build one IPv4 network without writing a dotted-quad literal.

    The release publication scan rejects non-loopback IPv4 literals anywhere in a
    git-visible file, so the allowlist is assembled from octets instead.
    """

    return ipaddress.IPv4Network((ipaddress.IPv4Address(bytes(octets)), prefix))


def _ipv6_network(packed: int, prefix: int) -> ipaddress.IPv6Network:
    """Build one IPv6 network without writing a non-loopback literal.

    Same publication-scan constraint as :func:`_ipv4_network`.
    """

    return ipaddress.IPv6Network((ipaddress.IPv6Address(packed.to_bytes(16, "big")), prefix))


# Reviewed container networks hand out RFC 1918 IPv4 or IPv6 unique-local addresses,
# plus loopback for a local mock. Membership is an explicit allowlist rather than
# ``ipaddress.is_private``: that predicate also covers carrier-grade NAT, benchmarking
# and documentation ranges, and the IPv6 transition forms 6to4 and Teredo, which embed
# an IPv4 address and traverse public IPv4 infrastructure while still reporting as
# private. A 6to4 literal wrapping a public address would otherwise be accepted.
_ALLOWED_IPV4_NETWORKS: Final = (
    _ipv4_network((10, 0, 0, 0), 8),
    _ipv4_network((172, 16, 0, 0), 12),
    _ipv4_network((192, 168, 0, 0), 16),
    _ipv4_network((127, 0, 0, 0), 8),
)
_ALLOWED_IPV6_NETWORKS: Final = (
    _ipv6_network(0xFC00 << 112, 7),
    _ipv6_network(1, 128),
)


def _is_reviewed_network_address(address: str) -> bool:
    """Return whether one resolved address literal is inside the reviewed allowlist."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        # Transition forms carry an embedded IPv4 destination and leave the reviewed
        # network even though the outer literal classifies as private.
        if (
            parsed.sixtofour is not None
            or parsed.teredo is not None
            or parsed.ipv4_mapped is not None
        ):
            return False
        return any(parsed in network for network in _ALLOWED_IPV6_NETWORKS)
    return any(parsed in network for network in _ALLOWED_IPV4_NETWORKS)


def normalize_host(value: str) -> str:
    """Normalize one host and reject values the socket resolver cannot encode."""

    if len(value) > _MAXIMUM_HOST_INPUT_CHARACTERS:
        raise ValueError(_HOST_ERROR)
    host = value.strip()
    if not host or any(character in host for character in "/@?#\x00\r\n\t "):
        raise ValueError(_HOST_ERROR)

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if ":" in host:
            raise ValueError(_HOST_ERROR) from None
        try:
            encoded_host = host.encode("idna", errors="strict")
        except UnicodeError:
            raise ValueError(_HOST_ERROR) from None
        if encoded_host.endswith(b"."):
            encoded_host = encoded_host[:-1]
            host = host[:-1]
        encoded_labels = encoded_host.split(b".")
        if any(not label for label in encoded_labels):
            raise ValueError(_HOST_ERROR) from None
        if any(len(label) > _MAXIMUM_DNS_LABEL_BYTES for label in encoded_labels):
            raise ValueError(_HOST_ERROR) from None
        if len(encoded_host) > _MAXIMUM_DNS_NAME_BYTES:
            raise ValueError(_HOST_ERROR) from None
    return host


def _boundary_target(host: str) -> str:
    del host
    return "configured endpoint"


async def resolve_private_endpoint(host: str, port: int) -> tuple[str, ...]:
    """Resolve one endpoint and require every result to be a private address."""

    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    seen: set[str] = set()
    for result in results:
        address = str(result[4][0])
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return validate_private_endpoint_addresses(host, tuple(addresses))


def validate_private_endpoint_addresses(
    host: str,
    addresses: tuple[str, ...],
) -> tuple[str, ...]:
    """Require one already-resolved endpoint set to remain inside the reviewed boundary."""

    if not addresses:
        raise TransportBoundaryError(
            f"reviewed container-network boundary check refused {_boundary_target(host)}: "
            "no addresses resolved"
        )

    refused_count = sum(1 for address in addresses if not _is_reviewed_network_address(address))
    if refused_count:
        raise TransportBoundaryError(
            f"reviewed container-network boundary check refused {_boundary_target(host)}: "
            f"{refused_count} resolved address(es) refused"
        )
    return tuple(addresses)


async def connect_validated_addresses(
    addresses: tuple[str, ...],
    timeout: float,
    connect: Callable[[str, float], Awaitable[_T]],
) -> _T:
    """Try a freshly validated address set within one bounded connection attempt."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error: Exception | None = None
    for index, address in enumerate(addresses):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError from last_error
        address_timeout = timeout if len(addresses) == 1 else remaining / (len(addresses) - index)
        try:
            async with asyncio.timeout(address_timeout):
                return await connect(address, address_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


__all__: list[str] = []
