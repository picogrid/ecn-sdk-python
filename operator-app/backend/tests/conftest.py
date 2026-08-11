# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def fail_on_unhandled_loop_exception() -> AsyncIterator[None]:
    """Turn delayed transport/task exceptions into deterministic test failures."""

    loop = asyncio.get_running_loop()
    captured: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    try:
        yield
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    if captured:
        messages = ", ".join(str(item.get("message", "unhandled exception")) for item in captured)
        pytest.fail(f"asyncio loop reported unhandled exceptions: {messages}")
