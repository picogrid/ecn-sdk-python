# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from typing import Any, cast

import pytest

from picogrid_ecn_client import (
    Entities,
    Locations,
    ValidationError,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
@pytest.mark.asyncio
async def test_entity_watch_rejects_nonpositive_or_noninteger_buffer(value: object) -> None:
    entities = Entities(cast(Any, object()))
    with pytest.raises(ValidationError, match="positive integer"):
        await entities.watch(buffer_size=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
@pytest.mark.asyncio
async def test_location_watch_rejects_nonpositive_or_noninteger_buffer(value: object) -> None:
    locations = Locations(cast(Any, object()))
    with pytest.raises(ValidationError, match="positive integer"):
        await locations.watch(buffer_size=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, "1", float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_location_wait_rejects_invalid_timeout(value: object) -> None:
    locations = Locations(cast(Any, object()))
    with pytest.raises(ValidationError, match="positive number"):
        await locations.wait_for_update(cast(Any, object()), timeout=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, "1", float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_terminal_geolocation_wait_rejects_invalid_timeout(value: object) -> None:
    locations = Locations(cast(Any, object()))
    with pytest.raises(ValidationError, match="positive number"):
        await locations.wait_for_terminal_geolocation(timeout=value)  # type: ignore[arg-type]
