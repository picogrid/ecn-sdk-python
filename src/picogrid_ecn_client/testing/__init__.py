# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Lazy exports for deterministic, loopback-only MQTT v5 testing facilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mock_ecn import (
        FULL_ACCESS_TOKEN,
        NO_ACCESS_TOKEN,
        READ_ONLY_TOKEN,
        MockECN,
        MockEvents,
        MockScenario,
    )

_EXPORTS = frozenset(
    {
        "FULL_ACCESS_TOKEN",
        "NO_ACCESS_TOKEN",
        "READ_ONLY_TOKEN",
        "MockECN",
        "MockEvents",
        "MockScenario",
    }
)


def __getattr__(name: str) -> object:
    """Load one supported offline testing export on first access.

    Args:
        name: Exported testing symbol name.

    Returns:
        The requested class or synthetic token constant.
    """
    from importlib import import_module

    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(".mock_ecn", __name__), name)


__all__ = [
    "FULL_ACCESS_TOKEN",
    "NO_ACCESS_TOKEN",
    "READ_ONLY_TOKEN",
    "MockECN",
    "MockEvents",
    "MockScenario",
]
